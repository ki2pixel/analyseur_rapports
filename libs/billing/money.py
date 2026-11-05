"""Decimal money helpers for billing (French formats, no floats)."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Optional

CENTS = Decimal("0.01")
ZERO = Decimal("0.00")


def parse_decimal(value: Any, *, default: Optional[Decimal] = None) -> Optional[Decimal]:
    """Parse a French or plain decimal string ("1 056,00", "8.00", "0,00 %")."""
    if value is None:
        return default
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        try:
            return Decimal(str(value))
        except InvalidOperation:
            return default

    text = str(value).strip()
    if not text:
        return default
    cleaned = (
        text.replace("\u00a0", "")
        .replace("\u202f", "")
        .replace(" ", "")
        .replace("€", "")
        .replace("$", "")
        .replace("%", "")
        .replace(",", ".")
    )
    try:
        return Decimal(cleaned)
    except (InvalidOperation, ValueError):
        return default


def quantize_money(value: Decimal) -> Decimal:
    return value.quantize(CENTS, rounding=ROUND_HALF_UP)


def to_cents(value: Any, *, default: int = 0) -> int:
    parsed = parse_decimal(value)
    if parsed is None:
        return default
    return int((parsed * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def cents_to_decimal(cents: Any) -> Decimal:
    try:
        return quantize_money(Decimal(int(cents)) / 100)
    except (TypeError, ValueError):
        return ZERO


def decimal_str(value: Any, *, places: int = 2) -> str:
    parsed = parse_decimal(value, default=ZERO) or ZERO
    quantum = Decimal("1").scaleb(-places)
    return str(parsed.quantize(quantum, rounding=ROUND_HALF_UP))


def format_eur(cents: Any) -> str:
    value = cents_to_decimal(cents)
    formatted = f"{value:,.2f}".replace(",", " ").replace(".", ",")
    return f"{formatted} €"


def format_amount_plain(cents: Any) -> str:
    """Amount without currency, comma decimal (display friendly)."""
    value = cents_to_decimal(cents)
    return f"{value:,.2f}".replace(",", " ").replace(".", ",")


def format_amount_csv(cents: Any) -> str:
    """Amount without currency or thousands separator (CSV/Indy friendly)."""
    value = cents_to_decimal(cents)
    return f"{value:.2f}".replace(".", ",")


def format_rate(value: Any) -> str:
    parsed = parse_decimal(value, default=ZERO)
    formatted = f"{parsed:.2f}".replace(".", ",")
    return f"{formatted} %"
