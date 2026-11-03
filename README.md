# Analyseur de rapports vidéos (Camille)

Monorepo Flask local pour :

- **Reporting** — rapport mensuel des vidéos dans `archives/` (UI + exports HTML/JSON).
- **Convertisseur BDC** — extraction PDF → JSON + batch mensuel partagé, avec cartes de contrôle et alertes de chevauchement.
- **Rapprochement** — comparaison reporting ↔ BDC, score de conformité, inspecteur d’écarts et export certifié.
- **Dashboard** — démarrage / arrêt des trois services (`./scripts/launch_services.sh`).

Le parcours cible est un **rituel mensuel en 3 actes** : réception des commandes BDC, scan de
production vidéo, puis équilibrage et certification du rapprochement. Un sélecteur de mois
global (`?month=YYYY-MM`) synchronise les trois services.

Les dossiers d'archives sont attendus au format `<id> Camille YYYY-MM-DD_HH-MM-SS/`
(ex. `273 Camille 2025-10-28_10-24-42/`), avec `metadata.json` (`video_path`, …) et
éventuellement `*_scenes.csv` en secours si `ffprobe` est absent. Seuls les fichiers `.mp4`
sont analysés : les `.mov` et tout autre format sont ignorés, ainsi que toutes les variantes
de jingles M6+ (`m6+.mp4`, `m6_plus.mp4`, `m6+max anime.mp4`, …). Les doublons de capture
d’un même lot sont dédupliqués automatiquement.

## Installation

Prérequis : Python 3.10+, `ffprobe` recommandé (`ffmpeg`).

```bash
python3 -m venv venv
source venv/bin/activate          # Windows : .\venv\Scripts\activate
pip install --upgrade pip
make install-dev                  # ou : pip install -r requirements.txt && pip install -e .[pdf,dev]
```

Sous Linux (exemple Debian/Ubuntu) :

```bash
sudo apt update && sudo apt install -y python3-venv ffmpeg
```

### Configuration secrets (une fois)

```bash
cp .env.example .env
# renseigner ADMIN_TOKEN et FLASK_SECRET_KEY
```

Le fichier `.env` n’est pas versionné. `./scripts/launch_services.sh` et les apps Flask
le chargent automatiquement (`python-dotenv`).

## Lancer les applications

### Tableau de bord (recommandé)

```bash
./scripts/launch_services.sh
```

Dashboard : `http://127.0.0.1:5500/` — boutons pour Reporting (`5010`), BDC (`5000`),
Rapprochement (`5030`).

Overrides optionnels : `SERVICE_DASHBOARD_HOST`, `SERVICE_DASHBOARD_PORT`,
`SERVICE_DASHBOARD_BROWSER=0/1`.

### Un service à la fois

```bash
make serve-reporting      # http://127.0.0.1:5010/
make serve-bdc            # http://127.0.0.1:5000/
make serve-reconciliation # http://127.0.0.1:5030/
```

Tous bindent `127.0.0.1` par défaut ; debug désactivé sauf `APP_DEBUG=1` / `FLASK_DEBUG=1`.

## Sécurité et déploiement

Usage prévu : **outil local / intranet restreint**. Les services ne sont pas prêts pour
une exposition Internet publique sans durcissement supplémentaire.

| Mode | Hôte | Notes |
|------|------|--------|
| Local (défaut) | `127.0.0.1` | Recommandé. Auth loopback automatique (`TRUST_LOOPBACK_AUTH`, défaut actif). |
| Intranet | proxy + éventuellement `APP_HOST=0.0.0.0` | Reverse proxy de confiance uniquement. |
| Prod publique | — | Non supporté tel quel (SSO/RBAC, TLS, rate-limit, etc.). |

Protections en place :

- Secret partagé `ADMIN_TOKEN` (cookie `admin_token` ou en-tête `X-Admin-Token`) ; page `/auth` hors loopback.
- CSRF sur les `POST` (formulaires + `X-CSRFToken` pour les `fetch`).
- Headers de sécurité (CSP, `X-Frame-Options`, etc.). Polices self-hostées sous `/static/fonts/` (`font-src 'self'`). La CSP garde encore `style-src 'unsafe-inline'` tant que des styles dynamiques / export print existent (largeurs de jauges et barres) ; objectif : classes CSS uniquement.
- Chemins de fichiers bornés (rapprochement : whitelist + `resolve()`).
- Écritures atomiques + verrous ; messages d’erreur génériques côté client.
- Assets `/static` : `Cache-Control` + query `?v=` (mtime) via `static_url()` dans les templates.

Variables utiles : `APP_HOST`, `APP_DEBUG`, `TRUST_LOOPBACK_AUTH`, `BDC_URL`, `REPORTING_URL`,
`RECONCILIATION_URL`, `BDC_MAX_PDF_FILES`, `BDC_MAX_PDF_BYTES`, `REPORTING_MAX_WORKERS`.

Recharger les polices locales : `./scripts/fetch_fonts.sh`.

## Workflow métier — le rituel mensuel

1. **Choisir le mois cible** dans le sélecteur global de la barre supérieure. Le paramètre
   `?month=YYYY-MM` est propagé aux trois services et conservé dans les URLs.
2. **Acte 1 — Réception commandes (BDC)** : déposer les PDF dans `apps/bdc_converter/pdfs/`,
   lancer la conversion. Chaque PDF produit une carte avec son numéro BDC, ses lots détectés
   et un badge « Plage détectée », « Chevauchement » ou « Structure modifiée » (dérive de
   template non bloquante). Sorties : `convert_bdc_MM-YYYY.json` + batch
   `data/batches/YYYY-MM.json`.
3. **Acte 2 — Scan production (Reporting)** : analyser le mois puis **Générer JSON** →
   `apps/reporting/rapport-YYYY-MM.json`. La barre empilée montre la répartition des durées ;
   le tiroir « Fichiers écartés ou atypiques » liste les jingles M6+, doublons, formats non
   `.mp4` et vidéos sans durée. Si un batch BDC existe pour le mois, ses commandes, sources
   et avertissements enrichissent les métadonnées, mais le scan des archives reste la source
   des vidéos produites.
4. **Acte 3 — Équilibrage & victoire (Réconciliation)** : ouvrir `/?month=YYYY-MM` charge
   automatiquement `rapport-YYYY-MM.json` et `convert_bdc_MM-YYYY.json`. Le score, les onglets
   et l’inspecteur guident la résolution ; les exports HTML, PDF et JSON sont disponibles.

### Lire le rapprochement

- **Score de conformité** : `BDC conformes / total BDC × 100`. Un BDC est conforme lorsque
  les volumes correspondent exactement, ou lorsqu’un écart de doublon/jingle a été justifié ;
  les volumes manquants ne peuvent pas être justifiés. La certification 100 % exige aussi zéro
  signalement bloquant (chevauchement BDC, lot facturé sans production, production en attente
  de BDC).
- **Lots scindés** : lorsqu’un même numéro de lot correspond à plusieurs dossiers (ex. `309`
  des 1er et 27 octobre), chaque BDC est rattaché au dossier dont la répartition de durées
  complète le mieux ses quantités facturées. Un seul BDC sur ce lot reçoit tous les dossiers ;
  deux BDC distincts peuvent donc se répartir les dossiers sans faux chevauchement.
- **Écart** = `Réalisé − Facturé` : `0` = vert « En règle », `> 0` = ambre « non facturée »,
  `< 0` = corail « manquante ».
- **Onglets** : Tous les BDC / Écarts à inspecter / Conformes. Les BDC conformes sont repliés
  par défaut.
- **Inspecteur d’écarts** : vue Attendu BDC vs Réalisé production, fichiers MP4 et durées,
  diagnostic automatique (jingle/doublon, seuil ±3 s, chevauchement BDC, décalage M−1/M+1).
- **Actions rapides** : exclure un jingle/doublon ou justifier un doublon identifié, avec
  recalcul immédiat du score et des exports. L’exclusion cible un lot et un regroupement BDC
  précis ; pour un doublon, seule l’occurrence en surplus est retirée, tandis qu’un jingle est
  exclu sur tout le mois. Un doublon suppose le même nom normalisé, la même catégorie et une
  durée à ±1 s. La justification n’est proposée que si le doublon ou le jingle explique
  entièrement l’écart. Ces ajustements sont **en mémoire pour la session** ; un rechargement
  sans token repart des fichiers sources.

La sélection manuelle des deux JSON reste disponible dans le panneau « Sélection manuelle »
pour les cas hors mois standard.

Détails convertisseur : [`apps/bdc_converter/README.md`](apps/bdc_converter/README.md).

## Générer un rapport HTML (CLI)

```bash
cd apps/reporting
python generate_report.py --month 2025-10 --out rapport_octobre.html
```

- `--month` au format `YYYY-MM`
- `--out` chemin HTML de sortie (défaut : `rapport.html`)

## Catégorisation des durées

- courte : `< 2 min`
- standard : `[2, 5) min`
- moyenne : `[5, 7) min`
- longue : `≥ 7 min` (420 s et plus)

## Structure du monorepo

```
apps/
  bdc_converter/     # PDF → JSON + batch + cartes de contrôle
  reporting/         # archives / batches → rapports + tiroir des fichiers atypiques
  reconciliation/    # score, inspecteur d’écarts, exports certifiés
  static/            # CSS/JS partagés (global.js, bdc_convert.js, reconciliation.js)
  templates/         # base.html + _ritual_stepper.html
libs/
  schemas/           # contrat dataclass partagé
  pipeline/          # BDC → MonthlyBatch + video_names.py (jingles/doublons)
  reconciliation/    # plages contextualisées, chevauchements, score, diagnostics
  web/               # auth, CSRF, headers, I/O, chemins sûrs
scripts/
  launch_services.sh
  service_dashboard.py
audits/              # audits (ex. audit-backend-…)
.env.example
Makefile
```

## Schéma pivot partagé

Les dataclasses de [`libs/schemas`](libs/schemas) forment le contrat entre conversion BDC
et reporting : `MonthlyBatch`, `PurchaseOrder`, `VideoEntry`, `VideoCategory`, etc.
Les rapports `rapport-YYYY-MM.json` récents ajoutent `files_detail` (nom, durée, catégorie)
et `warnings` (fichiers écartés) ; ces champs sont **optionnels** pour préserver la
compatibilité avec les rapports historiques.

## Commandes utiles

| Commande | Rôle |
|----------|------|
| `make install-dev` | Dépendances + outils dev |
| `make test` | pytest |
| `make lint` | mypy |
| `make format` | black |
| `make serve-*` | services Flask sur loopback |

## Limitations connues

- Sans `ffprobe` ni `*_scenes.csv`, les vidéos sans durée sont ignorées pour la catégorisation
  et remontées dans le tiroir « Fichiers écartés ou atypiques ».
- `video_path` peut pointer hors du dépôt (ex. `workflow_mediapipe/projets_extraits/...`) : seul le
  sous-dossier (hash) lu doit rester sous le dossier d'archive, et seuls les `.mp4` sont analysés.
- Les rapports historiques sans `files_detail` restent lisibles, mais affichent les durées comme
  inconnues ; les diagnostics de seuil temporel s’appliquent aux rapports régénérés.
- Les actions de l’inspecteur (exclure un fichier, justifier un doublon) sont conservées en mémoire
  pour la session uniquement ; elles ne modifient jamais les JSON sources.
- Exposition hors machine locale : conserver CSRF + jeton ; ne pas désactiver `TRUST_LOOPBACK_AUTH` à la légère si le port n’est plus uniquement loopback.
