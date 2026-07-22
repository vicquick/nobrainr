"""Golden-v3 abstention scoring: the system must DECLINE confidently
answering facts that never existed (BEAM/LoCoMo `_abs` pattern)."""

from __future__ import annotations

from nobrainr.services.eval_retrieval import (
    ABSTENTION_CONFIDENCE_BAR,
    _abstention_passes,
)


def test_empty_results_pass():
    assert _abstention_passes([]) is True


def test_low_confidence_top_hit_passes():
    assert _abstention_passes([{"id": "a", "similarity": 0.40}]) is True


def test_confident_top_hit_fails():
    # confidently "answering" a fact that never existed = fabrication risk
    assert _abstention_passes([{"id": "a", "similarity": 0.80}]) is False


def test_relevance_fallback_used():
    assert _abstention_passes([{"id": "a", "relevance": 0.90}]) is False
    assert _abstention_passes([{"id": "a", "relevance": 0.30}]) is True


def test_bar_is_exclusive():
    assert _abstention_passes(
        [{"id": "a", "similarity": ABSTENTION_CONFIDENCE_BAR}]) is False
    assert _abstention_passes(
        [{"id": "a", "similarity": ABSTENTION_CONFIDENCE_BAR - 0.01}]) is True


def test_missing_scores_pass():
    # no score info = cannot be called confident
    assert _abstention_passes([{"id": "a"}]) is True
