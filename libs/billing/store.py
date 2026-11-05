"""Filesystem persistence for billing projects and immutable versions."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from libs.web.io import atomic_write_text

DEFAULT_DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "billing"


def data_dir(root: Optional[Path] = None) -> Path:
    return Path(root) if root is not None else DEFAULT_DATA_DIR


def month_dir(month: str, root: Optional[Path] = None) -> Path:
    return data_dir(root) / month


def project_path(month: str, root: Optional[Path] = None) -> Path:
    return month_dir(month, root) / "project.json"


def versions_dir(month: str, root: Optional[Path] = None) -> Path:
    return month_dir(month, root) / "versions"


def version_path(month: str, version: int, root: Optional[Path] = None) -> Path:
    return versions_dir(month, root) / f"v{int(version)}.json"


def attachment_path(month: str, version: int, root: Optional[Path] = None) -> Path:
    return versions_dir(month, root) / f"v{int(version)}-facture.pdf"


def sources_dir(month: str, version: int, root: Optional[Path] = None) -> Path:
    return month_dir(month, root) / "sources" / f"v{int(version)}"


def _read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def load_project(month: str, root: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    path = project_path(month, root)
    if not path.is_file():
        return None
    payload = _read_json(path)
    if not isinstance(payload, dict):
        return None
    return payload


def save_project(project: Dict[str, Any], root: Optional[Path] = None) -> Path:
    path = project_path(str(project["month"]), root)
    atomic_write_text(path, json.dumps(project, ensure_ascii=False, indent=2))
    return path


def load_version(month: str, version: int, root: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    path = version_path(month, version, root)
    if not path.is_file():
        return None
    payload = _read_json(path)
    if not isinstance(payload, dict):
        return None
    return payload


def save_version(
    month: str,
    version: Dict[str, Any],
    root: Optional[Path] = None,
    *,
    overwrite: bool = False,
) -> Path:
    path = version_path(month, int(version["version"]), root)
    if path.exists() and not overwrite:
        raise FileExistsError(f"La version {version['version']} est déjà gelée.")
    atomic_write_text(path, json.dumps(version, ensure_ascii=False, indent=2))
    return path


def list_months(root: Optional[Path] = None) -> List[str]:
    base = data_dir(root)
    if not base.is_dir():
        return []
    months = []
    for entry in base.iterdir():
        if entry.is_dir() and (entry / "project.json").is_file():
            months.append(entry.name)
    return sorted(months, reverse=True)


def file_info(path: Path) -> Dict[str, Any]:
    """Hash + metadata for a source file (name only, never a path)."""
    try:
        stat = path.stat()
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return {
            "name": path.name,
            "sha256": digest.hexdigest(),
            "size": stat.st_size,
            "modified_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
            .replace(microsecond=0)
            .isoformat(),
        }
    except OSError:
        return {"name": path.name, "sha256": "", "size": 0, "modified_at": ""}


def copy_source(
    path: Path,
    month: str,
    version: int,
    root: Optional[Path] = None,
) -> Dict[str, Any]:
    info = file_info(path)
    if not info["sha256"]:
        return info
    target = sources_dir(month, version, root) / path.name
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, target)
    return info


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as tmp:
            tmp.write(payload)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def save_attachment(month: str, version: int, payload: bytes, root: Optional[Path] = None) -> Path:
    path = attachment_path(month, version, root)
    _atomic_write_bytes(path, payload)
    return path
