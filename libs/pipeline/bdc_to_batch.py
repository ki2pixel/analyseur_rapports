"""
Module de transformation pour mapper les données extraites des PDF BDC
vers le schéma pivot MonthlyBatch.

Ce module fournit des fonctions pour :
- Inférer la catégorie de durée d'un article depuis sa description
- Transformer un JSON BDC (liste d'objets PurchaseOrder) en MonthlyBatch
- Enrichir avec métadonnées de source et horodatage
"""

import json
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Dict, List, Optional, Union

from libs.schemas import (
    AnnexeVideoItem,
    MonthlyBatch,
    OrderArticle,
    PurchaseOrder,
    SourceFile,
    VideoCategory,
    VideoEntry,
)

_CATEGORY_DURATION_MAP = {
    VideoCategory.SHORT: 90,
    VideoCategory.STANDARD: 240,
    VideoCategory.MEDIUM: 360,
    VideoCategory.LONG: 540,
}


def infer_category_from_description(description: str) -> VideoCategory:
    """
    Infère la catégorie de durée depuis la description d'un article BDC.

    L'ordre d'évaluation est volontairement strict (LONG -> MEDIUM -> STANDARD
    -> SHORT) afin qu'une description contenant plusieurs indices ne soit jamais
    rabattue sur une catégorie inférieure.
    """
    desc_lower = (description or "").lower().strip()

    if any(
        keyword in desc_lower
        for keyword in (
            "long",
            "longue",
            "plus de 7",
            "plus de 8",
            "8 min et plus",
            "> 7",
            "> 8",
            ">7",
            ">8",
        )
    ):
        return VideoCategory.LONG

    if any(
        keyword in desc_lower
        for keyword in (
            "moyen",
            "moyenne",
            "medium",
            "de 5 à 8",
            "5 à 8",
            "entre 5 et 8",
            "5-8",
            "entre 5 et 7",
            "5 à 7",
            "5-7",
        )
    ):
        return VideoCategory.MEDIUM

    if any(
        keyword in desc_lower
        for keyword in (
            "standard",
            "de 2 à 5",
            "entre 2 et 5",
            "entre 3 et 5",
            "2-5",
            "3-5",
        )
    ):
        return VideoCategory.STANDARD

    if any(
        keyword in desc_lower for keyword in ("court", "courte", "short", "moins de 2", "< 2", "<2")
    ):
        return VideoCategory.SHORT

    return VideoCategory.UNKNOWN


def _parse_annex_video_item(raw: Dict) -> Optional[AnnexeVideoItem]:
    """Convertit une entrée d'annexe JSON en ``AnnexeVideoItem`` validé."""
    if not isinstance(raw, dict):
        return None
    video_name = str(raw.get("video_name") or "").strip()
    lot = str(raw.get("lot") or "").strip()
    if not video_name or not lot:
        return None

    duration_sec: Optional[int] = None
    raw_duration = raw.get("duration_sec")
    if raw_duration is not None:
        try:
            parsed_duration = int(round(float(raw_duration)))
        except (TypeError, ValueError):
            parsed_duration = None
        if parsed_duration is not None and parsed_duration >= 0:
            duration_sec = parsed_duration

    category_label = raw.get("category_label")
    inferred = VideoCategory.UNKNOWN
    if category_label:
        inferred = infer_category_from_description(str(category_label))

    return AnnexeVideoItem(
        lot=lot,
        video_name=video_name,
        duration_text=raw.get("duration_text"),
        duration_sec=duration_sec,
        category_label=str(category_label) if category_label else None,
        inferred_category=inferred,
        montant=raw.get("montant"),
    )


def parse_bdc_json_to_orders(bdc_data: List[Dict]) -> List[PurchaseOrder]:
    """
    Transforme une liste d'objets BDC (format JSON actuel) en liste de PurchaseOrder.
    Les articles incomplets sont ignorés individuellement ; idem pour les commandes invalides.

    Args:
        bdc_data: Liste de dicts depuis json.load() du fichier BDC

    Returns:
        Liste de PurchaseOrder avec articles enrichis de catégories inférées
    """
    import sys

    orders = []
    skipped_articles: List[str] = []
    skipped_orders: List[str] = []

    for bdc_item in bdc_data:
        articles = []
        for article_dict in bdc_item.get("articles", []):
            if not isinstance(article_dict, dict):
                skipped_articles.append("article non-objet")
                continue
            description = article_dict.get("description") or ""
            if not str(description).strip():
                skipped_articles.append(
                    f"article sans description (bdc={bdc_item.get('numero_bdc')!r})"
                )
                continue
            try:
                inferred_cat = infer_category_from_description(description)
                article = OrderArticle(
                    description=description,
                    quantite=article_dict.get("quantite"),
                    unite=article_dict.get("unite"),
                    taux=article_dict.get("taux"),
                    montant=article_dict.get("montant"),
                    inferred_category=inferred_cat,
                )
                articles.append(article)
            except (ValueError, TypeError, KeyError) as exc:
                skipped_articles.append(f"article ignoré: {exc}")

        annex_videos = [
            item
            for item in (_parse_annex_video_item(raw) for raw in bdc_item.get("annex_videos") or [])
            if item is not None
        ]
        bdc_format = str(bdc_item.get("bdc_format") or "").strip().lower()
        if bdc_format not in {"simplified", "detailed"}:
            bdc_format = (
                "detailed" if (annex_videos or bdc_item.get("concerned_lots")) else "simplified"
            )

        try:
            order = PurchaseOrder(
                numero_bdc=bdc_item.get("numero_bdc") or "",
                date_commande=bdc_item.get("date_commande"),
                date_livraison=bdc_item.get("date_livraison"),
                reference=bdc_item.get("reference"),
                references=bdc_item.get("references", []),
                articles=articles,
                bdc_format=bdc_format,
                period=bdc_item.get("period"),
                concerned_lots=[str(lot) for lot in (bdc_item.get("concerned_lots") or [])],
                annex_videos=annex_videos,
            )
            orders.append(order)
        except (ValueError, KeyError) as exc:
            skipped_orders.append(str(exc))
            print(
                f"Avertissement: Ignoré la commande invalide due à une erreur de validation : {exc}",
                file=sys.stderr,
            )

    if skipped_articles:
        print(
            f"Avertissement: {len(skipped_articles)} article(s) ignoré(s) lors du parsing BDC.",
            file=sys.stderr,
        )
    if skipped_orders:
        print(
            f"Avertissement: {len(skipped_orders)} commande(s) ignorée(s) lors du parsing BDC.",
            file=sys.stderr,
        )

    return orders


MONTH_NAME_MAP = {
    "janvier": "01",
    "janv": "01",
    "février": "02",
    "fevrier": "02",
    "mars": "03",
    "avril": "04",
    "mai": "05",
    "juin": "06",
    "juillet": "07",
    "août": "08",
    "aout": "08",
    "septembre": "09",
    "octobre": "10",
    "novembre": "11",
    "décembre": "12",
    "decembre": "12",
}


def _valid_month(value: str) -> Optional[str]:
    try:
        month = int(value)
    except (TypeError, ValueError):
        return None
    if 1 <= month <= 12:
        return f"{month:02d}"
    return None


def _infer_batch_id(name_hint: Optional[str]) -> str:
    if not name_hint:
        return datetime.now().strftime("%Y-%m")

    name = Path(name_hint).name
    # convert_bdc_10-2026.json, bdc_10-2026.json, bdc_10_2026.json
    match = re.search(r"(?:convert_)?bdc[_-](\d{1,2})[-_](\d{4})\.json$", name, re.IGNORECASE)
    if match:
        month = _valid_month(match.group(1))
        if month:
            return f"{match.group(2)}-{month}"

    # bdc_2026-10.json
    match = re.search(r"(?:convert_)?bdc[_-](\d{4})[-_](\d{1,2})\.json$", name, re.IGNORECASE)
    if match:
        month = _valid_month(match.group(2))
        if month:
            return f"{match.group(1)}-{month}"

    # bdc_octobre_2025.json
    match = re.search(r"(?:convert_)?bdc[_-]([a-zéûôè]+)[-_](\d{4})\.json$", name, re.IGNORECASE)
    if match:
        month = MONTH_NAME_MAP.get(match.group(1).lower())
        if month:
            return f"{match.group(2)}-{month}"

    # Last resort: any explicit YYYY-MM in the filename.
    match = re.search(r"(\d{4})-(\d{2})", name)
    if match:
        month = _valid_month(match.group(2))
        if month:
            return f"{match.group(1)}-{month}"

    return datetime.now().strftime("%Y-%m")


def parse_quantity(value: Union[str, int, float, None], *, default: int = 0) -> int:
    """Parse une quantité BDC en gérant les formats français sans flottants."""
    if value is None:
        return default
    if isinstance(value, (int, float)):
        try:
            parsed = Decimal(str(value))
        except InvalidOperation:
            return default
        return max(0, int(parsed.quantize(Decimal("1"), rounding=ROUND_HALF_UP)))

    raw = str(value).strip()
    if not raw:
        return default

    cleaned = (
        raw.replace("\u00a0", "")
        .replace("\u202f", "")
        .replace(" ", "")
        .replace("€", "")
        .replace(",", ".")
    )
    try:
        parsed = Decimal(cleaned)
    except (InvalidOperation, ValueError):
        return default
    return max(0, int(parsed.quantize(Decimal("1"), rounding=ROUND_HALF_UP)))


def _parse_quantity(value: Optional[str]) -> int:
    """Compatibilité avec l'ancien nom privé."""
    return parse_quantity(value)


def _annex_entries_for_order(
    order: PurchaseOrder, batch_id: str
) -> tuple[List[VideoEntry], int, int]:
    """Construit les ``VideoEntry`` d'une annexe détaillée.

    Retourne les entrées, le nombre d'items ignorés (lot/nom manquant) et le
    nombre de durées absentes (durée déduite de la catégorie).
    """
    entries: List[VideoEntry] = []
    skipped = 0
    missing_duration = 0
    for index, item in enumerate(order.annex_videos):
        if not item.lot or not item.video_name:
            skipped += 1
            continue
        duration = item.duration_sec
        source = "bdc_annex"
        if duration is None:
            duration = _CATEGORY_DURATION_MAP.get(item.inferred_category)
            source = "bdc_annex_category"
            missing_duration += 1
        if duration is None:
            skipped += 1
            continue
        entries.append(
            VideoEntry(
                video_id=f"{order.numero_bdc}-annex-{index}",
                id_camille=item.lot,
                person="",
                month=batch_id,
                video_name=item.video_name,
                video_path=f"bdc://{order.numero_bdc}/annex/{index}",
                duration_sec=float(duration),
                duration_source=source,
                archive_folder=f"bdc:{order.numero_bdc}",
                source_bdc=order.numero_bdc,
                source_article_index=index,
            )
        )
    return entries, skipped, missing_duration


def _orders_to_video_entries(
    batch_id: str, orders: List[PurchaseOrder]
) -> tuple[List[VideoEntry], List[str]]:
    entries: List[VideoEntry] = []
    warnings: List[str] = []
    zero_quantity_lines = 0
    unknown_articles = 0
    empty_orders = 0
    annex_skipped = 0
    annex_missing_duration = 0
    annex_orders = 0
    for order in orders:
        if order.bdc_format == "detailed" and order.annex_videos:
            annex_orders += 1
            order_entries, skipped, missing_duration = _annex_entries_for_order(order, batch_id)
            annex_skipped += skipped
            annex_missing_duration += missing_duration
            entries.extend(order_entries)
            continue
        if not order.articles:
            empty_orders += 1
            continue
        group_label = (
            order.reference
            or "/".join(str(ref) for ref in order.references if ref)
            or order.numero_bdc
        )
        for article_index, article in enumerate(order.articles):
            if article.inferred_category == VideoCategory.UNKNOWN:
                unknown_articles += 1
                continue
            quantity = _parse_quantity(article.quantite)
            if quantity <= 0:
                zero_quantity_lines += 1
                continue
            duration = _CATEGORY_DURATION_MAP.get(article.inferred_category)
            if duration is None:
                unknown_articles += 1
                continue
            for replica_index in range(quantity):
                video_id = f"{order.numero_bdc}-{article_index}-{replica_index}"
                video_name = f"{article.description or 'Vidéo'} ({order.numero_bdc})"
                entries.append(
                    VideoEntry(
                        video_id=video_id,
                        id_camille=group_label,
                        person="",
                        month=batch_id,
                        video_name=video_name,
                        video_path=f"bdc://{order.numero_bdc}/{article_index}/{replica_index}",
                        duration_sec=duration,
                        duration_source="bdc_inferred",
                        archive_folder=f"batch:{batch_id}",
                        source_bdc=order.numero_bdc,
                        source_article_index=article_index,
                    )
                )
    if unknown_articles:
        warnings.append(
            f"{unknown_articles} article(s) BDC non catégorisé(s) ignoré(s) "
            "(description sans catégorie de durée reconnue)."
        )
    if zero_quantity_lines:
        warnings.append(f"{zero_quantity_lines} ligne(s) BDC à quantité nulle ignorée(s).")
    if empty_orders:
        warnings.append(f"{empty_orders} bon(s) de commande sans article ignoré(s).")
    if annex_skipped:
        warnings.append(
            f"{annex_skipped} vidéo(s) d'annexe ignorée(s) (lot, nom ou durée indisponible)."
        )
    if annex_missing_duration:
        warnings.append(
            f"{annex_missing_duration} vidéo(s) d'annexe sans durée explicite : "
            "durée estimée depuis la catégorie BDC."
        )
    if annex_orders:
        warnings.append(
            f"{annex_orders} bon(s) de commande détaillé(s) : vidéos issues de l'annexe."
        )
    return entries, warnings


def _build_monthly_batch(
    bdc_data: List[Dict],
    batch_id: str,
    source_files: List[SourceFile],
) -> MonthlyBatch:
    usable_data: List[Dict] = []
    failed_documents = 0
    for item in bdc_data:
        if isinstance(item, dict) and str(item.get("status") or "ok") == "failed":
            failed_documents += 1
            continue
        usable_data.append(item)

    orders = parse_bdc_json_to_orders(usable_data)
    video_entries, warnings = _orders_to_video_entries(batch_id, orders)
    if failed_documents:
        warnings.insert(
            0,
            f"{failed_documents} BDC en échec d'extraction ignoré(s).",
        )
    return MonthlyBatch(
        batch_id=batch_id,
        generated_at=datetime.now(),
        source_files=source_files,
        orders=orders,
        video_entries=video_entries,
        warnings=warnings,
    )


def _build_source_file(name: str, path: Optional[str]) -> SourceFile:
    return SourceFile(
        name=name,
        path=path or name,
        file_type="json",
        processed_at=datetime.now(),
    )


def create_monthly_batch_from_data(
    bdc_data: List[Dict],
    *,
    batch_id: Optional[str] = None,
    source_name: str = "in-memory",
    source_path: Optional[str] = None,
) -> MonthlyBatch:
    resolved_batch_id = batch_id or datetime.now().strftime("%Y-%m")
    source_file = _build_source_file(source_name, source_path)
    return _build_monthly_batch(bdc_data, resolved_batch_id, [source_file])


def create_monthly_batch_from_bdc(
    bdc_file_path: str,
    batch_id: Optional[str] = None,
) -> MonthlyBatch:
    """
    Crée un MonthlyBatch depuis un fichier JSON BDC.
    """
    bdc_path = Path(bdc_file_path)
    with open(bdc_path, "r", encoding="utf-8") as f:
        bdc_data = json.load(f)

    resolved_batch_id = batch_id or _infer_batch_id(bdc_path.name)
    source_file = _build_source_file(bdc_path.name, str(bdc_path.absolute()))
    return _build_monthly_batch(bdc_data, resolved_batch_id, [source_file])


if __name__ == "__main__":
    # Exemple d'usage CLI pour test
    import sys

    if len(sys.argv) != 2:
        print("Usage: python bdc_to_batch.py <bdc_json_file>")
        sys.exit(1)

    bdc_file = sys.argv[1]
    batch = create_monthly_batch_from_bdc(bdc_file)

    print(f"Batch créé: {batch.batch_id}")
    print(f"Commandes: {len(batch.orders)}")
    for order in batch.orders[:3]:  # Aperçu des 3 premières
        print(f"  - {order.numero_bdc}: {len(order.articles)} articles")
        for art in order.articles[:2]:  # 2 premiers articles
            print(f"    * {art.description} -> {art.inferred_category.value}")
