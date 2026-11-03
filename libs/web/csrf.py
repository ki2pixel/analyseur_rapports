"""CSRF protection setup."""

from __future__ import annotations

from flask import Flask
from flask_wtf.csrf import CSRFProtect, generate_csrf

csrf = CSRFProtect()


def init_csrf(app: Flask) -> CSRFProtect:
    app.config.setdefault("WTF_CSRF_TIME_LIMIT", None)
    app.config.setdefault("WTF_CSRF_HEADERS", ["X-CSRFToken", "X-CSRF-Token"])
    csrf.init_app(app)

    @app.context_processor
    def _inject_csrf_token():
        return {"csrf_token": generate_csrf}

    return csrf
