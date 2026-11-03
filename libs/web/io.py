"""Atomic file writes and per-resource locks."""

from __future__ import annotations

import os
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - non-Unix
    fcntl = None  # type: ignore[assignment]


class ResourceBusyError(RuntimeError):
    """Raised when a resource lock cannot be acquired without blocking."""


_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _thread_lock_for(key: str) -> threading.Lock:
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[key] = lock
        return lock


@contextmanager
def resource_lock(key: str, *, blocking: bool = False) -> Iterator[None]:
    """Acquire an exclusive lock for a logical resource (e.g. month id)."""
    thread_lock = _thread_lock_for(key)
    acquired = thread_lock.acquire(blocking=blocking)
    if not acquired:
        raise ResourceBusyError(f"Ressource déjà en cours de traitement: {key}")

    lock_dir = Path(tempfile.gettempdir()) / "analyseur_rapports_locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / f"{key.replace('/', '_').replace(os.sep, '_')}.lock"
    fh = open(lock_path, "a+", encoding="utf-8")
    try:
        if fcntl is not None:
            flags = fcntl.LOCK_EX
            if not blocking:
                flags |= fcntl.LOCK_NB
            try:
                fcntl.flock(fh.fileno(), flags)
            except BlockingIOError as exc:
                raise ResourceBusyError(f"Ressource déjà en cours de traitement: {key}") from exc
        yield
    finally:
        if fcntl is not None:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        fh.close()
        thread_lock.release()


def atomic_write_text(path: Path, content: str, *, encoding: str = "utf-8") -> None:
    """Write content atomically via temp file + os.replace."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding=encoding) as tmp:
            tmp.write(content)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
