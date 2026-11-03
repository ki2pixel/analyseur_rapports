# Repository Guidelines

## Project Structure & Module Organization

Python monorepo with three Flask apps and shared libraries.

- **`apps/`**: Independent Flask applications:
  - **`bdc_converter/`**: PDF purchase orders → monthly JSON + `MonthlyBatch`; per-PDF control cards and BDC overlap alerts.
  - **`reporting/`**: Video archives / batches → monthly HTML/JSON reports; M6+ jingle filtering, deduplication, empty-lot suppression, duration details and warnings.
  - **`reconciliation/`**: Compare reporting vs BDC exports; `?month=YYYY-MM` auto-load, conformity score, split-view inspector, in-memory adjustments.
  - **`static/`**, **`templates/`**: Shared UI assets (incl. self-hosted fonts), base layout and the 3-act `_ritual_stepper.html`.
- **`libs/`**: Shared logic:
  - **`schemas/`**: Shared dataclasses (`MonthlyBatch`, `PurchaseOrder`, `VideoEntry`, `VideoGroupCategory`, …).
  - **`pipeline/`**: BDC JSON → `MonthlyBatch` + `video_names.py` (M6+ regex and name normalization).
  - **`reconciliation/`**: Contextual range expansion, BDC overlap detection, conformity score, balance and diagnostics.
  - **`web/`**: Cross-app hardening (auth/`ADMIN_TOKEN`, CSRF, security headers, nav URLs, safe paths, atomic I/O, logging).
- **`scripts/`**: `launch_services.sh` + service dashboard (port 5500) + `fetch_fonts.sh`.
- **`archives/`**: Input video folders (`<id> Camille YYYY-MM-DD_HH-MM-SS/`).
- **`data/`**: Generated artifacts (batches, …) — not versioned.
- **Reconciliation inputs**: `apps/reporting/rapport-YYYY-MM.json` + `apps/bdc_converter/convert_bdc_MM-YYYY.json`; `?month=YYYY-MM` pairs them. Regenerate only the affected month.
- **`.env` / `.env.example`**: Local secrets (`ADMIN_TOKEN`, `FLASK_SECRET_KEY`) and optional `BDC_URL` / `REPORTING_URL` / `RECONCILIATION_URL`. Never commit `.env`.

## Build, Test, and Development Commands

Use a virtualenv (`python3 -m venv venv`) then:

- **`make install-dev`**: Runtime + PDF + dev tools (`black`, `mypy`, `pytest`, …).
- **`make format`**: `black` (line length 100).
- **`make lint`**: `mypy` (Python 3.10).
- **`make test`**: full `pytest` suite.
- **`make serve-reporting`**: reporting on `127.0.0.1:5010`.
- **`make serve-bdc`**: BDC converter on `127.0.0.1:5000`.
- **`make serve-reconciliation`**: reconciliation on `127.0.0.1:5030`.
- **`./scripts/launch_services.sh`**: dashboard on `127.0.0.1:5500` (loads `.env`, then spawns services).

Defaults: bind **loopback**, **debug off**. Override with `APP_HOST` / `APP_DEBUG`. Local browser auth is automatic when `TRUST_LOOPBACK_AUTH` is enabled (default outside tests).

## Coding Style & Naming Conventions

- **Formatter**: `black`, line length 100 (`pyproject.toml`).
- **Type checking**: `mypy`, target 3.10.
- **Architecture**: exchange data via `libs/schemas` dataclasses; put reusable Flask security helpers in `libs/web`, not duplicated per app.
- **Video names**: use `libs/pipeline/video_names.py` (`EXCLUDED_VIDEO_PATTERN`, `is_excluded_video_name`, `normalize_video_name`); do not duplicate the M6+ regex or dedup normalization.
- **JSON compatibility**: `files_detail` (file name, duration, category) and `warnings` are optional additions to `rapport-*.json`; old reports without them must remain readable.
- **BDC extraction contract**: `extract_bdc_data()` returns `status` (`ok` / `partial` / `failed`), `warnings` and `totals`. `failed` documents are excluded from `MonthlyBatch`; `partial` stays usable but must surface its warnings. Dates are parsed strictly as `JJ/MM/AAAA`; never fall back to `datetime.now()` when no valid date exists. Multi-month lots are allowed but must be reported (`month.mixed`, batch warning), never silently mixed.
- **BDC table extraction**: header-driven by coordinates (`HEADER_SYNONYMS` / `UNIT_WORDS`); do not reintroduce positional “last three numbers” logic. Keep the table fallback for pages without a detectable header, and cover borderless / reordered / extra-column / multiline cases in `tests/test_convert_bdc_layout.py`.
- **BDC drift detection**: `layout.fingerprint` is the ordered list of canonical header column names (unknown columns keep their normalized label) compared with `EXPECTED_LAYOUT_FINGERPRINT`. A difference must stay non-blocking: emit the `info` `structure_modifiee` warning and the “Structure modifiée” UI badge, but never force `partial`/`failed` by itself. Update the constant and the `expected_layout_fingerprint` fixtures deliberately when the reference template changes.
- **Reconciliation contract**: `delta = actual - billed`; keep `cluster_results` (all BDC) distinct from `cluster_differences` (non-compliant only), and preserve `conformity`, `certification` and `overlapping_bdc_lots`.
- **Duplicate detection**: same normalized name + same category + duration within ±1.0 s; only the surplus occurrence is flagged/excluded (jingles are excluded for the whole month).
- **Disambiguated lots**: keep `309 (date)` IDs distinct. A single BDC referencing base lot `309` receives all its folders; when several BDCs reference the same base lot, allocate each folder to the BDC whose billed category mix it best completes (and keep genuine overlaps detectable).
- **Month context**: use `?month=YYYY-MM`; reconciliation GET auto-loads the matching pair, reporting GET only preselects the month.
- **UI semantics**: `.text-positive` is reserved for `delta == 0`; use `.text-warn` for `delta > 0` and `.text-negative` for `delta < 0`.

## Testing Guidelines

- **Framework**: `pytest`.
- **Locations**: `./tests/` (including security regressions) and `./apps/*/tests/`.
- **Commands**: `make test` or `pytest tests/test_filename.py`.
- Tests set `ADMIN_TOKEN` / disable CSRF as needed; loopback trust is off when `TESTING=True`.
- **Local archives may not cover every month.** When `archives/` lacks a month, load the versioned `rapport-YYYY-MM.json` / `convert_bdc_MM-YYYY.json` fixtures instead of scanning `archives/` (the July reconciliation test follows this rule).
- **BDC golden corpus**: `tests/fixtures/bdc/` (PDF + expected + SHA-256 manifest). Run `pytest tests/test_convert_bdc_extraction.py` after any change to `convert_bdc.py`; a changed PDF must be re-validated and its SHA-256 updated.
- Regression coverage expected: **120/300/420 s boundaries**, M6+ variants (`m6+…`, `m6_plus…`, `m6+max anime…`), duplicate names, empty lots, BDC range normalization (`38-92` → `LOTS 38 à 92`), French quantity parsing (`"9,00"`, `"27,00"`), contextual range expansion, BDC overlap detection, conformity score and inspector diagnostics.
- September regeneration check: `total_mp4 == 254`, no `m6+max anime.mp4` / `m6+.mp4` in report entries, lot 63 at **1 short + 2 standard**, `BDC-26-1528` compliant, and overlap `BDC-26-1479` / `BDC-26-1482` on lots **21, 23, 26, 27, 31, 33, 36, 39**.
- July regression expectations: `missing=[181,189,193,195]`, `unexpected=[215,233,239]`, no phantom lots in the `38 à 92` range.
- Inspector route tests use the Flask test client with CSRF disabled; keep adjustments session-scoped.

## Security notes for contributors

- Mutating routes need CSRF + admin token (unless loopback trust applies).
- Do not accept raw filesystem paths from the client; use whitelist + `libs.web.paths.safe_join_filename`.
- Prefer `libs.web.io.atomic_write_text` / `resource_lock` for generated artifacts.
- Client-facing errors: generic message + `error_id`; log full exceptions server-side.
- The `/inspector/exclude`, `/inspector/justify` and `/inspector/reset` routes are mutating and **session-only**: keep auth + CSRF and validate the token, filename and cluster key against the stored analysis.
- Month auto-load resolves filenames from `list_available_reports()` and opens them through `safe_join_filename`; never build a path from raw `?month=` input.
- Per-file PDF failures return a generic message; the full exception stays in server logs.
- Do not return absolute output paths to clients; return file names.

## UI conventions

- Dark theme lives in `apps/static/css/global.css` (Space Grotesk / Plus Jakarta Sans, accents `#f45b69` / `#2fd6c5`); no TailwindCSS.
- Set `active_page` in each page template so the shared nav and `_ritual_stepper.html` highlight the right act.
- Keep the global month selector (`data-month-form`) and propagate `?month=YYYY-MM` in inter-app links.
- No inline scripts (CSP `script-src 'self'`); inline styles are only for dynamic widths (score gauge, balance and stacked bars).
- L’export HTML de réconciliation est volontairement autonome : `apps/reconciliation/templates/export_styles.css` est un partial Jinja inliné dans `<style>` et les polices sont embarquées via `font_uri()` ; ne pas le remplacer par un `<link>` vers `/static`.
- L’export PDF réutilise ce document HTML via WeasyPrint (`pdf_mode=True`) et ajoute `export_pdf_styles.css` pour la pagination ; en mode PDF, les cartes d’écart renvoient le détail fichier à l’annexe 1 pour rester légères.
- Use business terminology: “Bons de commande (Regroupements de lots)”, “Lots produits (Dossiers monteur)”, “Lots facturés sans dossier vidéo ce mois-ci”, “Lots produits en attente de BDC”.
- Inspector adjustments are in-memory; never mutate the source JSON from a POST.

## Commit & Pull Request Guidelines

Descriptive commits; prefix by area when useful (`reporting:`, `libs/web:`, `bdc:`, `docs:`).
