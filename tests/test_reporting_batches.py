"""Tests de l'intégration des batches BDC dans le reporting."""

import json
from datetime import datetime

from apps.reporting import app as reporting_app_module
from apps.reporting.app import get_batch_for_month, month_options


def _write_archive_month(archives):
    folder = archives / "1 Camille 2026-10-01_10-00-00"
    sub = folder / "hash_a"
    sub.mkdir(parents=True)
    (sub / "metadata.json").write_text(
        json.dumps({"video_path": "/external/archives/1/video.mp4"}),
        encoding="utf-8",
    )
    (sub / "video_scenes.csv").write_text(
        "Scene,Timecode In,Timecode Out\n1,00:00:00.000,00:01:30.000\n",
        encoding="utf-8",
    )


def _write_batch(batches, month):
    batches.mkdir(parents=True, exist_ok=True)
    payload = {
        "batch_id": month,
        "generated_at": datetime(2026, 10, 2, 9, 0).isoformat(),
        "source_files": [
            {
                "name": "BDC-26-1689.pdf",
                "path": "/external/pdfs/BDC-26-1689.pdf",
                "file_type": "pdf",
                "processed_at": datetime(2026, 10, 2, 8, 0).isoformat(),
                "status": "processed",
            }
        ],
        "orders": [
            {
                "numero_bdc": "BDC-26-1689",
                "reference": "LOTS 1",
                "references": ["LOTS 1"],
                "articles": [
                    {
                        "description": "vidéo courte",
                        "quantite": "2",
                        "inferred_category": "short",
                    }
                ],
            }
        ],
        "video_entries": [],
        "warnings": ["Avertissement issu du batch BDC"],
    }
    (batches / f"{month}.json").write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )


def test_get_batch_for_month_merges_bdc_metadata_with_archive_scan(tmp_path, monkeypatch):
    archives = tmp_path / "archives"
    batches = tmp_path / "batches"
    _write_archive_month(archives)
    _write_batch(batches, "2026-10")

    monkeypatch.setattr(reporting_app_module, "ARCHIVES_DIR", archives)
    monkeypatch.setattr(reporting_app_module, "BATCHES_DIR", batches)

    batch = get_batch_for_month("2026-10")

    # The archive scan remains the production source of truth.
    assert len(batch.video_entries) == 1
    assert batch.video_entries[0].id_camille == "1"
    # The BDC batch only contributes invoice-side metadata.
    assert [order.numero_bdc for order in batch.orders] == ["BDC-26-1689"]
    assert [source.name for source in batch.source_files] == ["BDC-26-1689.pdf"]
    assert "Avertissement issu du batch BDC" in batch.warnings


def test_month_options_includes_months_available_only_in_batches(tmp_path):
    archives = tmp_path / "archives"
    batches = tmp_path / "batches"
    batches.mkdir()
    _write_batch(batches, "2026-10")

    assert month_options(archives, batches) == ["2026-10"]
