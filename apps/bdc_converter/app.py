from __future__ import annotations

import json
import logging
from dataclasses import asdict
from pathlib import Path

from flask import Flask, jsonify, render_template, request
from jinja2 import ChoiceLoader, FileSystemLoader

from apps.bdc_converter.convert_bdc import convert_folder, parse_french_date
from libs.pipeline.bdc_to_batch import create_monthly_batch_from_data
from libs.reconciliation import detect_bdc_overlaps, extract_reference_ids
from libs.web import (
    ResourceBusyError,
    atomic_write_text,
    client_error_payload,
    get_run_settings,
    get_service_urls,
    init_app_security,
    log_exception,
    resource_lock,
)

BASE_DIR = Path(__file__).parent
ROOT_DIR = Path(__file__).resolve().parents[2]
PDF_FOLDER = BASE_DIR / "pdfs"
DATA_DIR = ROOT_DIR / "data"
BATCH_OUTPUT_DIR = DATA_DIR / "batches"
BATCH_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

SHARED_STATIC_DIR = ROOT_DIR / "apps" / "static"
SHARED_TEMPLATES_DIR = ROOT_DIR / "apps" / "templates"

logger = logging.getLogger("analyseur.bdc")

app = Flask(__name__, static_folder=str(SHARED_STATIC_DIR), static_url_path="/static")
app.jinja_loader = ChoiceLoader(
    [
        FileSystemLoader(str(BASE_DIR / "templates")),
        FileSystemLoader(str(SHARED_TEMPLATES_DIR)),
    ]
)
init_app_security(app, default_port=5000)


def collect_bdc_months(results) -> dict[str, int]:
    """Compte les BDC par mois (YYYY-MM) en ne gardant que les dates valides."""
    months: dict[str, int] = {}
    for result in results:
        if result.get("status") == "failed":
            continue
        parsed = parse_french_date(result.get("date_commande"))
        if parsed is None:
            parsed = parse_french_date(result.get("date_livraison"))
        if parsed is None:
            continue
        key = parsed.strftime("%Y-%m")
        months[key] = months.get(key, 0) + 1
    return months


def infer_bdc_month_year(results) -> str | None:
    """Retourne le mois majoritaire au format MM-YYYY, ou None."""
    months = collect_bdc_months(results)
    if not months:
        return None
    best = max(months.items(), key=lambda item: item[1])[0]
    year, month = best.split("-")
    return f"{month}-{year}"


def _build_documents(results, overlap_map) -> list[dict]:
    documents = []
    for result in results:
        numero_bdc = result.get("numero_bdc")
        doc_overlaps = {}
        if result.get("status", "ok") != "failed":
            for lot_id in extract_reference_ids(result):
                other_numbers = [
                    number for number in overlap_map.get(lot_id, []) if number != numero_bdc
                ]
                if other_numbers:
                    doc_overlaps[lot_id] = other_numbers
        documents.append(
            {
                "filename": result.get("source_filename"),
                "numero_bdc": numero_bdc,
                "reference": result.get("reference"),
                "references": result.get("references") or [],
                "articles_count": len(result.get("articles") or []),
                "status": result.get("status", "ok"),
                "layout": result.get("layout"),
                "warnings": [
                    issue.get("message") if isinstance(issue, dict) else str(issue)
                    for issue in result.get("warnings", [])
                    if issue
                    and (
                        not isinstance(issue, dict) or issue.get("severity") in ("error", "warning")
                    )
                ],
                "overlaps": [
                    {"lot": lot_id, "bdc_numbers": numbers}
                    for lot_id, numbers in doc_overlaps.items()
                ],
            }
        )
    return documents


@app.route("/")
def index():
    return render_template("index.html", current_month=request.args.get("month"))


@app.route("/api/convert", methods=["POST"])
def convert_endpoint():
    progress_log = []

    def progress_callback(*, filename, index, total):
        percent = round((index / total) * 100, 1)
        progress_log.append(
            {
                "filename": filename,
                "index": index,
                "total": total,
                "percent": percent,
            }
        )

    try:
        summary = convert_folder(
            input_folder=str(PDF_FOLDER),
            output_file="",
            progress_callback=progress_callback,
            persist_json=False,
        )

        results = summary.get("results") or []
        assert isinstance(results, list)

        usable_results = [result for result in results if result.get("status", "ok") != "failed"]
        month_counts = collect_bdc_months(usable_results)
        month_year = infer_bdc_month_year(usable_results)
        mixed_months = len(month_counts) > 1

        overlaps = detect_bdc_overlaps(usable_results)
        overlap_map = {str(item["lot"]): list(item["bdc_numbers"]) for item in overlaps}
        documents = _build_documents(results, overlap_map)

        if not usable_results:
            return (
                jsonify(
                    {
                        "error": "Aucun BDC exploitable : vérifier les PDF signalés.",
                        "documents": documents,
                        "failed_files": summary.get("failed_files", []),
                        "failed_documents": summary.get("failed_documents", []),
                        "month": {
                            "value": None,
                            "documents_by_month": month_counts,
                            "mixed": False,
                        },
                    }
                ),
                400,
            )

        if month_year is None:
            return (
                jsonify(
                    {
                        "error": "Mois indéterminé : aucune date de commande valide.",
                        "documents": documents,
                        "failed_files": summary.get("failed_files", []),
                        "failed_documents": summary.get("failed_documents", []),
                        "month": {
                            "value": None,
                            "documents_by_month": month_counts,
                            "mixed": False,
                        },
                    }
                ),
                400,
            )

        output_name = f"convert_bdc_{month_year}.json"
        output_path = BASE_DIR / output_name
        month, year = month_year.split("-")
        batch_id = f"{year}-{month}"

        with resource_lock(f"bdc-convert-{batch_id}", blocking=False):
            atomic_write_text(
                output_path,
                json.dumps(results, ensure_ascii=False, indent=4),
            )
            summary["output_file"] = str(output_path)

            batch = create_monthly_batch_from_data(
                results,
                batch_id=batch_id,
                source_name=output_path.name,
                source_path=str(output_path),
            )
            if mixed_months:
                detail = ", ".join(f"{key} ({count})" for key, count in month_counts.items())
                mixed_warning = (
                    f"Information : BDC de plusieurs mois ({detail}) " f"regroupés sous {batch_id}."
                )
                batch.warnings.append(mixed_warning)
            batch_file_path = BATCH_OUTPUT_DIR / f"{batch.batch_id}.json"
            batch_already_exists = batch_file_path.exists()
            atomic_write_text(
                batch_file_path,
                json.dumps(
                    asdict(batch),
                    ensure_ascii=False,
                    indent=2,
                    default=_json_default,
                ),
            )
    except ResourceBusyError:
        payload, status = client_error_payload(
            message="Une conversion pour ce mois est déjà en cours.",
            status=409,
        )
        return jsonify(payload), status
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 400
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        error_id = log_exception(exc, message="BDC conversion failed")
        payload, status = client_error_payload(
            message="Erreur lors de la conversion.",
            error_id=error_id,
            status=500,
        )
        return jsonify(payload), status

    return jsonify(
        {
            "message": "Conversion terminée",
            "output_file": output_path.name,
            "processed_files": summary["processed_files"],
            "successful_files": len(usable_results),
            "failed_files": summary.get("failed_files", []),
            "failed_documents": summary.get("failed_documents", []),
            "documents": documents,
            "overlaps": overlaps,
            "month": {
                "value": batch_id,
                "documents_by_month": month_counts,
                "mixed": mixed_months,
            },
            "progress": progress_log,
            "batch": {
                "batch_id": batch.batch_id,
                "file": batch_file_path.name,
                "orders": len(batch.orders),
                "source_files": [sf.name for sf in batch.source_files],
                "warnings": list(batch.warnings),
                "already_exists": batch_already_exists,
                "report_url": f"{get_service_urls().reporting.rstrip('/')}/?month={batch.batch_id}",
            },
        }
    )


def _json_default(obj):
    """Serialize dataclasses nested values (datetime, Enum) for json.dumps."""
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    if hasattr(obj, "value"):
        return obj.value
    return str(obj)


if __name__ == "__main__":
    settings = get_run_settings(5000)
    app.run(host=settings.host, port=settings.port, debug=settings.debug)
