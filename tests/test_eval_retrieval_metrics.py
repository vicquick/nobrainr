"""Unit tests for the retrieval eval metrics (Recall@K, MRR, nDCG@K).

Pure-math tests — no DB, no LLM. Just verifying the formulas behave as
expected on a handful of hand-computed cases so we can trust them when
the weekly scheduler publishes numbers.
"""

from __future__ import annotations

import math

from nobrainr.services.eval_retrieval import (
    _dcg,
    _ndcg_at_k,
    _recall_at_k,
    _reciprocal_rank,
)


def test_recall_perfect_hit():
    returned = ["a", "b", "c"]
    expected = {"a"}
    assert _recall_at_k(returned, expected, k=10) == 1.0


def test_recall_partial():
    returned = ["x", "a", "b", "c"]
    expected = {"a", "b", "d"}
    # 2 of 3 expected recovered → 2/3
    assert _recall_at_k(returned, expected, k=10) == 2 / 3


def test_recall_k_cutoff_drops_late_hits():
    returned = ["x", "y", "z", "a"]
    expected = {"a"}
    # Relevant item is at rank 4 → not in top-3 → recall@3 = 0
    assert _recall_at_k(returned, expected, k=3) == 0.0


def test_recall_empty_expected():
    assert _recall_at_k(["a"], set(), k=10) == 0.0


def test_rr_first_position():
    assert _reciprocal_rank(["a", "b"], {"a"}) == 1.0


def test_rr_second_position():
    assert _reciprocal_rank(["b", "a"], {"a"}) == 0.5


def test_rr_no_hit():
    assert _reciprocal_rank(["b", "c"], {"a"}) == 0.0


def test_dcg_sanity():
    # Two relevant in top-2 → DCG = 1/log2(2) + 1/log2(3) = 1 + ~0.6309
    assert math.isclose(_dcg([1, 1]), 1.0 + 1.0 / math.log2(3), rel_tol=1e-9)


def test_ndcg_perfect_order():
    # The only relevant is already at rank 1 → nDCG = 1.0
    assert _ndcg_at_k(["a", "b", "c"], {"a"}, k=3) == 1.0


def test_ndcg_worse_than_ideal():
    # Relevant at rank 2 → nDCG = (1/log2(3)) / 1.0 = ~0.6309
    v = _ndcg_at_k(["x", "a", "y"], {"a"}, k=3)
    assert math.isclose(v, 1.0 / math.log2(3), rel_tol=1e-9)


def test_ndcg_zero_when_nothing_relevant():
    assert _ndcg_at_k(["x", "y", "z"], {"a"}, k=3) == 0.0
