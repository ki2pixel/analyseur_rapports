"""Unit tests for the billing domain (draft, rates, versions, guards)."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from libs.billing import build_draft, build_rate_grid, diff_versions, validate_transition
from libs.billing.compute import add_manual_line, apply_line_update
from libs.billing.model import compute_totals, normalize_version


def _reporting(*lots):
    """Build a minimal reporting payload from (lot, {category: count}) pairs."""
    report = []
    for lot, categories in lots:
        payload = {}
        for category in ("short", "standard", "medium", "long"):
            count = int(categories.get(category, 0))
            payload[category] = {
                "count": count,
                "files": [f"{lot}-{category}-{index}.mp4" for index in range(count)],
            }
        report.append({"id_camille": str(lot), "person": "Camille", "categories": payload})
    return {"month": "2026-10", "report": report}


def _bdc(number, delivery, articles, status="ok", warnings=None, lots=("1",)):
    rows = []
    total = Decimal("0")
    for description, quantity, rate in articles:
        amount = Decimal(str(quantity)) * Decimal(str(rate))
        total += amount
        rows.append(
            {
                "description": description,
                "quantite": f"{quantity:.2f}".replace(".", ","),
                "taux": f"{rate:.2f}".replace(".", ","),
                "montant": f"{amount:.2f}".replace(".", ","),
            }
        )
    references = [f"LOTS {lot}" for lot in lots]
    return {
        "numero_bdc": number,
        "date_livraison": delivery,
        "reference": " / ".join(references),
        "references": references,
        "articles": rows,
        "totals": {"total": f"{total:.2f}".replace(".", ",")},
        "status": status,
        "warnings": list(warnings or []),
    }


SHORT = "Recadrage - vidéo courte (moins de 2 min)"
STANDARD = "Recadrage - vidéo standard (entre 3 et 5 minutes)"
MEDIUM = "Recadrage - vidéo moyenne (entre 5 et 7 minutes)"
LONG = "Recadrage - vidéo longue (plus de 8 minutes)"


def test_rate_grid_uses_weighted_majority_and_warns_on_conflict():
    payload = [
        _bdc("BDC-1", "01/10/2026", [(SHORT, 10, 4), (STANDARD, 2, 8)]),
        _bdc("BDC-2", "02/10/2026", [(SHORT, 2, 5)]),
    ]
    grid = build_rate_grid(payload)
    assert grid["rates"]["short"] == Decimal("4.00")
    assert grid["rates"]["standard"] == Decimal("8.00")
    assert any(issue["code"] == "rate_conflict" for issue in grid["warnings"])


def test_draft_actual_capped_provisions_surplus_and_missing_lot():
    report = _reporting(("1", {"short": 3, "standard": 1}), ("2", {"short": 2}))
    bdc = [_bdc("BDC-1", "01/10/2026", [(SHORT, 2, 4), (STANDARD, 1, 8)])]
    version = build_draft(month="2026-10", reporting_payload=report, bdc_payload=bdc, version=1)

    lines = {line["line_id"]: line for line in version["lines"]}
    assert lines["bdc:BDC-1"]["total_cents"] == 1600
    assert lines["uncovered:production"]["total_cents"] == 1200
    assert version["totals"]["ht_cents"] == 2800
    assert lines["uncovered:production"]["detail"]["counts"]["short"] == 3
    assert any(issue["code"] == "no_snapshot" for issue in version["issues"])


def test_draft_ordered_basis_bills_full_order_even_under_production():
    report = _reporting(("1", {"short": 1, "standard": 1}))
    bdc = [_bdc("BDC-1", "01/10/2026", [(SHORT, 2, 4), (STANDARD, 1, 8)])]
    version = build_draft(
        month="2026-10",
        reporting_payload=report,
        bdc_payload=bdc,
        version=1,
        settings={"billing_basis": "ordered"},
    )
    lines = {line["line_id"]: line for line in version["lines"]}
    assert lines["bdc:BDC-1"]["total_cents"] == 1600
    assert version["totals"]["ht_cents"] == 1600


def test_draft_uses_snapshot_without_reporting_payload():
    snapshot = {
        "generated_at": "2026-11-03T12:00:00+00:00",
        "analysis": {
            "cluster_results": [
                {
                    "cluster_key": "1",
                    "label": "Lot 1",
                    "cluster_ids": ["1"],
                    "bdc_numbers": ["BDC-1"],
                    "billed": {"short": 2, "standard": 0, "medium": 0, "long": 0},
                    "actual": {"short": 3, "standard": 0, "medium": 0, "long": 0},
                    "billed_unknown": 0,
                    "billed_total": 2,
                    "actual_total": 3,
                }
            ],
            "missing_invoices": [],
            "unexpected_ids": [],
            "overlapping_bdc_lots": [],
            "certification": {"is_certified": True, "blocking_issues": 0},
            "conformity": {
                "score": 100.0,
                "compliant_orders": 1,
                "total_orders": 1,
            },
            "issues_count": 0,
        },
    }
    bdc = [_bdc("BDC-1", "01/10/2026", [(SHORT, 2, 4)])]
    version = build_draft(
        month="2026-10",
        reporting_payload=None,
        bdc_payload=bdc,
        snapshot=snapshot,
        version=1,
    )
    assert version["snapshot_used"] is True
    assert version["certification"]["is_certified"] is True
    assert not any(issue["code"] == "no_snapshot" for issue in version["issues"])
    assert version["totals"]["ht_cents"] == 1200


def test_stale_snapshot_hash_blocks_finalization():
    snapshot = {
        "sources": {
            "reporting": {"sha256": "old-reporting"},
            "bdc": {"sha256": "same-bdc"},
        },
        "analysis": {
            "cluster_results": [
                {
                    "cluster_key": "1",
                    "label": "Lot 1",
                    "cluster_ids": ["1"],
                    "bdc_numbers": ["BDC-1"],
                    "billed": {"short": 2, "standard": 0, "medium": 0, "long": 0},
                    "actual": {"short": 2, "standard": 0, "medium": 0, "long": 0},
                    "billed_unknown": 0,
                    "billed_total": 2,
                    "actual_total": 2,
                }
            ],
            "missing_invoices": [],
            "unexpected_ids": [],
            "overlapping_bdc_lots": [],
            "certification": {"is_certified": True, "blocking_issues": 0},
            "conformity": {"score": 100.0, "compliant_orders": 1, "total_orders": 1},
            "issues_count": 0,
        },
    }
    bdc = [_bdc("BDC-1", "01/10/2026", [(SHORT, 2, 4)])]
    version = build_draft(
        month="2026-10",
        reporting_payload=None,
        bdc_payload=bdc,
        snapshot=snapshot,
        sources={
            "reporting": {"sha256": "new-reporting"},
            "bdc": {"sha256": "same-bdc"},
        },
        version=1,
    )
    issues = {issue["code"]: issue for issue in version["issues"]}
    assert issues["stale_snapshot"]["severity"] == "error"
    assert validate_transition(version, "envoyé_pour_vérification")


def test_october_2026_reference_billing_matches_invoice_45():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    report_path = root / "apps" / "reporting" / "rapport-2026-10.json"
    bdc_path = root / "apps" / "bdc_converter" / "convert_bdc_10-2026.json"
    if not report_path.is_file() or not bdc_path.is_file():
        pytest.skip("October reference sources are not available")

    version = build_draft(
        month="2026-10",
        reporting_payload=json.loads(report_path.read_text(encoding="utf-8")),
        bdc_payload=json.loads(bdc_path.read_text(encoding="utf-8")),
        version=1,
    )
    bdc_lines = [line for line in version["lines"] if line["kind"] == "bdc"]
    assert len(bdc_lines) == 7
    assert version["totals"]["ht_cents"] == 167600
    assert version["certification"]["is_certified"] is True
    totals = {line["bdc_number"]: line["total_cents"] for line in bdc_lines}
    assert totals == {
        "BDC-26-1689": 11600,
        "BDC-26-1695": 6800,
        "BDC-26-1856": 110000,
        "BDC-26-1862": 8400,
        "BDC-26-1867": 22000,
        "BDC-26-1872": 6800,
        "BDC-26-1879": 2000,
    }
    assert not any(line["kind"] == "uncovered" for line in version["lines"])


def test_adjacent_missing_lot_is_not_provisioned():
    report = _reporting(("1", {"short": 1}), ("2", {"short": 2}))
    bdc = [_bdc("BDC-1", "01/10/2026", [(SHORT, 1, 4)])]
    snapshot = {
        "analysis": {
            "cluster_results": [
                {
                    "cluster_key": "1",
                    "label": "Lot 1",
                    "cluster_ids": ["1"],
                    "bdc_numbers": ["BDC-1"],
                    "billed": {"short": 1, "standard": 0, "medium": 0, "long": 0},
                    "actual": {"short": 1, "standard": 0, "medium": 0, "long": 0},
                    "billed_unknown": 0,
                    "billed_total": 1,
                    "actual_total": 1,
                }
            ],
            "missing_invoices": [
                {
                    "id_camille": "2",
                    "person": "Camille",
                    "categories": {"short": {"count": 2}},
                    "adjacent_month": {"month": "2026-09", "bdc_numbers": ["BDC-OLD"]},
                }
            ],
            "unexpected_ids": [],
            "overlapping_bdc_lots": [],
            "certification": {"is_certified": True, "blocking_issues": 0},
            "conformity": {"score": 100.0, "compliant_orders": 1, "total_orders": 1},
            "issues_count": 0,
        }
    }
    version = build_draft(
        month="2026-10",
        reporting_payload=report,
        bdc_payload=bdc,
        snapshot=snapshot,
        version=1,
    )
    assert not any(line["kind"] == "uncovered" for line in version["lines"])
    assert version["already_billed"][0]["lot"] == "2"
    assert version["totals"]["ht_cents"] == 400


def test_missing_rate_blocks_provision_for_uncovered_category():
    report = _reporting(("1", {"medium": 2}))
    bdc = [_bdc("BDC-1", "01/10/2026", [(SHORT, 1, 4)])]
    version = build_draft(month="2026-10", reporting_payload=report, bdc_payload=bdc, version=1)
    codes = {issue["code"] for issue in version["issues"]}
    assert {"missing_rate", "missing_rate_missing_lot"} & codes
    assert any(issue["severity"] == "error" for issue in version["issues"])


def test_rate_override_resolves_missing_category():
    report = _reporting(("1", {"medium": 2}))
    bdc = [_bdc("BDC-1", "01/10/2026", [(SHORT, 1, 4)])]
    version = build_draft(
        month="2026-10",
        reporting_payload=report,
        bdc_payload=bdc,
        settings={"rate_overrides": {"medium": "12.00"}},
        version=1,
    )
    codes = {issue["code"] for issue in version["issues"]}
    assert "missing_rate" not in codes
    assert "missing_rate_missing_lot" not in codes
    assert version["rate_grid"]["medium"] == "12.00"
    provision = next(line for line in version["lines"] if line["kind"] == "uncovered")
    assert provision["detail"]["counts"]["medium"] == 2
    assert provision["total_cents"] == 2400
    assert version["totals"]["ht_cents"] == 2400


def test_line_update_and_manual_line_recompute_totals():
    report = _reporting(("1", {"short": 1}))
    bdc = [_bdc("BDC-1", "01/10/2026", [(SHORT, 1, 4)])]
    version = build_draft(month="2026-10", reporting_payload=report, bdc_payload=bdc, version=1)
    version = apply_line_update(
        version,
        "bdc:BDC-1",
        label="BDC-1 ajusté",
        quantity="2",
        unit_price="10,00",
        included=True,
        note="test",
    )
    line = version["lines"][0]
    assert line["label"] == "BDC-1 ajusté"
    assert line["total_cents"] == 2000
    assert line["edited"] is True

    version = add_manual_line(
        version,
        label="Frais divers",
        quantity="1",
        unit_price="25,50",
        note="manuel",
    )
    assert version["totals"]["ht_cents"] == 4550
    manual = [line for line in version["lines"] if line["kind"] == "manual"][0]
    assert manual["total_cents"] == 2550


def test_validate_transition_guards_and_acknowledgements():
    report = _reporting(("1", {"short": 1}))
    bdc = [_bdc("BDC-1", "01/10/2026", [(SHORT, 1, 4)])]
    version = build_draft(month="2026-10", reporting_payload=report, bdc_payload=bdc, version=1)

    assert validate_transition(version, "validé_client")
    version["acknowledged_warnings"] = [
        issue["code"] for issue in version["issues"] if issue["severity"] == "warning"
    ]
    assert not validate_transition(version, "validé_client")

    version["issues"].append(
        {"code": "boom", "severity": "error", "message": "Bloquant", "context": {}}
    )
    assert validate_transition(version, "envoyé_pour_vérification")

    version["issues"] = [item for item in version["issues"] if item["code"] != "boom"]
    version["certification"] = {"is_certified": True, "blocking_issues": 0}
    version["status"] = "validé_client"
    assert validate_transition(version, "émis")
    version["invoice"] = {
        "number": "KD202611-45",
        "issued_on": "2026-11-03",
        "total_cents": version["totals"]["ht_cents"],
        "note": "",
    }
    assert not validate_transition(version, "émis")

    version["certification"] = {"is_certified": False, "blocking_issues": 1}
    assert validate_transition(version, "émis")
    assert not validate_transition(version, "émis", override_reason="Correction client")


def test_new_version_recalculates_automatic_lines_and_keeps_manual_lines():
    report = _reporting(("1", {"short": 2}))
    bdc = [_bdc("BDC-1", "01/10/2026", [(SHORT, 2, 4)])]
    v1 = build_draft(month="2026-10", reporting_payload=report, bdc_payload=bdc, version=1)
    v1 = apply_line_update(v1, "bdc:BDC-1", unit_price="99,00")
    v1 = add_manual_line(v1, label="Frais de dossier", quantity="1", unit_price="10,00")

    v2 = build_draft(
        month="2026-10",
        reporting_payload=report,
        bdc_payload=bdc,
        version=2,
        previous=v1,
        keep_manual_lines=True,
    )
    bdc_line = next(line for line in v2["lines"] if line["kind"] == "bdc")
    assert bdc_line["total_cents"] == 800
    assert any(line["kind"] == "manual" for line in v2["lines"])
    assert v2["totals"]["ht_cents"] == 1800


def test_diff_versions_reports_added_removed_and_changed_lines():
    report_one = _reporting(("1", {"short": 2}))
    report_two = _reporting(("1", {"short": 3}), ("2", {"standard": 1}))
    bdc_one = [_bdc("BDC-1", "01/10/2026", [(SHORT, 2, 4)], lots=("1",))]
    bdc_two = [
        _bdc("BDC-1", "01/10/2026", [(SHORT, 3, 4)], lots=("1",)),
        _bdc("BDC-2", "02/10/2026", [(STANDARD, 1, 8)], lots=("2",)),
    ]
    v1 = build_draft(month="2026-10", reporting_payload=report_one, bdc_payload=bdc_one, version=1)
    v2 = build_draft(month="2026-10", reporting_payload=report_two, bdc_payload=bdc_two, version=2)

    diff = diff_versions(v1, v2)
    changes = {item["line_id"]: item["change"] for item in diff["lines"]}
    assert changes["bdc:BDC-1"] == "changed"
    assert changes["bdc:BDC-2"] == "added"
    assert diff["totals"]["delta"] == 1200


def test_compute_totals_ignores_excluded_lines():
    lines = [
        {
            "quantity": "1",
            "unit_price_cents": 1000,
            "vat_rate": "0.00",
            "included": True,
        },
        {
            "quantity": "2",
            "unit_price_cents": 500,
            "vat_rate": "20.00",
            "included": False,
        },
    ]
    totals = compute_totals(lines)
    assert totals == {"ht_cents": 1000, "vat_cents": 0, "ttc_cents": 1000}


def test_normalize_version_recomputes_line_totals():
    version = normalize_version(
        {
            "month": "2026-10",
            "version": 1,
            "settings": {"vat_rate": "0.00"},
            "lines": [
                {
                    "line_id": "manual:1",
                    "kind": "manual",
                    "label": "Test",
                    "quantity": "3",
                    "unit_price_cents": 250,
                }
            ],
        }
    )
    assert version["lines"][0]["total_cents"] == 750
    assert version["totals"]["ht_cents"] == 750


def _cluster(number, modules, billed, actual, certified=False):
    return {
        "cluster_key": number,
        "label": f"Lots {number}",
        "cluster_ids": [number],
        "bdc_numbers": [number],
        "billed": billed,
        "actual": actual,
        "billed_unknown": 0,
        "billed_total": sum(billed.values()),
        "actual_total": sum(actual.values()),
        "certification": {"is_certified": certified, "blocking_issues": 0},
    }


def test_invoice_43_to_45_diff_is_reproduced():
    """Golden scenario: draft 43 → client regularization → final 45."""
    v1_bdc = [
        _bdc("BDC-26-1689", "01/10/2026", [(SHORT, 5, 4), (STANDARD, 12, 8)], lots=("1",)),
        _bdc("BDC-26-1695", "02/10/2026", [(SHORT, 5, 4), (STANDARD, 6, 8)], lots=("2",)),
        _bdc("BDC-26-1856", "23/10/2026", [(SHORT, 72, 4), (STANDARD, 96, 8)], lots=("3",)),
        _bdc("BDC-26-1862", "26/10/2026", [(SHORT, 7, 4), (STANDARD, 7, 8)], lots=("4",)),
        _bdc(
            "BDC-26-1867",
            "29/10/2026",
            [(SHORT, 4, 4), (STANDARD, 23, 8), (LONG, 1, 16)],
            lots=("5",),
        ),
        _bdc("BDC-26-1872", "30/10/2026", [(SHORT, 7, 4), (STANDARD, 5, 8)], lots=("6",)),
    ]
    v1_analysis = {
        "generated_at": "2026-11-02T10:00:00+00:00",
        "cluster_results": [
            _cluster(
                "BDC-26-1689",
                ["1"],
                {"short": 5, "standard": 12, "medium": 0, "long": 0},
                {"short": 5, "standard": 12, "medium": 0, "long": 0},
            ),
            _cluster(
                "BDC-26-1695",
                ["2"],
                {"short": 5, "standard": 6, "medium": 0, "long": 0},
                {"short": 5, "standard": 6, "medium": 0, "long": 0},
            ),
            _cluster(
                "BDC-26-1856",
                ["3"],
                {"short": 72, "standard": 96, "medium": 0, "long": 0},
                {"short": 83, "standard": 96, "medium": 0, "long": 0},
            ),
            _cluster(
                "BDC-26-1862",
                ["4"],
                {"short": 7, "standard": 7, "medium": 0, "long": 0},
                {"short": 7, "standard": 7, "medium": 0, "long": 0},
            ),
            _cluster(
                "BDC-26-1867",
                ["5"],
                {"short": 4, "standard": 23, "medium": 0, "long": 1},
                {"short": 5, "standard": 26, "medium": 0, "long": 1},
            ),
            _cluster(
                "BDC-26-1872",
                ["6"],
                {"short": 7, "standard": 5, "medium": 0, "long": 0},
                {"short": 7, "standard": 5, "medium": 0, "long": 0},
            ),
        ],
        "missing_invoices": [
            {
                "id_camille": "309",
                "person": "Camille",
                "categories": {"short": {"count": 1}, "standard": {"count": 1}},
            }
        ],
        "unexpected_ids": [],
        "overlapping_bdc_lots": [],
        "certification": {"is_certified": False, "blocking_issues": 1},
        "conformity": {"score": 0.0, "compliant_orders": 0, "total_orders": 6},
        "issues_count": 1,
    }
    v1 = build_draft(
        month="2026-10",
        reporting_payload=None,
        bdc_payload=v1_bdc,
        snapshot={"analysis": v1_analysis},
        version=1,
    )
    assert v1["totals"]["ht_cents"] == 169200
    v1_lines = {line["line_id"]: line for line in v1["lines"]}
    assert v1_lines["uncovered:production"]["total_cents"] == 8400
    assert "13 courtes / 4 standards" in v1_lines["uncovered:production"]["note"]

    v2_bdc = [
        _bdc("BDC-26-1689", "01/10/2026", [(SHORT, 5, 4), (STANDARD, 12, 8)], lots=("1",)),
        _bdc("BDC-26-1695", "02/10/2026", [(SHORT, 5, 4), (STANDARD, 6, 8)], lots=("2",)),
        _bdc("BDC-26-1856", "23/10/2026", [(SHORT, 83, 4), (STANDARD, 96, 8)], lots=("3",)),
        _bdc("BDC-26-1862", "26/10/2026", [(SHORT, 7, 4), (STANDARD, 7, 8)], lots=("4",)),
        _bdc(
            "BDC-26-1867",
            "29/10/2026",
            [(SHORT, 5, 4), (STANDARD, 23, 8), (LONG, 1, 16)],
            lots=("5",),
        ),
        _bdc("BDC-26-1872", "30/10/2026", [(SHORT, 7, 4), (STANDARD, 5, 8)], lots=("6",)),
        _bdc("BDC-26-1879", "03/11/2026", [(SHORT, 3, 4), (STANDARD, 1, 8)], lots=("7",)),
    ]
    v2_analysis = {
        "generated_at": "2026-11-03T12:00:00+00:00",
        "cluster_results": [
            _cluster(
                "BDC-26-1689",
                ["1"],
                {"short": 5, "standard": 12, "medium": 0, "long": 0},
                {"short": 5, "standard": 12, "medium": 0, "long": 0},
                certified=True,
            ),
            _cluster(
                "BDC-26-1695",
                ["2"],
                {"short": 5, "standard": 6, "medium": 0, "long": 0},
                {"short": 5, "standard": 6, "medium": 0, "long": 0},
                certified=True,
            ),
            _cluster(
                "BDC-26-1856",
                ["3"],
                {"short": 83, "standard": 96, "medium": 0, "long": 0},
                {"short": 83, "standard": 96, "medium": 0, "long": 0},
                certified=True,
            ),
            _cluster(
                "BDC-26-1862",
                ["4"],
                {"short": 7, "standard": 7, "medium": 0, "long": 0},
                {"short": 7, "standard": 7, "medium": 0, "long": 0},
                certified=True,
            ),
            _cluster(
                "BDC-26-1867",
                ["5"],
                {"short": 5, "standard": 23, "medium": 0, "long": 1},
                {"short": 5, "standard": 23, "medium": 0, "long": 1},
                certified=True,
            ),
            _cluster(
                "BDC-26-1872",
                ["6"],
                {"short": 7, "standard": 5, "medium": 0, "long": 0},
                {"short": 7, "standard": 5, "medium": 0, "long": 0},
                certified=True,
            ),
            _cluster(
                "BDC-26-1879",
                ["7"],
                {"short": 3, "standard": 1, "medium": 0, "long": 0},
                {"short": 3, "standard": 1, "medium": 0, "long": 0},
                certified=True,
            ),
        ],
        "missing_invoices": [],
        "unexpected_ids": [],
        "overlapping_bdc_lots": [],
        "certification": {"is_certified": True, "blocking_issues": 0},
        "conformity": {"score": 100.0, "compliant_orders": 7, "total_orders": 7},
        "issues_count": 0,
    }
    v2 = build_draft(
        month="2026-10",
        reporting_payload=None,
        bdc_payload=v2_bdc,
        snapshot={"analysis": v2_analysis},
        version=2,
    )
    assert v2["totals"]["ht_cents"] == 167600
    assert not any(line["kind"] == "uncovered" for line in v2["lines"])

    diff = diff_versions(v1, v2)
    assert diff["totals"] == {"before": 169200, "after": 167600, "delta": -1600}
    changes = {change["line_id"]: change for change in diff["lines"]}
    assert changes["bdc:BDC-26-1856"]["delta_cents"] == 4400
    assert changes["bdc:BDC-26-1867"]["delta_cents"] == 400
    assert changes["bdc:BDC-26-1879"]["delta_cents"] == 2000
    assert changes["uncovered:production"]["delta_cents"] == -8400


class TestDetailedFormatBilling:
    def test_category_labels_reflect_8_minutes(self):
        from libs.billing.model import CATEGORY_LABELS

        assert CATEGORY_LABELS["medium"] == "Moyenne (5 – 8 min)"
        assert CATEGORY_LABELS["long"] == "Longue (≥ 8 min)"

    def test_new_bdc_medium_label_maps_to_medium_category(self):
        from libs.pipeline.bdc_to_batch import infer_category_from_description

        assert (
            infer_category_from_description("Recadrage - vidéo moyenne (de 5 à 8 min)").value
            == "medium"
        )
        assert (
            infer_category_from_description("Recadrage - vidéo standard (de 2 à 5 min)").value
            == "standard"
        )

    def test_line_detail_exposes_bdc_format_and_annex_summary(self):
        article = [("Recadrage - vidéo courte (moins de 2 min)", 2, 4)]
        bdc = _bdc("BDC-M6-0001", "24/11/2026", article, lots=("42",))
        bdc["bdc_format"] = "detailed"
        bdc["concerned_lots"] = ["42"]
        bdc["annex_videos"] = [
            {"lot": "42", "video_name": "a.mp4", "duration_sec": 30, "category_label": "courte"},
            {"lot": "42", "video_name": "b.mp4", "duration_sec": 40, "category_label": "courte"},
        ]
        reporting = _reporting(("42", {"short": 2}))

        version = build_draft(
            month="2026-11",
            reporting_payload=reporting,
            bdc_payload=[bdc],
            snapshot=None,
        )

        line = next(item for item in version["lines"] if item["line_id"] == "bdc:BDC-M6-0001")
        assert line["detail"]["bdc_format"] == "detailed"
        assert line["detail"]["annex_videos"] == 2
        assert line["detail"]["video_match_summary"]["annex_total"] == 2

    def test_simplified_line_detail_stays_simplified(self):
        article = [("Recadrage - vidéo courte (moins de 2 min)", 1, 4)]
        bdc = _bdc("BDC-26-1882", "03/11/2026", article, lots=("2",))
        reporting = _reporting(("2", {"short": 1}))

        version = build_draft(
            month="2026-11",
            reporting_payload=reporting,
            bdc_payload=[bdc],
            snapshot=None,
        )

        line = next(item for item in version["lines"] if item["line_id"] == "bdc:BDC-26-1882")
        assert line["detail"]["bdc_format"] == "simplified"
        assert line["detail"]["video_match_summary"] == {}

    def test_compensated_lot_mismatch_becomes_info_issue(self):
        analysis = {
            "cluster_results": [
                {
                    "cluster_key": "42",
                    "cluster_ids": ["42"],
                    "bdc_numbers": ["BDC-M6-0001"],
                    "billed": {"short": 2, "standard": 0, "medium": 0, "long": 0},
                    "actual": {"short": 2, "standard": 0, "medium": 0, "long": 0},
                    "billed_unknown": 0,
                    "diagnostics": [
                        {"code": "compensated_lot_mismatch", "severity": "warn", "message": "x"}
                    ],
                    "components": [],
                }
            ],
            "missing_invoices": [],
            "unexpected_ids": [],
            "overlapping_bdc_lots": [],
            "certification": {"is_certified": True},
            "conformity": {"score": 100.0},
        }
        article = [("Recadrage - vidéo courte (moins de 2 min)", 2, 4)]
        bdc = _bdc("BDC-M6-0001", "24/11/2026", article, lots=("42",))

        version = build_draft(
            month="2026-11",
            reporting_payload=None,
            bdc_payload=[bdc],
            snapshot={"analysis": analysis},
        )

        codes = {issue["code"]: issue["severity"] for issue in version["issues"]}
        assert codes["compensated_lot_mismatch"] == "info"
        errors = [item for item in version["issues"] if item["severity"] == "error"]
        assert errors == []
