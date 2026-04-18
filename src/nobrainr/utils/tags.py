"""Tag canonicalization — collapse case + separator variants.

Why: before 2026-04-18 tags accumulated 12,605 unique strings for
~48K memories. Case variants (Python/python, IFC/ifc, QGIS/qgis), space
vs hyphen (`data transformation` vs `data-transformation`), and synonym
clusters (`lesson`/`lessons`/`lessons-learned`, `postmortem`/
`postmortem-lesson`) made tag filters unreliable. Normalizing at insert
time + a one-shot backfill collapses ~17% duplicates and fixes filter
recall.

Preserves `.` `/` `:` `(` `)` — they carry real signal (filename tags,
namespaced tags like `color:red`, branch-name tags like
`feature/knowledge-crawl-evolution`).
"""

from __future__ import annotations

import re

# Rules:
#   1. lowercase (Python → python)
#   2. run of whitespace or underscores → single "-"
#   3. trim leading/trailing "-"
#   4. apply explicit alias table for known synonym clusters
#
# Any other chars pass through untouched so filenames / paths / namespaces
# keep working as tags.
_WHITESPACE_OR_UNDERSCORE = re.compile(r"[\s_]+")
_COLLAPSE_DASHES = re.compile(r"-{2,}")

_ALIASES: dict[str, str] = {
    # Lessons cluster — dominant tag is "lesson" (8,351 memories).
    "lessons": "lesson",
    "lessons-learned": "lesson",
    "meta-lesson": "lesson",
    # Keep "lesson-repeated" and "lesson-classifier" — distinct signals.
    # Post-mortem cluster — was fragmented into postmortem / postmortem-lesson /
    # postmortem-application. Collapse to single canonical "postmortem".
    "postmortem-lesson": "postmortem",
    "postmortem-application": "postmortem",
    "post-mortem": "postmortem",
    # Incident cluster.
    "incidents": "incident",
    # Keep "critical-incident" — qualifies severity.
}


def canonicalize_tag(tag: str) -> str:
    """Canonicalize a single tag. Returns empty string for empty input."""
    if not tag:
        return ""
    s = tag.strip().lower()
    s = _WHITESPACE_OR_UNDERSCORE.sub("-", s)
    s = _COLLAPSE_DASHES.sub("-", s).strip("-")
    return _ALIASES.get(s, s)


def canonicalize_tags(tags: list[str] | None) -> list[str]:
    """Canonicalize + deduplicate a tag list, preserving order."""
    if not tags:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for t in tags:
        c = canonicalize_tag(t)
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out
