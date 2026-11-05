"""Facturation Flask app: versioned billing base from BDC + reconciliation."""

from __future__ import annotations

import base64
import csv
import io
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from flask import (
    Flask,
    make_response,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from jinja2 import ChoiceLoader, FileSystemLoader

from libs.billing import build_draft, diff_versions, validate_transition
from libs.billing.compute import add_manual_line, apply_line_update
from libs.billing.model import (
    ALLOWED_TRANSITIONS,
    BILLING_BASES,
    CATEGORY_KEYS,
    CATEGORY_LABELS,
    DEFAULT_SETTINGS,
    STATUS_BADGES,
    STATUS_LABELS,
    STATUS_ORDER,
    find_line,
    new_project,
    normalize_version,
    now_iso,
    project_index,
    version_index_entry,
)
from libs.billing.money import (
    decimal_str,
    format_amount_csv,
    format_amount_plain,
    format_eur,
    format_rate,
    parse_decimal,
    to_cents,
)
from libs.billing import store
from libs.web import (
    ResourceBusyError,
    log_exception,
    resource_lock,
    init_app_security,
)

BASE_DIR = Path(__file__).resolve().parent
ROOT_DIR = BASE_DIR.parents[1]
REPORTING_DIR = ROOT_DIR / "apps" / "reporting"
BDC_DIR = ROOT_DIR / "apps" / "bdc_converter"
RECONCILIATION_DIR = ROOT_DIR / "data" / "reconciliation"
BILLING_DATA_DIR = ROOT_DIR / "data" / "billing"

SHARED_STATIC_DIR = BASE_DIR.parent / "static"
SHARED_TEMPLATES_DIR = BASE_DIR.parent / "templates"

MONTH_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
INVOICE_NUMBER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{2,39}$")
MAX_PDF_SIZE = 6 * 1024 * 1024
EXPORT_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; font-src data:; "
    "img-src data:; base-uri 'none'; form-action 'none'"
)

FRENCH_MONTHS = (
    "janvier",
    "février",
    "mars",
    "avril",
    "mai",
    "juin",
    "juillet",
    "août",
    "septembre",
    "octobre",
    "novembre",
    "décembre",
)

app = Flask(__name__, static_folder=str(SHARED_STATIC_DIR), static_url_path="/static")
app.jinja_loader = ChoiceLoader(
    [
        FileSystemLoader(str(BASE_DIR / "templates")),
        FileSystemLoader(str(SHARED_TEMPLATES_DIR)),
    ]
)
init_app_security(app, default_port=5040)


def _format_fr_month(value: Any) -> str:
    text = str(value or "").strip()
    if not re.match(r"^\d{4}-\d{2}$", text):
        return text or "Mois non précisé"
    year, month = text.split("-")
    try:
        return f"{FRENCH_MONTHS[int(month) - 1]} {year}"
    except (ValueError, IndexError):
        return text


def _format_fr_date(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "—"
    if "T" in text:
        text = text.split("T", 1)[0]
    for pattern in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, pattern).strftime("%d/%m/%Y")
        except ValueError:
            continue
    return text


def _format_fr_datetime(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "—"
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return _format_fr_date(text)
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone()
    return parsed.strftime("%d/%m/%Y à %H:%M")


def _font_data_uri(filename: str) -> str:
    path = SHARED_STATIC_DIR / "fonts" / filename
    try:
        payload = base64.b64encode(path.read_bytes()).decode("ascii")
    except OSError:
        return ""
    return f"data:font/woff2;base64,{payload}"


app.jinja_env.filters["fr_month"] = _format_fr_month
app.jinja_env.filters["fr_date"] = _format_fr_date
app.jinja_env.filters["fr_datetime"] = _format_fr_datetime
app.jinja_env.filters["eur"] = format_eur
app.jinja_env.filters["amount"] = format_amount_plain
app.jinja_env.filters["rate"] = format_rate
app.jinja_env.filters["qty"] = lambda value: str(value or "1.00").replace(".", ",")
app.jinja_env.filters["status_label"] = lambda value: STATUS_LABELS.get(value, value)
app.jinja_env.filters["status_badge"] = lambda value: STATUS_BADGES.get(value, "badge--neutral")


def _format_score(value: Any) -> str:
    try:
        return f"{float(value):.1f}".replace(".", ",")
    except (TypeError, ValueError):
        return "0,0"


app.jinja_env.filters["score"] = _format_score
app.jinja_env.globals["font_uri"] = _font_data_uri
app.jinja_env.globals["category_labels"] = CATEGORY_LABELS
app.jinja_env.globals["category_keys"] = CATEGORY_KEYS
app.jinja_env.globals["billing_bases"] = BILLING_BASES
app.jinja_env.globals["status_labels"] = STATUS_LABELS


# ---------------------------------------------------------------------------
# Sources / months
# ---------------------------------------------------------------------------


def _month_from_bdc_filename(name: str) -> Optional[str]:
    match = re.match(r"convert_bdc_(\d{2})-(\d{4})\.json$", name)
    if not match:
        return None
    return f"{match.group(2)}-{match.group(1)}"


def _month_from_reporting_filename(name: str) -> Optional[str]:
    match = re.match(r"rapport-(\d{4}-\d{2})\.json$", name)
    return match.group(1) if match else None


def _reporting_path(month: str) -> Path:
    return REPORTING_DIR / f"rapport-{month}.json"


def _bdc_path(month: str) -> Path:
    return BDC_DIR / f"convert_bdc_{month[5:7]}-{month[:4]}.json"


def _snapshot_path(month: str) -> Path:
    return RECONCILIATION_DIR / f"{month}.json"


def _available_months() -> List[Dict[str, Any]]:
    months: Dict[str, Dict[str, Any]] = {}

    if REPORTING_DIR.is_dir():
        for path in REPORTING_DIR.glob("rapport-*.json"):
            month = _month_from_reporting_filename(path.name)
            if month:
                months.setdefault(month, {"value": month})["reporting"] = True

    if BDC_DIR.is_dir():
        for path in BDC_DIR.glob("convert_bdc_*.json"):
            month = _month_from_bdc_filename(path.name)
            if month:
                months.setdefault(month, {"value": month})["bdc"] = True

    for month in store.list_months(BILLING_DATA_DIR):
        months.setdefault(month, {"value": month})["project"] = True

    result = []
    for month in sorted(months, reverse=True):
        item = months[month]
        item.setdefault("reporting", False)
        item.setdefault("bdc", False)
        item.setdefault("project", False)
        item["label"] = _format_fr_month(month)
        result.append(item)
    return result


def _latest_month() -> str:
    months = _available_months()
    if months:
        return str(months[0]["value"])
    return datetime.now().strftime("%Y-%m")


def _read_json_file(path: Path, label: str) -> Any:
    if not path.is_file():
        raise ValueError(f"{label} introuvable pour ce mois.")
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} illisible : JSON invalide.") from exc


def _load_inputs(month: str) -> Tuple[Optional[dict], List[dict], Optional[dict]]:
    reporting_payload = None
    bdc_payload: List[dict] = []
    snapshot = None

    reporting_path = _reporting_path(month)
    if reporting_path.is_file():
        payload = _read_json_file(reporting_path, "Rapport de production")
        if not isinstance(payload, dict):
            raise ValueError("Le rapport de production doit être un objet JSON.")
        reporting_payload = payload

    bdc_path = _bdc_path(month)
    if bdc_path.is_file():
        payload = _read_json_file(bdc_path, "Rapport BDC")
        if not isinstance(payload, list):
            raise ValueError("Le rapport BDC doit être une liste JSON.")
        bdc_payload = payload

    snapshot_path = _snapshot_path(month)
    if snapshot_path.is_file():
        payload = _read_json_file(snapshot_path, "Snapshot de réconciliation")
        if isinstance(payload, dict):
            snapshot = payload

    return reporting_payload, bdc_payload, snapshot


def _source_snapshot(month: str, version: Optional[int] = None) -> Dict[str, Any]:
    sources: Dict[str, Any] = {}
    for key, path in (
        ("reporting", _reporting_path(month)),
        ("bdc", _bdc_path(month)),
        ("reconciliation", _snapshot_path(month)),
    ):
        info = store.file_info(path)
        info["missing"] = not bool(info.get("sha256"))
        if version is not None and not info["missing"]:
            stored = store.copy_source(path, month, version, BILLING_DATA_DIR)
            info = {**info, "copied": stored.get("sha256") == info.get("sha256")}
        sources[key] = info
    return sources


def _source_staleness(version: Dict[str, Any]) -> List[Dict[str, Any]]:
    changes = []
    stored_sources = version.get("sources") or {}
    for key, path in (
        ("reporting", _reporting_path(str(version.get("month")))),
        ("bdc", _bdc_path(str(version.get("month")))),
        ("reconciliation", _snapshot_path(str(version.get("month")))),
    ):
        current = store.file_info(path)
        stored = stored_sources.get(key) or {}
        stored_hash = stored.get("sha256") or ""
        current_hash = current.get("sha256") or ""
        if stored_hash != current_hash:
            changes.append(
                {
                    "key": key,
                    "name": current.get("name") or path.name,
                    "before": stored_hash,
                    "after": current_hash,
                }
            )
    return changes


def _bdc_pdf_issues(month: str, bdc_payload: List[dict]) -> List[Dict[str, Any]]:
    """Warn when a PDF represented in the BDC JSON is newer than that JSON."""
    json_path = _bdc_path(month)
    if not json_path.is_file():
        return []
    try:
        json_mtime = json_path.stat().st_mtime
    except OSError:
        return []
    sources = {
        str(order.get("source_filename") or "")
        for order in bdc_payload
        if order.get("source_filename")
    }
    issues: List[Dict[str, Any]] = []
    pdf_dir = BDC_DIR / "pdfs"
    if not pdf_dir.is_dir():
        return issues
    for pdf in sorted(pdf_dir.glob("*.pdf")):
        if pdf.name not in sources:
            continue
        try:
            pdf_mtime = pdf.stat().st_mtime
        except OSError:
            continue
        if pdf_mtime <= json_mtime + 1:
            continue
        issues.append(
            {
                "code": "bdc_pdf_newer",
                "severity": "warning",
                "message": (
                    f"Le PDF {pdf.name} est plus récent que le JSON BDC : "
                    "relancez l’acte 1 pour actualiser les montants."
                ),
                "context": {"pdf": pdf.name},
            }
        )
    return issues


# ---------------------------------------------------------------------------
# Projects / versions
# ---------------------------------------------------------------------------


def _load_project(month: str) -> Optional[dict]:
    return store.load_project(month, BILLING_DATA_DIR)


def _project_or_new(month: str) -> dict:
    project = _load_project(month)
    if project is None:
        project = new_project(month)
    project.setdefault("settings", dict(DEFAULT_SETTINGS))
    return project


def _save_version_and_index(
    month: str,
    version: dict,
    project: dict,
    *,
    overwrite: bool,
) -> None:
    store.save_version(month, version, BILLING_DATA_DIR, overwrite=overwrite)
    project["versions"] = [
        item
        for item in project.get("versions") or []
        if int(item.get("version") or 0) != int(version["version"])
    ]
    project["versions"].append(version_index_entry(version))
    project_index(project)
    project["settings"] = {**DEFAULT_SETTINGS, **(version.get("settings") or {})}
    store.save_project(project, BILLING_DATA_DIR)


def _current_version(project: Optional[dict]) -> Optional[dict]:
    if not project:
        return None
    number = int(project.get("current_version") or 0)
    if number <= 0:
        return None
    return store.load_version(str(project["month"]), number, BILLING_DATA_DIR)


def _settings_from_form(form: Any, fallback: dict) -> dict:
    settings = {**DEFAULT_SETTINGS, **(fallback or {})}
    basis = str(form.get("billing_basis") or "").strip()
    if basis in BILLING_BASES:
        settings["billing_basis"] = basis
    vat_rate = str(form.get("vat_rate") or "").strip()
    if vat_rate:
        settings["vat_rate"] = vat_rate

    if any(f"rate_{category}" in form for category in CATEGORY_KEYS):
        overrides: Dict[str, str] = {}
        for category in CATEGORY_KEYS:
            raw = str(form.get(f"rate_{category}") or "").strip()
            if not raw:
                continue
            parsed = parse_decimal(raw)
            if parsed is not None and parsed >= 0:
                overrides[category] = decimal_str(parsed)
        settings["rate_overrides"] = overrides
    return settings


def _build_new_version(
    month: str,
    *,
    project: dict,
    settings: dict,
    reason: str,
    previous: Optional[dict] = None,
    keep_manual_lines: bool = False,
    force_new: bool = False,
) -> dict:
    reporting_payload, bdc_payload, snapshot = _load_inputs(month)
    if not bdc_payload:
        raise ValueError("Aucun rapport BDC pour ce mois : lancez l’acte 1 avant de facturer.")
    if reporting_payload is None and snapshot is None:
        raise ValueError("Aucun rapport de production ni snapshot de réconciliation pour ce mois.")

    current = _current_version(project)
    if current and current.get("status") == "brouillon" and not force_new:
        version_number = int(current["version"])
        overwrite = True
        if previous is None:
            previous = current
        keep_manual_lines = True
    else:
        version_number = (
            max(
                int(project.get("current_version") or 0),
                int(current.get("version") or 0) if current else 0,
            )
            + 1
        )
        overwrite = False

    sources = _source_snapshot(month, version=version_number)
    draft = build_draft(
        month=month,
        reporting_payload=reporting_payload,
        bdc_payload=bdc_payload,
        snapshot=snapshot,
        sources=sources,
        settings=settings,
        version=version_number,
        reason=reason,
        previous=previous,
        keep_manual_lines=keep_manual_lines,
    )
    pdf_issues = _bdc_pdf_issues(month, bdc_payload)
    if pdf_issues:
        draft["issues"].extend(pdf_issues)
        severity_order = {"error": 0, "warning": 1, "info": 2}
        draft["issues"].sort(
            key=lambda item: (
                severity_order.get(str(item.get("severity") or ""), 9),
                str(item.get("code") or ""),
            )
        )
    _save_version_and_index(month, draft, project, overwrite=overwrite)
    return draft


def _load_version_for_request(
    month: str, version_number: Optional[int]
) -> Tuple[Optional[dict], Optional[dict]]:
    project = _load_project(month)
    if version_number:
        version = store.load_version(month, int(version_number), BILLING_DATA_DIR)
    else:
        version = _current_version(project)
    return project, version


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------


def _page_context(
    month: str,
    *,
    version_number: Optional[int] = None,
    error: Optional[str] = None,
    notice: Optional[str] = None,
) -> Dict[str, Any]:
    project, version = _load_version_for_request(month, version_number)
    previous = None
    if version and int(version.get("version") or 1) > 1:
        previous = store.load_version(month, int(version["version"]) - 1, BILLING_DATA_DIR)
    diff = diff_versions(previous, version) if version and previous else None
    staleness = _source_staleness(version) if version else []
    validation = {}
    next_statuses: List[str] = []
    if version:
        override_reason = str(version.get("override_reason") or "")
        current_status = str(version.get("status") or "brouillon")
        next_statuses = (
            []
            if current_status == "brouillon"
            else [
                target
                for target in STATUS_ORDER
                if target in ALLOWED_TRANSITIONS.get(current_status, ())
            ]
        )
        for target in next_statuses:
            validation[target] = validate_transition(
                version, target, override_reason=override_reason
            )

    attachment = None
    if version:
        path = store.attachment_path(month, int(version["version"]), BILLING_DATA_DIR)
        attachment = {"exists": path.is_file(), "name": path.name}

    versions = []
    if project:
        for item in sorted(
            project.get("versions") or [], key=lambda entry: int(entry.get("version") or 0)
        ):
            versions.append(item)

    return {
        "current_month": month,
        "month_label": _format_fr_month(month),
        "months": _available_months(),
        "project": project,
        "version": version,
        "versions": versions,
        "previous": previous,
        "diff": diff,
        "staleness": staleness,
        "validation": validation,
        "next_statuses": next_statuses,
        "attachment": attachment,
        "can_edit": bool(version and version.get("status") == "brouillon"),
        "is_draft": bool(version and version.get("status") == "brouillon"),
        "reporting_exists": _reporting_path(month).is_file(),
        "bdc_exists": _bdc_path(month).is_file(),
        "snapshot_exists": _snapshot_path(month).is_file(),
        "sources": _source_snapshot(month) if not version else version.get("sources") or {},
        "error": error,
        "notice": notice,
        "today": datetime.now().strftime("%Y-%m-%d"),
    }


def _render_index(
    month: str,
    *,
    version_number: Optional[int] = None,
    error: Optional[str] = None,
    notice: Optional[str] = None,
    status: int = 200,
):
    context = _page_context(
        month,
        version_number=version_number,
        error=error,
        notice=notice,
    )
    return render_template("index.html", **context), status


def _redirect_index(
    month: str,
    *,
    notice: Optional[str] = None,
    error: Optional[str] = None,
    version_number: Optional[int] = None,
):
    params: Dict[str, Any] = {"month": month}
    if version_number:
        params["version"] = version_number
    if notice:
        params["notice"] = notice
    if error:
        params["error"] = error
    return redirect(url_for("index", **params))


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/")
def index():
    requested = request.args.get("month") or _latest_month()
    if not MONTH_PATTERN.match(requested):
        return _render_index(
            _latest_month(),
            error="Format de mois invalide. Utilisez YYYY-MM.",
            status=400,
        )
    version_number = request.args.get("version", type=int)
    return _render_index(
        requested,
        version_number=version_number,
        error=request.args.get("error"),
        notice=request.args.get("notice"),
    )


@app.post("/draft")
def create_draft():
    month = request.form.get("month") or ""
    if not MONTH_PATTERN.match(month):
        return _redirect_index(_latest_month(), error="Mois invalide.")

    project = _project_or_new(month)
    settings = _settings_from_form(request.form, project.get("settings") or {})
    try:
        with resource_lock(f"billing:{month}"):
            draft = _build_new_version(
                month,
                project=project,
                settings=settings,
                reason=request.form.get("reason") or "Brouillon initial",
            )
    except ResourceBusyError:
        return _redirect_index(month, error="Un calcul est déjà en cours pour ce mois.")
    except ValueError as exc:
        return _redirect_index(month, error=str(exc))
    except Exception as exc:
        error_id = log_exception(exc, message="Billing draft failed")
        return _redirect_index(month, error=f"Erreur inattendue (réf. {error_id}).")

    return _redirect_index(
        month,
        version_number=int(draft["version"]),
        notice=f"Brouillon v{draft['version']} calculé.",
    )


@app.post("/recompute")
def recompute_draft():
    month = request.form.get("month") or ""
    if not MONTH_PATTERN.match(month):
        return _redirect_index(_latest_month(), error="Mois invalide.")

    project, current = _load_version_for_request(month, None)
    project = project or _project_or_new(month)
    settings = _settings_from_form(request.form, project.get("settings") or {})
    try:
        with resource_lock(f"billing:{month}"):
            draft = _build_new_version(
                month,
                project=project,
                settings=settings,
                reason=request.form.get("reason") or "Recalcul depuis les sources",
                previous=current,
                keep_manual_lines=bool(current and current.get("status") == "brouillon"),
            )
    except ResourceBusyError:
        return _redirect_index(month, error="Un calcul est déjà en cours pour ce mois.")
    except ValueError as exc:
        return _redirect_index(month, error=str(exc))
    except Exception as exc:
        error_id = log_exception(exc, message="Billing recompute failed")
        return _redirect_index(month, error=f"Erreur inattendue (réf. {error_id}).")

    return _redirect_index(
        month,
        version_number=int(draft["version"]),
        notice=f"Version v{draft['version']} recalculée.",
    )


@app.post("/new-version")
def new_version():
    month = request.form.get("month") or ""
    if not MONTH_PATTERN.match(month):
        return _redirect_index(_latest_month(), error="Mois invalide.")
    project, current = _load_version_for_request(month, None)
    project = project or _project_or_new(month)
    if not current:
        return _redirect_index(month, error="Aucune version source à dupliquer.")
    if current.get("status") == "brouillon":
        return _redirect_index(
            month,
            version_number=int(current["version"]),
            error=(
                "La version courante est un brouillon : utilisez « Recalculer depuis "
                "les sources » pour l’actualiser."
            ),
        )
    settings = _settings_from_form(request.form, project.get("settings") or {})
    try:
        with resource_lock(f"billing:{month}"):
            draft = _build_new_version(
                month,
                project=project,
                settings=settings,
                reason=request.form.get("reason") or "Nouvelle version depuis les sources",
                previous=current,
                keep_manual_lines=True,
                force_new=True,
            )
    except ResourceBusyError:
        return _redirect_index(month, error="Un calcul est déjà en cours pour ce mois.")
    except ValueError as exc:
        return _redirect_index(month, error=str(exc))
    except Exception as exc:
        error_id = log_exception(exc, message="Billing new version failed")
        return _redirect_index(month, error=f"Erreur inattendue (réf. {error_id}).")
    return _redirect_index(
        month,
        version_number=int(draft["version"]),
        notice=f"Nouvelle version v{draft['version']} créée.",
    )


def _load_draft_for_update(month: str, version_number: int):
    version = store.load_version(month, int(version_number), BILLING_DATA_DIR)
    if version is None:
        raise ValueError("Version introuvable.")
    if version.get("status") != "brouillon":
        raise ValueError("Cette version est gelée : créez une nouvelle version pour la modifier.")
    return version


def _save_draft(month: str, version: dict) -> None:
    project = _project_or_new(month)
    _save_version_and_index(month, version, project, overwrite=True)


@app.post("/lines/<path:line_id>")
def update_line(line_id: str):
    month = request.form.get("month") or ""
    version_number = request.form.get("version", type=int) or 0
    if not MONTH_PATTERN.match(month) or version_number <= 0:
        return _redirect_index(_latest_month(), error="Requête invalide.")
    try:
        with resource_lock(f"billing:{month}"):
            version = _load_draft_for_update(month, version_number)
            updated = apply_line_update(
                version,
                line_id,
                label=request.form.get("label"),
                quantity=request.form.get("quantity"),
                unit_price=request.form.get("unit_price"),
                note=request.form.get("note"),
                included=request.form.get("included") == "on",
                delivery_date=request.form.get("delivery_date"),
            )
            _save_draft(month, updated)
    except ResourceBusyError:
        return _redirect_index(month, error="Une mise à jour est déjà en cours.")
    except ValueError as exc:
        return _redirect_index(month, version_number=version_number, error=str(exc))
    except Exception as exc:
        error_id = log_exception(exc, message="Billing line update failed")
        return _redirect_index(
            month,
            version_number=version_number,
            error=f"Erreur inattendue (réf. {error_id}).",
        )
    return _redirect_index(month, version_number=version_number, notice="Ligne mise à jour.")


@app.post("/lines")
def create_line():
    month = request.form.get("month") or ""
    version_number = request.form.get("version", type=int) or 0
    if not MONTH_PATTERN.match(month) or version_number <= 0:
        return _redirect_index(_latest_month(), error="Requête invalide.")
    try:
        with resource_lock(f"billing:{month}"):
            version = _load_draft_for_update(month, version_number)
            updated = add_manual_line(
                version,
                label=request.form.get("label") or "",
                quantity=request.form.get("quantity") or "1",
                unit_price=request.form.get("unit_price") or "0",
                note=request.form.get("note") or "",
            )
            _save_draft(month, updated)
    except ResourceBusyError:
        return _redirect_index(month, error="Une mise à jour est déjà en cours.")
    except ValueError as exc:
        return _redirect_index(month, version_number=version_number, error=str(exc))
    except Exception as exc:
        error_id = log_exception(exc, message="Billing manual line failed")
        return _redirect_index(
            month,
            version_number=version_number,
            error=f"Erreur inattendue (réf. {error_id}).",
        )
    return _redirect_index(month, version_number=version_number, notice="Ligne manuelle ajoutée.")


@app.post("/lines/<path:line_id>/delete")
def delete_line(line_id: str):
    month = request.form.get("month") or ""
    version_number = request.form.get("version", type=int) or 0
    if not MONTH_PATTERN.match(month) or version_number <= 0:
        return _redirect_index(_latest_month(), error="Requête invalide.")
    try:
        with resource_lock(f"billing:{month}"):
            version = _load_draft_for_update(month, version_number)
            line = find_line(version, line_id)
            if line is None:
                raise ValueError("Ligne introuvable.")
            if line.get("kind") == "manual" or line.get("source") == "manual":
                version["lines"] = [
                    item
                    for item in version.get("lines") or []
                    if str(item.get("line_id")) != line_id
                ]
            else:
                line["included"] = False
            _save_draft(month, normalize_version(version))
    except ResourceBusyError:
        return _redirect_index(month, error="Une mise à jour est déjà en cours.")
    except ValueError as exc:
        return _redirect_index(month, version_number=version_number, error=str(exc))
    except Exception as exc:
        error_id = log_exception(exc, message="Billing line delete failed")
        return _redirect_index(
            month,
            version_number=version_number,
            error=f"Erreur inattendue (réf. {error_id}).",
        )
    return _redirect_index(month, version_number=version_number, notice="Ligne retirée.")


def _acknowledged_codes(form: Any) -> List[str]:
    values = form.getlist("acknowledge")
    return sorted({str(value) for value in values if str(value).strip()})


@app.post("/freeze")
def freeze_version():
    month = request.form.get("month") or ""
    version_number = request.form.get("version", type=int) or 0
    target = request.form.get("target_status") or "envoyé_pour_vérification"
    if not MONTH_PATTERN.match(month) or version_number <= 0:
        return _redirect_index(_latest_month(), error="Requête invalide.")
    if target not in {"envoyé_pour_vérification", "validé_client"}:
        return _redirect_index(month, error="Statut cible invalide.")
    try:
        with resource_lock(f"billing:{month}"):
            version = _load_draft_for_update(month, version_number)
            version["acknowledged_warnings"] = _acknowledged_codes(request.form)
            version["override_reason"] = request.form.get("override_reason") or ""
            blockers = validate_transition(
                version, target, override_reason=version["override_reason"]
            )
            if blockers:
                raise ValueError(" ".join(blockers[:4]))
            version["status"] = target
            version["frozen_at"] = now_iso()
            version["reason"] = request.form.get("reason") or version.get("reason") or ""
            _save_draft(month, normalize_version(version))
    except ResourceBusyError:
        return _redirect_index(month, error="Une mise à jour est déjà en cours.")
    except ValueError as exc:
        return _redirect_index(month, version_number=version_number, error=str(exc))
    except Exception as exc:
        error_id = log_exception(exc, message="Billing freeze failed")
        return _redirect_index(
            month,
            version_number=version_number,
            error=f"Erreur inattendue (réf. {error_id}).",
        )
    return _redirect_index(
        month,
        version_number=version_number,
        notice=f"Version v{version_number} gelée ({STATUS_LABELS.get(target, target)}).",
    )


@app.post("/status")
def update_status():
    month = request.form.get("month") or ""
    version_number = request.form.get("version", type=int) or 0
    target = request.form.get("target_status") or ""
    if not MONTH_PATTERN.match(month) or version_number <= 0:
        return _redirect_index(_latest_month(), error="Requête invalide.")
    if target not in STATUS_LABELS:
        return _redirect_index(month, error="Statut cible invalide.")
    if target == "brouillon":
        return _redirect_index(
            month,
            version_number=version_number,
            error="Une version gelée ne redevient pas brouillon.",
        )
    try:
        with resource_lock(f"billing:{month}"):
            version = store.load_version(month, version_number, BILLING_DATA_DIR)
            if version is None:
                raise ValueError("Version introuvable.")
            if version.get("status") == "brouillon":
                raise ValueError("Utilisez le gel du brouillon pour changer son statut.")
            version["acknowledged_warnings"] = _acknowledged_codes(request.form)
            override = request.form.get("override_reason") or version.get("override_reason") or ""
            version["override_reason"] = override
            blockers = validate_transition(version, target, override_reason=override)
            if blockers:
                raise ValueError(" ".join(blockers[:4]))
            version["status"] = target
            if target in {"remplacé", "annulé"}:
                version["reason"] = request.form.get("reason") or version.get("reason") or ""
            _save_draft(month, normalize_version(version))
    except ResourceBusyError:
        return _redirect_index(month, error="Une mise à jour est déjà en cours.")
    except ValueError as exc:
        return _redirect_index(month, version_number=version_number, error=str(exc))
    except Exception as exc:
        error_id = log_exception(exc, message="Billing status update failed")
        return _redirect_index(
            month,
            version_number=version_number,
            error=f"Erreur inattendue (réf. {error_id}).",
        )
    return _redirect_index(
        month,
        version_number=version_number,
        notice=f"Statut mis à jour : {STATUS_LABELS.get(target, target)}.",
    )


@app.post("/invoice")
def update_invoice():
    month = request.form.get("month") or ""
    version_number = request.form.get("version", type=int) or 0
    number = (request.form.get("invoice_number") or "").strip()
    issued_on = (request.form.get("issued_on") or "").strip()
    if not MONTH_PATTERN.match(month) or version_number <= 0:
        return _redirect_index(_latest_month(), error="Requête invalide.")
    if not INVOICE_NUMBER_PATTERN.match(number):
        return _redirect_index(
            month,
            version_number=version_number,
            error="Numéro de facture invalide (3 à 40 caractères alphanumériques).",
        )
    try:
        issued_date = datetime.strptime(issued_on, "%Y-%m-%d")
    except ValueError:
        return _redirect_index(
            month, version_number=version_number, error="Date d’émission invalide."
        )
    total_raw = (request.form.get("invoice_total") or "").strip()
    invoice_total_cents: Optional[int] = None
    if total_raw:
        parsed_total = parse_decimal(total_raw)
        if parsed_total is None or parsed_total < 0:
            return _redirect_index(
                month,
                version_number=version_number,
                error="Total Indy invalide.",
            )
        invoice_total_cents = to_cents(parsed_total)
    try:
        with resource_lock(f"billing:{month}"):
            version = store.load_version(month, version_number, BILLING_DATA_DIR)
            if version is None:
                raise ValueError("Version introuvable.")
            if version.get("status") == "brouillon":
                raise ValueError("La version doit être gelée avant d’enregistrer la facture Indy.")
            invoice = version.setdefault("invoice", {})
            invoice["number"] = number
            invoice["issued_on"] = issued_date.strftime("%Y-%m-%d")
            invoice["total_cents"] = (
                invoice_total_cents
                if invoice_total_cents is not None
                else int(version["totals"]["ht_cents"])
            )
            invoice["note"] = (request.form.get("invoice_note") or "").strip()
            _save_draft(month, normalize_version(version))
    except ResourceBusyError:
        return _redirect_index(month, error="Une mise à jour est déjà en cours.")
    except ValueError as exc:
        return _redirect_index(month, version_number=version_number, error=str(exc))
    except Exception as exc:
        error_id = log_exception(exc, message="Billing invoice metadata failed")
        return _redirect_index(
            month,
            version_number=version_number,
            error=f"Erreur inattendue (réf. {error_id}).",
        )
    return _redirect_index(
        month, version_number=version_number, notice="Métadonnées de facture enregistrées."
    )


@app.post("/attachment")
def upload_attachment():
    month = request.form.get("month") or ""
    version_number = request.form.get("version", type=int) or 0
    if not MONTH_PATTERN.match(month) or version_number <= 0:
        return _redirect_index(_latest_month(), error="Requête invalide.")
    upload = request.files.get("invoice_pdf")
    if upload is None or not upload.filename:
        return _redirect_index(
            month, version_number=version_number, error="Aucun fichier PDF sélectionné."
        )
    if not upload.filename.lower().endswith(".pdf"):
        return _redirect_index(
            month, version_number=version_number, error="Le fichier doit être un PDF."
        )
    payload = upload.read()
    if not payload or not payload.startswith(b"%PDF"):
        return _redirect_index(
            month, version_number=version_number, error="Le fichier PDF est invalide."
        )
    if len(payload) > MAX_PDF_SIZE:
        return _redirect_index(month, version_number=version_number, error="Le PDF dépasse 6 Mo.")
    try:
        with resource_lock(f"billing:{month}"):
            version = store.load_version(month, version_number, BILLING_DATA_DIR)
            if version is None:
                raise ValueError("Version introuvable.")
            store.save_attachment(month, version_number, payload, BILLING_DATA_DIR)
    except ValueError as exc:
        return _redirect_index(month, version_number=version_number, error=str(exc))
    except Exception as exc:
        error_id = log_exception(exc, message="Billing attachment failed")
        return _redirect_index(
            month,
            version_number=version_number,
            error=f"Erreur inattendue (réf. {error_id}).",
        )
    return _redirect_index(
        month, version_number=version_number, notice="PDF Indy attaché à la version."
    )


def _get_version_or_404(month: str, version_number: int) -> Optional[dict]:
    if not MONTH_PATTERN.match(month) or version_number <= 0:
        return None
    return store.load_version(month, version_number, BILLING_DATA_DIR)


@app.get("/invoice-pdf")
def download_invoice_pdf():
    month = request.args.get("month") or ""
    version_number = request.args.get("version", type=int) or 0
    version = _get_version_or_404(month, version_number)
    if version is None:
        return "Version introuvable", 404
    path = store.attachment_path(month, version_number, BILLING_DATA_DIR)
    if not path.is_file():
        return "Aucun PDF Indy attaché à cette version", 404
    return send_file(
        path,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"facture-{month}-v{version_number}.pdf",
    )


def _export_filename(month: str, version_number: int, extension: str) -> str:
    return f"facturation-{month}-v{version_number}.{extension}"


def _render_export(
    version: dict,
    *,
    pdf_mode: bool = False,
) -> str:
    previous = None
    if int(version.get("version") or 1) > 1:
        previous = store.load_version(
            str(version["month"]), int(version["version"]) - 1, BILLING_DATA_DIR
        )
    diff = diff_versions(previous, version) if previous else None
    return render_template(
        "export.html",
        version=version,
        diff=diff,
        pdf_mode=pdf_mode,
        month_label=_format_fr_month(version.get("month")),
        generated_at=datetime.now().isoformat(timespec="seconds"),
    )


@app.get("/export/html")
def export_html():
    month = request.args.get("month") or ""
    version_number = request.args.get("version", type=int) or 0
    version = _get_version_or_404(month, version_number)
    if version is None:
        return "Version introuvable", 404
    html = _render_export(version)
    response = make_response(html)
    response.headers["Content-Type"] = "text/html; charset=utf-8"
    response.headers["Content-Disposition"] = (
        f"attachment; filename={_export_filename(month, version_number, 'html')}"
    )
    response.headers["Content-Security-Policy"] = EXPORT_CSP
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/export/pdf")
def export_pdf():
    month = request.args.get("month") or ""
    version_number = request.args.get("version", type=int) or 0
    version = _get_version_or_404(month, version_number)
    if version is None:
        return "Version introuvable", 404
    try:
        from weasyprint import HTML as WeasyPrintHTML
    except Exception as exc:
        error_id = log_exception(exc, message="Billing PDF export unavailable")
        return f"Export PDF indisponible (réf. {error_id}).", 503

    try:
        html = _render_export(version, pdf_mode=True)
        pdf_bytes = WeasyPrintHTML(string=html, base_url=str(BASE_DIR)).write_pdf()
    except Exception as exc:
        error_id = log_exception(exc, message="Billing PDF export failed")
        return f"Erreur lors de l’export PDF (réf. {error_id}).", 500

    response = make_response(pdf_bytes)
    response.headers["Content-Type"] = "application/pdf"
    response.headers["Content-Disposition"] = (
        f"attachment; filename={_export_filename(month, version_number, 'pdf')}"
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/export/csv")
def export_csv():
    month = request.args.get("month") or ""
    version_number = request.args.get("version", type=int) or 0
    version = _get_version_or_404(month, version_number)
    if version is None:
        return "Version introuvable", 404

    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";", lineterminator="\n")
    writer.writerow(["Libellé", "Quantité", "Prix unitaire HT", "TVA", "Total HT"])
    for line in version.get("lines") or []:
        if not line.get("included", True):
            continue
        writer.writerow(
            [
                line.get("label") or "",
                decimal_str(line.get("quantity"), places=2).replace(".", ","),
                format_amount_csv(line.get("unit_price_cents")),
                format_rate(line.get("vat_rate")),
                format_amount_csv(line.get("total_cents")),
            ]
        )
    totals = version.get("totals") or {}
    writer.writerow([])
    writer.writerow(["Total HT", "", "", "", format_amount_csv(totals.get("ht_cents"))])
    writer.writerow(["TVA", "", "", "", format_amount_csv(totals.get("vat_cents"))])
    writer.writerow(["Total TTC", "", "", "", format_amount_csv(totals.get("ttc_cents"))])

    payload = "\ufeff" + buffer.getvalue()
    response = make_response(payload)
    response.headers["Content-Type"] = "text/csv; charset=utf-8"
    response.headers["Content-Disposition"] = (
        f"attachment; filename={_export_filename(month, version_number, 'csv')}"
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/guide")
def guide():
    path = BASE_DIR / "README.md"
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return "Guide indisponible.", 404
    return render_template(
        "guide.html",
        content=content,
        current_month=request.args.get("month"),
    )


if __name__ == "__main__":
    from libs.web import get_run_settings

    settings = get_run_settings(5040)
    app.run(host=settings.host, port=settings.port, debug=settings.debug)
