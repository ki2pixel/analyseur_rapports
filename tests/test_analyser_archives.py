"""
Tests pour la gestion des chemins externes dans l'analyse des archives et pour l'analyse du mois de juillet 2026.
"""

import json
import os
from pathlib import Path
from datetime import datetime
import pytest

from apps.reporting.analyser import (
    CategoryThresholds,
    analyze_month,
    categorize,
    collect_video_entries,
    collect_video_entries_with_warnings,
    derive_duration_from_scenes,
    disambiguate_folders,
    is_analyzable_video,
    parse_folder_name,
    _cache_key,
)
from libs.reconciliation.comparison import compare_reports


def test_collect_video_entries_external_path(tmp_path):
    """Vérifie que des metadata.json avec video_path externe sont correctement analysées."""
    archive_folder = tmp_path / "1 Camille 2026-07-01_09-58-50"
    archive_folder.mkdir(parents=True)

    sub = archive_folder / "hash_subfolder_123"
    sub.mkdir()

    meta_file = sub / "metadata.json"
    meta_payload = {
        "video_path": "/external/location/outside/repo/video.mp4",
        "video_stem": "video",
        "video_hash": "hash_subfolder_123",
    }
    meta_file.write_text(json.dumps(meta_payload), encoding="utf-8")

    # Scènes CSV factices pour le calcul de durée fallback
    scenes_file = sub / "video_scenes.csv"
    scenes_content = "Scene,Timecode In,Timecode Out\n1,00:00:00.000,00:02:30.000\n"
    scenes_file.write_text(scenes_content, encoding="utf-8")

    dt = datetime(2026, 7, 1)
    entries = collect_video_entries(archive_folder, "1", "Camille", dt)

    assert len(entries) == 1
    assert entries[0].id_camille == "1"
    assert entries[0].video_name == "video.mp4"
    assert entries[0].duration_sec == 150.0  # 2m 30s -> 150s


@pytest.mark.parametrize(
    "name,expected",
    [
        ("video.mp4", True),
        ("VIDEO.MP4", True),
        ("clip.Mp4", True),
        ("video.mov", False),
        ("video.MOV", False),
        ("video.Mov", False),
        ("video.avi", False),
        ("video.mkv", False),
        ("video.mts", False),
        ("video", False),
        ("m6+.mp4", False),
        ("M6+.MP4", False),
        ("m6_plus.mp4", False),
        ("m6+max anime.mp4", False),
        ("M6_PLUS_TEST.mp4", False),
        ("M6 - une vraie vidéo.mp4", True),
        (
            "M6+ - Pretty Little Liars - Extrait 5 - Quand Caleb vient s’excuser auprès de Spencer.mp4",
            True,
        ),
        ("M6+ - Pretty Little Liars - Extrait 6 - La mort de Maya.mp4", True),
        (
            "M6+MAX ANIME 1EP - LAA9 - post-diff 2 EP33 – Les Aventuriers découvrent la vallée des perles !.mp4",
            True,
        ),
    ],
)
def test_is_analyzable_video(name, expected):
    """Vérifie que seuls les .mp4 sont analysables (liste blanche) et que les jingles sont exclus."""
    assert is_analyzable_video(Path(name)) is expected


def test_collect_video_entries_whitelist_mp4(tmp_path):
    """Vérifie que '.mov' et les autres formats sont ignorés lors de la collecte des vidéos."""
    archive_folder = tmp_path / "5 Camille 2026-07-02_10-00-00"
    archive_folder.mkdir(parents=True)

    names = ["video.mp4", "clip.mov", "m6+.mp4", "autre.avi"]
    for index, name in enumerate(names):
        sub = archive_folder / f"hash_subfolder_{index}"
        sub.mkdir()
        (sub / "metadata.json").write_text(
            json.dumps(
                {
                    "video_path": f"/external/location/outside/repo/{name}",
                    "video_stem": Path(name).stem,
                }
            ),
            encoding="utf-8",
        )

    entries = collect_video_entries(archive_folder, "5", "Camille", datetime(2026, 7, 2))

    assert [entry.video_name for entry in entries] == ["video.mp4"]


def test_july_2026_reconciliation():
    """Vérifie que le rapprochement de juillet 2026 identifie uniquement 215, 233, 239 comme écarts."""
    # Les archives de juillet ne sont pas stockées localement : on valide sur le JSON de test.
    report_path = Path("apps/reporting/rapport-2026-07.json")
    assert report_path.exists()
    report_content = json.loads(report_path.read_text(encoding="utf-8"))
    assert len(report_content["report"]) == 39

    # Charger l'export BDC de juillet 2026
    bdc_path = Path("apps/bdc_converter/convert_bdc_07-2026.json")
    assert bdc_path.exists()
    bdc_content = json.loads(bdc_path.read_text(encoding="utf-8"))

    res = compare_reports(report_content, bdc_content)

    assert res["unexpected_ids"] == ["215", "233", "239"]
    missing_ids = [m["id_camille"] for m in res["missing_invoices"]]
    assert missing_ids == ["181", "189", "193", "195"]
    # L'expansion contextuelle ne doit produire aucun lot fantôme dans la plage 38..92.
    range_cluster = next(
        cluster for cluster in res["cluster_results"] if "BDC-26-1333" in cluster["bdc_numbers"]
    )
    assert len(range_cluster["cluster_ids"]) == 18
    assert range_cluster["missing_reporting_ids"] == []
    assert range_cluster["delta"] == {
        "short": 0,
        "standard": 0,
        "medium": 0,
        "long": 0,
    }


def test_range_reference_parsing():
    """Vérifie que les références sous forme d'intervalles (ex: 'LOTS 38 à 92') sont correctement étendues."""
    bdc_orders = [
        {
            "numero_bdc": "BDC-TEST",
            "reference": "LOTS 38 à 42",
            "articles": [{"description": "vidéo standard", "quantite": "5"}],
        }
    ]
    report = {
        "report": [
            {
                "id_camille": "38",
                "person": "Camille",
                "categories": {"standard": {"count": 1, "files": ["v1.mp4"]}},
            },
            {
                "id_camille": "40",
                "person": "Camille",
                "categories": {"standard": {"count": 1, "files": ["v2.mp4"]}},
            },
            {
                "id_camille": "42",
                "person": "Camille",
                "categories": {"standard": {"count": 1, "files": ["v3.mp4"]}},
            },
        ]
    }
    res = compare_reports(report, bdc_orders)
    # Les lots 38, 40 et 42 en prod ne doivent pas être considérés manquants dans le BDC
    missing_ids = [m["id_camille"] for m in res["missing_invoices"]]
    assert missing_ids == []
    # Les numéros 39 et 41 de la plage ne doivent pas être signalés comme lots facturés inconnus
    assert res["unexpected_ids"] == []


@pytest.mark.parametrize(
    "duration,expected",
    [
        (None, None),
        (-1, None),
        (0, "short"),
        (119.9, "short"),
        (120, "standard"),
        (299.9, "standard"),
        (300, "medium"),
        (479.9, "medium"),
        (480, "long"),
        (500, "long"),
    ],
)
def test_categorize_threshold_boundaries(duration, expected):
    assert categorize(duration, CategoryThresholds()) == expected


def _reset_duration_cache(monkeypatch):
    monkeypatch.setattr("apps.reporting.analyser._DUR_CACHE", {})
    monkeypatch.setattr("apps.reporting.analyser._DurationCacheState.loaded", True)
    monkeypatch.setattr("apps.reporting.analyser._DurationCacheState.modified", False)
    monkeypatch.setattr("apps.reporting.analyser._save_duration_cache", lambda: None)


def _make_subfolder(archive_folder, name, video_path, duration="00:01:30.000"):
    sub = archive_folder / name
    sub.mkdir()
    (sub / "metadata.json").write_text(
        json.dumps({"video_path": video_path, "video_stem": Path(video_path).stem}),
        encoding="utf-8",
    )
    if duration is not None:
        (sub / "video_scenes.csv").write_text(
            "Scene,Timecode In,Timecode Out\n1,00:00:00.000," + duration + "\n",
            encoding="utf-8",
        )
    return sub


def test_collect_video_entries_deduplicates_same_name(tmp_path, monkeypatch):
    _reset_duration_cache(monkeypatch)
    archive_folder = tmp_path / "29 Camille 2026-08-01_10-00-00"
    archive_folder.mkdir()
    _make_subfolder(
        archive_folder,
        "hash_a",
        "/external/archives/a/CABANON_TAT_M6.mp4",
    )
    _make_subfolder(
        archive_folder,
        "hash_b",
        "/external/archives/b/CABANON_TAT_M6.mp4",
    )

    entries, warnings = collect_video_entries_with_warnings(
        archive_folder, "29", "Camille", datetime(2026, 8, 1)
    )

    assert len(entries) == 1
    assert entries[0].video_name == "CABANON_TAT_M6.mp4"
    assert any("Doublon ignoré" in warning for warning in warnings)


def test_collect_video_entries_reports_jingle_warning(tmp_path, monkeypatch):
    _reset_duration_cache(monkeypatch)
    archive_folder = tmp_path / "63 Camille 2026-09-08_19-31-55"
    archive_folder.mkdir()
    _make_subfolder(
        archive_folder,
        "hash_jingle",
        "/external/archives/63/m6+max anime.mp4",
        duration=None,
    )

    entries, warnings = collect_video_entries_with_warnings(
        archive_folder, "63", "Camille", datetime(2026, 9, 8)
    )

    assert entries == []
    assert any("Jingle M6+" in warning for warning in warnings)
    assert any("Aucune vidéo valide" in warning for warning in warnings)


def test_analyze_month_skips_lot_without_categorizable_video(tmp_path, monkeypatch):
    _reset_duration_cache(monkeypatch)
    archives = tmp_path / "archives"
    archive_folder = archives / "13 Camille 2026-06-01_10-00-00"
    archive_folder.mkdir(parents=True)
    _make_subfolder(
        archive_folder,
        "hash_empty",
        "/external/archives/13/video-sans-duree.mp4",
        duration=None,
    )

    reports = analyze_month(archives, 2026, 6, CategoryThresholds())

    assert reports == []


def test_analyze_month_mixed_numeric_and_alphanumeric_sort(tmp_path, monkeypatch):
    _reset_duration_cache(monkeypatch)
    archives = tmp_path / "archives"
    for lot_id in ("12B", "12"):
        folder = archives / f"{lot_id} Camille 2026-07-01_10-00-00"
        folder.mkdir(parents=True)
        _make_subfolder(
            folder,
            f"hash_{lot_id}",
            f"/external/archives/{lot_id}/video.mp4",
        )

    reports = analyze_month(archives, 2026, 7, CategoryThresholds())

    assert [report.id_camille for report in reports] == ["12", "12B"]
    assert all(report.categories["short"].count == 1 for report in reports)


def test_disambiguate_folders_duplicate_different_contents(tmp_path, monkeypatch):
    _reset_duration_cache(monkeypatch)
    archives = tmp_path / "archives"
    folder1 = archives / "309 Camille 2026-10-01_09-17-44"
    folder2 = archives / "309 Camille 2026-10-27_13-11-40"
    folder1.mkdir(parents=True)
    folder2.mkdir(parents=True)

    _make_subfolder(folder1, "h1", "/external/archives/309/v1.mp4")
    _make_subfolder(folder2, "h2", "/external/archives/309/v2.mp4")

    disambiguated = disambiguate_folders([folder1, folder2])
    effective_ids = [item[1] for item in disambiguated]
    assert effective_ids == ["309 (2026-10-01)", "309 (2026-10-27)"]

    reports = analyze_month(archives, 2026, 10, CategoryThresholds())
    assert [r.id_camille for r in reports] == ["309 (2026-10-01)", "309 (2026-10-27)"]


def test_disambiguate_folders_same_day_keeps_time(tmp_path, monkeypatch):
    _reset_duration_cache(monkeypatch)
    archives = tmp_path / "archives"
    folder1 = archives / "309 Camille 2026-10-01_09-17-44"
    folder2 = archives / "309 Camille 2026-10-01_18-11-40"
    folder1.mkdir(parents=True)
    folder2.mkdir(parents=True)

    _make_subfolder(folder1, "h1", "/external/archives/309/v1.mp4")
    _make_subfolder(folder2, "h2", "/external/archives/309/v2.mp4")

    disambiguated = disambiguate_folders([folder1, folder2])
    effective_ids = [item[1] for item in disambiguated]

    assert effective_ids == [
        "309 (2026-10-01 09:17:44)",
        "309 (2026-10-01 18:11:40)",
    ]

    reports = analyze_month(archives, 2026, 10, CategoryThresholds())
    assert [r.id_camille for r in reports] == effective_ids


def test_derive_duration_from_scenes_ignores_empty_last_rows(tmp_path):
    folder = tmp_path / "lot"
    folder.mkdir()
    (folder / "video_scenes.csv").write_text(
        "Scene,Timecode In,Timecode Out\n"
        "1,00:00:00.000,00:02:30.000\n"
        "2,00:02:30.000,\n"
        "Total,,,\n",
        encoding="utf-8",
    )

    assert derive_duration_from_scenes(folder) == 150.0


def test_cache_key_depends_on_scenes_csv_when_video_is_missing(tmp_path):
    sub1 = tmp_path / "hash_a"
    sub2 = tmp_path / "hash_b"
    sub1.mkdir()
    sub2.mkdir()
    (sub1 / "video_scenes.csv").write_text(
        "Scene,Timecode In,Timecode Out\n1,00:00:00.000,00:01:00.000\n",
        encoding="utf-8",
    )
    (sub2 / "video_scenes.csv").write_text(
        "Scene,Timecode In,Timecode Out\n1,00:00:00.000,00:02:00.000\n",
        encoding="utf-8",
    )
    missing_video = Path("/external/archives/EXTRAIT 3.mp4")

    key1 = _cache_key(missing_video, sub1)
    key2 = _cache_key(missing_video, sub2)

    assert key1 != key2

    (sub1 / "video_scenes.csv").write_text(
        "Scene,Timecode In,Timecode Out\n1,00:00:00.000,00:03:00.000\n",
        encoding="utf-8",
    )
    os.utime(sub1 / "video_scenes.csv", ns=(2_000_000_000, 2_000_000_000))
    assert _cache_key(missing_video, sub1) != key1
