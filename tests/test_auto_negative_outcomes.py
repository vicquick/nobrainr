"""Tests for the auto-negative outcome logger.

The pre-2026-04-18 feedback loop was dead because nothing ever wrote
was_useful=false. memory_search now auto-logs negatives under two
conditions — low recall count or low top-1 rerank score — so the
scheduler feedback job has variance to learn from.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from nobrainr.mcp.server import _log_auto_negative_outcomes


@pytest.mark.asyncio
async def test_low_recall_logs_one_negative_per_result(monkeypatch):
    # 2 results (< threshold 3) → 2 negatives, each tagged low_recall.
    results = [
        {"id": "11111111-1111-1111-1111-111111111111", "search_rank": 1},
        {"id": "22222222-2222-2222-2222-222222222222", "search_rank": 2},
    ]
    store = AsyncMock(return_value={})
    with patch("nobrainr.mcp.server.queries.store_memory_outcome", store):
        await _log_auto_negative_outcomes(
            results, "trace-abc", "short thin query"
        )
    assert store.await_count == 2
    for call in store.await_args_list:
        # memory_id + was_useful are positional; trace/context kwargs
        assert call.args[1] is False  # was_useful
        assert call.kwargs["context"] == "auto:low_recall"
        assert call.kwargs["query_trace_id"] == "trace-abc"
        assert call.kwargs["agent_id"] == "auto-negative"


@pytest.mark.asyncio
async def test_low_rerank_only_targets_top_1():
    # Enough results to skip low_recall, but top-1 has rerank_score 0.1
    # (< 0.3 threshold) → exactly one negative against top-1.
    results = [
        {"id": f"{i}" * 8 + "-1111-1111-1111-111111111111", "search_rank": i, "rerank_score": s}
        for i, s in enumerate([0.1, 0.5, 0.6, 0.7], start=1)
    ]
    store = AsyncMock(return_value={})
    with patch("nobrainr.mcp.server.queries.store_memory_outcome", store):
        await _log_auto_negative_outcomes(results, "trace-xyz", "q")
    assert store.await_count == 1
    call = store.await_args_list[0]
    assert call.kwargs["context"] == "auto:low_rerank"
    assert call.kwargs["result_rank"] == 1


@pytest.mark.asyncio
async def test_both_signals_merge_into_one_row_on_top_1():
    # Top-1 triggers BOTH low_recall (only 2 hits total) AND low_rerank
    # (score 0.1). Should collapse into a single outcome row with both
    # reasons in context, so ratio aggregates don't double-count.
    results = [
        {"id": "11111111-1111-1111-1111-111111111111", "search_rank": 1, "rerank_score": 0.1},
        {"id": "22222222-2222-2222-2222-222222222222", "search_rank": 2, "rerank_score": 0.2},
    ]
    store = AsyncMock(return_value={})
    with patch("nobrainr.mcp.server.queries.store_memory_outcome", store):
        await _log_auto_negative_outcomes(results, "trace-dup", "q")
    # 2 distinct (id, rank) keys — rank-1 merged, rank-2 kept
    assert store.await_count == 2
    top_call = next(
        c for c in store.await_args_list
        if str(c.args[0]).startswith("1111")
    )
    assert "low_recall" in top_call.kwargs["context"]
    assert "low_rerank" in top_call.kwargs["context"]


@pytest.mark.asyncio
async def test_healthy_search_logs_nothing():
    # 5 strong results → no triggers → no store_memory_outcome calls.
    results = [
        {"id": f"{i}" * 8 + "-1111-1111-1111-111111111111", "search_rank": i, "rerank_score": 0.8}
        for i in range(1, 6)
    ]
    store = AsyncMock(return_value={})
    with patch("nobrainr.mcp.server.queries.store_memory_outcome", store):
        await _log_auto_negative_outcomes(results, "trace-ok", "q")
    assert store.await_count == 0


@pytest.mark.asyncio
async def test_missing_rerank_score_skips_rerank_signal():
    # When reranker didn't run there's no rerank_score — don't fire the
    # low_rerank negative. Only low_recall fires when applicable.
    results = [
        {"id": "11111111-1111-1111-1111-111111111111", "search_rank": 1},
    ]
    store = AsyncMock(return_value={})
    with patch("nobrainr.mcp.server.queries.store_memory_outcome", store):
        await _log_auto_negative_outcomes(results, "trace-nr", "q")
    # Only low_recall (1 result < 3 threshold) — no low_rerank attempted.
    assert store.await_count == 1
    assert store.await_args_list[0].kwargs["context"] == "auto:low_recall"


@pytest.mark.asyncio
async def test_logging_failure_is_swallowed():
    # If the DB call blows up, we must not bubble the error into
    # memory_search. Swallow and log — fire-and-forget contract.
    results = [
        {"id": "11111111-1111-1111-1111-111111111111", "search_rank": 1},
    ]
    store = AsyncMock(side_effect=RuntimeError("db down"))
    with patch("nobrainr.mcp.server.queries.store_memory_outcome", store):
        # Should not raise
        await _log_auto_negative_outcomes(results, "trace-err", "q")
