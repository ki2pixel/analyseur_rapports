"""
Tests pour la fonction analyze_batch qui analyse un MonthlyBatch.
"""

import pytest
from datetime import datetime
from pathlib import Path

from apps.reporting.analyser import analyze_batch, CategoryThresholds
from libs.schemas import MonthlyBatch, VideoGroupReport, VideoEntry, VideoCategory


class TestAnalyzeBatch:
    """Tests pour la fonction analyze_batch."""

    def test_analyze_empty_batch(self):
        """Teste l'analyse d'un batch vide."""
        batch = MonthlyBatch(batch_id="2025-12", generated_at=datetime.now())
        thresholds = CategoryThresholds()

        reports = analyze_batch(batch, thresholds)

        assert reports == []

    def test_analyze_batch_single_group(self):
        """Teste l'analyse d'un batch avec un seul groupe vidéo."""
        # Créer des video_entries fictives
        video_entries = [
            VideoEntry(
                video_id="vid1",
                id_camille="273",
                person="Camille",
                month="2025-12",
                video_name="video1.mp4",
                video_path="/fake/path/video1.mp4",
                duration_sec=90,  # < 120 -> short
                duration_source="test",
                archive_folder="/fake/archive",
            ),
            VideoEntry(
                video_id="vid2",
                id_camille="273",
                person="Camille",
                month="2025-12",
                video_name="video2.mp4",
                video_path="/fake/path/video2.mp4",
                duration_sec=240,  # 240 < 300 -> standard
                duration_source="test",
                archive_folder="/fake/archive",
            ),
            VideoEntry(
                video_id="vid3",
                id_camille="273",
                person="Camille",
                month="2025-12",
                video_name="video3.mp4",
                video_path="/fake/path/video3.mp4",
                duration_sec=400,  # 400 < 480 -> medium
                duration_source="test",
                archive_folder="/fake/archive",
            ),
        ]

        batch = MonthlyBatch(
            batch_id="2025-12", generated_at=datetime.now(), video_entries=video_entries
        )
        thresholds = CategoryThresholds(
            short_lt=120,  # < 2 min
            standard_lt=300,  # < 5 min
            medium_lt=480,  # < 8 min
            long_ge=480,  # >= 8 min
        )

        reports = analyze_batch(batch, thresholds)

        # Vérifications
        assert len(reports) == 1
        report = reports[0]
        assert isinstance(report, VideoGroupReport)
        assert report.id_camille == "273"
        assert report.person == "Camille"
        assert report.month == "2025-12"

        # Vérifier les catégories
        assert report.categories[VideoCategory.SHORT].count == 1
        assert report.categories[VideoCategory.SHORT].files == ["video1.mp4"]

        assert report.categories[VideoCategory.STANDARD].count == 1
        assert report.categories[VideoCategory.STANDARD].files == ["video2.mp4"]

        assert report.categories[VideoCategory.MEDIUM].count == 1
        assert report.categories[VideoCategory.MEDIUM].files == ["video3.mp4"]

        assert report.categories[VideoCategory.LONG].count == 0

        # Vérifier les totaux
        assert report.totals["total_mp4"] == 3

    def test_analyze_batch_multiple_groups(self):
        """Teste l'analyse d'un batch avec plusieurs groupes."""
        video_entries = [
            # Groupe 1
            VideoEntry(
                video_id="1",
                id_camille="273",
                person="Camille",
                month="2025-12",
                video_name="vid1.mp4",
                video_path="/fake/vid1.mp4",
                duration_sec=60,  # short
                duration_source="test",
                archive_folder="/fake",
            ),
            # Groupe 2
            VideoEntry(
                video_id="2",
                id_camille="456",
                person="Dupont",
                month="2025-12",
                video_name="vid2.mp4",
                video_path="/fake/vid2.mp4",
                duration_sec=180,  # standard
                duration_source="test",
                archive_folder="/fake",
            ),
            VideoEntry(
                video_id="3",
                id_camille="456",
                person="Dupont",
                month="2025-12",
                video_name="vid3.mp4",
                video_path="/fake/vid3.mp4",
                duration_sec=500,  # > 480 -> long
                duration_source="test",
                archive_folder="/fake",
            ),
        ]

        batch = MonthlyBatch(
            batch_id="2025-12", generated_at=datetime.now(), video_entries=video_entries
        )
        thresholds = CategoryThresholds()

        reports = analyze_batch(batch, thresholds)

        # Vérifications
        assert len(reports) == 2

        # Trier par id_camille pour prédire l'ordre
        reports.sort(key=lambda r: r.id_camille)

        # Premier rapport (273 Camille)
        assert reports[0].id_camille == "273"
        assert reports[0].categories[VideoCategory.SHORT].count == 1
        assert reports[0].categories[VideoCategory.SHORT].files == ["vid1.mp4"]
        assert reports[0].totals["total_mp4"] == 1

        # Second rapport (456 Dupont)
        assert reports[1].id_camille == "456"
        assert reports[1].categories[VideoCategory.STANDARD].count == 1
        assert reports[1].categories[VideoCategory.LONG].count == 1
        assert reports[1].totals["total_mp4"] == 2

    def test_analyze_batch_with_unknown_durations(self):
        """Teste l'analyse avec des durées None (ignorées)."""
        video_entries = [
            VideoEntry(
                video_id="1",
                id_camille="273",
                person="Camille",
                month="2025-12",
                video_name="vid1.mp4",
                video_path="/fake/vid1.mp4",
                duration_sec=None,  # None -> ignorée
                duration_source="test",
                archive_folder="/fake",
            ),
            VideoEntry(
                video_id="2",
                id_camille="273",
                person="Camille",
                month="2025-12",
                video_name="vid2.mp4",
                video_path="/fake/vid2.mp4",
                duration_sec=90,  # short
                duration_source="test",
                archive_folder="/fake",
            ),
        ]

        batch = MonthlyBatch(
            batch_id="2025-12", generated_at=datetime.now(), video_entries=video_entries
        )
        thresholds = CategoryThresholds()

        reports = analyze_batch(batch, thresholds)

        assert len(reports) == 1
        report = reports[0]
        assert report.categories[VideoCategory.SHORT].count == 1
        assert report.categories[VideoCategory.SHORT].files == ["vid2.mp4"]
        assert report.totals["total_mp4"] == 1  # vid1 ignorée

    def test_analyze_batch_files_sorted_alphabetically(self):
        """Teste que les fichiers dans chaque catégorie sont triés alphabétiquement."""
        video_entries = [
            VideoEntry(
                video_id="1",
                id_camille="273",
                person="Camille",
                month="2025-12",
                video_name="z_video.mp4",
                video_path="/fake/z.mp4",
                duration_sec=90,  # short
                duration_source="test",
                archive_folder="/fake",
            ),
            VideoEntry(
                video_id="2",
                id_camille="273",
                person="Camille",
                month="2025-12",
                video_name="a_video.mp4",
                video_path="/fake/a.mp4",
                duration_sec=90,  # short
                duration_source="test",
                archive_folder="/fake",
            ),
        ]

        batch = MonthlyBatch(
            batch_id="2025-12", generated_at=datetime.now(), video_entries=video_entries
        )
        thresholds = CategoryThresholds()

        reports = analyze_batch(batch, thresholds)

        assert len(reports) == 1
        report = reports[0]
        # Les fichiers doivent être triés alphabétiquement
        assert report.categories[VideoCategory.SHORT].files == ["a_video.mp4", "z_video.mp4"]

    def test_analyze_batch_skips_all_unknown_durations(self):
        """Un lot dont aucune vidéo n'est catégorisable ne doit pas être émis."""
        video_entries = [
            VideoEntry(
                video_id="1",
                id_camille="13",
                person="Camille",
                month="2026-06",
                video_name="sans-duree-1.mp4",
                video_path="/fake/1.mp4",
                duration_sec=None,
                duration_source="archives_scan",
                archive_folder="/fake",
            ),
            VideoEntry(
                video_id="2",
                id_camille="13",
                person="Camille",
                month="2026-06",
                video_name="sans-duree-2.mp4",
                video_path="/fake/2.mp4",
                duration_sec=None,
                duration_source="archives_scan",
                archive_folder="/fake",
            ),
        ]
        batch = MonthlyBatch(
            batch_id="2026-06", generated_at=datetime.now(), video_entries=video_entries
        )

        assert analyze_batch(batch, CategoryThresholds()) == []

    def test_analyze_batch_preserves_synthetic_replicas(self):
        """Les réplicas BDC synthétiques partagent un nom mais doivent tous être comptés."""
        video_entries = [
            VideoEntry(
                video_id=f"BDC-1-0-{index}",
                id_camille="LOTS 1",
                person="LOTS 1",
                month="2026-09",
                video_name="Recadrage - vidéo courte (BDC-1)",
                video_path=f"bdc://BDC-1/0/{index}",
                duration_sec=90,
                duration_source="bdc_inferred",
                archive_folder="batch:2026-09",
            )
            for index in range(3)
        ]
        batch = MonthlyBatch(
            batch_id="2026-09", generated_at=datetime.now(), video_entries=video_entries
        )

        reports = analyze_batch(batch, CategoryThresholds())

        assert len(reports) == 1
        assert reports[0].categories[VideoCategory.SHORT].count == 3

    def test_analyze_batch_excludes_jingles_and_duplicates(self):
        """Les jingles M6+ et les vrais doublons de capture sont ignorés."""
        video_entries = [
            VideoEntry(
                video_id="1",
                id_camille="29",
                person="Camille",
                month="2026-08",
                video_name="CABANON_TAT_M6.mp4",
                video_path="/fake/29/a.mp4",
                duration_sec=90,
                duration_source="archives_scan",
                archive_folder="/fake",
            ),
            VideoEntry(
                video_id="2",
                id_camille="29",
                person="Camille",
                month="2026-08",
                video_name="CABANON_TAT_M6.mp4",
                video_path="/fake/29/b.mp4",
                duration_sec=90,
                duration_source="archives_scan",
                archive_folder="/fake",
            ),
            VideoEntry(
                video_id="3",
                id_camille="29",
                person="Camille",
                month="2026-08",
                video_name="m6+max anime.mp4",
                video_path="/fake/29/c.mp4",
                duration_sec=60,
                duration_source="archives_scan",
                archive_folder="/fake",
            ),
        ]
        batch = MonthlyBatch(
            batch_id="2026-08", generated_at=datetime.now(), video_entries=video_entries
        )

        reports = analyze_batch(batch, CategoryThresholds())

        assert len(reports) == 1
        assert reports[0].categories[VideoCategory.SHORT].count == 1
        assert reports[0].categories[VideoCategory.SHORT].files == ["CABANON_TAT_M6.mp4"]


if __name__ == "__main__":
    pytest.main([__file__])
