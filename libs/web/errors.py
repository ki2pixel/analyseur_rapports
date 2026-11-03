"""Client-safe error payloads and server-side exception logging."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from libs.web.logging_setup import request_id

_logger = logging.getLogger("analyseur.errors")


def new_error_id() -> str:
    rid = request_id()
    if rid:
        return rid
    return uuid.uuid4().hex[:12]


def log_exception(exc: BaseException, *, message: str = "Unhandled error") -> str:
    error_id = new_error_id()
    _logger.exception("%s error_id=%s", message, error_id, exc_info=exc)
    return error_id


def client_error_payload(
    *,
    message: str = "Une erreur interne est survenue.",
    error_id: str | None = None,
    status: int = 500,
    extra: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], int]:
    payload: dict[str, Any] = {
        "error": message,
        "error_id": error_id or new_error_id(),
    }
    if extra:
        payload.update(extra)
    return payload, status
