# Fixtures BDC — corpus de référence

Corpus de non-régression pour `apps/bdc_converter/convert_bdc.py` : 9 bons de
commande Media Solution d'octobre/novembre 2026 (8 au format simplifié et 1 au
format détaillé avec annexe vidéo).

## Contenu

- `pdfs/` : les PDF d'origine (copies, jamais modifiés).
- `expected/` : la sortie attendue de `extract_bdc_data()`, au format canonique
  du projet (dates `JJ/MM/AAAA`, références normalisées, articles, empreinte de
  layout et métadonnées du format détaillé le cas échéant).
- `external/parseur/` : les JSON produits par Parseur pour les 7 premiers BDC
  d'octobre/novembre. **Non normatifs.** (`BDC-26-1882.pdf` et `BDC-M6-0001.pdf`
  ont `"external": null` et sont validés directement d'après les PDF.)
- `manifest.json` : SHA-256 de chaque PDF et notes de validation.

> Les PDF et les JSON Parseur contiennent des données client (nom, e-mail,
> téléphone). Ne pas publier ce dossier tel quel si le dépôt devient public.

## Provenance des attendus

Les valeurs ont été validées manuellement :

- **articles** : sortie identique entre notre extracteur et Parseur (7/7 pour les
  documents historiques après normalisation des nombres), complétée par la
  vérification manuelle de `BDC-26-1882.pdf` et `BDC-M6-0001.pdf` ;
- **totaux** : valeurs Parseur (quand présentes), recoupées avec `Sous-total` /
  `Total` du PDF ;
- **dates** : lues dans le PDF, jamais reprises de Parseur ;
- **references** : sortie de notre extracteur, plus normalisée que Parseur ;
- **annexe détaillée (`BDC-M6-0001.pdf`)** : lots concernés (73), nombre de
  vidéos en annexe (134) et total HT d'annexe (`712.00`) recoupés avec le PDF.

Parseur interprète les dates ambiguës au format US : `01/10/2026` devient
`2026-01-10`, `02/10/2026` devient `2026-02-10` et `03/11/2026` devient
`2026-03-11`. C'est précisément le type d'inversion JJ/MM ↔ MM/JJ que la
validation stricte des dates empêche.

## Ajouter un PDF

1. Copier le PDF dans `pdfs/`.
2. Lancer l'extraction (`extract_bdc_data()`) et relire le résultat (articles,
   dates, totaux, `layout.fingerprint` et, le cas échéant, `bdc_format`,
   `period`, `concerned_lots`, `annex_videos`, `annex_summary`).
3. Écrire `expected/<nom>.json` avec :
   - pour tous les formats : `expected_layout_fingerprint`, `expected_status` et
     `expected_warnings` ;
   - pour un BDC détaillé : `expected_bdc_format` (`"detailed"`),
     `expected_period`, `expected_concerned_lots`, `expected_annex_count` et
     `expected_annex_total_ht`.
4. Ajouter l'entrée dans `manifest.json` :

```bash
sha256sum tests/fixtures/bdc/pdfs/<nom>.pdf
```

5. Lancer `pytest tests/test_convert_bdc_extraction.py`.

Si un PDF est régénéré, le test échoue volontairement : il faut re-valider les
attendus puis mettre à jour le SHA-256.
