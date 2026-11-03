"""Shared-secret admin authentication via header or cookie."""

from __future__ import annotations

import hmac
import os
from functools import wraps
from typing import Any, Callable

from flask import Flask, Request, current_app, jsonify, redirect, render_template_string, request

ADMIN_COOKIE = "admin_token"
ADMIN_HEADER = "X-Admin-Token"
LOOPBACK_ADDRS = frozenset({"127.0.0.1", "::1", "localhost"})

_AUTH_PAGE = """
<!doctype html>
<html lang="fr">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Authentification</title>
  <link rel="stylesheet" href="/static/css/global.css" />
</head>
<body class="auth-page">
  <div class="main-content auth-shell">
    <h1>Accès protégé</h1>
    <p>Saisissez le jeton défini dans <code>ADMIN_TOKEN</code> (fichier <code>.env</code>).</p>
    <p class="muted muted--tight">
      En accès local (<code>127.0.0.1</code>), l'auth est normalement automatique.
      Si vous voyez cette page, vérifiez <code>TRUST_LOOPBACK_AUTH</code> ou saisissez le jeton une fois.
    </p>
    {% if error %}<p class="auth-error">{{ error }}</p>{% endif %}
    <form method="post" action="{{ auth_url }}">
      <input type="hidden" name="csrf_token" value="{{ csrf_token() }}"/>
      <input type="hidden" name="next" value="{{ next_url }}"/>
      <label for="token">ADMIN_TOKEN</label>
      <input id="token" name="token" type="password" autocomplete="current-password" required class="mt-sm mb-md"/>
      <button type="submit" class="btn btn-primary">Continuer</button>
    </form>
  </div>
</body>
</html>
"""


def _truthy(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def trust_loopback_auth(app: Flask | None = None) -> bool:
    """Trust 127.0.0.1/::1 without cookie/header (local-only convenience).

    Default: enabled outside tests. Disable with TRUST_LOOPBACK_AUTH=0.
    """
    flask_app = app or current_app
    if flask_app.config.get("TESTING"):
        return _truthy(os.environ.get("TRUST_LOOPBACK_AUTH"))
    raw = os.environ.get("TRUST_LOOPBACK_AUTH")
    if raw is None:
        return True
    return _truthy(raw)


def is_loopback_request(req: Request) -> bool:
    addr = (req.remote_addr or "").strip().lower()
    return addr in LOOPBACK_ADDRS


def extract_admin_token(req: Request) -> str | None:
    header = req.headers.get(ADMIN_HEADER)
    if header and header.strip():
        return header.strip()
    cookie = req.cookies.get(ADMIN_COOKIE)
    if cookie and cookie.strip():
        return cookie.strip()
    form_token = req.form.get("token") if req.form else None
    if form_token and form_token.strip():
        return form_token.strip()
    return None


def token_matches(provided: str | None, expected: str) -> bool:
    if not provided or not expected:
        return False
    return hmac.compare_digest(provided, expected)


def is_authenticated(req: Request, expected: str, *, app: Flask | None = None) -> bool:
    flask_app = app
    if flask_app is None:
        try:
            flask_app = current_app._get_current_object()  # type: ignore[attr-defined]
        except RuntimeError:
            flask_app = None
    if flask_app is not None and trust_loopback_auth(flask_app) and is_loopback_request(req):
        return True
    return token_matches(extract_admin_token(req), expected)


def register_auth_routes(app: Flask) -> None:
    @app.get("/auth")
    def auth_get():
        next_url = request.args.get("next") or "/"
        if trust_loopback_auth(app) and is_loopback_request(request):
            response = redirect(next_url)
            response.set_cookie(
                ADMIN_COOKIE,
                app.config["ADMIN_TOKEN"],
                httponly=True,
                samesite="Strict",
                secure=False,
            )
            return response
        return render_template_string(
            _AUTH_PAGE,
            error=None,
            next_url=next_url,
            auth_url="/auth",
        )

    @app.post("/auth")
    def auth_post():
        expected = app.config["ADMIN_TOKEN"]
        provided = request.form.get("token", "")
        next_url = request.form.get("next") or "/"
        if not token_matches(provided, expected):
            return (
                render_template_string(
                    _AUTH_PAGE,
                    error="Jeton invalide.",
                    next_url=next_url,
                    auth_url="/auth",
                ),
                401,
            )
        response = redirect(next_url)
        response.set_cookie(
            ADMIN_COOKIE,
            provided,
            httponly=True,
            samesite="Strict",
            secure=False,
        )
        return response


def install_auth_guard(app: Flask, *, exempt_endpoints: set[str] | None = None) -> None:
    exempt = set(exempt_endpoints or set())
    exempt.update({"auth_get", "auth_post", "static"})

    @app.before_request
    def _require_admin():
        if request.endpoint in exempt or request.endpoint is None:
            return None
        if str(request.endpoint).endswith(".static") or request.endpoint == "static":
            return None
        expected = app.config.get("ADMIN_TOKEN")
        if not expected:
            return jsonify({"error": "Authentification non configurée."}), 500
        if is_authenticated(request, expected, app=app):
            return None
        if request.accept_mimetypes.accept_html and request.method in {"GET", "HEAD"}:
            return redirect(f"/auth?next={request.full_path.rstrip('?')}")
        return (
            jsonify(
                {
                    "error": "Authentification requise.",
                    "hint": f"Fournir {ADMIN_HEADER} ou cookie {ADMIN_COOKIE}.",
                }
            ),
            401,
        )


def require_admin(view: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(view)
    def wrapped(*args: Any, **kwargs: Any):
        expected = current_app.config.get("ADMIN_TOKEN", "")
        if not is_authenticated(request, expected, app=current_app):
            return jsonify({"error": "Authentification requise."}), 401
        return view(*args, **kwargs)

    return wrapped
