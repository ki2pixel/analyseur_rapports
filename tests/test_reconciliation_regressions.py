"""Régressions de production : jingles, doublons et chevauchements BDC."""

import json
from pathlib import Path

from apps.reconciliation.app import _build_view_model
from libs.reconciliation import compare_reports


def _load_month(month: str):
    report_path = Path(f"apps/reporting/rapport-{month}.json")
    bdc_month = f"{month[5:7]}-{month[:4]}"
    bdc_path = Path(f"apps/bdc_converter/convert_bdc_{bdc_month}.json")
    assert report_path.exists()
    assert bdc_path.exists()
    return (
        json.loads(report_path.read_text(encoding="utf-8")),
        json.loads(bdc_path.read_text(encoding="utf-8")),
    )


def test_september_jingles_and_details_are_filtered():
    report, _ = _load_month("2026-09")
    serialized_report = json.dumps(report["report"], ensure_ascii=False)

    assert "m6+max anime.mp4" not in serialized_report
    assert '"m6+.mp4"' not in serialized_report
    assert report["total_mp4"] == 254

    lot_63 = next(group for group in report["report"] if group["id_camille"] == "63")
    assert lot_63["categories"]["short"]["count"] == 1
    assert lot_63["categories"]["standard"]["count"] == 2
    assert lot_63["categories"]["short"]["files_detail"][0]["duration_sec"] is not None


def test_september_overlap_and_1528_compliance():
    report, bdc = _load_month("2026-09")
    result = compare_reports(report, bdc)

    assert result["overlapping_bdc_lots"] == [
        {"lot": "21", "bdc_numbers": ["BDC-26-1479", "BDC-26-1482"]},
        {"lot": "23", "bdc_numbers": ["BDC-26-1479", "BDC-26-1482"]},
        {"lot": "26", "bdc_numbers": ["BDC-26-1479", "BDC-26-1482"]},
        {"lot": "27", "bdc_numbers": ["BDC-26-1479", "BDC-26-1482"]},
        {"lot": "31", "bdc_numbers": ["BDC-26-1479", "BDC-26-1482"]},
        {"lot": "33", "bdc_numbers": ["BDC-26-1479", "BDC-26-1482"]},
        {"lot": "36", "bdc_numbers": ["BDC-26-1479", "BDC-26-1482"]},
        {"lot": "39", "bdc_numbers": ["BDC-26-1479", "BDC-26-1482"]},
    ]

    cluster = next(
        item for item in result["cluster_results"] if item["bdc_numbers"] == ["BDC-26-1528"]
    )
    assert cluster["delta"] == {"short": 0, "standard": 0, "medium": 0, "long": 0}
    assert cluster["is_compliant"] is True
    assert cluster["requires_review"] is False
    assert cluster["diagnostic"] is None
    assert result["certification"]["is_certified"] is False


def test_september_export_annex_reconciles_overlaps():
    report, bdc = _load_month("2026-09")
    view_model = _build_view_model(compare_reports(report, bdc))
    annex = view_model["annex"]

    assert annex["cluster_billed_total"] == 261
    assert annex["cluster_actual_total"] == 256
    assert annex["overlap_actual"] == 29
    assert annex["unattached_actual"] == 27
    assert annex["grand_billed"] == 261
    assert annex["grand_actual"] == 254
    assert annex["grand_delta"] == -7


def test_october_corrected_bdcs_are_all_compliant():
    report, bdc = _load_month("2026-10")
    result = compare_reports(report, bdc)

    assert result["conformity"]["score"] == 100.0
    assert result["conformity"]["compliant_orders"] == 7
    assert result["conformity"]["total_orders"] == 7
    assert result["conformity"]["review_clusters"] == 0
    assert result["certification"] == {"is_certified": True, "blocking_issues": 0}
    assert result["issues_count"] == 0
    assert result["missing_invoices"] == []
    assert result["unexpected_ids"] == []
    assert result["overlapping_bdc_lots"] == []
    assert result["cluster_differences"] == []

    clusters = {cluster["bdc_numbers"][0]: cluster for cluster in result["cluster_results"]}
    assert clusters["BDC-26-1856"]["billed"] == {
        "short": 83,
        "standard": 96,
        "medium": 0,
        "long": 0,
    }
    assert clusters["BDC-26-1867"]["cluster_ids"] == [
        "304",
        "305",
        "309 (2026-10-27)",
        "313",
        "315",
        "322",
        "325",
        "328",
        "335",
    ]
    assert clusters["BDC-26-1867"]["actual_total"] == 29
    assert clusters["BDC-26-1879"]["cluster_ids"] == ["309 (2026-10-01)", "310"]
    assert clusters["BDC-26-1879"]["actual_total"] == 4
    assert all(
        cluster["delta"] == {"short": 0, "standard": 0, "medium": 0, "long": 0}
        for cluster in clusters.values()
    )

    annex = _build_view_model(result)["annex"]
    assert annex["overlap_actual"] == 0
    assert annex["grand_billed"] == 266
    assert annex["grand_actual"] == 266
    assert annex["grand_delta"] == 0


def test_october_wide_range_expansion_and_bdc_1856():
    report, bdc = _load_month("2026-10")
    result = compare_reports(report, bdc)

    cluster_1856 = next(
        item for item in result["cluster_results"] if item["bdc_numbers"] == ["BDC-26-1856"]
    )
    # Range 'LOTS 41 à 276' encompasses 84 production lots (including lots 89 and 183 whose M6+ titles are preserved)
    assert len(cluster_1856["cluster_ids"]) == 84
    assert cluster_1856["billed"] == {"short": 83, "standard": 96, "medium": 0, "long": 0}
    assert cluster_1856["actual"] == {"short": 83, "standard": 96, "medium": 0, "long": 0}
    assert cluster_1856["delta"] == {"short": 0, "standard": 0, "medium": 0, "long": 0}
    assert cluster_1856["billed_total"] == 179
    assert cluster_1856["actual_total"] == 179

    # Lots between 41 and 276 (such as 48, 50, etc.) must not be marked unbilled
    missing_ids = [item["id_camille"] for item in result["missing_invoices"]]
    assert missing_ids == []
    assert "48" not in missing_ids
    assert "50" not in missing_ids

    # The corrected BDC closes every gap: no lot needs a suspect badge.
    clean_lots = [lot for lot in cluster_1856["production_lots"] if not lot.get("has_suspects")]
    suspect_lots = [
        lot["id_camille"] for lot in cluster_1856["production_lots"] if lot.get("has_suspects")
    ]
    assert len(clean_lots) == 84
    assert suspect_lots == []

    # BDC-26-1872 includes lots 355 and 356 and is 100% compliant
    cluster_1872 = next(
        item for item in result["cluster_results"] if item["bdc_numbers"] == ["BDC-26-1872"]
    )
    assert "355" in cluster_1872["cluster_ids"]
    assert "356" in cluster_1872["cluster_ids"]
    assert cluster_1872["is_compliant"] is True
    assert cluster_1872["delta"] == {"short": 0, "standard": 0, "medium": 0, "long": 0}
    assert cluster_1872.get("unbilled_lots") is None

    # BDC-26-1867 now gets the 2026-10-27 folder only, while the new
    # BDC-26-1879 gets the 2026-10-01 folder plus lot 310.
    cluster_1867 = next(
        item for item in result["cluster_results"] if item["bdc_numbers"] == ["BDC-26-1867"]
    )
    assert "309 (2026-10-27)" in cluster_1867["cluster_ids"]
    assert "309 (2026-10-01)" not in cluster_1867["cluster_ids"]
    assert cluster_1867["actual_total"] == 29
    assert cluster_1867["delta"] == {"short": 0, "standard": 0, "medium": 0, "long": 0}

    cluster_1879 = next(
        item for item in result["cluster_results"] if item["bdc_numbers"] == ["BDC-26-1879"]
    )
    assert cluster_1879["cluster_ids"] == ["309 (2026-10-01)", "310"]
    assert cluster_1879["actual_total"] == 4
    assert cluster_1879["delta"] == {"short": 0, "standard": 0, "medium": 0, "long": 0}


def test_october_bdc_1695_duplicate_diagnostic_even_with_zero_delta():
    report, bdc = _load_month("2026-10")
    result = compare_reports(report, bdc)

    cluster_1695 = next(
        item for item in result["cluster_results"] if item["bdc_numbers"] == ["BDC-26-1695"]
    )
    # BDC-26-1695 has zero delta overall
    assert all(value == 0 for value in cluster_1695["delta"].values())

    # But contains a duplicate file between lot 26 and lot 27
    diagnostic = cluster_1695["diagnostic"]
    assert diagnostic is not None
    assert diagnostic["code"] == "jingle_or_duplicate"
    assert diagnostic["suspect_kind"] == "duplicate"
    assert diagnostic["suspect_files"][0]["name"] == (
        "Extrait prédiff – Ils doivent imiter les animaux lors de l’expédition.mp4"
    )

    # The duplicate is real but only the surplus occurrence (lot 27) is flagged.
    suspect_lots = [
        lot["id_camille"] for lot in cluster_1695["production_lots"] if lot.get("has_suspects")
    ]
    assert suspect_lots == ["27"]

    # Video with 121.1s in lot 27 is standard and NOT flagged as near threshold (no false positive)
    lot_27 = next(lot for lot in cluster_1695["production_lots"] if lot["id_camille"] == "27")
    video_121 = next(
        detail
        for detail in lot_27["categories"]["standard"]["file_details"]
        if "Antho et Emi" in detail["name"]
    )
    assert video_121["duration_sec"] is not None and video_121["duration_sec"] > 120.0
    assert video_121["is_near_boundary"] is False

    # BDC-26-1856 is now fully aligned: no duplicate or threshold diagnostic remains.
    cluster_1856 = next(
        item for item in result["cluster_results"] if item["bdc_numbers"] == ["BDC-26-1856"]
    )
    assert cluster_1856["diagnostics"] == []
    assert cluster_1856["diagnostic"] is None
