# Convertisseur BDC (`apps/bdc_converter`)

Extrait les informations des bons de commande PDF (formats simplifié et détaillé avec annexe) et produit :

1. un JSON mensuel `convert_bdc_MM-YYYY.json` (consommé par le rapprochement et la facturation) ;
2. un `MonthlyBatch` partagé dans `data/batches/YYYY-MM.json` (consommé par le reporting).

Fait partie du monorepo ; préférer le venv et le `.env` **à la racine du dépôt**.

## Fonctionnement

1. **Scan de `pdfs/`** — chaque PDF est traité par `convert_bdc.py` (`apps/bdc_converter/pdfs/` est
   exclu du suivi Git via `.gitignore`).
2. **Métadonnées & format (`bdc_format`)** — numéro BDC au motif `^BDC-(?:[A-Z0-9]{1,12}-)+\d{1,8}$`
   (gère aussi bien `BDC-25-1873`, `BDC-26-1882` que `BDC-M6-0001` ou `BDC-2026-M6-0001`),
   dates `JJ/MM/AAAA`, `reference` / `references` (découpe sur `/`, `\`, `;`, `,`, `et`).
   Les plages `38-92`, `38 au 92` ou `38 à 92` sont normalisées en `LOTS 38 à 92` ; le tiret
   n’est plus interprété comme un simple séparateur de lots. Les listes coupées autour de
   `N° de réf :` sont recollées, y compris lorsqu’elles se poursuivent sur la ligne suivante,
   et les plages mixtes (`LOTS 41-50/55`) conservent toutes leurs bornes.
   Si un libellé change (`Référence :`, `Date de commande :`, `Livraison :`), un repli par
   coordonnées retrouve le numéro BDC et les dates à partir de la ligne du libellé.
   - **`"simplified"`** : bon de commande historique sur une page (`# | Description | Quantité | Taux | Montant`, lots dans `N° de réf :`).
   - **`"detailed"`** : page 1 récapitulative exposant `period` (`Période : …`, hors mention `SIRET`),
     `concerned_lots` (bloc `Lots concernés (N)` qui prime sur une plage large type `LOTS 42 À 311`),
     `concerned_lots_declared_count`, `annex_declared_count` (`Détail des N vidéos en annexe`),
     et une ou plusieurs pages `Annexe : liste des vidéos`.
3. **Articles (page 1)** — extraction **par coordonnées** : la ligne d’en-tête (`Description`, `Quantité`,
   `Taux`, `Montant`, avec synonymes `Désignation`, `Qté`, `P.U.`, `Prix`, `Total HT`…)
   définit les frontières de colonnes, puis chaque mot est affecté par recouvrement horizontal.
   Cela couvre les tableaux **sans filets**, les colonnes réordonnées ou supplémentaires
   (une colonne inconnue est ignorée) et les descriptions **multi-lignes**. Les unités
   (`Unité`, `Heure`, `Forfait`…) sont rattachées à la ligne d’article précédente.
   Repli sur `pdfplumber.extract_tables(TABLE_SETTINGS)` si aucun en-tête n’est détecté.
   Les synonymes sont centralisés dans `HEADER_SYNONYMS` / `UNIT_WORDS` (`convert_bdc.py`).
   Les montants à séparateur de milliers (`1 056,00`) sont reconnus, et une ligne « Total »
   n’est ignorée que sur frontière de mot (une description « Totally Spies » reste un article).
   Les pages d’annexe sont explicitement exclues de cette boucle pour ne jamais polluer `articles`.
4. **Annexe vidéo (format `detailed`)** — `_extract_annex_items` repère les pages commençant par
   `Annexe` et détecte l’en-tête `Lot | Vidéo | Durée | Catégorie | Montant`. Chaque ligne vidéo est
   extraite de manière **pilotée par les types** de droite à gauche (`montant` → `catégorie` →
   `durée` `N min SS` ou `N s` → `titre`, avec lot numérique en première colonne) plutôt que par
   recouvrement horizontal, afin qu’un jeton comme le `0` de `0 min 18` ne soit pas mal affecté.
   Les lignes sans numéro de lot en début de ligne sont recollées au titre précédent, les espaces
   parasites avant extension (`.mp4`, `.mov`, `.mxf`, `.avi`, `.mkv`) sont supprimés, et les folios
   isolés de pied de page ainsi que les lignes récapitulatives (`N × …`, `Total des vidéos : …`)
   sont filtrés et synthétisés dans `annex_summary` (`total_videos`, `total_ht`, `category_rows`, `pages`).
5. **Validation** — chaque document reçoit un `status` (`ok`, `partial`, `failed`) et une liste
   de `warnings` : numéro BDC, dates `JJ/MM/AAAA` strictes, présence d’articles, cohérence
   `quantité × taux = montant`, `Σ montants = total`. Pour le format détaillé,
   `_validate_detailed_format()` ajoute 7 contrôles croisés non bloquants (`warning`) :
   `lots_concernes_incoherents`, `annexe_lots_hors_perimetre`, `annexe_volume_incoherent`,
   `annexe_quantite_incoherente`, `annexe_montant_incoherent`, `annexe_categorie_incoherente` et
   `annexe_total_incoherent`. Un document `failed` est exclu du batch ; un document `partial`
   reste exploitable mais signalé dans l’UI et le JSON.
6. **Détection de dérive** — la ligne d’en-tête de la première page produit une empreinte de
   template (`layout.fingerprint`) : noms canoniques des colonnes reconnues, libellé normalisé
   pour une colonne inconnue, dans l’ordre gauche-droite. Elle est comparée à
   `EXPECTED_LAYOUT_FINGERPRINT` (`index | description | quantite | taux | montant`).
   Toute différence (colonne ajoutée/supprimée/déplacée, en-tête non reconnu) émet un
   avertissement **informatif** `structure_modifiee` et affiche le badge « Structure modifiée »
   sur la carte du PDF. Cela ne change pas le `status` : la conversion reste non bloquante.
   Les synonymes (`Désignation`, `Qté`, `P.U.`, `Total HT`…) ne déclenchent pas de dérive
   puisqu’ils sont ramenés aux noms canoniques.
7. **Persistance** — nom de sortie **toujours** dérivé du mois majoritaire des dates valides :
   `convert_bdc_MM-YYYY.json` (pas de `output_name` libre côté API). Si aucun mois n’est
   déterminable, la conversion est refusée et aucun fichier n’est écrit. Chaque entrée porte aussi
   `source_filename`.
8. **Batch** — `create_monthly_batch_from_data` écrit `data/batches/YYYY-MM.json`. Pour les BDC
   simplifiés, les articles alimentent le batch (les articles non reconnus ou à quantité nulle sont
   ignorés et signalés dans `warnings`, sans être convertis arbitrairement). Pour les BDC détaillés
   disposant de `annex_videos`, `MonthlyBatch.video_entries` est alimenté directement depuis
   l’annexe (`duration_source="bdc_annex"`, ou `"bdc_annex_category"` si la durée est absente),
   jamais par réplication des articles. Les catégories reconnues intègrent les paliers `5 à 8` /
   `5-8` / `8 min et plus` en plus des libellés historiques `5-7` / `plus de 7`. Les BDC en échec
   d’extraction sont exclus.

Plafonds configurables (env) : `BDC_MAX_PDF_FILES` (défaut 200), `BDC_MAX_PDF_BYTES` (défaut 20 Mo).

## Prérequis

- Python 3.10+
- Dépendances du monorepo (`Flask`, `Flask-WTF`, `python-dotenv`, `pdfplumber` via `make install-dev` / extra `pdf`)
- PDF déposés dans `apps/bdc_converter/pdfs/`
- Fichier `.env` à la racine (`ADMIN_TOKEN`, `FLASK_SECRET_KEY`) — voir le [README racine](../../README.md)

## Installation (via monorepo)

Depuis la racine du dépôt :

```bash
python3 -m venv venv
source venv/bin/activate
make install-dev
cp .env.example .env   # si pas déjà fait
```

## Lancer le serveur

```bash
# recommandation : dashboard unifié
./scripts/launch_services.sh

# ou service seul (écoute 127.0.0.1:5000)
make serve-bdc
```

En local, l’authentification loopback est automatique. Sinon : cookie / en-tête `ADMIN_TOKEN`,
et CSRF (`X-CSRFToken`) pour les `POST`.

## Frontend

Page `/` : bouton **Lancer la conversion** → `POST /api/convert` (JS : `/static/js/bdc_convert.js`).

Le dossier `pdfs/` est scanné côté serveur ; aucun upload utilisateur. Après conversion, chaque
PDF apparaît sous forme de carte :

- numéro BDC, nombre d’articles, badge de format (**« Simplifié »** ou **« Détaillé (N vidéos) »**),
  et pour un BDC détaillé une ligne synthétisant `Période : … · N lots concernés` ;
- badges d’état et d’intégrité (**« ✓ Intègre »**, **« Partiel »**, **« Échec extraction »**,
  **« Structure modifiée »** avec infobulle de structure attendue, **« Plage détectée »**,
  **« Chevauchement lot … »**) ;
- avertissements `error`/`warning` (l’avertissement `info` `structure_modifiee` est affiché via
  le badge et la ligne `Structure détectée : …`), cartes d’erreur pour les PDF illisibles,
  panneau global des chevauchements entre BDC et panneau des avertissements du batch.

## CLI

```bash
cd apps/bdc_converter
python3 convert_bdc.py
```

Produit `resultats_bdc.json` (usage CLI legacy). L’UI Flask préfère `convert_bdc_MM-YYYY.json`.

## Structure de sortie (JSON BDC)

### Format simplifié

```json
[
  {
    "numero_bdc": "BDC-25-1873",
    "date_commande": "22/12/2025",
    "date_livraison": "22/12/2025",
    "reference": "LOTS 202/204",
    "references": ["LOTS 202", "204"],
    "source_filename": "BDC-25-1873.pdf",
    "totals": { "subtotal": "16,00", "total": "16,00" },
    "bdc_format": "simplified",
    "period": null,
    "concerned_lots": [],
    "concerned_lots_declared_count": null,
    "annex_videos": [],
    "annex_declared_count": null,
    "annex_summary": null,
    "status": "ok",
    "layout": {
      "fingerprint": "index | description | quantite | taux | montant",
      "expected": "index | description | quantite | taux | montant",
      "changed": false,
      "columns": [
        { "name": "index", "label": "#" },
        { "name": "description", "label": "Description" },
        { "name": "quantite", "label": "Quantité" },
        { "name": "taux", "label": "Taux" },
        { "name": "montant", "label": "Montant" }
      ]
    },
    "warnings": [],
    "articles": [
      {
        "description": "Recadrage - vidéo courte (moins de 2 min)",
        "quantite": "4,00",
        "unite": "Unité",
        "taux": "4,00",
        "montant": "16,00"
      }
    ]
  }
]
```

### Champs additionnels du format détaillé (`bdc_format == "detailed"`)

```json
{
  "numero_bdc": "BDC-M6-0001",
  "bdc_format": "detailed",
  "period": "Lots 42 et + · 11/2026",
  "concerned_lots": ["42", "45", "50"],
  "concerned_lots_declared_count": 73,
  "annex_declared_count": 134,
  "annex_videos": [
    {
      "lot": "42",
      "video_name": "Le lapsus d’Olivier Minne déclenche un fou rire sur le plateau.mp4",
      "duration_text": "0 min 18",
      "duration_sec": 18,
      "category_label": "courte",
      "montant": "4,00"
    }
  ],
  "annex_summary": {
    "total_videos": "134",
    "total_ht": "712.00",
    "category_rows": [
      "91 × Recadrage - vidéo courte (moins de 2 min) 364,00"
    ],
    "pages": [2, 3, 4, 5, 6]
  }
}
```

`status` vaut `ok`, `partial` ou `failed`. `warnings` contient des objets
`{"code", "severity", "message"}` (`severity` : `error`, `warning`, `info`).
Les clés `totals`, `status`, `warnings`, `layout`, `bdc_format`, `period`, `concerned_lots`,
`concerned_lots_declared_count`, `annex_videos`, `annex_declared_count` et `annex_summary` sont
optionnelles pour les anciens consommateurs : un JSON sans ces clés reste lisible (traité comme
`simplified`).

Le bloc `layout` porte l’empreinte de template (voir « Détection de dérive ») :
`fingerprint`, `expected`, `changed` et le détail `columns` (nom canonique + libellé d’origine).
Un `fingerprint` à `null` signifie qu’aucun en-tête n’a été reconnu et que le repli table a été
utilisé.

Les articles sans `description` sont ignorés lors de la transformation en `PurchaseOrder`
(pipeline `libs/pipeline/bdc_to_batch.py`).

## API

| Méthode | Route | Notes |
|---------|-------|--------|
| `GET` | `/` | UI |
| `GET`/`POST` | `/auth` | Saisie jeton (hors loopback) |
| `POST` | `/api/convert` | Auth + CSRF ; écritures atomiques / verrou par mois ; `409` si conversion déjà en cours |

Réponse typique de `/api/convert` : `message`, `output_file` (nom de fichier), `processed_files`,
`successful_files`, `failed_files`, `failed_documents`, `documents` (cartes de contrôle avec
`status`, `warnings`, `layout`, `bdc_format`, `period`, `concerned_lots_count`,
`annex_videos_count` et `overlaps`), `overlaps` (lots partagés entre plusieurs BDC), `month`
(`value`, `documents_by_month`, `mixed`), `progress`, `batch` (`batch_id`, `file` (nom),
`report_url`, …). Note : `documents[].warnings` filtre les alertes sur `severity in ("error", "warning")`.

## Tests et fixtures

- `pytest tests/test_convert_bdc_extraction.py` : corpus de référence (9 PDF), formats `simplified` et `detailed`, invariants, validation et dérive de template.
- `pytest tests/test_convert_bdc_layout.py` : extraction par coordonnées (sans filets, colonnes réordonnées/ajoutées, descriptions multi-lignes, unités) et empreinte de layout.
- `pytest tests/test_bdc_to_batch.py` : transformation vers `MonthlyBatch` (articles simplifiés et annexes détaillées).
- `pytest tests/test_november_mixed_formats.py` : non-régression bout-en-bout sur novembre 2026 (coexistence `BDC-26-1882` simplifié et `BDC-M6-0001` détaillé, actif si les PDF locaux et archives `2026-11` sont présents).
- Fixtures : `tests/fixtures/bdc/` (9 PDF, attendus avec `expected_layout_fingerprint` et champs détaillés, manifeste SHA-256).
- `external/parseur/` contient les JSON Parseur pour les 7 premiers BDC simplifiés, **non normatifs** : Parseur interprète les dates ambiguës au format US (`01/10/2026` → `2026-01-10`).

## Dépannage

- **Pas d’en-tête de tableau détecté** — l’extraction par coordonnées échoue et le repli `extract_tables(TABLE_SETTINGS)` est utilisé. Si les deux échouent, vérifier les libellés de colonnes et compléter `HEADER_SYNONYMS` / `UNIT_WORDS` ; le document sort alors en `failed` (`articles_absents`) au lieu d’être silencieusement vide.
- **Statut `partial`** — une incohérence a été détectée (référence absente, `quantité × taux ≠ montant`, `Σ montants ≠ total`, ligne incomplète, ou écart croisé entre page 1 et annexe détaillée comme `annexe_quantite_incoherente` / `annexe_montant_incoherent` / `lots_concernes_incoherents`). Le batch est créé mais l’alerte est visible dans l’UI et dans `warnings`.
- **Structure modifiée** — le `layout.fingerprint` diffère de `EXPECTED_LAYOUT_FINGERPRINT` (colonne ajoutée/supprimée/déplacée ou en-tête inconnu). C’est un avertissement **informatif** : la conversion continue, mais vérifier la carte du PDF et, si le changement est voulu, mettre à jour la constante et les fixtures après validation. Un `fingerprint` à `null` signale le repli sur l’extraction par tables.
- **Statut `failed`** — numéro BDC, date de commande ou articles manquants. Le document est exclu du batch ; aucun fichier mensuel n’est écrit si aucun BDC n’est exploitable.
- **Mois indéterminé** — aucune date de commande valide au format `JJ/MM/AAAA` ; la conversion est refusée (pas de repli sur le mois courant).
- **Lot multi-mois** — les BDC de plusieurs mois sont regroupés sous le mois majoritaire et signalés dans `month.mixed` / les warnings du batch (cas des BDC corrigés renvoyés le mois suivant).
- **Trop de PDF / fichier trop gros** — relever `BDC_MAX_PDF_FILES` / `BDC_MAX_PDF_BYTES` ou alléger le dossier `pdfs/`.
- **401** — fournir `X-Admin-Token` ou ouvrir depuis `127.0.0.1` avec `TRUST_LOOPBACK_AUTH` actif.
- **PDF illisible** — une carte d’erreur signale le fichier ; le détail de l’exception reste dans les logs serveur.
- **Références absentes** — `reference` / `references` peuvent être vides ; le consommateur doit le tolérer. Un avertissement `reference_absente` est émis.
