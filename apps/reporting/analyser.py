from __future__ import annotations

import csv
import json
import os
import re
import subprocess
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from libs.pipeline.video_names import (
    EXCLUDED_VIDEO_PATTERN,
    is_excluded_video_name,
    normalize_video_name,
)
from libs.schemas import MonthlyBatch, VideoCategory, VideoGroupCategory, VideoGroupReport
from libs.web.io import atomic_write_text
from libs.web.paths import PathValidationError, assert_under_directory

ROOT = Path(__file__).resolve().parents[2]
ARCHIVES_ROOT = ROOT / "archives"
ALLOWED_VIDEO_SUFFIXES = {".mp4"}
_CACHE_DIR = ROOT / ".cache"
_CACHE_DIR.mkdir(parents=True, exist_ok=True)
_DURATION_CACHE_PATH = _CACHE_DIR / "durations.json"

# Thread-safe duration cache
_DUR_CACHE: Dict[str, float] = {}
_DUR_LOCK = threading.Lock()


def _load_duration_cache() -> None:
    global _DUR_CACHE
    if _DurationCacheState.loaded:
        return
    try:
        if _DURATION_CACHE_PATH.exists():
            data = json.loads(_DURATION_CACHE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                _DUR_CACHE = {str(k): float(v) for k, v in data.items() if v is not None}
    except Exception:
        # Ignore corrupt cache
        _DUR_CACHE = {}
    finally:
        _DurationCacheState.loaded = True


def _save_duration_cache() -> None:
    try:
        with _DUR_LOCK:
            atomic_write_text(
                _DURATION_CACHE_PATH,
                json.dumps(_DUR_CACHE, ensure_ascii=False, indent=2),
            )
    except Exception:
        pass


class _DurationCacheState:
    loaded: bool = False
    modified: bool = False


def _cache_key(path: Path, subfolder: Path) -> str:
    """Build a cache key that also covers the scenes-CSV fallback source."""
    parts = [str(path.resolve()), str(subfolder.resolve())]
    candidates = [path]
    try:
        candidates.extend(sorted(subfolder.glob("*_scenes.csv")))
    except OSError:
        pass
    for candidate in candidates:
        try:
            stat = candidate.stat()
            parts.append(f"{candidate.name}|{stat.st_size}|{stat.st_mtime_ns}")
        except OSError:
            parts.append(f"{candidate.name}|0|0")
    return "|".join(parts)


@dataclass
class CategoryThresholds:
    short_lt: int = 120  # < 2 min
    standard_lt: int = 300  # < 5 min
    medium_lt: int = 420  # < 7 min
    long_ge: int = 420  # >= 7 min


DEFAULT_THRESHOLDS = CategoryThresholds()


@dataclass
class VideoEntry:
    id_camille: str
    person: str
    date: datetime
    video_path: Path
    video_name: str
    duration_sec: Optional[float]


@dataclass
class CategoryResult:
    label: str
    count: int
    files: List[str]
    file_details: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class GroupReport:
    id_camille: str
    person: str
    categories: Dict[str, CategoryResult]


def parse_folder_name(name: str) -> Optional[Tuple[str, str, datetime]]:
    # Expected: "<id> Camille YYYY-MM-DD_HH-MM-SS" (time optional for legacy folders)
    parts = name.split()
    if len(parts) < 3:
        return None

    id_camille = parts[0]
    person = parts[1]
    date_part = parts[2]
    match = re.fullmatch(r"(\d{4}-\d{2}-\d{2})(?:_(\d{2})-(\d{2})-(\d{2}))?", date_part)
    if not match:
        return None
    date_text = match.group(1)
    time_text = match.group(2)
    try:
        if time_text:
            dt = datetime.strptime(
                f"{date_text} {time_text}:{match.group(3)}:{match.group(4)}",
                "%Y-%m-%d %H:%M:%S",
            )
        else:
            dt = datetime.strptime(date_text, "%Y-%m-%d")
    except ValueError:
        return None
    return id_camille, person, dt


def _get_duration_with_cache(video_path: Path, subfolder: Path) -> Optional[float]:
    _load_duration_cache()
    key = _cache_key(video_path, subfolder)
    with _DUR_LOCK:
        if key in _DUR_CACHE:
            return _DUR_CACHE[key]

    # Miss: compute
    dur = None
    if video_path.is_file():
        dur = ffprobe_duration_seconds(video_path)
    if dur is None:
        dur = derive_duration_from_scenes(subfolder)

    # Store if found
    if dur is not None:
        with _DUR_LOCK:
            _DUR_CACHE[key] = dur
            _DurationCacheState.modified = True
    return dur


def list_month_folders(archives_dir: Path, year: int, month: int) -> List[Path]:
    out: List[Path] = []
    for child in archives_dir.iterdir():
        if not child.is_dir():
            continue
        parsed = parse_folder_name(child.name)
        if not parsed:
            continue
        _, _, dt = parsed
        if dt.year == year and dt.month == month:
            out.append(child)
    return sorted(out)


def load_metadata_json(folder: Path) -> Optional[dict]:
    meta = folder / "metadata.json"
    if not meta.exists():
        return None
    try:
        return json.loads(meta.read_text(encoding="utf-8"))
    except Exception:
        return None


def timecode_to_seconds(tc: str) -> Optional[float]:
    # format: HH:MM:SS.mmm
    try:
        hh, mm, rest = tc.split(":")
        ss = float(rest)
        return int(hh) * 3600 + int(mm) * 60 + ss
    except Exception:
        return None


def derive_duration_from_scenes(folder: Path) -> Optional[float]:
    # Look for *_scenes.csv and take the last valid 'Timecode Out'
    try:
        scenes_files = sorted(folder.glob("*_scenes.csv"))
    except OSError:
        return None
    if not scenes_files:
        return None
    scenes_file = scenes_files[0]
    last_seconds: Optional[float] = None
    try:
        with scenes_file.open("r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                raw = (row.get("Timecode Out") or row.get("TimecodeOut") or "").strip()
                if not raw:
                    continue
                seconds = timecode_to_seconds(raw)
                if seconds is not None:
                    last_seconds = seconds
    except Exception:
        return None
    return last_seconds


def ffprobe_duration_seconds(path: Path) -> Optional[float]:
    try:
        cmd = [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ]
        res = subprocess.run(cmd, shell=False, capture_output=True, text=True, timeout=15)
        if res.returncode == 0 and res.stdout.strip():
            return float(res.stdout.strip())
    except Exception:
        return None
    return None


def _video_exclusion_reason(video_path: Path) -> Optional[str]:
    if video_path.suffix.lower() not in ALLOWED_VIDEO_SUFFIXES:
        return "format"
    if is_excluded_video_name(video_path.name):
        return "jingle"
    return None


def is_analyzable_video(video_path: Path) -> bool:
    """Seuls les .mp4 sont analysés ; les jingles M6+ sont exclus."""
    return _video_exclusion_reason(video_path) is None


def collect_video_entries_with_warnings(
    archive_folder: Path, id_camille: str, person: str, date_dt: datetime
) -> Tuple[List[VideoEntry], List[str]]:
    entries: List[VideoEntry] = []
    warnings: List[str] = []

    # Build candidate subfolders + metadata first (sequential, cheap)
    candidates: List[Tuple[Path, dict]] = []
    for sub in sorted(archive_folder.iterdir(), key=lambda p: p.name):
        if not sub.is_dir():
            continue
        try:
            assert_under_directory(sub, archive_folder)
        except PathValidationError:
            continue
        meta = load_metadata_json(sub)
        if not meta:
            continue
        raw_video_path = meta.get("video_path")
        if not raw_video_path:
            continue
        video_path = Path(raw_video_path)
        reason = _video_exclusion_reason(video_path)
        if reason == "jingle":
            warnings.append(f"Jingle M6+ ignoré : {video_path.name} (lot {id_camille}).")
            continue
        if reason == "format":
            warnings.append(f"Format ignoré : {video_path.name} (lot {id_camille}).")
            continue
        candidates.append((sub, {"meta": meta, "video_path": video_path}))

    # Deduplicate before expensive duration extraction, keeping the first
    # subfolder in deterministic (alphabetical) order.
    seen_names: set[str] = set()
    unique_candidates: List[Tuple[Path, dict]] = []
    for sub, candidate in candidates:
        video_path = candidate["video_path"]
        normalized_name = normalize_video_name(video_path.name)
        if normalized_name in seen_names:
            warnings.append(f"Doublon ignoré : {video_path.name} (lot {id_camille}).")
            continue
        seen_names.add(normalized_name)
        unique_candidates.append((sub, candidate))

    if not unique_candidates:
        warnings.append(f"Aucune vidéo valide dans le dossier {archive_folder.name}.")
        return [], warnings

    def worker(sub: Path, meta: dict, video_path: Path) -> VideoEntry:
        duration = _get_duration_with_cache(video_path, sub)
        return VideoEntry(
            id_camille=id_camille,
            person=person,
            date=date_dt,
            video_path=video_path,
            video_name=video_path.name if video_path.name else meta.get("video_stem", "unknown"),
            duration_sec=duration,
        )

    try:
        configured_workers = int(os.environ.get("REPORTING_MAX_WORKERS", "8"))
    except ValueError:
        configured_workers = 8
    configured_workers = max(1, configured_workers)
    max_workers = min(configured_workers, max(1, len(unique_candidates)))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [
            executor.submit(worker, sub, candidate["meta"], candidate["video_path"])
            for sub, candidate in unique_candidates
        ]
        for fut in as_completed(futures):
            try:
                entry = fut.result()
                if entry.duration_sec is None:
                    warnings.append(f"Durée indéterminée : {entry.video_name} (lot {id_camille}).")
                entries.append(entry)
            except Exception:
                # Skip failed entries
                pass

    entries.sort(key=lambda item: (normalize_video_name(item.video_name), str(item.video_path)))
    return entries, warnings


def collect_video_entries(
    archive_folder: Path, id_camille: str, person: str, date_dt: datetime
) -> List[VideoEntry]:
    entries, _ = collect_video_entries_with_warnings(archive_folder, id_camille, person, date_dt)
    return entries


def categorize(duration: Optional[float], th: CategoryThresholds) -> Optional[str]:
    if duration is None or duration < 0:
        return None
    if duration < th.short_lt:  # [0, 120[ -> < 2 min
        return "short"
    if duration < th.standard_lt:  # [120, 300[ -> 2 à 5 min
        return "standard"
    if duration < th.medium_lt:  # [300, 420[ -> 5 à 7 min
        return "medium"
    # [420, +inf[ -> >= 7 min (corrige l'angle mort à 420.0s)
    return "long"


def _lot_sort_key(item: Tuple[str, str]) -> Tuple[int, int, str]:
    """Garantit l'absence de TypeError entre identifiants numériques et alphanumériques."""
    lot_id = (item[0] or "").strip()
    digits = re.findall(r"\d+", lot_id)
    if digits:
        return (0, int(digits[0]), lot_id)
    return (1, 0, lot_id)


def disambiguate_folders(
    month_folders: List[Path],
) -> List[Tuple[Path, str, str, datetime]]:
    """
    Associe chaque dossier d'archive à son identifiant de lot effectif.
    Si plusieurs dossiers existent pour un même (id_camille, person) avec des contenus
    non identiques, chaque lot est suffixé par sa date pour éviter de les écraser/fusionner.
    """
    raw_folders: List[Tuple[Path, str, str, datetime]] = []
    by_lot: Dict[Tuple[str, str], List[Tuple[Path, datetime]]] = defaultdict(list)
    for folder in month_folders:
        parsed = parse_folder_name(folder.name)
        if not parsed:
            continue
        id_camille, person, dt = parsed
        raw_folders.append((folder, id_camille, person, dt))
        by_lot[(id_camille, person)].append((folder, dt))

    distinct_duplicate_lots: Set[Tuple[str, str]] = set()
    date_counts_by_lot: Dict[Tuple[str, str], Dict[str, int]] = defaultdict(
        lambda: defaultdict(int)
    )
    for (id_camille, person), folder_list in by_lot.items():
        for _, dt in folder_list:
            date_counts_by_lot[(id_camille, person)][dt.strftime("%Y-%m-%d")] += 1
        if len(folder_list) <= 1:
            continue
        video_sets = []
        for folder, dt in folder_list:
            vids = collect_video_entries(folder, id_camille, person, dt)
            norm_names = frozenset(normalize_video_name(v.video_name) for v in vids if v.video_name)
            if norm_names:
                video_sets.append(norm_names)
        if len(video_sets) > 1 and len(set(video_sets)) > 1:
            distinct_duplicate_lots.add((id_camille, person))

    results: List[Tuple[Path, str, str, datetime]] = []
    for folder, id_camille, person, dt in raw_folders:
        if (id_camille, person) in distinct_duplicate_lots:
            day = dt.strftime("%Y-%m-%d")
            if date_counts_by_lot[(id_camille, person)][day] > 1:
                # Two distinct folders for the same lot were produced the same
                # day: keep the time so their effective IDs cannot collide.
                effective_id = f"{id_camille} ({dt.strftime('%Y-%m-%d %H:%M:%S')})"
            else:
                effective_id = f"{id_camille} ({day})"
        else:
            effective_id = id_camille
        results.append((folder, effective_id, person, dt))
    return results


def analyze_month(
    archives_dir: Path, year: int, month: int, thresholds: CategoryThresholds
) -> List[GroupReport]:
    month_folders = list_month_folders(archives_dir, year, month)
    groups: Dict[Tuple[str, str], List[VideoEntry]] = {}

    for folder, id_camille, person, dt in disambiguate_folders(month_folders):
        videos = collect_video_entries(folder, id_camille, person, dt)
        if not videos:
            continue
        groups.setdefault((id_camille, person), []).extend(videos)

    results: List[GroupReport] = []
    for (id_camille, person), vids in sorted(groups.items(), key=lambda x: _lot_sort_key(x[0])):
        cats: Dict[str, CategoryResult] = {
            "standard": CategoryResult("vidéo standard (entre 2 et 5 minutes)", 0, []),
            "short": CategoryResult("vidéo courte (moins de 2 min)", 0, []),
            "medium": CategoryResult("vidéo moyenne (entre 5 et 7 minutes)", 0, []),
            "long": CategoryResult("vidéo longue (7 min et plus)", 0, []),
        }
        seen_names: set[str] = set()
        for video in vids:
            normalized_name = normalize_video_name(video.video_name)
            if normalized_name in seen_names:
                continue
            seen_names.add(normalized_name)
            cat = categorize(video.duration_sec, thresholds)
            if cat in cats:
                cats[cat].count += 1
                cats[cat].files.append(video.video_name)
                cats[cat].file_details.append(
                    {
                        "name": video.video_name,
                        "duration_sec": video.duration_sec,
                        "category": cat,
                    }
                )
        if not any(category.count for category in cats.values()):
            continue
        # Sort files in each category alphabetically for stable display
        for category in cats.values():
            category.files.sort()
            category.file_details.sort(key=lambda item: str(item.get("name", "")))
        results.append(GroupReport(id_camille=id_camille, person=person, categories=cats))

    # Persist duration cache if modified
    if _DurationCacheState.modified:
        _save_duration_cache()
        _DurationCacheState.modified = False

    return results


def analyze_batch(batch: MonthlyBatch, thresholds: CategoryThresholds) -> List[VideoGroupReport]:
    """
    Analyse un MonthlyBatch pour produire les rapports de groupes vidéo.

    Contrairement à analyze_month qui scanne les dossiers physiques, cette fonction
    utilise directement les video_entries du batch (provenant potentiellement
    de l'extraction PDF ou d'autres sources).

    Args:
        batch: MonthlyBatch contenant les video_entries à analyser
        thresholds: Seuils de catégorisation des durées

    Returns:
        Liste de VideoGroupReport, un par groupe (id_camille, person)
    """
    # Grouper les vidéos par (id_camille, person)
    groups: Dict[Tuple[str, str], list] = {}

    for video_entry in batch.video_entries:
        key = (video_entry.id_camille, video_entry.person)
        groups.setdefault(key, []).append(video_entry)

    results: List[VideoGroupReport] = []

    # Pour chaque groupe, créer un rapport
    for (id_camille, person), video_entries in sorted(
        groups.items(), key=lambda x: _lot_sort_key(x[0])
    ):
        # Initialiser les catégories avec les objets du schéma pivot
        categories: Dict[VideoCategory, VideoGroupCategory] = {
            VideoCategory.SHORT: VideoGroupCategory(
                label="vidéo courte (moins de 2 min)", count=0, files=[]
            ),
            VideoCategory.STANDARD: VideoGroupCategory(
                label="vidéo standard (entre 2 et 5 minutes)", count=0, files=[]
            ),
            VideoCategory.MEDIUM: VideoGroupCategory(
                label="vidéo moyenne (entre 5 et 7 minutes)", count=0, files=[]
            ),
            VideoCategory.LONG: VideoGroupCategory(
                label="vidéo longue (7 min et plus)", count=0, files=[]
            ),
        }

        seen_real_names: set[str] = set()

        # Catégoriser chaque vidéo
        for video_entry in video_entries:
            video_name = Path(video_entry.video_name or "").name
            if is_excluded_video_name(video_name):
                continue

            is_synthetic = getattr(video_entry, "duration_source", "") == "bdc_inferred" or (
                video_entry.video_path or ""
            ).startswith("bdc://")
            if not is_synthetic:
                normalized_name = normalize_video_name(video_name)
                if normalized_name in seen_real_names:
                    continue
                seen_real_names.add(normalized_name)

            cat_str = categorize(video_entry.duration_sec, thresholds)
            if cat_str:
                cat_enum = VideoCategory(cat_str)  # Convertir str vers enum
                categories[cat_enum].count += 1
                categories[cat_enum].files.append(video_entry.video_name)
                categories[cat_enum].file_details.append(
                    {
                        "name": video_entry.video_name,
                        "duration_sec": video_entry.duration_sec,
                        "category": cat_str,
                    }
                )

        if not any(category.count for category in categories.values()):
            continue

        # Trier les fichiers par ordre alphabétique pour stabilité
        for category in categories.values():
            category.files.sort()
            category.file_details.sort(key=lambda item: str(item.get("name", "")))

        # Calculer les totaux (pour compatibilité avec l'ancien format)
        total_mp4 = sum(cat.count for cat in categories.values())
        totals = {"total_mp4": total_mp4}

        # Créer le VideoGroupReport
        report = VideoGroupReport(
            id_camille=id_camille,
            person=person,
            month=batch.batch_id,  # ex. "2025-12"
            categories=categories,
            totals=totals,
        )
        results.append(report)

    # Persister le cache de durée si modifié (même logique qu'analyze_month)
    if _DurationCacheState.modified:
        _save_duration_cache()
        _DurationCacheState.modified = False

    return results
