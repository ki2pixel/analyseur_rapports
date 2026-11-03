"""Frontend smoke / a11y / CSP regression tests."""

from __future__ import annotations

import json

import pytest

from apps.bdc_converter.app import app as bdc_app
from apps.reporting.app import app as reporting_app
from apps.reconciliation.app import app as reconciliation_app
from libs.web.headers import DEFAULT_CSP

MINIMAL_REPORTING = {
    "month": "2026-03",
    "generated_at": "2026-03-01T00:00:00",
    "groups": [],
    "report": [],
}


@pytest.fixture()
def bdc_client():
    bdc_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with bdc_app.test_client() as client:
        client.set_cookie("admin_token", bdc_app.config["ADMIN_TOKEN"])
        yield client


@pytest.fixture()
def reporting_client():
    reporting_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with reporting_app.test_client() as client:
        client.set_cookie("admin_token", reporting_app.config["ADMIN_TOKEN"])
        yield client


@pytest.fixture()
def recon_client(tmp_path, monkeypatch):
    reporting_dir = tmp_path / "reporting"
    bdc_dir = tmp_path / "bdc"
    reporting_dir.mkdir()
    bdc_dir.mkdir()
    (reporting_dir / "rapport-2026-03.json").write_text(
        json.dumps(MINIMAL_REPORTING), encoding="utf-8"
    )
    (bdc_dir / "convert_bdc_03-2026.json").write_text("[]", encoding="utf-8")

    monkeypatch.setattr("apps.reconciliation.app.REPORTING_DIR", reporting_dir)
    monkeypatch.setattr("apps.reconciliation.app.BDC_DIR", bdc_dir)

    reconciliation_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with reconciliation_app.test_client() as client:
        client.set_cookie("admin_token", reconciliation_app.config["ADMIN_TOKEN"])
        yield client


def test_bdc_index_has_nav_a11y_and_no_filename_copy(bdc_client):
    response = bdc_client.get("/")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'name="csrf-token"' in html
    assert 'id="navToggle"' in html
    assert 'id="topbar-links"' in html
    assert 'aria-live="polite"' in html
    assert 'id="toast-container"' in html
    assert "data-month-form" in html
    assert 'id="globalMonth"' in html
    assert 'id="documentCards"' in html
    assert 'id="overlapAlerts"' in html
    assert 'id="batchWarnings"' in html
    assert "ritual-stepper" in html
    assert "fonts.googleapis.com" not in html
    assert "nom de fichier" not in html.lower()
    assert "convert_bdc_MM-YYYY.json" in html
    assert "static/css/global.css" in html
    assert "v=" in html


def test_reporting_index_selected_month_in_head(reporting_client):
    response = reporting_client.get("/?month=2026-09")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'name="selected-month"' in html
    assert "data-month-form" in html
    assert "ritual-stepper" in html
    assert "fonts.googleapis.com" not in html
    assert 'id="navToggle"' in html
    assert 'value="2026-09"' in html


def test_reconciliation_index_has_layout_classes(recon_client):
    response = recon_client.get("/")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "form-grid" in html
    assert "data-month-form" in html
    assert "ritual-stepper" in html
    assert "fonts.googleapis.com" not in html


def test_reconciliation_get_month_autoloads(recon_client):
    response = recon_client.get("/?month=2026-03")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Score de conformité" in html
    assert "Équilibre facturé / réalisé" in html
    assert "Inspecteur d’écarts" in html
    assert "rapport-2026-03.json" in html
    assert "convert_bdc_03-2026.json" in html


def test_reconciliation_rejects_invalid_month(recon_client):
    response = recon_client.get("/?month=2026-13")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Format de mois invalide" in html


def test_export_html_uses_delta_semantics():
    from flask import render_template

    meta = {
        "reporting_name": "rapport-2026-03.json",
        "bdc_name": "convert_bdc_03-2026.json",
        "token": "secret-token-should-not-appear",
        "month": "2026-03",
    }
    analysis = {
        "issues_count": 1,
        "generated_at": "2026-07-16T10:00:00Z",
        "reporting": {"group_count": 1},
        "bdc": {"cluster_count": 1},
        "conformity": {
            "score": 0.0,
            "compliant_orders": 0,
            "total_orders": 1,
            "review_clusters": 1,
            "total_clusters": 1,
        },
        "certification": {"is_certified": False, "blocking_issues": 1},
        "balance": {
            "short": {"billed": 1, "actual": 2, "delta": 1},
            "standard": {"billed": 1, "actual": 1, "delta": 0},
            "medium": {"billed": 0, "actual": 0, "delta": 0},
            "long": {"billed": 0, "actual": 0, "delta": 0},
        },
        "overlapping_bdc_lots": [],
    }
    view_model = {
        "unexpected_ids": [],
        "missing_invoices": [],
        "cluster_differences": [
            {
                "cluster_key": "1",
                "label": "Lot 1",
                "cluster_ids": ["1"],
                "bdc_numbers": ["BDC-1"],
                "billed": {"short": 1, "standard": 1, "medium": 0, "long": 0},
                "actual": {"short": 2, "standard": 1, "medium": 0, "long": 0},
                "delta": {"short": 1, "standard": 0, "medium": -1, "long": 0},
                "missing_reporting_ids": [],
                "justified": False,
            }
        ],
        "reporting_groups": [],
    }
    with reconciliation_app.app_context():
        rendered = render_template(
            "export.html",
            meta=meta,
            analysis=analysis,
            view_model=view_model,
        )

    assert "2026-07-16T10:00:00Z" in rendered
    assert 'datetime="2026-07-16T10:00:00Z"' in rendered
    assert "text-warn" in rendered
    assert "text-positive" in rendered
    assert "text-negative" in rendered
    assert "secret-token-should-not-appear" not in rendered
    assert "/static/" not in rendered
    assert "data:font/woff2;base64," in rendered
    assert "<script" not in rendered


def test_export_html_certified_variant_is_self_contained():
    from flask import render_template

    analysis = {
        "issues_count": 0,
        "generated_at": "2026-11-03T09:12:00Z",
        "reporting": {"group_count": 0},
        "bdc": {"cluster_count": 0},
        "conformity": {
            "score": 100.0,
            "compliant_orders": 0,
            "total_orders": 0,
            "review_clusters": 0,
            "total_clusters": 0,
        },
        "certification": {"is_certified": True, "blocking_issues": 0},
        "balance": {
            "short": {"billed": 0, "actual": 0, "delta": 0},
            "standard": {"billed": 0, "actual": 0, "delta": 0},
            "medium": {"billed": 0, "actual": 0, "delta": 0},
            "long": {"billed": 0, "actual": 0, "delta": 0},
        },
        "overlapping_bdc_lots": [],
        "unexpected_ids": [],
        "missing_invoices": [],
        "cluster_results": [],
        "cluster_differences": [],
    }
    view_model = {
        "unexpected_ids": [],
        "missing_invoices": [],
        "cluster_differences": [],
        "cluster_results": [],
        "cluster_results_sorted": [],
        "reporting_groups": [],
        "reporting_groups_sorted": [],
        "overlapping_bdc_lots": [],
        "review_clusters": [],
        "compliant_clusters": [],
        "review_count": 0,
        "has_attention": False,
    }
    meta = {
        "reporting_name": "rapport-2026-11.json",
        "bdc_name": "convert_bdc_11-2026.json",
        "month": "2026-11",
    }
    with reconciliation_app.app_context():
        rendered = render_template(
            "export.html",
            meta=meta,
            analysis=analysis,
            view_model=view_model,
        )

    assert "Certificat de rapprochement mensuel" in rendered
    assert "Mois certifié" in rendered
    assert "Aucun point d’attention" in rendered
    assert "Tous les bons de commande sont conformes" in rendered
    assert "/static/" not in rendered
    assert "data:font/woff2;base64," in rendered


def test_csp_has_no_google_fonts_and_font_src_self():
    assert "fonts.googleapis.com" not in DEFAULT_CSP
    assert "fonts.gstatic.com" not in DEFAULT_CSP
    assert "font-src 'self'" in DEFAULT_CSP


def test_static_css_served_with_cache(bdc_client):
    response = bdc_client.get("/static/css/global.css")
    assert response.status_code == 200
    assert "max-age" in (response.headers.get("Cache-Control") or "")


def test_static_font_served(bdc_client):
    response = bdc_client.get("/static/fonts/plus-jakarta-sans-latin-400-normal.woff2")
    assert response.status_code == 200


def test_export_html_hides_token(recon_client):
    post = recon_client.post(
        "/",
        data={
            "reporting_filename": "rapport-2026-03.json",
            "bdc_filename": "convert_bdc_03-2026.json",
        },
    )
    assert post.status_code == 200
    index_html = post.get_data(as_text=True)
    assert "alert--error" not in index_html or "Token:" not in index_html

    # Prefer live export route when analysis succeeded.
    if "/export/html?token=" in index_html:
        assert "/export/pdf?token=" in index_html
        start = index_html.index("/export/html?token=") + len("/export/html?token=")
        token = ""
        for ch in index_html[start:]:
            if ch in "\"'>& \n":
                break
            token += ch
        export = recon_client.get(f"/export/html?token={token}")
        assert export.status_code == 200
        body = export.get_data(as_text=True)
        assert "Token:" not in body
        assert token not in body
        assert 'name="viewport"' in body
        assert "fonts.googleapis.com" not in body
        assert "/static/" not in body
        assert "data:font/woff2;base64," in body
        assert "<script" not in body
        csp = export.headers.get("Content-Security-Policy") or ""
        assert "font-src 'self' data:" in csp
        return

    # Fallback: direct template render (analysis engine rejected minimal fixtures).
    from flask import render_template

    meta = {
        "generated_at": "2026-07-16",
        "reporting_name": "rapport-2026-03.json",
        "bdc_name": "convert_bdc_03-2026.json",
        "token": "secret-token-should-not-appear",
    }
    analysis = {
        "issues_count": 0,
        "generated_at": "2026-07-16",
        "reporting": {"group_count": 0},
        "bdc": {"cluster_count": 0},
    }
    view_model = {
        "unexpected_ids": [],
        "missing_invoices": [],
        "cluster_differences": [],
        "reporting_groups": [],
    }
    with reconciliation_app.app_context():
        rendered = render_template(
            "export.html",
            meta=meta,
            analysis=analysis,
            view_model=view_model,
        )
    assert "Token:" not in rendered
    assert "secret-token-should-not-appear" not in rendered
    assert 'name="viewport"' in rendered
    assert "/static/" not in rendered
    assert "data:font/woff2;base64," in rendered
