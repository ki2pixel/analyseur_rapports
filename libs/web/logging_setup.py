"""Structured logging helpers."""

from __future__ import annotations

import logging
import uuid
from contextvars import ContextVar

_request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)


def configure_logging(name: str = "analyseur") -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s [%(name)s] rid=%(request_id)s %(message)s")
        )
        logger.addHandler(stream_handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False

    class RequestIdFilter(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            record.request_id = _request_id_var.get() or "-"
            return True

    for existing_handler in logger.handlers:
        if not any(
            isinstance(f, logging.Filter) and f.__class__.__name__ == "RequestIdFilter"
            for f in existing_handler.filters
        ):
            existing_handler.addFilter(RequestIdFilter())

    return logger


def bind_request_id(value: str | None = None) -> str:
    rid = value or uuid.uuid4().hex[:12]
    _request_id_var.set(rid)
    return rid


def request_id() -> str | None:
    return _request_id_var.get()
