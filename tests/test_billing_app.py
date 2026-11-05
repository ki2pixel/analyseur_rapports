"""Flask app tests for the billing service."""

from __future__ import annotations

import io
import json
import os

import pytest

from apps.billing.app import app as billing_app
from apps.reconciliation.app import app as reconciliation_app


MINIMAL_REPORTING = {
    "month": "2026-10",
    "report": [
        {
            "id_camille": "1",
            "person": "Camille",
            "categories": {
                "short": {
                    "count": 3,
                    "files": ["a.mp4", "b.mp4", "c.mp4"],
                },
                "standard": {"count": 1, "files": ["d.mp4"]},
                "medium": {"count": 0, "files": []},
                "long": {"count": 0, "files": []},
            },
        }
    ],
}

MINIMAL_BDC = [
    {
        "numero_bdc": "BDC-1",
        "date_livraison": "01/10/2026",
        "reference": "LOTS 1",
        "references": ["LOTS 1"],
        "articles": [
            {
                "description": "Recadrage - vidéo courte (moins de 2 min)",
                "quantite": "2,00",
                "taux": "4,00",
                "montant": "8,00",
            },
            {
                "description": "Recadrage - vidéo standard (entre 3 et 5 minutes)",
                "quantite": "1,00",
                "taux": "8,00",
                "montant": "8,00",
            },
        ],
        "totals": {"total": "16,00"},
        "status": "ok",
        "warnings": [],
    }
]


@pytest.fixture()
def billing_client(tmp_path, monkeypatch):
    reporting_dir = tmp_path / "reporting"
    bdc_dir = tmp_path / "bdc"
    billing_dir = tmp_path / "billing"
    reconciliation_dir = tmp_path / "reconciliation"
    reporting_dir.mkdir()
    bdc_dir.mkdir()
    reconciliation_dir.mkdir()
    (reporting_dir / "rapport-2026-10.json").write_text(
        json.dumps(MINIMAL_REPORTING), encoding="utf-8"
    )
    (bdc_dir / "convert_bdc_10-2026.json").write_text(json.dumps(MINIMAL_BDC), encoding="utf-8")

    monkeypatch.setattr("apps.billing.app.REPORTING_DIR", reporting_dir)
    monkeypatch.setattr("apps.billing.app.BDC_DIR", bdc_dir)
    monkeypatch.setattr("apps.billing.app.BILLING_DATA_DIR", billing_dir)
    monkeypatch.setattr("apps.billing.app.RECONCILIATION_DIR", reconciliation_dir)

    billing_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with billing_app.test_client() as client:
        client.set_cookie("admin_token", billing_app.config["ADMIN_TOKEN"])
        yield client, tmp_path


def _create_and_freeze(client):
    response = client.post(
        "/draft",
        data={"month": "2026-10", "billing_basis": "actual_capped", "vat_rate": "0.00"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "Version 1" in response.get_data(as_text=True)

    response = client.post(
        "/freeze",
        data={
            "month": "2026-10",
            "version": "1",
            "target_status": "envoyé_pour_vérification",
            "reason": "Envoi pour vérification",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    return response


def test_index_renders_empty_state_and_sources(billing_client):
    client, _ = billing_client
    response = client.get("/?month=2026-10")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Acte 4 · Facturation" in html
    assert "Calculer le premier brouillon" in html
    assert "rapport-2026-10.json" in html
    assert "convert_bdc_10-2026.json" in html
    assert "data-month-form" in html


def test_guide_route_renders_markdown(billing_client):
    client, _ = billing_client
    response = client.get("/guide")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Guide de facturation" in html
    assert "Mois avec écarts" in html
    assert '<pre class="guide-pre">' in html


def test_create_draft_and_exports(billing_client):
    client, _ = billing_client
    response = client.post(
        "/draft",
        data={"month": "2026-10", "billing_basis": "actual_capped", "vat_rate": "0.00"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Version 1" in html
    assert "20,00 €" in html
    assert "Lots réalisés sans BDC" in html

    exported = client.get("/export/html?month=2026-10&version=1")
    assert exported.status_code == 200
    body = exported.get_data(as_text=True)
    assert "Document de préparation" in body
    assert "ne constitue pas une facture" in body
    assert "/static/" not in body
    assert "<script" not in body
    assert "data:font/woff2;base64," in body

    csv_response = client.get("/export/csv?month=2026-10&version=1")
    assert csv_response.status_code == 200
    csv_body = csv_response.get_data(as_text=True)
    assert csv_body.startswith("\ufeffLibellé;Quantité")
    assert "BDC-1" in csv_body
    assert "20,00" in csv_body

    pdf_response = client.get("/export/pdf?month=2026-10&version=1")
    assert pdf_response.status_code == 200
    assert pdf_response.headers["Content-Type"] == "application/pdf"
    assert pdf_response.data.startswith(b"%PDF")


def test_recompute_preserves_manual_lines_on_draft(billing_client):
    client, _ = billing_client
    client.post(
        "/draft",
        data={"month": "2026-10", "billing_basis": "actual_capped", "vat_rate": "0.00"},
        follow_redirects=True,
    )
    client.post(
        "/lines",
        data={
            "month": "2026-10",
            "version": "1",
            "label": "Frais de mise en page",
            "quantity": "1",
            "unit_price": "10,00",
            "note": "manuel",
        },
        follow_redirects=True,
    )

    response = client.post(
        "/recompute",
        data={"month": "2026-10", "billing_basis": "actual_capped", "vat_rate": "0.00"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Frais de mise en page" in html
    assert "Version 1" in html
    assert "30,00 €" in html


def test_recompute_accepts_rate_override_for_missing_category(billing_client):
    client, tmp_path = billing_client
    report_path = tmp_path / "reporting" / "rapport-2026-10.json"
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    payload["report"][0]["categories"]["medium"] = {
        "count": 2,
        "files": ["medium-1.mp4", "medium-2.mp4"],
    }
    report_path.write_text(json.dumps(payload), encoding="utf-8")

    first = client.post(
        "/draft",
        data={"month": "2026-10", "billing_basis": "actual_capped", "vat_rate": "0.00"},
        follow_redirects=True,
    )
    assert "Aucun taux connu" in first.get_data(as_text=True)

    second = client.post(
        "/recompute",
        data={
            "month": "2026-10",
            "billing_basis": "actual_capped",
            "vat_rate": "0.00",
            "rate_short": "",
            "rate_standard": "",
            "rate_medium": "12,00",
            "rate_long": "",
        },
        follow_redirects=True,
    )
    assert second.status_code == 200
    html = second.get_data(as_text=True)
    assert "Aucun taux connu" not in html
    assert "44,00 €" in html

    version = json.loads(
        (tmp_path / "billing" / "2026-10" / "versions" / "v1.json").read_text(encoding="utf-8")
    )
    assert version["settings"]["rate_overrides"]["medium"] == "12.00"


def test_frozen_version_is_read_only_and_new_version_is_allowed(billing_client):
    client, _ = billing_client
    _create_and_freeze(client)

    updated = client.post(
        "/lines/bdc:BDC-1",
        data={
            "month": "2026-10",
            "version": "1",
            "label": "Tentative",
            "quantity": "1",
            "unit_price": "4,00",
            "included": "on",
        },
        follow_redirects=True,
    )
    assert "gelée" in updated.get_data(as_text=True)

    new_version = client.post(
        "/new-version",
        data={"month": "2026-10", "reason": "Régularisation client"},
        follow_redirects=True,
    )
    assert new_version.status_code == 200
    html = new_version.get_data(as_text=True)
    assert "Version 2" in html
    assert "Régularisation client" in html

    updated = client.post(
        "/lines/bdc:BDC-1",
        data={
            "month": "2026-10",
            "version": "2",
            "label": "BDC-1 ajusté",
            "quantity": "1",
            "unit_price": "10,00",
            "included": "on",
            "note": "Après échange client",
        },
        follow_redirects=True,
    )
    assert updated.status_code == 200
    assert "Ligne mise à jour" in updated.get_data(as_text=True)

    export = client.get("/export/html?month=2026-10&version=2")
    assert export.status_code == 200
    body = export.get_data(as_text=True)
    assert "Évolution depuis la version 1" in body
    assert "BDC-1 ajusté" in body
    assert "Modifiée" in body


def test_invoice_metadata_and_status_transitions(billing_client):
    client, tmp_path = billing_client
    _create_and_freeze(client)

    invoice = client.post(
        "/invoice",
        data={
            "month": "2026-10",
            "version": "1",
            "invoice_number": "KD202611-45",
            "issued_on": "2026-11-03",
            "invoice_note": "",
        },
        follow_redirects=True,
    )
    assert invoice.status_code == 200
    assert "KD202611-45" in invoice.get_data(as_text=True)

    validated = client.post(
        "/status",
        data={
            "month": "2026-10",
            "version": "1",
            "target_status": "validé_client",
            "acknowledge": "no_snapshot",
        },
        follow_redirects=True,
    )
    assert validated.status_code == 200
    assert "Validée client" in validated.get_data(as_text=True)

    emitted = client.post(
        "/status",
        data={
            "month": "2026-10",
            "version": "1",
            "target_status": "émis",
            "override_reason": "Régularisation client acceptée",
        },
        follow_redirects=True,
    )
    assert emitted.status_code == 200
    assert "Émise" in emitted.get_data(as_text=True)

    version = json.loads(
        (tmp_path / "billing" / "2026-10" / "versions" / "v1.json").read_text(encoding="utf-8")
    )
    assert version["status"] == "émis"
    assert version["invoice"]["number"] == "KD202611-45"


def test_invoice_pdf_attachment_is_validated_and_downloadable(billing_client):
    client, tmp_path = billing_client
    _create_and_freeze(client)

    uploaded = client.post(
        "/attachment",
        data={
            "month": "2026-10",
            "version": "1",
            "invoice_pdf": (io.BytesIO(b"%PDF-1.4\nfake invoice"), "facture.pdf"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert uploaded.status_code == 200
    assert "PDF Indy attaché" in uploaded.get_data(as_text=True)
    stored = tmp_path / "billing" / "2026-10" / "versions" / "v1-facture.pdf"
    assert stored.is_file()

    download = client.get("/invoice-pdf?month=2026-10&version=1")
    assert download.status_code == 200
    assert download.data.startswith(b"%PDF")

    rejected = client.post(
        "/attachment",
        data={
            "month": "2026-10",
            "version": "1",
            "invoice_pdf": (io.BytesIO(b"not a pdf"), "facture.pdf"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert rejected.status_code == 200
    assert "PDF est invalide" in rejected.get_data(as_text=True)


def test_pdf_newer_than_bdc_json_warns(billing_client):
    client, tmp_path = billing_client
    json_path = tmp_path / "bdc" / "convert_bdc_10-2026.json"
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    payload[0]["source_filename"] = "BDC-1.pdf"
    json_path.write_text(json.dumps(payload), encoding="utf-8")

    pdf_dir = tmp_path / "bdc" / "pdfs"
    pdf_dir.mkdir()
    pdf_path = pdf_dir / "BDC-1.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")
    future = json_path.stat().st_mtime + 10
    os.utime(pdf_path, (future, future))

    response = client.post(
        "/draft",
        data={"month": "2026-10", "billing_basis": "actual_capped", "vat_rate": "0.00"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "plus récent que le JSON BDC" in response.get_data(as_text=True)


def test_post_without_admin_token_is_rejected(billing_client):
    _, _ = billing_client
    with billing_app.test_client() as anonymous:
        response = anonymous.post("/draft", data={"month": "2026-10"})
        assert response.status_code == 401


def test_invalid_month_is_rejected(billing_client):
    client, _ = billing_client
    response = client.get("/?month=2026-13")
    assert response.status_code == 400
    assert "Format de mois invalide" in response.get_data(as_text=True)


def test_reconciliation_handoff_writes_snapshot(tmp_path, monkeypatch):
    reporting_dir = tmp_path / "reporting"
    bdc_dir = tmp_path / "bdc"
    handoff_dir = tmp_path / "reconciliation"
    reporting_dir.mkdir()
    bdc_dir.mkdir()
    (reporting_dir / "rapport-2026-10.json").write_text(
        json.dumps(MINIMAL_REPORTING), encoding="utf-8"
    )
    (bdc_dir / "convert_bdc_10-2026.json").write_text(json.dumps(MINIMAL_BDC), encoding="utf-8")

    monkeypatch.setattr("apps.reconciliation.app.REPORTING_DIR", reporting_dir)
    monkeypatch.setattr("apps.reconciliation.app.BDC_DIR", bdc_dir)
    monkeypatch.setattr("apps.reconciliation.app.HANDOFF_DIR", handoff_dir)
    reconciliation_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)

    with reconciliation_app.test_client() as client:
        client.set_cookie("admin_token", reconciliation_app.config["ADMIN_TOKEN"])
        stale = client.post(
            "/handoff/billing",
            data={
                "month": "2026-10",
                "token": "stale-token",
                "reporting_name": "rapport-2026-10.json",
                "bdc_name": "convert_bdc_10-2026.json",
            },
            follow_redirects=True,
        )
        assert "Session expirée" in stale.get_data(as_text=True)
        assert not (handoff_dir / "2026-10.json").exists()

        response = client.post(
            "/handoff/billing",
            data={
                "month": "2026-10",
                "reporting_name": "rapport-2026-10.json",
                "bdc_name": "convert_bdc_10-2026.json",
            },
            follow_redirects=True,
        )
        assert response.status_code == 200
        html = response.get_data(as_text=True)
        assert "Analyse transmise à la facturation" in html
        assert "Ouvrir l’acte 4 · Facturation" in html

    snapshot_path = handoff_dir / "2026-10.json"
    assert snapshot_path.is_file()
    payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert payload["month"] == "2026-10"
    assert payload["analysis"]["cluster_results"]
    assert payload["sources"]["reporting"]["sha256"]
    assert payload["sources"]["bdc"]["sha256"]
