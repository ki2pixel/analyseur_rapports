"""Tests du moteur de rapprochement : plages, chevauchements, score et diagnostics."""

from libs.reconciliation.comparison import (
    compare_reports,
    detect_bdc_overlaps,
    extract_reference_ids,
)


def _category(count, files=None, durations=None):
    files = files or []
    details = []
    for index, name in enumerate(files):
        duration = durations[index] if durations and index < len(durations) else None
        details.append({"name": name, "duration_sec": duration})
    return {"count": count, "files": files, "files_detail": details}


def _report(groups):
    return {
        "report": [
            {
                "id_camille": group_id,
                "person": "Camille",
                "categories": categories,
            }
            for group_id, categories in groups.items()
        ]
    }


def _order(number, reference, articles):
    return {
        "numero_bdc": number,
        "reference": reference,
        "references": [reference],
        "articles": articles,
    }


def test_range_expands_only_known_production_lots():
    report = _report(
        {
            "38": {"standard": _category(1, ["v1.mp4"])},
            "40": {"standard": _category(1, ["v2.mp4"])},
            "42": {"standard": _category(1, ["v3.mp4"])},
        }
    )
    bdc = [
        _order(
            "BDC-RANGE",
            "LOTS 38 à 92",
            [{"description": "vidéo standard", "quantite": "3"}],
        )
    ]

    result = compare_reports(report, bdc)

    assert result["missing_invoices"] == []
    assert result["unexpected_ids"] == []
    cluster = result["cluster_results"][0]
    assert cluster["cluster_ids"] == ["38", "40", "42"]
    assert cluster["missing_reporting_ids"] == []
    assert cluster["delta"] == {"short": 0, "standard": 0, "medium": 0, "long": 0}


def test_disambiguated_lots_are_allocated_to_matching_bdcs():
    report = _report(
        {
            "309 (2026-10-01)": {"short": _category(3, ["a1.mp4", "a2.mp4", "a3.mp4"])},
            "309 (2026-10-27)": {"long": _category(1, ["b1.mp4"])},
            "310": {"standard": _category(1, ["c1.mp4"])},
        }
    )
    bdc = [
        _order(
            "BDC-LONG",
            "LOT 309",
            [{"description": "vidéo longue", "quantite": "1"}],
        ),
        _order(
            "BDC-SEPT",
            "LOTS 309/310",
            [
                {"description": "vidéo courte", "quantite": "3"},
                {"description": "vidéo standard", "quantite": "1"},
            ],
        ),
    ]

    result = compare_reports(report, bdc)
    clusters = {cluster["bdc_numbers"][0]: cluster for cluster in result["cluster_results"]}

    assert clusters["BDC-LONG"]["cluster_ids"] == ["309 (2026-10-27)"]
    assert clusters["BDC-SEPT"]["cluster_ids"] == ["309 (2026-10-01)", "310"]
    assert all(
        cluster["delta"] == {"short": 0, "standard": 0, "medium": 0, "long": 0}
        for cluster in clusters.values()
    )
    assert result["overlapping_bdc_lots"] == []
    assert result["missing_invoices"] == []
    assert result["conformity"]["score"] == 100.0


def test_single_bdc_still_receives_all_disambiguated_folders():
    report = _report(
        {
            "309 (2026-10-01)": {"short": _category(3, ["a1.mp4", "a2.mp4", "a3.mp4"])},
            "309 (2026-10-27)": {"long": _category(1, ["b1.mp4"])},
        }
    )
    bdc = [
        _order(
            "BDC-ALL",
            "LOT 309",
            [
                {"description": "vidéo courte", "quantite": "3"},
                {"description": "vidéo longue", "quantite": "1"},
            ],
        )
    ]

    result = compare_reports(report, bdc)

    cluster = result["cluster_results"][0]
    assert cluster["cluster_ids"] == ["309 (2026-10-01)", "309 (2026-10-27)"]
    assert cluster["delta"] == {"short": 0, "standard": 0, "medium": 0, "long": 0}
    assert result["missing_invoices"] == []


def test_disambiguated_folders_can_still_be_genuinely_overlapping():
    report = _report(
        {
            "309 (2026-10-01)": {"short": _category(3, ["a1.mp4", "a2.mp4", "a3.mp4"])},
            "309 (2026-10-27)": {"long": _category(1, ["b1.mp4"])},
        }
    )
    bdc = [
        _order(
            "BDC-A",
            "LOT 309",
            [{"description": "vidéo courte", "quantite": "3"}],
        ),
        _order(
            "BDC-B",
            "LOT 309",
            [{"description": "vidéo courte", "quantite": "3"}],
        ),
    ]

    result = compare_reports(report, bdc)

    assert result["overlapping_bdc_lots"] == [
        {"lot": "309 (2026-10-01)", "bdc_numbers": ["BDC-A", "BDC-B"]}
    ]
    assert [item["id_camille"] for item in result["missing_invoices"]] == ["309 (2026-10-27)"]


def test_overlap_detection_and_delta_sign():
    report = _report(
        {
            "21": {"standard": _category(1, ["v21.mp4"])},
            "22": {"standard": _category(1, ["v22.mp4"])},
            "23": {"standard": _category(1, ["v23.mp4"])},
        }
    )
    bdc = [
        _order(
            "BDC-A",
            "LOTS 21/23",
            [{"description": "vidéo standard", "quantite": "2"}],
        ),
        _order(
            "BDC-B",
            "LOTS 21 à 23",
            [{"description": "vidéo standard", "quantite": "2"}],
        ),
    ]

    result = compare_reports(report, bdc)

    assert result["overlapping_bdc_lots"] == [
        {"lot": "21", "bdc_numbers": ["BDC-A", "BDC-B"]},
        {"lot": "23", "bdc_numbers": ["BDC-A", "BDC-B"]},
    ]
    assert result["conformity"]["score"] == 50.0
    cluster_b = next(
        cluster for cluster in result["cluster_results"] if cluster["bdc_numbers"] == ["BDC-B"]
    )
    assert cluster_b["delta"]["standard"] == 1  # Réalisé (3) − Facturé (2)
    assert cluster_b["diagnostic"]["code"] == "bdc_overlap"
    assert cluster_b["requires_review"] is True


def test_jingle_diagnostic_and_corrected_delta():
    report = _report(
        {
            "63": {
                "short": _category(
                    2,
                    ["manche.mp4", "m6+max anime.mp4"],
                    [90.0, 60.0],
                )
            }
        }
    )
    bdc = [
        _order(
            "BDC-1528",
            "LOT 63",
            [{"description": "vidéo courte", "quantite": "1"}],
        )
    ]

    result = compare_reports(report, bdc)

    cluster = result["cluster_results"][0]
    assert cluster["delta"]["short"] == 1
    assert cluster["has_jingle_or_duplicate"] is True
    assert cluster["diagnostic"]["code"] == "jingle_or_duplicate"
    assert "parfait accord" in cluster["diagnostic"]["message"]


def test_near_boundary_diagnostic():
    report = _report(
        {
            "1": {
                "standard": _category(1, ["bord.mp4"], [119.5]),
            }
        }
    )
    bdc = [
        _order(
            "BDC-NEAR",
            "LOT 1",
            [{"description": "vidéo courte", "quantite": "1"}],
        )
    ]

    result = compare_reports(report, bdc)

    cluster = result["cluster_results"][0]
    assert cluster["delta"] == {"short": -1, "standard": 1, "medium": 0, "long": 0}
    assert cluster["has_near_threshold_video"] is True
    assert cluster["diagnostic"]["code"] == "near_boundary"
    assert "La vidéo « bord.mp4 » du lot 1" in cluster["diagnostic"]["message"]
    assert "du seuil de 120 s" in cluster["diagnostic"]["message"]


def test_near_boundary_diagnostic_aggregates_multiple_videos():
    report = _report(
        {
            "1": {
                "standard": _category(
                    2,
                    ["bord-120.mp4", "bord-300.mp4"],
                    [119.5, 299.5],
                ),
            }
        }
    )
    bdc = [
        _order(
            "BDC-NEAR",
            "LOT 1",
            [{"description": "vidéo courte", "quantite": "2"}],
        )
    ]

    result = compare_reports(report, bdc)

    cluster = result["cluster_results"][0]
    diagnostic = cluster["diagnostic"]
    assert diagnostic["code"] == "near_boundary"
    assert "2 vidéos de ce bon de commande" in diagnostic["message"]
    assert "des seuils de 120 s et 300 s" in diagnostic["message"]
    assert "bord-120.mp4" not in diagnostic["message"]
    assert "bord-300.mp4" not in diagnostic["message"]


def test_adjacent_month_diagnostic():
    report = _report({"1": {"standard": _category(1, ["v1.mp4"])}})
    bdc = [
        _order(
            "BDC-CURRENT",
            "LOTS 1/189",
            [{"description": "vidéo standard", "quantite": "1"}],
        )
    ]
    adjacent = {
        "2026-08": {
            "report": _report({"189": {"standard": _category(1, ["v189.mp4"])}}),
            "bdc": [
                _order(
                    "BDC-AUG",
                    "LOT 189",
                    [{"description": "vidéo standard", "quantite": "1"}],
                )
            ],
        }
    }

    result = compare_reports(report, bdc, adjacent_months=adjacent)

    cluster = result["cluster_results"][0]
    assert cluster["found_in_adjacent_month"] is True
    assert cluster["diagnostic"]["code"] == "adjacent_month"
    assert "2026-08" in cluster["diagnostic"]["message"]
    assert "BDC-AUG" in cluster["diagnostic"]["message"]


def test_adjacent_invoice_without_adjacent_production():
    """Un lot produit ce mois et facturé sur le BDC du mois suivant doit être signalé."""
    report = _report({"189": {"short": _category(1, ["v189.mp4"])}})
    adjacent = {
        "2026-08": {
            "report": _report({}),
            "bdc": [_order("BDC-AUG", "LOT 189", [])],
        }
    }

    result = compare_reports(report, [], adjacent_months=adjacent)

    missing = result["missing_invoices"][0]
    assert missing["id_camille"] == "189"
    assert missing["adjacent_month"] == {
        "month": "2026-08",
        "bdc_numbers": ["BDC-AUG"],
        "in_report": False,
    }


def test_adjacent_range_does_not_create_phantom_lots():
    report = _report({"50": {"short": _category(1, ["v50.mp4"])}})
    adjacent = {
        "2026-09": {
            "report": _report(
                {
                    "38": {"short": _category(1, ["v38.mp4"])},
                    "40": {"short": _category(1, ["v40.mp4"])},
                }
            ),
            "bdc": [_order("BDC-SEP", "LOTS 38 à 92", [])],
        }
    }

    result = compare_reports(report, [], adjacent_months=adjacent)

    missing = result["missing_invoices"][0]
    # 50 lies inside 38..92 but is absent from the September report: no phantom hit.
    assert missing["id_camille"] == "50"
    assert missing["adjacent_month"] is None


def test_unknown_bdc_articles_block_certification():
    report = _report({"1": {"short": _category(1, ["v1.mp4"])}})
    bdc = [
        _order(
            "BDC-UNKNOWN",
            "LOT 1",
            [{"description": "Prestation diverse", "quantite": "2"}],
        )
    ]

    result = compare_reports(report, bdc)

    cluster = result["cluster_results"][0]
    assert cluster["billed_unknown"] == 2
    assert cluster["billed_total"] == 2
    assert cluster["is_compliant"] is False
    assert cluster["has_unknown_articles"] is True
    assert cluster["diagnostic"]["code"] == "unknown_articles"
    assert result["unknown_billed"] == 2
    assert result["certification"]["is_certified"] is False


def test_justified_duplicate_is_counted_as_compliant():
    report = _report(
        {
            "1": {"standard": _category(1, ["v1.mp4"])},
            "2": {"standard": _category(2, ["dup.mp4", "dup.mp4"])},
        }
    )
    bdc = [
        _order(
            "BDC-OK",
            "LOT 1",
            [{"description": "vidéo standard", "quantite": "1"}],
        ),
        _order(
            "BDC-ISSUE",
            "LOT 2",
            [{"description": "vidéo standard", "quantite": "1"}],
        ),
    ]

    initial = compare_reports(report, bdc)
    assert initial["conformity"]["score"] == 50.0
    issue_cluster = next(
        cluster for cluster in initial["cluster_results"] if cluster["bdc_numbers"] == ["BDC-ISSUE"]
    )
    assert issue_cluster["justification_eligible"] is True

    justified = compare_reports(report, bdc, justified_clusters={issue_cluster["cluster_key"]})

    assert justified["conformity"]["score"] == 100.0
    assert justified["conformity"]["justified_orders"] == 1
    assert justified["conformity"]["strict_compliant_orders"] == 1
    assert justified["cluster_differences"] == []
    justified_cluster = next(
        cluster
        for cluster in justified["cluster_results"]
        if cluster["bdc_numbers"] == ["BDC-ISSUE"]
    )
    assert justified_cluster["justified"] is True
    assert justified_cluster["is_compliant"] is True


def test_missing_volume_cannot_be_justified():
    report = _report(
        {
            "1": {"standard": _category(1, ["v1.mp4"])},
            "2": {"standard": _category(1, ["v2.mp4"])},
        }
    )
    bdc = [
        _order(
            "BDC-OK",
            "LOT 1",
            [{"description": "vidéo standard", "quantite": "1"}],
        ),
        _order(
            "BDC-ISSUE",
            "LOT 2",
            [{"description": "vidéo standard", "quantite": "2"}],
        ),
    ]

    initial = compare_reports(report, bdc)
    assert initial["conformity"]["score"] == 50.0
    issue_cluster = next(
        cluster for cluster in initial["cluster_results"] if cluster["bdc_numbers"] == ["BDC-ISSUE"]
    )
    assert issue_cluster["justification_eligible"] is False
    assert "volumes manquants" in issue_cluster["justification_hint"]

    attempted = compare_reports(report, bdc, justified_clusters={issue_cluster["cluster_key"]})

    assert attempted["conformity"]["score"] == 50.0
    assert attempted["conformity"]["justified_orders"] == 0
    attempted_cluster = next(
        cluster
        for cluster in attempted["cluster_results"]
        if cluster["bdc_numbers"] == ["BDC-ISSUE"]
    )
    assert attempted_cluster["justified"] is False
    assert attempted_cluster["is_compliant"] is False
    assert attempted_cluster["requires_review"] is True


def test_detect_bdc_overlaps_and_extract_reference_ids():
    bdc = [
        _order("BDC-A", "LOTS 21/23", []),
        _order("BDC-B", "LOTS 21 à 25", []),
    ]

    overlaps = detect_bdc_overlaps(bdc)
    assert overlaps == [
        {"lot": "21", "bdc_numbers": ["BDC-A", "BDC-B"]},
        {"lot": "23", "bdc_numbers": ["BDC-A", "BDC-B"]},
    ]

    assert extract_reference_ids(bdc[1]) == ("21", "22", "23", "24", "25")
    assert extract_reference_ids(bdc[1], {"21", "23"}) == ("21", "23")


def test_wide_range_expansion():
    order = _order("BDC-WIDE", "LOTS 41 à 276", [])
    extracted_full = extract_reference_ids(order)
    assert len(extracted_full) == 236
    assert extracted_full[0] == "41"
    assert extracted_full[-1] == "276"
    known = {"41", "48", "50", "276"}
    assert extract_reference_ids(order, known) == ("41", "48", "50", "276")


def test_reference_identifiers_are_not_expanded_as_lot_ranges():
    for reference in ("BDC-26-1689", "2026-10", "2026-10-01", "01/09/2026", "10-2026"):
        order = _order("BDC-X", reference, [])
        # No range expansion: the identifier is kept as a single fallback label.
        assert extract_reference_ids(order) == (reference,)


def test_overlap_bdc_numbers_are_deterministically_sorted():
    bdc = [
        _order("BDC-26-1482", "LOTS 21 à 23", []),
        _order("BDC-26-1479", "LOTS 21/23", []),
    ]

    overlaps = detect_bdc_overlaps(bdc)

    assert [item["lot"] for item in overlaps] == ["21", "23"]
    assert overlaps[0]["bdc_numbers"] == ["BDC-26-1479", "BDC-26-1482"]


def test_production_lots_file_annotation():
    report = _report(
        {
            "10": {
                "short": _category(
                    2,
                    ["clip_dup.mp4", "clip_near.mp4"],
                    durations=[45.0, 118.5],
                )
            },
            "20": {
                "short": _category(
                    1,
                    ["clip_dup.mp4"],
                    durations=[45.5],
                )
            },
        }
    )
    bdc = [_order("BDC-1", "LOTS 10/20", [{"description": "vidéo courte", "quantite": "2"}])]

    result = compare_reports(report, bdc)
    cluster = result["cluster_results"][0]
    assert cluster["delta"]["short"] == 1  # 3 actual - 2 billed

    lots = {lot["id_camille"]: lot for lot in cluster["production_lots"]}
    lot_10_files = lots["10"]["categories"]["short"]["file_details"]
    lot_20_files = lots["20"]["categories"]["short"]["file_details"]

    assert lot_10_files[0]["name"] == "clip_dup.mp4"
    # Only the surplus occurrence is flagged; the first one stays representative.
    assert lot_10_files[0]["is_duplicate"] is False
    assert lot_10_files[0]["category_delta"] == 1

    assert lot_10_files[1]["name"] == "clip_near.mp4"
    assert lot_10_files[1]["is_near_boundary"] is True
    assert lot_10_files[1]["near_threshold"] == 120.0

    assert lot_20_files[0]["name"] == "clip_dup.mp4"
    assert lot_20_files[0]["is_duplicate"] is True
