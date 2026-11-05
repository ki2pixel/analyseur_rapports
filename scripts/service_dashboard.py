#!/usr/bin/env python3
"""
Simple web dashboard that exposes buttons to start/stop the four Flask services.

Usage:
    python scripts/service_dashboard.py

Environment variables:
    SERVICE_DASHBOARD_HOST   Hostname for the dashboard (default: 127.0.0.1)
    SERVICE_DASHBOARD_PORT   Port for the dashboard (default: 5500)
    SERVICE_DASHBOARD_BROWSER  Auto-open browser when set to "1" (default: 1)
    BDC_URL / REPORTING_URL / RECONCILIATION_URL / BILLING_URL  Service open links
"""

from __future__ import annotations

import atexit
import os
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

from flask import Flask, jsonify, render_template, request

ROOT_DIR = Path(__file__).resolve().parents[1]
SHARED_STATIC_DIR = ROOT_DIR / "apps" / "static"
TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
HOST = os.environ.get("SERVICE_DASHBOARD_HOST", "127.0.0.1")
PORT = int(os.environ.get("SERVICE_DASHBOARD_PORT", "5500"))
AUTO_OPEN_BROWSER = os.environ.get("SERVICE_DASHBOARD_BROWSER", "1") != "0"

app = Flask(
    __name__,
    static_folder=str(SHARED_STATIC_DIR),
    static_url_path="/static",
    template_folder=str(TEMPLATES_DIR),
)

# Ensure repo root is importable when launched as a script.
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from libs.web import get_service_urls, init_app_security  # noqa: E402

init_app_security(app, default_port=PORT)


@dataclass
class ServiceDefinition:
    key: str
    label: str
    command: list[str]
    url: str
    description: str


def _build_services() -> Dict[str, ServiceDefinition]:
    urls = get_service_urls()
    return {
        "bdc": ServiceDefinition(
            key="bdc",
            label="Convertisseur BDC",
            command=["make", "serve-bdc"],
            url=urls.bdc,
            description="Convertit des PDF BDC en batches JSON.",
        ),
        "reporting": ServiceDefinition(
            key="reporting",
            label="Reporting",
            command=["make", "serve-reporting"],
            url=urls.reporting,
            description="Génère et affiche les rapports mensuels.",
        ),
        "reconciliation": ServiceDefinition(
            key="reconciliation",
            label="Rapprochement",
            command=["make", "serve-reconciliation"],
            url=urls.reconciliation,
            description="Compare les exports reporting ↔ BDC.",
        ),
        "billing": ServiceDefinition(
            key="billing",
            label="Facturation",
            command=["make", "serve-billing"],
            url=urls.billing,
            description="Prépare et versionne la base de facturation à partir des BDC.",
        ),
    }


SERVICES: Dict[str, ServiceDefinition] = _build_services()


@dataclass
class ServiceProcess:
    process: subprocess.Popen
    started_at: float


service_processes: Dict[str, ServiceProcess] = {}
process_lock = threading.Lock()


def _is_running(proc: ServiceProcess | None) -> bool:
    if not proc:
        return False
    if proc.process.poll() is None:
        return True
    return False


def _ensure_process_entry(name: str) -> Optional[ServiceProcess]:
    proc = service_processes.get(name)
    if proc and proc.process.poll() is not None:
        service_processes.pop(name, None)
        return None
    return proc


def _start_service(name: str) -> dict:
    definition = SERVICES[name]
    with process_lock:
        proc_entry = _ensure_process_entry(name)
        if proc_entry and _is_running(proc_entry):
            return {"ok": False, "message": "Service déjà lancé."}

        try:
            process = subprocess.Popen(
                definition.command,
                cwd=ROOT_DIR,
                stdout=None,
                stderr=None,
                preexec_fn=os.setsid if hasattr(os, "setsid") else None,
            )
        except FileNotFoundError:
            return {
                "ok": False,
                "message": "Commande introuvable. Assurez-vous que 'make' est installé.",
            }
        service_processes[name] = ServiceProcess(process=process, started_at=time.time())
        return {"ok": True, "message": "Service démarré."}


def _stop_service(name: str) -> dict:
    with process_lock:
        proc_entry = _ensure_process_entry(name)
        if not proc_entry:
            return {"ok": False, "message": "Service non démarré."}

        process = proc_entry.process
        if process.poll() is not None:
            service_processes.pop(name, None)
            return {"ok": False, "message": "Service déjà arrêté."}

        if hasattr(os, "killpg"):
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        else:
            process.terminate()

        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
        finally:
            service_processes.pop(name, None)
        return {"ok": True, "message": "Service arrêté."}


def _status_snapshot() -> list[dict]:
    snapshot = []
    with process_lock:
        for key, definition in SERVICES.items():
            proc_entry = _ensure_process_entry(key)
            running = proc_entry.process.poll() is None if proc_entry else False
            started_at = proc_entry.started_at if running and proc_entry else None
            uptime = time.time() - started_at if started_at else None
            snapshot.append(
                {
                    "key": key,
                    "label": definition.label,
                    "running": running,
                    "url": definition.url,
                    "uptime": round(uptime, 1) if uptime else None,
                }
            )
    return snapshot


@app.get("/")
def index():
    return render_template("dashboard.html", services=SERVICES.values())


@app.get("/status")
def status():
    return jsonify(_status_snapshot())


@app.post("/start/<service_name>")
def start_service_route(service_name: str):
    if service_name not in SERVICES:
        return jsonify({"ok": False, "message": "Service inconnu."}), 404
    payload = _start_service(service_name)
    return jsonify(payload)


@app.post("/stop/<service_name>")
def stop_service_route(service_name: str):
    if service_name not in SERVICES:
        return jsonify({"ok": False, "message": "Service inconnu."}), 404
    payload = _stop_service(service_name)
    status_code = 200 if payload["ok"] else 400
    return jsonify(payload), status_code


def _open_browser():
    time.sleep(1.0)
    url = f"http://{HOST}:{PORT}/"
    try:
        webbrowser.open(url)
    except webbrowser.Error:
        pass


def _cleanup():
    for key in list(service_processes.keys()):
        _stop_service(key)


atexit.register(_cleanup)


def main():
    if AUTO_OPEN_BROWSER:
        threading.Thread(target=_open_browser, daemon=True).start()
    app.run(host=HOST, port=PORT, debug=False)


if __name__ == "__main__":
    main()
