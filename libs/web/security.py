"""One-shot Flask app hardening bootstrap."""

from __future__ import annotations

from flask import Flask, g, request

from libs.web.assets import register_asset_helpers
from libs.web.auth import install_auth_guard, register_auth_routes
from libs.web.config import apply_security_config
from libs.web.csrf import init_csrf
from libs.web.headers import register_security_headers
from libs.web.logging_setup import bind_request_id, configure_logging


def init_app_security(
    app: Flask,
    *,
    default_port: int | None = None,
    skip_auth: bool = False,
) -> None:
    """Apply secret/token config, CSRF, auth guard, headers, and request logging."""
    configure_logging(app.name)
    apply_security_config(app, default_port=default_port)
    init_csrf(app)
    register_security_headers(app)
    register_asset_helpers(app)

    if not skip_auth:
        register_auth_routes(app)
        install_auth_guard(app)

    @app.before_request
    def _bind_request_id():
        incoming = request.headers.get("X-Request-ID")
        g.request_id = bind_request_id(incoming)

    @app.after_request
    def _echo_request_id(response):
        rid = getattr(g, "request_id", None)
        if rid:
            response.headers.setdefault("X-Request-ID", rid)
        return response
