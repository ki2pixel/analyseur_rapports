"""Tests de normalisation des références de lots extraites des PDF BDC."""

import pytest

from apps.bdc_converter.convert_bdc import (
    _extract_reference,
    _looks_like_numeric,
    _row_is_total,
    _split_quantity_and_unit,
)


@pytest.mark.parametrize(
    "reference,expected",
    [
        ("LOTS 38-92", "LOTS 38 à 92"),
        ("LOTS 38 à 92", "LOTS 38 à 92"),
        ("LOTS 38 au 92", "LOTS 38 à 92"),
        ("38-92", "LOTS 38 à 92"),
        ("LOT 21 - 56", "LOTS 21 à 56"),
        ("LOTS 1/38-42", "LOTS 1/38 à 42"),
    ],
)
def test_extract_reference_normalizes_ranges(reference, expected):
    value, parts = _extract_reference(f"N° réf : {reference}\nDate : 01/09/2026")

    assert value == expected
    assert parts == [expected]


def test_extract_reference_preserves_explicit_lot_list():
    value, parts = _extract_reference("N° réf : LOTS 1/4/8\nDate : 01/09/2026")

    assert value == "LOTS 1/4/8"
    assert parts == ["LOTS 1", "4", "8"]


def test_extract_reference_splits_on_et():
    value, parts = _extract_reference("N° réf : LOTS 1 et 4\nDate : 01/09/2026")

    assert value == "LOTS 1 et 4"
    assert parts == ["LOTS 1", "4"]


def test_extract_reference_with_phone_number_adjacent_to_lot_number():
    text = (
        "LOTS 286/287/291/293/295 camille.moine.pro@gmail.com\n"
        "N° de réf :\n"
        "/298/300/303 07 61 53 28 61\n"
        "Date : 26/10/2026"
    )
    value, parts = _extract_reference(text)
    assert value == "LOTS 286/287/291/293/295/298/300/303"
    assert parts == ["LOTS 286", "287", "291", "293", "295", "298", "300", "303"]


@pytest.mark.parametrize(
    "text",
    [
        "N° de réf : LOTS 18/19/\n20/23\nDate : 01/09/2026",
        "LOTS 18/19/\nN° de réf :\n20/23\nDate : 01/09/2026",
        "LOTS 18/19\nN° de réf :\n20/23\nDate : 01/09/2026",
    ],
)
def test_extract_reference_joins_lot_list_wrapped_around_label(text):
    value, parts = _extract_reference(text)

    assert value == "LOTS 18/19/20/23"
    assert parts == ["LOTS 18", "19", "20", "23"]


def test_extract_reference_keeps_mixed_range_and_lot():
    value, parts = _extract_reference("N° de réf : LOTS 41-50/55\nDate : 01/09/2026")

    assert value == "LOTS 41 à 50/55"
    assert parts == ["LOTS 41 à 50/55"]


@pytest.mark.parametrize(
    "value,expected",
    [
        ("1 056,00", ("1 056,00", "")),
        ("1 234,50 €", ("1 234,50", "€")),
        ("125,00 Unité", ("125,00", "Unité")),
        ("2 Vidéo courte", ("2", "Vidéo courte")),
        ("9,00", ("9,00", "")),
    ],
)
def test_split_quantity_and_unit(value, expected):
    assert _split_quantity_and_unit(value) == expected


@pytest.mark.parametrize(
    "value",
    ["1 056,00", "1 056,00 €", "125,00 Unité", "8,00"],
)
def test_numeric_pattern_accepts_thousands(value):
    assert _looks_like_numeric(value) is True


@pytest.mark.parametrize("value", ["2026-10", "01/09/2026", "BDC-26-1856"])
def test_numeric_pattern_rejects_identifiers(value):
    assert _looks_like_numeric(value) is False


def test_total_row_does_not_match_article_names():
    assert _row_is_total(["1", "Totally Spies", "2", "8,00", "16,00"]) is False
    assert _row_is_total(["Sous-total", "", "", "", "16,00"]) is True
    assert _row_is_total(["Total", "", "", "", "16,00"]) is True
