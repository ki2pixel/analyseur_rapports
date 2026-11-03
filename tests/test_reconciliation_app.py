import pytest
from datetime import datetime, timedelta, timezone
import json
import re
import uuid

from apps.reconciliation.app import app as reconciliation_app
from apps.reconciliation.app import (
    StoredAnalysis,
    _cleanup_store,
    _effective_reporting_payload,
    _recompute_record,
    _store,
    _store_analysis,
)
from libs.reconciliation import compare_reports


def test_cleanup_store_removes_old_entries():
    # Setup: clear the store first
    _store.clear()

    # Add an old entry (more than 2 hours old)
    old_token = "old_token"
    _store[old_token] = StoredAnalysis(
        token=old_token,
        reporting_name="rep.json",
        bdc_name="bdc.json",
        reporting_payload={},
        bdc_payload=[],
        analysis={},
        created_at=datetime.now(timezone.utc) - timedelta(hours=3),
    )

    # Add a recent entry
    recent_token = "recent_token"
    _store[recent_token] = StoredAnalysis(
        token=recent_token,
        reporting_name="rep.json",
        bdc_name="bdc.json",
        reporting_payload={},
        bdc_payload=[],
        analysis={},
        created_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )

    assert len(_store) == 2

    # Execute cleanup
    _cleanup_store()

    # Only the recent one should remain
    assert len(_store) == 1
    assert recent_token in _store
    assert old_token not in _store


def test_cleanup_store_respects_max_size():
    # Setup: clear the store first
    _store.clear()

    # Add 60 recent entries (exceeds max size of 50)
    now = datetime.now(timezone.utc)
    for i in range(60):
        token = f"token_{i}"
        _store[token] = StoredAnalysis(
            token=token,
            reporting_name="rep.json",
            bdc_name="bdc.json",
            reporting_payload={},
            bdc_payload=[],
            analysis={},
            # Slightly different timestamps to ensure predictable sorting
            created_at=now - timedelta(seconds=60 - i),
        )

    assert len(_store) == 60

    # Execute cleanup
    _cleanup_store()

    # Should be truncated to 50 entries
    assert len(_store) == 50
    # The oldest 10 entries (token_0 to token_9) should be removed
    for i in range(10):
        assert f"token_{i}" not in _store
    # The 50 newest entries should still be there
    for i in range(10, 60):
        assert f"token_{i}" in _store


def test_store_analysis_triggers_cleanup():
    _store.clear()

    # Mocking old entry
    old_token = "old_token"
    _store[old_token] = StoredAnalysis(
        token=old_token,
        reporting_name="rep.json",
        bdc_name="bdc.json",
        reporting_payload={},
        bdc_payload=[],
        analysis={},
        created_at=datetime.now(timezone.utc) - timedelta(hours=3),
    )

    # This should trigger cleanup and add a new one
    new_token = _store_analysis("rep.json", "bdc.json", {}, [], {})

    assert old_token not in _store
    assert new_token in _store
    assert len(_store) == 1


def _reporting_payload_with_jingle():
    return {
        "report": [
            {
                "id_camille": "29",
                "person": "Camille",
                "categories": {
                    "short": {
                        "count": 2,
                        "files": ["CABANON_TAT_M6.mp4", "m6+max anime.mp4"],
                        "files_detail": [
                            {"name": "CABANON_TAT_M6.mp4", "duration_sec": 90.0},
                            {"name": "m6+max anime.mp4", "duration_sec": 60.0},
                        ],
                    }
                },
            }
        ]
    }


def test_effective_reporting_payload_excludes_one_occurrence():
    record = StoredAnalysis(
        token="token",
        reporting_name="rapport-2026-08.json",
        bdc_name="convert_bdc_08-2026.json",
        reporting_payload=_reporting_payload_with_jingle(),
        bdc_payload=[],
        analysis={},
        created_at=datetime.now(timezone.utc),
        excluded_files={("*", "m6+max anime.mp4"): 1},
    )

    effective = _effective_reporting_payload(record)
    category = effective["report"][0]["categories"]["short"]

    assert category["files"] == ["CABANON_TAT_M6.mp4"]
    assert [detail["name"] for detail in category["files_detail"]] == ["CABANON_TAT_M6.mp4"]
    assert category["count"] == 1
    # Le payload d'origine doit rester intact pour permettre une réinitialisation.
    assert record.reporting_payload["report"][0]["categories"]["short"]["count"] == 2


def test_effective_reporting_payload_scopes_exclusion_to_one_lot():
    payload = {
        "total_mp4": 3,
        "group_totals": {"10 Camille": 2, "20 Camille": 1},
        "report": [
            {
                "id_camille": "10",
                "person": "Camille",
                "categories": {
                    "short": {
                        "count": 2,
                        "files": ["same.mp4", "same.mp4"],
                        "files_detail": [
                            {"name": "same.mp4", "duration_sec": 45.0},
                            {"name": "same.mp4", "duration_sec": 45.5},
                        ],
                    }
                },
            },
            {
                "id_camille": "20",
                "person": "Camille",
                "categories": {
                    "short": {
                        "count": 1,
                        "files": ["same.mp4"],
                        "files_detail": [
                            {"name": "same.mp4", "duration_sec": 50.0},
                        ],
                    }
                },
            },
        ],
    }
    record = StoredAnalysis(
        token="token",
        reporting_name="rapport-2026-08.json",
        bdc_name="convert_bdc_08-2026.json",
        reporting_payload=payload,
        bdc_payload=[],
        analysis={},
        created_at=datetime.now(timezone.utc),
        excluded_files={("10", "same.mp4"): 1},
    )

    effective = _effective_reporting_payload(record)
    lot_10, lot_20 = effective["report"]

    # Only one occurrence in lot 10 is removed; lot 20 keeps its own file.
    assert lot_10["categories"]["short"]["files"] == ["same.mp4"]
    assert lot_10["categories"]["short"]["count"] == 1
    assert lot_20["categories"]["short"]["files"] == ["same.mp4"]
    assert lot_20["categories"]["short"]["count"] == 1
    assert effective["total_mp4"] == 2
    assert effective["group_totals"] == {"10 Camille": 1, "20 Camille": 1}
    # Source payload remains untouched.
    assert record.reporting_payload["total_mp4"] == 3


def test_recompute_record_after_justification():
    reporting_payload = {
        "report": [
            {
                "id_camille": "1",
                "person": "Camille",
                "categories": {
                    "standard": {
                        "count": 2,
                        "files": ["dup.mp4", "dup.mp4"],
                        "files_detail": [
                            {"name": "dup.mp4", "duration_sec": 180.0},
                            {"name": "dup.mp4", "duration_sec": 181.0},
                        ],
                    }
                },
            }
        ]
    }
    bdc_payload = [
        {
            "numero_bdc": "BDC-1",
            "reference": "LOT 1",
            "articles": [{"description": "vidéo standard", "quantite": "1"}],
        }
    ]
    record = StoredAnalysis(
        token="token",
        reporting_name="rapport-2026-03.json",
        bdc_name="convert_bdc_03-2026.json",
        reporting_payload=reporting_payload,
        bdc_payload=bdc_payload,
        analysis=compare_reports(reporting_payload, bdc_payload),
        created_at=datetime.now(timezone.utc),
        month="2026-03",
    )
    assert record.analysis["conformity"]["score"] == 0.0

    record.justified_clusters.add(record.analysis["cluster_results"][0]["cluster_key"])
    _recompute_record(record)

    assert record.analysis["conformity"]["score"] == 100.0
    assert record.analysis["conformity"]["justified_orders"] == 1
    assert record.analysis["conformity"]["strict_compliant_orders"] == 0
    assert record.analysis["cluster_results"][0]["justified"] is True
    assert record.analysis["cluster_differences"] == []


def test_justify_rejects_gap_not_caused_by_duplicate(tmp_path, monkeypatch):
    _store.clear()
    reporting_dir = tmp_path / "reporting"
    bdc_dir = tmp_path / "bdc"
    reporting_dir.mkdir()
    bdc_dir.mkdir()

    report = {
        "report": [
            {
                "id_camille": "10",
                "person": "Camille",
                "categories": {
                    "short": {
                        "count": 1,
                        "files": ["real.mp4"],
                        "files_detail": [{"name": "real.mp4", "duration_sec": 90.0}],
                    }
                },
            }
        ]
    }
    bdc = [
        {
            "numero_bdc": "BDC-MISSING-1",
            "reference": "LOT 10",
            "references": ["LOT 10"],
            "articles": [{"description": "vidéo courte", "quantite": "2"}],
        }
    ]
    (reporting_dir / "rapport-2026-11.json").write_text(json.dumps(report), encoding="utf-8")
    (bdc_dir / "convert_bdc_11-2026.json").write_text(json.dumps(bdc), encoding="utf-8")

    monkeypatch.setattr("apps.reconciliation.app.REPORTING_DIR", reporting_dir)
    monkeypatch.setattr("apps.reconciliation.app.BDC_DIR", bdc_dir)
    reconciliation_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)

    with reconciliation_app.test_client() as client:
        client.set_cookie("admin_token", reconciliation_app.config["ADMIN_TOKEN"])
        post = client.post(
            "/",
            data={
                "reporting_filename": "rapport-2026-11.json",
                "bdc_filename": "convert_bdc_11-2026.json",
            },
        )
        assert post.status_code == 200
        token_match = re.search(r"/export/html\?token=([a-f0-9]+)", post.get_data(as_text=True))
        assert token_match is not None
        token = token_match.group(1)

        refused = client.post(
            "/inspector/justify",
            data={"token": token, "cluster_key": "10"},
        )
        assert refused.status_code == 400
        assert "Justification impossible" in refused.get_data(as_text=True)

        page = client.get(f"/?token={token}&month=2026-11").get_data(as_text=True)
        assert "Justification impossible" in page
        assert "0.0%" in page


def test_inspector_actions_route(tmp_path, monkeypatch):
    _store.clear()
    reporting_dir = tmp_path / "reporting"
    bdc_dir = tmp_path / "bdc"
    reporting_dir.mkdir()
    bdc_dir.mkdir()

    report = {
        "report": [
            {
                "id_camille": "63",
                "person": "Camille",
                "categories": {
                    "short": {
                        "count": 3,
                        "files": [
                            "real.mp4",
                            "m6+max anime.mp4",
                            "m6+max anime.mp4",
                        ],
                        "files_detail": [
                            {"name": "real.mp4", "duration_sec": 90.0},
                            {"name": "m6+max anime.mp4", "duration_sec": 60.0},
                            {"name": "m6+max anime.mp4", "duration_sec": 61.0},
                        ],
                    }
                },
            }
        ]
    }
    bdc = [
        {
            "numero_bdc": "BDC-1528",
            "reference": "LOT 63",
            "references": ["LOT 63"],
            "articles": [{"description": "vidéo courte", "quantite": "1"}],
        }
    ]
    (reporting_dir / "rapport-2026-09.json").write_text(json.dumps(report), encoding="utf-8")
    (bdc_dir / "convert_bdc_09-2026.json").write_text(json.dumps(bdc), encoding="utf-8")

    monkeypatch.setattr("apps.reconciliation.app.REPORTING_DIR", reporting_dir)
    monkeypatch.setattr("apps.reconciliation.app.BDC_DIR", bdc_dir)
    reconciliation_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)

    with reconciliation_app.test_client() as client:
        client.set_cookie("admin_token", reconciliation_app.config["ADMIN_TOKEN"])
        post = client.post(
            "/",
            data={
                "reporting_filename": "rapport-2026-09.json",
                "bdc_filename": "convert_bdc_09-2026.json",
            },
        )
        assert post.status_code == 200
        html = post.get_data(as_text=True)
        token_match = re.search(r"/export/html\?token=([a-f0-9]+)", html)
        assert token_match is not None
        token = token_match.group(1)
        assert "0.0%" in html

        justify = client.post(
            "/inspector/justify",
            data={"token": token, "cluster_key": "63"},
        )
        assert justify.status_code == 302
        after_justify = client.get(justify.headers["Location"]).get_data(as_text=True)
        assert "100.0%" in after_justify
        assert "Annuler la justification" in after_justify
        undo = client.post(
            "/inspector/justify",
            data={"token": token, "cluster_key": "63", "undo": "1"},
        )
        assert undo.status_code == 302

        exclude = client.post(
            "/inspector/exclude",
            data={
                "token": token,
                "filename": "m6+max anime.mp4",
                "remove_all": "1",
            },
        )
        assert exclude.status_code == 302
        after_exclude = client.get(exclude.headers["Location"]).get_data(as_text=True)
        assert "100.0%" in after_exclude
        assert "m6+max anime.mp4" not in after_exclude

        reset = client.post("/inspector/reset", data={"token": token})
        assert reset.status_code == 302
        after_reset = client.get(reset.headers["Location"]).get_data(as_text=True)
        assert "0.0%" in after_reset
        assert "m6+max anime.mp4" in after_reset


def test_inspector_actions_with_expired_token_auto_recovery(tmp_path, monkeypatch):
    _store.clear()
    reporting_dir = tmp_path / "reporting"
    bdc_dir = tmp_path / "bdc"
    reporting_dir.mkdir()
    bdc_dir.mkdir()

    report = {
        "report": [
            {
                "id_camille": "63",
                "person": "Camille",
                "categories": {
                    "short": {
                        "count": 2,
                        "files": ["real.mp4", "real.mp4"],
                        "files_detail": [
                            {"name": "real.mp4", "duration_sec": 90.0},
                            {"name": "real.mp4", "duration_sec": 90.5},
                        ],
                    }
                },
            }
        ]
    }
    bdc = [
        {
            "numero_bdc": "BDC-1528",
            "reference": "LOT 63",
            "references": ["LOT 63"],
            "articles": [{"description": "vidéo courte", "quantite": "1"}],
        }
    ]
    (reporting_dir / "rapport-2026-09.json").write_text(json.dumps(report), encoding="utf-8")
    (bdc_dir / "convert_bdc_09-2026.json").write_text(json.dumps(bdc), encoding="utf-8")

    monkeypatch.setattr("apps.reconciliation.app.REPORTING_DIR", reporting_dir)
    monkeypatch.setattr("apps.reconciliation.app.BDC_DIR", bdc_dir)
    reconciliation_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)

    with reconciliation_app.test_client() as client:
        client.set_cookie("admin_token", reconciliation_app.config["ADMIN_TOKEN"])

        # 1. POST justify with an expired/unknown token but valid month
        _store.clear()
        res_justify = client.post(
            "/inspector/justify",
            data={
                "token": "expired_token_123",
                "month": "2026-09",
                "cluster_key": "63",
            },
        )
        assert res_justify.status_code == 302
        redirect_url = res_justify.headers["Location"]
        assert "month=2026-09" in redirect_url
        page = client.get(redirect_url).get_data(as_text=True)
        assert "Annuler la justification" in page

        # 2. POST undo justification with an expired token
        _store.clear()
        res_undo = client.post(
            "/inspector/justify",
            data={
                "token": "expired_token_456",
                "month": "2026-09",
                "cluster_key": "63",
                "undo": "1",
            },
        )
        assert res_undo.status_code == 302
        page_after_undo = client.get(res_undo.headers["Location"]).get_data(as_text=True)
        assert "Justifier le doublon" in page_after_undo

        # 3. Export routes with expired token + month fallback
        _store.clear()
        export_html = client.get("/export/html?token=expired_token&month=2026-09")
        assert export_html.status_code == 200
        assert "text/html" in export_html.headers["Content-Type"]

        _store.clear()
        export_json = client.get("/export/json?token=expired_token&month=2026-09")
        assert export_json.status_code == 200
        assert "application/json" in export_json.headers["Content-Type"]

        # 4. Unknown token without month or reports should return 404
        _store.clear()
        bad = client.post(
            "/inspector/justify",
            data={"token": "unknown_random_token", "cluster_key": "63"},
        )
        assert bad.status_code == 404
        assert "Analyse introuvable ou expirée" in bad.get_data(as_text=True)


def test_token_is_reloaded_when_source_files_change(tmp_path, monkeypatch):
    _store.clear()
    reporting_dir = tmp_path / "reporting"
    bdc_dir = tmp_path / "bdc"
    reporting_dir.mkdir()
    bdc_dir.mkdir()

    report = {
        "report": [
            {
                "id_camille": "63",
                "person": "Camille",
                "categories": {
                    "short": {
                        "count": 1,
                        "files": ["real.mp4"],
                        "files_detail": [{"name": "real.mp4", "duration_sec": 90.0}],
                    }
                },
            }
        ]
    }
    bdc = [
        {
            "numero_bdc": "BDC-1528",
            "reference": "LOT 63",
            "references": ["LOT 63"],
            "articles": [{"description": "vidéo courte", "quantite": "1"}],
        }
    ]
    reporting_path = reporting_dir / "rapport-2026-09.json"
    reporting_path.write_text(json.dumps(report), encoding="utf-8")
    (bdc_dir / "convert_bdc_09-2026.json").write_text(json.dumps(bdc), encoding="utf-8")

    monkeypatch.setattr("apps.reconciliation.app.REPORTING_DIR", reporting_dir)
    monkeypatch.setattr("apps.reconciliation.app.BDC_DIR", bdc_dir)
    reconciliation_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)

    with reconciliation_app.test_client() as client:
        client.set_cookie("admin_token", reconciliation_app.config["ADMIN_TOKEN"])
        post = client.post(
            "/",
            data={
                "reporting_filename": "rapport-2026-09.json",
                "bdc_filename": "convert_bdc_09-2026.json",
            },
        )
        assert post.status_code == 200
        token_match = re.search(r"/export/html\?token=([a-f0-9]+)", post.get_data(as_text=True))
        assert token_match is not None
        token = token_match.group(1)

        # A regenerated source report invalidates the in-memory analysis.
        report["report"][0]["categories"]["short"]["count"] = 2
        report["report"][0]["categories"]["short"]["files"].append("extra.mp4")
        report["report"][0]["categories"]["short"]["files_detail"].append(
            {"name": "extra.mp4", "duration_sec": 91.0}
        )
        reporting_path.write_text(json.dumps(report), encoding="utf-8")

        page = client.get(f"/?token={token}&month=2026-09")
        assert page.status_code == 200
        html = page.get_data(as_text=True)
        assert "fichiers sources modifiés" in html
        new_token_match = re.search(r"/export/html\?token=([a-f0-9]+)", html)
        assert new_token_match is not None
        assert new_token_match.group(1) != token


def test_export_includes_justified_differences_and_file_details(tmp_path, monkeypatch):
    _store.clear()
    reporting_dir = tmp_path / "reporting"
    bdc_dir = tmp_path / "bdc"
    reporting_dir.mkdir()
    bdc_dir.mkdir()

    report = {
        "report": [
            {
                "id_camille": "10",
                "person": "Camille",
                "categories": {
                    "short": {
                        "count": 3,
                        "files": ["clip1.mp4", "clip2.mp4", "clip1.mp4"],
                        "files_detail": [
                            {"name": "clip1.mp4", "duration_sec": 45.5},
                            {"name": "clip2.mp4", "duration_sec": 80.0},
                            {"name": "clip1.mp4", "duration_sec": 46.0},
                        ],
                    },
                    "standard": {"count": 0, "files": [], "files_detail": []},
                    "medium": {"count": 0, "files": [], "files_detail": []},
                    "long": {"count": 0, "files": [], "files_detail": []},
                },
            }
        ]
    }
    bdc = [
        {
            "numero_bdc": "BDC-TEST-1",
            "reference": "LOT 10",
            "references": ["LOT 10"],
            "articles": [{"description": "vidéo courte", "quantite": "2"}],
        }
    ]
    (reporting_dir / "rapport-2026-11.json").write_text(json.dumps(report), encoding="utf-8")
    (bdc_dir / "convert_bdc_11-2026.json").write_text(json.dumps(bdc), encoding="utf-8")

    monkeypatch.setattr("apps.reconciliation.app.REPORTING_DIR", reporting_dir)
    monkeypatch.setattr("apps.reconciliation.app.BDC_DIR", bdc_dir)
    reconciliation_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)

    with reconciliation_app.test_client() as client:
        client.set_cookie("admin_token", reconciliation_app.config["ADMIN_TOKEN"])
        post = client.post(
            "/",
            data={
                "reporting_filename": "rapport-2026-11.json",
                "bdc_filename": "convert_bdc_11-2026.json",
            },
        )
        assert post.status_code == 200
        html = post.get_data(as_text=True)
        token_match = re.search(r"/export/html\?token=([a-f0-9]+)", html)
        assert token_match is not None
        token = token_match.group(1)

        # Justify the cluster
        justify = client.post(
            "/inspector/justify",
            data={"token": token, "cluster_key": "10"},
        )
        assert justify.status_code == 302

        # Export HTML
        exp_html = client.get(f"/export/html?token={token}&month=2026-11")
        assert exp_html.status_code == 200
        assert (
            "filename=certificat-rapprochement-2026-11.html"
            in exp_html.headers["Content-Disposition"]
        )
        html_text = exp_html.get_data(as_text=True)
        assert "BDC-TEST-1" in html_text
        assert "Écart justifié" in html_text
        assert "clip1.mp4" in html_text
        assert "45.5 s" in html_text
        assert "clip2.mp4" in html_text
        assert "80.0 s" in html_text

        # Export JSON
        exp_json = client.get(f"/export/json?token={token}&month=2026-11")
        assert exp_json.status_code == 200
        data_json = json.loads(exp_json.get_data(as_text=True))
        diffs = data_json["cluster_differences"]
        assert len(diffs) == 1
        assert diffs[0]["bdc_numbers"] == ["BDC-TEST-1"]
        assert diffs[0]["justified"] is True


def test_export_hides_duplicate_highlights_for_justified_cluster(tmp_path, monkeypatch):
    _store.clear()
    reporting_dir = tmp_path / "reporting"
    bdc_dir = tmp_path / "bdc"
    reporting_dir.mkdir()
    bdc_dir.mkdir()

    report = {
        "report": [
            {
                "id_camille": "10",
                "person": "Camille",
                "categories": {
                    "short": {
                        "count": 2,
                        "files": ["dup.mp4", "dup.mp4"],
                        "files_detail": [
                            {"name": "dup.mp4", "duration_sec": 45.5},
                            {"name": "dup.mp4", "duration_sec": 46.0},
                        ],
                    },
                    "standard": {"count": 0, "files": [], "files_detail": []},
                    "medium": {"count": 0, "files": [], "files_detail": []},
                    "long": {"count": 0, "files": [], "files_detail": []},
                },
            }
        ]
    }
    bdc = [
        {
            "numero_bdc": "BDC-DUP-1",
            "reference": "LOT 10",
            "references": ["LOT 10"],
            "articles": [{"description": "vidéo courte", "quantite": "1"}],
        }
    ]
    (reporting_dir / "rapport-2026-11.json").write_text(json.dumps(report), encoding="utf-8")
    (bdc_dir / "convert_bdc_11-2026.json").write_text(json.dumps(bdc), encoding="utf-8")

    monkeypatch.setattr("apps.reconciliation.app.REPORTING_DIR", reporting_dir)
    monkeypatch.setattr("apps.reconciliation.app.BDC_DIR", bdc_dir)
    reconciliation_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)

    with reconciliation_app.test_client() as client:
        client.set_cookie("admin_token", reconciliation_app.config["ADMIN_TOKEN"])
        post = client.post(
            "/",
            data={
                "reporting_filename": "rapport-2026-11.json",
                "bdc_filename": "convert_bdc_11-2026.json",
            },
        )
        assert post.status_code == 200
        token_match = re.search(r"/export/html\?token=([a-f0-9]+)", post.get_data(as_text=True))
        assert token_match is not None
        token = token_match.group(1)

        before = client.get(f"/export/html?token={token}&month=2026-11")
        assert before.status_code == 200
        before_html = before.get_data(as_text=True)
        assert "Jingle / doublon" in before_html
        assert "doublon" in before_html
        assert "À vérifier" in before_html

        justify = client.post(
            "/inspector/justify",
            data={"token": token, "cluster_key": "10"},
        )
        assert justify.status_code == 302

        after = client.get(f"/export/html?token={token}&month=2026-11")
        assert after.status_code == 200
        after_html = after.get_data(as_text=True)
        assert "Écart justifié" in after_html
        assert "BDC-DUP-1" in after_html
        assert "dup.mp4" in after_html
        assert "Jingle / doublon" not in after_html
        assert "ressemble à un doublon" not in after_html
        assert "apparaît en double" not in after_html
        assert "À vérifier" not in after_html


def test_export_total_uses_net_balance_and_explains_it(tmp_path, monkeypatch):
    _store.clear()
    reporting_dir = tmp_path / "reporting"
    bdc_dir = tmp_path / "bdc"
    reporting_dir.mkdir()
    bdc_dir.mkdir()

    files = [f"clip{i}.mp4" for i in range(1, 5)]
    report = {
        "report": [
            {
                "id_camille": "10",
                "person": "Camille",
                "categories": {
                    "short": {
                        "count": 4,
                        "files": files,
                        "files_detail": [{"name": name, "duration_sec": 45.0} for name in files],
                    },
                    "standard": {"count": 0, "files": [], "files_detail": []},
                    "medium": {"count": 0, "files": [], "files_detail": []},
                    "long": {"count": 0, "files": [], "files_detail": []},
                },
            }
        ]
    }
    bdc = [
        {
            "numero_bdc": "BDC-NET-1",
            "reference": "LOT 10",
            "references": ["LOT 10"],
            "articles": [
                {"description": "vidéo courte", "quantite": "1"},
                {"description": "vidéo longue", "quantite": "1"},
            ],
        }
    ]
    (reporting_dir / "rapport-2026-11.json").write_text(json.dumps(report), encoding="utf-8")
    (bdc_dir / "convert_bdc_11-2026.json").write_text(json.dumps(bdc), encoding="utf-8")

    monkeypatch.setattr("apps.reconciliation.app.REPORTING_DIR", reporting_dir)
    monkeypatch.setattr("apps.reconciliation.app.BDC_DIR", bdc_dir)
    reconciliation_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)

    with reconciliation_app.test_client() as client:
        client.set_cookie("admin_token", reconciliation_app.config["ADMIN_TOKEN"])
        post = client.post(
            "/",
            data={
                "reporting_filename": "rapport-2026-11.json",
                "bdc_filename": "convert_bdc_11-2026.json",
            },
        )
        assert post.status_code == 200
        token_match = re.search(r"/export/html\?token=([a-f0-9]+)", post.get_data(as_text=True))
        assert token_match is not None
        token = token_match.group(1)

        export = client.get(f"/export/html?token={token}&month=2026-11")
        assert export.status_code == 200
        body = export.get_data(as_text=True)
        assert "Écart net : +2" in body
        assert "3 non facturées" in body
        assert "1 manquante" in body
        assert "= +2" in body
        assert "solde net" in body


def test_export_pdf_downloads_valid_document(tmp_path, monkeypatch):
    pytest.importorskip("weasyprint")
    _store.clear()
    reporting_dir = tmp_path / "reporting"
    bdc_dir = tmp_path / "bdc"
    reporting_dir.mkdir()
    bdc_dir.mkdir()

    report = {
        "report": [
            {
                "id_camille": "10",
                "person": "Camille",
                "categories": {
                    "short": {
                        "count": 2,
                        "files": ["clip1.mp4", "clip2.mp4"],
                        "files_detail": [
                            {"name": "clip1.mp4", "duration_sec": 45.5},
                            {"name": "clip2.mp4", "duration_sec": 80.0},
                        ],
                    },
                    "standard": {"count": 0, "files": [], "files_detail": []},
                    "medium": {"count": 0, "files": [], "files_detail": []},
                    "long": {"count": 0, "files": [], "files_detail": []},
                },
            }
        ]
    }
    bdc = [
        {
            "numero_bdc": "BDC-PDF-1",
            "reference": "LOT 10",
            "references": ["LOT 10"],
            "articles": [{"description": "vidéo courte", "quantite": "1"}],
        }
    ]
    (reporting_dir / "rapport-2026-11.json").write_text(json.dumps(report), encoding="utf-8")
    (bdc_dir / "convert_bdc_11-2026.json").write_text(json.dumps(bdc), encoding="utf-8")

    monkeypatch.setattr("apps.reconciliation.app.REPORTING_DIR", reporting_dir)
    monkeypatch.setattr("apps.reconciliation.app.BDC_DIR", bdc_dir)
    reconciliation_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)

    with reconciliation_app.test_client() as client:
        client.set_cookie("admin_token", reconciliation_app.config["ADMIN_TOKEN"])
        post = client.post(
            "/",
            data={
                "reporting_filename": "rapport-2026-11.json",
                "bdc_filename": "convert_bdc_11-2026.json",
            },
        )
        assert post.status_code == 200
        index_html = post.get_data(as_text=True)
        assert "/export/pdf?token=" in index_html
        assert "Exporter PDF" in index_html

        token_match = re.search(r"/export/html\?token=([a-f0-9]+)", index_html)
        assert token_match is not None
        token = token_match.group(1)

        html_export = client.get(f"/export/html?token={token}&month=2026-11")
        assert html_export.status_code == 200
        assert "Le détail fichier par fichier figure dans l’annexe 1." not in html_export.get_data(
            as_text=True
        )

        pdf = client.get(f"/export/pdf?token={token}&month=2026-11")
        assert pdf.status_code == 200
        assert pdf.headers["Content-Type"] == "application/pdf"
        assert "filename=rapprochement-2026-11.pdf" in pdf.headers["Content-Disposition"]
        assert pdf.data.startswith(b"%PDF-")
        assert len(pdf.data) > 1000

        assert client.get("/export/pdf").status_code == 400
