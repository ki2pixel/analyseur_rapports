# Repository Guidelines

## Project Structure & Module Organization

Python monorepo with four Flask apps and shared libraries.

- **`apps/`**: Independent Flask applications (each documented in its local `README.md`):
  - **`bdc_converter/`**: PDF purchase orders → monthly JSON + `MonthlyBatch`; per-PDF control cards and BDC overlap alerts.
  - **`reporting/`**: Video archives / batches → monthly HTML/JSON reports; M6+ jingle filtering, deduplication, empty-lot suppression, duration details and warnings.
  - **`reconciliation/`**: Compare reporting vs BDC exports; `?month=YYYY-MM` auto-load, conformity score, split-view inspector, in-memory adjustments, and a **billing handoff** snapshot.
  - **`billing/`**: Versioned billing base from BDC JSON + reconciliation snapshot; one line per BDC, provision for uncovered production, statuses, diff, HTML/PDF/CSV exports and Indy invoice metadata.
  - **`static/`**, **`templates/`**: Shared UI assets (incl. self-hosted fonts), base layout and the 4-act `_ritual_stepper.html`.
- **`libs/`**: Shared logic:
  - **`schemas/`**: Shared dataclasses (`MonthlyBatch`, `PurchaseOrder`, `AnnexeVideoItem`, `VideoEntry`, `VideoCategory`, `VideoGroupCategory`, …).
  - **`pipeline/`**: BDC JSON → `MonthlyBatch` + `video_names.py` (M6+ regex and name normalization).
  - **`reconciliation/`**: Contextual range expansion, BDC overlap detection, conformity score, balance and diagnostics.
  - **`billing/`**: Pure billing compute (rate grid, draft lines, provision, version diff, transition guards) + filesystem store + Decimal money helpers.
  - **`web/`**: Cross-app hardening (auth/`ADMIN_TOKEN`, CSRF, security headers, nav URLs, safe paths, atomic I/O, logging).
- **`scripts/`**: `launch_services.sh` + service dashboard (port 5500) + `fetch_fonts.sh`.
- **`archives/`**: Input video folders (`<id> Camille YYYY-MM-DD_HH-MM-SS/`).
- **`data/`**: Generated artifacts (batches, reconciliation handoff snapshots, billing projects/versions/source snapshots) — not versioned.
- **Reconciliation inputs**: `apps/reporting/rapport-YYYY-MM.json` + `apps/bdc_converter/convert_bdc_MM-YYYY.json`; `?month=YYYY-MM` pairs them. Regenerate only the affected month.
- **Billing inputs**: the same two JSONs + optional `data/reconciliation/YYYY-MM.json` snapshot; billing projects live in `data/billing/YYYY-MM/`.
- **`.env` / `.env.example`**: Local secrets (`ADMIN_TOKEN`, `FLASK_SECRET_KEY`) and optional `BDC_URL` / `REPORTING_URL` / `RECONCILIATION_URL` / `BILLING_URL`. Never commit `.env`.

## Build, Test, and Development Commands

Use a virtualenv (`python3 -m venv venv`) then:

- **`make install-dev`**: Runtime + PDF + dev tools (`black`, `mypy`, `pytest`, …).
- **`make format`**: `black` (line length 100).
- **`make lint`**: `mypy` (Python 3.10).
- **`make test`**: full `pytest` suite.
- **`make serve-reporting`**: reporting on `127.0.0.1:5010`.
- **`make serve-bdc`**: BDC converter on `127.0.0.1:5000`.
- **`make serve-reconciliation`**: reconciliation on `127.0.0.1:5030`.
- **`make serve-billing`**: billing on `127.0.0.1:5040`.
- **`./scripts/launch_services.sh`**: dashboard on `127.0.0.1:5500` (loads `.env`, then spawns services).

Defaults: bind **loopback**, **debug off**. Override with `APP_HOST` / `APP_DEBUG`. Local browser auth is automatic when `TRUST_LOOPBACK_AUTH` is enabled (default outside tests).

## Coding Style & Naming Conventions

- **Formatter**: `black`, line length 100 (`pyproject.toml`).
- **Type checking**: `mypy`, target 3.10.
- **Architecture**: exchange data via `libs/schemas` dataclasses; put reusable Flask security helpers in `libs/web`, not duplicated per app.
- **Video names**: use `libs/pipeline/video_names.py` (`EXCLUDED_VIDEO_PATTERN`, `is_excluded_video_name`, `normalize_video_name`); do not duplicate the M6+ regex or dedup normalization.
- **JSON compatibility**: `files_detail` (file name, duration, category) and `warnings` are optional additions to `rapport-*.json`; old reports without them must remain readable.
- **BDC extraction contract**: `extract_bdc_data()` returns `status` (`ok` / `partial` / `failed`), `warnings` and `totals`. `failed` documents are excluded from `MonthlyBatch`; `partial` stays usable but must surface its warnings. Dates are parsed strictly as `JJ/MM/AAAA`; never fall back to `datetime.now()` when no valid date exists. Multi-month lots are allowed but must be reported (`month.mixed`, batch warning), never silently mixed.
- **BDC formats (`bdc_format`)**: `"simplified"` (historical: single page `# | Description | Quantité | Taux | Montant`, lots in `N° de réf`) and `"detailed"` (page 1 summary + one or more `Annexe : liste des vidéos` pages). A detailed BDC exposes `period`, `concerned_lots` (explicit list, authoritative over the `LOTS 42 À 311` range), `annex_declared_count` and `annex_videos` (`lot`, `video_name`, `duration_sec`, `category_label`, `montant`), plus an optional `annex_summary` (`total_videos`, `total_ht`). `annex_videos` feeds `MonthlyBatch.video_entries` (`duration_source="bdc_annex"`), never the article replica path. Never run `_parse_articles_from_table()` on an annex page: it must not pollute `articles`.
- **BDC identifiers**: the `BDC-` prefix is guaranteed, the suffix is variable (`BDC-26-1882`, `BDC-M6-0001`, `BDC-2026-M6-0001`). Use `^BDC-(?:[A-Z0-9]{1,12}-)+\d{1,8}$` for validation/extraction and keep `BDC_IDENTIFIER_PATTERN` in `libs/reconciliation/comparison.py` aligned, so an identifier never yields phantom lot numbers.
- **BDC table extraction**: header-driven by coordinates (`HEADER_SYNONYMS` / `UNIT_WORDS`); do not reintroduce positional “last three numbers” logic. Keep the table fallback for pages without a detectable header, and cover borderless / reordered / extra-column / multiline cases in `tests/test_convert_bdc_layout.py`. The video annex is parsed **type-driven** (montant → catégorie → durée → titre, lot in the first column), not by horizontal overlap: a token like the `0` of `0 min 18` starts before the `Durée` column and would be misassigned. Page footers (isolated page numbers) and the recap rows (`N × …`, `Total des vidéos : …`) must be filtered explicitly.
- **BDC drift detection**: `layout.fingerprint` is the ordered list of canonical header column names (unknown columns keep their normalized label) compared with `EXPECTED_LAYOUT_FINGERPRINT`. A difference must stay non-blocking: emit the `info` `structure_modifiee` warning and the “Structure modifiée” UI badge, but never force `partial`/`failed` by itself. Update the constant and the `expected_layout_fingerprint` fixtures deliberately when the reference template changes.
- **Reconciliation contract**: `delta = actual - billed`; keep `cluster_results` (all BDC) distinct from `cluster_differences` (non-compliant only), and preserve `conformity`, `certification` and `overlapping_bdc_lots`.
- **Billing contract**: the billing base is **not** a legal invoice. A BDC line is `min(réalisé, commandé) × taux` by default (`billing_basis = actual_capped`), with an optional `ordered` mode. Surplus production and lots without BDC go into the `uncovered:production` provision line. Never mutate a frozen version: create a new version and show the diff. A billed source snapshot (`data/reconciliation/YYYY-MM.json`) is preferred over recomputing without session adjustments. `émis` requires reconciliation certification or a documented override, plus the Indy number/date.
- **Billing money**: use `libs/billing/money.py` (`Decimal`, integer cents, `ROUND_HALF_UP`); never use floats for amounts, rates or totals.
- **Duplicate detection**: same normalized name + same category + duration within ±1.0 s; only the surplus occurrence is flagged/excluded (jingles are excluded for the whole month). When a detailed BDC explicitly bills both occurrences on two different lots (e.g. `Mortelle Adèle` on lots 209 and 219), the second one is genuinely ordered: no `jingle_or_duplicate` diagnostic.
- **Duration thresholds**: `short < 120 s`, `standard < 300 s`, `medium < 480 s` (5–8 min), `long ≥ 480 s` (8 min and more). These four bounds are shared by Reporting (`CategoryThresholds`), Reconciliation (`THRESHOLD_BOUNDARIES` / `_expected_category`) and Billing labels; do not reintroduce the historical `420 s` (5–7 min / ≥ 7 min) values.
- **Detailed video reconciliation**: when a cluster has an annex, compare it lot by lot with production. Matching key = normalized name without extension (so `.mov` ↔ `.mp4`), with a last-resort fallback on the radical without the studio suffix (`_TAT_M6`, `_TAT_W9`, `_TAT_Téva`, `_TAT_6ter`) inside the same lot. Statuses: `matched`, `category_mismatch`, `duration_drift` (> 5 s but same category, non-blocking `info`), `missing_in_production`, `extra_in_production`. A cluster exposes `has_video_detail` and `video_reconciliation`; simplified clusters keep `video_reconciliation = None`.
- **Option A (compensated lot mismatches)**: `is_compliant` and `actual_capped` stay evaluated at BDC level. Internal offsets between lots with a null aggregate delta emit a non-blocking `compensated_lot_mismatch` (`warn`) diagnostic; they never move volume to `uncovered:production` nor block certification.
- **Disambiguated lots**: keep `309 (date)` IDs distinct. A single BDC referencing base lot `309` receives all its folders; when several BDCs reference the same base lot, allocate each folder to the BDC whose billed category mix it best completes (and keep genuine overlaps detectable).
- **Month context**: use `?month=YYYY-MM`; reconciliation GET auto-loads the matching pair, reporting GET only preselects the month, billing GET auto-loads BDC + reporting + the optional reconciliation snapshot.
- **UI semantics**: `.text-positive` is reserved for `delta == 0`; use `.text-warn` for `delta > 0` and `.text-negative` for `delta < 0`.
- **Billing statuses**: `brouillon` → (`envoyé_pour_vérification` →) `validé_client` → `émis` → `remplacé`, with `annulé` reachable from `envoyé_pour_vérification` or `validé_client`; keep `brouillon` editable and every other version immutable.

## Testing Guidelines

- **Framework**: `pytest`.
- **Locations**: `./tests/` (including security regressions) and `./apps/*/tests/`.
- **Commands**: `make test` or `pytest tests/test_filename.py`.
- Tests set `ADMIN_TOKEN` / disable CSRF as needed; loopback trust is off when `TESTING=True`.
- **Local archives may not cover every month.** When `archives/` lacks a month, load the versioned `rapport-YYYY-MM.json` / `convert_bdc_MM-YYYY.json` fixtures instead of scanning `archives/` (the July reconciliation test follows this rule).
- **BDC golden corpus**: `tests/fixtures/bdc/` (PDF + expected + SHA-256 manifest). Run `pytest tests/test_convert_bdc_extraction.py` after any change to `convert_bdc.py`; a changed PDF must be re-validated and its SHA-256 updated.
- Regression coverage expected: **120/300/480 s boundaries**, M6+ variants (`m6+…`, `m6_plus…`, `m6+max anime…`), duplicate names, empty lots, BDC range normalization (`38-92` → `LOTS 38 à 92`), French quantity parsing (`"9,00"`, `"27,00"`), contextual range expansion, BDC overlap detection, conformity score and inspector diagnostics.
- September regeneration check: `total_mp4 == 254`, no `m6+max anime.mp4` / `m6+.mp4` in report entries, lot 63 at **1 short + 2 standard**, `BDC-26-1528` compliant, and overlap `BDC-26-1479` / `BDC-26-1482` on lots **21, 23, 26, 27, 31, 33, 36, 39**.
- July regression expectations: `missing=[181,189,193,195]`, `unexpected=[215,233,239]`, no phantom lots in the `38 à 92` range.
- November 2026 mixed-format check (`tests/test_november_mixed_formats.py`): runs when local `apps/bdc_converter/pdfs/` (`BDC-26-1882.pdf`, `BDC-M6-0001.pdf`) and `archives/` `2026-11` folders are present (otherwise skipped, complementing the versioned golden corpus in `tests/fixtures/bdc/`). Checks `BDC-26-1882` simplified = `48,00 €`, `BDC-M6-0001` detailed = `712,00 €` (73 lots, 134 videos), lot `40` without BDC = `28,00 €`, Total HT `788,00 €`, conformity `100 %`, no `missing_rate` error, no BDC overlap, no duplicate diagnostic on lots 209/219, lot `166` billed `medium` (`460,52 s`). `is_certified` stays `False` while lot `40` has no BDC: emission in Act 4 needs a documented override.
- Inspector route tests use the Flask test client with CSRF disabled; keep adjustments session-scoped.
- **Billing tests**: `tests/test_billing_logic.py` covers the rate grid, `actual_capped`/`ordered`, provision, adjacent-month exclusion, diffs and transition guards; `tests/test_billing_app.py` covers draft/freeze/status/export and the reconciliation handoff. Use temporary `data/billing` and `data/reconciliation` directories; never write to the real `data/` from tests.

## Security notes for contributors

- Mutating routes need CSRF + admin token (unless loopback trust applies).
- Do not accept raw filesystem paths from the client; use whitelist + `libs.web.paths.safe_join_filename`.
- Prefer `libs.web.io.atomic_write_text` / `resource_lock` for generated artifacts.
- Client-facing errors: generic message + `error_id`; log full exceptions server-side.
- The `/inspector/exclude`, `/inspector/justify` and `/inspector/reset` routes are mutating and **session-only**: keep auth + CSRF and validate the token, filename and cluster key against the stored analysis.
- The `/handoff/billing` route is mutating: keep auth + CSRF, resolve the analysis server-side, and write only to `data/reconciliation/YYYY-MM.json` with `atomic_write_text` + `resource_lock`.
- Billing mutating routes (`POST`) need CSRF + admin token; frozen versions may only receive metadata updates (status, invoice number/date, attachment), never line or total changes.
- Billing uploads: accept PDF only, cap at 6 MB, check the `%PDF` signature, and store under the generated version filename (never a client path).
- Month auto-load resolves filenames from `list_available_reports()` and opens them through `safe_join_filename`; never build a path from raw `?month=` input.
- Per-file PDF failures return a generic message; the full exception stays in server logs.
- Do not return absolute output paths to clients; return file names.

## UI conventions

- Dark theme lives in `apps/static/css/global.css` (Space Grotesk / Plus Jakarta Sans, accents `#f45b69` / `#2fd6c5`); no TailwindCSS.
- Set `active_page` in each page template so the shared nav and `_ritual_stepper.html` highlight the right act.
- Keep the global month selector (`data-month-form`) and propagate `?month=YYYY-MM` in inter-app links.
- No inline scripts (CSP `script-src 'self'`); inline styles are only for dynamic widths (score gauge, balance and stacked bars).
- L’export HTML de réconciliation est volontairement autonome : `apps/reconciliation/templates/export_styles.css` est un partial Jinja inliné dans `<style>` et les polices sont embarquées via `font_uri()` ; ne pas le remplacer par un `<link>` vers `/static`. Lorsque le mois inclut au moins un BDC détaillé, l’export affiche également l’encart **Rapprochement vidéo — BDC détaillé** (`video-strip`).
- L’export PDF réutilise ce document HTML via WeasyPrint (`pdf_mode=True`) et ajoute `export_pdf_styles.css` : une page unique dont la hauteur est calculée à partir du contenu (deux passes de mise en page), avec un plancher A4 et une mise à l’échelle uniforme si la hauteur dépasse la limite PDF de 14 400 pt. L’annexe fichiers reste en deux colonnes ; en mode PDF, les cartes d’écart renvoient le détail fichier à l’annexe 1 pour rester légères.
- L’export de facturation (`apps/billing/templates/export.html`) est **autonome** : `export_styles.css` est inliné dans `<style>`, les polices passent par `font_uri()`, aucun script et aucun `/static/`. Il porte la mention « Document de préparation — ne constitue pas une facture » et reste en A4 multi-pages.
- Le stepper est en **4 actes** ; l’acte 4 (`billing`) est actif sur la page de facturation et les statuts utilisent les badges existants (`badge--ok`, `badge--warn`, `badge--danger`, `badge--neutral`).
- Use business terminology: “Bons de commande (Regroupements de lots)”, “Lots produits (Dossiers monteur)”, “Lots facturés sans dossier vidéo ce mois-ci”, “Lots produits en attente de BDC”.
- Inspector adjustments are in-memory; never mutate the source JSON from a POST.

## Commit & Pull Request Guidelines

Descriptive commits; prefix by area when useful (`reporting:`, `libs/web:`, `bdc:`, `docs:`).
