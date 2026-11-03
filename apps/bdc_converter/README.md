# Convertisseur BDC (`apps/bdc_converter`)

Extrait les informations des bons de commande PDF et produit :

1. un JSON mensuel `convert_bdc_MM-YYYY.json` (consommé par le rapprochement) ;
2. un `MonthlyBatch` partagé dans `data/batches/YYYY-MM.json` (consommé par le reporting).

Fait partie du monorepo ; préférer le venv et le `.env` **à la racine du dépôt**.

## Fonctionnement

1. **Scan de `pdfs/`** — chaque PDF est traité par `convert_bdc.py`.
2. **Métadonnées** — numéro BDC, dates, `reference` / `references` (découpe sur `/`, `;`, `,`, `et`).
   Les plages `38-92`, `38 au 92` ou `38 à 92` sont normalisées en `LOTS 38 à 92` ; le tiret
   n’est plus interprété comme un simple séparateur de lots. Les listes coupées autour de
   `N° de réf :` sont recollées, y compris lorsqu’elles se poursuivent sur la ligne suivante,
   et les plages mixtes (`LOTS 41-50/55`) conservent toutes leurs bornes.
   Si un libellé change (`Référence :`, `Date de commande :`, `Livraison :`), un repli par
   coordonnées retrouve le numéro BDC et les dates à partir de la ligne du libellé.
3. **Articles** — extraction **par coordonnées** : la ligne d’en-tête (`Description`, `Quantité`,
   `Taux`, `Montant`, avec synonymes `Désignation`, `Qté`, `P.U.`, `Prix`, `Total HT`…)
   définit les frontières de colonnes, puis chaque mot est affecté par recouvrement horizontal.
   Cela couvre les tableaux **sans filets**, les colonnes réordonnées ou supplémentaires
   (une colonne inconnue est ignorée) et les descriptions **multi-lignes**. Les unités
   (`Unité`, `Heure`, `Forfait`…) sont rattachées à la ligne d’article précédente.
   Repli sur `pdfplumber.extract_tables(TABLE_SETTINGS)` si aucun en-tête n’est détecté.
   Les synonymes sont centralisés dans `HEADER_SYNONYMS` / `UNIT_WORDS` (`convert_bdc.py`).
   Les montants à séparateur de milliers (`1 056,00`) sont reconnus, et une ligne « Total »
   n’est ignorée que sur frontière de mot (une description « Totally Spies » reste un article).
4. **Validation** — chaque document reçoit un `status` (`ok`, `partial`, `failed`) et une liste
   de `warnings` : numéro BDC, dates `JJ/MM/AAAA` strictes, présence d’articles, cohérence
   `quantité × taux = montant`, `Σ montants = total`. Un document `failed` est exclu du batch ;
   un document `partial` reste exploitable mais signalé dans l’UI et le JSON.
5. **Détection de dérive** — la ligne d’en-tête de la première page produit une empreinte de
   template (`layout.fingerprint`) : noms canoniques des colonnes reconnues, libellé normalisé
   pour une colonne inconnue, dans l’ordre gauche-droite. Elle est comparée à
   `EXPECTED_LAYOUT_FINGERPRINT` (`index | description | quantite | taux | montant`).
   Toute différence (colonne ajoutée/supprimée/déplacée, en-tête non reconnu) émet un
   avertissement **informatif** `structure_modifiee` et affiche le badge « Structure modifiée »
   sur la carte du PDF. Cela ne change pas le `status` : la conversion reste non bloquante.
   Les synonymes (`Désignation`, `Qté`, `P.U.`, `Total HT`…) ne déclenchent pas de dérive
   puisqu’ils sont ramenés aux noms canoniques.
6. **Persistance** — nom de sortie **toujours** dérivé du mois majoritaire des dates valides :
   `convert_bdc_MM-YYYY.json` (pas de `output_name` libre côté API). Si aucun mois n’est
   déterminable, la conversion est refusée et aucun fichier n’est écrit. Chaque entrée porte aussi
   `source_filename`.
7. **Batch** — `create_monthly_batch_from_data` écrit `data/batches/YYYY-MM.json`. Les articles
   non reconnus ou à quantité nulle sont ignorés et signalés dans `warnings`, sans être
   convertis arbitrairement en vidéos standard. Les BDC en échec d’extraction sont exclus.

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
PDF apparaît sous forme de carte (numéro BDC, lots détectés, badge « Plage détectée » ou
« Chevauchement »), les PDF illisibles sont signalés et un panneau global liste les
chevauchements entre bons de commande.

## CLI

```bash
cd apps/bdc_converter
python3 convert_bdc.py
```

Produit `resultats_bdc.json` (usage CLI legacy). L’UI Flask préfère `convert_bdc_MM-YYYY.json`.

## Structure de sortie (JSON BDC)

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

`status` vaut `ok`, `partial` ou `failed`. `warnings` contient des objets
`{"code", "severity", "message"}` (`severity` : `error`, `warning`, `info`).
Les clés `totals`, `status`, `warnings` et `layout` sont optionnelles pour les anciens
consommateurs : un JSON sans ces clés reste lisible.

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
`successful_files`, `failed_files`, `failed_documents`, `documents` (cartes de contrôle, avec
`status`, `warnings` et `layout`), `overlaps` (lots partagés entre plusieurs BDC), `month`
(`value`, `documents_by_month`, `mixed`), `progress`, `batch` (`batch_id`, `file` (nom),
`report_url`, …).

## Tests et fixtures

- `pytest tests/test_convert_bdc_extraction.py` : golden corpus, invariants, validation et dérive de template.
- `pytest tests/test_convert_bdc_layout.py` : extraction par coordonnées (sans filets, colonnes réordonnées/ajoutées, descriptions multi-lignes, unités) et empreinte de layout.
- Fixtures : `tests/fixtures/bdc/` (PDF, attendus avec `expected_layout_fingerprint`, manifeste SHA-256).
- `external/parseur/` contient les JSON Parseur, **non normatifs** : Parseur interprète les dates ambiguës au format US (`01/10/2026` → `2026-01-10`).

## Dépannage

- **Pas d’en-tête de tableau détecté** — l’extraction par coordonnées échoue et le repli `extract_tables(TABLE_SETTINGS)` est utilisé. Si les deux échouent, vérifier les libellés de colonnes et compléter `HEADER_SYNONYMS` / `UNIT_WORDS` ; le document sort alors en `failed` (`articles_absents`) au lieu d’être silencieusement vide.
- **Statut `partial`** — une incohérence a été détectée (référence absente, `quantité × taux ≠ montant`, `Σ montants ≠ total`, ligne incomplète). Le batch est créé mais l’alerte est visible dans l’UI et dans `warnings`.
- **Structure modifiée** — le `layout.fingerprint` diffère de `EXPECTED_LAYOUT_FINGERPRINT` (colonne ajoutée/supprimée/déplacée ou en-tête inconnu). C’est un avertissement **informatif** : la conversion continue, mais vérifier la carte du PDF et, si le changement est voulu, mettre à jour la constante et les fixtures après validation. Un `fingerprint` à `null` signale le repli sur l’extraction par tables.
- **Statut `failed`** — numéro BDC, date de commande ou articles manquants. Le document est exclu du batch ; aucun fichier mensuel n’est écrit si aucun BDC n’est exploitable.
- **Mois indéterminé** — aucune date de commande valide au format `JJ/MM/AAAA` ; la conversion est refusée (pas de repli sur le mois courant).
- **Lot multi-mois** — les BDC de plusieurs mois sont regroupés sous le mois majoritaire et signalés dans `month.mixed` / les warnings du batch (cas des BDC corrigés renvoyés le mois suivant).
- **Trop de PDF / fichier trop gros** — relever `BDC_MAX_PDF_FILES` / `BDC_MAX_PDF_BYTES` ou alléger le dossier `pdfs/`.
- **401** — fournir `X-Admin-Token` ou ouvrir depuis `127.0.0.1` avec `TRUST_LOOPBACK_AUTH` actif.
- **PDF illisible** — une carte d’erreur signale le fichier ; le détail de l’exception reste dans les logs serveur.
- **Références absentes** — `reference` / `references` peuvent être vides ; le consommateur doit le tolérer. Un avertissement `reference_absente` est émis.
