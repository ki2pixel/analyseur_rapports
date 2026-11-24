"""
Tests unitaires pour les transformations BDC -> MonthlyBatch.

Ces tests valident :
- L'inférence des catégories depuis les descriptions d'articles
- La transformation complète des données BDC en MonthlyBatch
- La robustesse aux données manquantes/invalides
"""

import json
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

from libs.pipeline.bdc_to_batch import (
    create_monthly_batch_from_bdc,
    create_monthly_batch_from_data,
    infer_category_from_description,
    parse_bdc_json_to_orders,
    parse_quantity,
)
from libs.schemas import VideoCategory


class TestCategoryInference:
    """Tests pour l'inférence de catégories depuis les descriptions."""

    def test_infer_short_categories(self):
        """Teste l'inférence de la catégorie SHORT."""
        test_cases = [
            "Recadrage - vidéo courte (moins de 2 min)",
            "vidéo courte",
            "short video editing",
            "montage vidéo court",
        ]
        for desc in test_cases:
            assert infer_category_from_description(desc) == VideoCategory.SHORT

    def test_infer_standard_categories(self):
        """Teste l'inférence de la catégorie STANDARD."""
        test_cases = [
            "Recadrage - vidéo standard (entre 3 et 5 minutes)",
            "vidéo standard",
            "standard video work",
        ]
        for desc in test_cases:
            assert infer_category_from_description(desc) == VideoCategory.STANDARD

    def test_infer_medium_categories(self):
        """Teste l'inférence de la catégorie MEDIUM."""
        test_cases = [
            "Recadrage - vidéo moyenne (entre 5 et 7 minutes)",
            "vidéo moyenne",
            "montage moyen",
        ]
        for desc in test_cases:
            assert infer_category_from_description(desc) == VideoCategory.MEDIUM

    def test_infer_long_has_priority_over_standard(self):
        """Une description contenant plusieurs indices doit retenir LONG en premier."""
        assert (
            infer_category_from_description("vidéo standard mais plus de 8 minutes")
            == VideoCategory.LONG
        )
        assert infer_category_from_description("plus de 8 minutes") == VideoCategory.LONG

    def test_infer_long_categories(self):
        """Teste l'inférence de la catégorie LONG."""
        test_cases = [
            "Recadrage - vidéo longue (plus de 7 minutes)",
            "long video editing",
            "vidéo longue",
        ]
        for desc in test_cases:
            assert infer_category_from_description(desc) == VideoCategory.LONG

    def test_infer_unknown_categories(self):
        """Teste les cas où la catégorie reste UNKNOWN."""
        test_cases = [
            "Service de consulting vidéo",
            "Formation montage",
            "",  # Vide
            "Vidéo sans précision de durée",
        ]
        for desc in test_cases:
            assert infer_category_from_description(desc) == VideoCategory.UNKNOWN


class TestQuantityParsing:
    """Tests pour le parsing des quantités BDC au format français."""

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("9,00", 9),
            ("27,00", 27),
            ("2,999", 3),
            ("1\u00a0234,50", 1235),
            ("1 234,50", 1235),
            ("  4,00  ", 4),
            ("0,00", 0),
            ("0", 0),
        ],
    )
    def test_parse_french_quantities(self, raw, expected):
        assert parse_quantity(raw) == expected

    @pytest.mark.parametrize("raw", [None, "", "   ", "inconnu"])
    def test_parse_invalid_quantities_returns_zero(self, raw):
        assert parse_quantity(raw) == 0

    def test_parse_numeric_values(self):
        assert parse_quantity(3) == 3
        assert parse_quantity(2.6) == 3
        assert parse_quantity(0) == 0


class TestBDCTransformation:
    """Tests pour la transformation des données BDC."""

    def test_parse_valid_bdc_data(self):
        """Teste le parsing de données BDC valides."""
        bdc_data = [
            {
                "numero_bdc": "BDC-25-1719",
                "date_commande": "01/12/2025",
                "reference": "LOTS 14/4/8",
                "references": ["LOTS 14", "4", "8"],
                "articles": [
                    {
                        "description": "Recadrage - vidéo courte (moins de 2 min)",
                        "quantite": "6,00",
                        "unite": "Unité",
                        "taux": "4,00",
                        "montant": "24,00",
                    },
                    {
                        "description": "Recadrage - vidéo standard (entre 3 et 5 minutes)",
                        "quantite": "3,00",
                        "unite": "Unité",
                        "taux": "8,00",
                        "montant": "24,00",
                    },
                ],
            }
        ]

        orders = parse_bdc_json_to_orders(bdc_data)

        assert len(orders) == 1
        order = orders[0]
        assert order.numero_bdc == "BDC-25-1719"
        assert order.date_commande == "01/12/2025"
        assert order.reference == "LOTS 14/4/8"
        assert order.references == ["LOTS 14", "4", "8"]
        assert len(order.articles) == 2

        # Vérifier les catégories inférées
        assert order.articles[0].inferred_category == VideoCategory.SHORT
        assert order.articles[1].inferred_category == VideoCategory.STANDARD

        # Vérifier les autres champs
        assert order.articles[0].quantite == "6,00"
        assert order.articles[0].montant == "24,00"

    def test_parse_bdc_with_missing_fields(self):
        """Teste le parsing de données BDC avec champs optionnels manquants."""
        bdc_data = [
            {
                "numero_bdc": "BDC-25-TEST",
                "articles": [
                    {
                        "description": "Service inconnu",
                        "quantite": "1,00",
                        "unite": "Unité",
                        # taux et montant manquants
                    }
                ],
                # dates et références manquantes
            }
        ]

        orders = parse_bdc_json_to_orders(bdc_data)

        assert len(orders) == 1
        order = orders[0]
        assert order.numero_bdc == "BDC-25-TEST"
        assert order.date_commande is None
        assert order.reference is None
        assert order.references == []

        article = order.articles[0]
        assert article.description == "Service inconnu"
        assert article.quantite == "1,00"
        assert article.taux is None
        assert article.montant is None
        assert article.inferred_category == VideoCategory.UNKNOWN

    def test_parse_empty_articles(self):
        """Teste le parsing d'une commande sans articles."""
        bdc_data = [{"numero_bdc": "BDC-25-EMPTY", "articles": []}]

        orders = parse_bdc_json_to_orders(bdc_data)

        assert len(orders) == 1
        assert orders[0].numero_bdc == "BDC-25-EMPTY"
        assert orders[0].articles == []

    def test_parse_bdc_missing_numero_bdc_graceful_ignore(self):
        """Teste que les commandes sans numéro BDC sont ignorées sans faire planter tout le lot."""
        bdc_data = [
            {"numero_bdc": "BDC-25-VALID", "articles": []},
            {"numero_bdc": None, "articles": []},  # Invalide
            {
                # Missing 'numero_bdc' entirely
                "articles": []
            },
            {"numero_bdc": "BDC-25-VALID-2", "articles": []},
        ]

        orders = parse_bdc_json_to_orders(bdc_data)

        # Seules les 2 commandes valides doivent être renvoyées
        assert len(orders) == 2
        assert orders[0].numero_bdc == "BDC-25-VALID"
        assert orders[1].numero_bdc == "BDC-25-VALID-2"


class TestMonthlyBatchCreation:
    """Tests pour la création de MonthlyBatch depuis fichiers JSON."""

    def test_create_batch_from_valid_file(self):
        """Teste la création d'un MonthlyBatch depuis un fichier valide."""
        bdc_data = [
            {
                "numero_bdc": "BDC-25-1719",
                "articles": [
                    {
                        "description": "Recadrage - vidéo courte (moins de 2 min)",
                        "quantite": "6,00",
                        "unite": "Unité",
                        "taux": "4,00",
                        "montant": "24,00",
                    }
                ],
            }
        ]

        # Créer un fichier temporaire
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(bdc_data, f)
            temp_file = f.name

        try:
            batch = create_monthly_batch_from_bdc(temp_file)

            # Vérifications de base
            assert batch.batch_id is not None  # Batch ID doit être défini
            assert len(batch.orders) == 1
            assert len(batch.source_files) == 1

            # Vérifier le fichier source
            source = batch.source_files[0]
            assert source.name.endswith(".json")
            assert source.file_type == "json"
            assert source.status == "processed"
            assert isinstance(source.processed_at, datetime)

        finally:
            Path(temp_file).unlink()

    def test_create_batch_with_custom_batch_id(self):
        """Teste la création avec un batch_id personnalisé."""
        bdc_data = [{"numero_bdc": "BDC-25-TEST", "articles": []}]

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(bdc_data, f)
            temp_file = f.name

        try:
            batch = create_monthly_batch_from_bdc(temp_file, batch_id="2025-12")
            assert batch.batch_id == "2025-12"
        finally:
            Path(temp_file).unlink()

    def test_batch_id_inference_from_filename(self):
        """Teste l'inférence du batch_id depuis le nom du fichier."""
        bdc_data = [{"numero_bdc": "BDC-25-TEST", "articles": []}]

        # Tester différents noms de fichiers
        test_cases = [
            ("bdc_decembre_2025.json", "2025-12"),
            ("bdc_janvier_2024.json", "2024-01"),
            ("bdc_mars_2023.json", "2023-03"),
            ("convert_bdc_10-2026.json", "2026-10"),
            ("bdc_10_2026.json", "2026-10"),
            ("bdc_2026-10.json", "2026-10"),
            ("random_name.json", None),  # Doit utiliser la date actuelle
        ]

        for filename, expected_month in test_cases:
            # Créer un fichier temporaire avec le nom spécifique
            temp_dir = Path(tempfile.gettempdir())
            temp_file = temp_dir / filename

            try:
                with open(temp_file, "w") as f:
                    json.dump(bdc_data, f)

                batch = create_monthly_batch_from_bdc(str(temp_file))

                if expected_month:
                    assert batch.batch_id == expected_month
                else:
                    # Pour le cas sans pattern, on vérifie juste que ça ne crash pas
                    assert batch.batch_id is not None
            finally:
                if temp_file.exists():
                    temp_file.unlink()


class TestSyntheticEntries:
    def test_multi_lot_order_uses_full_reference_and_empty_person(self):
        bdc_data = [
            {
                "numero_bdc": "BDC-1",
                "reference": "LOTS 2/7/10",
                "references": ["LOTS 2", "7", "10"],
                "articles": [{"description": "vidéo courte", "quantite": "2"}],
            }
        ]

        batch = create_monthly_batch_from_data(bdc_data, batch_id="2026-10")

        assert len(batch.video_entries) == 2
        assert {entry.id_camille for entry in batch.video_entries} == {"LOTS 2/7/10"}
        assert {entry.person for entry in batch.video_entries} == {""}
        assert all(entry.source_bdc == "BDC-1" for entry in batch.video_entries)

    def test_unknown_articles_are_not_converted_to_standard(self):
        bdc_data = [
            {
                "numero_bdc": "BDC-UNK",
                "reference": "LOT 1",
                "references": ["LOT 1"],
                "articles": [
                    {"description": "Prestation diverse", "quantite": "1"},
                    {"description": "vidéo courte", "quantite": "0,00"},
                ],
            }
        ]

        batch = create_monthly_batch_from_data(bdc_data, batch_id="2026-10")

        assert batch.video_entries == []
        assert any("non catégorisé" in warning for warning in batch.warnings)
        assert any("quantité nulle" in warning for warning in batch.warnings)


class TestExtractionStatus:
    def test_failed_documents_are_excluded_and_warned(self):
        bdc_data = [
            {
                "numero_bdc": "BDC-OK",
                "reference": "LOT 1",
                "references": ["LOT 1"],
                "articles": [{"description": "vidéo courte", "quantite": "2"}],
                "status": "ok",
            },
            {
                "numero_bdc": None,
                "reference": None,
                "references": [],
                "articles": [],
                "status": "failed",
            },
        ]

        batch = create_monthly_batch_from_data(bdc_data, batch_id="2026-10")

        assert [order.numero_bdc for order in batch.orders] == ["BDC-OK"]
        assert len(batch.video_entries) == 2
        assert any("échec d'extraction" in warning for warning in batch.warnings)

    def test_empty_orders_are_warned(self):
        bdc_data = [
            {
                "numero_bdc": "BDC-EMPTY",
                "reference": "LOT 1",
                "references": ["LOT 1"],
                "articles": [],
            }
        ]

        batch = create_monthly_batch_from_data(bdc_data, batch_id="2026-10")

        assert batch.video_entries == []
        assert any("sans article" in warning for warning in batch.warnings)


class TestDetailedFormat:
    """Nouveau format détaillé : lots explicites et annexe vidéo."""

    @pytest.mark.parametrize(
        "description,expected",
        [
            ("Recadrage - vidéo moyenne (de 5 à 8 min)", VideoCategory.MEDIUM),
            ("Recadrage - vidéo moyenne (entre 5 et 8 minutes)", VideoCategory.MEDIUM),
            ("Recadrage - vidéo standard (de 2 à 5 min)", VideoCategory.STANDARD),
            ("Recadrage - vidéo longue (8 min et plus)", VideoCategory.LONG),
            ("Recadrage - vidéo courte (moins de 2 min)", VideoCategory.SHORT),
        ],
    )
    def test_infer_new_bdc_labels(self, description, expected):
        assert infer_category_from_description(description) == expected

    def _detailed_bdc(self):
        return [
            {
                "numero_bdc": "BDC-M6-0001",
                "date_commande": "24/11/2026",
                "date_livraison": "24/11/2026",
                "reference": "LOTS 42 À 311",
                "references": ["LOTS 42 à 311"],
                "bdc_format": "detailed",
                "period": "Lots 42 et + · 11/2026",
                "concerned_lots": ["42", "45"],
                "articles": [
                    {
                        "description": "Recadrage - vidéo courte (moins de 2 min)",
                        "quantite": "2,00",
                        "taux": "4,00",
                        "montant": "8,00",
                    }
                ],
                "annex_videos": [
                    {
                        "lot": "42",
                        "video_name": "a.mp4",
                        "duration_text": "0 min 18",
                        "duration_sec": 18,
                        "category_label": "courte",
                        "montant": "4,00",
                    },
                    {
                        "lot": "45",
                        "video_name": "b.mp4",
                        "duration_text": "1 min 20",
                        "duration_sec": 80,
                        "category_label": "courte",
                        "montant": "4,00",
                    },
                ],
                "totals": {"subtotal": "8,00", "total": "8,00"},
                "status": "ok",
            }
        ]

    def test_parse_hydrates_detailed_fields(self):
        orders = parse_bdc_json_to_orders(self._detailed_bdc())

        assert len(orders) == 1
        order = orders[0]
        assert order.bdc_format == "detailed"
        assert order.period == "Lots 42 et + · 11/2026"
        assert order.concerned_lots == ["42", "45"]
        assert len(order.annex_videos) == 2
        assert order.annex_videos[0].video_name == "a.mp4"
        assert order.annex_videos[0].inferred_category == VideoCategory.SHORT

    def test_detailed_batch_uses_annex_entries(self):
        batch = create_monthly_batch_from_data(self._detailed_bdc(), batch_id="2026-11")

        assert len(batch.video_entries) == 2
        entry = batch.video_entries[0]
        assert entry.id_camille == "42"
        assert entry.video_name == "a.mp4"
        assert entry.duration_sec == 18
        assert entry.duration_source == "bdc_annex"
        assert entry.month == "2026-11"
        assert any("détaillé" in warning for warning in batch.warnings)

    def test_detailed_format_detected_from_concerned_lots(self):
        data = self._detailed_bdc()
        data[0].pop("bdc_format")
        data[0].pop("annex_videos")

        orders = parse_bdc_json_to_orders(data)

        assert orders[0].bdc_format == "detailed"

    def test_annex_entry_without_duration_uses_category(self):
        data = self._detailed_bdc()
        data[0]["annex_videos"] = [
            {
                "lot": "42",
                "video_name": "a.mp4",
                "category_label": "standard",
                "montant": "8,00",
            }
        ]

        batch = create_monthly_batch_from_data(data, batch_id="2026-11")

        assert batch.video_entries[0].duration_source == "bdc_annex_category"
        assert batch.video_entries[0].duration_sec == 240
        assert any("sans durée explicite" in warning for warning in batch.warnings)

    def test_simplified_order_still_replicates_articles(self):
        bdc_data = [
            {
                "numero_bdc": "BDC-26-1882",
                "date_commande": "03/11/2026",
                "date_livraison": "03/11/2026",
                "reference": "LOTS 2/8",
                "references": ["LOTS 2", "8"],
                "articles": [
                    {
                        "description": "Recadrage - vidéo courte (moins de 2 min)",
                        "quantite": "3,00",
                        "taux": "4,00",
                        "montant": "12,00",
                    }
                ],
                "totals": {"subtotal": "12,00", "total": "12,00"},
            }
        ]

        batch = create_monthly_batch_from_data(bdc_data, batch_id="2026-11")

        assert len(batch.video_entries) == 3
        assert batch.video_entries[0].duration_source == "bdc_inferred"
        assert batch.orders[0].bdc_format == "simplified"


if __name__ == "__main__":
    pytest.main([__file__])
