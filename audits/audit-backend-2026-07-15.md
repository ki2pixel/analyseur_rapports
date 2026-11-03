# Audit backend — analyseur_rapports

Date : 2026-07-15

## 1. Synthèse exécutive

L’application est un monorepo Flask local composé de trois services principaux :

- **BDC converter** : conversion de PDF BDC en JSON et batches mensuels.
- **Reporting** : analyse des archives vidéo et génération d’exports HTML/JSON.
- **Reconciliation** : rapprochement des exports reporting et BDC.
- **Dashboard de services** : lancement/arrêt des trois services Flask.

Le socle fonctionnel est cohérent pour un usage local ou intranet restreint. Les risques principaux apparaissent dès que les services sont exposés à un réseau non maîtrisé : absence d’authentification, absence de protection CSRF, modes debug possibles sur `0.0.0.0`, endpoints mutatifs accessibles sans contrôle et lecture de fichiers insuffisamment bornée côté rapprochement.

État global : **acceptable pour un outil local contrôlé**, **non prêt pour exposition réseau ou production** sans durcissement.

## 2. Périmètre audité

Fichiers et modules examinés :

- `./apps/bdc_converter/app.py`
- `./apps/bdc_converter/convert_bdc.py`
- `./apps/reporting/app.py`
- `./apps/reporting/analyser.py`
- `./apps/reporting/generate_report.py`
- `./apps/reconciliation/app.py`
- `./libs/pipeline/bdc_to_batch.py`
- `./libs/reconciliation/comparison.py`
- `./libs/schemas/__init__.py`
- `./scripts/service_dashboard.py`
- `./tests/`
- `./apps/bdc_converter/tests/`
- `./Makefile`
- `./pyproject.toml`
- `./requirements.txt`

## 3. Vérifications exécutées

Commandes exécutées depuis la racine du dépôt :

- `make test`
- `make lint`
- `source venv/bin/activate && make test`
- `source venv/bin/activate && make lint`

Résultats :

- **Tests hors venv** : échec à la collecte, `pdfplumber` absent de l’environnement système.
- **Tests avec venv** : `27 passed`, `1 failed`.
  - Échec : `./apps/bdc_converter/tests/test_app.py::test_convert_endpoint_success`
  - Cause : le test attend que `output_name` soit utilisé et sanitizé, mais `./apps/bdc_converter/app.py` appelle `convert_folder(..., output_file="")` puis force `convert_bdc_MM-YYYY.json`.
- **Mypy hors venv et avec venv** : échec identique, 3 erreurs dans `./libs/reconciliation/comparison.py`.

Erreurs mypy observées :

- `./libs/reconciliation/comparison.py:145` : affectation d’une `list[str]` dans un dictionnaire typé comme `int`.
- `./libs/reconciliation/comparison.py:179` : objet inféré trop générique, appel à `.get` non garanti.
- `./libs/reconciliation/comparison.py:184` : type de valeur incompatible dans le dictionnaire retourné.

## 4. Points positifs

- Les commandes externes `ffprobe` sont exécutées avec `shell=False` et un `timeout=15` dans `./apps/reporting/analyser.py:201`, ce qui limite fortement le risque d’injection shell.
- Aucun usage applicatif direct de `eval`, `exec`, `pickle` ou `yaml.load` n’a été identifié dans les modules applicatifs audités.
- Les templates Flask/Jinja utilisent l’échappement automatique par défaut ; aucun filtre `safe` dangereux n’a été détecté dans les templates audités.
- Les données partagées passent par des dataclasses communes dans `./libs/schemas/__init__.py`, ce qui donne un contrat métier centralisé.
- Le rapprochement dispose déjà d’un mécanisme de TTL et de taille maximale pour son store en mémoire dans `./apps/reconciliation/app.py:92` à `./apps/reconciliation/app.py:107`.
- Le dashboard lance les services via une liste de commandes statique dans `./scripts/service_dashboard.py:46` à `./scripts/service_dashboard.py:68`, et non à partir d’une commande utilisateur arbitraire.

## 5. Risques et constats prioritaires

### 5.1 Critique — Modes debug Flask exposables sur `0.0.0.0`

Occurrences :

- `./apps/bdc_converter/app.py:162`
- `./apps/reporting/app.py:473`
- `./apps/reconciliation/app.py:316`

Les trois applications appellent `app.run(..., host="0.0.0.0", debug=True)` lorsqu’elles sont lancées directement. Le `Makefile` utilise `flask run --host=0.0.0.0`, sans activer explicitement debug, mais les blocs `__main__` restent dangereux.

Impact :

- Exposition potentielle du debugger Werkzeug si un service est lancé directement.
- Risque d’exécution de code à distance dans une configuration debug mal protégée.
- Divulgation d’informations internes via pages d’erreur détaillées.

Recommandations :

1. Remplacer `debug=True` par une variable d’environnement explicitement contrôlée et désactivée par défaut.
2. Éviter `0.0.0.0` par défaut ; utiliser `127.0.0.1` pour l’usage local.
3. Déléguer l’exposition réseau éventuelle à un reverse proxy configuré.

### 5.2 Élevé — Absence d’authentification et d’autorisation

Endpoints concernés :

- `./apps/bdc_converter/app.py:78` — `POST /api/convert`
- `./apps/reporting/app.py:288` — `GET/POST /`
- `./apps/reporting/app.py:437` — `POST /generate-json`
- `./apps/reconciliation/app.py:199` — `GET/POST /`
- `./apps/reconciliation/app.py:269` — `GET /export/json`
- `./apps/reconciliation/app.py:287` — `GET /export/html`
- `./scripts/service_dashboard.py:446` — `POST /start/<service_name>`
- `./scripts/service_dashboard.py:454` — `POST /stop/<service_name>`

Impact :

- Un tiers pouvant accéder au réseau peut lancer des conversions, générer des fichiers, lire des exports ou piloter les services.
- Le dashboard peut démarrer/arrêter des processus applicatifs sans contrôle.
- Les exports de rapprochement sont protégés uniquement par un token UUID en paramètre d’URL.

Recommandations :

1. Ajouter une authentification minimale pour tous les services si exposition hors poste local.
2. Restreindre les endpoints mutatifs au loopback ou à un réseau de confiance.
3. Protéger le dashboard par authentification ou ne l’exposer strictement que sur `127.0.0.1`.
4. Éviter les tokens sensibles dans les URLs pour des rapports potentiellement confidentiels.

### 5.3 Élevé — Absence de protection CSRF sur les endpoints mutatifs

Endpoints concernés :

- `./apps/bdc_converter/app.py:78`
- `./apps/reporting/app.py:288`
- `./apps/reporting/app.py:437`
- `./apps/reconciliation/app.py:199`
- `./scripts/service_dashboard.py:446`
- `./scripts/service_dashboard.py:454`

Impact :

- Un navigateur authentifié implicitement par le réseau local pourrait être amené à déclencher des actions via une page externe.
- Risque de conversion forcée, génération forcée de JSON ou arrêt/démarrage de services.

Recommandations :

1. Ajouter une protection CSRF sur les formulaires et requêtes `POST`.
2. Vérifier l’en-tête `Origin` ou `Referer` pour les actions locales critiques.
3. Pour les API locales, exiger un secret statique ou token d’administration non accessible depuis une page tierce.

### 5.4 Élevé — Lecture de fichiers insuffisamment bornée dans le rapprochement

Code concerné :

- `./apps/reconciliation/app.py:217` à `./apps/reconciliation/app.py:224`
- `./apps/reconciliation/app.py:226` à `./apps/reconciliation/app.py:239`

Le backend reçoit `reporting_filename` et `bdc_filename` depuis le formulaire, construit les chemins avec `REPORTING_DIR / selected_reporting` et `BDC_DIR / selected_bdc`, puis vérifie seulement `exists()` et `is_file()` avant `read_text()`.

Le formulaire HTML ne propose que des fichiers issus de `list_available_reports()`, mais le backend n’impose pas que la valeur soumise appartienne réellement à cette liste. Une requête forgée pourrait tenter des chemins avec `..` vers d’autres fichiers JSON accessibles au processus.

Impact :

- Lecture potentielle de fichiers arbitraires si le chemin résolu existe et contient du JSON.
- Divulgation indirecte possible via erreurs ou analyse rendue.

Recommandations :

1. Refuser toute valeur contenant `/`, `\`, `..` ou des séparateurs de chemin.
2. Comparer la valeur reçue à une liste blanche issue de `list_available_reports()`.
3. Résoudre le chemin (`resolve()`) et vérifier qu’il reste sous le dossier attendu.
4. Vérifier l’extension et le pattern exact côté serveur, pas seulement côté UI.

### 5.5 Moyen — État global et écritures concurrentes non protégées

Zones concernées :

- `./apps/bdc_converter/app.py:109` à `./apps/bdc_converter/app.py:128`
- `./apps/reporting/app.py:461` à `./apps/reporting/app.py:464`
- `./apps/reporting/analyser.py:41` à `./apps/reporting/analyser.py:48`

Les conversions BDC et générations de rapports écrivent des fichiers déterministes :

- `convert_bdc_MM-YYYY.json`
- `./data/batches/YYYY-MM.json`
- `rapport-YYYY-MM.json`
- `./.cache/durations.json`

Aucun verrou inter-requête ou écriture atomique n’est en place.

Impact :

- Corruption ou incohérence possible si deux conversions/générations se chevauchent.
- Résultat final dépendant de la dernière requête terminée.
- Cache des durées potentiellement tronqué en cas d’arrêt brutal ou de concurrence multi-processus.

Recommandations :

1. Ajouter un verrou par ressource ou par mois.
2. Écrire dans un fichier temporaire puis renommer atomiquement.
3. Retourner un statut `409 Conflict` si une conversion du même mois est déjà en cours.

### 5.6 Moyen — Contrôle insuffisant des ressources sur PDF, archives et ffprobe

Zones concernées :

- `./apps/bdc_converter/convert_bdc.py:211` à `./apps/bdc_converter/convert_bdc.py:222`
- `./apps/bdc_converter/convert_bdc.py:225` à `./apps/bdc_converter/convert_bdc.py:265`
- `./apps/reporting/analyser.py:246` à `./apps/reporting/analyser.py:258`
- `./apps/reporting/analyser.py:189` à `./apps/reporting/analyser.py:206`

Le convertisseur traite tous les PDF présents côté serveur. L’analyse vidéo lance jusqu’à 8 workers et un `ffprobe` par vidéo candidate.

Impact :

- Risque de saturation CPU/I/O si de nombreux PDF ou fichiers vidéo sont présents.
- `pdfplumber` peut être coûteux sur des PDF volumineux ou malformés.
- Une archive locale malveillante peut référencer des chemins vidéo arbitraires via `metadata.json`.

Recommandations :

1. Définir un nombre maximal de PDF par conversion et une taille maximale par PDF.
2. Ajouter des métriques de durée et de volume traité.
3. Valider que les chemins vidéo référencés restent sous un périmètre attendu.
4. Rendre configurable le nombre de workers.

### 5.7 Moyen — Messages d’erreur internes renvoyés aux clients

Exemples :

- `./apps/bdc_converter/app.py:131` à `./apps/bdc_converter/app.py:132`
- `./apps/reporting/app.py:315` à `./apps/reporting/app.py:316`
- `./apps/reporting/app.py:362` à `./apps/reporting/app.py:363`
- `./apps/reporting/app.py:467` à `./apps/reporting/app.py:468`
- `./apps/reconciliation/app.py:260` à `./apps/reconciliation/app.py:261`

Impact :

- Divulgation de chemins locaux, noms de fichiers, traces logiques ou détails d’exception.
- Facilite le diagnostic pour un attaquant si le service est exposé.

Recommandations :

1. Journaliser l’exception complète côté serveur.
2. Retourner au client un message générique et un identifiant d’erreur.
3. Conserver les détails seulement en mode développement local.

### 5.8 Moyen — Incohérence fonctionnelle dans `/api/convert`

Code et test concernés :

- `./apps/bdc_converter/app.py:80` à `./apps/bdc_converter/app.py:106`
- `./apps/bdc_converter/app.py:38` à `./apps/bdc_converter/app.py:56`
- `./apps/bdc_converter/tests/test_app.py:46` à `./apps/bdc_converter/tests/test_app.py:52`

La fonction `sanitize_output_name()` existe et le test attend que `output_name` influence `output_file`. Le endpoint ignore aujourd’hui `payload` et passe `output_file=""` à `convert_folder`, avant de générer automatiquement `convert_bdc_MM-YYYY.json`.

Impact :

- Test en échec.
- Contrat API ambigu.
- Code mort ou obsolète autour de `sanitize_output_name()`.

Recommandations :

1. Décider si le nom de sortie est libre ou toujours inféré par mois.
2. Aligner le test, l’UI et le endpoint.
3. Supprimer `sanitize_output_name()` si la personnalisation n’est plus supportée, ou la réintégrer proprement.

### 5.9 Moyen — Robustesse insuffisante sur les données BDC malformées

Code concerné :

- `./libs/pipeline/bdc_to_batch.py:81` à `./libs/pipeline/bdc_to_batch.py:90`
- `./libs/pipeline/bdc_to_batch.py:95` à `./libs/pipeline/bdc_to_batch.py:110`

`parse_bdc_json_to_orders()` accède à `article_dict["description"]` avant le bloc `try` qui protège la création de `PurchaseOrder`. Un article sans `description` peut donc interrompre tout le parsing.

Impact :

- Un JSON BDC partiellement malformé peut casser une conversion batch.
- Les tests couvrent les commandes sans `numero_bdc`, mais pas les articles sans description.

Recommandations :

1. Utiliser `article_dict.get("description", "")`.
2. Valider chaque article indépendamment.
3. Retourner un rapport des lignes ignorées plutôt que seulement imprimer sur `stderr`.

### 5.10 Moyen — Erreurs de typage statique non résolues

Code concerné :

- `./libs/reconciliation/comparison.py:145`
- `./libs/reconciliation/comparison.py:179`
- `./libs/reconciliation/comparison.py:184`

Impact :

- `make lint` échoue systématiquement.
- Les types de structures d’agrégation sont trop flous, ce qui augmente le risque de régressions.

Recommandations :

1. Introduire des alias de type précis pour les agrégats de rapprochement.
2. Éviter de mélanger compteurs entiers et métadonnées (`_missing_ids`) dans un même dictionnaire typé `Dict[str, int]`.
3. Ajouter des tests unitaires autour des écarts de cluster multi-lots.

### 5.11 Faible à moyen — Clé secrète Flask de développement en fallback

Code concerné :

- `./apps/reconciliation/app.py:48`

`app.secret_key` utilise `RECONCILIATION_SECRET_KEY` ou `dev-reconciliation`. L’application ne semble pas s’appuyer fortement sur des sessions Flask aujourd’hui, mais ce fallback serait dangereux si des sessions signées étaient ajoutées.

Recommandations :

1. Exiger une clé forte hors mode développement.
2. Échouer au démarrage en production si la clé n’est pas fournie.

### 5.12 Faible à moyen — Headers de sécurité absents

Aucun durcissement global des réponses n’a été identifié :

- `Content-Security-Policy`
- `X-Content-Type-Options`
- `X-Frame-Options` ou `frame-ancestors`
- `Referrer-Policy`

Impact :

- Surface XSS/clickjacking plus large si une vulnérabilité de rendu apparaît.
- CSP plus difficile à ajouter ensuite car les templates contiennent des scripts et styles inline.

Recommandations :

1. Ajouter des headers de sécurité via `after_request` ou reverse proxy.
2. Déplacer progressivement les scripts inline vers des fichiers statiques pour permettre une CSP stricte.

## 6. Observations par service

### 6.1 BDC converter

Points forts :

- Le dossier PDF est côté serveur, donc pas d’upload direct exposé dans le endpoint actuel.
- Les noms de fichiers PDF sont listés via `os.listdir()` et filtrés sur `.pdf`.
- La conversion produit un `MonthlyBatch` partagé avec les autres modules.

Points à corriger :

- Endpoint `POST /api/convert` non authentifié et non protégé CSRF.
- Écriture concurrente possible de `convert_bdc_MM-YYYY.json` et `./data/batches/YYYY-MM.json`.
- Contrat API incohérent avec le test autour de `output_name`.
- Pas de plafond explicite sur le nombre ou la taille des PDF traités.
- Les erreurs sont renvoyées avec détails internes.

### 6.2 Reporting

Points forts :

- Validation de base du format `YYYY-MM` via split/int sur les endpoints d’export.
- `ffprobe` est lancé sans shell et avec timeout.
- Les noms de fichiers d’export sont construits à partir d’un mois parsé, ce qui limite le risque de traversal.

Points à corriger :

- Endpoints mutatifs sans authentification ni CSRF.
- `debug=True` si lancement direct.
- Écriture concurrente possible de rapports JSON.
- Cache de durées non atomique.
- Les chemins vidéo issus de `metadata.json` ne sont pas bornés à un répertoire de confiance.
- Exceptions renvoyées au client.

### 6.3 Reconciliation

Points forts :

- Store en mémoire borné par TTL et taille maximale.
- Tokens d’export générés avec `uuid.uuid4().hex`.
- Les listes proposées à l’utilisateur sont construites via glob + regex.

Points à corriger :

- Les valeurs soumises ne sont pas vérifiées contre la liste blanche réellement disponible.
- Risque de lecture de fichiers hors répertoire attendu via chemins forgés.
- Endpoints sans authentification ni CSRF.
- Store en mémoire non persistant et non partagé entre workers.
- Cleanup déclenché seulement lors du stockage, pas avant chaque lecture d’export.
- Fallback `secret_key` faible.

### 6.4 Dashboard de services

Points forts :

- Commandes statiques, pas d’injection de commande utilisateur directe.
- Hôte par défaut `127.0.0.1`.
- `debug=False`.

Points à corriger :

- Si `SERVICE_DASHBOARD_HOST` est changé, les routes start/stop deviennent très sensibles.
- Pas d’authentification ni de CSRF.
- Possibilité d’arrêter ou démarrer les services via requête HTTP forgée.

## 7. Qualité, maintenabilité et architecture

### 7.1 Couplage entre couche web et logique métier

Les services Flask contiennent à la fois :

- parsing des requêtes,
- orchestration métier,
- accès disque,
- sérialisation,
- construction des réponses.

Cela reste lisible pour un petit outil, mais complique les tests et le durcissement.

Recommandation : extraire des fonctions de service pures pour la conversion, la génération de rapport et le rapprochement, puis garder les routes Flask comme adaptateurs minces.

### 7.2 Validation métier légère

`./libs/schemas/__init__.py` impose quelques champs obligatoires, mais plusieurs champs structurants restent faiblement validés :

- format réel de `batch_id`,
- plage du mois,
- types exacts des listes/dictionnaires,
- cohérence des durées,
- existence ou confinement des chemins.

Recommandation : renforcer progressivement les validateurs au niveau schéma ou via fonctions de parsing dédiées.

### 7.3 Observabilité limitée

Le code utilise surtout `print()` ou des exceptions retournées au client.

Recommandation : adopter le module `logging`, inclure un identifiant de corrélation par requête et journaliser les erreurs côté serveur.

### 7.4 Tests incomplets sur la sécurité

Les tests actuels couvrent surtout la logique métier et quelques comportements endpoint. Il manque des tests sur :

- traversal dans `reporting_filename` / `bdc_filename`,
- refus des mois invalides,
- concurrence ou double lancement de conversion,
- JSON BDC partiellement malformé,
- absence de `description` dans un article,
- cohérence des headers de réponse,
- contrat réel de `/api/convert`.

## 8. Plan de remédiation recommandé

### Priorité P0 — Avant toute exposition réseau

1. Désactiver `debug=True` par défaut dans les trois apps.
2. Remplacer les hôtes par défaut exposés par `127.0.0.1` quand l’usage est local.
3. Ajouter une authentification minimale sur tous les services.
4. Ajouter une protection CSRF ou un token d’API pour les endpoints mutatifs.
5. Corriger la lecture de fichiers dans `./apps/reconciliation/app.py` avec whitelist + `resolve()` + vérification de répertoire parent.

### Priorité P1 — Fiabilité et robustesse

1. Corriger le test en échec ou le contrat de `/api/convert`.
2. Corriger les erreurs mypy dans `./libs/reconciliation/comparison.py`.
3. Ajouter des verrous et écritures atomiques pour les fichiers générés.
4. Ajouter des plafonds de taille/nombre pour les PDF et les analyses vidéo.
5. Remplacer les erreurs détaillées côté client par des messages génériques.
6. Renforcer le parsing BDC pour les articles incomplets.

### Priorité P2 — Durcissement progressif

1. Ajouter des headers de sécurité.
2. Déplacer les scripts inline vers des fichiers statiques pour permettre une CSP plus stricte.
3. Ajouter une journalisation structurée.
4. Ajouter des tests de sécurité et de non-régression.
5. Documenter explicitement le modèle de déploiement local/intranet/production.

## 9. Conclusion

L’application est bien structurée pour un flux métier local : conversion BDC, reporting vidéo et rapprochement partagent un modèle commun et les traitements dangereux classiques comme `shell=True` ou `eval` ne sont pas présents.

Les principaux risques sont liés à l’exposition réseau : les services sont conçus comme outils locaux mais plusieurs routes peuvent modifier l’état, lire des fichiers ou piloter des processus sans authentification ni CSRF. La priorité est donc de clarifier le modèle de déploiement, puis de sécuriser les routes mutatives et les accès fichiers avant toute utilisation hors machine locale contrôlée.
