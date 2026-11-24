from __future__ import annotations

import base64
import copy
import hashlib
import json
import logging
import re
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from flask import (
    Flask,
    make_response,
    redirect,
    render_template,
    request,
    url_for,
)
from jinja2 import ChoiceLoader, FileSystemLoader
from werkzeug.datastructures import FileStorage

from libs.reconciliation import compare_reports, serialize_comparison
from libs.web import (
    PathValidationError,
    atomic_write_text,
    get_run_settings,
    init_app_security,
    log_exception,
    resource_lock,
    safe_join_filename,
)
from libs.web.headers import DEFAULT_CSP

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
ROOT_DIR = BASE_DIR.parents[1]
REPORTING_DIR = ROOT_DIR / "apps" / "reporting"
BDC_DIR = ROOT_DIR / "apps" / "bdc_converter"
HANDOFF_DIR = ROOT_DIR / "data" / "reconciliation"

SHARED_STATIC_DIR = BASE_DIR.parent / "static"
SHARED_TEMPLATES_DIR = BASE_DIR.parent / "templates"

MAX_UPLOAD_SIZE = 6 * 1024 * 1024  # 6 MB per file
ALLOWED_MIME_PREFIXES = ("application/json", "text/json", "text/plain")
MONTH_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
REPORTING_MONTH_PATTERN = re.compile(r"rapport-(\d{4}-\d{2})\.json$")

CATEGORY_KEYS = ("short", "standard", "medium", "long")
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
EXPORT_CSP = DEFAULT_CSP.replace("font-src 'self'", "font-src 'self' data:")

# Export PDF : une page continue dont la hauteur est calée sur le contenu.
PDF_PAGE_BOTTOM_MARGIN_MM = 14  # aligné sur la marge basse de @page dans export_pdf_styles.css
PDF_PAGE_BOTTOM_MARGIN_PX = PDF_PAGE_BOTTOM_MARGIN_MM / 25.4 * 96
PDF_MIN_PAGE_HEIGHT_PX = 297 / 25.4 * 96  # plancher A4 pour les rapports courts
PDF_PROBE_PAGE_HEIGHT_PX = 60_000.0
PDF_PROBE_MAX_ATTEMPTS = 3
PDF_PAGE_HEIGHT_SAFETY_PX = 2.0
PDF_MAX_PAGE_HEIGHT_PT = 14_400.0  # limite PDF 1.7 : 200 pouces

logger = logging.getLogger("analyseur.reconciliation")


def _format_fr_month(value: Any) -> str:
    match = re.match(r"^(\d{4})-(\d{2})$", str(value or ""))
    if not match:
        return str(value or "")
    year, month = int(match.group(1)), int(match.group(2))
    if not 1 <= month <= 12:
        return str(value)
    return f"{FRENCH_MONTHS[month - 1].capitalize()} {year}"


def _format_fr_datetime(value: Any) -> str:
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    else:
        parsed = parsed.astimezone(timezone.utc)
    return f"{parsed.day} {FRENCH_MONTHS[parsed.month - 1]} {parsed.year} " f"à {parsed:%H:%M} UTC"


def _lot_total(group: Optional[dict]) -> int:
    if not group:
        return 0
    categories = group.get("categories") or {}
    return sum(int((categories.get(cat) or {}).get("count") or 0) for cat in CATEGORY_KEYS)


def _lot_sort_key(value: Any) -> Tuple[int, int, str]:
    text = str(value or "").strip()
    digits = re.findall(r"\d+", text)
    if digits:
        try:
            return (0, int(digits[0]), text)
        except ValueError:
            pass
    return (1, 0, text)


def _cluster_sort_key(cluster: dict) -> Tuple[int, int, str]:
    keys = [_lot_sort_key(value) for value in cluster.get("cluster_ids") or []]
    return min(keys) if keys else (2, 0, "")


@lru_cache(maxsize=None)
def _font_data_uri(filename: str) -> str:
    path = SHARED_STATIC_DIR / "fonts" / filename
    try:
        payload = base64.b64encode(path.read_bytes()).decode("ascii")
    except OSError:
        return ""
    return f"data:font/woff2;base64,{payload}"


app = Flask(
    __name__,
    template_folder=str(TEMPLATES_DIR),
    static_folder=str(SHARED_STATIC_DIR),
    static_url_path="/static",
)

app.jinja_loader = ChoiceLoader(
    [
        FileSystemLoader(str(TEMPLATES_DIR)),
        FileSystemLoader(str(SHARED_TEMPLATES_DIR)),
    ]
)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_SIZE * 2
init_app_security(app, default_port=5030)
app.jinja_env.filters["fr_month"] = _format_fr_month
app.jinja_env.filters["fr_datetime"] = _format_fr_datetime
app.jinja_env.filters["lot_total"] = _lot_total
app.jinja_env.globals["font_uri"] = _font_data_uri


@dataclass
class StoredAnalysis:
    token: str
    reporting_name: str
    bdc_name: str
    reporting_payload: dict
    bdc_payload: list
    analysis: dict
    created_at: datetime
    month: Optional[str] = None
    adjacent_months: Optional[Dict[str, Dict[str, Any]]] = None
    excluded_files: Dict[Tuple[str, str], int] = field(default_factory=dict)
    justified_clusters: Set[str] = field(default_factory=set)
    source_fingerprint: str = ""


_store: Dict[str, StoredAnalysis] = {}
_store_lock = threading.Lock()


def _read_json_upload(upload: Optional[FileStorage], label: str):
    if upload is None or upload.filename == "":
        raise ValueError(f"Le fichier {label} est requis.")

    content_type = upload.mimetype or ""
    if content_type:
        if not content_type.startswith(ALLOWED_MIME_PREFIXES):
            raise ValueError(f"{label} doit être un fichier JSON (type reçu: {content_type}).")

    raw = upload.read()
    if not raw:
        raise ValueError(f"{label} est vide.")
    if len(raw) > MAX_UPLOAD_SIZE:
        raise ValueError(
            f"{label} dépasse la taille maximale autorisée ({MAX_UPLOAD_SIZE // (1024 * 1024)} Mo)."
        )

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} n'est pas un JSON valide : {exc}") from exc

    return data


def _cleanup_store():
    now = datetime.now(timezone.utc)
    to_remove = []
    for token, record in _store.items():
        if (now - record.created_at).total_seconds() > 7200:
            to_remove.append(token)
    for token in to_remove:
        _store.pop(token, None)

    MAX_STORE_SIZE = 50
    if len(_store) > MAX_STORE_SIZE:
        sorted_records = sorted(_store.items(), key=lambda x: x[1].created_at)
        num_to_remove = len(_store) - MAX_STORE_SIZE
        for i in range(num_to_remove):
            _store.pop(sorted_records[i][0], None)


def _file_fingerprint(path: Path) -> str:
    try:
        stat = path.stat()
    except OSError:
        return "missing"
    return f"{stat.st_size}:{stat.st_mtime_ns}"


def _sha256_file(path: Path) -> str:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return ""


def _source_fingerprint(reporting_name: str, bdc_name: str) -> str:
    return "|".join(
        (
            _file_fingerprint(REPORTING_DIR / reporting_name),
            _file_fingerprint(BDC_DIR / bdc_name),
        )
    )


def _store_analysis(
    reporting_name: str,
    bdc_name: str,
    reporting_payload: dict,
    bdc_payload: list,
    analysis: dict,
    *,
    month: Optional[str] = None,
    adjacent_months: Optional[Dict[str, Dict[str, Any]]] = None,
) -> str:
    token = uuid.uuid4().hex
    record = StoredAnalysis(
        token=token,
        reporting_name=reporting_name,
        bdc_name=bdc_name,
        reporting_payload=reporting_payload,
        bdc_payload=bdc_payload,
        analysis=analysis,
        created_at=datetime.now(timezone.utc),
        month=month,
        adjacent_months=adjacent_months,
        source_fingerprint=_source_fingerprint(reporting_name, bdc_name),
    )
    with _store_lock:
        _cleanup_store()
        _store[token] = record
    return token


def _get_analysis(token: str) -> StoredAnalysis:
    with _store_lock:
        _cleanup_store()
        record = _store.get(token)
        if record is None:
            raise KeyError(token)
        return record


def _resolve_record(
    token: str,
    *,
    month: Optional[str] = None,
    reporting_name: Optional[str] = None,
    bdc_name: Optional[str] = None,
) -> Optional[StoredAnalysis]:
    """
    Resolve an analysis for a request.

    A valid token always wins.  An expired token (or a token whose source JSON
    changed on disk) is never silently replaced by another session's in-memory
    analysis: a fresh analysis is rebuilt from the files and the new record is
    returned.  Callers compare ``record.token`` with the requested token to
    display an explicit notice.
    """
    if token:
        with _store_lock:
            _cleanup_store()
            record = _store.get(token)
            if record is not None:
                current_fingerprint = _source_fingerprint(record.reporting_name, record.bdc_name)
                if (
                    not record.source_fingerprint
                    or current_fingerprint == record.source_fingerprint
                ):
                    return record
                _store.pop(token, None)

    if not month and not (reporting_name and bdc_name):
        return None

    try:
        reporting_reports, bdc_reports = list_available_reports()
        target_reporting = reporting_name
        target_bdc = bdc_name
        if not target_reporting or not target_bdc:
            if month and MONTH_PATTERN.match(month):
                target_reporting = _find_report_for_month(reporting_reports, month)
                target_bdc = _find_bdc_for_month(bdc_reports, month)

        if target_reporting and target_bdc:
            return _create_analysis_record(
                target_reporting,
                target_bdc,
                reporting_reports,
                bdc_reports,
            )
    except Exception:
        logger.warning(
            "Auto-recovery of reconciliation analysis failed (token=%s, month=%s, rep=%s, bdc=%s)",
            token,
            month,
            reporting_name,
            bdc_name,
            exc_info=True,
        )

    return None


def _month_from_reporting_name(filename: str) -> Optional[str]:
    match = REPORTING_MONTH_PATTERN.search(filename or "")
    return match.group(1) if match else None


def _shift_month(month: str, delta: int) -> str:
    year, month_number = (int(part) for part in month.split("-"))
    total = year * 12 + (month_number - 1) + delta
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


def _find_report_for_month(reports: List[dict], month: str) -> Optional[str]:
    for report in reports:
        if report.get("month") == month:
            return str(report.get("filename"))
    return None


def _find_bdc_for_month(reports: List[dict], month: str) -> Optional[str]:
    bdc_month = f"{month[5:7]}-{month[:4]}"
    for report in reports:
        if report.get("month") == bdc_month:
            return str(report.get("filename"))
    return None


def _read_json_file(path: Path, label: str):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"Erreur de lecture du {label}.") from exc


def _load_adjacent_months(
    month: str,
    reporting_reports: List[dict],
    bdc_reports: List[dict],
) -> Optional[Dict[str, Dict[str, Any]]]:
    if not MONTH_PATTERN.match(month):
        return None

    reporting_allowed = {str(report["filename"]) for report in reporting_reports}
    bdc_allowed = {str(report["filename"]) for report in bdc_reports}
    adjacent: Dict[str, Dict[str, Any]] = {}

    for delta in (-1, 1):
        neighbor = _shift_month(month, delta)
        reporting_name = _find_report_for_month(reporting_reports, neighbor)
        bdc_name = _find_bdc_for_month(bdc_reports, neighbor)
        if not reporting_name and not bdc_name:
            continue
        try:
            reporting_payload: dict = {}
            if reporting_name:
                reporting_path = safe_join_filename(
                    REPORTING_DIR,
                    reporting_name,
                    allowed_names=reporting_allowed,
                )
                loaded_report = _read_json_file(reporting_path, "rapport de production adjacent")
                if isinstance(loaded_report, dict):
                    reporting_payload = loaded_report

            bdc_payload: list = []
            if bdc_name:
                bdc_path = safe_join_filename(
                    BDC_DIR,
                    bdc_name,
                    allowed_names=bdc_allowed,
                )
                loaded_bdc = _read_json_file(bdc_path, "rapport BDC adjacent")
                if isinstance(loaded_bdc, list):
                    bdc_payload = loaded_bdc

            if not reporting_payload and not bdc_payload:
                continue
            adjacent[neighbor] = {"report": reporting_payload, "bdc": bdc_payload}
        except Exception:
            logger.warning("Impossible de charger le mois adjacent %s", neighbor, exc_info=True)

    return adjacent or None


def _cluster_has_differences(cluster: dict) -> bool:
    delta = cluster.get("delta") or {}
    has_delta = any(val != 0 for val in delta.values())
    has_missing = bool(cluster.get("missing_reporting_ids"))
    return has_delta or has_missing or not cluster.get("is_compliant", True)


def _strip_justified_duplicate_highlights(cluster: dict) -> dict:
    """Retire les mises en avant de doublon d'un cluster justifié, pour l'export."""
    if not cluster.get("justified"):
        return cluster

    cleaned = copy.deepcopy(cluster)
    diagnostics = [
        diagnostic
        for diagnostic in cleaned.get("diagnostics") or []
        if not (
            diagnostic.get("code") == "jingle_or_duplicate"
            and diagnostic.get("suspect_kind") == "duplicate"
        )
    ]
    cleaned["diagnostics"] = diagnostics
    current = cleaned.get("diagnostic")
    if (
        current
        and current.get("code") == "jingle_or_duplicate"
        and current.get("suspect_kind") == "duplicate"
    ):
        cleaned["diagnostic"] = diagnostics[0] if diagnostics else None
    cleaned["has_jingle_or_duplicate"] = any(
        diagnostic.get("code") == "jingle_or_duplicate" for diagnostic in diagnostics
    )

    for lot in cleaned.get("production_lots") or []:
        lot_has_suspects = False
        for category in (lot.get("categories") or {}).values():
            for detail in category.get("file_details") or []:
                if detail.get("is_duplicate"):
                    detail["is_duplicate"] = False
                detail["is_suspect"] = bool(
                    detail.get("is_jingle")
                    or detail.get("is_near_boundary")
                    or detail.get("is_duplicate")
                )
                lot_has_suspects = lot_has_suspects or detail["is_suspect"]
        lot["has_suspects"] = lot_has_suspects
    return cleaned


def _build_annex_summary(
    analysis: dict,
    reporting_groups: List[dict],
    missing_invoices: List[dict],
    cluster_results: List[dict],
) -> dict:
    lot_totals = {
        str(group.get("id_camille") or ""): _lot_total(group) for group in reporting_groups
    }
    claims: Dict[str, int] = {}
    for cluster in cluster_results:
        for gid in cluster.get("cluster_ids") or []:
            key = str(gid)
            claims[key] = claims.get(key, 0) + 1

    overlap_actual = sum(
        lot_totals.get(gid, 0) * (count - 1) for gid, count in claims.items() if count > 1
    )
    unattached_ids = [str(item.get("id_camille") or "") for item in missing_invoices]
    unattached_actual = sum(lot_totals.get(gid, 0) for gid in unattached_ids)

    cluster_billed_total = sum(int(cluster.get("billed_total") or 0) for cluster in cluster_results)
    cluster_actual_total = sum(int(cluster.get("actual_total") or 0) for cluster in cluster_results)
    bdc_totals = (analysis.get("bdc") or {}).get("totals") or {}
    reporting_totals = (analysis.get("reporting") or {}).get("totals") or {}
    grand_billed = int(bdc_totals.get("overall") or cluster_billed_total)
    grand_actual = int(reporting_totals.get("overall") or sum(lot_totals.values()))

    return {
        "cluster_billed_total": cluster_billed_total,
        "cluster_actual_total": cluster_actual_total,
        "overlap_actual": overlap_actual,
        "unattached_billed": 0,
        "unattached_actual": unattached_actual,
        "unattached_ids": unattached_ids,
        "grand_billed": grand_billed,
        "grand_actual": grand_actual,
        "grand_delta": grand_actual - grand_billed,
        "unknown_billed": int(bdc_totals.get("unknown") or analysis.get("unknown_billed") or 0),
    }


def _clusters_with_video_detail(cluster_results: List[dict]) -> List[dict]:
    return [cluster for cluster in cluster_results if cluster.get("has_video_detail")]


def _build_video_detail_summary(
    cluster_results: List[dict],
    missing_invoices: List[dict],
) -> Optional[dict]:
    """Agrège le rapprochement vidéo des BDC détaillés pour l'export.

    Retourne ``None`` quand le mois ne contient aucun BDC détaillé : les mois
    entièrement simplifiés gardent un export inchangé.
    """
    clusters = _clusters_with_video_detail(cluster_results)
    if not clusters:
        return None

    annex_total = 0
    counts: Dict[str, int] = {}
    drifts: List[dict] = []
    missing: List[dict] = []
    bdc_numbers: List[str] = []
    for cluster in clusters:
        reconciliation = cluster.get("video_reconciliation") or {}
        annex_total += int(reconciliation.get("annex_total") or 0)
        for key, value in (reconciliation.get("counts") or {}).items():
            counts[key] = counts.get(key, 0) + int(value or 0)
        drifts.extend(reconciliation.get("duration_drifts") or [])
        missing.extend(
            item
            for item in (reconciliation.get("items") or [])
            if item.get("status") == "missing_in_production"
        )
        for number in reconciliation.get("bdc_numbers") or []:
            if number not in bdc_numbers:
                bdc_numbers.append(str(number))

    matched = counts.get("matched", 0) + counts.get("duration_drift", 0)
    missing_lots = [
        str(item.get("id_camille") or "") for item in missing_invoices if item.get("id_camille")
    ]

    return {
        "bdc_numbers": bdc_numbers,
        "annex_total": annex_total,
        "matched": matched,
        "match_rate": round(100 * matched / annex_total, 1) if annex_total else 0.0,
        "counts": counts,
        "duration_drifts": drifts,
        "missing_in_production": missing,
        "missing_lots": missing_lots,
    }


def _build_view_model(analysis: dict, *, strip_justified_duplicates: bool = False) -> dict:
    analysis = analysis or {}
    cluster_results = analysis.get("cluster_results") or analysis.get("cluster_differences") or []
    if strip_justified_duplicates:
        cluster_results = [
            _strip_justified_duplicate_highlights(cluster) for cluster in cluster_results
        ]
    conformity = analysis.get("conformity") or {
        "score": 0.0,
        "compliant_orders": 0,
        "total_orders": 0,
        "review_clusters": 0,
        "total_clusters": 0,
    }
    certification = analysis.get("certification") or {
        "is_certified": False,
        "blocking_issues": analysis.get("issues_count", 0),
    }
    clusters_with_differences = [
        cluster for cluster in cluster_results if _cluster_has_differences(cluster)
    ]
    reporting_payload = analysis.get("reporting") or {}
    reporting_groups = list((reporting_payload.get("groups") or {}).values())
    missing_invoices = analysis.get("missing_invoices") or []
    balance = analysis.get("balance") or {}
    review_clusters = [cluster for cluster in cluster_results if cluster.get("requires_review")]
    annex = _build_annex_summary(
        analysis,
        reporting_groups,
        missing_invoices,
        cluster_results,
    )
    return {
        "unexpected_ids": analysis.get("unexpected_ids") or [],
        "missing_invoices": missing_invoices,
        "cluster_differences": clusters_with_differences,
        "cluster_results": cluster_results,
        "reporting_groups": reporting_groups,
        "reporting_groups_sorted": sorted(
            reporting_groups, key=lambda group: _lot_sort_key(group.get("id_camille"))
        ),
        "overlapping_bdc_lots": analysis.get("overlapping_bdc_lots") or [],
        "conformity": conformity,
        "certification": certification,
        "balance": balance,
        "annex": annex,
        "video_detail": _build_video_detail_summary(cluster_results, missing_invoices),
        "review_count": len(review_clusters),
        "review_clusters": review_clusters,
        "cluster_results_sorted": sorted(cluster_results, key=_cluster_sort_key),
        "has_attention": bool(
            (analysis.get("overlapping_bdc_lots") or [])
            or (analysis.get("unexpected_ids") or [])
            or missing_invoices
            or annex.get("unknown_billed")
        ),
    }


def _create_analysis_record(
    reporting_name: str,
    bdc_name: str,
    reporting_reports: List[dict],
    bdc_reports: List[dict],
) -> StoredAnalysis:
    reporting_allowed = {str(report["filename"]) for report in reporting_reports}
    bdc_allowed = {str(report["filename"]) for report in bdc_reports}

    reporting_path = safe_join_filename(
        REPORTING_DIR,
        reporting_name,
        allowed_names=reporting_allowed,
    )
    bdc_path = safe_join_filename(
        BDC_DIR,
        bdc_name,
        allowed_names=bdc_allowed,
    )

    reporting_payload = _read_json_file(reporting_path, "rapport de production")
    bdc_payload = _read_json_file(bdc_path, "rapport BDC")

    if not isinstance(reporting_payload, dict):
        raise ValueError("Le rapport de production doit être un objet JSON.")
    if not isinstance(bdc_payload, list):
        raise ValueError("Le rapport BDC doit être une liste JSON.")

    month = _month_from_reporting_name(reporting_name)
    adjacent_months = (
        _load_adjacent_months(month, reporting_reports, bdc_reports) if month else None
    )
    analysis = compare_reports(
        reporting_payload,
        bdc_payload,
        adjacent_months=adjacent_months,
    )
    token = _store_analysis(
        reporting_name,
        bdc_name,
        reporting_payload,
        bdc_payload,
        analysis,
        month=month,
        adjacent_months=adjacent_months,
    )
    return _get_analysis(token)


def _analyze_selection(
    reporting_name: str,
    bdc_name: str,
    reporting_reports: List[dict],
    bdc_reports: List[dict],
) -> dict:
    return _context_from_record(
        _create_analysis_record(
            reporting_name,
            bdc_name,
            reporting_reports,
            bdc_reports,
        )
    )


def _context_from_record(record: StoredAnalysis) -> dict:
    return {
        "analysis": record.analysis,
        "view_model": _build_view_model(record.analysis),
        "meta": {
            "reporting_name": record.reporting_name,
            "bdc_name": record.bdc_name,
            "token": record.token,
            "month": record.month,
            "adjustments": {
                "excluded_files": sum(record.excluded_files.values()),
                "justified_clusters": len(record.justified_clusters),
            },
        },
        "selected_reporting": record.reporting_name,
        "selected_bdc": record.bdc_name,
        "current_month": record.month,
    }


def _record_file_index(record: StoredAnalysis) -> Dict[str, Set[str]]:
    """Map filename -> set of lot IDs where it occurs in the source report."""
    index: Dict[str, Set[str]] = {}
    for group in record.reporting_payload.get("report", []) or []:
        lot = str(group.get("id_camille") or "")
        for cat_data in (group.get("categories") or {}).values():
            if not isinstance(cat_data, dict):
                continue
            names = [str(name) for name in (cat_data.get("files") or [])]
            details = cat_data.get("files_detail") or cat_data.get("file_details") or []
            names.extend(str(detail.get("name") or "") for detail in details)
            for name in names:
                if name:
                    index.setdefault(name, set()).add(lot)
    return index


def _count_reporting_filename(
    record: StoredAnalysis, filename: str, lot: Optional[str] = None
) -> int:
    total = 0
    for group in record.reporting_payload.get("report", []) or []:
        group_lot = str(group.get("id_camille") or "")
        if lot is not None and group_lot != lot:
            continue
        for cat_data in (group.get("categories") or {}).values():
            if not isinstance(cat_data, dict):
                continue
            details = cat_data.get("files_detail") or cat_data.get("file_details") or []
            if details:
                total += sum(1 for detail in details if str(detail.get("name") or "") == filename)
                continue
            files = [str(name) for name in (cat_data.get("files") or [])]
            total += sum(1 for name in files if name == filename)
    return total


def _take_excluded(remaining: Dict[Tuple[str, str], int], lot: str, filename: str) -> bool:
    for key in ((lot, filename), ("*", filename)):
        if remaining.get(key, 0) > 0:
            remaining[key] -= 1
            return True
    return False


def _mp4_count(names: List[str]) -> int:
    return sum(1 for name in names if str(name).lower().endswith(".mp4"))


def _effective_reporting_payload(record: StoredAnalysis) -> dict:
    payload = copy.deepcopy(record.reporting_payload)
    if not record.excluded_files:
        return payload

    remaining_files = dict(record.excluded_files)
    remaining_details = dict(record.excluded_files)
    total_mp4 = 0
    group_totals: Dict[str, int] = {}

    for group in payload.get("report", []) or []:
        lot = str(group.get("id_camille") or "")
        group_total = 0
        categories = group.get("categories") or {}
        for cat_data in categories.values():
            if not isinstance(cat_data, dict):
                continue
            files = [str(name) for name in (cat_data.get("files") or [])]
            details = list(cat_data.get("files_detail") or cat_data.get("file_details") or [])

            new_files = []
            for name in files:
                if _take_excluded(remaining_files, lot, name):
                    continue
                new_files.append(name)

            new_details = []
            for detail in details:
                name = str(detail.get("name") or "")
                if name and _take_excluded(remaining_details, lot, name):
                    continue
                new_details.append(detail)

            cat_data["files"] = new_files
            if "files_detail" in cat_data:
                cat_data["files_detail"] = new_details
            if "file_details" in cat_data:
                cat_data["file_details"] = new_details

            if details:
                cat_data["count"] = len(new_details)
            else:
                cat_data["count"] = len(new_files)

            counted_names = [str(detail.get("name") or "") for detail in new_details]
            if not details:
                counted_names = new_files
            category_mp4 = _mp4_count(counted_names)
            group_total += category_mp4

        person = str(group.get("person") or "")
        group_totals[f"{lot} {person}"] = group_total
        total_mp4 += group_total

    payload["total_mp4"] = total_mp4
    payload["group_totals"] = group_totals
    return payload


def _recompute_record(record: StoredAnalysis) -> None:
    payload = _effective_reporting_payload(record)
    analysis = compare_reports(
        payload,
        record.bdc_payload,
        adjacent_months=record.adjacent_months,
        justified_clusters=set(record.justified_clusters),
    )
    active_justifications = {
        str(cluster.get("cluster_key"))
        for cluster in analysis.get("cluster_results", [])
        if cluster.get("justified")
    }
    with _store_lock:
        record.analysis = analysis
        record.justified_clusters.intersection_update(active_justifications)


def list_available_reports() -> Tuple[List[dict], List[dict]]:
    reporting_reports = []
    if REPORTING_DIR.exists():
        for path in REPORTING_DIR.glob("rapport-*.json"):
            name = path.name
            match = re.search(r"rapport-(\d{4}-\d{2})\.json", name)
            if match:
                month = match.group(1)
                reporting_reports.append(
                    {
                        "filename": name,
                        "month": month,
                        "display": f"Rapport production - {month}",
                    }
                )

    bdc_reports = []
    if BDC_DIR.exists():
        for path in BDC_DIR.glob("convert_bdc_*.json"):
            name = path.name
            match = re.search(r"convert_bdc_(\d{2}-\d{4})\.json", name)
            if match:
                month = match.group(1)
                bdc_reports.append(
                    {
                        "filename": name,
                        "month": month,
                        "display": f"Rapport BDC - {month}",
                    }
                )

    reporting_reports.sort(key=lambda item: item["month"], reverse=True)
    bdc_reports.sort(key=lambda item: item["month"].split("-")[::-1], reverse=True)
    return reporting_reports, bdc_reports


@app.route("/", methods=["GET", "POST"])
def index():
    reporting_reports, bdc_reports = list_available_reports()
    context: Dict[str, Any] = {
        "error": None,
        "notice": None,
        "analysis": None,
        "view_model": None,
        "meta": None,
        "reporting_reports": reporting_reports,
        "bdc_reports": bdc_reports,
        "selected_reporting": None,
        "selected_bdc": None,
        "current_month": request.args.get("month"),
    }
    if request.args.get("notice") == "expired":
        context["notice"] = (
            "Session expirée : l'analyse a été rechargée depuis les fichiers sources "
            "et les ajustements précédents ont été réinitialisés."
        )
    elif request.args.get("notice") == "handoff":
        context["notice"] = (
            "Analyse transmise à la facturation. Ouvrez l’acte 4 pour préparer la base "
            "de facturation du mois."
        )
    elif request.args.get("notice") == "handoff_failed":
        context["error"] = "La transmission à la facturation a échoué" + (
            f" (réf. {request.args.get('error_id')})." if request.args.get("error_id") else "."
        )

    token = request.args.get("token")
    requested_month = request.args.get("month")
    if token:
        record = _resolve_record(token, month=requested_month)
        if record is not None:
            context.update(_context_from_record(record))
            if record.token != token:
                context["notice"] = (
                    "Session expirée ou fichiers sources modifiés : l'analyse a été "
                    "rechargée et les ajustements précédents ont été réinitialisés."
                )
            return render_template("index.html", **context)
        context["error"] = "Analyse introuvable ou expirée."

    if request.method == "POST":
        reporting_name = request.form.get("reporting_filename") or ""
        bdc_name = request.form.get("bdc_filename") or ""
        context["selected_reporting"] = reporting_name or None
        context["selected_bdc"] = bdc_name or None
        context["current_month"] = _month_from_reporting_name(reporting_name)

        try:
            if not reporting_name or not bdc_name:
                raise ValueError(
                    "Veuillez sélectionner un rapport de production et un rapport BDC."
                )
            context.update(
                _analyze_selection(
                    reporting_name,
                    bdc_name,
                    reporting_reports,
                    bdc_reports,
                )
            )
        except PathValidationError as exc:
            context["error"] = str(exc)
        except ValueError as exc:
            context["error"] = str(exc)
        except Exception as exc:
            error_id = log_exception(exc, message="Reconciliation analysis failed")
            context["error"] = f"Erreur inattendue (réf. {error_id})."

        return render_template("index.html", **context)

    month = request.args.get("month")
    if month:
        context["error"] = None
        context["current_month"] = month
        if not MONTH_PATTERN.match(month):
            context["error"] = "Format de mois invalide. Utilisez YYYY-MM."
        else:
            reporting_name = _find_report_for_month(reporting_reports, month)
            bdc_name = _find_bdc_for_month(bdc_reports, month)
            context["selected_reporting"] = reporting_name
            context["selected_bdc"] = bdc_name

            if not reporting_name:
                context["error"] = (
                    f"Rapport de production introuvable pour {month} "
                    f"(attendu : rapport-{month}.json)."
                )
            elif not bdc_name:
                context["error"] = (
                    f"Rapport BDC introuvable pour {month} "
                    f"(attendu : convert_bdc_{month[5:7]}-{month[:4]}.json)."
                )
            else:
                try:
                    context.update(
                        _analyze_selection(
                            reporting_name,
                            bdc_name,
                            reporting_reports,
                            bdc_reports,
                        )
                    )
                except PathValidationError as exc:
                    context["error"] = str(exc)
                except ValueError as exc:
                    context["error"] = str(exc)
                except Exception as exc:
                    error_id = log_exception(exc, message="Reconciliation analysis failed")
                    context["error"] = f"Erreur inattendue (réf. {error_id})."

    return render_template("index.html", **context)


def _redirect_to_record(record: StoredAnalysis, *, recovered: bool = False):
    params: Dict[str, Any] = {"token": record.token}
    if record.month:
        params["month"] = record.month
    if recovered:
        params["notice"] = "expired"
    return redirect(url_for("index", **params))


@app.post("/inspector/exclude")
def inspector_exclude():
    token = request.form.get("token") or ""
    filename = request.form.get("filename") or ""
    lot = request.form.get("id_camille") or ""
    cluster_key = request.form.get("cluster_key") or ""
    month = request.form.get("month") or request.args.get("month") or ""
    reporting_name = request.form.get("reporting_name") or ""
    bdc_name = request.form.get("bdc_name") or ""

    record = _resolve_record(
        token,
        month=month,
        reporting_name=reporting_name,
        bdc_name=bdc_name,
    )
    if record is None:
        if month:
            return redirect(url_for("index", month=month, notice="expired"))
        return "Analyse introuvable ou expirée", 404

    file_index = _record_file_index(record)
    if not filename or filename not in file_index:
        return "Fichier non autorisé", 400
    if lot and lot not in file_index[filename]:
        return "Lot non autorisé pour ce fichier", 400

    if cluster_key:
        cluster = next(
            (
                item
                for item in record.analysis.get("cluster_results", [])
                if str(item.get("cluster_key")) == cluster_key
            ),
            None,
        )
        if cluster is None or (lot and lot not in (cluster.get("cluster_ids") or [])):
            return "Regroupement non autorisé pour ce fichier", 400

    scope = request.form.get("scope") or ""
    if not scope and request.form.get("remove_all") == "1":
        scope = "month"
    if scope not in {"occurrence", "lot", "month"}:
        scope = "occurrence" if lot else "month"

    if scope == "month":
        key = ("*", filename)
        count = _count_reporting_filename(record, filename)
    elif scope == "lot":
        if not lot:
            return "Lot requis pour une exclusion ciblée", 400
        key = (lot, filename)
        count = _count_reporting_filename(record, filename, lot)
    else:
        if not lot:
            lot = sorted(file_index[filename], key=_lot_sort_key)[0]
        key = (lot, filename)
        count = 1

    if count <= 0:
        return "Fichier non autorisé", 400

    with _store_lock:
        record.excluded_files[key] = record.excluded_files.get(key, 0) + count
    _recompute_record(record)
    return _redirect_to_record(record, recovered=bool(token and record.token != token))


@app.post("/inspector/justify")
def inspector_justify():
    token = request.form.get("token") or ""
    cluster_key = request.form.get("cluster_key") or ""
    month = request.form.get("month") or request.args.get("month") or ""
    reporting_name = request.form.get("reporting_name") or ""
    bdc_name = request.form.get("bdc_name") or ""
    is_undo = bool(request.form.get("undo"))

    record = _resolve_record(
        token,
        month=month,
        reporting_name=reporting_name,
        bdc_name=bdc_name,
    )
    if record is None:
        if month:
            return redirect(url_for("index", month=month, notice="expired"))
        return "Analyse introuvable ou expirée", 404

    cluster = next(
        (
            item
            for item in record.analysis.get("cluster_results", [])
            if str(item.get("cluster_key")) == cluster_key
        ),
        None,
    )
    if not cluster_key or cluster is None:
        return "Écart non autorisé", 400
    if not is_undo and not cluster.get("justification_eligible"):
        message = cluster.get("justification_hint") or (
            "Justification réservée aux écarts causés par un doublon ou un jingle"
        )
        return message, 400

    with _store_lock:
        if is_undo:
            record.justified_clusters.discard(cluster_key)
        else:
            record.justified_clusters.add(cluster_key)
    _recompute_record(record)
    return _redirect_to_record(record, recovered=bool(token and record.token != token))


@app.post("/inspector/reset")
def inspector_reset():
    token = request.form.get("token") or ""
    month = request.form.get("month") or request.args.get("month") or ""
    reporting_name = request.form.get("reporting_name") or ""
    bdc_name = request.form.get("bdc_name") or ""

    record = _resolve_record(
        token,
        month=month,
        reporting_name=reporting_name,
        bdc_name=bdc_name,
    )
    if record is None:
        if month:
            return redirect(url_for("index", month=month, notice="expired"))
        return "Analyse introuvable ou expirée", 404

    with _store_lock:
        record.excluded_files.clear()
        record.justified_clusters.clear()
    _recompute_record(record)
    return _redirect_to_record(record, recovered=bool(token and record.token != token))


@app.post("/handoff/billing")
def handoff_billing():
    """Persist the effective reconciliation analysis for the billing service."""
    token = request.form.get("token") or ""
    month = request.form.get("month") or ""
    reporting_name = request.form.get("reporting_name") or ""
    bdc_name = request.form.get("bdc_name") or ""

    record = _resolve_record(
        token,
        month=month,
        reporting_name=reporting_name,
        bdc_name=bdc_name,
    )
    if record is None:
        return redirect(url_for("index", month=month, notice="handoff_failed"))

    if token and record.token != token:
        # The token expired or the source files changed: never silently hand off
        # an analysis rebuilt without the session adjustments.
        return redirect(
            url_for(
                "index",
                token=record.token,
                month=record.month or month,
                notice="expired",
            )
        )

    target_month = record.month or _month_from_reporting_name(record.reporting_name)
    if not target_month or not MONTH_PATTERN.match(target_month):
        return redirect(
            url_for(
                "index",
                token=record.token,
                month=month,
                notice="handoff_failed",
            )
        )

    payload = {
        "month": target_month,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "reporting_name": record.reporting_name,
        "bdc_name": record.bdc_name,
        "source_fingerprint": record.source_fingerprint,
        "sources": {
            "reporting": {
                "name": record.reporting_name,
                "sha256": _sha256_file(REPORTING_DIR / record.reporting_name),
            },
            "bdc": {
                "name": record.bdc_name,
                "sha256": _sha256_file(BDC_DIR / record.bdc_name),
            },
        },
        "adjustments": {
            "excluded_files": [
                {"lot": lot, "filename": filename, "count": count}
                for (lot, filename), count in sorted(record.excluded_files.items())
            ],
            "justified_clusters": sorted(record.justified_clusters),
        },
        "certification": record.analysis.get("certification") or {},
        "analysis": record.analysis,
    }

    try:
        HANDOFF_DIR.mkdir(parents=True, exist_ok=True)
        with resource_lock(f"reconciliation-handoff:{target_month}"):
            atomic_write_text(
                HANDOFF_DIR / f"{target_month}.json",
                json.dumps(payload, ensure_ascii=False, indent=2),
            )
    except Exception as exc:
        error_id = log_exception(exc, message="Reconciliation billing handoff failed")
        return redirect(
            url_for(
                "index",
                token=record.token,
                month=target_month,
                notice="handoff_failed",
                error_id=error_id,
            )
        )

    return redirect(
        url_for(
            "index",
            token=record.token,
            month=target_month,
            notice="handoff",
        )
    )


@app.route("/export/json", methods=["GET"])
def export_json():
    token = request.args.get("token") or ""
    month = request.args.get("month") or ""
    reporting_name = request.args.get("reporting_name") or ""
    bdc_name = request.args.get("bdc_name") or ""
    if not token and not month and not (reporting_name and bdc_name):
        return "Token manquant", 400

    record = _resolve_record(
        token,
        month=month,
        reporting_name=reporting_name,
        bdc_name=bdc_name,
    )
    if record is None:
        return "Analyse introuvable ou expirée", 404

    export_payload = dict(record.analysis)
    cluster_results = export_payload.get("cluster_results") or []
    export_payload["cluster_differences"] = [
        cluster for cluster in cluster_results if _cluster_has_differences(cluster)
    ]
    if token and record.token != token:
        export_payload["session_notice"] = (
            "Session expirée ou fichiers sources modifiés : analyse rechargée depuis le disque."
        )
    payload = serialize_comparison(export_payload)
    resp = make_response(payload)
    resp.headers["Content-Type"] = "application/json; charset=utf-8"
    resp.headers["Content-Disposition"] = (
        f"attachment; filename=rapprochement-{record.reporting_name}-{record.bdc_name}.json"
    )
    return resp


def _export_filename(record: StoredAnalysis, extension: str) -> str:
    certification = record.analysis.get("certification") or {}
    prefix = "certificat-rapprochement" if certification.get("is_certified") else "rapprochement"
    if record.month:
        return f"{prefix}-{record.month}.{extension}"
    stem = re.sub(
        r"[^A-Za-z0-9._-]+",
        "-",
        f"{record.reporting_name}-{record.bdc_name}",
    )
    return f"{prefix}-{stem}.{extension}"


def _render_export_document(
    record: StoredAnalysis,
    *,
    pdf_mode: bool = False,
    recovered: bool = False,
    pdf_page_height_px: Optional[float] = None,
) -> str:
    return render_template(
        "export.html",
        analysis=record.analysis,
        view_model=_build_view_model(record.analysis, strip_justified_duplicates=True),
        meta={
            "reporting_name": record.reporting_name,
            "bdc_name": record.bdc_name,
            "token": record.token,
            "month": record.month,
            "session_notice": (
                "Session expirée ou fichiers sources modifiés : analyse rechargée "
                "depuis le disque, ajustements de session réinitialisés."
                if recovered
                else ""
            ),
        },
        pdf_mode=pdf_mode,
        pdf_page_height_px=pdf_page_height_px,
    )


def _pdf_content_bottom_px(document: Any) -> float:
    """Hauteur du contenu rendu (px CSS), mesurée sur la page sonde WeasyPrint."""
    page = document.pages[0]
    page_box = getattr(page, "_page_box", None)
    if page_box is None:
        raise RuntimeError("WeasyPrint page box unavailable")

    root = next(
        (child for child in page_box.children if getattr(child, "element_tag", None) == "html"),
        None,
    )
    if root is None:
        root = page_box.children[0] if page_box.children else None
    if root is None:
        raise RuntimeError("WeasyPrint page has no content box")
    return float(root.position_y + root.margin_height())


def _pdf_zoom_for_page_height(page_height_pt: float) -> float:
    """Ramène une page trop haute sous la limite de 14 400 pt (200 pouces)."""
    if page_height_pt <= 0:
        return 1.0
    return min(1.0, PDF_MAX_PAGE_HEIGHT_PT / page_height_pt)


def _render_single_page_pdf(
    record: StoredAnalysis,
    *,
    weasyprint_html: Any,
    recovered: bool = False,
) -> bytes:
    """Rend l'export sur une page unique, dimensionnée pour contenir tout le contenu."""
    probe_height = PDF_PROBE_PAGE_HEIGHT_PX
    probe_document = None
    for _ in range(PDF_PROBE_MAX_ATTEMPTS):
        probe_html = _render_export_document(
            record,
            pdf_mode=True,
            recovered=recovered,
            pdf_page_height_px=probe_height,
        )
        probe_document = weasyprint_html(string=probe_html).render()
        if len(probe_document.pages) == 1:
            break
        probe_height *= 2
    else:
        raise RuntimeError("Unable to fit the export on a single page")

    content_bottom_px = _pdf_content_bottom_px(probe_document)
    del probe_document

    page_height_px = max(
        PDF_MIN_PAGE_HEIGHT_PX,
        content_bottom_px + PDF_PAGE_BOTTOM_MARGIN_PX + PDF_PAGE_HEIGHT_SAFETY_PX,
    )
    document = None
    for _ in range(2):
        html = _render_export_document(
            record,
            pdf_mode=True,
            recovered=recovered,
            pdf_page_height_px=page_height_px,
        )
        document = weasyprint_html(string=html).render()
        if len(document.pages) == 1:
            break
        # Marge de sécurité si l'arrondi de mise en page a débordé.
        page_height_px = page_height_px * 1.01 + 24
    else:
        raise RuntimeError("Unable to fit the export on a single page")

    zoom = _pdf_zoom_for_page_height(document.pages[0].height * 0.75)
    pdf_bytes = document.write_pdf(zoom=zoom)
    if not pdf_bytes:
        raise RuntimeError("WeasyPrint returned an empty PDF")
    return pdf_bytes


@app.route("/export/html", methods=["GET"])
def export_html():
    token = request.args.get("token") or ""
    month = request.args.get("month") or ""
    reporting_name = request.args.get("reporting_name") or ""
    bdc_name = request.args.get("bdc_name") or ""
    if not token and not month and not (reporting_name and bdc_name):
        return "Token manquant", 400

    record = _resolve_record(
        token,
        month=month,
        reporting_name=reporting_name,
        bdc_name=bdc_name,
    )
    if record is None:
        return "Analyse introuvable ou expirée", 404

    html = _render_export_document(record, recovered=bool(token and record.token != token))
    resp = make_response(html)
    resp.headers["Content-Type"] = "text/html; charset=utf-8"
    resp.headers["Content-Disposition"] = f"attachment; filename={_export_filename(record, 'html')}"
    resp.headers["Content-Security-Policy"] = EXPORT_CSP
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/export/pdf", methods=["GET"])
def export_pdf():
    token = request.args.get("token") or ""
    month = request.args.get("month") or ""
    reporting_name = request.args.get("reporting_name") or ""
    bdc_name = request.args.get("bdc_name") or ""
    if not token and not month and not (reporting_name and bdc_name):
        return "Token manquant", 400

    record = _resolve_record(
        token,
        month=month,
        reporting_name=reporting_name,
        bdc_name=bdc_name,
    )
    if record is None:
        return "Analyse introuvable ou expirée", 404

    try:
        from weasyprint import HTML as WeasyPrintHTML
    except Exception as exc:
        error_id = log_exception(exc, message="Reconciliation PDF export unavailable")
        return f"Export PDF indisponible (réf. {error_id}).", 503

    try:
        pdf_bytes = _render_single_page_pdf(
            record,
            weasyprint_html=WeasyPrintHTML,
            recovered=bool(token and record.token != token),
        )
    except Exception as exc:
        error_id = log_exception(exc, message="Reconciliation PDF export failed")
        return f"Erreur lors de l'export PDF (réf. {error_id}).", 500

    resp = make_response(pdf_bytes)
    resp.headers["Content-Type"] = "application/pdf"
    resp.headers["Content-Disposition"] = f"attachment; filename={_export_filename(record, 'pdf')}"
    resp.headers["Cache-Control"] = "no-store"
    return resp


if __name__ == "__main__":
    settings = get_run_settings(5030)
    app.run(host=settings.host, port=settings.port, debug=settings.debug)
