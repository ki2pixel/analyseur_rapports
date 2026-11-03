"""HTTP security headers."""

from __future__ import annotations

from flask import Flask, Response, request

DEFAULT_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "font-src 'self'; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)

# Cache static assets for a day; templates use ?v=mtime for invalidation.
STATIC_CACHE_CONTROL = "public, max-age=86400"


def register_security_headers(app: Flask, *, csp: str = DEFAULT_CSP) -> None:
    @app.after_request
    def _set_security_headers(response: Response) -> Response:
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Content-Security-Policy", csp)
        response.headers.setdefault(
            "Permissions-Policy", "geolocation=(), microphone=(), camera=()"
        )
        if request.path.startswith("/static/"):
            response.headers["Cache-Control"] = STATIC_CACHE_CONTROL
        return response
