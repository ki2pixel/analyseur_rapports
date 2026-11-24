"""Tests de non-régression et de validation de l'extraction des BDC PDF."""

import hashlib
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pdfplumber
import pytest

from apps.bdc_converter.convert_bdc import (
    _extract_articles_from_words,
    _status_from_issues,
    _to_decimal,
    extract_bdc_data,
    parse_french_date,
    validate_bdc_data,
)

FIXTURES = Path(__file__).parent / "fixtures" / "bdc"
MANIFEST = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))
DOCUMENTS = MANIFEST["documents"]


def _load_expected(document):
    path = FIXTURES / "expected" / document["expected"]
    return json.loads(path.read_text(encoding="utf-8"))


def _valid_data():
    return {
        "numero_bdc": "BDC-26-1689",
        "date_commande": "01/10/2026",
        "date_livraison": "01/10/2026",
        "reference": "LOTS 2/7",
        "references": ["LOTS 2", "7"],
        "articles": [
            {
                "description": "Recadrage - vidéo courte (moins de 2 min)",
                "quantite": "5,00",
                "unite": "Unité",
                "taux": "4,00",
                "montant": "20,00",
            }
        ],
        "totals": {"subtotal": "20,00", "total": "20,00"},
    }


def test_manifest_checksums():
    for document in DOCUMENTS:
        pdf_path = FIXTURES / "pdfs" / document["filename"]
        assert (
            hashlib.sha256(pdf_path.read_bytes()).hexdigest() == document["sha256"]
        ), f"{document['filename']} a changé : re-valider la fixture et son SHA-256."


@pytest.mark.parametrize("document", DOCUMENTS, ids=lambda doc: doc["filename"])
def test_golden_corpus(document):
    expected = _load_expected(document)
    actual = extract_bdc_data(str(FIXTURES / "pdfs" / document["filename"]))

    for key in (
        "numero_bdc",
        "date_commande",
        "date_livraison",
        "reference",
        "references",
        "articles",
    ):
        assert actual[key] == expected[key], f"{document['filename']} : {key}"

    assert actual["totals"] == expected["totals"]
    assert actual["layout"]["fingerprint"] == expected["expected_layout_fingerprint"]
    assert actual["layout"]["expected"] == expected["expected_layout_fingerprint"]
    assert actual["layout"]["changed"] is False
    assert actual["status"] == expected["expected_status"]
    assert actual["warnings"] == expected["expected_warnings"]


@pytest.mark.parametrize("document", DOCUMENTS, ids=lambda doc: doc["filename"])
def test_golden_corpus_format_detail(document):
    """Le format simplifié reste inchangé ; le détaillé expose son annexe."""
    expected = _load_expected(document)
    actual = extract_bdc_data(str(FIXTURES / "pdfs" / document["filename"]))

    assert actual["bdc_format"] == expected.get("expected_bdc_format", "simplified")
    if actual["bdc_format"] == "detailed":
        assert actual["period"] == expected["expected_period"]
        assert actual["concerned_lots"] == expected["expected_concerned_lots"]
        assert len(actual["annex_videos"]) == expected["expected_annex_count"]
        assert actual["annex_summary"]["total_ht"] == expected["expected_annex_total_ht"]
    else:
        assert actual["period"] is None
        assert actual["concerned_lots"] == []
        assert actual["annex_videos"] == []


def test_detailed_bdc_annex_is_not_mixed_into_articles():
    """Les lignes de l'annexe ne doivent jamais polluer le tableau d'articles."""
    actual = extract_bdc_data(str(FIXTURES / "pdfs" / "BDC-M6-0001.pdf"))

    assert [article["description"] for article in actual["articles"]] == [
        "Recadrage - vidéo courte (moins de 2 min)",
        "Recadrage - vidéo standard (de 2 à 5 min)",
        "Recadrage - vidéo moyenne (de 5 à 8 min)",
    ]
    assert len(actual["annex_videos"]) == 134
    assert all(item["video_name"] for item in actual["annex_videos"])


def test_detailed_bdc_annex_reconstructs_wrapped_titles():
    actual = extract_bdc_data(str(FIXTURES / "pdfs" / "BDC-M6-0001.pdf"))

    names = {item["video_name"] for item in actual["annex_videos"]}
    assert "Cathy s'est fait courtiser par un agriculteur d'une autre saison !.mp4" in names
    assert "Gulli - MSL1 - Kondogbia co-animateur de « Ma Super Ligue 1 ».mp4" in names


def test_detailed_bdc_extracts_mov_and_duration_variants():
    actual = extract_bdc_data(str(FIXTURES / "pdfs" / "BDC-M6-0001.pdf"))
    by_lot = {}
    for item in actual["annex_videos"]:
        by_lot.setdefault(item["lot"], []).append(item)

    mov_names = sorted(
        item["video_name"] for item in actual["annex_videos"] if item["video_name"].endswith(".mov")
    )
    assert mov_names == [
        "LMCR2_RS BFC EXTRAIT 2.Copy.01.mov",
        "LMCR3 - EP26 CVDL 1 RS EXTRAIT 1.mov",
        "LMCR3 - EP26 CVDL 1 RS EXTRAIT 2.mov",
    ]
    lot_166 = by_lot["166"][0]
    assert lot_166["duration_sec"] == 7 * 60 + 41
    assert lot_166["category_label"] == "moyenne"


def test_detailed_bdc_ignores_page_numbers_and_summary_rows():
    actual = extract_bdc_data(str(FIXTURES / "pdfs" / "BDC-M6-0001.pdf"))

    lots = {item["lot"] for item in actual["annex_videos"]}
    assert "2" not in lots and "6" not in lots
    assert lots == set(actual["concerned_lots"])
    assert actual["annex_summary"]["total_videos"] == "134"
    assert actual["annex_declared_count"] == 134


def test_detailed_bdc_cross_checks_are_silent_when_coherent():
    actual = extract_bdc_data(str(FIXTURES / "pdfs" / "BDC-M6-0001.pdf"))

    codes = [issue["code"] for issue in actual["warnings"]]
    assert "annexe_volume_incoherent" not in codes
    assert "annexe_montant_incoherent" not in codes
    assert "annexe_categorie_incoherente" not in codes
    assert "lots_concernes_incoherents" not in codes


def test_validate_detailed_format_reports_volume_mismatch():
    data = {
        "numero_bdc": "BDC-M6-0001",
        "date_commande": "24/11/2026",
        "date_livraison": "24/11/2026",
        "reference": "LOTS 42 À 311",
        "references": ["LOTS 42 à 311"],
        "bdc_format": "detailed",
        "concerned_lots": ["42", "45"],
        "concerned_lots_declared_count": 3,
        "annex_declared_count": 4,
        "annex_videos": [
            {
                "lot": "42",
                "video_name": "a.mp4",
                "duration_sec": 30,
                "category_label": "courte",
                "montant": "4,00",
            }
        ],
        "articles": [
            {
                "description": "Recadrage - vidéo courte (moins de 2 min)",
                "quantite": "2,00",
                "taux": "4,00",
                "montant": "8,00",
            }
        ],
        "totals": {"subtotal": "8,00", "total": "8,00"},
    }

    codes = {issue["code"] for issue in validate_bdc_data(data)}
    assert "lots_concernes_incoherents" in codes
    assert "annexe_volume_incoherent" in codes
    assert "annexe_quantite_incoherente" in codes


@pytest.mark.parametrize(
    "numero",
    [
        "BDC-26-1882",
        "BDC-M6-0001",
        "BDC-W9-0001",
        "BDC-6TER-0001",
        "BDC-2026-M6-0001",
    ],
)
def test_bdc_number_pattern_accepts_variants(numero):
    from apps.bdc_converter.convert_bdc import BDC_NUMBER_PATTERN

    assert BDC_NUMBER_PATTERN.match(numero)


@pytest.mark.parametrize("numero", ["BDC-", "BDC-M6", "26-1882", "BDC-M6-"])
def test_bdc_number_pattern_rejects_invalid(numero):
    from apps.bdc_converter.convert_bdc import BDC_NUMBER_PATTERN

    assert not BDC_NUMBER_PATTERN.match(numero)


@pytest.mark.parametrize("document", DOCUMENTS, ids=lambda doc: doc["filename"])
def test_corpus_invariants(document):
    expected = _load_expected(document)
    amounts = []
    for article in expected["articles"]:
        quantite = _to_decimal(article["quantite"])
        taux = _to_decimal(article["taux"])
        montant = _to_decimal(article["montant"])
        assert quantite is not None
        assert taux is not None
        assert montant is not None
        assert abs(quantite * taux - montant) <= Decimal("0.01")
        amounts.append(montant)

    total = _to_decimal(expected["totals"]["total"])
    assert total is not None
    assert abs(sum(amounts, Decimal("0")) - total) <= Decimal("0.01")


@pytest.mark.parametrize("document", DOCUMENTS, ids=lambda doc: doc["filename"])
def test_word_based_extraction_matches_golden(document):
    expected = _load_expected(document)
    with pdfplumber.open(FIXTURES / "pdfs" / document["filename"]) as pdf:
        page = pdf.pages[0]
        articles = _extract_articles_from_words(page.extract_words(), page.width)

    assert articles == expected["articles"]


def test_extract_bdc_data_falls_back_to_table_extraction(monkeypatch):
    monkeypatch.setattr(
        "apps.bdc_converter.convert_bdc._extract_articles_from_words",
        lambda *_: [],
    )

    data = extract_bdc_data(FIXTURES / "pdfs" / "BDC-26-1689.pdf")

    assert len(data["articles"]) == 2
    assert data["articles"][0]["description"].startswith("Recadrage")


def test_extract_bdc_data_falls_back_to_table_totals(monkeypatch):
    monkeypatch.setattr(
        "apps.bdc_converter.convert_bdc._extract_totals_from_words",
        lambda *_: {"subtotal": None, "total": None},
    )

    data = extract_bdc_data(FIXTURES / "pdfs" / "BDC-26-1689.pdf")

    assert data["totals"] == {"subtotal": "116,00", "total": "116,00"}


def test_changed_layout_is_reported_as_info(monkeypatch):
    monkeypatch.setattr(
        "apps.bdc_converter.convert_bdc._extract_layout",
        lambda *_: {
            "fingerprint": "index | description | quantite | remise | taux | montant",
            "expected": "index | description | quantite | taux | montant",
            "changed": True,
            "columns": [],
        },
    )

    data = extract_bdc_data(FIXTURES / "pdfs" / "BDC-26-1689.pdf")

    assert data["layout"]["changed"] is True
    assert data["status"] == "ok"
    assert [issue["code"] for issue in data["warnings"]] == ["structure_modifiee"]
    assert data["warnings"][0]["severity"] == "info"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("01/10/2026", date(2026, 10, 1)),
        ("03/11/2026", date(2026, 11, 3)),
        ("29/02/2024", date(2024, 2, 29)),
    ],
)
def test_parse_french_date_accepts(raw, expected):
    assert parse_french_date(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "2026-01-10",
        "10/13/2026",
        "31/02/2026",
        "29/02/2026",
        "01/10/26",
        "1/10/2026",
        "",
        None,
    ],
)
def test_parse_french_date_rejects(raw):
    assert parse_french_date(raw) is None


def test_validate_ok():
    issues = validate_bdc_data(_valid_data())

    assert issues == []
    assert _status_from_issues(issues) == "ok"


def test_validate_without_articles_is_failed():
    data = _valid_data()
    data["articles"] = []

    issues = validate_bdc_data(data)

    assert [issue["code"] for issue in issues] == ["articles_absents"]
    assert _status_from_issues(issues) == "failed"


def test_validate_without_order_date_is_failed():
    data = _valid_data()
    data["date_commande"] = None

    issues = validate_bdc_data(data)

    assert "date_commande_invalide" in [issue["code"] for issue in issues]
    assert _status_from_issues(issues) == "failed"


def test_validate_invalid_bdc_number_is_failed():
    data = _valid_data()
    data["numero_bdc"] = "BC-26-1689"

    issues = validate_bdc_data(data)

    assert "numero_bdc_invalide" in [issue["code"] for issue in issues]
    assert _status_from_issues(issues) == "failed"


def test_validate_line_mismatch_is_partial():
    data = _valid_data()
    data["articles"][0]["montant"] = "21,00"
    data["totals"]["total"] = "21,00"

    issues = validate_bdc_data(data)

    assert "montant_incoherent" in [issue["code"] for issue in issues]
    assert _status_from_issues(issues) == "partial"


def test_validate_total_mismatch_is_partial():
    data = _valid_data()
    data["totals"]["total"] = "99,00"

    issues = validate_bdc_data(data)

    assert "total_incoherent" in [issue["code"] for issue in issues]
    assert _status_from_issues(issues) == "partial"


def test_validate_missing_reference_is_partial():
    data = _valid_data()
    data["reference"] = None

    issues = validate_bdc_data(data)

    assert "reference_absente" in [issue["code"] for issue in issues]
    assert _status_from_issues(issues) == "partial"


def test_validate_absent_delivery_date_is_informative_only():
    data = _valid_data()
    data["date_livraison"] = None

    issues = validate_bdc_data(data)

    assert "date_livraison_absente" in [issue["code"] for issue in issues]
    assert _status_from_issues(issues) == "ok"


def test_validate_invalid_delivery_date_is_partial():
    data = _valid_data()
    data["date_livraison"] = "2026-10-01"

    issues = validate_bdc_data(data)

    assert "date_livraison_invalide" in [issue["code"] for issue in issues]
    assert _status_from_issues(issues) == "partial"
