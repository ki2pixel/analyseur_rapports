import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from flask import Flask, render_template, request, make_response, jsonify
from jinja2 import ChoiceLoader, FileSystemLoader

from .analyser import (
    DEFAULT_THRESHOLDS,
    analyze_month,
    analyze_batch,
    CategoryThresholds,
    GroupReport,
    CategoryResult,
    collect_video_entries_with_warnings,
    parse_folder_name,
    list_month_folders,
    disambiguate_folders,
)
from libs.schemas import (
    VideoCategory,
    MonthlyBatch,
    VideoEntry,
    SourceFile,
    OrderArticle,
    PurchaseOrder,
)
from libs.web import (
    ResourceBusyError,
    atomic_write_text,
    client_error_payload,
    get_run_settings,
    init_app_security,
    log_exception,
    resource_lock,
)

ROOT_DIR = Path(__file__).resolve().parents[2]
TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
BATCHES_DIR = ROOT_DIR / "data" / "batches"

SHARED_STATIC_DIR = ROOT_DIR / "apps" / "static"
SHARED_TEMPLATES_DIR = ROOT_DIR / "apps" / "templates"

logger = logging.getLogger("analyseur.reporting")

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
init_app_security(app, default_port=5010)

ARCHIVES_DIR = ROOT_DIR / "archives"

# Default thresholds shared with the CLI.
THRESHOLDS = DEFAULT_THRESHOLDS


def compute_totals(report):
    """Compute total .mp4 counts overall and per group for a report of GroupReport objects."""
    if not report:
        return 0, {}
    total_mp4 = 0
    group_totals = {}
    for group in report:
        group_id = f"{group.id_camille} {group.person}"
        group_total = 0
        for cat_key in ("short", "standard", "medium", "long"):
            cat = group.categories.get(cat_key)
            files = cat.files if cat and getattr(cat, "files", None) else []
            mp4_count = sum(1 for f in files if isinstance(f, str) and f.lower().endswith(".mp4"))
            group_total += mp4_count
        group_totals[group_id] = group_total
        total_mp4 += group_total
    return total_mp4, group_totals


def serialize_report(report):
    """Convert GroupReport objects into plain dicts for JSON export."""
    serialized = []
    for group in report or []:
        categories = {}
        for key, cat in (group.categories or {}).items():
            if not cat:
                continue
            files = list(cat.files)
            file_details = list(getattr(cat, "file_details", []) or [])
            if not file_details and files:
                file_details = [
                    {"name": name, "duration_sec": None, "category": key} for name in files
                ]
            categories[key] = {
                "label": cat.label,
                "count": cat.count,
                "files": files,
                "files_detail": file_details,
            }
        serialized.append(
            {
                "id_camille": group.id_camille,
                "person": group.person,
                "categories": categories,
            }
        )
    return serialized


def month_options(base_dir: Path, batches_dir: Optional[Path] = None):
    months = set()
    if base_dir.exists():
        for child in base_dir.iterdir():
            if not child.is_dir():
                continue
            parsed = parse_folder_name(child.name)
            if parsed:
                months.add(parsed[2].strftime("%Y-%m"))
    if batches_dir and batches_dir.exists():
        for path in batches_dir.glob("*.json"):
            match = re.fullmatch(r"(\d{4}-\d{2})\.json", path.name)
            if match:
                months.add(match.group(1))
    return sorted(months)


def _parse_datetime(value: Optional[str]) -> datetime:
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            pass
    return datetime.now()


def _deserialize_source_file(data: dict) -> SourceFile:
    return SourceFile(
        name=data.get("name", "unknown"),
        path=data.get("path") or "",
        file_type=data.get("file_type", "json"),
        processed_at=_parse_datetime(data.get("processed_at")),
        status=data.get("status", "processed"),
    )


def _deserialize_order_article(data: dict) -> OrderArticle:
    inferred_raw = data.get("inferred_category")
    if isinstance(inferred_raw, VideoCategory):
        inferred = inferred_raw
    elif inferred_raw:
        try:
            inferred = VideoCategory(inferred_raw)
        except ValueError:
            inferred = VideoCategory.UNKNOWN
    else:
        inferred = VideoCategory.UNKNOWN

    return OrderArticle(
        description=data.get("description", ""),
        quantite=data.get("quantite"),
        unite=data.get("unite"),
        taux=data.get("taux"),
        montant=data.get("montant"),
        inferred_category=inferred,
        linked_video_ids=data.get("linked_video_ids", []),
    )


def _deserialize_purchase_order(data: dict) -> PurchaseOrder:
    return PurchaseOrder(
        numero_bdc=data.get("numero_bdc", "UNKNOWN"),
        date_commande=data.get("date_commande"),
        date_livraison=data.get("date_livraison"),
        reference=data.get("reference"),
        references=data.get("references", []),
        articles=[_deserialize_order_article(article) for article in data.get("articles", [])],
    )


def _deserialize_video_entry(data: dict) -> VideoEntry:
    return VideoEntry(
        video_id=data.get("video_id") or data.get("video_name") or "unknown",
        id_camille=data.get("id_camille", "0"),
        person=data.get("person", "Camille"),
        month=data.get("month", "1970-01"),
        video_name=data.get("video_name", "unknown"),
        video_path=data.get("video_path", ""),
        duration_sec=data.get("duration_sec"),
        duration_source=data.get("duration_source", "unknown"),
        archive_folder=data.get("archive_folder", ""),
        source_bdc=data.get("source_bdc"),
        source_article_index=data.get("source_article_index"),
    )


def load_monthly_batch_from_file(month: str) -> Optional[MonthlyBatch]:
    path = BATCHES_DIR / f"{month}.json"
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return MonthlyBatch(
            batch_id=payload.get("batch_id", month),
            generated_at=_parse_datetime(payload.get("generated_at")),
            source_files=[_deserialize_source_file(sf) for sf in payload.get("source_files", [])],
            orders=[_deserialize_purchase_order(po) for po in payload.get("orders", [])],
            video_entries=[_deserialize_video_entry(ve) for ve in payload.get("video_entries", [])],
            warnings=list(payload.get("warnings") or []),
        )
    except Exception:
        logger.warning("Impossible de lire le batch %s", path, exc_info=True)
        return None


def get_batch_for_month(selected: str) -> MonthlyBatch:
    try:
        year_str, month_str = selected.split("-")
        year, month = int(year_str), int(month_str)
    except ValueError as exc:
        raise ValueError("Format de mois invalide") from exc

    batch = create_batch_from_archives(year, month)
    stored = load_monthly_batch_from_file(selected)
    if stored is not None:
        # The BDC batch is an invoice-side index: use its orders/sources/warnings
        # as metadata, but keep the archive scan as the production source of truth.
        batch.orders = stored.orders
        if stored.source_files:
            batch.source_files = stored.source_files
        batch.warnings = list(dict.fromkeys([*batch.warnings, *stored.warnings]))
    return batch


def convert_video_reports_to_legacy(video_reports):
    """
    Convertit une liste de VideoGroupReport (nouveau schéma) en liste de GroupReport (ancien schéma)
    pour maintenir la compatibilité avec les templates et fonctions existants.
    """
    legacy_reports = []

    for video_report in video_reports:
        # Convertir les catégories VideoCategory -> str
        legacy_categories = {}
        for cat_enum, cat_data in video_report.categories.items():
            legacy_categories[cat_enum.value] = CategoryResult(
                label=cat_data.label,
                count=cat_data.count,
                files=list(cat_data.files),
                file_details=list(cat_data.file_details),
            )

        # Créer le GroupReport legacy
        legacy_report = GroupReport(
            id_camille=video_report.id_camille,
            person=video_report.person,
            categories=legacy_categories,
        )

        legacy_reports.append(legacy_report)

    return legacy_reports


def create_batch_from_archives(year: int, month: int) -> MonthlyBatch:
    """
    Crée un MonthlyBatch à partir des données d'archives existantes.
    Cette fonction permet de tester l'intégration des nouveaux schémas
    avec les données existantes.
    """
    month_folders = list_month_folders(ARCHIVES_DIR, year, month)
    video_entries = []
    warnings = []

    disambiguated = disambiguate_folders(month_folders)
    for folder, id_camille, person, dt in disambiguated:
        videos, folder_warnings = collect_video_entries_with_warnings(
            folder, id_camille, person, dt
        )
        warnings.extend(folder_warnings)
        if not videos:
            continue

        # Convertir VideoEntry (ancien) en VideoEntry (nouveau schéma)
        for old_video in videos:
            new_video = VideoEntry(
                video_id=f"{id_camille}_{person}_{old_video.video_name}_{dt.isoformat()}",
                id_camille=old_video.id_camille,
                person=old_video.person,
                month=f"{year:04d}-{month:02d}",
                video_name=old_video.video_name,
                video_path=str(old_video.video_path),
                duration_sec=old_video.duration_sec,
                duration_source="archives_scan",
                archive_folder=str(folder),
            )
            video_entries.append(new_video)

    batch = MonthlyBatch(
        batch_id=f"{year:04d}-{month:02d}",
        generated_at=datetime.now(),
        video_entries=video_entries,
        warnings=warnings,
    )

    return batch


@app.route("/", methods=["GET", "POST"])
def index():
    months = month_options(ARCHIVES_DIR, BATCHES_DIR)
    selected = request.form.get("month") if request.method == "POST" else request.args.get("month")
    report = None
    error = None
    total_mp4 = None
    batch_meta = None
    group_totals = {}

    if request.method == "POST":
        if not selected:
            error = "Veuillez choisir un mois."
        elif selected not in months:
            error = "Aucune archive disponible pour ce mois."
        else:
            try:
                batch = get_batch_for_month(selected)
                video_reports = analyze_batch(batch, THRESHOLDS)
                # Convertir vers format legacy pour compatibilité templates
                report = convert_video_reports_to_legacy(video_reports)
                total_mp4, group_totals = compute_totals(report)
                batch_meta = {
                    "batch_id": batch.batch_id,
                    "sources": [sf.name for sf in batch.source_files],
                    "orders": len(batch.orders),
                    "video_entries": len(batch.video_entries),
                    "generated_at": batch.generated_at,
                    "warnings": list(batch.warnings),
                }
            except Exception as e:
                error_id = log_exception(e, message="Reporting analysis failed")
                error = f"Erreur lors de l'analyse (réf. {error_id})."
    elif selected and selected not in months:
        error = "Aucune archive disponible pour ce mois."
        selected = None

    category_totals = {key: 0 for key in ("short", "standard", "medium", "long")}
    for group in report or []:
        for key in category_totals:
            category_totals[key] += group.categories[key].count
    category_total = sum(category_totals.values())
    category_shares = {
        key: (value / category_total * 100 if category_total else 0)
        for key, value in category_totals.items()
    }

    return render_template(
        "index.html",
        months=months,
        selected=selected,
        report=report,
        total_mp4=total_mp4,
        group_totals=group_totals,
        error=error,
        batch_meta=batch_meta,
        category_totals=category_totals,
        category_shares=category_shares,
        category_total=category_total,
        current_month=selected or request.args.get("month"),
    )


@app.template_filter("fmt_count")
def fmt_count(v):
    try:
        return f"{int(v)}"
    except Exception:
        return str(v)


@app.route("/export", methods=["GET"])
def export_html():
    """Export the selected month report as downloadable HTML using the report template."""
    selected = request.args.get("month")
    if not selected:
        return "Paramètre 'month' manquant (YYYY-MM)", 400

    try:
        batch = get_batch_for_month(selected)
        video_reports = analyze_batch(batch, THRESHOLDS)
        report = convert_video_reports_to_legacy(video_reports)
        total_mp4, group_totals = compute_totals(report)
        html = render_template(
            "report.html",
            report=report,
            selected=selected,
            total_mp4=total_mp4,
            group_totals=group_totals,
        )
        resp = make_response(html)
        filename = f"rapport-{selected}.html"
        resp.headers["Content-Type"] = "text/html; charset=utf-8"
        resp.headers["Content-Disposition"] = f"attachment; filename={filename}"
        return resp
    except Exception as e:
        error_id = log_exception(e, message="Reporting HTML export failed")
        return f"Erreur lors de l'export (réf. {error_id}).", 500


@app.route("/export/json", methods=["GET"])
def export_json():
    """Export the selected month report as downloadable JSON."""
    selected = request.args.get("month")
    if not selected:
        return "Paramètre 'month' manquant (YYYY-MM)", 400
    try:
        year_str, month_str = selected.split("-")
        year, month = int(year_str), int(month_str)
    except Exception:
        return "Format de 'month' invalide. Utilisez YYYY-MM.", 400

    try:
        batch = get_batch_for_month(selected)
        video_reports = analyze_batch(batch, THRESHOLDS)
        report = convert_video_reports_to_legacy(video_reports)
        total_mp4, group_totals = compute_totals(report)
        payload = {
            "month": selected,
            "total_mp4": total_mp4,
            "group_totals": group_totals,
            "report": serialize_report(report),
            "warnings": list(batch.warnings),
        }
        resp = make_response(json.dumps(payload, ensure_ascii=False, indent=2))
        filename = f"rapport-{selected}.json"
        resp.headers["Content-Type"] = "application/json; charset=utf-8"
        resp.headers["Content-Disposition"] = f"attachment; filename={filename}"
        return resp
    except Exception as e:
        error_id = log_exception(e, message="Reporting JSON export failed")
        return f"Erreur lors de l'export JSON (réf. {error_id}).", 500


@app.route("/export/batch/json", methods=["GET"])
def export_json_batch():
    """Export the selected month report as downloadable JSON using the new analyze_batch approach."""
    selected = request.args.get("month")
    if not selected:
        return "Paramètre 'month' manquant (YYYY-MM)", 400
    try:
        year_str, month_str = selected.split("-")
        year, month = int(year_str), int(month_str)
    except Exception:
        return "Format de 'month' invalide. Utilisez YYYY-MM.", 400

    try:
        batch = get_batch_for_month(selected)
        video_reports = analyze_batch(batch, THRESHOLDS)
        report = convert_video_reports_to_legacy(video_reports)
        total_mp4, group_totals = compute_totals(report)
        payload = {
            "month": selected,
            "total_mp4": total_mp4,
            "group_totals": group_totals,
            "report": serialize_report(report),
            "warnings": list(batch.warnings),
            "batch_info": {
                "batch_id": batch.batch_id,
                "video_entries_count": len(batch.video_entries),
                "reports_count": len(video_reports),
            },
        }
        resp = make_response(json.dumps(payload, ensure_ascii=False, indent=2))
        filename = f"rapport-batch-{selected}.json"
        resp.headers["Content-Type"] = "application/json; charset=utf-8"
        resp.headers["Content-Disposition"] = f"attachment; filename={filename}"
        return resp
    except Exception as e:
        error_id = log_exception(e, message="Reporting batch JSON export failed")
        return f"Erreur lors de l'export JSON batch (réf. {error_id}).", 500


@app.route("/generate-json", methods=["POST"])
def generate_json_endpoint():
    """Génère et sauvegarde le rapport JSON côté serveur."""
    selected = request.args.get("month")
    if not selected:
        return jsonify({"status": "error", "message": "Paramètre 'month' manquant (YYYY-MM)"}), 400
    try:
        year_str, month_str = selected.split("-")
        year, month = int(year_str), int(month_str)
    except Exception:
        return (
            jsonify(
                {"status": "error", "message": "Format de 'month' invalide. Utilisez YYYY-MM."}
            ),
            400,
        )

    try:
        with resource_lock(f"reporting-json-{selected}", blocking=False):
            batch = get_batch_for_month(selected)
            video_reports = analyze_batch(batch, THRESHOLDS)
            report = convert_video_reports_to_legacy(video_reports)
            total_mp4, group_totals = compute_totals(report)
            payload = {
                "month": selected,
                "total_mp4": total_mp4,
                "group_totals": group_totals,
                "report": serialize_report(report),
                "warnings": list(batch.warnings),
            }

            filename = f"rapport-{selected}.json"
            save_path = Path(__file__).resolve().parent / filename
            atomic_write_text(
                save_path,
                json.dumps(payload, ensure_ascii=False, indent=2),
            )

        return jsonify({"status": "success", "file": filename})
    except ResourceBusyError:
        payload, status = client_error_payload(
            message="Une génération pour ce mois est déjà en cours.",
            status=409,
        )
        return jsonify({"status": "error", **payload}), status
    except Exception as e:
        error_id = log_exception(e, message="Reporting generate-json failed")
        payload, status = client_error_payload(
            message="Erreur lors de la génération du rapport.",
            error_id=error_id,
            status=500,
        )
        return jsonify({"status": "error", **payload}), status


if __name__ == "__main__":
    settings = get_run_settings(5010)
    app.run(host=settings.host, port=settings.port, debug=settings.debug)
