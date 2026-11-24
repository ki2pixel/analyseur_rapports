# Reporting Vidéos (`apps/reporting`)

Acte 2 du rituel mensuel : scanne les archives vidéo d’un mois donné, calcule les durées et
répartitions par catégorie, enrichit éventuellement les métadonnées depuis le batch BDC
(`data/batches/YYYY-MM.json`) et génère les rapports HTML et `rapport-YYYY-MM.json`.

Fait partie du monorepo ; préférer le venv et le `.env` **à la racine du dépôt**.

## Fonctionnement

1. **Entrées (`archives/`)** — dossiers attendus au format `<id> Camille YYYY-MM-DD_HH-MM-SS/`
   (l’heure reste optionnelle pour les anciens dossiers). Chaque sous-dossier valide contient
   un `metadata.json` (`video_path`, `video_stem` optionnel) et éventuellement un fichier
   `*_scenes.csv`. Seuls les sous-dossiers situés sous le dossier d’archive sont lus
   (`assert_under_directory`).
2. **Filtrage et déduplication** (`collect_video_entries_with_warnings` et
   [`libs/pipeline/video_names.py`](../../libs/pipeline/video_names.py)) :
   - **Format `.mp4` uniquement** (`ALLOWED_VIDEO_SUFFIXES = {".mp4"}`) : les `.mov` et autres
     extensions sont écartés avec un avertissement `Format ignoré : …`.
   - **Jingles M6+ exclus** via `is_excluded_video_name` (`m6+.mp4`, `m6_plus.mp4`,
     `m6+max anime.mp4`, etc.) avec un avertissement `Jingle M6+ ignoré : …`.
   - **Déduplication intra-lot** sur `normalize_video_name` (NFKC, espaces normalisés,
     casse ignorée) : seule la première occurrence (ordre alphabétique des sous-dossiers) est
     conservée ; les suivantes émettent `Doublon ignoré : …`.
   - **Suppression des lots vides** : un dossier sans vidéo exploitable remonte `Aucune vidéo
     valide dans le dossier …` et n’apparaît pas dans le rapport.
3. **Extraction des durées & cache** :
   - Mesure via `ffprobe` (timeout 15 s), avec repli sur le dernier `Timecode Out` valide de
     `*_scenes.csv` si la vidéo est inaccessible ou `ffprobe` absent.
   - Exécution parallélisée (`ThreadPoolExecutor`, plafond `REPORTING_MAX_WORKERS`, défaut `8`).
   - Cache persistant dans `.cache/durations.json` indexé par chemin résolu, taille et `mtime_ns`
     du fichier vidéo et des `*_scenes.csv`.
   - Les vidéos sans durée exploitable sont signalées (`Durée indéterminée : …`) et ignorées
     pour le comptage par catégorie.
4. **Désambiguïsation des lots multi-dossiers (`disambiguate_folders`)** :
   - Lorsqu’un même `(id_camille, person)` possède plusieurs dossiers dans le mois avec des
     jeux de fichiers distincts, chaque dossier reçoit un identifiant suffixé par sa date :
     `309 (2026-10-01)` (ou `309 (2026-10-01 10:24:42)` si plusieurs dossiers distincts ont la
     même date), afin d’éviter toute fusion silencieuse.
5. **Catégorisation (`CategoryThresholds`)** :
   - `short` : `< 120 s` (`< 2 min`)
   - `standard` : `[120, 300) s` (`2 à 5 min`)
   - `medium` : `[300, 480) s` (`5 à 8 min`)
   - `long` : `≥ 480 s` (`8 min et plus`)
6. **Fusion avec le batch BDC (`get_batch_for_month`)** :
   - L’application Flask scanne d’abord `archives/` (`create_batch_from_archives`). Si
     `data/batches/YYYY-MM.json` existe pour ce mois, ses commandes (`orders`), `source_files`
     et `warnings` enrichissent les métadonnées affichées, mais le scan des archives reste la
     source de vérité des vidéos produites.

## Lancer le serveur

```bash
# recommandation : dashboard unifié
./scripts/launch_services.sh

# ou service seul (écoute 127.0.0.1:5010)
make serve-reporting
```

Ouvrir `http://127.0.0.1:5010/?month=YYYY-MM` : `GET` pré-sélectionne le mois si des archives
ou un batch existent ; soumettre **Analyser le mois** (`POST /`) lance l’analyse.

## Interface & Exports

- **Barre de production empilée** : répartition visuelle `Courte` / `Standard` / `Moyenne` /
  `Longue` avec pourcentages et légende.
- **Accordéon « Synthèse .mp4 »** : total des `.mp4` et détail par lot (état replié/déplié
  mémorisé dans `localStorage` par mois).
- **Tiroir « Fichiers écartés ou atypiques »** : rassemble tous les avertissements du lot
  (jingles M6+, doublons, formats non `.mp4`, durées indéterminées, warnings du batch BDC).
- **Générer JSON** (`POST /generate-json?month=YYYY-MM`, protégé CSRF + auth + verrou
  `resource_lock`) : écrit atomiquement `apps/reporting/rapport-YYYY-MM.json`, consommé par
  la réconciliation (Acte 3) et la facturation (Acte 4).

## API

| Méthode | Route | Notes |
|---------|-------|--------|
| `GET`/`POST` | `/` | UI (`?month=YYYY-MM` pré-sélectionne le mois) |
| `GET`/`POST` | `/auth` | Saisie jeton (hors loopback) |
| `GET` | `/export?month=YYYY-MM` | Téléchargement HTML (`rapport-YYYY-MM.html`) via `report.html` |
| `GET` | `/export/json?month=YYYY-MM` | Téléchargement JSON (`rapport-YYYY-MM.json`) |
| `GET` | `/export/batch/json?month=YYYY-MM` | Téléchargement JSON enrichi (`rapport-batch-YYYY-MM.json` + `batch_info`) |
| `POST` | `/generate-json?month=YYYY-MM` | Auth + CSRF ; écrit `apps/reporting/rapport-YYYY-MM.json` (`409` si déjà en cours) |

## Structure du JSON généré (`rapport-YYYY-MM.json`)

```json
{
  "month": "2026-11",
  "total_mp4": 150,
  "group_totals": { "2 Camille": 3 },
  "report": [
    {
      "id_camille": "2",
      "person": "Camille",
      "categories": {
        "short": {
          "label": "vidéo courte (moins de 2 min)",
          "count": 3,
          "files": ["exemple.mp4"],
          "files_detail": [
            { "name": "exemple.mp4", "duration_sec": 71.2, "category": "short" }
          ]
        }
      }
    }
  ],
  "warnings": []
}
```

Les champs `files_detail` et `warnings` sont optionnels pour préserver la rétrocompatibilité
avec les rapports historiques.

## CLI

```bash
cd apps/reporting
python generate_report.py --month 2025-10 --out rapport_octobre.html
```

- `--month` : mois cible au format `YYYY-MM`
- `--out` : fichier HTML de sortie (défaut : `rapport.html`)

> Note : `generate_report.py` appelle directement `analyze_month()` sur `archives/` sans
> fusionner les métadonnées de `data/batches/YYYY-MM.json`.

## Tests associés

- `pytest tests/test_analyser_archives.py` : extraction, jingles M6+, déduplication, seuils 120/300/480 s, lots multi-dossiers.
- `pytest tests/test_analyze_batch.py` : agrégation de `MonthlyBatch` vers `VideoGroupReport`.
- `pytest tests/test_reporting_batches.py` : lecture de `data/batches/` et sérialisation.
- `pytest tests/test_november_mixed_formats.py` : vérifie notamment le palier `medium` (`460,52 s` sur le lot `166`).
