"""Cheap heuristic quality score for newly inserted memories.

Why this exists (2026-06-11): the LLM quality_scoring job processes a small
batch per cycle, so during bulk imports 40-50% of the corpus sat with
quality_score NULL — invisible to quality-based tiering, archiving, and
ranking for weeks. This heuristic gives every row a usable score at insert
time; the LLM job later refines rows flagged ``quality_heuristic`` in
metadata, NULL rows first.

Deliberately dumb and deterministic: a few lexical signals that correlate
with the LLM rubric's specificity/actionability axes. It must never call a
model or the DB.
"""

import re

# Categories whose content is usually reusable knowledge vs ambient noise
_CATEGORY_WEIGHT = {
    "security": 0.10, "architecture": 0.10, "debugging": 0.08,
    "patterns": 0.08, "infrastructure": 0.06, "tooling": 0.05,
    "deployment": 0.05, "data": 0.04, "backend": 0.04, "frontend": 0.04,
    "insight": 0.02, "documentation": 0.0, "business": 0.0,
    "session-log": -0.10, "_archived": -0.20,
}

_SPECIFICITY_PATTERNS = (
    re.compile(r"\b\d+(\.\d+)+\b"),            # versions: 4.1.2
    re.compile(r"(?:/[\w.-]+){2,}"),           # paths: /opt/nobrainr/src
    re.compile(r"\b[a-z_]+\([^)]*\)"),         # calls: store_memory(...)
    re.compile(r"`[^`]+`"),                    # inline code
    re.compile(r"\b(?:https?://|ssh://)\S+"),  # urls
    re.compile(r"\b[A-Z_]{3,}=\S+"),           # env assignments
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),      # dates
)


def heuristic_quality_score(
    content: str, *, category: str | None = None, confidence: float | None = None,
) -> float:
    """Score content 0.05-0.95 from lexical signals only."""
    text = (content or "").strip()
    if len(text) < 30:
        return 0.1

    score = 0.35

    # Length band: 150-1500 chars is the sweet spot for self-contained
    # learnings; very short lacks context, very long is usually a dump.
    n = len(text)
    if 150 <= n <= 1500:
        score += 0.15
    elif n > 4000:
        score -= 0.10

    hits = sum(1 for p in _SPECIFICITY_PATTERNS if p.search(text))
    score += min(hits, 4) * 0.06

    score += _CATEGORY_WEIGHT.get((category or "").lower(), 0.0)

    # Extractor's own confidence, gently weighted
    if isinstance(confidence, (int, float)):
        score += (float(confidence) - 0.5) * 0.2

    return round(min(0.95, max(0.05, score)), 3)
