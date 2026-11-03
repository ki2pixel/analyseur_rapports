"""Static asset URL helpers (cache-busting query string)."""

from __future__ import annotations

from pathlib import Path

from flask import Flask, url_for

_STATIC_MTIME_CACHE: dict[str, str] = {}


def _static_root(app: Flask) -> Path:
    folder = app.static_folder
    if not folder:
        return Path(__file__).resolve().parents[2] / "apps" / "static"
    return Path(folder)


def static_asset_version(app: Flask, filename: str) -> str:
    """Return a short mtime-based version for cache busting."""
    cached = _STATIC_MTIME_CACHE.get(filename)
    if cached is not None:
        return cached
    path = _static_root(app) / filename
    try:
        version = str(int(path.stat().st_mtime))
    except OSError:
        version = "0"
    _STATIC_MTIME_CACHE[filename] = version
    return version


def register_asset_helpers(app: Flask) -> None:
    @app.context_processor
    def _inject_asset_helpers():
        def static_url(filename: str) -> str:
            version = static_asset_version(app, filename)
            return url_for("static", filename=filename, v=version)

        from libs.web.config import get_service_urls

        return {
            "static_url": static_url,
            "service_urls": get_service_urls(),
        }
