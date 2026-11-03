# Fixtures BDC — corpus de référence

Corpus de non-régression pour `apps/bdc_converter/convert_bdc.py` : 7 bons de
commande Media Solution d'octobre/novembre 2026.

## Contenu

- `pdfs/` : les PDF d'origine (copies, jamais modifiés).
- `expected/` : la sortie attendue de `extract_bdc_data()`, au format canonique
  du projet (dates `JJ/MM/AAAA`, références normalisées, articles).
- `external/parseur/` : les JSON produits par Parseur. **Non normatifs.**
- `manifest.json` : SHA-256 de chaque PDF et notes de validation.

> Les PDF et les JSON Parseur contiennent des données client (nom, e-mail,
> téléphone). Ne pas publier ce dossier tel quel si le dépôt devient public.

## Provenance des attendus

Les valeurs ont été validées manuellement :

- **articles** : sortie identique entre notre extracteur et Parseur (7/7 après
  normalisation des nombres) ;
- **totaux** : valeurs Parseur, recoupées avec `Sous-total` / `Total` du PDF ;
- **dates** : lues dans le PDF, jamais reprises de Parseur ;
- **references** : sortie de notre extracteur, plus normalisée que Parseur.

Parseur interprète les dates ambiguës au format US : `01/10/2026` devient
`2026-01-10`, `02/10/2026` devient `2026-02-10` et `03/11/2026` devient
`2026-03-11`. C'est précisément le type d'erreur que P0 doit empêcher.

## Ajouter un PDF

1. Copier le PDF dans `pdfs/`.
2. Lancer l'extraction et relire le résultat (articles, dates, totaux).
3. Écrire `expected/<nom>.json` avec `expected_status` et `expected_warnings`.
4. Ajouter l'entrée dans `manifest.json` :

```bash
sha256sum tests/fixtures/bdc/pdfs/<nom>.pdf
```

5. Lancer `pytest tests/test_convert_bdc_extraction.py`.

Si un PDF est régénéré, le test échoue volontairement : il faut re-valider les
attendus puis mettre à jour le SHA-256.
