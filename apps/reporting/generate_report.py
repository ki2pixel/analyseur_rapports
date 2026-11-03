#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Dict, List

from analyser import (
    ARCHIVES_ROOT,
    DEFAULT_THRESHOLDS,
    analyze_month,
)
from jinja2 import Environment, FileSystemLoader, select_autoescape

from libs.web.io import atomic_write_text

BASE_DIR = Path(__file__).resolve().parent
ARCHIVES_DIR = ARCHIVES_ROOT


def compute_totals(report) -> Dict[str, Any]:
    """Compute total_mp4 and per-group totals for a list of GroupReport objects."""
    if not report:
        return {"total_mp4": 0, "group_totals": {}}
    total_mp4 = 0
    group_totals: Dict[str, int] = {}
    for group in report:
        group_id = f"{group.id_camille} {group.person}"
        group_total = 0
        for cat_key in ("short", "standard", "medium", "long"):
            cat = group.categories.get(cat_key)
            files = cat.files if cat and getattr(cat, "files", None) else []
            mp4_count = sum(1 for f in files if isinstance(f, str) and f.lower().endswith(".mp4"))
            group_total += mp4_count
        group_totals[group_id] = group_total
        total_mp4 += group_total
    return {"total_mp4": total_mp4, "group_totals": group_totals}


def build_env():
    env = Environment(
        loader=FileSystemLoader(str(BASE_DIR / "templates")),
        autoescape=select_autoescape(["html", "xml"]),
    )
    return env


def main():
    parser = argparse.ArgumentParser(description="Génère un rapport HTML des vidéos par mois")
    parser.add_argument("--month", required=True, help="Mois au format YYYY-MM")
    parser.add_argument(
        "--out", default=str(BASE_DIR / "rapport.html"), help="Fichier de sortie HTML"
    )
    args = parser.parse_args()
    year, month = args.month.split("-")

    report = analyze_month(ARCHIVES_DIR, int(year), int(month), DEFAULT_THRESHOLDS)
    totals = compute_totals(report)

    env = build_env()
    tpl = env.get_template("report.html")
    html = tpl.render(report=report, selected=args.month, **totals)

    out_path = Path(args.out)
    atomic_write_text(out_path, html)
    print(f"Rapport écrit: {out_path}")


if __name__ == "__main__":
    main()
