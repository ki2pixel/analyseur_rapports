# Réconciliation (`apps/reconciliation`)

Acte 3 du rituel mensuel : compare le rapport de production (`apps/reporting/rapport-YYYY-MM.json`)
avec l’export BDC (`apps/bdc_converter/convert_bdc_MM-YYYY.json`), calcule le score de conformité,
diagnostique les écarts jusqu’au niveau fichier, permet des ajustements en session puis fige un
snapshot pour la facturation (`data/reconciliation/YYYY-MM.json`).

Fait partie du monorepo ; préférer le venv et le `.env` **à la racine du dépôt**.

## Lancer le serveur

```bash
# recommandation : dashboard unifié
./scripts/launch_services.sh

# ou service seul (écoute 127.0.0.1:5030)
make serve-reconciliation
```

Ouvrir `http://127.0.0.1:5030/?month=YYYY-MM` : `GET` résout automatiquement la paire
`rapport-YYYY-MM.json` et `convert_bdc_MM-YYYY.json` via `list_available_reports()` et
`safe_join_filename()` (sans jamais construire de chemin depuis un `?month=` brut). Le panneau
« Sélection manuelle des deux fichiers JSON » (`POST /`) reste disponible pour des paires hors
convention mensuelle.

## Logique de comparaison (`libs/reconciliation/comparison.py`)

- **Convention d’écart** : `delta = actual - billed` (`Réalisé − Facturé`).
  - `0` : vert `.text-positive` (« En règle ») ;
  - `> 0` : ambre `.text-warn` (« non facturée ») ;
  - `< 0` : corail `.text-negative` (« manquante »).
- **Résolution des lots BDC (`_extract_reference_ids`)** :
  - Un BDC au format `detailed` expose `concerned_lots` : cette liste explicite prime sur
    l’expansion de la plage (ex. `LOTS 42 à 311`) afin de ne pas revendiquer des lots
    intermédiaires non commandés.
  - Pour les plages (`38 à 92`), l’expansion est intersectée avec les lots présents dans le
    rapport de production (largeur max `RECONCILIATION_MAX_RANGE_WIDTH`, défaut `5000`).
  - `BDC_IDENTIFIER_PATTERN` filtre les numéros `BDC-…` pour éviter tout lot fantôme.
- **Lots scindés (`_resolve_order_cluster_ids`)** : lorsque plusieurs dossiers portent le même
  lot de base (`309 (2026-10-01)` et `309 (2026-10-27)`), un BDC unique sur `309` reçoit tous
  les dossiers ; si plusieurs BDC référencent `309`, chaque dossier est alloué au BDC dont il
  complète le mieux la répartition de durées (limitant les faux chevauchements).
- **Score et certification** :
  - `score = compliant_orders / total_orders × 100` (arrondi à 1 décimale).
  - Un regroupement (cluster) est conforme si `missing_reporting_ids` est vide, tous les
    `delta` valent `0` et `billed_unknown == 0`, ou s’il a été **justifié** (doublon/jingle
    expliquant intégralement un surplus, sans volume manquant ni article non reconnu).
  - `certification.is_certified` vaut `True` uniquement si `score == 100.0` **et**
    `issues_count == 0` (aucun chevauchement, aucun lot sans BDC, aucun lot facturé sans
    dossier vidéo, aucun cluster non conforme).
- **Décalages inter-mois (`_load_adjacent_months`)** : charge automatiquement les mois `M−1`
  et `M+1` disponibles pour signaler les lots déjà présents ou facturés sur un mois voisin.

## Rapprochement vidéo détaillé et Option A

Lorsque le regroupement inclut un BDC détaillé avec `annex_videos`, `_reconcile_cluster_videos()`
rapproche l’annexe vidéo et la production lot par lot :

- **Clé de correspondance** : nom normalisé sans extension (`.mov` ↔ `.mp4` toléré, espaces
  avant extension nettoyés), puis repli dans le même lot sur le radical sans suffixe studio
  (`_TAT_M6`, `_TAT_W9`, `_TAT_Téva`, `_TAT_6ter`).
- **Statuts vidéo** :
  - `matched` : vidéo appariée dans la bonne catégorie ;
  - `duration_drift` : dérive de durée `> 5 s` (`ANNEX_DURATION_DRIFT_SECONDS = 5.0`) mais
    même catégorie — reste comptée dans `match_rate` et signalée par un badge `info` non bloquant ;
  - `category_mismatch` : catégorie facturée ≠ catégorie mesurée (`warn`) ;
  - `missing_in_production` : vidéo facturée en annexe absente de l’archive (`warn`) ;
  - `extra_in_production` : fichier produit dans le lot mais absent de l’annexe (`⚠ Non listée sur BDC`).
- **Doublons multi-lots légitimes** : si une annexe facture explicitement le même titre sur deux
  lots distincts (ex. lots `209` et `219`), la seconde occurrence est légitime et ne déclenche
  pas le diagnostic `jingle_or_duplicate`.
- **Option A (`compensated_lot_mismatch`)** : la conformité (`is_compliant`) et la part
  facturable restent évaluées au niveau du BDC. Des écarts vidéo qui se compensent entre lots
  d’un même BDC (delta agrégé nul) émettent un diagnostic `warn` non bloquant
  `compensated_lot_mismatch` (« Écarts internes compensés ») sans bloquer la certification.

## Inspecteur d’écarts & session en mémoire

L’inspecteur liste les onglets **Tous les BDC**, **Écarts à inspecter** et **Conformes ✓**
(repliés par défaut). Il remonte les diagnostics automatiques :

- `unknown_articles` : articles BDC non rattachés à une catégorie ;
- `jingle_or_duplicate` : jingle M6+ ou doublon (même nom normalisé, même catégorie, durée à
  `±1,0 s`) ;
- `near_boundary` : vidéo mesurée à moins de `3,0 s` des seuils `120 / 300 / 480 s` ;
- diagnostics d’annexe vidéo (`compensated_lot_mismatch`, `video_duration_drift`,
  `video_category_mismatch`, `video_missing_in_production`, `video_reconciled`) ;
- `bdc_overlap` et `adjacent_month`.

Les actions rapides sont **conservées en mémoire pour la session** (`_store`, TTL 2 h, max
50 analyses, empreinte disque vérifiée pour détecter toute modification des JSON sources) et ne
modifient jamais `rapport-*.json` ni `convert_bdc_*.json` :

- **Exclure** (`POST /inspector/exclude`) : scopes `occurrence` (1 exemplaire sur un lot),
  `lot` ou `month` (tous les lots pour un jingle) ;
- **Justifier** (`POST /inspector/justify`) : accepte un écart causé exclusivement par un
  doublon/jingle identifié (ou annule avec `undo=1`) ;
- **Réinitialiser** (`POST /inspector/reset`) : supprime toutes les exclusions et justifications
  de la session.

## Transmission à la facturation & Exports

- **Handoff (`POST /handoff/billing`)** : fige l’analyse effective (exclusions et
  justifications incluses) avec empreintes SHA-256 des sources dans
  `data/reconciliation/YYYY-MM.json` (écriture atomique + `resource_lock`). Refuse de
  transmettre silencieusement si le jeton de session a expiré ou si les sources ont changé.
- **Export JSON (`GET /export/json`)** : télécharge l’analyse complète (`cluster_results` +
  `cluster_differences`).
- **Export HTML (`GET /export/html`)** : document autonome (`export_styles.css` inliné,
  polices embarquées via `font_uri()`, sans JS, `EXPORT_CSP` dédiée). Affiche l’encart
  **Rapprochement vidéo — BDC détaillé** (`video-strip`) dès qu’au moins un BDC détaillé est
  présent. Nommé `certificat-rapprochement-…` lorsque `is_certified=True`.
- **Export PDF (`GET /export/pdf`)** : réutilise `export.html` en mode PDF (`pdf_mode=True`)
  avec `export_pdf_styles.css` via WeasyPrint : page unique continue calculée par deux passes
  de mesure, plancher A4 et mise à l’échelle uniforme sous la limite PDF 1.7 de `14 400 pt`.

## API

| Méthode | Route | Notes |
|---------|-------|--------|
| `GET`/`POST` | `/` | UI (`?month=YYYY-MM` auto-charge la paire mensuelle) |
| `GET`/`POST` | `/auth` | Saisie jeton (hors loopback) |
| `POST` | `/inspector/exclude` | Auth + CSRF ; exclusion en session (`occurrence` / `lot` / `month`) |
| `POST` | `/inspector/justify` | Auth + CSRF ; justification en session (`undo=1` pour annuler) |
| `POST` | `/inspector/reset` | Auth + CSRF ; remet la session à zéro |
| `POST` | `/handoff/billing` | Auth + CSRF ; écrit `data/reconciliation/YYYY-MM.json` |
| `GET` | `/export/json` | Export JSON |
| `GET` | `/export/html` | Export HTML autonome |
| `GET` | `/export/pdf` | Export PDF WeasyPrint page unique |

## Tests associés

- `pytest tests/test_reconciliation_logic.py` : comparaison, plages, lots scindés, rapprochement vidéo détaillé, Option A, seuils 480 s.
- `pytest tests/test_reconciliation_regressions.py` : non-régression juillet / septembre.
- `pytest tests/test_reconciliation_app.py` : routes Flask, sessions, inspecteur, exports HTML/PDF/JSON et handoff facturation.
- `pytest tests/test_november_mixed_formats.py` : scénario mixte simplifié + détaillé de novembre 2026.
