"""Safe path resolution helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable


class PathValidationError(ValueError):
    """Raised when a user-supplied path/filename is rejected."""


def _contains_forbidden(name: str) -> bool:
    if not name or name.strip() != name:
        return True
    if "/" in name or "\\" in name or "\x00" in name:
        return True
    if ".." in name:
        return True
    return False


def safe_join_filename(
    base_dir: Path,
    filename: str,
    *,
    allowed_names: Iterable[str] | None = None,
) -> Path:
    """Resolve filename under base_dir with optional whitelist."""
    if _contains_forbidden(filename):
        raise PathValidationError("Nom de fichier invalide.")

    if allowed_names is not None:
        allowed = set(allowed_names)
        if filename not in allowed:
            raise PathValidationError("Fichier non autorisé.")

    base = base_dir.resolve()
    candidate = (base / filename).resolve()
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise PathValidationError("Chemin hors répertoire autorisé.") from exc

    if not candidate.is_file():
        raise PathValidationError("Fichier introuvable.")

    return candidate


def assert_under_directory(path: Path, root: Path) -> Path:
    """Resolve path and ensure it stays under root."""
    resolved = path.resolve()
    root_resolved = root.resolve()
    try:
        resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise PathValidationError("Chemin hors répertoire autorisé.") from exc
    return resolved
