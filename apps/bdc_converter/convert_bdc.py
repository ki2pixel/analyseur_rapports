import json
import logging
import os
import re
import unicodedata
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pdfplumber

from libs.web.io import atomic_write_text

logger = logging.getLogger("analyseur.bdc.convert")

TABLE_SETTINGS = {
    "vertical_strategy": "lines",
    "horizontal_strategy": "lines",
    "snap_tolerance": 3,
    "join_tolerance": 3,
}

NUMERIC_PATTERN = re.compile(
    r"^(?:€|\$)?\s*(?:\d{1,3}(?:[ \u00a0\u202f]\d{3})+|\d+)"
    r"(?:[.,]\d+)?\s*(?:€|\$)?(?:\s+\w+)?$",
    re.UNICODE,
)
REFERENCE_PATTERN = re.compile(
    r"(?:N[°º]?\s*(?:de)?\s*réf|référence|réf\.?)\s*:\s*(.+)", re.IGNORECASE
)
REFERENCE_SPLIT_PATTERN = re.compile(r"(?:/|\\|,|;|\bet\b)", re.IGNORECASE)
RANGE_DETECT_PATTERN = re.compile(r"\b(\d+)\s*(?:à|au|-)\s*(\d+)\b", re.IGNORECASE)
PHONE_NUMBER_PATTERN = re.compile(
    r"(?<!\w)(?:\+33\s?(?:\(0\)\s?)?|0)\d(?:[\s./-]?\d{2}){4}\b", re.UNICODE
)
EMAIL_PATTERN = re.compile(r"\b[\w.%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b", re.UNICODE)
LOT_REFERENCE_PATTERN = re.compile(r"^(?:LOTS?\s*)?[\d\s/,-]+$", re.IGNORECASE)
LOT_SEQUENCE_CAPTURE_PATTERN = re.compile(r"(?:LOTS?\s*)[\d\s/,-]+", re.IGNORECASE)
HEADER_KEYWORDS = ("description", "quant", "taux", "montant")
MIN_COLUMNS_FOR_ARTICLE = 3

BDC_NUMBER_PATTERN = re.compile(r"^BDC-(?:[A-Z0-9]{1,12}-)+\d{1,8}$")
BDC_NUMBER_SEARCH_PATTERN = re.compile(r"\bBDC-(?:[A-Z0-9]{1,12}-)+\d{1,8}\b", re.IGNORECASE)
SUBTOTAL_PATTERN = re.compile(r"sous[- ]?total\s*(?:€\s*)?([\d\s\u00a0\u202f.,]+)", re.IGNORECASE)
TOTAL_PATTERN = re.compile(r"\btotal\b\s*(?:€\s*)?([\d\s\u00a0\u202f.,]+)", re.IGNORECASE)
MONEY_TOLERANCE = Decimal("0.01")

HEADER_LINE_TOLERANCE = 2.0
HEADER_GAP_TOLERANCE = 12.0
HEADER_SYNONYMS = {
    "index": {"#", "n°", "no", "num", "numero"},
    "description": {"description", "designation", "libelle", "prestation"},
    "quantite": {"quantite", "qte", "qty", "nombre"},
    "unite": {"unite", "unit"},
    "taux": {"taux", "pu", "prix", "unitaire", "p.u.", "p.u"},
    "montant": {"montant", "total", "amount"},
}
UNIT_WORDS = {"unite", "u", "h", "heure", "heures", "jour", "jours", "forfait"}
FOOTER_PATTERN = re.compile(
    r"^(?:adresse|facturation|livraison|tva|siret|siren|www\.|https?://|t[ée]l[ée]phone|tel\b)",
    re.IGNORECASE,
)
EXPECTED_LAYOUT_FINGERPRINT = "index | description | quantite | taux | montant"

ANNEX_HEADER_SYNONYMS = {
    "lot": {"lot", "n°", "no", "num"},
    "video": {"video", "titre", "fichier", "nom"},
    "duree": {"duree"},
    "categorie": {"categorie"},
    "montant": {"montant", "total"},
}
ANNEX_REQUIRED_COLUMNS = {"lot", "video", "duree", "categorie", "montant"}
ANNEX_TITLE = re.compile(r"^annexe\b", re.IGNORECASE)
ANNEX_DURATION_MIN_SEC_PATTERN = re.compile(r"^(\d{1,3})\s*min\s*(\d{1,2})$", re.IGNORECASE)
ANNEX_DURATION_SEC_PATTERN = re.compile(r"^(\d{1,4})\s*s$", re.IGNORECASE)
ANNEX_MONEY_PATTERN = re.compile(r"^\d{1,3}(?:[ \u00a0\u202f]\d{3})*,\d{2}$")
ANNEX_CATEGORY_WORDS = {
    "courte": "short",
    "court": "short",
    "standard": "standard",
    "moyenne": "medium",
    "moyen": "medium",
    "longue": "long",
    "long": "long",
}
ANNEX_SUMMARY_TOTAL = re.compile(
    r"Total\s+des\s+vid[ée]os?\s*:\s*(\d+)\s+vid[ée]os?\s+([\d\s\u00a0\u202f.,]+)",
    re.IGNORECASE,
)
ANNEX_SUMMARY_ROW = re.compile(r"^\d+\s*×")
PERIOD_PATTERN = re.compile(r"P[ée]riode\s*:\s*(.+)", re.IGNORECASE)
SIRET_PATTERN = re.compile(r"\bSIRET\b.*$", re.IGNORECASE)
CONCERNED_LOTS_HEADER = re.compile(r"Lots?\s+concern[ée]s?\s*\(\s*(\d+)\s*\)", re.IGNORECASE)
ANNEX_DECLARED_PATTERN = re.compile(
    r"D[ée]tail\s+des?\s+(\d+)\s+vid[ée]os?\s+en\s+annexe", re.IGNORECASE
)
VIDEO_EXTENSION_PATTERN = re.compile(r"\s*(\.(?:mp4|mov|mxf|avi|mkv))$", re.IGNORECASE)

DEFAULT_MAX_PDF_FILES = 200
DEFAULT_MAX_PDF_BYTES = 20 * 1024 * 1024


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        return default


def parse_french_date(value) -> date | None:
    """Parse une date au format français strict JJ/MM/AAAA.

    Aucune interprétation US n'est tentée : une date ambiguë comme 01/10/2026
    reste le 1er octobre, et un format ISO est rejeté.
    """
    if not value:
        return None
    text = str(value).strip()
    if not re.fullmatch(r"\d{2}/\d{2}/\d{4}", text):
        return None
    try:
        return datetime.strptime(text, "%d/%m/%Y").date()
    except ValueError:
        return None


def _to_decimal(value) -> Decimal | None:
    """Convertit un montant français (``1 056,00``, ``€8,00``) en Decimal."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    cleaned = (
        text.replace("\u00a0", "")
        .replace("\u202f", "")
        .replace(" ", "")
        .replace("€", "")
        .replace("$", "")
        .replace(",", ".")
    )
    try:
        return Decimal(cleaned)
    except (InvalidOperation, ValueError):
        return None


def _issue(code: str, severity: str, message: str) -> dict:
    return {"code": code, "severity": severity, "message": message}


def _status_from_issues(issues) -> str:
    if any(issue["severity"] == "error" for issue in issues):
        return "failed"
    if any(issue["severity"] == "warning" for issue in issues):
        return "partial"
    return "ok"


def _clean_reference_line(value):
    if not value:
        return ""
    without_phone = PHONE_NUMBER_PATTERN.sub(" ", value)
    without_email = EMAIL_PATTERN.sub(" ", without_phone)
    compact_spaces = re.sub(r"\s{2,}", " ", without_email)
    return compact_spaces.strip(" /;,|-").strip()


def _looks_like_date(value: str) -> bool:
    return bool(re.fullmatch(r"\d{2}/\d{2}/\d{4}", value.strip()))


def _extract_preceding_lot_sequence(full_text, reference_start_index):
    if reference_start_index <= 0:
        return ""
    preceding_text = full_text[:reference_start_index]
    lines = [line.strip() for line in preceding_text.splitlines() if line.strip()]
    for line in reversed(lines[-4:]):
        match = LOT_SEQUENCE_CAPTURE_PATTERN.search(line)
        if match:
            return _clean_reference_line(match.group(0))
    return ""


def _format_lot_reference(parts):
    if not parts:
        return None, []
    prefix = "LOTS" if len(parts) > 1 else "LOT"
    formatted_value = f"{prefix} {'/'.join(parts)}"
    formatted_parts = [f"{prefix} {parts[0]}"]
    if len(parts) > 1:
        formatted_parts.extend(parts[1:])
    return formatted_value, formatted_parts


def _split_reference_parts(candidate_line):
    parts = [
        part.strip()
        for part in REFERENCE_SPLIT_PATTERN.split(candidate_line)
        if part and not REFERENCE_SPLIT_PATTERN.fullmatch(part) and part.strip()
    ]
    if not parts and candidate_line:
        parts = [candidate_line]
    return parts


def _normalize_range_part(part):
    normalized = RANGE_DETECT_PATTERN.sub(r"\1 à \2", part)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    if not re.match(r"^LOTS?\b", normalized, re.IGNORECASE):
        normalized = f"LOTS {normalized}"
    else:
        normalized = re.sub(r"^LOT\b", "LOTS", normalized, count=1, flags=re.IGNORECASE)
    return normalized


def _normalize_range_reference(candidate_line):
    """Canonicalise les plages, y compris mélangées à des lots séparés par '/'."""
    if not RANGE_DETECT_PATTERN.search(candidate_line):
        return None, []

    raw_parts = _split_reference_parts(candidate_line)
    normalized_parts = []
    for part in raw_parts:
        if RANGE_DETECT_PATTERN.search(part):
            normalized_parts.append(_normalize_range_part(part))
        else:
            normalized_parts.append(part)

    cleaned_parts = [
        re.sub(r"^LOTS?\s+", "", part, count=1, flags=re.IGNORECASE).strip()
        for part in normalized_parts
    ]
    value = "LOTS " + "/".join(part for part in cleaned_parts if part)
    return value.strip(), normalized_parts


def _extract_reference(text):
    match = REFERENCE_PATTERN.search(text)
    if not match:
        return None, []

    first_raw = match.group(1).strip()
    first_clean = _clean_reference_line(first_raw)

    if not first_clean:
        prefix = _extract_preceding_lot_sequence(text, match.start())
        if not prefix:
            return None, []
        candidate_line = prefix
    else:
        candidate_line = first_clean

        starts_with_continuation = first_raw.startswith("/") or (
            LOT_REFERENCE_PATTERN.match(first_clean)
            and not re.match(r"^LOTS?\b", first_clean, re.IGNORECASE)
        )
        if starts_with_continuation:
            prefix = _extract_preceding_lot_sequence(text, match.start())
            if prefix:
                candidate_line = prefix.rstrip("/")
                suffix = first_clean.strip("/")
                if suffix:
                    candidate_line += "/" + suffix

        # The reference may wrap over several lines after the label, e.g.
        # ``N° de réf : LOTS 18/19/`` then ``20/23``.  Consume lot-like
        # continuations until a date or any unrelated line.
        for raw_line in text[match.end() :].splitlines():
            stripped = raw_line.strip()
            if not stripped:
                continue
            cleaned = _clean_reference_line(stripped)
            if not cleaned:
                continue
            if _looks_like_date(cleaned):
                break
            if not (stripped.startswith("/") or LOT_REFERENCE_PATTERN.match(cleaned)):
                break
            suffix = cleaned.strip("/")
            if not suffix:
                continue
            candidate_line = candidate_line.rstrip("/") + "/" + suffix

    normalized_range, _range_parts = _normalize_range_reference(candidate_line)
    if normalized_range:
        # A range is kept as a single reference so the original bounds and any
        # mixed explicit lot ("LOTS 1/38 à 42") survive round-tripping.
        return normalized_range, [normalized_range]

    if LOT_REFERENCE_PATTERN.match(candidate_line):
        lot_numbers = re.findall(r"\d+", candidate_line)
        formatted_value, formatted_parts = _format_lot_reference(lot_numbers)
        if formatted_value:
            return formatted_value, formatted_parts

    parts = _split_reference_parts(candidate_line)
    return candidate_line or None, parts


def _normalize_cell(value):
    if not value:
        return ""
    return re.sub(r"\s+", " ", value.replace("\n", " ")).strip()


def _extract_totals_from_table(table):
    """Récupère Sous-total et Total d'une table, sans confondre les notes libres.

    Les PDF du client fusionnent parfois ``Sous-total``, ``Total €…`` et une
    annotation (``Total +44 €``) dans la même cellule. On prend donc le premier
    montant valide et on retire ``Sous-total`` avant de chercher ``Total``.
    """
    subtotal = None
    total = None
    for raw_row in table:
        joined = _normalize_cell(" ".join(cell for cell in raw_row if cell))
        if not joined:
            continue
        if subtotal is None:
            match = SUBTOTAL_PATTERN.search(joined)
            if match and _to_decimal(match.group(1)) is not None:
                subtotal = match.group(1).strip()
        if total is None:
            without_subtotal = SUBTOTAL_PATTERN.sub(" ", joined)
            match = TOTAL_PATTERN.search(without_subtotal)
            if match and _to_decimal(match.group(1)) is not None:
                total = match.group(1).strip()
    return {"subtotal": subtotal, "total": total}


def _looks_like_numeric(value):
    normalized = value.replace("€", "").replace("$", "").strip()
    return bool(NUMERIC_PATTERN.match(normalized))


def _pop_numeric_from_end(cells):
    for idx in range(len(cells) - 1, -1, -1):
        if _looks_like_numeric(cells[idx]):
            return cells.pop(idx)
    return ""


def _row_is_total(cells):
    joined = " ".join(cells).lower()
    return bool(re.search(r"\b(?:sous[- ]?total|total)\b", joined))


def _row_is_header(cells):
    joined = " ".join(cells).lower()
    return all(keyword in joined for keyword in HEADER_KEYWORDS)


def _split_quantity_and_unit(value):
    normalized = _normalize_cell(value)
    if not normalized:
        return "", ""
    match = re.match(r"^([\d\s\u00a0\u202f.,]+?)\s+([^\d\s].*)$", normalized)
    if match:
        return match.group(1).strip(), match.group(2).strip()
    return normalized, ""


def _normalize_header_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    without_accents = "".join(
        character for character in normalized if not unicodedata.combining(character)
    )
    return re.sub(r"\s+", " ", without_accents).strip().lower()


def _group_words_into_lines(words, tolerance: float = HEADER_LINE_TOLERANCE):
    lines: list[dict] = []
    for word in sorted(words, key=lambda item: (item["top"], item["x0"])):
        for line in lines:
            if abs(line["top"] - word["top"]) <= tolerance:
                line["words"].append(word)
                line["top"] = min(line["top"], word["top"])
                break
        else:
            lines.append({"top": word["top"], "words": [word]})
    for line in lines:
        line["words"].sort(key=lambda item: item["x0"])
    return sorted(lines, key=lambda line: line["top"])


def _cluster_header_words(words):
    clusters = []
    current = None
    for word in sorted(words, key=lambda item: item["x0"]):
        if current is None or word["x0"] - current["text_end"] > HEADER_GAP_TOLERANCE:
            current = {
                "words": [word],
                "start": word["x0"],
                "text_end": word["x1"],
            }
            clusters.append(current)
        else:
            current["words"].append(word)
            current["text_end"] = max(current["text_end"], word["x1"])
    return clusters


def _match_header_name(cluster):
    tokens = []
    for word in cluster["words"]:
        tokens.extend(_normalize_header_text(word["text"]).split())
    normalized_text = " ".join(tokens)
    for name, synonyms in HEADER_SYNONYMS.items():
        if normalized_text in synonyms:
            return name
    for name, synonyms in HEADER_SYNONYMS.items():
        if any(token in synonyms for token in tokens):
            return name
    return None


def _build_columns_from_clusters(clusters, page_width: float):
    columns = []
    for index, cluster in enumerate(clusters):
        end = clusters[index + 1]["start"] if index + 1 < len(clusters) else page_width
        columns.append(
            {
                "name": _match_header_name(cluster),
                "label": " ".join(word["text"] for word in cluster["words"]).strip(),
                "start": cluster["start"],
                "end": end,
            }
        )
    return columns


def _detect_table_header(lines, page_width: float):
    for line in lines:
        columns = _build_columns_from_clusters(
            _cluster_header_words(line["words"]),
            page_width,
        )
        names = {column["name"] for column in columns if column["name"]}
        if {"description", "quantite", "montant"} <= names:
            return line["top"], columns
    return None, []


def _build_layout(columns):
    parts = []
    for column in columns:
        if column["name"]:
            parts.append(column["name"])
        elif column.get("label"):
            parts.append(_normalize_header_text(column["label"]))
    fingerprint = " | ".join(part for part in parts if part) or None
    return {
        "fingerprint": fingerprint,
        "expected": EXPECTED_LAYOUT_FINGERPRINT,
        "changed": fingerprint != EXPECTED_LAYOUT_FINGERPRINT,
        "columns": [
            {"name": column["name"], "label": column.get("label", "")} for column in columns
        ],
    }


def _empty_layout():
    return {
        "fingerprint": None,
        "expected": EXPECTED_LAYOUT_FINGERPRINT,
        "changed": True,
        "columns": [],
    }


def _extract_layout(words, page_width: float):
    lines = _group_words_into_lines(words)
    header_top, columns = _detect_table_header(lines, page_width)
    if header_top is None:
        return _empty_layout()
    return _build_layout(columns)


def _layout_message(layout) -> str:
    if layout["fingerprint"]:
        return (
            f"Structure du tableau modifiée : attendu « {layout['expected']} », "
            f"détecté « {layout['fingerprint']} »."
        )
    return "En-tête de tableau non reconnu ; extraction par table de secours utilisée."


def _column_name_for_word(word, columns):
    best_name = None
    best_overlap = 0.0
    for column in columns:
        overlap = min(word["x1"], column["end"]) - max(word["x0"], column["start"])
        if overlap > best_overlap:
            best_overlap = overlap
            best_name = column["name"]
    if best_name is not None:
        return best_name
    center = (word["x0"] + word["x1"]) / 2
    nearest = min(
        columns,
        key=lambda column: abs(center - (column["start"] + column["end"]) / 2),
    )
    return nearest["name"]


def _looks_like_unit(value: str) -> bool:
    return _normalize_header_text(value) in UNIT_WORDS


def _extract_articles_from_words(words, page_width: float):
    lines = _group_words_into_lines(words)
    header_top, columns = _detect_table_header(lines, page_width)
    if header_top is None:
        return []

    articles = []
    current = None
    last_article_top = None
    for line in lines:
        if line["top"] <= header_top + 1:
            continue
        if _detect_table_header([line], page_width)[0] is not None:
            continue

        cells: dict[str, list[str]] = {}
        for column in columns:
            name = column["name"]
            if name:
                cells[name] = []
        for word in line["words"]:
            name = _column_name_for_word(word, columns)
            if name:
                cells[name].append(word["text"])
        normalized = {name: _normalize_cell(" ".join(values)) for name, values in cells.items()}

        joined = " ".join(value for value in normalized.values() if value).lower()

        index_text = normalized.get("index", "")
        description_text = normalized.get("description", "")
        quantity_text = normalized.get("quantite", "")
        rate_text = normalized.get("taux", "")
        amount_text = normalized.get("montant", "")

        is_total_line = bool(re.search(r"sous[- ]?total|\btotal\b", joined)) and (
            not description_text
            or _normalize_header_text(description_text) in {"total", "sous-total"}
        )
        if is_total_line:
            break

        if (
            current is not None
            and last_article_top is not None
            and not _looks_like_numeric(quantity_text)
            and not _looks_like_numeric(amount_text)
            and (FOOTER_PATTERN.match(description_text) or line["top"] - last_article_top > 60)
        ):
            break

        is_new_article = (
            bool(re.fullmatch(r"\d+", index_text))
            or (
                bool(description_text)
                and (_looks_like_numeric(quantity_text) or _looks_like_numeric(amount_text))
            )
            or (_looks_like_numeric(quantity_text) and _looks_like_numeric(amount_text))
        )
        if not is_new_article:
            if current is None:
                continue
            unit_text = ""
            if _looks_like_unit(quantity_text):
                unit_text = quantity_text
            elif _looks_like_unit(description_text):
                unit_text = description_text

            if unit_text:
                if not current["unite"]:
                    current["unite"] = unit_text
            elif quantity_text and not _looks_like_numeric(quantity_text) and not description_text:
                if not current["unite"]:
                    current["unite"] = quantity_text
            elif (
                description_text
                and not _looks_like_numeric(quantity_text)
                and not _looks_like_numeric(amount_text)
            ):
                current["description"] = _normalize_cell(
                    f"{current['description']} {description_text}"
                )
            continue

        quantity_value, unit = _split_quantity_and_unit(quantity_text)
        if not unit:
            unit = normalized.get("unite", "")
        description = re.sub(r"^\d+\s*", "", description_text).strip()
        current = {
            "description": description,
            "quantite": quantity_value,
            "unite": unit,
            "taux": rate_text,
            "montant": amount_text,
        }
        articles.append(current)
        last_article_top = line["top"]

    return articles


def _extract_first_amount(value):
    if not value:
        return None
    match = re.search(r"[€$]?\s*\d[\d\s\u00a0\u202f.,]*", value)
    if not match:
        return None
    candidate = re.sub(r"^[€$]\s*", "", match.group(0)).strip()
    if _to_decimal(candidate) is None:
        return None
    return candidate


def _parse_annex_duration(text: str) -> int | None:
    """Convertit ``0 min 18`` / ``71 s`` en secondes."""
    normalized = _normalize_cell(text)
    if not normalized:
        return None
    match = ANNEX_DURATION_MIN_SEC_PATTERN.match(normalized)
    if match:
        return int(match.group(1)) * 60 + int(match.group(2))
    match = ANNEX_DURATION_SEC_PATTERN.match(normalized)
    if match:
        return int(match.group(1))
    return None


def _is_annex_header_line(line) -> bool:
    """Détecte ``Lot | Vidéo | Durée | Catégorie | Montant`` par coordonnées."""
    names = set()
    for word in line["words"]:
        token = _normalize_header_text(word["text"]).strip(":.")
        for name, synonyms in ANNEX_HEADER_SYNONYMS.items():
            if token in synonyms:
                names.add(name)
                break
    return ANNEX_REQUIRED_COLUMNS <= names


def _looks_like_page_number(line, page_height: float) -> bool:
    """Ignore le folio isolé en pied de page (``2``, ``3``…)."""
    words = line["words"]
    if len(words) != 1:
        return False
    if not re.fullmatch(r"\d{1,3}", words[0]["text"].strip()):
        return False
    return line["top"] > page_height - 80


def _is_annex_page_text(text: str) -> bool:
    first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    return bool(ANNEX_TITLE.match(first_line))


def _annex_row_from_line(line, page_height: float, lot_top: float | None):
    """Extrait une ligne d'annexe, pilotée par les types (montant → catégorie → durée).

    Le titre occupe le reste des jetons : il peut contenir des nombres, ce qui
    rend l'affectation par recouvrement horizontal fragile.  On repère donc les
    colonnes de droite par leur type et tout ce qui précède est le titre.
    """
    if _looks_like_page_number(line, page_height):
        return None

    words = line["words"]
    texts = [word["text"] for word in words]
    if not texts:
        return None

    first = words[0]
    lot_text = first["text"].strip()
    if not re.fullmatch(r"\d{1,4}", lot_text):
        return None
    if lot_top is not None and first["x0"] > lot_top:
        return None
    if _normalize_header_text(first["text"]) in {"lot", "n°", "no", "num"}:
        return None

    body = texts[1:]
    if not body:
        return None

    montant = ""
    if ANNEX_MONEY_PATTERN.match(body[-1].strip()):
        montant = body.pop()

    category_label = None
    if body:
        candidate = _normalize_header_text(body[-1])
        if candidate in ANNEX_CATEGORY_WORDS:
            category_label = body.pop()

    duration_text = None
    duration_sec = None
    # La durée s'écrit sur trois jetons (« 0 min 18 ») ou deux (« 71 s »).
    if len(body) >= 3:
        candidate = _normalize_cell(" ".join(body[-3:]))
        if ANNEX_DURATION_MIN_SEC_PATTERN.match(candidate):
            duration_text = candidate
            duration_sec = _parse_annex_duration(candidate)
            body = body[:-3]
    if duration_sec is None and len(body) >= 2:
        candidate = _normalize_cell(" ".join(body[-2:]))
        if ANNEX_DURATION_SEC_PATTERN.match(candidate):
            duration_text = candidate
            duration_sec = _parse_annex_duration(candidate)
            body = body[:-2]
    if duration_sec is None and body:
        candidate = body[-1].strip()
        if ANNEX_DURATION_SEC_PATTERN.match(candidate):
            duration_text = candidate
            duration_sec = _parse_annex_duration(candidate)
            body = body[:-1]

    video_name = _normalize_cell(" ".join(body))
    if not video_name:
        return None
    # Sans durée, la ligne est une continuation de titre et non une vidéo.
    if duration_sec is None:
        return None

    return {
        "lot": lot_text,
        "video_name": video_name,
        "duration_text": duration_text,
        "duration_sec": duration_sec,
        "category_label": category_label,
        "montant": montant or None,
    }


def _extract_annex_items(pages) -> dict:
    """Extrait l'annexe vidéo multi-pages d'un BDC détaillé.

    Une vidéo peut occuper plusieurs lignes PDF : seule la première porte le lot,
    la durée, la catégorie et le montant.  Les lignes suivantes n'ont pas
    d'identifiant de lot et sont donc recollées au titre précédent.
    """
    items: list[dict] = []
    summary: dict = {
        "total_videos": None,
        "total_ht": None,
        "category_rows": [],
        "pages": [],
    }
    lot_header_top: float | None = None

    for page in pages:
        text = page.extract_text() or ""
        if not _is_annex_page_text(text):
            continue
        summary["pages"].append(page.page_number)
        lines = _group_words_into_lines(page.extract_words())
        header_seen = False
        for line in lines:
            if _is_annex_header_line(line):
                header_seen = True
                lot_words = [
                    word
                    for word in line["words"]
                    if _normalize_header_text(word["text"]).strip(":.") in {"lot", "n°", "no"}
                ]
                if lot_words:
                    lot_header_top = max(word["x1"] for word in lot_words)
                continue
            if not header_seen:
                continue
            if _looks_like_page_number(line, page.height):
                continue

            joined = _normalize_cell(" ".join(word["text"] for word in line["words"]))
            if not joined:
                continue
            if ANNEX_SUMMARY_ROW.match(joined):
                summary["category_rows"].append(joined)
                continue
            total_match = ANNEX_SUMMARY_TOTAL.search(joined)
            if total_match:
                summary["total_videos"] = total_match.group(1)
                summary["total_ht"] = _to_decimal(total_match.group(2))
                continue
            if ANNEX_TITLE.match(joined) or joined.lower().startswith("bon de commande"):
                continue

            if not _annex_line_starts_lot(line, lot_header_top):
                # Continuation d'un titre coupé sur plusieurs lignes.
                if items:
                    items[-1]["video_name"] = _normalize_cell(f"{items[-1]['video_name']} {joined}")
                continue

            row = _annex_row_from_line(line, page.height, lot_header_top)
            if row:
                items.append(row)

    cleaned: list[dict] = []
    for item in items:
        if not item.get("video_name"):
            continue
        item["video_name"] = VIDEO_EXTENSION_PATTERN.sub(r"\1", _normalize_cell(item["video_name"]))
        cleaned.append(item)
    cleaned = [item for item in cleaned if item.get("duration_sec") is not None]
    summary["total_ht"] = str(summary["total_ht"]) if summary["total_ht"] is not None else None
    return {"items": cleaned, "summary": summary}


def _annex_line_starts_lot(line, lot_header_top: float | None) -> bool:
    """Une nouvelle vidéo commence par un identifiant numérique dans la colonne Lot."""
    words = line["words"]
    if not words:
        return False
    first = words[0]
    if not re.fullmatch(r"\d{1,4}", first["text"].strip()):
        return False
    return lot_header_top is None or first["x0"] <= lot_header_top


def _extract_period(text: str) -> str | None:
    match = PERIOD_PATTERN.search(text)
    if not match:
        return None
    value = SIRET_PATTERN.sub("", match.group(1))
    value = _normalize_cell(value)
    return value or None


def _extract_concerned_lots(text: str) -> tuple[list[str], int | None]:
    """Lit le bloc ``Lots concernés (N)`` et retourne les identifiants explicités."""
    match = CONCERNED_LOTS_HEADER.search(text)
    if not match:
        return [], None
    try:
        declared = int(match.group(1))
    except (TypeError, ValueError):
        declared = None

    block = text[match.end() :]
    stop = re.search(r"#\s*Description", block, re.IGNORECASE)
    if stop:
        block = block[: stop.start()]
    else:
        stop = re.search(r"\n\s*(?:Sous-total|Total)\b", block, re.IGNORECASE)
        if stop:
            block = block[: stop.start()]

    lots: list[str] = []
    seen: set[str] = set()
    for number in re.findall(r"\d{1,6}", block):
        if number not in seen:
            seen.add(number)
            lots.append(number)
    return lots, declared


def _extract_annex_declared_count(text: str) -> int | None:
    match = ANNEX_DECLARED_PATTERN.search(text)
    if not match:
        return None
    try:
        return int(match.group(1))
    except (TypeError, ValueError):
        return None


def _extract_totals_from_words(words):
    subtotal = None
    total = None
    for line in _group_words_into_lines(words):
        texts = [word["text"] for word in line["words"]]
        normalized = [_normalize_header_text(text) for text in texts]

        if subtotal is None:
            for index, text in enumerate(normalized):
                if re.search(r"sous[- ]?total", text):
                    subtotal = _extract_first_amount(" ".join(texts[index + 1 :]))
                    break

        if total is None:
            for index, text in enumerate(normalized):
                if text == "total":
                    raw = _normalize_cell(" ".join(texts[index + 1 :]))
                    if raw.startswith("+"):
                        break
                    # « Total des vidéos : 134 vidéos » n'est pas un total monétaire.
                    if re.match(r"^(?:des|de)\b", raw, re.IGNORECASE):
                        break
                    total = _extract_first_amount(raw)
                    break

    return {"subtotal": subtotal, "total": total}


def _extract_metadata_from_words(words):
    metadata = {"numero_bdc": None, "date_commande": None, "date_livraison": None}
    for line in _group_words_into_lines(words):
        texts = [word["text"] for word in line["words"]]
        joined = " ".join(texts)
        normalized = _normalize_header_text(joined)

        if metadata["numero_bdc"] is None:
            match = BDC_NUMBER_SEARCH_PATTERN.search(joined)
            if match:
                metadata["numero_bdc"] = match.group(0).upper()

        if metadata["date_commande"] is None and re.search(
            r"\bdate\b(?!\s+(?:de\s+)?livraison)", normalized
        ):
            for text in texts:
                if re.fullmatch(r"\d{2}/\d{2}/\d{4}", text):
                    metadata["date_commande"] = text
                    break

        if metadata["date_livraison"] is None and "livraison" in normalized:
            for text in texts:
                if re.fullmatch(r"\d{2}/\d{2}/\d{4}", text):
                    metadata["date_livraison"] = text
                    break

    return metadata


def _parse_articles_from_table(table):
    articles = []
    for raw_row in table:
        cleaned_cells = [_normalize_cell(cell) for cell in raw_row if _normalize_cell(cell)]
        if len(cleaned_cells) < MIN_COLUMNS_FOR_ARTICLE:
            continue
        header_cell = cleaned_cells[0].lower()
        if "description" in header_cell or _row_is_header(cleaned_cells):
            continue
        if _row_is_total(cleaned_cells):
            continue

        working_cells = cleaned_cells[:]
        montant = _pop_numeric_from_end(working_cells)
        taux = _pop_numeric_from_end(working_cells)
        quantite = _pop_numeric_from_end(working_cells)
        description = " ".join(working_cells).strip()
        description = re.sub(r"^\d+\s*", "", description)
        quantite_value, unite = _split_quantity_and_unit(quantite)

        # On ignore les lignes qui n'ont pas de description utile.
        if not description and not quantite_value and not taux and not montant:
            continue

        articles.append(
            {
                "description": description,
                "quantite": quantite_value,
                "unite": unite,
                "taux": taux,
                "montant": montant,
            }
        )
    return articles


def validate_bdc_data(data) -> list[dict]:
    """Contrôle la cohérence d'un BDC extrait et retourne les alertes.

    ``error`` bloque l'écriture dans le batch, ``warning`` marque le document
    comme partiel, ``info`` est purement informatif.
    """
    issues: list[dict] = []

    numero_bdc = str(data.get("numero_bdc") or "").strip()
    if not numero_bdc:
        issues.append(_issue("numero_bdc_absent", "error", "Numéro de BDC introuvable."))
    elif not BDC_NUMBER_PATTERN.match(numero_bdc):
        issues.append(
            _issue(
                "numero_bdc_invalide",
                "error",
                f"Numéro de BDC invalide : {numero_bdc!r}.",
            )
        )

    if parse_french_date(data.get("date_commande")) is None:
        issues.append(
            _issue(
                "date_commande_invalide",
                "error",
                "Date de commande absente ou invalide (JJ/MM/AAAA attendu).",
            )
        )

    date_livraison = data.get("date_livraison")
    if date_livraison:
        if parse_french_date(date_livraison) is None:
            issues.append(
                _issue(
                    "date_livraison_invalide",
                    "warning",
                    "Date de livraison invalide (JJ/MM/AAAA attendu).",
                )
            )
    else:
        issues.append(_issue("date_livraison_absente", "info", "Date de livraison absente."))

    if not data.get("reference"):
        issues.append(_issue("reference_absente", "warning", "Référence de lots absente."))

    articles = data.get("articles") or []
    if not articles:
        issues.append(_issue("articles_absents", "error", "Aucun article extrait du tableau."))
        return issues

    total_amount = Decimal("0")
    for index, article in enumerate(articles, start=1):
        description = str(article.get("description") or "").strip()
        if not description:
            issues.append(
                _issue("article_sans_description", "warning", f"Article {index} sans description.")
            )

        quantite = _to_decimal(article.get("quantite"))
        taux = _to_decimal(article.get("taux"))
        montant = _to_decimal(article.get("montant"))

        if quantite is None or taux is None or montant is None:
            issues.append(
                _issue(
                    "article_incomplet",
                    "warning",
                    f"Article {index} incomplet (quantité/taux/montant).",
                )
            )
            if montant is not None:
                total_amount += montant
            continue

        total_amount += montant
        if abs(quantite * taux - montant) > MONEY_TOLERANCE:
            issues.append(
                _issue(
                    "montant_incoherent",
                    "warning",
                    f"Article {index} : {quantite} × {taux} ≠ {montant}.",
                )
            )

    totals = data.get("totals") or {}
    reference_total = _to_decimal(totals.get("total"))
    if reference_total is None:
        reference_total = _to_decimal(totals.get("subtotal"))

    if reference_total is None:
        issues.append(_issue("total_absent", "warning", "Total du BDC introuvable."))
    elif abs(total_amount - reference_total) > MONEY_TOLERANCE:
        issues.append(
            _issue(
                "total_incoherent",
                "warning",
                f"Somme des articles ({total_amount}) ≠ total ({reference_total}).",
            )
        )

    issues.extend(_validate_detailed_format(data, articles, reference_total))
    return issues


def _validate_detailed_format(data, articles, reference_total) -> list[dict]:
    """Contrôles croisés non bloquants propres au format détaillé."""
    issues: list[dict] = []
    if str(data.get("bdc_format") or "simplified") != "detailed":
        return issues

    concerned_lots = list(data.get("concerned_lots") or [])
    declared_count = data.get("concerned_lots_declared_count")
    if declared_count is not None and len(concerned_lots) != declared_count:
        issues.append(
            _issue(
                "lots_concernes_incoherents",
                "warning",
                f"Lots concernés : {len(concerned_lots)} identifiés "
                f"pour {declared_count} annoncés.",
            )
        )

    annex_videos = list(data.get("annex_videos") or [])
    annex_lots = {str(item.get("lot")) for item in annex_videos if item.get("lot")}
    if concerned_lots and annex_videos:
        unknown_lots = sorted(annex_lots - set(concerned_lots))
        if unknown_lots:
            issues.append(
                _issue(
                    "annexe_lots_hors_perimetre",
                    "warning",
                    "Lots présents dans l'annexe mais absents de « Lots concernés » : "
                    + ", ".join(unknown_lots[:10])
                    + ("…" if len(unknown_lots) > 10 else "")
                    + ".",
                )
            )

    declared_videos = data.get("annex_declared_count")
    if declared_videos is not None and annex_videos and len(annex_videos) != declared_videos:
        issues.append(
            _issue(
                "annexe_volume_incoherent",
                "warning",
                f"Annexe : {len(annex_videos)} vidéos extraites "
                f"pour {declared_videos} annoncées.",
            )
        )

    article_quantity = 0
    for article in articles:
        quantity = _to_decimal(article.get("quantite"))
        if quantity is not None:
            article_quantity += int(quantity)
    if annex_videos and article_quantity and len(annex_videos) != article_quantity:
        issues.append(
            _issue(
                "annexe_quantite_incoherente",
                "warning",
                f"Annexe : {len(annex_videos)} vidéos extraites pour "
                f"{article_quantity} quantité(s) facturée(s) sur la page 1.",
            )
        )

    if annex_videos:
        annex_amount = sum(
            (_to_decimal(item.get("montant")) or Decimal("0")) for item in annex_videos
        )
        if reference_total is not None and abs(annex_amount - reference_total) > MONEY_TOLERANCE:
            issues.append(
                _issue(
                    "annexe_montant_incoherent",
                    "warning",
                    f"Somme de l'annexe ({annex_amount}) ≠ total du BDC ({reference_total}).",
                )
            )

        category_counts: dict[str, int] = {}
        for item in annex_videos:
            category = str(item.get("category") or "").strip().lower()
            if not category:
                category = _category_key_from_annex_label(item.get("category_label"))
            if category:
                category_counts[category] = category_counts.get(category, 0) + 1
        article_counts: dict[str, int] = {}
        for article in articles:
            category = _category_key_from_annex_label(article.get("description"))
            quantity = _to_decimal(article.get("quantite"))
            if category and quantity is not None:
                article_counts[category] = article_counts.get(category, 0) + int(quantity)
        for category in sorted(set(category_counts) | set(article_counts)):
            if category_counts.get(category, 0) != article_counts.get(category, 0):
                issues.append(
                    _issue(
                        "annexe_categorie_incoherente",
                        "warning",
                        f"Catégorie « {category} » : {category_counts.get(category, 0)} vidéo(s) "
                        f"en annexe pour {article_counts.get(category, 0)} facturée(s).",
                    )
                )

    summary = data.get("annex_summary") or {}
    summary_total = _to_decimal(summary.get("total_ht"))
    if summary_total is not None and reference_total is not None:
        if abs(summary_total - reference_total) > MONEY_TOLERANCE:
            issues.append(
                _issue(
                    "annexe_total_incoherent",
                    "warning",
                    f"Total de l'annexe ({summary_total}) ≠ total du BDC ({reference_total}).",
                )
            )

    return issues


def _category_key_from_annex_label(value) -> str:
    normalized = _normalize_header_text(str(value or ""))
    if not normalized:
        return ""
    for word, key in ANNEX_CATEGORY_WORDS.items():
        if re.search(rf"\b{re.escape(word)}\b", normalized):
            return key
    return ""


def extract_bdc_data(pdf_path):
    data = {
        "numero_bdc": None,
        "date_commande": None,
        "date_livraison": None,
        "reference": None,
        "references": [],
        "articles": [],
        "totals": {"subtotal": None, "total": None},
        "bdc_format": "simplified",
        "period": None,
        "concerned_lots": [],
        "concerned_lots_declared_count": None,
        "annex_videos": [],
        "annex_declared_count": None,
        "annex_summary": None,
    }

    with pdfplumber.open(pdf_path) as pdf:
        # On extrait le texte de la première page pour les métadonnées
        first_page = pdf.pages[0]
        text = first_page.extract_text() or ""

        # 1. Extraction des métadonnées : regex d'abord, repli par coordonnées
        bdc_match = re.search(
            r"Bon de commande\s+(BDC-(?:[A-Z0-9]{1,12}-)+\d{1,8})", text, re.IGNORECASE
        )
        if bdc_match:
            data["numero_bdc"] = bdc_match.group(1).upper()
        else:
            bdc_match = BDC_NUMBER_SEARCH_PATTERN.search(text)
            if bdc_match:
                data["numero_bdc"] = bdc_match.group(0).upper()

        date_match = re.search(
            r"(?:Date(?:\s+de\s+commande)?|Commande)\s*:\s*(\d{2}/\d{2}/\d{4})",
            text,
            re.IGNORECASE,
        )
        if date_match:
            data["date_commande"] = date_match.group(1)

        livraison_match = re.search(
            r"(?:Date\s+de\s+livraison|Livraison)\s*:\s*(\d{2}/\d{2}/\d{4})",
            text,
            re.IGNORECASE,
        )
        if livraison_match:
            data["date_livraison"] = livraison_match.group(1)

        data["period"] = _extract_period(text)
        concerned_lots, declared_count = _extract_concerned_lots(text)
        data["concerned_lots"] = concerned_lots
        data["concerned_lots_declared_count"] = declared_count
        data["annex_declared_count"] = _extract_annex_declared_count(text)

        first_page_words = first_page.extract_words()
        layout = _extract_layout(first_page_words, first_page.width)
        fallback = _extract_metadata_from_words(first_page_words)
        for key, value in fallback.items():
            if data[key] is None and value:
                data[key] = value

        reference_value, reference_parts = _extract_reference(text)
        data["reference"] = reference_value
        data["references"] = reference_parts

        # 2. Annexe vidéo éventuelle (pages 2+), avant les articles de la page 1
        annex_payload = _extract_annex_items(pdf.pages[1:])
        data["annex_videos"] = annex_payload["items"]
        if annex_payload["summary"]["pages"]:
            data["annex_summary"] = annex_payload["summary"]
        if data["annex_videos"] or data["concerned_lots"]:
            data["bdc_format"] = "detailed"

        # 3. Articles et totaux : lecture par coordonnées, repli sur les tables.
        # Les pages d'annexe sont exclues pour ne jamais polluer ``articles``.
        annex_pages = set(annex_payload["summary"]["pages"])
        for page in pdf.pages:
            if page.page_number in annex_pages:
                continue
            words = first_page_words if page.page_number == 1 else page.extract_words()
            page_articles = _extract_articles_from_words(words, page.width)
            tables = []
            if page_articles:
                data["articles"].extend(page_articles)
            else:
                tables = page.extract_tables(TABLE_SETTINGS) or []
                for table in tables:
                    data["articles"].extend(_parse_articles_from_table(table))

            page_totals = _extract_totals_from_words(words)
            if page_totals["subtotal"] is None or page_totals["total"] is None:
                if not tables:
                    tables = page.extract_tables(TABLE_SETTINGS) or []
                for table in tables:
                    table_totals = _extract_totals_from_table(table)
                    for key, value in table_totals.items():
                        if page_totals[key] is None and value:
                            page_totals[key] = value

            for key, value in page_totals.items():
                if data["totals"][key] is None and value:
                    data["totals"][key] = value

    data["layout"] = layout
    data["warnings"] = validate_bdc_data(data)
    if layout["changed"]:
        data["warnings"].append(_issue("structure_modifiee", "info", _layout_message(layout)))
    data["status"] = _status_from_issues(data["warnings"])
    return data


def list_pdf_files(input_folder="./pdfs"):
    if not os.path.exists(input_folder):
        raise FileNotFoundError(f"Le dossier {input_folder} n'existe pas.")

    pdf_files = [
        filename for filename in os.listdir(input_folder) if filename.lower().endswith(".pdf")
    ]
    if not pdf_files:
        raise FileNotFoundError(
            f"Aucun PDF trouvé dans {input_folder}. Ajoutez des fichiers avant de lancer la conversion."
        )

    max_files = _int_env("BDC_MAX_PDF_FILES", DEFAULT_MAX_PDF_FILES)
    max_bytes = _int_env("BDC_MAX_PDF_BYTES", DEFAULT_MAX_PDF_BYTES)
    pdf_files = sorted(pdf_files)

    if len(pdf_files) > max_files:
        raise ValueError(
            f"Trop de PDF ({len(pdf_files)}). Maximum autorisé: {max_files} (BDC_MAX_PDF_FILES)."
        )

    oversized = []
    for filename in pdf_files:
        path = os.path.join(input_folder, filename)
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        if size > max_bytes:
            oversized.append(filename)
    if oversized:
        raise ValueError(
            f"PDF trop volumineux (max {max_bytes} octets): {', '.join(oversized[:5])}"
        )

    return pdf_files


def convert_folder(
    input_folder="./pdfs",
    output_file="resultats_bdc.json",
    progress_callback=None,
    pdf_files=None,
    persist_json=True,
):
    """
    Parcourt input_folder, extrait les données de chaque PDF et renvoie la liste des commandes.

    Optionnellement, écrit un JSON sur disque pour compatibilité CLI.
    """
    results = []
    failed_files = []
    pdf_files = pdf_files or list_pdf_files(input_folder)

    total_files = len(pdf_files)
    for index, filename in enumerate(pdf_files, start=1):
        print(f"Traitement de {filename}...")
        path = os.path.join(input_folder, filename)
        try:
            extracted = extract_bdc_data(path)
            extracted["source_filename"] = filename
            if extracted.get("status") == "failed":
                logger.warning(
                    "BDC %s en échec d'extraction: %s",
                    filename,
                    "; ".join(issue["message"] for issue in extracted.get("warnings", [])),
                )
            results.append(extracted)
        except Exception:
            logger.exception("Erreur de lecture du PDF %s", filename)
            failed_files.append({"filename": filename, "error": "Lecture du PDF impossible."})

        if progress_callback:
            try:
                progress_callback(filename=filename, index=index, total=total_files)
            except Exception:
                # On évite de faire échouer la conversion si la callback de monitoring plante.
                pass

    if persist_json:
        atomic_write_text(
            Path(output_file),
            json.dumps(results, ensure_ascii=False, indent=4),
        )

    failed_documents = [
        str(result.get("source_filename") or "")
        for result in results
        if result.get("status") == "failed"
    ]

    return {
        "output_file": os.path.abspath(output_file),
        "processed_files": len(pdf_files),
        "successful_files": len(results) - len(failed_documents),
        "failed_files": failed_files,
        "failed_documents": failed_documents,
        "results": results,
    }


def main():
    # Dossier contenant vos PDF
    input_folder = "./pdfs"
    output_file = "resultats_bdc.json"

    try:
        summary = convert_folder(input_folder=input_folder, output_file=output_file)
    except FileNotFoundError as exc:
        print(f"Erreur : {exc}")
        return

    print(f"Terminé ! Les données sont dans {summary['output_file']}")


if __name__ == "__main__":
    main()
