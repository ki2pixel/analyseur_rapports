from __future__ import annotations

import json
import os
import re
from collections import defaultdict
from datetime import datetime
from itertools import product
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple, Union

from libs.pipeline.bdc_to_batch import infer_category_from_description, parse_quantity
from libs.pipeline.video_names import is_excluded_video_name, normalize_video_name
from libs.schemas import VideoCategory

CATEGORY_KEYS = [
    VideoCategory.SHORT.value,
    VideoCategory.STANDARD.value,
    VideoCategory.MEDIUM.value,
    VideoCategory.LONG.value,
]

NUMBER_CAPTURE = re.compile(r"\d+")
# The lookarounds prevent "BDC-26-1689" (hyphen part of an identifier) from
# being parsed as lots 26..1689.  Dates and BDC identifiers are filtered
# explicitly in ``_numbers_from_token``.
RANGE_PATTERN = re.compile(r"(?<![\w-])(\d+)\s*(?:à|au|to|a|-)\s*(\d+)(?![\w-])", re.IGNORECASE)
BDC_IDENTIFIER_PATTERN = re.compile(
    r"\bBDC-(?:[A-Z0-9]{1,12}-)+\d{1,8}\b|\bBDC[-\s]?\d", re.IGNORECASE
)
DATE_IDENTIFIER_PATTERN = re.compile(
    r"^(?:\d{4}-\d{2}(?:-\d{2})?|\d{2}/\d{2}/\d{4}|(?:0[1-9]|1[0-2])-\d{4})$"
)
LOT_IDENTIFIER_PATTERN = re.compile(r"^\d+(?:\s*\([^)]*\))?$")
MAX_RANGE_WIDTH = int(os.environ.get("RECONCILIATION_MAX_RANGE_WIDTH", "5000"))
NEAR_BOUNDARY_SECONDS = 3.0
THRESHOLD_BOUNDARIES = (120.0, 300.0, 480.0)
THRESHOLD_CATEGORIES = {
    120.0: ("short", "standard"),
    300.0: ("standard", "medium"),
    480.0: ("medium", "long"),
}
# Same file name + same category + nearly identical duration = duplicate.
# A one-second tolerance absorbs re-encoding jitter without merging two
# genuinely different videos that happen to share a title.
DUPLICATE_DURATION_TOLERANCE_SECONDS = 1.0
# Tolérance de dérive de durée entre l'annexe BDC et l'archive avant badge info.
ANNEX_DURATION_DRIFT_SECONDS = 5.0
VIDEO_EXTENSION_PATTERN = re.compile(r"\.(?:mp4|mov|mxf|avi|mkv)$", re.IGNORECASE)
STUDIO_SUFFIX_PATTERN = re.compile(r"[_\s]*_TAT_[A-Za-z0-9À-ÿ]+$", re.IGNORECASE)


def _init_category_counter() -> Dict[str, int]:
    return {key: 0 for key in CATEGORY_KEYS}


def _normalize_count(value: Optional[int]) -> int:
    try:
        return int(value or 0)
    except (ValueError, TypeError):
        return 0


def _parse_quantity(raw_value: Optional[str]) -> int:
    return parse_quantity(raw_value)


def _sort_key(value: Union[str, int, None]) -> Tuple[int, str, str]:
    if value is None:
        return (2, "", "")
    if isinstance(value, int):
        return (0, f"{value:09d}", str(value))
    raw = str(value)
    digits = NUMBER_CAPTURE.findall(raw)
    if digits:
        try:
            return (0, f"{int(digits[0]):09d}", raw)
        except ValueError:
            pass
    return (1, "", raw)


def _bdc_sort_key(value: Union[str, int, None]) -> Tuple[int, Tuple[int, ...], str]:
    """Natural sort for BDC numbers (all numeric groups, not just the first)."""
    if value is None:
        return (2, (), "")
    raw = str(value)
    digits = NUMBER_CAPTURE.findall(raw)
    if digits:
        return (0, tuple(int(number) for number in digits), raw)
    return (1, (), raw)


def _base_lot_id(raw: str) -> str:
    cleaned = str(raw).strip()
    m = re.match(r"^(\d+)\b", cleaned)
    if m:
        return str(int(m.group(1)))
    return cleaned


def _normalize_lot_id(raw: str) -> str:
    cleaned = str(raw).strip()
    return str(int(cleaned)) if cleaned.isdigit() else cleaned


def _expand_range(start: int, end: int, known_ids: Optional[Set[str]] = None) -> List[str]:
    """Expand a range, optionally restricted to lots that exist in production."""
    if start > end:
        start, end = end, start
    if end - start > MAX_RANGE_WIDTH:
        return [str(start), str(end)] if start != end else [str(start)]

    if known_ids is None:
        return [str(number) for number in range(start, end + 1)]

    candidates = [
        str(number)
        for number in range(start, end + 1)
        if str(number) in known_ids or any(_base_lot_id(kid) == str(number) for kid in known_ids)
    ]
    if candidates:
        return candidates
    # No production lot in this range: do not manufacture phantom lot IDs
    # from the bounds (they pollute the adjacent-month index and diagnostics).
    return []


def _numbers_from_token(token: str, known_ids: Optional[Set[str]]) -> List[str]:
    token_text = str(token).strip()
    if not token_text:
        return []
    if DATE_IDENTIFIER_PATTERN.match(token_text):
        return []
    if BDC_IDENTIFIER_PATTERN.search(token_text) and not re.search(
        r"\bLOTS?\b", token_text, re.IGNORECASE
    ):
        return []

    numbers: List[str] = []
    for start_str, end_str in RANGE_PATTERN.findall(token_text):
        try:
            numbers.extend(_expand_range(int(start_str), int(end_str), known_ids))
        except ValueError:
            continue

    # Process standalone numbers outside ranges without double-counting bounds.
    remainder = RANGE_PATTERN.sub(" ", token_text)
    numbers.extend(NUMBER_CAPTURE.findall(remainder))
    return numbers


def _extract_reference_ids(
    order: dict, known_reporting_ids: Optional[Set[str]] = None
) -> Tuple[str, ...]:
    """
    Identifie les numéros de lots à partir des champs `references` / `reference`.

    Un BDC détaillé expose ``concerned_lots`` : cette liste explicite prime sur
    l'expansion de la plage ``LOTS 42 À 311``, qui ne doit pas revendiquer les
    lots intermédiaires non facturés.

    Lorsque ``known_reporting_ids`` est fourni, les plages sont intersectées avec
    les lots réellement présents dans le rapport de production.  Un même numéro
    de base peut correspondre à plusieurs dossiers désambiguïsés
    (ex. ``309 (2026-10-01)`` et ``309 (2026-10-27)``) : tous sont rattachés.
    """
    normalized: List[str] = []
    seen: Set[str] = set()

    def add_lot(candidate: str) -> None:
        if candidate and candidate not in seen:
            seen.add(candidate)
            normalized.append(candidate)

    def resolve(candidate: str) -> None:
        cleaned = _normalize_lot_id(candidate)
        if known_reporting_ids:
            base = _base_lot_id(cleaned)
            candidates = sorted(
                (cid for cid in known_reporting_ids if _base_lot_id(cid) == base),
                key=_sort_key,
            )
            if candidates:
                for item in candidates:
                    add_lot(item)
                return
        add_lot(cleaned)

    concerned_lots = order.get("concerned_lots") or []
    if isinstance(concerned_lots, (list, tuple)) and concerned_lots:
        for raw in concerned_lots:
            text = str(raw).strip()
            if not text:
                continue
            if LOT_IDENTIFIER_PATTERN.match(text) or text.isdigit():
                resolve(text)
                continue
            for number in NUMBER_CAPTURE.findall(text):
                resolve(number)
        if normalized:
            normalized.sort(key=_sort_key)
            return tuple(normalized)

    tokens: List[str] = []
    references = order.get("references") or []
    if isinstance(references, str):
        tokens.append(references)
    else:
        tokens.extend(references)
    reference_value = order.get("reference")
    if reference_value:
        tokens.append(str(reference_value))

    numbers: List[str] = []
    for token in tokens:
        if not token:
            continue
        numbers.extend(_numbers_from_token(str(token), known_reporting_ids))

    for raw in numbers:
        resolve(raw)

    if normalized:
        normalized.sort(key=_sort_key)
        return tuple(normalized)

    fallback = reference_value or order.get("numero_bdc") or "UNKNOWN"
    return (str(fallback),)


def extract_reference_ids(
    order: dict, known_reporting_ids: Optional[Set[str]] = None
) -> Tuple[str, ...]:
    """Public helper used by the BDC importer for overlap previews."""
    return _extract_reference_ids(order, known_reporting_ids)


def detect_bdc_overlaps(bdc_payload: List[dict]) -> List[dict]:
    """Detect lots claimed by more than one BDC (full ranges, no production filter)."""
    claims: Dict[str, Set[str]] = defaultdict(set)
    for order in bdc_payload:
        numero = str(order.get("numero_bdc") or "UNKNOWN")
        for lot_id in _extract_reference_ids(order, None):
            claims[lot_id].add(numero)

    return [
        {"lot": lot_id, "bdc_numbers": sorted(claims[lot_id], key=_bdc_sort_key)}
        for lot_id in sorted(claims.keys(), key=_sort_key)
        if len(claims[lot_id]) > 1
    ]


def _display_lot_id(raw: str) -> str:
    text = str(raw).strip()
    if "(" in text:
        return text
    return _base_lot_id(text)


def _cluster_label(cluster_ids: Sequence[str]) -> str:
    if not cluster_ids:
        return "Sans lot identifié"
    display_ids = [_display_lot_id(cid) for cid in cluster_ids]
    if len(display_ids) == 1:
        return f"Lot {display_ids[0]}"
    joined = ", ".join(display_ids)
    return f"Lots {joined}"


def _parse_file_details(cat_data: dict, files: List[str], category: str) -> List[Dict[str, Any]]:
    raw_details = cat_data.get("files_detail") or cat_data.get("file_details") or []
    details: List[Dict[str, Any]] = []
    if isinstance(raw_details, list):
        for item in raw_details:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or item.get("file") or "")
            if not name:
                continue
            duration = item.get("duration_sec")
            try:
                duration_value = float(duration) if duration is not None else None
            except (TypeError, ValueError):
                duration_value = None
            details.append({"name": name, "duration_sec": duration_value, "category": category})

    if not details:
        details = [{"name": name, "duration_sec": None, "category": category} for name in files]
    return details


def _collect_reporting_groups(report_payload: dict) -> Dict[str, dict]:
    groups: Dict[str, dict] = {}
    for entry in report_payload.get("report", []):
        raw_group_id = str(entry.get("id_camille") or "").strip()
        if not raw_group_id:
            continue
        group_id = _normalize_lot_id(raw_group_id)
        categories_raw = entry.get("categories") or {}
        categories = {}
        for cat in CATEGORY_KEYS:
            cat_data = categories_raw.get(cat) or {}
            files = [str(name) for name in (cat_data.get("files") or [])]
            categories[cat] = {
                "count": _normalize_count(cat_data.get("count")),
                "files": files,
                "file_details": _parse_file_details(cat_data, files, cat),
            }
        groups[group_id] = {
            "id_camille": raw_group_id,
            "person": entry.get("person", ""),
            "categories": categories,
            "total": sum(cat["count"] for cat in categories.values()),
        }
    return groups


def _sum_counts(groups: Dict[str, dict]) -> Dict[str, int]:
    totals = _init_category_counter()
    for group in groups.values():
        for cat, value in group["categories"].items():
            totals[cat] += value["count"]
    totals["overall"] = sum(totals.values())
    return totals


def _collect_cluster_production(
    groups: Dict[str, dict], cluster_ids: Sequence[str]
) -> Tuple[Dict[str, int], List[str], List[dict], List[dict]]:
    aggregated = _init_category_counter()
    missing_ids: List[str] = []
    lots: List[dict] = []
    files: List[dict] = []

    for gid in cluster_ids:
        group = groups.get(gid)
        if not group:
            missing_ids.append(gid)
            continue

        lot_categories = {}
        for cat, value in group["categories"].items():
            aggregated[cat] += value["count"]
            lot_categories[cat] = value
            for detail in value.get("file_details") or []:
                files.append(
                    {
                        "lot": gid,
                        "name": detail.get("name", ""),
                        "duration_sec": detail.get("duration_sec"),
                        "category": cat,
                    }
                )
        lots.append(
            {
                "id_camille": gid,
                "person": group.get("person", ""),
                "total": group.get("total", 0),
                "categories": lot_categories,
            }
        )

    aggregated["overall"] = sum(aggregated.values())
    return aggregated, missing_ids, lots, files


def _duration_value(item: dict) -> Optional[float]:
    duration = item.get("duration_sec")
    if duration is None:
        return None
    try:
        return float(duration)
    except (TypeError, ValueError):
        return None


def _near_boundary_threshold(duration: float, delta: Dict[str, int]) -> Optional[float]:
    """Return the threshold when a duration is near it AND the delta is relevant."""
    if duration < 0:
        return None
    for threshold in THRESHOLD_BOUNDARIES:
        if abs(threshold - duration) > NEAR_BOUNDARY_SECONDS:
            continue
        categories = THRESHOLD_CATEGORIES.get(threshold, ())
        if any(delta.get(category, 0) for category in categories):
            return threshold
    return None


def _duplicate_extras(
    production_files: List[dict], annex_index: Optional[Dict[str, List[dict]]] = None
) -> List[dict]:
    """
    Return the surplus occurrences of duplicate files.

    Two files are duplicates only when they share the same normalized name,
    the same category and a duration within ``DUPLICATE_DURATION_TOLERANCE_SECONDS``.
    The first occurrence is kept as representative; every additional one is an
    extra that may explain a positive delta.

    Lorsqu'un BDC détaillé facture explicitement les deux occurrences sur deux
    lots distincts (ex. « Mortelle Adèle » sur les lots 209 et 219), la seconde
    n'est pas un doublon suspect : elle est réellement commandée.
    """
    groups: Dict[Tuple[str, str], List[Tuple[Optional[float], dict]]] = defaultdict(list)
    for item in production_files:
        name = str(item.get("name") or "")
        if not name or is_excluded_video_name(name):
            continue
        group_key = (normalize_video_name(name), str(item.get("category") or ""))
        groups[group_key].append((_duration_value(item), item))

    extras: List[dict] = []
    for entries in groups.values():
        if len(entries) < 2:
            continue
        if annex_index and _all_occurrences_billed(entries, annex_index):
            continue
        ordered = sorted(
            entries,
            key=lambda pair: (pair[0] is None, pair[0] if pair[0] is not None else 0.0),
        )
        clusters: List[Dict[str, Any]] = []
        for duration, item in ordered:
            placed = False
            for cluster in clusters:
                reference = cluster["duration"]
                if duration is None and reference is None:
                    cluster["items"].append(item)
                    placed = True
                    break
                if (
                    duration is not None
                    and reference is not None
                    and abs(reference - duration) <= DUPLICATE_DURATION_TOLERANCE_SECONDS
                ):
                    cluster["items"].append(item)
                    placed = True
                    break
            if not placed:
                clusters.append({"duration": duration, "items": [item]})
        for cluster in clusters:
            if len(cluster["items"]) > 1:
                extras.extend(cluster["items"][1:])
    return extras


def _all_occurrences_billed(
    entries: Sequence[Tuple[Optional[float], dict]], annex_index: Dict[str, List[dict]]
) -> bool:
    """Vrai si chaque occurrence est facturée sur son propre lot dans une annexe."""
    lots = {str(item.get("lot") or "") for _, item in entries}
    if len(lots) < 2:
        return False
    for _, item in entries:
        lot = str(item.get("lot") or "")
        if not lot:
            return False
        name_key = _video_match_key(str(item.get("name") or ""))
        billed = any(
            annex["key"] == name_key or annex["key_no_studio"] == name_key
            for annex in annex_index.get(lot, [])
        )
        if not billed:
            return False
    return True


def _annotate_production_lots(
    production_lots: List[dict],
    delta: Dict[str, int],
    production_files: List[dict],
    annex_index: Optional[Dict[str, List[dict]]] = None,
) -> List[dict]:
    """Enrichit chaque fichier de production avec son statut d'écart, doublon ou frontière."""
    flat_details: List[dict] = []
    for lot in production_lots:
        for cat_info in (lot.get("categories") or {}).values():
            flat_details.extend(cat_info.get("file_details") or [])
    duplicate_ids = {id(item) for item in _duplicate_extras(flat_details, annex_index)}
    annotated_lots: List[dict] = []

    for lot in production_lots:
        annotated_lot = dict(lot)
        annotated_categories = {}
        lot_has_suspects = False
        for cat, cat_info in lot.get("categories", {}).items():
            cat_delta = delta.get(cat, 0)
            annotated_details = []
            for detail in cat_info.get("file_details") or []:
                name = str(detail.get("name") or "")
                is_jingle = is_excluded_video_name(name)
                is_dup = id(detail) in duplicate_ids

                dur = detail.get("duration_sec")
                is_near = False
                near_th: Optional[float] = None
                if dur is not None:
                    try:
                        dur_val = float(dur)
                    except (TypeError, ValueError):
                        dur_val = None
                    if dur_val is not None:
                        near_th = _near_boundary_threshold(dur_val, delta)
                        is_near = near_th is not None

                is_suspect = is_jingle or is_dup or is_near
                if is_suspect:
                    lot_has_suspects = True

                annex_match = None
                if annex_index:
                    annex_match = _annex_match_for_file(
                        annex_index,
                        str(lot.get("id_camille") or ""),
                        name,
                        dur,
                    )

                annotated_details.append(
                    {
                        **detail,
                        "is_duplicate": is_dup,
                        "is_jingle": is_jingle,
                        "is_near_boundary": is_near,
                        "is_suspect": is_suspect,
                        "near_threshold": near_th,
                        "category_delta": cat_delta,
                        "annex_match": annex_match,
                    }
                )
            annotated_categories[cat] = {
                **cat_info,
                "file_details": annotated_details,
                "delta": cat_delta,
            }
        annotated_lot["categories"] = annotated_categories
        annotated_lot["has_suspects"] = lot_has_suspects
        annotated_lots.append(annotated_lot)

    return annotated_lots


def _annex_match_for_file(
    annex_index: Dict[str, List[dict]],
    lot: str,
    name: str,
    duration: Any,
) -> Optional[Dict[str, Any]]:
    """Décrit la correspondance d'un fichier de production avec l'annexe du lot."""
    lot_annex = annex_index.get(str(lot))
    if not lot_annex:
        return None
    key = _video_match_key(name)
    key_no_studio = _video_match_key(name, drop_studio_suffix=True)
    for annex in lot_annex:
        kind = None
        if annex["key"] == key:
            kind = "strict"
        elif annex["key_no_studio"] == key_no_studio:
            kind = "radical"
        if kind is None:
            continue
        bdc_duration = annex.get("duration_sec")
        production_duration = _float_or_none(duration)
        drift = None
        if bdc_duration is not None and production_duration is not None:
            drift = round(production_duration - bdc_duration, 2)
        extension_differs = False
        annex_name = str(annex.get("name") or "")
        prod_ext = VIDEO_EXTENSION_PATTERN.search(name)
        annex_ext = VIDEO_EXTENSION_PATTERN.search(annex_name)
        if prod_ext and annex_ext:
            extension_differs = prod_ext.group(0).lower() != annex_ext.group(0).lower()
        return {
            "status": "matched",
            "match_kind": kind,
            "annex_name": annex_name,
            "bdc_duration_sec": bdc_duration,
            "duration_drift_sec": drift,
            "duration_drift": drift is not None and abs(drift) > ANNEX_DURATION_DRIFT_SECONDS,
            "extension_differs": extension_differs,
        }
    return {
        "status": "not_listed",
        "match_kind": None,
        "annex_name": None,
        "bdc_duration_sec": None,
        "duration_drift_sec": None,
        "duration_drift": False,
        "extension_differs": False,
    }


def _video_match_key(name: str, *, drop_studio_suffix: bool = False) -> str:
    """Clé de rapprochement vidéo tolérante.

    Le nom est normalisé (NFC/NFKC, espaces, minuscules), l'extension est
    supprimée pour rendre ``.mov`` et ``.mp4`` interchangeables, ainsi que
    l'espace parasite inséré avant l'extension lors d'un saut de ligne PDF.
    """
    value = normalize_video_name(name)
    value = re.sub(r"\s+(?=\.[a-z0-9]+$)", "", value)
    value = VIDEO_EXTENSION_PATTERN.sub("", value)
    if drop_studio_suffix:
        value = STUDIO_SUFFIX_PATTERN.sub("", value)
    return value.strip()


def _build_annex_video_index(bdc_orders: Sequence[dict]) -> Dict[str, List[dict]]:
    """Indexe les vidéos d'annexe par lot, avec leurs clés de rapprochement."""
    index: Dict[str, List[dict]] = defaultdict(list)
    for order in bdc_orders:
        number = str(order.get("numero_bdc") or "")
        for item in order.get("annex_videos") or []:
            if not isinstance(item, dict):
                continue
            lot = str(item.get("lot") or "").strip()
            name = str(item.get("video_name") or "").strip()
            if not lot or not name:
                continue
            index[lot].append(
                {
                    "bdc": number,
                    "lot": lot,
                    "name": name,
                    "key": _video_match_key(name),
                    "key_no_studio": _video_match_key(name, drop_studio_suffix=True),
                    "duration_sec": _float_or_none(item.get("duration_sec")),
                    "category": _category_key(item.get("category_label")),
                    "montant": item.get("montant"),
                }
            )
    return index


def _category_key(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    mapping = {
        "courte": "short",
        "court": "short",
        "short": "short",
        "standard": "standard",
        "moyenne": "medium",
        "moyen": "medium",
        "medium": "medium",
        "longue": "long",
        "long": "long",
    }
    return mapping.get(normalized, "")


def _float_or_none(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _reconcile_cluster_videos(
    cluster_ids: Sequence[str],
    bdc_orders: Sequence[dict],
    production_files: List[dict],
) -> Optional[Dict[str, Any]]:
    """Rapproche les vidéos d'annexe BDC et la production, lot par lot.

    Retourne ``None`` quand aucun BDC du cluster ne détaille ses vidéos
    (rétrocompatibilité des BDC simplifiés).
    """
    annex_index = _build_annex_video_index(bdc_orders)
    if not annex_index:
        return None

    cluster_lots = {str(lot) for lot in cluster_ids}
    relevant_lots = [lot for lot in annex_index if lot in cluster_lots] or list(annex_index)

    production_by_lot: Dict[str, List[dict]] = defaultdict(list)
    for item in production_files:
        production_by_lot[str(item.get("lot") or "")].append(item)

    statuses: List[dict] = []
    for lot in sorted(relevant_lots, key=_sort_key):
        annex_items = annex_index.get(lot, [])
        production = production_by_lot.get(lot, [])
        used_production: Set[int] = set()
        unmatched_annex = list(annex_items)

        # 1. Correspondance stricte (extension ignorée).
        remaining: List[dict] = []
        for annex in unmatched_annex:
            match_index = next(
                (
                    index
                    for index, prod in enumerate(production)
                    if index not in used_production
                    and _video_match_key(str(prod.get("name") or "")) == annex["key"]
                ),
                None,
            )
            if match_index is None:
                remaining.append(annex)
                continue
            used_production.add(match_index)
            statuses.append(
                _video_status(annex, production[match_index], "matched", lot, lot_match="strict")
            )

        # 2. Repli sur le radical sans suffixe studio (même lot).
        still_remaining: List[dict] = []
        for annex in remaining:
            match_index = next(
                (
                    index
                    for index, prod in enumerate(production)
                    if index not in used_production
                    and _video_match_key(str(prod.get("name") or ""), drop_studio_suffix=True)
                    == annex["key_no_studio"]
                ),
                None,
            )
            if match_index is None:
                still_remaining.append(annex)
                continue
            used_production.add(match_index)
            statuses.append(
                _video_status(annex, production[match_index], "matched", lot, lot_match="radical")
            )

        for annex in still_remaining:
            statuses.append(
                {
                    "lot": lot,
                    "bdc": annex["bdc"],
                    "name": annex["name"],
                    "bdc_duration_sec": annex["duration_sec"],
                    "bdc_category": annex["category"],
                    "production_duration_sec": None,
                    "production_category": "",
                    "status": "missing_in_production",
                    "category_matches": None,
                    "duration_drift_sec": None,
                    "match_kind": None,
                }
            )

        for index, prod in enumerate(production):
            if index in used_production:
                continue
            statuses.append(
                {
                    "lot": lot,
                    "bdc": "",
                    "name": str(prod.get("name") or ""),
                    "bdc_duration_sec": None,
                    "bdc_category": "",
                    "production_duration_sec": _float_or_none(prod.get("duration_sec")),
                    "production_category": str(prod.get("category") or ""),
                    "status": "extra_in_production",
                    "category_matches": None,
                    "duration_drift_sec": None,
                    "match_kind": None,
                }
            )

    counts: Dict[str, int] = defaultdict(int)
    for item in statuses:
        counts[item["status"]] += 1

    matched = counts.get("matched", 0)
    # Une dérive de durée reste une vidéo appariée dans la bonne catégorie :
    # elle compte dans le taux et reste signalée par un badge ``info``.
    matched_with_drift = matched + counts.get("duration_drift", 0)
    total_annex = sum(1 for item in statuses if item["bdc"])
    return {
        "has_detail": True,
        "lots": sorted({item["lot"] for item in statuses}, key=_sort_key),
        "annex_total": total_annex,
        "matched": matched,
        "match_rate": (round(100 * matched_with_drift / total_annex, 1) if total_annex else 0.0),
        "counts": dict(counts),
        "items": statuses,
        "duration_drifts": [item for item in statuses if item["status"] == "duration_drift"],
        "bdc_numbers": sorted(
            {
                str(order.get("numero_bdc") or "")
                for order in bdc_orders
                if order.get("annex_videos")
            },
            key=_bdc_sort_key,
        ),
    }


def _video_status(
    annex: dict, production: dict, status: str, lot: str, *, lot_match: str
) -> Dict[str, Any]:
    bdc_duration = annex.get("duration_sec")
    prod_duration = _float_or_none(production.get("duration_sec"))
    prod_category = str(production.get("category") or "")
    bdc_category = annex.get("category") or ""

    resolved_status = status
    category_matches: Optional[bool] = None
    drift: Optional[float] = None
    if bdc_category and prod_category:
        category_matches = bdc_category == prod_category
        if not category_matches:
            resolved_status = "category_mismatch"
    if bdc_duration is not None and prod_duration is not None:
        drift = round(prod_duration - bdc_duration, 2)
        if resolved_status == "matched" and abs(drift) > ANNEX_DURATION_DRIFT_SECONDS:
            resolved_status = "duration_drift"

    return {
        "lot": lot,
        "bdc": annex.get("bdc") or "",
        "name": annex.get("name") or "",
        "bdc_duration_sec": bdc_duration,
        "bdc_category": bdc_category,
        "production_duration_sec": prod_duration,
        "production_category": prod_category,
        "status": resolved_status,
        "category_matches": category_matches,
        "duration_drift_sec": drift,
        "match_kind": lot_match,
    }


def _billed_counts_for_order(order: dict) -> Dict[str, int]:
    counts = {**_init_category_counter(), "unknown": 0}
    for article in order.get("articles") or []:
        category = infer_category_from_description(article.get("description", ""))
        category_key = category.value if isinstance(category, VideoCategory) else str(category)
        quantity = parse_quantity(article.get("quantite"))
        counts[category_key] = counts.get(category_key, 0) + quantity
    return counts


def _actual_counts_for_ids(reporting_groups: Dict[str, dict], ids: Iterable[str]) -> Dict[str, int]:
    counts = _init_category_counter()
    for gid in ids:
        group = reporting_groups.get(gid)
        if not group:
            continue
        for cat in CATEGORY_KEYS:
            cat_data = group["categories"].get(cat) or {}
            counts[cat] += _normalize_count(cat_data.get("count"))
    return counts


def _count_delta_cost(actual: Dict[str, int], billed: Dict[str, int]) -> int:
    return sum(abs(actual.get(cat, 0) - billed.get(cat, 0)) for cat in CATEGORY_KEYS)


def _assignment_options(order_indices: Sequence[int]) -> List[Optional[Tuple[int, ...]]]:
    options: List[Optional[Tuple[int, ...]]] = [None]
    for mask in range(1, 1 << len(order_indices)):
        options.append(
            tuple(
                order_indices[index] for index in range(len(order_indices)) if mask & (1 << index)
            )
        )
    return options


MAX_ASSIGNMENT_COMBINATIONS = 50_000


def _resolve_order_cluster_ids(
    bdc_payload: List[dict], reporting_groups: Dict[str, dict]
) -> List[List[str]]:
    """
    Allocate disambiguated production folders to the BDC orders that reference them.

    When a single order references a base lot (e.g. ``309``) that exists as several
    folders (``309 (2026-10-01)`` and ``309 (2026-10-27)``), all folders are attached
    to that order, as before.  When several orders reference the same base lot, each
    folder is assigned to the order whose billed category counts it best completes:
    a BDC billing 3 short + 1 standard gets ``309 (2026-10-01)`` + ``310`` while a BDC
    billing 1 long gets ``309 (2026-10-27)``.  This avoids counting the same folder in
    both BDCs and reporting a phantom overlap.
    """
    known_ids = set(reporting_groups)
    by_base: Dict[str, List[str]] = defaultdict(list)
    for gid in known_ids:
        by_base[_base_lot_id(gid)].append(gid)
    ambiguous_by_base = {
        base: sorted(ids, key=_sort_key) for base, ids in by_base.items() if len(ids) > 1
    }
    if not ambiguous_by_base:
        return [list(_extract_reference_ids(order, known_ids)) for order in bdc_payload]

    order_bases: List[Set[str]] = []
    assigned: List[Set[str]] = []
    for order in bdc_payload:
        raw_ids = list(_extract_reference_ids(order, known_ids))
        bases: Set[str] = set()
        kept: List[str] = []
        for gid in raw_ids:
            base = _base_lot_id(gid)
            if base in ambiguous_by_base:
                bases.add(base)
            else:
                kept.append(gid)
        order_bases.append(bases)
        assigned.append(set(kept))

    base_orders: Dict[str, List[int]] = defaultdict(list)
    for index, bases in enumerate(order_bases):
        for base in bases:
            base_orders[base].append(index)

    billed_counts = [_billed_counts_for_order(order) for order in bdc_payload]
    for base, order_indices in sorted(base_orders.items(), key=lambda item: _sort_key(item[0])):
        candidates = ambiguous_by_base[base]
        order_indices = sorted(order_indices)
        if len(order_indices) == 1:
            assigned[order_indices[0]].update(candidates)
            continue

        baseline = {
            index: _actual_counts_for_ids(reporting_groups, assigned[index])
            for index in order_indices
        }
        candidate_counts = {
            candidate: _actual_counts_for_ids(reporting_groups, [candidate])
            for candidate in candidates
        }
        options = _assignment_options(order_indices)
        combination_count = len(options) ** len(candidates)
        if combination_count > MAX_ASSIGNMENT_COMBINATIONS:
            # Too many folders/orders to brute-force: pick the best fit per folder.
            for candidate in candidates:
                best_index = min(
                    order_indices,
                    key=lambda index: _count_delta_cost(
                        {
                            cat: baseline[index].get(cat, 0)
                            + candidate_counts[candidate].get(cat, 0)
                            for cat in CATEGORY_KEYS
                        },
                        billed_counts[index],
                    ),
                )
                assigned[best_index].add(candidate)
            continue

        best_key: Optional[Tuple[int, int, int]] = None
        best_combo: Optional[Tuple[Optional[Tuple[int, ...]], ...]] = None
        for combo in product(options, repeat=len(candidates)):
            actual = {index: dict(baseline[index]) for index in order_indices}
            claims_count = 0
            assigned_count = 0
            for candidate, targets in zip(candidates, combo):
                if not targets:
                    continue
                assigned_count += 1
                claims_count += len(targets)
                for index in targets:
                    for cat in CATEGORY_KEYS:
                        actual[index][cat] = actual[index].get(cat, 0) + candidate_counts[
                            candidate
                        ].get(cat, 0)
            cost = sum(
                _count_delta_cost(actual[index], billed_counts[index]) for index in order_indices
            )
            key = (cost, claims_count, -assigned_count)
            if best_key is None or key < best_key:
                best_key = key
                best_combo = combo
        for candidate, targets in zip(candidates, best_combo or ()):
            if targets:
                for index in targets:
                    assigned[index].add(candidate)

    return [sorted(ids, key=_sort_key) for ids in assigned]


def _summarize_bdc_orders(
    bdc_payload: List[dict], reporting_groups: Dict[str, dict]
) -> Dict[str, Any]:
    clusters: Dict[str, Dict[str, Any]] = {}
    claims: Dict[str, Set[str]] = defaultdict(set)
    order_summaries: List[Dict[str, Any]] = []
    order_cluster_ids = _resolve_order_cluster_ids(bdc_payload, reporting_groups)

    for order, cluster_ids in zip(bdc_payload, order_cluster_ids):
        counts = _billed_counts_for_order(order)

        total = sum(value for value in counts.values())
        numero_bdc = str(order.get("numero_bdc") or "UNKNOWN")
        cluster_key = "|".join(cluster_ids)
        order_summaries.append(
            {
                "numero_bdc": numero_bdc,
                "cluster_ids": cluster_ids,
                "cluster_key": cluster_key,
                "counts": counts,
                "total": total,
            }
        )

        cluster = clusters.setdefault(
            cluster_key,
            {
                "cluster_key": cluster_key,
                "cluster_ids": cluster_ids,
                "counts": {**_init_category_counter(), "unknown": 0},
                "bdc_numbers": [],
                "source_references": [],
                "total": 0,
            },
        )
        for key, value in counts.items():
            cluster["counts"][key] = cluster["counts"].get(key, 0) + value
        cluster["total"] += total
        if numero_bdc not in cluster["bdc_numbers"]:
            cluster["bdc_numbers"].append(numero_bdc)

        source_reference = order.get("reference")
        if source_reference and source_reference not in cluster["source_references"]:
            cluster["source_references"].append(str(source_reference))
        for reference in order.get("references") or []:
            if reference and reference not in cluster["source_references"]:
                cluster["source_references"].append(str(reference))

        for gid in cluster_ids:
            claims[gid].add(numero_bdc)

    covered_ids = sorted(claims.keys(), key=_sort_key)
    overlapping_bdc_lots = [
        {"lot": lot_id, "bdc_numbers": sorted(claims[lot_id], key=_bdc_sort_key)}
        for lot_id in covered_ids
        if len(claims[lot_id]) > 1
    ]

    summarized: Dict[str, dict] = {}
    for cluster_key, cluster_data in clusters.items():
        cluster_ids = cluster_data["cluster_ids"]
        label = _cluster_label(cluster_ids)
        source_references = cluster_data["source_references"]
        if len(source_references) == 1 and " à " in source_references[0]:
            label = source_references[0]

        summarized[cluster_key] = {
            "cluster_key": cluster_key,
            "cluster_ids": cluster_ids,
            "label": label,
            "counts": cluster_data["counts"],
            "total": cluster_data["total"],
            "bdc_numbers": sorted(cluster_data["bdc_numbers"], key=_bdc_sort_key),
            "source_references": source_references,
        }

    totals = {**_init_category_counter(), "unknown": 0}
    for cluster_data in summarized.values():
        counts_map = cluster_data["counts"]
        for cat in (*CATEGORY_KEYS, "unknown"):
            totals[cat] += int(counts_map.get(cat, 0))
    totals["overall"] = sum(totals.values())

    return {
        "clusters": summarized,
        "covered_ids": covered_ids,
        "totals": totals,
        "order_summaries": order_summaries,
        "overlapping_bdc_lots": overlapping_bdc_lots,
    }


def _build_adjacent_index(
    adjacent_months: Optional[Dict[str, Dict[str, Any]]],
) -> Dict[str, Dict[str, Any]]:
    index: Dict[str, Dict[str, Any]] = {}
    if not adjacent_months:
        return index

    for month, payload in adjacent_months.items():
        if not isinstance(payload, dict):
            continue
        report_payload = payload.get("report")
        if not isinstance(report_payload, dict):
            continue
        groups = _collect_reporting_groups(report_payload)
        adjacent_ids = set(groups)
        claims: Dict[str, Set[str]] = defaultdict(set)
        for order in payload.get("bdc") or []:
            if not isinstance(order, dict):
                continue
            numero = str(order.get("numero_bdc") or "UNKNOWN")
            # Restrict range expansion to lots actually present in the adjacent
            # report: a BDC "LOTS 41 à 276" must not index 236 phantom lots.
            for gid in _extract_reference_ids(order, adjacent_ids):
                if not LOT_IDENTIFIER_PATTERN.match(str(gid).strip()):
                    continue
                claims[gid].add(numero)

        for gid in groups:
            index[gid] = {
                "month": month,
                "bdc_numbers": sorted(claims.get(gid, set()), key=_bdc_sort_key),
                "in_report": True,
            }
        for gid, numbers in claims.items():
            if gid not in index:
                index[gid] = {
                    "month": month,
                    "bdc_numbers": sorted(numbers, key=_bdc_sort_key),
                    "in_report": False,
                }
    return index


def _expected_category(duration: float) -> str:
    if duration < 120:
        return VideoCategory.SHORT.value
    if duration < 300:
        return VideoCategory.STANDARD.value
    if duration < 480:
        return VideoCategory.MEDIUM.value
    return VideoCategory.LONG.value


def _format_duration(seconds: float) -> str:
    total = int(round(seconds))
    return f"{total // 60}m{total % 60:02d}s"


def _video_reconciliation_diagnostics(
    video_reconciliation: Dict[str, Any],
    delta: Dict[str, int],
) -> List[dict]:
    """Diagnostics non bloquants issus du rapprochement lot/vidéo."""
    diagnostics: List[dict] = []
    items = video_reconciliation.get("items") or []
    counts = video_reconciliation.get("counts") or {}

    missing = [item for item in items if item.get("status") == "missing_in_production"]
    extra = [item for item in items if item.get("status") == "extra_in_production"]
    mismatched = [item for item in items if item.get("status") == "category_mismatch"]
    drifts = [item for item in items if item.get("status") == "duration_drift"]

    delta_is_zero = all(value == 0 for value in delta.values())
    # Option A : des écarts internes compensés n'invalident pas le BDC.
    if delta_is_zero and (missing or extra or mismatched):
        details = []
        for lot in sorted({item["lot"] for item in missing + extra + mismatched}, key=_sort_key):
            lot_missing = sum(1 for item in missing if item["lot"] == lot)
            lot_extra = sum(1 for item in extra if item["lot"] == lot)
            parts = []
            if lot_missing:
                parts.append(f"{lot_missing} absente(s) de l'archive")
            if lot_extra:
                parts.append(f"{lot_extra} non listée(s) sur le BDC")
            if parts:
                details.append(f"lot {lot} : " + ", ".join(parts))
        diagnostics.append(
            {
                "code": "compensated_lot_mismatch",
                "title": "Écarts internes compensés",
                "severity": "warn",
                "message": (
                    "Le total du BDC est conforme mais des écarts se compensent entre lots"
                    + (" (" + " ; ".join(details[:6]) + ")." if details else ".")
                    + " La facturation reste calculée au niveau du BDC."
                ),
                "lots": sorted(
                    {item["lot"] for item in missing + extra + mismatched}, key=_sort_key
                ),
                "missing": missing[:5],
                "extra": extra[:5],
            }
        )

    if drifts:
        first = drifts[0]
        bdc_duration = first.get("bdc_duration_sec")
        prod_duration = first.get("production_duration_sec")
        diagnostics.append(
            {
                "code": "video_duration_drift",
                "title": "Dérive de durée annexe",
                "severity": "info",
                "message": (
                    f"« {first.get('name')} » (lot {first.get('lot')}) est facturée à "
                    f"{_format_duration(float(bdc_duration)) if bdc_duration is not None else '?'} "
                    f"mais mesurée à "
                    f"{_format_duration(float(prod_duration)) if prod_duration is not None else '?'}"
                    " dans l'archive : la catégorie reste identique."
                ),
                "drift_files": drifts[:5],
            }
        )

    if mismatched:
        diagnostics.append(
            {
                "code": "video_category_mismatch",
                "title": "Catégorie vidéo divergente",
                "severity": "warn",
                "message": (
                    f"{len(mismatched)} vidéo(s) facturée(s) dans une catégorie différente "
                    "de celle mesurée dans l'archive."
                ),
                "mismatch_files": mismatched[:5],
            }
        )

    if missing:
        diagnostics.append(
            {
                "code": "video_missing_in_production",
                "title": "Vidéos facturées absentes de l'archive",
                "severity": "warn",
                "message": (
                    f"{len(missing)} vidéo(s) figurent sur l'annexe du BDC mais sont "
                    "introuvables dans la production (tolérance .mov/.mp4 et radical studio)."
                ),
                "missing_files": missing[:5],
            }
        )

    if counts.get("matched") and not (missing or extra or mismatched or drifts):
        diagnostics.append(
            {
                "code": "video_reconciled",
                "title": "Annexe détaillée rapprochée",
                "severity": "info",
                "message": (
                    f"{counts.get('matched')} vidéo(s) de l'annexe retrouvée(s) "
                    f"dans la production ({video_reconciliation.get('match_rate', 0)} %)."
                ),
            }
        )

    return diagnostics


def _build_cluster_diagnostic(
    cluster_ids: Sequence[str],
    bdc_numbers: Sequence[str],
    billed: Dict[str, int],
    actual: Dict[str, int],
    delta: Dict[str, int],
    missing_reporting_ids: Sequence[str],
    production_files: List[dict],
    overlap_map: Dict[str, List[str]],
    adjacent_index: Dict[str, Dict[str, Any]],
    annex_index: Optional[Dict[str, List[dict]]] = None,
    video_reconciliation: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[dict], List[dict], Dict[str, bool]]:
    diagnostics: List[dict] = []
    billed_unknown = _normalize_count(billed.get("unknown"))

    jingles = [
        item for item in production_files if is_excluded_video_name(str(item.get("name") or ""))
    ]
    duplicate_extras = _duplicate_extras(production_files, annex_index)

    near_boundary = []
    for item in production_files:
        duration_value = _duration_value(item)
        if duration_value is None:
            continue
        threshold = _near_boundary_threshold(duration_value, delta)
        if threshold is None:
            continue
        near_boundary.append(
            {
                **item,
                "threshold": threshold,
                "expected_category": _expected_category(duration_value),
            }
        )

    overlap_lots = [gid for gid in cluster_ids if gid in overlap_map]
    adjacent_hits = [
        (gid, adjacent_index[gid]) for gid in missing_reporting_ids if gid in adjacent_index
    ]

    if billed_unknown:
        diagnostics.append(
            {
                "code": "unknown_articles",
                "title": "Articles BDC non reconnus",
                "severity": "warn",
                "message": (
                    f"{billed_unknown} vidéo(s) facturée(s) sur ce bon de commande "
                    "n'ont pas pu être rattachées à une catégorie de durée. "
                    "Le rapprochement et la certification restent bloqués jusqu'à "
                    "correction de la description ou de la catégorie."
                ),
                "unknown_count": billed_unknown,
            }
        )

    positive_delta = any(value > 0 for value in delta.values())
    has_missing_volume = any(value < 0 for value in delta.values())
    suspects = [{**item, "suspect_kind": "jingle"} for item in jingles] + [
        {**item, "suspect_kind": "duplicate"} for item in duplicate_extras
    ]
    duplicate_resolves = False
    if suspects:
        first = suspects[0]
        kind = "jingle M6+" if jingles else "doublon"
        if positive_delta:
            adjusted_actual = dict(actual)
            for item in suspects:
                category = str(item.get("category") or "")
                if category in adjusted_actual:
                    adjusted_actual[category] = max(0, adjusted_actual[category] - 1)
            adjusted_delta = {
                cat: adjusted_actual.get(cat, 0) - billed.get(cat, 0) for cat in CATEGORY_KEYS
            }
            duplicate_resolves = all(value == 0 for value in adjusted_delta.values())
            suffix = (
                "En l'excluant, ce BDC passerait en parfait accord."
                if duplicate_resolves
                else "Il explique probablement tout ou partie de l'écart."
            )
        else:
            suffix = (
                "Ce fichier apparaît en double dans la production de ce bon de commande."
                if not jingles
                else "Ce fichier semble être un jingle présent dans la production."
            )
        diagnostics.append(
            {
                "code": "jingle_or_duplicate",
                "title": "Jingle / doublon détecté",
                "severity": "warn",
                "message": (
                    f"Le fichier « {first.get('name')} » du lot {first.get('lot')} "
                    f"ressemble à un {kind}. {suffix}"
                ),
                "suspect_files": suspects[:5],
                "suspect_kind": "jingle" if jingles else "duplicate",
            }
        )

    if near_boundary:
        count_near = len(near_boundary)
        if count_near == 1:
            first = near_boundary[0]
            threshold = first["threshold"]
            duration_value = float(first["duration_sec"])
            message = (
                f"La vidéo « {first.get('name')} » du lot {first.get('lot')} est mesurée à "
                f"{_format_duration(duration_value)}, à moins de "
                f"{int(NEAR_BOUNDARY_SECONDS)} s du seuil de {int(threshold)} s. "
                "Une permutation de catégorie peut expliquer l'écart."
            )
        else:
            thresholds = sorted({int(item["threshold"]) for item in near_boundary})
            threshold_labels = [f"{value} s" for value in thresholds]
            if len(threshold_labels) == 1:
                threshold_text = f"du seuil de {threshold_labels[0]}"
            else:
                threshold_text = (
                    "des seuils de "
                    + ", ".join(threshold_labels[:-1])
                    + " et "
                    + threshold_labels[-1]
                )
            message = (
                f"{count_near} vidéos de ce bon de commande sont mesurées à moins de "
                f"{int(NEAR_BOUNDARY_SECONDS)} s {threshold_text}. "
                "Une permutation de catégorie peut expliquer l'écart."
            )
        diagnostics.append(
            {
                "code": "near_boundary",
                "title": "Effet de seuil temporel",
                "severity": "info",
                "message": message,
                "near_files": near_boundary[:5],
            }
        )

    if video_reconciliation:
        diagnostics.extend(_video_reconciliation_diagnostics(video_reconciliation, delta))

    if overlap_lots:
        other_numbers = sorted(
            {
                number
                for lot_id in overlap_lots
                for number in overlap_map.get(lot_id, [])
                if number not in bdc_numbers
            },
            key=_bdc_sort_key,
        )
        diagnostics.append(
            {
                "code": "bdc_overlap",
                "title": "Chevauchement de BDC",
                "severity": "warn",
                "message": (
                    "Les lots "
                    + ", ".join(overlap_lots)
                    + " sont revendiqués par plusieurs bons de commande"
                    + (" : " + ", ".join(other_numbers) + "." if other_numbers else ".")
                ),
                "overlap_lots": overlap_lots,
            }
        )

    if adjacent_hits:
        report_hits = [(gid, info) for gid, info in adjacent_hits if info.get("in_report")]
        bdc_hits = [(gid, info) for gid, info in adjacent_hits if not info.get("in_report")]
        parts = []
        if report_hits:
            months = sorted({info["month"] for _, info in report_hits})
            numbers = sorted(
                {number for _, info in report_hits for number in info.get("bdc_numbers", [])},
                key=_bdc_sort_key,
            )
            detail = (
                "Lot(s) "
                + ", ".join(gid for gid, _ in report_hits)
                + " retrouvé(s) dans le rapport de "
                + ", ".join(months)
            )
            if numbers:
                detail += " (" + ", ".join(numbers) + ")"
            parts.append(detail)
        if bdc_hits:
            months = sorted({info["month"] for _, info in bdc_hits})
            numbers = sorted(
                {number for _, info in bdc_hits for number in info.get("bdc_numbers", [])},
                key=_bdc_sort_key,
            )
            detail = (
                "Lot(s) "
                + ", ".join(gid for gid, _ in bdc_hits)
                + " facturé(s) sur le BDC de "
                + ", ".join(months)
            )
            if numbers:
                detail += " (" + ", ".join(numbers) + ")"
            parts.append(detail)
        diagnostics.append(
            {
                "code": "adjacent_month",
                "title": "Décalage inter-mois",
                "severity": "info",
                "message": " ; ".join(parts) + ".",
                "adjacent_lots": [{"lot": gid, **info} for gid, info in adjacent_hits],
            }
        )

    justification_eligible = bool(
        suspects
        and duplicate_resolves
        and not missing_reporting_ids
        and not has_missing_volume
        and not billed_unknown
    )
    flags = {
        "has_jingle_or_duplicate": bool(jingles or duplicate_extras),
        "has_near_threshold_video": bool(near_boundary),
        "has_unknown_articles": bool(billed_unknown),
        "overlaps_with_bdc": bool(overlap_lots),
        "found_in_adjacent_month": bool(adjacent_hits),
        "justification_eligible": justification_eligible,
    }
    diagnostic = diagnostics[0] if diagnostics else None
    return diagnostic, diagnostics, flags


def _justification_hint(
    *,
    missing_reporting_ids: Sequence[str],
    delta: Dict[str, int],
    has_duplicate: bool,
    has_unknown: bool,
    eligible: bool,
    base_compliant: bool,
) -> Optional[str]:
    """Explique pourquoi un écart peut ou non être justifié."""
    if base_compliant or eligible:
        return None
    if missing_reporting_ids or any(value < 0 for value in delta.values()):
        return "Justification impossible : des volumes manquants subsistent dans la production."
    if has_unknown:
        return "Justification impossible : des articles BDC non reconnus subsistent."
    if has_duplicate:
        return (
            "Justification impossible : le doublon ou le jingle détecté n'explique "
            "qu'une partie de l'écart."
        )
    return "Justification réservée aux écarts causés par un doublon ou un jingle."


def compare_reports(
    report_content: dict,
    bdc_content: List[dict],
    adjacent_months: Optional[Dict[str, Dict[str, Any]]] = None,
    justified_clusters: Optional[Set[str]] = None,
) -> dict:
    reporting_groups = _collect_reporting_groups(report_content)
    reporting_totals = _sum_counts(reporting_groups)
    reporting_ids = set(reporting_groups.keys())

    bdc_summary = _summarize_bdc_orders(bdc_content, reporting_groups)
    covered_ids = {str(item) for item in bdc_summary["covered_ids"]}

    missing_invoices = sorted(reporting_ids - covered_ids, key=_sort_key)
    unexpected_ids = sorted(covered_ids - reporting_ids, key=_sort_key)

    adjacent_index = _build_adjacent_index(adjacent_months)
    overlapping_bdc_lots = bdc_summary["overlapping_bdc_lots"]
    overlap_map = {str(item["lot"]): list(item["bdc_numbers"]) for item in overlapping_bdc_lots}
    justified = set(justified_clusters or set())

    cluster_results: List[dict] = []
    cluster_differences: List[dict] = []
    clusters = bdc_summary["clusters"]
    assert isinstance(clusters, dict)

    orders_by_cluster: Dict[str, List[dict]] = defaultdict(list)
    for order, summary in zip(bdc_content, bdc_summary["order_summaries"]):
        orders_by_cluster[str(summary["cluster_key"])].append(order)

    for cluster_key, cluster_data in clusters.items():
        cluster_ids = cluster_data["cluster_ids"]
        billed_counts = cluster_data["counts"]
        actual_counts, raw_missing_reporting_ids, production_lots, production_files = (
            _collect_cluster_production(reporting_groups, cluster_ids)
        )
        missing_reporting_ids = list(raw_missing_reporting_ids)
        cluster_orders = orders_by_cluster.get(str(cluster_key), [])
        annex_index = _build_annex_video_index(cluster_orders)
        video_reconciliation = _reconcile_cluster_videos(
            cluster_ids, cluster_orders, production_files
        )
        billed_unknown = _normalize_count(billed_counts.get("unknown"))

        delta = {
            cat: actual_counts.get(cat, 0) - billed_counts.get(cat, 0) for cat in CATEGORY_KEYS
        }
        base_compliant = (
            not missing_reporting_ids
            and all(value == 0 for value in delta.values())
            and billed_unknown == 0
        )

        diagnostic, diagnostics, flags = _build_cluster_diagnostic(
            cluster_ids=cluster_ids,
            bdc_numbers=cluster_data["bdc_numbers"],
            billed=billed_counts,
            actual=actual_counts,
            delta=delta,
            missing_reporting_ids=missing_reporting_ids,
            production_files=production_files,
            overlap_map=overlap_map,
            adjacent_index=adjacent_index,
            annex_index=annex_index or None,
            video_reconciliation=video_reconciliation,
        )

        justification_eligible = bool(flags.get("justification_eligible"))
        is_justified = cluster_key in justified and justification_eligible
        is_compliant = base_compliant or is_justified
        overlaps_with_bdc = any(gid in overlap_map for gid in cluster_ids)
        requires_review = (not is_compliant) or overlaps_with_bdc
        justification_hint = _justification_hint(
            missing_reporting_ids=missing_reporting_ids,
            delta=delta,
            has_duplicate=bool(flags.get("has_jingle_or_duplicate")),
            has_unknown=billed_unknown > 0,
            eligible=justification_eligible,
            base_compliant=base_compliant,
        )

        annotated_lots = _annotate_production_lots(
            production_lots=production_lots,
            delta=delta,
            production_files=production_files,
            annex_index=annex_index or None,
        )

        cluster_result = {
            "cluster_key": cluster_key,
            "label": cluster_data["label"],
            "cluster_ids": cluster_ids,
            "bdc_numbers": cluster_data["bdc_numbers"],
            "source_references": cluster_data["source_references"],
            "billed": {cat: billed_counts.get(cat, 0) for cat in CATEGORY_KEYS},
            "billed_unknown": billed_unknown,
            "actual": {cat: actual_counts.get(cat, 0) for cat in CATEGORY_KEYS},
            "delta": delta,
            "billed_total": sum(billed_counts.get(cat, 0) for cat in CATEGORY_KEYS)
            + billed_unknown,
            "actual_total": sum(actual_counts.get(cat, 0) for cat in CATEGORY_KEYS),
            "missing_reporting_ids": missing_reporting_ids,
            "production_lots": annotated_lots,
            "production_files": production_files,
            "is_compliant": is_compliant,
            "requires_review": requires_review,
            "justified": is_justified,
            "justification_hint": justification_hint,
            "diagnostic": diagnostic,
            "diagnostics": diagnostics,
            "bdc_format": "detailed" if video_reconciliation else "simplified",
            "has_video_detail": bool(video_reconciliation),
            "video_reconciliation": video_reconciliation,
            **flags,
        }
        cluster_results.append(cluster_result)
        if not is_compliant:
            cluster_differences.append(cluster_result)

    cluster_by_key = {str(cluster["cluster_key"]): cluster for cluster in cluster_results}
    order_summaries = bdc_summary["order_summaries"]
    compliant_orders = 0
    justified_orders = 0
    for order in order_summaries:
        cluster = cluster_by_key.get(str(order["cluster_key"]))
        if cluster is not None and cluster.get("is_compliant"):
            compliant_orders += 1
            if cluster.get("justified"):
                justified_orders += 1

    total_orders = len(order_summaries)
    score = round(100 * compliant_orders / total_orders, 1) if total_orders else 0.0

    bdc_totals = bdc_summary["totals"]
    balance = {
        cat: {
            "billed": bdc_totals.get(cat, 0),
            "actual": reporting_totals.get(cat, 0),
            "delta": reporting_totals.get(cat, 0) - bdc_totals.get(cat, 0),
        }
        for cat in CATEGORY_KEYS
    }

    missing_invoices_payload = [
        {
            "id_camille": gid,
            "person": reporting_groups[gid]["person"],
            "categories": reporting_groups[gid]["categories"],
            "adjacent_month": adjacent_index.get(gid) or adjacent_index.get(_base_lot_id(gid)),
        }
        for gid in missing_invoices
    ]

    issues_count = (
        len(missing_invoices)
        + len(unexpected_ids)
        + len(cluster_differences)
        + len(overlapping_bdc_lots)
    )

    return {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "reporting": {
            "groups": reporting_groups,
            "totals": reporting_totals,
            "group_count": len(reporting_groups),
        },
        "bdc": {
            "totals": bdc_totals,
            "cluster_count": len(clusters),
            "order_count": total_orders,
        },
        "conformity": {
            "score": score,
            "compliant_orders": compliant_orders,
            "strict_compliant_orders": compliant_orders - justified_orders,
            "justified_orders": justified_orders,
            "total_orders": total_orders,
            "review_clusters": sum(1 for cluster in cluster_results if cluster["requires_review"]),
            "total_clusters": len(cluster_results),
        },
        "certification": {
            "is_certified": score == 100.0 and issues_count == 0,
            "blocking_issues": issues_count,
        },
        "balance": balance,
        "unknown_billed": _normalize_count(bdc_totals.get("unknown")),
        "missing_invoices": missing_invoices_payload,
        "unexpected_ids": unexpected_ids,
        "cluster_differences": cluster_differences,
        "cluster_results": cluster_results,
        "overlapping_bdc_lots": overlapping_bdc_lots,
        "issues_count": issues_count,
    }


def serialize_comparison(result: dict) -> str:
    return json.dumps(result, ensure_ascii=False, indent=2)
