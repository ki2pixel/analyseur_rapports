import json

import pytest

from app import app


@pytest.fixture()
def test_client():
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with app.test_client() as client:
        client.set_cookie("admin_token", app.config["ADMIN_TOKEN"])
        yield client


def test_convert_endpoint_success(monkeypatch, test_client, tmp_path):
    captured = {}

    def fake_convert_folder(input_folder, output_file, progress_callback, persist_json):
        captured["input_folder"] = input_folder
        captured["output_file"] = output_file
        captured["progress"] = []
        progress_callback(filename="BDC-1.pdf", index=1, total=2)
        progress_callback(filename="BDC-2.pdf", index=2, total=2)
        return {
            "output_file": "/tmp/custom.json",
            "processed_files": 3,
            "results": [
                {
                    "numero_bdc": "BDC-TEST",
                    "date_commande": "15/03/2026",
                    "date_livraison": "15/03/2026",
                    "reference": "LOTS 1",
                    "references": ["LOTS 1"],
                    "articles": [
                        {
                            "description": "Recadrage - vidéo courte (moins de 2 min)",
                            "quantite": "2,00",
                            "unite": "Unité",
                            "taux": "4,00",
                            "montant": "8,00",
                        }
                    ],
                    "totals": {"subtotal": "8,00", "total": "8,00"},
                    "status": "ok",
                    "warnings": [],
                    "source_filename": "BDC-1.pdf",
                }
            ],
        }

    monkeypatch.setattr("app.convert_folder", fake_convert_folder)
    monkeypatch.setattr("app.BASE_DIR", tmp_path)
    monkeypatch.setattr("app.BATCH_OUTPUT_DIR", tmp_path / "batches")
    (tmp_path / "batches").mkdir(parents=True, exist_ok=True)

    response = test_client.post("/api/convert", json={})
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["processed_files"] == 3
    assert captured["output_file"] == ""
    assert payload["output_file"].endswith("convert_bdc_03-2026.json")
    assert payload["progress"][0]["filename"] == "BDC-1.pdf"
    assert payload["progress"][-1]["percent"] == 100.0
    assert payload["batch"]["batch_id"] == "2026-03"


def test_convert_endpoint_missing_pdfs(monkeypatch, test_client):
    def fake_convert_folder(*_, **__):
        raise FileNotFoundError("Aucun PDF trouvé.")

    monkeypatch.setattr("app.convert_folder", fake_convert_folder)

    response = test_client.post("/api/convert", json={})
    payload = response.get_json()

    assert response.status_code == 400
    assert "Aucun PDF" in payload["error"]


def test_convert_requires_auth(monkeypatch):
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with app.test_client() as client:
        response = client.post("/api/convert", json={})
        assert response.status_code == 401


def _isolate_outputs(monkeypatch, tmp_path):
    monkeypatch.setattr("app.BASE_DIR", tmp_path)
    monkeypatch.setattr("app.BATCH_OUTPUT_DIR", tmp_path / "batches")
    (tmp_path / "batches").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _mock_convert(monkeypatch, results, processed=None):
    def fake_convert_folder(input_folder, output_file, progress_callback, persist_json):
        total = processed or len(results) or 1
        progress_callback(filename="BDC-1.pdf", index=1, total=total)
        return {
            "output_file": "",
            "processed_files": total,
            "successful_files": sum(
                1 for result in results if result.get("status", "ok") != "failed"
            ),
            "failed_files": [],
            "failed_documents": [
                result["source_filename"] for result in results if result.get("status") == "failed"
            ],
            "results": results,
        }

    monkeypatch.setattr("app.convert_folder", fake_convert_folder)


def _valid_result(
    filename="BDC-OK.pdf",
    numero="BDC-26-9001",
    date="01/10/2026",
    status="ok",
    warnings=None,
):
    return {
        "numero_bdc": numero,
        "date_commande": date,
        "date_livraison": date,
        "reference": "LOTS 1",
        "references": ["LOTS 1"],
        "articles": [
            {
                "description": "Recadrage - vidéo courte (moins de 2 min)",
                "quantite": "2,00",
                "unite": "Unité",
                "taux": "4,00",
                "montant": "8,00",
            }
        ],
        "totals": {"subtotal": "8,00", "total": "8,00"},
        "status": status,
        "warnings": warnings or [],
        "source_filename": filename,
    }


def _failed_result(filename="BDC-BAD.pdf"):
    return {
        "numero_bdc": None,
        "date_commande": None,
        "date_livraison": None,
        "reference": None,
        "references": [],
        "articles": [],
        "totals": {"subtotal": None, "total": None},
        "status": "failed",
        "warnings": [
            {
                "code": "numero_bdc_absent",
                "severity": "error",
                "message": "Numéro de BDC introuvable.",
            }
        ],
        "source_filename": filename,
    }


def test_convert_endpoint_refuses_all_failed(monkeypatch, test_client, tmp_path):
    _isolate_outputs(monkeypatch, tmp_path)
    _mock_convert(monkeypatch, [_failed_result()])

    response = test_client.post("/api/convert", json={})
    payload = response.get_json()

    assert response.status_code == 400
    assert "Aucun BDC exploitable" in payload["error"]
    assert payload["documents"][0]["status"] == "failed"
    assert not list(tmp_path.glob("convert_bdc_*.json"))


def test_convert_endpoint_refuses_undetermined_month(monkeypatch, test_client, tmp_path):
    _isolate_outputs(monkeypatch, tmp_path)
    result = _valid_result()
    result["date_commande"] = None
    result["date_livraison"] = None
    _mock_convert(monkeypatch, [result])

    response = test_client.post("/api/convert", json={})
    payload = response.get_json()

    assert response.status_code == 400
    assert "Mois indéterminé" in payload["error"]
    assert not list(tmp_path.glob("convert_bdc_*.json"))


def test_convert_endpoint_excludes_failed_from_batch(monkeypatch, test_client, tmp_path):
    _isolate_outputs(monkeypatch, tmp_path)
    _mock_convert(
        monkeypatch,
        [
            _valid_result(),
            _failed_result(),
        ],
    )

    response = test_client.post("/api/convert", json={})
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["successful_files"] == 1
    assert payload["failed_documents"] == ["BDC-BAD.pdf"]

    output_path = tmp_path / "convert_bdc_10-2026.json"
    output = json.loads(output_path.read_text(encoding="utf-8"))
    assert [document["status"] for document in output] == ["ok", "failed"]

    batch_path = tmp_path / "batches" / "2026-10.json"
    batch = json.loads(batch_path.read_text(encoding="utf-8"))
    assert [order["numero_bdc"] for order in batch["orders"]] == ["BDC-26-9001"]
    assert any("échec" in warning for warning in batch["warnings"])


def test_convert_endpoint_reports_mixed_months(monkeypatch, test_client, tmp_path):
    _isolate_outputs(monkeypatch, tmp_path)
    _mock_convert(
        monkeypatch,
        [
            _valid_result(filename="BDC-OCT.pdf", numero="BDC-26-9001", date="01/10/2026"),
            _valid_result(filename="BDC-NOV.pdf", numero="BDC-26-9002", date="03/11/2026"),
        ],
    )

    response = test_client.post("/api/convert", json={})
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["month"]["mixed"] is True
    assert payload["month"]["documents_by_month"] == {"2026-10": 1, "2026-11": 1}
    assert payload["batch"]["batch_id"] == "2026-10"

    batch_path = tmp_path / "batches" / "2026-10.json"
    batch = json.loads(batch_path.read_text(encoding="utf-8"))
    assert any("plusieurs mois" in warning for warning in batch["warnings"])


def test_convert_endpoint_surfaces_partial_warnings(monkeypatch, test_client, tmp_path):
    _isolate_outputs(monkeypatch, tmp_path)
    _mock_convert(
        monkeypatch,
        [
            _valid_result(
                status="partial",
                warnings=[
                    {
                        "code": "total_incoherent",
                        "severity": "warning",
                        "message": "Somme des articles ≠ total.",
                    }
                ],
            )
        ],
    )

    response = test_client.post("/api/convert", json={})
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["documents"][0]["status"] == "partial"
    assert payload["documents"][0]["warnings"] == ["Somme des articles ≠ total."]


def test_convert_endpoint_exposes_layout_drift(monkeypatch, test_client, tmp_path):
    _isolate_outputs(monkeypatch, tmp_path)
    result = _valid_result()
    result["layout"] = {
        "fingerprint": "index | description | quantite | remise | taux | montant",
        "expected": "index | description | quantite | taux | montant",
        "changed": True,
        "columns": [],
    }
    _mock_convert(monkeypatch, [result])

    response = test_client.post("/api/convert", json={})
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["documents"][0]["layout"]["changed"] is True
    assert payload["documents"][0]["layout"]["fingerprint"].endswith("remise | taux | montant")


def test_convert_endpoint_tolerates_documents_without_layout(monkeypatch, test_client, tmp_path):
    _isolate_outputs(monkeypatch, tmp_path)
    _mock_convert(monkeypatch, [_valid_result()])

    response = test_client.post("/api/convert", json={})
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["documents"][0]["layout"] is None
