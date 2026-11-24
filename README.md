# Analyseur de rapports vidéos (Camille)

Monorepo Flask local pour :

- **Convertisseur BDC** — extraction PDF → JSON + batch mensuel partagé (formats simplifié et détaillé avec annexes), cartes de contrôle et alertes de chevauchement.
- **Reporting** — rapport mensuel des vidéos dans `archives/` (UI + exports HTML/JSON).
- **Rapprochement** — comparaison reporting ↔ BDC (dont rapprochement vidéo lot par lot), score de conformité, inspecteur d’écarts et export certifié.
- **Facturation** — base de facturation versionnée à partir des BDC et du rapprochement, contrôle des écarts, export HTML/PDF/CSV et suivi du numéro Indy.
- **Dashboard** — démarrage / arrêt des quatre services (`./scripts/launch_services.sh`).

Le parcours cible est un **rituel mensuel en 4 actes** : réception des commandes BDC, scan de
production vidéo, équilibrage et certification du rapprochement, puis préparation et envoi de
la facture. Un sélecteur de mois global (`?month=YYYY-MM`) synchronise les quatre services.

Les dossiers d'archives sont attendus au format `<id> Camille YYYY-MM-DD_HH-MM-SS/`
(ex. `273 Camille 2025-10-28_10-24-42/`), avec `metadata.json` (`video_path`, …) et
éventuellement `*_scenes.csv` en secours si `ffprobe` est absent. Seuls les fichiers `.mp4`
sont analysés : les `.mov` et tout autre format sont ignorés, ainsi que toutes les variantes
de jingles M6+ (`m6+.mp4`, `m6_plus.mp4`, `m6+max anime.mp4`, …). Les doublons de capture
d’un même lot sont dédupliqués automatiquement.

## Installation

Prérequis : Python 3.10+, `ffprobe` recommandé (`ffmpeg`). L’extra `[pdf]` installe
`pdfplumber` (extraction BDC) et `weasyprint` (exports PDF de réconciliation et de facturation).

```bash
python3 -m venv venv
source venv/bin/activate          # Windows : .\venv\Scripts\activate
pip install --upgrade pip
make install-dev                  # ou : pip install -e .[pdf,dev,cli]
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

Dashboard : `http://127.0.0.1:5500/` — boutons pour Convertisseur BDC (`5000`),
Reporting (`5010`), Rapprochement (`5030`), Facturation (`5040`).

Overrides optionnels : `SERVICE_DASHBOARD_HOST`, `SERVICE_DASHBOARD_PORT`,
`SERVICE_DASHBOARD_BROWSER=0/1`.

### Un service à la fois

```bash
make serve-bdc            # http://127.0.0.1:5000/
make serve-reporting      # http://127.0.0.1:5010/
make serve-reconciliation # http://127.0.0.1:5030/
make serve-billing        # http://127.0.0.1:5040/
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
`RECONCILIATION_URL`, `BILLING_URL`, `BDC_MAX_PDF_FILES`, `BDC_MAX_PDF_BYTES`,
`REPORTING_MAX_WORKERS`.

Recharger les polices locales : `./scripts/fetch_fonts.sh`.

## Workflow métier — le rituel mensuel

1. **Choisir le mois cible** dans le sélecteur global de la barre supérieure. Le paramètre
   `?month=YYYY-MM` est propagé aux quatre services et conservé dans les URLs.
2. **Acte 1 — Réception commandes (BDC)** : déposer les PDF dans `apps/bdc_converter/pdfs/`
   (dossier local exclu du suivi Git via `.gitignore`), puis lancer la conversion.
   L’extracteur gère deux formats (`simplified` sur une page et `detailed` avec page 1
   récapitulative, `Période`, `Lots concernés` et `Annexe : liste des vidéos`) ainsi que les
   numéros BDC à suffixe variable (`BDC-26-1882`, `BDC-M6-0001`, …). Chaque PDF produit une
   carte avec son format (« Simplifié » / « Détaillé »), ses lots détectés et les badges
   « Plage détectée », « Chevauchement » ou « Structure modifiée » (dérive de template non
   bloquante). Sorties : `convert_bdc_MM-YYYY.json` + batch `data/batches/YYYY-MM.json`.
3. **Acte 2 — Scan production (Reporting)** : analyser le mois puis **Générer JSON** →
   `apps/reporting/rapport-YYYY-MM.json`. La barre empilée montre la répartition des durées ;
   le tiroir « Fichiers écartés ou atypiques » liste les jingles M6+, doublons, formats non
   `.mp4` et vidéos sans durée. Si un batch BDC existe pour le mois, ses commandes, sources
   et avertissements enrichissent les métadonnées, mais le scan des archives reste la source
   des vidéos produites.
4. **Acte 3 — Équilibrage & victoire (Réconciliation)** : ouvrir `/?month=YYYY-MM` charge
   automatiquement `rapport-YYYY-MM.json` et `convert_bdc_MM-YYYY.json`. Le score, les onglets
   et l’inspecteur guident la résolution ; les exports HTML, PDF et JSON sont disponibles.
   Le bouton **« Transmettre à la facturation »** fige l’analyse effective (ajustements inclus)
   dans `data/reconciliation/YYYY-MM.json`.
5. **Acte 4 — Facturation & envoi** : ouvrir `http://127.0.0.1:5040/?month=YYYY-MM`. Le service
   calcule un brouillon à partir des BDC et du snapshot de réconciliation, propose une ligne par
   BDC et une provision « Lots réalisés sans BDC », puis permet de geler des versions, de suivre
   les statuts, d’enregistrer le numéro Indy et d’archiver le PDF Indy. Il **ne produit pas la
   facture légale** : le PDF officiel reste émis dans Indy. Les exports HTML/PDF sont des
   récapitulatifs de préparation ; le CSV sert à la saisie ou à l’import.

### Lire le rapprochement

- **Score de conformité** : `BDC conformes / total BDC × 100`. Un BDC est conforme lorsque
  les volumes correspondent exactement, ou lorsqu’un écart de doublon/jingle a été justifié ;
  les volumes manquants ne peuvent pas être justifiés. La certification 100 % exige aussi zéro
  signalement bloquant (chevauchement BDC, lot facturé sans production, production en attente
  de BDC).
- **Rapprochement vidéo détaillé** : lorsqu’un BDC est au format `detailed`, l’annexe vidéo
  est comparée lot par lot aux fichiers `.mp4` produits. L’appariement ignore l’extension
  (`.mov` ↔ `.mp4`) et dispose d’un repli sur le radical sans suffixe studio (`_TAT_M6`,
  `_TAT_W9`, `_TAT_Téva`, `_TAT_6ter`) au sein du même lot. Statuts et badges : `✓ Annexe`,
  `ℹ .mov ↔ .mp4`, `ℹ Radical studio`, dérive de durée (`> 5 s` dans la même catégorie,
  badge `info` non bloquant), `catégorie divergente`, `facturée absente de l’archive` et
  `⚠ Non listée sur BDC`. Lorsqu’un BDC détaillé facture explicitement un même titre sur deux
  lots distincts (ex. lots 209 et 219), aucun faux doublon n’est signalé. L’export HTML/PDF
  affiche un encart récapitulatif dédié dès qu’au moins un BDC détaillé est présent.
- **Écarts internes compensés (Option A)** : la conformité et la part facturable (`actual_capped`)
  restent évaluées au niveau du BDC. Lorsque des écarts vidéo se compensent entre lots au sein
  d’un même BDC (delta global nul par catégorie), un diagnostic `compensated_lot_mismatch`
  (« Écarts internes compensés ») est émis sans déplacer de volume en provision ni bloquer la
  certification.
- **Lots scindés** : lorsqu’un même numéro de lot correspond à plusieurs dossiers (ex. `309`
  des 1er et 27 octobre), chaque BDC est rattaché au dossier dont la répartition de durées
  complète le mieux ses quantités facturées. Un seul BDC sur ce lot reçoit tous les dossiers ;
  deux BDC distincts peuvent donc se répartir les dossiers sans faux chevauchement.
- **Écart** = `Réalisé − Facturé` : `0` = vert « En règle », `> 0` = ambre « non facturée »,
  `< 0` = corail « manquante ».
- **Onglets** : Tous les BDC / Écarts à inspecter / Conformes. Les BDC conformes sont repliés
  par défaut.
- **Inspecteur d’écarts** : vue Attendu BDC vs Réalisé production, fichiers et durées,
  diagnostics automatiques (jingle/doublon, seuil ±3 s, rapprochement annexe / dérive > 5 s /
  écarts internes compensés, chevauchement BDC, décalage M−1/M+1).
- **Actions rapides** : exclure un jingle/doublon ou justifier un doublon identifié, avec
  recalcul immédiat du score et des exports. L’exclusion cible un lot et un regroupement BDC
  précis ; pour un doublon, seule l’occurrence en surplus est retirée, tandis qu’un jingle est
  exclu sur tout le mois. Un doublon suppose le même nom normalisé, la même catégorie et une
  durée à ±1 s. La justification n’est proposée que si le doublon ou le jingle explique
  entièrement l’écart. Ces ajustements sont **en mémoire pour la session** ; un rechargement
  sans token repart des fichiers sources.

La sélection manuelle des deux JSON reste disponible dans le panneau « Sélection manuelle »
pour les cas hors mois standard.

### Lire la facturation

- **Base de calcul** : par défaut, une ligne BDC vaut `min(réalisé, commandé) × taux` ; le
  surplus de production part dans la ligne « Lots réalisés sans BDC ». Le mode « Au commandé »
  facture le total du BDC même si la production est inférieure.
- **Formats BDC & Option A** : les lignes BDC affichent le format (`Simplifié` ou
  `Détaillé (N vidéos)`) et le taux de correspondance annexe (`Annexe : XX %`). Les écarts
  internes compensés entre lots apparaissent en point d’attention informatif
  (`compensated_lot_mismatch`) sans modifier le montant ni générer de provision.
- **Provision & lignes manuelles** : la ligne « Lots réalisés sans BDC » agrège les surplus sur
  BDC couverts et les lots sans BDC, avec le détail par catégorie. En brouillon, toute ligne
  (BDC, provision ou ligne manuelle ajoutée) est modifiable ou peut être exclue ; les lots
  déjà facturés sur un mois voisin sont exclus de la provision.
- **Versions** : un brouillon est modifiable ; une version gelée est immuable. Une correction
  après envoi crée une nouvelle version et le diff s’affiche automatiquement.
- **Statuts** : `brouillon` → (`envoyé_pour_vérification` →) `validé_client` → `émis` →
  `remplacé`, avec `annulé` possible depuis `envoyé_pour_vérification` ou `validé_client`.
  Le passage en `validé_client` exige l’acquittement des avertissements ; le passage en `émis`
  exige la certification de réconciliation ou un override justifié, ainsi que le numéro et la
  date Indy. La TVA est paramétrable (défaut `0.00 %`, art. 293 B du CGI) et le PDF Indy peut
  être attaché à la version (6 Mo max).
- **Sources** : le snapshot de réconciliation est prioritaire ; sans lui, le calcul est refait
  avec `compare_reports()` et un avertissement « snapshot absent » apparaît. Chaque version
  archive ses sources et leurs empreintes SHA-256.
- **Ce que le service ne fait pas** : il ne génère pas la facture légale, ne tient pas de
  grand livre et ne gère pas les avoirs. Le PDF officiel reste produit par Indy ; le service
  prépare, contrôle, versionne et archive le PDF Indy une fois émis.

Documentations détaillées par application :

- Acte 1 — Convertisseur BDC : [`apps/bdc_converter/README.md`](apps/bdc_converter/README.md)
- Acte 2 — Reporting Vidéos : [`apps/reporting/README.md`](apps/reporting/README.md)
- Acte 3 — Réconciliation : [`apps/reconciliation/README.md`](apps/reconciliation/README.md)
- Acte 4 — Facturation (scénarios et check-list, aussi accessible via **« Guide »** dans l’UI) : [`apps/billing/README.md`](apps/billing/README.md)

## Générer un rapport HTML (CLI)

```bash
cd apps/reporting
python generate_report.py --month 2025-10 --out rapport_octobre.html
```

- `--month` au format `YYYY-MM`
- `--out` chemin HTML de sortie (défaut : `rapport.html`)

## Catégorisation des durées

- courte : `< 2 min` (`< 120 s`)
- standard : `[2, 5) min` (`120 à 300 s`)
- moyenne : `[5, 8) min` (`300 à 480 s`)
- longue : `≥ 8 min` (`480 s` et plus)

## Structure du monorepo

```
apps/
  bdc_converter/     # PDF → JSON + batch + cartes de contrôle
  reporting/         # archives / batches → rapports + tiroir des fichiers atypiques
  reconciliation/    # score, inspecteur d’écarts, exports certifiés
  billing/           # base de facturation versionnée, contrôles et récapitulatifs
  static/            # CSS (global, dashboard, print-report), polices woff2 et JS (global, dashboard, bdc_convert, reporting, reconciliation)
  templates/         # base.html + _ritual_stepper.html
libs/
  schemas/           # contrat dataclass partagé
  pipeline/          # BDC → MonthlyBatch + video_names.py (jingles/doublons)
  reconciliation/    # plages contextualisées, chevauchements, vidéo détaillée, score, diagnostics
  billing/           # calcul de la base, grille de taux, versions, diff, argent (Decimal)
  web/               # auth, CSRF, headers, I/O, chemins sûrs
scripts/
  launch_services.sh
  service_dashboard.py
  fetch_fonts.sh
  templates/dashboard.html
tests/               # tests pytest (extraction BDC, reporting, réconciliation, facturation, sécurité) + fixtures/bdc/
data/
  batches/           # batches BDC mensuels
  reconciliation/    # snapshots de certification transmis à la facturation
  billing/           # projets, versions gelées, sources archivées
audits/              # audits datés historiques (ex. audit-backend-…)
.env.example
Makefile
pyproject.toml
```

## Schéma pivot partagé

Les dataclasses de [`libs/schemas`](libs/schemas) forment le contrat entre conversion BDC
et reporting : `MonthlyBatch`, `PurchaseOrder` (avec `bdc_format`, `period`,
`concerned_lots` et `annex_videos`), `AnnexeVideoItem`, `VideoEntry`, `VideoCategory`,
`VideoGroupCategory`, `VideoGroupReport` et `SourceFile`.
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
- Le script CLI `apps/reporting/generate_report.py` appelle `analyze_month()` directement sur
  `archives/` sans fusionner les métadonnées `data/batches/YYYY-MM.json`, contrairement à
  l’application Flask reporting (`get_batch_for_month()`).
- Les rapports historiques sans `files_detail` restent lisibles, mais affichent les durées comme
  inconnues ; les diagnostics de seuil temporel s’appliquent aux rapports régénérés. De même,
  les JSON générés avant l’alignement du seuil à `480 s` (comme `apps/reporting/rapport-2026-10.json`)
  conservent leurs libellés d’origine (`5 et 7 minutes` / `7 min et plus`) tant qu’ils ne sont pas
  régénérés.
- Les actions de l’inspecteur (exclure un fichier, justifier un doublon) sont conservées en mémoire
  pour la session uniquement ; elles ne modifient jamais les JSON sources.
- Plafonds et téléversements : `BDC_MAX_PDF_FILES=200` et `BDC_MAX_PDF_BYTES=20 Mo` côté
  conversion BDC ; `6 Mo` max (PDF `%PDF` uniquement) pour l’attachement de facture Indy côté
  facturation.
- La facturation ne produit pas de facture légale et ne gère pas les avoirs : le PDF officiel
  reste émis dans Indy. Elle prépare, versionne et archive la base ; une correction après
  émission suppose un avoir émis hors de l’outil.
- Exposition hors machine locale : conserver CSRF + jeton ; ne pas désactiver `TRUST_LOOPBACK_AUTH` à la légère si le port n’est plus uniquement loopback.
