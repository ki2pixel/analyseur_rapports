"""Non-régression bout-en-bout du mois mixte novembre 2026.

Deux formats de BDC coexistent : le simplifié historique (BDC-26-1882) et le
détaillé (BDC-M6-0001).  Les archives locales sont utilisées quand elles sont
présentes ; sinon le test est ignoré (même règle que les régressions juillet /
septembre documentées dans AGENTS.md).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apps.bdc_converter.convert_bdc import extract_bdc_data
from apps.reporting.analyser import CategoryThresholds, analyze_month
from apps.reporting.app import serialize_report
from libs.billing import build_draft
from libs.reconciliation import compare_reports

ROOT = Path(__file__).resolve().parents[1]
ARCHIVES = ROOT / "archives"
BDC_DIR = ROOT / "apps" / "bdc_converter" / "pdfs"

SIMPLIFIED_PDF = BDC_DIR / "BDC-26-1882.pdf"
DETAILED_PDF = BDC_DIR / "BDC-M6-0001.pdf"

pytestmark = pytest.mark.skipif(
    not (SIMPLIFIED_PDF.is_file() and DETAILED_PDF.is_file()),
    reason="PDF de novembre 2026 absents",
)


@pytest.fixture(scope="module")
def november_payloads():
    if not any(ARCHIVES.glob("* 2026-11-*")):
        pytest.skip("Archives de novembre 2026 absentes")
    bdc = [extract_bdc_data(str(SIMPLIFIED_PDF)), extract_bdc_data(str(DETAILED_PDF))]
    reports = analyze_month(ARCHIVES, 2026, 11, CategoryThresholds())
    reporting = {"month": "2026-11", "report": serialize_report(reports)}
    return bdc, reporting


def test_both_bdc_formats_extract_without_error(november_payloads):
    bdc, _ = november_payloads
    by_number = {item["numero_bdc"]: item for item in bdc}

    assert set(by_number) == {"BDC-26-1882", "BDC-M6-0001"}
    assert by_number["BDC-26-1882"]["bdc_format"] == "simplified"
    assert by_number["BDC-M6-0001"]["bdc_format"] == "detailed"
    assert all(item["status"] == "ok" for item in bdc)
    assert all(not item["warnings"] for item in bdc)

    detailed = by_number["BDC-M6-0001"]
    assert len(detailed["concerned_lots"]) == 73
    assert len(detailed["annex_videos"]) == 134
    assert detailed["period"] == "Lots 42 et + · 11/2026"


def test_medium_threshold_aligns_lot_166(november_payloads):
    _, reporting = november_payloads

    lots = {group["id_camille"]: group for group in reporting["report"]}
    lot_166 = lots["166"]["categories"]
    assert lot_166["medium"]["count"] == 1
    assert lot_166["long"]["count"] == 0
    assert lot_166["medium"]["files_detail"][0]["duration_sec"] == pytest.approx(460.52, abs=0.1)


def test_reconciliation_is_fully_compliant(november_payloads):
    bdc, reporting = november_payloads

    result = compare_reports(reporting, bdc)

    assert result["conformity"]["score"] == 100.0
    assert result["conformity"]["total_orders"] == 2
    assert result["overlapping_bdc_lots"] == []
    assert [item["id_camille"] for item in result["missing_invoices"]] == ["40"]
    for cluster in result["cluster_results"]:
        assert cluster["is_compliant"] is True
        assert all(value == 0 for value in cluster["delta"].values())


def test_no_false_duplicate_on_shared_video(november_payloads):
    bdc, reporting = november_payloads

    result = compare_reports(reporting, bdc)
    detailed = next(cluster for cluster in result["cluster_results"] if cluster["has_video_detail"])

    assert detailed["has_jingle_or_duplicate"] is False
    codes = {diag["code"] for diag in detailed["diagnostics"]}
    assert "jingle_or_duplicate" not in codes


def test_video_reconciliation_matches_all_but_duration_drift(november_payloads):
    bdc, reporting = november_payloads

    result = compare_reports(reporting, bdc)
    detailed = next(cluster for cluster in result["cluster_results"] if cluster["has_video_detail"])
    reconciliation = detailed["video_reconciliation"]

    assert reconciliation["annex_total"] == 134
    assert reconciliation["counts"] == {"matched": 133, "duration_drift": 1}
    # Les 134 vidéos sont appariées ; la dérive du lot 185 reste un badge info.
    assert reconciliation["match_rate"] == 100.0
    drift = reconciliation["duration_drifts"][0]
    assert drift["lot"] == "185"
    assert drift["bdc_duration_sec"] == 3
    assert drift["status"] == "duration_drift"
    assert drift["category_matches"] is True


def test_mov_extension_tolerance_on_real_annex(november_payloads):
    bdc, _ = november_payloads
    detailed = next(item for item in bdc if item["bdc_format"] == "detailed")

    mov_lots = {
        item["lot"]
        for item in detailed["annex_videos"]
        if item["video_name"].lower().endswith(".mov")
    }

    assert mov_lots == {"112", "113", "201"}


def test_billing_totals_for_mixed_month(november_payloads):
    bdc, reporting = november_payloads

    analysis = compare_reports(reporting, bdc)
    version = build_draft(
        month="2026-11",
        reporting_payload=reporting,
        bdc_payload=bdc,
        snapshot={"analysis": analysis},
    )

    lines = {line["line_id"]: line for line in version["lines"]}
    assert lines["bdc:BDC-26-1882"]["total_cents"] == 4800
    assert lines["bdc:BDC-M6-0001"]["total_cents"] == 71200
    assert lines["uncovered:production"]["total_cents"] == 2800
    assert version["totals"]["ht_cents"] == 78800
    assert [item for item in version["issues"] if item["severity"] == "error"] == []


def test_lot_40_without_bdc_blocks_certification(november_payloads):
    bdc, reporting = november_payloads

    result = compare_reports(reporting, bdc)

    assert result["certification"]["is_certified"] is False
    assert result["certification"]["blocking_issues"] == 1
