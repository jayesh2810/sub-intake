"""NAICS class code resolution against the reference table."""

from functools import lru_cache
from pathlib import Path
from typing import List, Optional

import yaml

from intake.models import NaicsInfo

RULES_DIR = Path(__file__).parent / "rules"


@lru_cache(maxsize=1)
def _classes() -> List[dict]:
    data = yaml.safe_load((RULES_DIR / "naics.yaml").read_text())
    return data["classes"]


def _to_info(entry: dict, matched_on: str) -> NaicsInfo:
    return NaicsInfo(
        code=entry["code"],
        title=entry["title"],
        hazard_grade=entry["hazard_grade"],
        notes=entry.get("notes"),
        matched_on=matched_on,
    )


def lookup(query: str) -> Optional[NaicsInfo]:
    """Resolve a NAICS code or a business description to a single class.

    An exact code match wins. Otherwise the query is scanned for keywords and
    the most specific match is returned.
    """
    q = (query or "").strip().lower()
    if not q:
        return None

    for entry in _classes():
        if entry["code"] == q:
            return _to_info(entry, "code")

    # Longest keyword first so "structural metal" beats "metal".
    best: Optional[tuple[int, dict, str]] = None
    for entry in _classes():
        for kw in entry.get("keywords", []):
            if kw in q and (best is None or len(kw) > best[0]):
                best = (len(kw), entry, kw)

    if best is None:
        return None
    _, entry, kw = best
    return _to_info(entry, kw)


def candidates(query: str, limit: int = 5) -> List[NaicsInfo]:
    """Every class whose keywords overlap the query, best match first."""
    q = (query or "").strip().lower()
    if not q:
        return []

    scored = []
    for entry in _classes():
        hits = [kw for kw in entry.get("keywords", []) if kw in q]
        if entry["code"] == q:
            scored.append((10_000, entry, "code"))
        elif hits:
            scored.append((max(len(k) for k in hits), entry, max(hits, key=len)))

    scored.sort(key=lambda t: t[0], reverse=True)
    return [_to_info(e, m) for _, e, m in scored[:limit]]
