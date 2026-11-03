"""Tests de l'extraction par coordonnées (P1) : colonnes, lignes, unités."""

import pytest

from apps.bdc_converter.convert_bdc import (
    EXPECTED_LAYOUT_FINGERPRINT,
    _extract_articles_from_words,
    _extract_layout,
    _extract_metadata_from_words,
    _extract_reference,
    _extract_totals_from_words,
)


def _word(text, x0, x1, top):
    return {"text": text, "x0": x0, "x1": x1, "top": top}


def _header(top=100, columns=None):
    columns = columns or [
        ("#", 50, 56),
        ("Description", 70, 130),
        ("Quantité", 300, 340),
        ("Taux", 380, 410),
        ("Montant", 480, 520),
    ]
    return [_word(text, x0, x1, top) for text, x0, x1 in columns]


def _article(top, index, description, quantity, rate, amount, description_x=70):
    words = [
        _word(str(index), 50, 56, top),
        _word(description, description_x, description_x + 60, top),
        _word(quantity, 310, 330, top),
        _word(rate, 385, 405, top),
        _word(amount, 490, 510, top),
    ]
    return words


def test_borderless_table_uses_header_coordinates():
    words = _header()
    words += _article(130, 1, "Recadrage courte", "5,00", "4,00", "20,00")
    words.append(_word("Unité", 310, 330, 142))
    words += _article(170, 2, "Recadrage standard", "12,00", "8,00", "96,00")
    words.append(_word("Unité", 310, 330, 182))
    words += [
        _word("Sous-total", 300, 350, 210),
        _word("116,00", 490, 510, 210),
    ]

    articles = _extract_articles_from_words(words, page_width=600)

    assert articles == [
        {
            "description": "Recadrage courte",
            "quantite": "5,00",
            "unite": "Unité",
            "taux": "4,00",
            "montant": "20,00",
        },
        {
            "description": "Recadrage standard",
            "quantite": "12,00",
            "unite": "Unité",
            "taux": "8,00",
            "montant": "96,00",
        },
    ]


def test_columns_can_be_reordered():
    words = _header(
        columns=[
            ("Description", 70, 130),
            ("Montant", 300, 350),
            ("Quantité", 380, 420),
            ("Taux", 480, 510),
        ]
    )
    words += [
        _word("1", 50, 56, 130),
        _word("Recadrage", 70, 130, 130),
        _word("20,00", 310, 330, 130),
        _word("5,00", 390, 410, 130),
        _word("4,00", 490, 510, 130),
    ]

    articles = _extract_articles_from_words(words, page_width=600)

    assert articles == [
        {
            "description": "Recadrage",
            "quantite": "5,00",
            "unite": "",
            "taux": "4,00",
            "montant": "20,00",
        }
    ]


def test_header_synonyms_and_multiword_labels():
    words = _header(
        columns=[
            ("Désignation", 70, 140),
            ("Qté", 300, 320),
            ("Prix", 380, 400),
            ("unitaire", 402, 440),
            ("Montant HT", 480, 540),
        ]
    )
    words += [
        _word("1", 50, 56, 130),
        _word("Recadrage", 70, 130, 130),
        _word("5,00", 310, 330, 130),
        _word("4,00", 385, 405, 130),
        _word("20,00", 490, 510, 130),
    ]

    articles = _extract_articles_from_words(words, page_width=600)

    assert articles == [
        {
            "description": "Recadrage",
            "quantite": "5,00",
            "unite": "",
            "taux": "4,00",
            "montant": "20,00",
        }
    ]


def test_unknown_column_is_ignored():
    words = _header(
        columns=[
            ("Description", 70, 130),
            ("Quantité", 300, 340),
            ("Remise", 380, 420),
            ("Taux", 450, 480),
            ("Montant", 520, 560),
        ]
    )
    words += [
        _word("1", 50, 56, 130),
        _word("Recadrage", 70, 130, 130),
        _word("5,00", 310, 330, 130),
        _word("1,00", 390, 410, 130),
        _word("4,00", 460, 470, 130),
        _word("20,00", 530, 550, 130),
    ]

    articles = _extract_articles_from_words(words, page_width=600)

    assert articles[0]["quantite"] == "5,00"
    assert articles[0]["taux"] == "4,00"
    assert articles[0]["montant"] == "20,00"


def test_separate_unit_column_is_attached():
    words = _header(
        columns=[
            ("Description", 70, 130),
            ("Quantité", 300, 340),
            ("Unité", 360, 400),
            ("Taux", 430, 460),
            ("Montant", 500, 540),
        ]
    )
    words += [
        _word("1", 50, 56, 130),
        _word("Recadrage", 70, 130, 130),
        _word("5,00", 310, 330, 130),
        _word("Unité", 365, 395, 130),
        _word("4,00", 435, 455, 130),
        _word("20,00", 505, 525, 130),
    ]

    articles = _extract_articles_from_words(words, page_width=600)

    assert articles[0]["quantite"] == "5,00"
    assert articles[0]["unite"] == "Unité"


def test_multiline_description_is_merged():
    words = _header()
    words += _article(130, 1, "Recadrage", "5,00", "4,00", "20,00")
    words += [
        _word("standard", 70, 120, 145),
        _word("(entre", 125, 150, 145),
        _word("3", 155, 160, 145),
        _word("et", 162, 170, 145),
        _word("5", 172, 177, 145),
        _word("minutes)", 180, 220, 145),
    ]

    articles = _extract_articles_from_words(words, page_width=600)

    assert articles[0]["description"] == "Recadrage standard (entre 3 et 5 minutes)"


def test_unit_in_description_column_is_attached():
    words = _header()
    words += _article(130, 1, "Recadrage", "5,00", "4,00", "20,00")
    words.append(_word("Unité", 70, 100, 145))

    articles = _extract_articles_from_words(words, page_width=600)

    assert articles[0]["description"] == "Recadrage"
    assert articles[0]["unite"] == "Unité"


def test_footer_without_total_does_not_pollute_description():
    words = _header()
    words += _article(130, 1, "Recadrage", "5,00", "4,00", "20,00")
    words += [
        _word("Adresse", 50, 90, 250),
        _word("de", 92, 105, 250),
        _word("livraison", 107, 150, 250),
    ]

    articles = _extract_articles_from_words(words, page_width=600)

    assert articles[0]["description"] == "Recadrage"


def test_repeated_header_is_skipped():
    words = _header()
    words += _article(130, 1, "Recadrage", "5,00", "4,00", "20,00")
    words += _header(top=170)
    words += _article(200, 2, "Recadrage standard", "12,00", "8,00", "96,00")
    words += [
        _word("Sous-total", 300, 350, 230),
        _word("116,00", 490, 510, 230),
    ]

    articles = _extract_articles_from_words(words, page_width=600)

    assert [article["description"] for article in articles] == [
        "Recadrage",
        "Recadrage standard",
    ]


def test_without_header_returns_no_articles():
    words = _article(130, 1, "Recadrage", "5,00", "4,00", "20,00")

    assert _extract_articles_from_words(words, page_width=600) == []


def test_description_starting_with_total_is_not_a_total_line():
    words = _header()
    words += _article(130, 1, "Total look", "2,00", "4,00", "8,00")
    words += [
        _word("Sous-total", 300, 350, 160),
        _word("8,00", 490, 510, 160),
    ]

    articles = _extract_articles_from_words(words, page_width=600)

    assert len(articles) == 1
    assert articles[0]["description"] == "Total look"
    assert articles[0]["montant"] == "8,00"


def test_totals_ignore_free_text_note():
    words = [
        _word("Sous-total", 300, 350, 100),
        _word("1", 490, 495, 100),
        _word("100,00", 500, 530, 100),
        _word("Total", 300, 330, 120),
        _word("€1", 480, 495, 120),
        _word("100,00", 500, 530, 120),
        _word("Total", 300, 330, 140),
        _word("+44", 480, 500, 140),
        _word("€", 505, 515, 140),
    ]

    assert _extract_totals_from_words(words) == {
        "subtotal": "1 100,00",
        "total": "1 100,00",
    }


def test_metadata_fallback_uses_dates_next_to_labels():
    words = [
        _word("Bon", 50, 70, 50),
        _word("de", 72, 85, 50),
        _word("commande", 87, 130, 50),
        _word("BDC-26-1689", 135, 200, 50),
        _word("Date", 50, 70, 70),
        _word(":", 72, 76, 70),
        _word("01/10/2026", 170, 220, 70),
        _word("Date", 50, 70, 90),
        _word("de", 72, 85, 90),
        _word("livraison", 87, 130, 90),
        _word(":", 132, 136, 90),
        _word("03/11/2026", 170, 220, 90),
    ]

    metadata = _extract_metadata_from_words(words)

    assert metadata == {
        "numero_bdc": "BDC-26-1689",
        "date_commande": "01/10/2026",
        "date_livraison": "03/11/2026",
    }


def test_metadata_fallback_does_not_treat_delivery_date_as_order_date():
    words = [
        _word("Date", 50, 70, 70),
        _word("livraison", 72, 120, 70),
        _word(":", 122, 126, 70),
        _word("03/11/2026", 170, 220, 70),
    ]

    metadata = _extract_metadata_from_words(words)

    assert metadata["date_commande"] is None
    assert metadata["date_livraison"] == "03/11/2026"


@pytest.mark.parametrize("label", ["Référence", "Réf.", "N° de réf"])
def test_reference_label_synonyms(label):
    value, parts = _extract_reference(f"{label} : LOTS 1/2\nDate : 01/10/2026")

    assert value == "LOTS 1/2"
    assert parts == ["LOTS 1", "2"]


def test_layout_fingerprint_matches_reference():
    layout = _extract_layout(_header(), page_width=600)

    assert layout["fingerprint"] == EXPECTED_LAYOUT_FINGERPRINT
    assert layout["expected"] == EXPECTED_LAYOUT_FINGERPRINT
    assert layout["changed"] is False
    assert [column["name"] for column in layout["columns"]] == [
        "index",
        "description",
        "quantite",
        "taux",
        "montant",
    ]


def test_layout_fingerprint_accepts_header_synonyms():
    words = _header(
        columns=[
            ("#", 50, 56),
            ("Désignation", 70, 140),
            ("Qté", 300, 320),
            ("Prix", 380, 400),
            ("unitaire", 402, 440),
            ("Montant HT", 480, 540),
        ]
    )

    layout = _extract_layout(words, page_width=600)

    assert layout["fingerprint"] == EXPECTED_LAYOUT_FINGERPRINT
    assert layout["changed"] is False


def test_layout_fingerprint_flags_unknown_column():
    words = _header(
        columns=[
            ("#", 50, 56),
            ("Description", 70, 130),
            ("Quantité", 300, 340),
            ("Remise", 380, 420),
            ("Taux", 450, 480),
            ("Montant", 520, 560),
        ]
    )

    layout = _extract_layout(words, page_width=600)

    assert layout["fingerprint"] == "index | description | quantite | remise | taux | montant"
    assert layout["changed"] is True


def test_layout_fingerprint_flags_column_removal():
    words = _header(
        columns=[
            ("Description", 70, 130),
            ("Quantité", 300, 340),
            ("Taux", 450, 480),
            ("Montant", 520, 560),
        ]
    )

    layout = _extract_layout(words, page_width=600)

    assert layout["fingerprint"] == "description | quantite | taux | montant"
    assert layout["changed"] is True


def test_layout_fingerprint_flags_missing_header():
    words = _article(130, 1, "Recadrage", "5,00", "4,00", "20,00")

    layout = _extract_layout(words, page_width=600)

    assert layout["fingerprint"] is None
    assert layout["columns"] == []
    assert layout["changed"] is True
