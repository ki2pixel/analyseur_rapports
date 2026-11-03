from __future__ import annotations

"""
Core dataclasses describing the pivot data model shared between PDF conversion
and monthly video analysis.

These models allow both apps to exchange strongly typed structures before any
rendering layer (Flask templates, API responses, CLI export) comes into play.
Validation stays intentionally light so that upstream ingestion can clamp edge
cases without blocking the overall pipeline.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional


class VideoCategory(str, Enum):
    """Unified names for duration-based buckets used across the reports."""

    SHORT = "short"
    STANDARD = "standard"
    MEDIUM = "medium"
    LONG = "long"
    UNKNOWN = "unknown"


@dataclass
class SourceFile:
    """Metadata about an input file (PDF, JSON, video) processed in a batch."""

    name: str
    path: str
    file_type: str  # e.g. "pdf", "json", "video"
    processed_at: datetime
    status: str = "processed"

    def __post_init__(self):
        if not self.name:
            raise ValueError("SourceFile.name ne peut pas être vide")
        if not self.path:
            raise ValueError("SourceFile.path ne peut pas être vide")
        if not self.file_type:
            raise ValueError("SourceFile.file_type ne peut pas être vide")


@dataclass
class OrderArticle:
    """Article extrait d'un BDC, potentiellement lié à une ou plusieurs vidéos."""

    description: str
    quantite: Optional[str] = None
    unite: Optional[str] = None
    taux: Optional[str] = None
    montant: Optional[str] = None
    inferred_category: VideoCategory = VideoCategory.UNKNOWN
    linked_video_ids: List[str] = field(default_factory=list)


@dataclass
class PurchaseOrder:
    """Représentation pivot d'un bon de commande extrait depuis un PDF."""

    numero_bdc: str
    date_commande: Optional[str]
    date_livraison: Optional[str]
    reference: Optional[str]
    references: List[str]
    articles: List[OrderArticle] = field(default_factory=list)

    def __post_init__(self):
        if not self.numero_bdc:
            raise ValueError("PurchaseOrder.numero_bdc est obligatoire")


@dataclass
class VideoEntry:
    """
    Vidéo disponible côté archives avec les métadonnées nécessaires pour
    catégoriser et rattacher les résultats d'analyse aux commandes associées.
    """

    video_id: str
    id_camille: str
    person: str
    month: str  # format YYYY-MM
    video_name: str
    video_path: str
    duration_sec: Optional[float]
    duration_source: str  # e.g. "ffprobe", "scenes_csv", "unknown"
    archive_folder: str
    source_bdc: Optional[str] = None
    source_article_index: Optional[int] = None

    def __post_init__(self):
        if not self.video_id:
            raise ValueError("VideoEntry.video_id est obligatoire")
        if not self.video_path:
            raise ValueError("VideoEntry.video_path est obligatoire")
        if not self.month or len(self.month) != 7 or self.month[4] != "-":
            raise ValueError("VideoEntry.month doit respecter le format YYYY-MM")


@dataclass
class VideoGroupCategory:
    """Prépare les blocs utilisés par report.html pour afficher les catégories."""

    label: str
    count: int = 0
    files: List[str] = field(default_factory=list)
    file_details: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class VideoGroupReport:
    """
    Résumé agrégé par id_camille/person, similaire à GroupReport existant mais
    indépendant de la couche de persistance actuelle.
    """

    id_camille: str
    person: str
    month: str
    categories: Dict[VideoCategory, VideoGroupCategory]
    totals: Dict[str, int] = field(default_factory=dict)


@dataclass
class MonthlyBatch:
    """
    Conteneur principal échangé entre l'outil d'extraction PDF et l'analyseur.
    """

    batch_id: str  # typiquement YYYY-MM
    generated_at: datetime
    source_files: List[SourceFile] = field(default_factory=list)
    orders: List[PurchaseOrder] = field(default_factory=list)
    video_entries: List[VideoEntry] = field(default_factory=list)
    reports: List[VideoGroupReport] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def __post_init__(self):
        if not self.batch_id:
            raise ValueError("MonthlyBatch.batch_id est obligatoire")
