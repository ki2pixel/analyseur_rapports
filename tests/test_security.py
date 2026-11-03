"""Security and hardening regression tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.reconciliation.app import app as reconciliation_app
from apps.bdc_converter.app import app as bdc_app
from libs.pipeline.bdc_to_batch import parse_bdc_json_to_orders
from libs.web.paths import PathValidationError, safe_join_filename


@pytest.fixture()
def recon_client(tmp_path, monkeypatch):
    reporting_dir = tmp_path / "reporting"
    bdc_dir = tmp_path / "bdc"
    reporting_dir.mkdir()
    bdc_dir.mkdir()
    (reporting_dir / "rapport-2026-03.json").write_text(
        json.dumps({"month": "2026-03", "report": []}), encoding="utf-8"
    )
    (bdc_dir / "convert_bdc_03-2026.json").write_text("[]", encoding="utf-8")

    monkeypatch.setattr("apps.reconciliation.app.REPORTING_DIR", reporting_dir)
    monkeypatch.setattr("apps.reconciliation.app.BDC_DIR", bdc_dir)

    reconciliation_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with reconciliation_app.test_client() as client:
        client.set_cookie("admin_token", reconciliation_app.config["ADMIN_TOKEN"])
        yield client, reporting_dir, bdc_dir


def test_reconciliation_rejects_path_traversal(recon_client):
    client, _, _ = recon_client
    response = client.post(
        "/",
        data={
            "reporting_filename": "../secrets.json",
            "bdc_filename": "convert_bdc_03-2026.json",
        },
    )
    assert response.status_code == 200
    assert b"invalide" in response.data.lower() or b"autoris" in response.data.lower()


def test_reconciliation_rejects_unknown_filename(recon_client):
    client, _, _ = recon_client
    response = client.post(
        "/",
        data={
            "reporting_filename": "rapport-1999-01.json",
            "bdc_filename": "convert_bdc_03-2026.json",
        },
    )
    assert response.status_code == 200
    assert b"autoris" in response.data.lower() or b"Fichier" in response.data


def test_safe_join_filename_confinement(tmp_path):
    base = tmp_path / "base"
    base.mkdir()
    (base / "ok.json").write_text("{}", encoding="utf-8")
    with pytest.raises(PathValidationError):
        safe_join_filename(base, "../ok.json", allowed_names={"ok.json"})
    resolved = safe_join_filename(base, "ok.json", allowed_names={"ok.json"})
    assert resolved.name == "ok.json"


def test_bdc_post_without_token_is_unauthorized():
    bdc_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with bdc_app.test_client() as client:
        response = client.post("/api/convert", json={})
        assert response.status_code == 401


def test_security_headers_present():
    bdc_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with bdc_app.test_client() as client:
        client.set_cookie("admin_token", bdc_app.config["ADMIN_TOKEN"])
        response = client.get("/")
        assert response.headers.get("X-Content-Type-Options") == "nosniff"
        assert response.headers.get("X-Frame-Options") == "DENY"
        csp = response.headers.get("Content-Security-Policy", "")
        assert "Content-Security-Policy" in response.headers
        assert "fonts.googleapis.com" not in csp
        assert "font-src 'self'" in csp


def test_parse_articles_without_description():
    orders = parse_bdc_json_to_orders(
        [
            {
                "numero_bdc": "BDC-1",
                "reference": "Lot 12",
                "articles": [
                    {"quantite": "1"},
                    {"description": "vidéo courte", "quantite": "2"},
                ],
            }
        ]
    )
    assert len(orders) == 1
    assert len(orders[0].articles) == 1
    assert orders[0].articles[0].description == "vidéo courte"
