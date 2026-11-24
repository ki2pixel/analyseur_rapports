"""Pure billing computation: invoice draft, rate grid, version diff, guards."""

from __future__ import annotations

import copy
import uuid
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from libs.billing.model import (
    ALLOWED_TRANSITIONS,
    CATEGORY_KEYS,
    CATEGORY_SHORT_LABELS,
    DEFAULT_SETTINGS,
    STATUS_LABELS,
    find_line,
    format_category_counts,
    make_line,
    new_version,
    normalize_version,
)
from libs.billing.money import (
    ZERO,
    decimal_str,
    parse_decimal,
    quantize_money,
    to_cents,
)
from libs.pipeline.bdc_to_batch import infer_category_from_description, parse_quantity
from libs.reconciliation import compare_reports


def _issue(code: str, severity: str, message: str, **context: Any) -> Dict[str, Any]:
    return {
        "code": code,
        "severity": severity,
        "message": message,
        "context": context,
    }


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _unique(items: Iterable[str]) -> List[str]:
    seen = set()
    result = []
    for item in items:
        value = str(item or "").strip()
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _video_match_summary(cluster: Dict[str, Any]) -> Dict[str, Any]:
    """Synthèse compacte du rapprochement vidéo d'un cluster (BDC détaillé)."""
    reconciliation = cluster.get("video_reconciliation") or {}
    if not reconciliation:
        return {}
    return {
        "annex_total": _int(reconciliation.get("annex_total")),
        "matched": _int(reconciliation.get("matched")),
        "match_rate": reconciliation.get("match_rate", 0.0),
        "counts": reconciliation.get("counts") or {},
    }


def _order_facts(order: Dict[str, Any]) -> Dict[str, Any]:
    """Per-BDC quantities, amounts and rates by inferred category."""
    number = str(order.get("numero_bdc") or "").strip()
    facts: Dict[str, Any] = {
        "number": number,
        "delivery_date": order.get("date_livraison") or "",
        "status": str(order.get("status") or "ok"),
        "warnings": [str(item) for item in (order.get("warnings") or [])],
        "layout_changed": bool((order.get("layout") or {}).get("changed")),
        "total_cents": to_cents((order.get("totals") or {}).get("total")),
        "category_quantities": {cat: 0 for cat in CATEGORY_KEYS},
        "category_amounts": {cat: ZERO for cat in CATEGORY_KEYS},
        "category_rates": {cat: [] for cat in CATEGORY_KEYS},
        "unknown_quantity": 0,
        "unknown_amount": ZERO,
        "bdc_format": str(order.get("bdc_format") or "simplified"),
        "annex_videos": len(order.get("annex_videos") or []),
        "concerned_lots": len(order.get("concerned_lots") or []),
    }

    computed_total = ZERO
    for article in order.get("articles") or []:
        description = str(article.get("description") or "")
        category = infer_category_from_description(description).value
        quantity = max(0, parse_quantity(article.get("quantite")))
        rate = parse_decimal(article.get("taux"))
        amount = parse_decimal(article.get("montant"))
        if amount is None and rate is not None:
            amount = rate * quantity
        amount = amount or ZERO
        computed_total += amount

        if category not in CATEGORY_KEYS or rate is None:
            facts["unknown_quantity"] += quantity
            facts["unknown_amount"] += amount
            continue

        facts["category_quantities"][category] += quantity
        facts["category_amounts"][category] += amount
        facts["category_rates"][category].append((quantity, rate))

    if not facts["total_cents"]:
        facts["total_cents"] = to_cents(computed_total)
    return facts


def _weighted_rate(entries: Sequence[Tuple[int, Decimal]]) -> Optional[Decimal]:
    total_quantity = sum(max(quantity, 0) for quantity, _ in entries)
    if total_quantity <= 0:
        return None
    total = sum((Decimal(quantity) * rate) for quantity, rate in entries)
    return quantize_money(total / Decimal(total_quantity))


def build_rate_grid(bdc_payload: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Build a month rate grid from BDC articles (category -> Decimal rate)."""
    votes: Dict[str, Dict[Decimal, int]] = {cat: {} for cat in CATEGORY_KEYS}
    sources: Dict[str, set] = {cat: set() for cat in CATEGORY_KEYS}
    warnings: List[Dict[str, Any]] = []

    for order in bdc_payload or []:
        number = str(order.get("numero_bdc") or "").strip()
        for article in order.get("articles") or []:
            category = infer_category_from_description(article.get("description") or "").value
            rate = parse_decimal(article.get("taux"))
            if category not in CATEGORY_KEYS or rate is None:
                continue
            quantity = max(1, parse_quantity(article.get("quantite")))
            votes[category][rate] = votes[category].get(rate, 0) + quantity
            if number:
                sources[category].add(number)

    rates: Dict[str, Decimal] = {}
    rate_sources: Dict[str, List[str]] = {}
    for category, category_votes in votes.items():
        if not category_votes:
            continue
        chosen = max(category_votes.items(), key=lambda item: (item[1], item[0]))
        rates[category] = quantize_money(chosen[0])
        rate_sources[category] = sorted(sources[category])
        if len(category_votes) > 1:
            detail = ", ".join(
                f"{decimal_str(rate)} € ({count})"
                for rate, count in sorted(category_votes.items(), key=lambda item: item[0])
            )
            warnings.append(
                _issue(
                    "rate_conflict",
                    "warning",
                    f"Plusieurs taux détectés pour « {CATEGORY_SHORT_LABELS.get(category, category)} » "
                    f"({detail}). Taux retenu : {decimal_str(chosen[0])} €.",
                    category=category,
                )
            )

    return {
        "rates": rates,
        "sources": rate_sources,
        "warnings": warnings,
    }


def _resolve_analysis(
    reporting_payload: Optional[Dict[str, Any]],
    bdc_payload: Sequence[Dict[str, Any]],
    snapshot: Optional[Dict[str, Any]],
) -> Tuple[Dict[str, Any], bool]:
    if snapshot and isinstance(snapshot.get("analysis"), dict):
        return snapshot["analysis"], True
    if reporting_payload is None:
        raise ValueError(
            "Aucun snapshot de réconciliation ni rapport de production disponible pour ce mois."
        )
    return compare_reports(reporting_payload, list(bdc_payload)), False


def _order_issues(order: Dict[str, Any], issues: List[Dict[str, Any]]) -> None:
    number = order.get("number") or "BDC"
    status = order.get("status") or "ok"
    if status == "failed":
        issues.append(
            _issue(
                "bdc_failed",
                "error",
                f"{number} est en échec d’extraction : la ligne ne peut pas être facturée en l’état.",
                bdc=number,
            )
        )
    elif status == "partial":
        issues.append(
            _issue(
                "bdc_partial",
                "warning",
                f"{number} est partiellement extrait : vérifier les montants avant émission.",
                bdc=number,
            )
        )
    if order.get("layout_changed"):
        issues.append(
            _issue(
                "bdc_layout_changed",
                "warning",
                f"{number} présente une structure de tableau modifiée.",
                bdc=number,
            )
        )
    if order.get("unknown_quantity"):
        issues.append(
            _issue(
                "unknown_articles",
                "warning",
                f"{number} contient des articles non reconnus : leurs montants sont repris du BDC.",
                bdc=number,
            )
        )
    for warning in order.get("warnings") or []:
        issues.append(
            _issue(
                "bdc_warning",
                "warning",
                f"{number} : {warning}",
                bdc=number,
            )
        )


def _line_label(number: str, delivery_date: Any) -> str:
    date = str(delivery_date or "").strip()
    if date:
        return f"{number} | Date de livraison: {date}"
    return number


def _provision_note(entries: Sequence[Dict[str, Any]]) -> str:
    missing_lots = sorted({str(item["lot"]) for item in entries if item.get("kind") == "missing"})
    surplus_bdcs = sorted({str(item["bdc"]) for item in entries if item.get("kind") == "surplus"})
    parts = []
    if missing_lots:
        parts.append("Lots " + ", ".join(missing_lots))
    if surplus_bdcs:
        parts.append("écarts " + ", ".join(surplus_bdcs))
    counts = {cat: 0 for cat in CATEGORY_KEYS}
    for item in entries:
        category = item.get("category")
        if category in counts:
            counts[category] += _int(item.get("count"))
    counts_label = format_category_counts(counts)
    note = " + ".join(parts)
    if counts_label:
        note = f"{note} ({counts_label})" if note else counts_label
    return note


def _entry_rate(entries: Sequence[Tuple[int, Decimal]]) -> Optional[Decimal]:
    return _weighted_rate(entries)


def _add_issue(issues: List[Dict[str, Any]], issue: Dict[str, Any]) -> None:
    for existing in issues:
        if existing.get("code") == issue.get("code") and existing.get("message") == issue.get(
            "message"
        ):
            return
    issues.append(issue)


def build_draft(
    *,
    month: str,
    reporting_payload: Optional[Dict[str, Any]],
    bdc_payload: Sequence[Dict[str, Any]],
    snapshot: Optional[Dict[str, Any]] = None,
    sources: Optional[Dict[str, Any]] = None,
    settings: Optional[Dict[str, Any]] = None,
    version: int = 1,
    reason: str = "",
    previous: Optional[Dict[str, Any]] = None,
    keep_manual_lines: bool = False,
) -> Dict[str, Any]:
    """Build a billing draft from BDC facts and the reconciliation analysis."""
    settings = {**DEFAULT_SETTINGS, **(settings or {})}
    basis = str(settings.get("billing_basis") or "actual_capped")
    vat_rate = str(settings.get("vat_rate") or "0.00")
    overrides = settings.get("rate_overrides") or {}

    analysis, snapshot_used = _resolve_analysis(reporting_payload, bdc_payload, snapshot)
    facts = {
        str(order.get("numero_bdc") or "").strip(): _order_facts(order) for order in bdc_payload
    }
    grid = build_rate_grid(bdc_payload)
    for category, value in (overrides or {}).items():
        parsed = parse_decimal(value)
        if category in CATEGORY_KEYS and parsed is not None:
            grid["rates"][category] = quantize_money(parsed)

    issues: List[Dict[str, Any]] = []
    issues.extend(grid["warnings"])
    if not snapshot_used:
        issues.append(
            _issue(
                "no_snapshot",
                "warning",
                "Aucun snapshot de réconciliation : analyse recalculée sans les ajustements de session.",
            )
        )
    else:
        snapshot_sources = (snapshot or {}).get("sources") or {}
        live_sources = sources or {}
        labels = {"reporting": "rapport de production", "bdc": "rapport BDC"}
        for key, label in labels.items():
            stored_hash = str((snapshot_sources.get(key) or {}).get("sha256") or "")
            live_hash = str((live_sources.get(key) or {}).get("sha256") or "")
            if stored_hash and live_hash and stored_hash != live_hash:
                _add_issue(
                    issues,
                    _issue(
                        "stale_snapshot",
                        "error",
                        f"Le snapshot de réconciliation ne correspond plus au {label} courant : "
                        "relancez l’acte 3 avant de facturer.",
                        source=key,
                    ),
                )

    preserved_ids: Dict[str, Dict[str, Any]] = {}
    if keep_manual_lines and previous:
        for line in previous.get("lines") or []:
            if line.get("source") == "manual" or line.get("kind") == "manual":
                preserved_ids[str(line.get("line_id"))] = copy.deepcopy(line)

    lines: List[Dict[str, Any]] = []
    used_ids: set = set()
    provision_entries: List[Dict[str, Any]] = []
    already_billed: List[Dict[str, Any]] = []
    missing_lot_labels: List[str] = []

    def add_line(line: Dict[str, Any]) -> None:
        line_id = str(line.get("line_id"))
        if line_id in preserved_ids:
            lines.append(preserved_ids.pop(line_id))
            used_ids.add(line_id)
            return
        while line_id in used_ids:
            line_id = f"{line_id}·bis"
        line["line_id"] = line_id
        used_ids.add(line_id)
        lines.append(line)

    for cluster in analysis.get("cluster_results") or []:
        bdc_numbers = _unique(cluster.get("bdc_numbers") or [])
        if not bdc_numbers:
            continue
        cluster_key = str(cluster.get("cluster_key") or "cluster")
        billed = {cat: _int((cluster.get("billed") or {}).get(cat)) for cat in CATEGORY_KEYS}
        actual = {cat: _int((cluster.get("actual") or {}).get(cat)) for cat in CATEGORY_KEYS}
        billed_unknown = _int(cluster.get("billed_unknown"))

        for diagnostic in cluster.get("diagnostics") or []:
            if diagnostic.get("code") != "compensated_lot_mismatch":
                continue
            _add_issue(
                issues,
                _issue(
                    "compensated_lot_mismatch",
                    "info",
                    "Écarts internes compensés entre lots sur "
                    f"{', '.join(bdc_numbers)} : le total reste facturé au niveau du BDC.",
                    cluster=cluster_key,
                ),
            )

        orders = [facts.get(number) for number in bdc_numbers]
        known_orders = [order for order in orders if order is not None]
        for number in bdc_numbers:
            if facts.get(number) is None:
                _add_issue(
                    issues,
                    _issue(
                        "bdc_missing_facts",
                        "error",
                        f"{number} est référencé dans l’analyse mais absent du JSON BDC.",
                        bdc=number,
                    ),
                )
        for order in known_orders:
            _order_issues(order, issues)

        if not known_orders:
            continue

        has_known_quantities = any(
            order["category_quantities"].get(category, 0)
            for order in known_orders
            for category in CATEGORY_KEYS
        )
        allocation_fallback = len(known_orders) > 1 and not has_known_quantities

        if basis == "ordered":
            cluster_line_amounts: Dict[str, Decimal] = {}
            for order in known_orders:
                cluster_line_amounts[order["number"]] = Decimal(order["total_cents"]) / 100
        elif len(known_orders) == 1:
            order = known_orders[0]
            amount = ZERO
            for category in CATEGORY_KEYS:
                ordered = billed.get(category, 0)
                done = actual.get(category, 0)
                billable = min(done, ordered)
                ratio = (Decimal(billable) / Decimal(ordered)) if ordered else ZERO
                amount += order["category_amounts"].get(category, ZERO) * ratio
            if billed_unknown or order.get("unknown_quantity"):
                amount = Decimal(order["total_cents"]) / 100
            cluster_line_amounts = {order["number"]: amount}
        elif allocation_fallback:
            cluster_line_amounts = {
                order["number"]: Decimal(order["total_cents"]) / 100 for order in known_orders
            }
            _add_issue(
                issues,
                _issue(
                    "unknown_articles_allocation",
                    "warning",
                    "Aucune catégorie reconnue pour les BDC partageant ce cluster : "
                    "les montants BDC sont repris sans prorata.",
                    cluster=cluster_key,
                    bdcs=bdc_numbers,
                ),
            )
        else:
            total_ordered = {
                category: sum(
                    order["category_quantities"].get(category, 0) for order in known_orders
                )
                for category in CATEGORY_KEYS
            }
            cluster_line_amounts = {}
            for order in known_orders:
                amount = ZERO
                for category in CATEGORY_KEYS:
                    ordered = total_ordered.get(category, 0)
                    ordered_b = order["category_quantities"].get(category, 0)
                    share = (Decimal(ordered_b) / Decimal(ordered)) if ordered else ZERO
                    done_b = Decimal(actual.get(category, 0)) * share
                    billable_amount = min(done_b, Decimal(ordered_b))
                    ratio = (billable_amount / Decimal(ordered_b)) if ordered_b else ZERO
                    amount += order["category_amounts"].get(category, ZERO) * ratio
                amount += order["unknown_amount"]
                cluster_line_amounts[order["number"]] = amount
            _add_issue(
                issues,
                _issue(
                    "multi_bdc_allocation",
                    "warning",
                    "Plusieurs BDC partagent un même cluster : allocation proportionnelle des volumes.",
                    cluster=cluster_key,
                    bdcs=bdc_numbers,
                ),
            )

        for order in known_orders:
            category_detail: Dict[str, Any] = {}
            for category in CATEGORY_KEYS:
                ordered = billed.get(category, 0)
                done = actual.get(category, 0)
                if basis == "ordered" or allocation_fallback:
                    billable = ordered
                elif len(known_orders) == 1:
                    billable = min(done, ordered)
                else:
                    total_ordered = sum(
                        item["category_quantities"].get(category, 0) for item in known_orders
                    )
                    ordered_b = order["category_quantities"].get(category, 0)
                    share = (Decimal(ordered_b) / Decimal(total_ordered)) if total_ordered else ZERO
                    billable = int(
                        min(Decimal(done) * share, Decimal(ordered_b)).quantize(Decimal("1"))
                    )
                category_detail[category] = {
                    "ordered": ordered,
                    "actual": done,
                    "billable": billable,
                    "surplus": max(done - ordered, 0),
                }
            amount = cluster_line_amounts.get(order["number"], ZERO)
            if amount <= 0 and not any(actual.values()):
                _add_issue(
                    issues,
                    _issue(
                        "no_production",
                        "warning",
                        f"{order['number']} n’a aucune production réalisée ce mois-ci.",
                        bdc=order["number"],
                    ),
                )
            add_line(
                make_line(
                    line_id=f"bdc:{order['number']}",
                    kind="bdc",
                    label=_line_label(order["number"], order["delivery_date"]),
                    bdc_number=order["number"],
                    delivery_date=order["delivery_date"],
                    unit_price_cents=to_cents(amount),
                    vat_rate=vat_rate,
                    detail={
                        "categories": category_detail,
                        "cluster_key": cluster_key,
                        "cluster_ids": cluster.get("cluster_ids") or [],
                        "billed_total": _int(cluster.get("billed_total")),
                        "actual_total": _int(cluster.get("actual_total")),
                        "bdc_format": order.get("bdc_format") or "simplified",
                        "annex_videos": _int(order.get("annex_videos")),
                        "concerned_lots": _int(order.get("concerned_lots")),
                        "video_match_summary": _video_match_summary(cluster),
                    },
                )
            )

        # Surplus on covered clusters: production beyond the ordered volume.
        for category in CATEGORY_KEYS:
            surplus = max(actual.get(category, 0) - billed.get(category, 0), 0)
            if surplus <= 0:
                continue
            rate = None
            if len(known_orders) == 1:
                rate = _entry_rate(known_orders[0]["category_rates"].get(category, []))
            if rate is None:
                rate = grid["rates"].get(category)
            if rate is None:
                _add_issue(
                    issues,
                    _issue(
                        "missing_rate",
                        "error",
                        f"Aucun taux connu pour la catégorie « {CATEGORY_SHORT_LABELS.get(category, category)} » "
                        f"sur le surplus de {', '.join(bdc_numbers)}.",
                        category=category,
                    ),
                )
                continue
            provision_entries.append(
                {
                    "kind": "surplus",
                    "bdc": " / ".join(bdc_numbers),
                    "category": category,
                    "count": surplus,
                    "rate": decimal_str(rate),
                    "amount_cents": to_cents(rate * surplus),
                }
            )

    for item in analysis.get("missing_invoices") or []:
        lot = str(item.get("id_camille") or "")
        person = str(item.get("person") or "")
        adjacent = item.get("adjacent_month")
        if adjacent:
            already_billed.append(
                {
                    "lot": lot,
                    "person": person,
                    "adjacent_month": adjacent,
                }
            )
            _add_issue(
                issues,
                _issue(
                    "adjacent_month",
                    "info",
                    f"Lot {lot} déjà facturé sur un mois voisin : non provisionné.",
                    lot=lot,
                ),
            )
            continue
        missing_lot_labels.append(lot)
        categories = item.get("categories") or {}
        for category in CATEGORY_KEYS:
            cat_data = categories.get(category) or {}
            if isinstance(cat_data, dict):
                count = _int(cat_data.get("count"))
                if not count and cat_data.get("files"):
                    count = len(cat_data.get("files") or [])
            else:
                count = _int(cat_data)
            if count <= 0:
                continue
            rate = grid["rates"].get(category)
            if rate is None:
                _add_issue(
                    issues,
                    _issue(
                        "missing_rate_missing_lot",
                        "error",
                        f"Aucun taux connu pour la catégorie « {CATEGORY_SHORT_LABELS.get(category, category)} » "
                        f"du lot {lot}.",
                        lot=lot,
                        category=category,
                    ),
                )
                continue
            provision_entries.append(
                {
                    "kind": "missing",
                    "lot": lot,
                    "person": person,
                    "category": category,
                    "count": count,
                    "rate": decimal_str(rate),
                    "amount_cents": to_cents(rate * count),
                }
            )

    for item in analysis.get("unexpected_ids") or []:
        _add_issue(
            issues,
            _issue(
                "unexpected_lot",
                "warning",
                f"Lot {item} facturé sur un BDC mais sans production ce mois-ci.",
                lot=str(item),
            ),
        )

    for overlap in analysis.get("overlapping_bdc_lots") or []:
        _add_issue(
            issues,
            _issue(
                "overlap",
                "warning",
                f"Lot {overlap.get('lot')} revendiqué par plusieurs BDC : "
                f"{', '.join(overlap.get('bdc_numbers') or [])}.",
                lot=str(overlap.get("lot")),
            ),
        )

    if provision_entries:
        provision_total = sum(_int(entry.get("amount_cents")) for entry in provision_entries)
        counts = {category: 0 for category in CATEGORY_KEYS}
        for entry in provision_entries:
            category = entry.get("category")
            if category in counts:
                counts[category] += _int(entry.get("count"))
        add_line(
            make_line(
                line_id="uncovered:production",
                kind="uncovered",
                label="Lots réalisés sans BDC",
                unit_price_cents=provision_total,
                vat_rate=vat_rate,
                detail={
                    "entries": provision_entries,
                    "counts": counts,
                    "already_billed": already_billed,
                    "missing_lots": sorted(set(missing_lot_labels)),
                },
                note=_provision_note(provision_entries),
            )
        )

    for line in preserved_ids.values():
        if str(line.get("line_id")) not in used_ids:
            lines.append(line)
            used_ids.add(str(line.get("line_id")))

    if not lines:
        _add_issue(
            issues,
            _issue(
                "no_lines",
                "warning",
                "Aucune ligne de facturation calculée pour ce mois.",
            ),
        )

    # Stable order: BDC lines by BDC number, then provision, then manual lines.
    import re

    def natural_key(value: str) -> Tuple:
        return tuple(
            int(part) if part.isdigit() else part.lower()
            for part in re.split(r"(\d+)", value or "")
        )

    def sort_key(line: Dict[str, Any]) -> Tuple[int, Any]:
        kind = line.get("kind")
        if kind == "bdc":
            return (0, natural_key(str(line.get("bdc_number") or "")))
        if kind == "uncovered":
            return (1, ())
        return (2, natural_key(str(line.get("line_id") or "")))

    lines.sort(key=sort_key)

    severity_order = {"error": 0, "warning": 1, "info": 2}
    issues.sort(
        key=lambda item: (
            severity_order.get(str(item.get("severity") or ""), 9),
            str(item.get("code") or ""),
        )
    )

    certification = analysis.get("certification") or {}
    conformity = analysis.get("conformity") or {}
    summary = {
        "score": conformity.get("score", 0.0),
        "compliant_orders": conformity.get("compliant_orders", 0),
        "total_orders": conformity.get("total_orders", 0),
        "issues_count": analysis.get("issues_count", 0),
        "missing_invoices": len(analysis.get("missing_invoices") or []),
        "unexpected_ids": len(analysis.get("unexpected_ids") or []),
        "overlaps": len(analysis.get("overlapping_bdc_lots") or []),
        "is_certified": bool(certification.get("is_certified")),
    }

    draft = new_version(
        month=month,
        version=version,
        reason=reason or "Brouillon initial",
        settings=settings,
    )
    draft["sources"] = sources or {}
    draft["rate_grid"] = {cat: decimal_str(rate) for cat, rate in grid["rates"].items()}
    draft["rate_sources"] = grid["sources"]
    draft["lines"] = lines
    draft["issues"] = issues
    draft["certification"] = certification
    draft["reconciliation_summary"] = summary
    draft["already_billed"] = already_billed
    draft["unbilled_production"] = {
        "entries": provision_entries,
        "counts": {
            category: sum(
                _int(entry.get("count"))
                for entry in provision_entries
                if entry.get("category") == category
            )
            for category in CATEGORY_KEYS
        },
        "missing_lots": sorted(set(missing_lot_labels)),
    }
    draft["snapshot_used"] = snapshot_used
    if snapshot and snapshot.get("generated_at"):
        draft["snapshot_generated_at"] = snapshot.get("generated_at")

    return normalize_version(draft)


def validate_transition(
    version: Dict[str, Any],
    target_status: str,
    *,
    override_reason: str = "",
) -> List[str]:
    """Return human-readable blockers for a status transition (empty = allowed)."""
    version = normalize_version(version)
    blockers: List[str] = []
    current_status = str(version.get("status") or "brouillon")
    if target_status != current_status and target_status not in ALLOWED_TRANSITIONS.get(
        current_status, ()
    ):
        blockers.append(
            "Transition impossible depuis "
            f"« {STATUS_LABELS.get(current_status, current_status)} » vers "
            f"« {STATUS_LABELS.get(target_status, target_status)} »."
        )

    errors = [item for item in version.get("issues") or [] if item.get("severity") == "error"]
    if target_status in {"envoyé_pour_vérification", "validé_client", "émis"} and errors:
        blockers.extend(item.get("message", "Erreur bloquante") for item in errors)

    if target_status == "validé_client":
        acknowledged = set(version.get("acknowledged_warnings") or [])
        warnings = [
            item
            for item in version.get("issues") or []
            if item.get("severity") == "warning" and item.get("code") not in acknowledged
        ]
        if warnings:
            blockers.append(
                f"{len(warnings)} avertissement(s) non acquitté(s) avant validation client."
            )

    if target_status == "émis":
        if version.get("status") not in {"validé_client", "émis"}:
            blockers.append(
                "La version doit être validée client avant de pouvoir être marquée émise."
            )
        certification = version.get("certification") or {}
        if not certification.get("is_certified") and not override_reason.strip():
            blockers.append(
                "La réconciliation n’est pas certifiée : renseignez une justification d’override."
            )
        invoice = version.get("invoice") or {}
        if not str(invoice.get("number") or "").strip():
            blockers.append("Le numéro de facture Indy est obligatoire avant le statut « Émise ».")
        if not str(invoice.get("issued_on") or "").strip():
            blockers.append("La date d’émission Indy est obligatoire avant le statut « Émise ».")
        invoice_total = _int(invoice.get("total_cents"))
        version_total = _int((version.get("totals") or {}).get("ht_cents"))
        if (
            invoice_total
            and invoice_total != version_total
            and not str(invoice.get("note") or "").strip()
        ):
            blockers.append(
                "Le total Indy diffère du total de la version : ajoutez une note d’écart."
            )

    return blockers


def _line_snapshot(line: Dict[str, Any]) -> Dict[str, Any]:
    detail = line.get("detail") or {}
    return {
        "label": line.get("label") or "",
        "total_cents": _int(line.get("total_cents")),
        "unit_price_cents": _int(line.get("unit_price_cents")),
        "quantity": line.get("quantity") or "1.00",
        "included": bool(line.get("included", True)),
        "note": line.get("note") or "",
        "detail": detail,
        "kind": line.get("kind") or "",
        "bdc_number": line.get("bdc_number") or "",
        "delivery_date": line.get("delivery_date") or "",
    }


def diff_versions(previous: Optional[Dict[str, Any]], current: Dict[str, Any]) -> Dict[str, Any]:
    """Compare two billing versions (line-level and totals)."""
    previous = normalize_version(previous) if previous else None
    current = normalize_version(current)
    before = {str(line.get("line_id")): line for line in (previous or {}).get("lines", [])}
    after = {str(line.get("line_id")): line for line in current.get("lines", [])}

    changes: List[Dict[str, Any]] = []
    for line_id, line in after.items():
        current_snapshot = _line_snapshot(line)
        if line_id not in before:
            changes.append(
                {
                    "line_id": line_id,
                    "change": "added",
                    "label": line.get("label") or line_id,
                    "after": current_snapshot,
                    "before": None,
                    "delta_cents": _int(line.get("total_cents")),
                }
            )
            continue
        before_snapshot = _line_snapshot(before[line_id])
        if before_snapshot == current_snapshot:
            continue
        changes.append(
            {
                "line_id": line_id,
                "change": "changed",
                "label": line.get("label") or line_id,
                "before": before_snapshot,
                "after": current_snapshot,
                "delta_cents": _int(line.get("total_cents"))
                - _int(before[line_id].get("total_cents")),
            }
        )

    for line_id, line in before.items():
        if line_id in after:
            continue
        changes.append(
            {
                "line_id": line_id,
                "change": "removed",
                "label": line.get("label") or line_id,
                "before": _line_snapshot(line),
                "after": None,
                "delta_cents": -_int(line.get("total_cents")),
            }
        )

    source_changes = []
    if previous:
        previous_sources = previous.get("sources") or {}
        current_sources = current.get("sources") or {}
        for name in sorted(set(previous_sources) | set(current_sources)):
            before_hash = (previous_sources.get(name) or {}).get("sha256")
            after_hash = (current_sources.get(name) or {}).get("sha256")
            if before_hash != after_hash:
                source_changes.append({"name": name, "before": before_hash, "after": after_hash})

    previous_totals = (previous or {}).get("totals") or {}
    current_totals = current.get("totals") or {}
    return {
        "lines": changes,
        "has_changes": bool(changes) or bool(source_changes),
        "totals": {
            "before": _int(previous_totals.get("ht_cents")),
            "after": _int(current_totals.get("ht_cents")),
            "delta": _int(current_totals.get("ht_cents")) - _int(previous_totals.get("ht_cents")),
        },
        "source_changes": source_changes,
    }


def apply_line_update(
    version: Dict[str, Any],
    line_id: str,
    *,
    label: Optional[str] = None,
    quantity: Optional[str] = None,
    unit_price: Optional[str] = None,
    note: Optional[str] = None,
    included: Optional[bool] = None,
    delivery_date: Optional[str] = None,
) -> Dict[str, Any]:
    """Update a draft line and recompute totals. Returns the normalized version."""
    line = find_line(version, line_id)
    if line is None:
        raise ValueError("Ligne introuvable.")
    if label is not None:
        line["label"] = label.strip()
    if quantity is not None:
        parsed = parse_decimal(quantity, default=Decimal("1")) or Decimal("1")
        if parsed < 0:
            raise ValueError("La quantité ne peut pas être négative.")
        line["quantity"] = decimal_str(parsed)
    if unit_price is not None:
        parsed = parse_decimal(unit_price, default=ZERO) or ZERO
        if parsed < 0:
            raise ValueError("Le prix unitaire ne peut pas être négatif.")
        line["unit_price_cents"] = to_cents(parsed)
    if note is not None:
        line["note"] = note.strip()
    if delivery_date is not None:
        line["delivery_date"] = delivery_date.strip()
    if included is not None:
        line["included"] = bool(included)
    if version.get("status") == "brouillon":
        line["edited"] = True
    return normalize_version(version)


def add_manual_line(
    version: Dict[str, Any],
    *,
    label: str,
    quantity: str,
    unit_price: str,
    note: str = "",
    vat_rate: Optional[str] = None,
) -> Dict[str, Any]:
    label = (label or "").strip()
    if not label:
        raise ValueError("Le libellé de la ligne est obligatoire.")
    parsed_quantity = parse_decimal(quantity, default=Decimal("1")) or Decimal("1")
    parsed_price = parse_decimal(unit_price, default=ZERO) or ZERO
    if parsed_quantity < 0 or parsed_price < 0:
        raise ValueError("La quantité et le prix doivent être positifs.")
    settings = version.get("settings") or {}
    version.setdefault("lines", []).append(
        make_line(
            line_id=f"manual:{uuid.uuid4().hex[:12]}",
            kind="manual",
            label=label,
            quantity=decimal_str(parsed_quantity),
            unit_price_cents=to_cents(parsed_price),
            vat_rate=vat_rate or str(settings.get("vat_rate", "0.00")),
            source="manual",
            note=note.strip(),
        )
    )
    return normalize_version(version)
