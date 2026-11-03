"""Shared helpers for normalising and filtering video file names."""

from __future__ import annotations

import re
import unicodedata

# Un jingle M6+ est un asset court/bumper (ex: 'm6+.mp4', 'm6_plus.mp4', 'm6+max anime.mp4').
# Les vraies vidéos de programme diffusées sur M6+ comportent un séparateur (' - ') suivi du titre.
EXCLUDED_VIDEO_PATTERN = re.compile(
    r"^m6(?:\+|_plus)(?![\s_]*[-–—])(?:[\s_]*(?:max[\s_]*anime|jingle|audio|video|anim|1ep|test))*[\s_]*(?:\.[a-z0-9]+)?$",
    re.IGNORECASE,
)


def is_excluded_video_name(name: str) -> bool:
    """Return True for M6+ jingles that must never be counted as content."""
    return bool(EXCLUDED_VIDEO_PATTERN.match((name or "").strip()))


def normalize_video_name(name: str) -> str:
    """Normalise a file name for duplicate detection within a lot."""
    normalized = unicodedata.normalize("NFKC", (name or "").strip())
    return re.sub(r"\s+", " ", normalized).casefold()
