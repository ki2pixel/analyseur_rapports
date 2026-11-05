"""Billing data model helpers (version snapshots, lines, totals)."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Dict, Iterable, Optional

from libs.billing.money import (
    ZERO,
    cents_to_decimal,
    decimal_str,
    parse_decimal,
    quantize_money,
)

CATEGORY_KEYS = ("short", "standard", "medium", "long")

CATEGORY_LABELS = {
    "short": "Courte (< 2 min)",
    "standard": "Standard (2 – 5 min)",
    "medium": "Moyenne (5 – 7 min)",
    "long": "Longue (≥ 7 min)",
}

CATEGORY_SHORT_LABELS = {
    "short": "courtes",
    "standard": "standards",
    "medium": "moyennes",
    "long": "longues",
}

CATEGORY_SINGULAR_LABELS = {
    "short": "courte",
    "standard": "standard",
    "medium": "moyenne",
    "long": "longue",
}

STATUS_LABELS = {
    "brouillon": "Brouillon",
    "envoyé_pour_vérification": "Envoyée pour vérification",
    "validé_client": "Validée client",
    "émis": "Émise",
    "remplacé": "Remplacée",
    "annulé": "Annulée",
}

STATUS_ORDER = (
    "brouillon",
    "envoyé_pour_vérification",
    "validé_client",
    "émis",
    "remplacé",
    "annulé",
)

ALLOWED_TRANSITIONS = {
    "brouillon": ("envoyé_pour_vérification", "validé_client"),
    "envoyé_pour_vérification": ("validé_client", "annulé"),
    "validé_client": ("émis", "annulé"),
    "émis": ("remplacé",),
    "remplacé": (),
    "annulé": (),
}

STATUS_BADGES = {
    "brouillon": "badge--neutral",
    "envoyé_pour_vérification": "badge--warn",
    "validé_client": "badge--ok",
    "émis": "badge--ok",
    "remplacé": "badge--neutral",
    "annulé": "badge--danger",
}

BILLING_BASES = {
    "actual_capped": "Au réalisé, plafonné au commandé",
    "ordered": "Au commandé",
}

DEFAULT_SETTINGS = {
    "billing_basis": "actual_capped",
    "vat_rate": "0.00",
    "rate_overrides": {},
}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def line_total_cents(line: Dict[str, Any]) -> int:
    quantity = parse_decimal(line.get("quantity"), default=Decimal("1")) or Decimal("1")
    unit_cents = int(line.get("unit_price_cents") or 0)
    total = quantize_money(quantity * cents_to_decimal(unit_cents))
    return int(total * 100)


def compute_totals(
    lines: Iterable[Dict[str, Any]], *, default_vat_rate: str = "0.00"
) -> Dict[str, int]:
    ht_cents = 0
    vat_cents = 0
    for line in lines:
        if not line.get("included", True):
            continue
        total = line_total_cents(line)
        line["total_cents"] = total
        ht_cents += total
        rate = parse_decimal(line.get("vat_rate") or default_vat_rate, default=ZERO) or ZERO
        vat_cents += int(
            (Decimal(total) * rate / 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        )
    return {
        "ht_cents": ht_cents,
        "vat_cents": vat_cents,
        "ttc_cents": ht_cents + vat_cents,
    }


def make_line(
    *,
    line_id: str,
    kind: str,
    label: str,
    unit_price_cents: int,
    quantity: str = "1",
    bdc_number: Optional[str] = None,
    delivery_date: Optional[str] = None,
    vat_rate: str = "0.00",
    detail: Optional[Dict[str, Any]] = None,
    source: str = "auto",
    note: str = "",
    included: bool = True,
) -> Dict[str, Any]:
    line = {
        "line_id": line_id,
        "kind": kind,
        "label": label,
        "bdc_number": bdc_number,
        "delivery_date": delivery_date,
        "quantity": decimal_str(quantity, places=2),
        "unit_price_cents": int(unit_price_cents),
        "vat_rate": decimal_str(vat_rate, places=2),
        "detail": detail or {},
        "source": source,
        "included": bool(included),
        "note": note or "",
        "edited": False,
    }
    line["total_cents"] = line_total_cents(line)
    return line


def new_version(
    *,
    month: str,
    version: int,
    status: str = "brouillon",
    reason: str = "",
    settings: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    settings = {**DEFAULT_SETTINGS, **(settings or {})}
    return {
        "month": month,
        "version": int(version),
        "created_at": now_iso(),
        "frozen_at": None,
        "status": status,
        "reason": reason or "",
        "settings": settings,
        "sources": {},
        "rate_grid": {},
        "rate_sources": {},
        "lines": [],
        "issues": [],
        "acknowledged_warnings": [],
        "certification": {},
        "reconciliation_summary": {},
        "totals": {"ht_cents": 0, "vat_cents": 0, "ttc_cents": 0},
        "invoice": {
            "number": "",
            "issued_on": "",
            "total_cents": 0,
            "note": "",
        },
        "notes": "",
    }


def normalize_version(version: Dict[str, Any]) -> Dict[str, Any]:
    """Ensure a loaded/edited version has coherent computed fields."""
    settings = {**DEFAULT_SETTINGS, **(version.get("settings") or {})}
    version["settings"] = settings
    version.setdefault("lines", [])
    version.setdefault("issues", [])
    version.setdefault("acknowledged_warnings", [])
    version.setdefault("certification", {})
    version.setdefault("reconciliation_summary", {})
    version.setdefault("rate_grid", {})
    version.setdefault("rate_sources", {})
    version.setdefault("sources", {})
    version.setdefault("frozen_at", None)
    version.setdefault("snapshot_generated_at", "")
    version.setdefault("snapshot_used", False)
    version.setdefault("already_billed", [])
    version.setdefault("unbilled_production", {"entries": [], "counts": {}, "missing_lots": []})
    version.setdefault("override_reason", "")
    version.setdefault("reason", "")
    invoice = version.setdefault("invoice", {})
    invoice.setdefault("number", "")
    invoice.setdefault("issued_on", "")
    invoice.setdefault("total_cents", 0)
    invoice.setdefault("note", "")

    for line in version["lines"]:
        line.setdefault("included", True)
        line.setdefault("quantity", "1.00")
        line.setdefault("vat_rate", settings.get("vat_rate", "0.00"))
        line.setdefault("detail", {})
        line.setdefault("source", "auto")
        line.setdefault("note", "")
        line.setdefault("edited", False)
        line["total_cents"] = line_total_cents(line)

    version["totals"] = compute_totals(
        version["lines"], default_vat_rate=str(settings.get("vat_rate", "0.00"))
    )
    return version


def version_index_entry(version: Dict[str, Any]) -> Dict[str, Any]:
    totals = version.get("totals") or {}
    invoice = version.get("invoice") or {}
    return {
        "version": int(version.get("version") or 1),
        "status": str(version.get("status") or "brouillon"),
        "created_at": version.get("created_at"),
        "frozen_at": version.get("frozen_at"),
        "reason": version.get("reason") or "",
        "total_ht_cents": int(totals.get("ht_cents") or 0),
        "total_ttc_cents": int(totals.get("ttc_cents") or 0),
        "invoice_number": invoice.get("number") or "",
        "invoice_issued_on": invoice.get("issued_on") or "",
    }


def new_project(month: str) -> Dict[str, Any]:
    return {
        "month": month,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "current_version": 0,
        "versions": [],
        "settings": dict(DEFAULT_SETTINGS),
    }


def project_index(project: Dict[str, Any]) -> Dict[str, Any]:
    project["versions"] = sorted(
        project.get("versions") or [], key=lambda item: int(item.get("version") or 0)
    )
    project["current_version"] = max(
        (int(item.get("version") or 0) for item in project["versions"]), default=0
    )
    project["updated_at"] = now_iso()
    return project


def find_line(version: Dict[str, Any], line_id: str) -> Optional[Dict[str, Any]]:
    for line in version.get("lines") or []:
        if str(line.get("line_id")) == line_id:
            return line
    return None


def category_counts_from_detail(detail: Dict[str, Any]) -> Dict[str, int]:
    counts = {key: 0 for key in CATEGORY_KEYS}
    for key in CATEGORY_KEYS:
        item = (detail or {}).get(key) or {}
        try:
            counts[key] = int(item.get("billable") or 0)
        except (TypeError, ValueError):
            counts[key] = 0
    return counts


def format_category_counts(counts: Dict[str, int]) -> str:
    parts = []
    for key in CATEGORY_KEYS:
        value = int(counts.get(key) or 0)
        if value:
            label = (
                CATEGORY_SINGULAR_LABELS.get(key, key)
                if value == 1
                else CATEGORY_SHORT_LABELS.get(key, key)
            )
            parts.append(f"{value} {label}")
    return " / ".join(parts)
