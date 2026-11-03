L’analyse du snapshot `analyseur_rapports_fileconcat.md` met en évidence plusieurs erreurs de logique et de calcul réparties entre la réconciliation, l’analyse des archives vidéo et l’extraction des PDF de bons de commande.

(Note : le fichier fourni est tronqué au milieu de `apps/reporting/rapport-2026-10.json` ; le dossier `libs/` n'apparaît donc pas dans l'extrait. Voici l'ensemble des failles identifiées dans le code visible ainsi que par recoupement avec les données d'octobre 2026).

---

### 1. Bugs critiques de calcul et d'état dans `apps/reconciliation`

* **Suppression excessive lors de l'exclusion d'un fichier (`_effective_reporting_payload` dans `apps/reconciliation/app.py`)** :
Les compteurs de décrémentation `remaining_files = dict(record.excluded_files)` et `remaining_details = dict(record.excluded_files)` sont réinitialisés **à l'intérieur** de la double boucle `for group in payload.get("report", [])` → `for cat_data in (group.get("categories") or {}).values():`.


* **Impact concret** : Lorsqu'un utilisateur clique sur « Exclure ce fichier & recalculer » pour un doublon (`remove_all` non actif, donc `count = 1`), l'application est censée ne retirer qu'**une seule occurrence** du fichier pour éliminer le surplus. Or, comme un doublon au sein d'un même lot est déjà filtré par le reporting (`seen_names`), les doublons en réconciliation sont quasi systématiquement **inter-lots** (par exemple dans `BDC-26-1695`, `"Extrait prédiff – Ils doivent imiter les animaux lors de l’expédition.mp4"` est présent dans le lot `26` et dans le lot `27`). À cause de la réinitialisation du dictionnaire à chaque lot, l'exclusion retire le fichier **dans tous les lots du mois** simultanément. Pour `BDC-26-1695`, le réalisé passe ainsi de `7` standards (surplus `+1`) à `5` standards (manquant `-1`), rendant le BDC non certifiable.


* **Effet de bord supplémentaire** : Le formulaire `/inspector/exclude` n'envoie que `filename` sans `id_camille` ni `cluster_key`. Si deux BDC différents contiennent un fichier portant le même nom (ex. `"M6 - SDM - Liliane et José.mp4"` présent dans les lots `59`, `108` et `295` en octobre 2026), l'exclusion impactera tous les lots correspondants du mois. De plus, `payload["total_mp4"]` et `payload["group_totals"]` ne sont pas recalculés dans `_effective_reporting_payload`.




* **Calcul erroné de la ligne « Hors BDC » en cas de chevauchement (`apps/reconciliation/templates/export.html`)** :
Dans l'annexe 2 du template d'export, les volumes non rattachés sont calculés par soustraction globale :


```jinja2
{% set unattached_billed = bdc_overall - annex_totals.billed %}
{% set unattached_actual = reporting_overall - annex_totals.actual %}

```


* **Impact concret** : `annex_totals.actual` additionne `cluster.actual_total` sur tous les BDC (`all_clusters`), tandis que `reporting_overall` compte chaque lot produit une seule fois. Dès qu'un lot produit est revendiqué par deux BDC en chevauchement (`overlapping_bdc_lots`), ses vidéos sont comptées deux fois dans `annex_totals.actual`. Par conséquent, `unattached_actual` devient **négatif**, ce qui déclenche la condition `{% if unattached_billed != 0 or unattached_actual != 0 %}` et affiche une ligne fantôme **« Hors BDC » avec un nombre négatif de vidéos réalisées** (ou masque de vrais lots en attente de BDC si le volume des lots orphelins compense exactement celui des lots chevauchants).




* **Fuite ou perte silencieuse des ajustements de session (`_resolve_record` dans `apps/reconciliation/app.py`)** :
* Si un utilisateur accède à un export (`/export/html?month=YYYY-MM`) sans `token` ou avec un `token` expiré, l'étape 2 de `_resolve_record` parcourt `_store` à l'envers et renvoie le dernier `StoredAnalysis` correspondant au mois, **avec ses exclusions/justifications en mémoire**, même s'il provient d'une autre session ou d'une analyse antérieure à une régénération des fichiers JSON sur disque.


* Inversement, si le `token` a expiré (> 2h ou > 50 analyses) au moment où l'utilisateur clique sur `/inspector/exclude` ou `/inspector/justify`, l'étape 3 de `_resolve_record` recrée silencieusement une analyse vierge depuis le disque et écrase tous les ajustements précédents sans avertir l'utilisateur.




* **Exclusion limitée au premier fichier suspect dans l'UI (`apps/reconciliation/templates/index.html`)** :
Le bouton « Exclure ce fichier & recalculer » cible uniquement `diag.suspect_files[0].name`. Si un diagnostic remonte plusieurs fichiers suspects distincts dans le même regroupement BDC, les fichiers `suspect_files[1:]` n'ont aucun bouton d'exclusion tant que le premier n'a pas été exclu.



---

### 2. Bugs de logique et de calcul dans `apps/reporting`

* **Désambiguïsation des lots (`disambiguate_folders` dans `apps/reporting/analyser.py`)** :
Lorsque plusieurs dossiers existent pour un même couple `(id_camille, person)` avec des contenus différents, l'identifiant du lot est renommé en `f"{id_camille} ({dt.strftime('%Y-%m-%d')})"` (ex. `"309 (2026-10-01)"` et `"309 (2026-10-27)"` dans `rapport-2026-10.json`).


* **Collision intra-journalière** : `parse_folder_name` ignore l'heure (`_HH-MM-SS`) et ne conserve que `%Y-%m-%d`. Si deux dossiers distincts pour le même lot ont été créés **le même jour**, ils reçoivent tous deux le même `effective_id` (`"309 (2026-10-01)"`) et sont quand même fusionnés dans `groups.setdefault((id_camille, person), [])`, tout en conservant un ID pollué par la date.


* **Tri cassé dans la réconciliation** : Dans `apps/reconciliation/app.py`, `_lot_sort_key` utilise `if text.isdigit():` (contrairement à `analyser.py` qui utilise `re.findall(r"\d+", lot_id)`). Comme `"309 (2026-10-01)".isdigit()` vaut `False`, les lots désambiguïsés sont relégués tout à la fin des tableaux de réconciliation au lieu d'être triés numériquement entre 305 et 310.




* **Collision de clé dans le cache des durées en mode secours CSV (`_cache_key` dans `apps/reporting/analyser.py`)** :
```python
def _cache_key(path: Path) -> str:
    try:
        st = path.stat()
        return f"{path.resolve()}|{st.st_size}|{int(st.st_mtime)}"
    except Exception:
        return f"{path.resolve()}|0|0"

```


Lorsque le fichier pointé par `video_path` dans `metadata.json` n'est pas présent localement (cas courant documenté dans le README où la durée est lue en secours via `derive_duration_from_scenes(subfolder)` dans `*_scenes.csv`), `path.stat()` échoue et la clé de cache devient `f"{path.resolve()}|0|0"` **sans inclure `subfolder**`. Si deux sous-dossiers d'archives ont un `video_path` générique identique (ex. `"EXTRAIT 3.mp4"` ou `"SEQ RS.mp4"`), le second lot récupère la durée mise en cache du premier lot au lieu de lire son propre fichier `*_scenes.csv`.


* **Écrasement potentiel du dernier timecode dans `derive_duration_from_scenes` (`apps/reporting/analyser.py`)** :
La boucle `for row in reader: last_tc_out = row.get("Timecode Out") or row.get("TimecodeOut")` écrase `last_tc_out` à chaque ligne sans vérifier si la valeur est non vide. Si la dernière ligne du CSV est une ligne de total ou une ligne partiellement vide où `Timecode Out` est vide, la fonction renvoie `None` au lieu du dernier timecode valide.


* **Code mort et non-utilisation des batches BDC (`apps/reporting/app.py`)** :
* `month_options(base_dir, batches_dir)` accepte `batches_dir` mais ne l'utilise jamais.


* `load_monthly_batch_from_file(month)` est définie pour lire `data/batches/{month}.json`, mais n'est appelée nulle part : `get_batch_for_month` appelle systématiquement `create_batch_from_archives`. En conséquence, `batch_meta["orders"]` vaut toujours `0` et `batch_meta["sources"]` est toujours vide dans l'interface de reporting.





---

### 3. Bugs de parsing dans `apps/bdc_converter/convert_bdc.py`

* **Corruption des quantités pour tout montant $\ge 1\,000,00\text{ €}$ (`NUMERIC_PATTERN` et `_pop_numeric_from_end`)** :
L'expression régulière `NUMERIC_PATTERN = re.compile(r"^(?:€|\$)?\s*\d+(?:[.,]\d+)?(?:\s+\w+)?$", re.UNICODE)` n'autorise **pas** d'espace séparateur de milliers entre les chiffres (comme `"1 056,00"`, format pourtant utilisé dans `BDC-26-1856.pdf`).


* **Impact concret** : Si une ligne d'article dépasse `1 000,00 €` (par ex. `125` vidéos standards à `8,00 €`), `_looks_like_numeric("1 000,00")` renvoie `False`. Dès lors, les trois appels successifs à `_pop_numeric_from_end(working_cells)` décalent toutes les colonnes :


1. `montant` récupère la valeur de `taux` (`"8,00"`) ;


2. `taux` récupère la valeur de `quantite` (`"125,00 Unité"`) ;


3. `quantite` récupère le numéro de ligne `#` en début de ligne (`"1"`, `"2"`, etc.) !






* **Faux positifs dans `_row_is_total(cells)**` :
```python
def _row_is_total(cells):
    joined = " ".join(cells).lower()
    return "sous-total" in joined or "total" in joined

```


Le test `"total" in joined` cherche la sous-chaîne `"total"` n'importe où dans la ligne. Si la description d'un article contient un mot incluant `"total"` (par ex. `"Totally Spies"`, émission présente dans plusieurs lots de `rapport-2026-10.json`), la ligne d'article est ignorée à tort comme s'il s'agissait de la ligne de total de la facture.


* **Perte de lots sur les références multi-lignes (`_extract_reference`)** :
* Dans `BDC-26-1695.pdf`, `BDC-26-1862.pdf`, `BDC-26-1867.pdf` et `BDC-26-1872.pdf`, la liste des lots est coupée sur deux lignes autour de `N° de réf :`. Le rattrapage de la première ligne (`_extract_preceding_lot_sequence`) n'est déclenché **que** si `first_line.startswith("/")`. Si le retour à la ligne PDF se produit *après* le slash (`LOTS 18/19/` puis `20/23`) ou après un espace, `first_line.startswith("/")` vaut `False` et tous les lots de la première ligne sont perdus.


* De même, si une référence combine une plage et des lots séparés par `/` (ex. `LOTS 41-50/55`), `_normalize_range_reference` intercepte la chaîne et retourne `["LOTS 41 à 50/55"]` sans effectuer le découpage `REFERENCE_SPLIT_PATTERN`.





---

### 4. Anomalies visibles dans les données d'octobre 2026 (`convert_bdc_10-2026.json` ↔ `rapport-2026-10.json`)

En croisant les deux fichiers JSON du mois `2026-10` présents dans le snapshot, on observe déjà trois cas intéressants à surveiller :

1. **`BDC-26-1695`** : Facturé `5` courtes / `6` standards (`11` vidéos), mais réalisé `5` courtes / `7` standards (`12` vidéos) à cause du fichier `"Extrait prédiff – Ils doivent imiter les animaux lors de l’expédition.mp4"` (`193.0s`) présent en doublon dans les lots `26` et `27`.


2. **`BDC-26-1856` (`LOTS 41 à 276`)** : Le volume global concorde (`168` vidéos facturées = `168` vidéos produites sur les lots 41 à 276), mais la répartition présente un écart croisé : `74` courtes / `93` standards / `1` longue facturées contre `75` courtes / `93` standards / `0` longue dans `rapport-2026-10.json`. De plus, deux vidéos différentes portent exactement le même nom `"M6 - SDM - Liliane et José.mp4"` dans cette plage (lot `59` à `50.8s` et lot `108` à `74.52s`), ce qui risque de fausser la détection de doublons si elle se base uniquement sur le nom normalisé du fichier sans comparer la durée.


3. **`BDC-26-1867` (`LOTS 304/305/309/313/315/322/325/328/335`)** : Le BDC facture `28` vidéos au total (`4` courtes, `23` standards, `1` longue). Or, dans `rapport-2026-10.json`, les 8 lots hors `309` (`304, 305, 313, 315, 322, 325, 328, 335`) totalisent déjà `28` vidéos à eux seuls, tandis que le lot `309` est scindé en `"309 (2026-10-01)"` (`3` vidéos) et `"309 (2026-10-27)"` (`1` vidéo), portant le total à `32` vidéos (et le lot `310` est orphelin de BDC).

L’analyse de `comparison.py` et `bdc_to_batch.py` confirme plusieurs erreurs de logique et de calcul qui expliquent notamment les comportements observés sur les données d’octobre 2026.

---

### 1. Bugs de logique et de calcul dans `libs/reconciliation/comparison.py`

* **Perte de lots lors de la résolution des lots désambiguïsés (`_extract_reference_ids`)** :
Lorsqu'un lot de production a été scindé par date dans le reporting (comme le lot `309` en octobre 2026, divisé en `"309 (2026-10-01)"` et `"309 (2026-10-27)"` dans `rapport-2026-10.json`), `_extract_reference_ids` cherche les candidats partageant le même `_base_lot_id` (`"309"`), mais **n'en sélectionne qu'un seul** (`cleaned = scored[0][1]`, celui dont la date est la plus proche de `date_commande`) :


* **Impact sur les volumes et les lots orphelins** : Sur `BDC-26-1867` (daté du `29/10/2026`), seul `"309 (2026-10-27)"` est rattaché au BDC. L'autre dossier `"309 (2026-10-01)"` n'est rattaché à aucun BDC et bascule automatiquement dans `missing_invoices` (« Lots produits en attente de BDC »).


* **Masquage d'un chevauchement de BDC** : Si deux BDC différents du même mois réclament tous deux le lot `309` mais à des dates de commande différentes (ex. début et fin de mois), le premier BDC est résolu vers `"309 (2026-10-01)"` et le second vers `"309 (2026-10-27)"`. Dans `_summarize_bdc_orders`, `claims[gid]` enregistre alors deux clés distinctes et **ne détecte pas le chevauchement**, alors que `detect_bdc_overlaps` (qui appelle `_extract_reference_ids(order, None)`) le détecte.


* **Non-déterminisme** : `known_reporting_ids` est un `set` (`reporting_ids = set(reporting_groups.keys())`). Si le BDC n'a pas de date valide (`order_dt is None`) ou si deux candidats sont à égale distance temporelle, `candidates[0]` dépend de l'ordre d'itération du `set` Python, qui varie d'une exécution à l'autre.




* **Faux positifs sur la détection de doublons inter-lots (`_annotate_production_lots` et `_build_cluster_diagnostic`)** :
La détection de doublons au sein d'un regroupement BDC repose uniquement sur le nom normalisé du fichier, sans vérifier ni la durée (`duration_sec`) ni la catégorie :


```python
name_counts = Counter(
    normalize_video_name(str(item.get("name") or "")) for item in production_files
)

```


* **Impact concret sur octobre 2026 (`BDC-26-1856`)** : Dans la plage `LOTS 41 à 276`, le lot `59` contient `"M6 - SDM - Liliane et José.mp4"` (`50.8s`) et le lot `108` contient un autre extrait intitulé `"M6 - SDM - Liliane et José.mp4"` (`74.52s`). Bien qu'il s'agisse de deux vidéos différentes (durées distinctes dans deux lots distincts), `name_counts` vaut `2`. Les deux lots sont marqués `is_duplicate = True` / `has_suspects = True`. Pire : comme `BDC-26-1856` a un écart de `+1` en vidéo courte (`75` réalisées vs `74` facturées) et `-1` en vidéo longue (`0` vs `1`), `positive_delta` vaut `True` et le diagnostic affirme à tort que `"M6 - SDM - Liliane et José.mp4"` du lot `59` est un doublon responsable de l'écart.




* **Erreur de calcul de `adjusted_actual` et `duplicate_resolves` pour les doublons multiples (`_build_cluster_diagnostic`)** :
Lorsqu'un fichier apparaît en doublon dans `production_files`, le code construit `duplicate_representatives` en ne gardant **qu'un seul exemplaire par nom normalisé** (`seen_duplicate_names`), puis calcule l'écart ajusté en soustrayant `1` par représentant :


```python
for item in suspects:
    category = str(item.get("category") or "")
    if category in adjusted_actual:
        adjusted_actual[category] = max(0, adjusted_actual[category] - 1)

```


* **Cas 1 (triplons ou plus)** : Si un même fichier est présent **3 fois** dans un BDC (soit `2` vidéos en surplus), `duplicate_representatives` ne contient qu'un seul élément et ne déduit que `1` au lieu de `name_counts[norm_name] - 1` (`2`). `duplicate_resolves` et `justification_eligible` restent alors à `False` alors que le triplon explique 100 % du surplus.


* **Cas 2 (homonymes de catégories différentes)** : Si deux fichiers de même nom appartiennent à deux catégories différentes (ex. `short` dans un lot et `standard` dans un autre), `duplicate_representatives` garde arbitrairement le premier (`duplicates[0]`) et déduit `1` de sa catégorie, même si le surplus (`delta > 0`) se trouve sur la catégorie du second fichier.




* **Détection unilatérale des seuils temporels (`NEAR_BOUNDARY_SECONDS` dans `_annotate_production_lots` et `_build_cluster_diagnostic`)** :
Le test de proximité des seuils (`120.0`, `300.0`, `420.0`) est écrit :


```python
if 0 < (th - dur_val) <= NEAR_BOUNDARY_SECONDS:

```


* **Angle mort au-dessus du seuil** : Cette condition ne détecte que les vidéos situées **en dessous** du seuil (`[117.0s, 120.0s[`), mais ignore totalement celles situées **juste au-dessus** (`[120.0s, 123.0s]`, `[300.0s, 303.0s]`, `[420.0s, 423.0s]`). Or, en production, un écart fréquent survient lorsqu'une vidéo attendue « courte » par le client dépasse légèrement 2 minutes au rendu (par ex. dans `rapport-2026-10.json` : `121.12s` dans le lot `27`, `123.44s` dans le lot `180`, `123.72s` dans le lot `273`) et bascule en « standard ». Utiliser `abs(th - dur_val) <= NEAR_BOUNDARY_SECONDS` permettrait de couvrir les deux côtés de la frontière.


* **Marquage suspect même sans écart** : Dans `_annotate_production_lots`, `is_near` active `is_suspect = True` et `lot["has_suspects"] = True` sans vérifier si `delta` présente un écart sur les catégories concernées. Ainsi, un lot appartenant à un BDC 100 % conforme affiche quand même le badge d'alerte `⚠ À vérifier` dès qu'une vidéo dure entre `117s` et `120s` (ex. lot `12` à `114.28s` ou lot `64` à `118.48s`).




* **Incohérence entre le score par commande (`order_summaries`) et l'analyse par regroupement (`clusters`)** :
Dans `_summarize_bdc_orders`, si deux BDC ciblent exactement les mêmes lots (`cluster_key` identique), leurs quantités facturées sont **additionnées** dans `clusters[cluster_key]["counts"]`. En revanche, le calcul de `compliant_orders` (lignes 606-620 de `comparison.py`) compare la production totale du `cluster_ids` aux quantités de **chaque commande prise isolément** (`order["counts"]`) :


* Si deux BDC sont émis en double par erreur pour le même lot (chacun facturant le volume exact produit), chaque commande prise isolément vérifie `actual == order["counts"]` (`order_ok = True`), ce qui donne un **score de conformité de 100 %**, alors que le cluster cumule le double du facturé (`delta < 0`).


* Inversement, si deux BDC complémentaires portent sur le même lot (ex. un BDC pour les vidéos courtes et un BDC rectificatif pour les vidéos standards du même lot), le cluster combiné est parfaitement équilibré (`base_compliant = True`), mais chacune des deux commandes prise isolément échoue (`order_ok = False`), faisant tomber le score à `0 %` sans possibilité de justification.




* **Omission silencieuse des articles de catégorie `unknown` (`_summarize_bdc_orders` et `compare_reports`)** :
Si la description d'un article BDC n'est pas reconnue par `infer_category_from_description`, elle reçoit la clé `"unknown"`. Or, `_summarize_bdc_orders` exclut `"unknown"` du total (`total = sum(value for key, value in counts.items() if key != "unknown")`), et `compare_reports` ne calcule `billed`, `delta`, `billed_total` et `bdc_totals` que sur `CATEGORY_KEYS` (`short`, `standard`, `medium`, `long`). Un article non reconnu est donc **totalement ignoré des totaux facturés et du calcul d'écart**, sans aucun warning ni signalement bloquant.


* **Pollution de l'index des mois adjacents par les plages BDC (`_build_adjacent_index`)** :
Pour indexer les BDC des mois $M-1$ et $M+1$, `_build_adjacent_index` appelle `_extract_reference_ids(order, None)` avec `known_reporting_ids = None`. Dès qu'un BDC du mois précédent ou suivant contient une plage (ex. `LOTS 41 à 276`), `_expand_range` génère **tous les entiers de la plage** (les 236 numéros) et les insère dans `adjacent_index` comme « facturés sur le mois adjacent ». Tout lot du mois courant non rattaché dont le numéro tombe dans cet intervalle se voit attribuer à tort un diagnostic de décalage inter-mois.


* **Expression régulière de plage trop permissive (`RANGE_PATTERN`)** :
`RANGE_PATTERN = re.compile(r"(\d+)\s*(?:à|au|to|-|a)\s*(\d+)", re.IGNORECASE)` inclut la lettre `a` seule et le tiret `-`. Si un champ `reference` contient un numéro de BDC en fallback (`"BDC-26-1689"`) ou une date (`"2026-10"`), `_numbers_from_token` interprète `"26-1689"` comme une plage de lots allant de `26` à `1689`.



---

### 2. Bugs de logique et de calcul dans `libs/pipeline/bdc_to_batch.py`

* **Quantité forcée à `1` par défaut et plancher `max(1, ...)` (`parse_quantity`)** :


```python
def parse_quantity(value: Union[str, int, float, None], *, default: int = 1) -> int:
    ...
    return max(1, int(parsed.quantize(Decimal("1"), rounding=ROUND_HALF_UP)))

```


* Si une ligne d'article a une quantité égale à `"0,00"`, `0`, `""` (chaîne vide) ou `None`, `parse_quantity` retourne **`1`** au lieu de `0`. Toute ligne de commentaire ou ligne à quantité nulle extraite d'un tableau PDF ajoute donc artificiellement `1` vidéo facturée.




* **Inférence du `batch_id` cassée pour le format de fichier standard (`_infer_batch_id`)** :


```python
match = re.search(r"bdc_(\w+)_(\d{4})\.json", name_hint, re.IGNORECASE)

```


* Les fichiers générés par `bdc_converter` sont nommés `convert_bdc_MM-YYYY.json` (ex. `convert_bdc_10-2026.json` avec un tiret `-` et un mois numérique). La regex attend un underscore `_` avant l'année, donc elle échoue systématiquement sur `convert_bdc_10-2026.json` et se rabat sur `datetime.now().strftime("%Y-%m")` (le mois de l'horloge système au lieu du mois du fichier).


* Même avec un underscore, `month_map` ne contient que les noms de mois en toutes lettres (`"janvier"`, `"octobre"`, etc.) et aucune clé numérique (`"01"` à `"12"`), ce qui renvoie la valeur par défaut `"01"` (janvier) pour tout mois numérique.




* **Rattachement arbitraire au premier lot et conversion silencieuse des `UNKNOWN` en `STANDARD` (`_orders_to_video_entries`)** :


* Lors de la création des `VideoEntry` synthétiques d'un `MonthlyBatch`, toutes les vidéos d'un BDC multi-lots sont affectées uniquement à `order.references[0]` (ex. `"LOTS 2"` pour `BDC-26-1689`) et le champ `person` reçoit également `group_label` (`"LOTS 2"`) au lieu du nom du monteur.


* `_CATEGORY_DURATION_MAP` attribue une durée de `180` secondes à `VideoCategory.UNKNOWN`. Si ce batch est analysé par `analyze_batch`, `180s` est catégorisé en `VideoCategory.STANDARD` (`[120, 300[`), transformant silencieusement des articles non reconnus en vidéos standards.