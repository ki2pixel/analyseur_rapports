"""Environment-driven security and runtime settings."""

from __future__ import annotations

import hashlib
import os
import sys
from dataclasses import dataclass
from pathlib import Path

_DOTENV_LOADED = False


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def load_env_file(*, override: bool = False) -> Path | None:
    """Load `.env` from the repository root if present.

    Existing process environment variables always win unless ``override=True``.
    """
    global _DOTENV_LOADED
    if _DOTENV_LOADED and not override:
        return _project_root() / ".env"

    env_path = _project_root() / ".env"
    try:
        from dotenv import load_dotenv
    except ImportError:
        _DOTENV_LOADED = True
        return env_path if env_path.is_file() else None

    if env_path.is_file():
        load_dotenv(env_path, override=override)
    _DOTENV_LOADED = True
    return env_path if env_path.is_file() else None


@dataclass(frozen=True)
class RunSettings:
    host: str
    port: int
    debug: bool


@dataclass(frozen=True)
class ServiceUrls:
    bdc: str
    reporting: str
    reconciliation: str
    billing: str


def _truthy(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def get_admin_token() -> str | None:
    load_env_file()
    token = os.environ.get("ADMIN_TOKEN")
    if token is None:
        return None
    token = token.strip()
    return token or None


def get_secret_key(*, allow_fallback: bool = False) -> str:
    load_env_file()
    secret = os.environ.get("FLASK_SECRET_KEY") or os.environ.get("RECONCILIATION_SECRET_KEY")
    if secret and secret.strip() and secret.strip() != "dev-reconciliation":
        return secret.strip()

    admin = get_admin_token()
    if admin:
        # Stable derived key so CSRF/cookies work without a separate secret.
        digest = hashlib.sha256(f"flask-secret:{admin}".encode("utf-8")).hexdigest()
        return digest

    if allow_fallback:
        return "dev-insecure-only-for-tests"

    raise RuntimeError(
        "FLASK_SECRET_KEY (ou ADMIN_TOKEN) doit être défini hors mode test "
        "(fichier .env à la racine du dépôt ou variables d'environnement)."
    )


def require_admin_token(*, testing: bool = False) -> str:
    load_env_file()
    token = get_admin_token()
    if token:
        return token
    if testing or "pytest" in sys.modules:
        return "test-admin-token"
    raise RuntimeError(
        "ADMIN_TOKEN doit être défini pour démarrer les services "
        "(fichier .env à la racine du dépôt, sauf en mode test)."
    )


def get_run_settings(default_port: int) -> RunSettings:
    load_env_file()
    host = os.environ.get("APP_HOST", "127.0.0.1").strip() or "127.0.0.1"
    port_raw = os.environ.get("PORT") or os.environ.get("APP_PORT") or str(default_port)
    try:
        port = int(port_raw)
    except ValueError:
        port = default_port
    debug = _truthy(os.environ.get("APP_DEBUG")) or _truthy(os.environ.get("FLASK_DEBUG"))
    return RunSettings(host=host, port=port, debug=debug)


def _normalize_service_url(raw: str | None, default: str) -> str:
    value = (raw or "").strip() or default
    return value if value.endswith("/") else f"{value}/"


def get_service_urls() -> ServiceUrls:
    """Cross-app navigation URLs (topbar + dashboard)."""
    load_env_file()
    return ServiceUrls(
        bdc=_normalize_service_url(os.environ.get("BDC_URL"), "http://127.0.0.1:5000/"),
        reporting=_normalize_service_url(os.environ.get("REPORTING_URL"), "http://127.0.0.1:5010/"),
        reconciliation=_normalize_service_url(
            os.environ.get("RECONCILIATION_URL"), "http://127.0.0.1:5030/"
        ),
        billing=_normalize_service_url(os.environ.get("BILLING_URL"), "http://127.0.0.1:5040/"),
    )


def apply_security_config(app, *, default_port: int | None = None) -> str:
    """Configure secret key and admin token on a Flask app. Returns the admin token."""
    load_env_file()
    testing = bool(app.config.get("TESTING"))
    admin_token = require_admin_token(testing=testing)
    app.config["ADMIN_TOKEN"] = admin_token
    app.config["SECRET_KEY"] = get_secret_key(allow_fallback=testing)
    app.secret_key = app.config["SECRET_KEY"]
    if default_port is not None:
        app.config["APP_DEFAULT_PORT"] = default_port
    return admin_token
