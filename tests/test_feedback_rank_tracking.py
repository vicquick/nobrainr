"""Feedback-loop v6 (2026-04-11): query_trace_id / result_rank / query_text.

Phase 1 ships the plumbing — a memory_search call hands back a shared
search_trace_id + 1-indexed search_rank on every result row, and
memory_feedback / store_memory_outcome accept those fields so the caller
can close the loop and we can later compute MRR/NDCG. These tests lock the
wire shape in place without requiring a live postgres.
"""

from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

from nobrainr.db import queries as db_queries
from nobrainr.mcp import server as mcp_server


# ──────────────────────────────────────────────
# Schema contract
# ──────────────────────────────────────────────

def test_schema_contains_v6_alter_and_index():
    """Schema SQL must carry the v6 ALTERs + the partial index on trace_id."""
    from nobrainr.db.schema import SCHEMA_SQL  # module-level string

    assert "ALTER TABLE memory_outcomes ADD COLUMN IF NOT EXISTS query_trace_id uuid" in SCHEMA_SQL
    assert "ALTER TABLE memory_outcomes ADD COLUMN IF NOT EXISTS query_text text" in SCHEMA_SQL
    assert "ALTER TABLE memory_outcomes ADD COLUMN IF NOT EXISTS result_rank int" in SCHEMA_SQL
    assert "idx_memory_outcomes_trace" in SCHEMA_SQL
    assert "WHERE query_trace_id IS NOT NULL" in SCHEMA_SQL


# ──────────────────────────────────────────────
# store_memory_outcome input normalization
# ──────────────────────────────────────────────

class _FakeConn:
    """Minimal pool/conn stand-in that captures the INSERT args."""

    def __init__(self):
        self.fetchrow = AsyncMock(return_value={
            "id": UUID("00000000-0000-0000-0000-000000000001"),
            "created_at": __import__("datetime").datetime.now(
                tz=__import__("datetime").timezone.utc,
            ),
        })

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _FakePool:
    def __init__(self):
        self.conn = _FakeConn()

    def acquire(self):
        return self.conn


def _captured_insert_args(fetchrow: AsyncMock) -> tuple:
    """Return the positional args of the most recent fetchrow call.

    Strips the leading SQL string so indices line up with the $1..$N
    placeholders in the INSERT: args[0] = memory_id, args[1] = was_useful,
    args[2] = context, args[3] = agent_id, args[4] = session_id,
    args[5] = query_trace_id, args[6] = query_text, args[7] = result_rank.
    """
    assert fetchrow.await_args is not None, "fetchrow was never called"
    all_args = fetchrow.await_args.args
    assert isinstance(all_args[0], str), "first arg should be SQL string"
    return all_args[1:]


async def test_store_memory_outcome_accepts_trace_fields():
    pool = _FakePool()
    with patch.object(db_queries, "get_pool", AsyncMock(return_value=pool)), \
         patch.object(db_queries, "publish", MagicMock()):
        out = await db_queries.store_memory_outcome(
            "11111111-1111-1111-1111-111111111111",
            True,
            query_trace_id="22222222-2222-2222-2222-222222222222",
            query_text="how did we fix coolify networking",
            result_rank=3,
        )

    assert out["traced"] is True
    args = _captured_insert_args(pool.conn.fetchrow)
    assert args[0] == UUID("11111111-1111-1111-1111-111111111111")
    assert args[1] is True
    assert args[5] == UUID("22222222-2222-2222-2222-222222222222")
    assert args[6] == "how did we fix coolify networking"
    assert args[7] == 3


async def test_store_memory_outcome_drops_bad_trace_uuid():
    pool = _FakePool()
    with patch.object(db_queries, "get_pool", AsyncMock(return_value=pool)), \
         patch.object(db_queries, "publish", MagicMock()):
        out = await db_queries.store_memory_outcome(
            "11111111-1111-1111-1111-111111111111",
            False,
            query_trace_id="not-a-uuid",
            result_rank=1,
        )

    assert out["traced"] is False
    args = _captured_insert_args(pool.conn.fetchrow)
    assert args[5] is None  # trace dropped
    assert args[7] == 1     # rank preserved


async def test_store_memory_outcome_rejects_zero_rank():
    """Rank is 1-indexed — 0 or negative means 'unknown', must coerce to None."""
    pool = _FakePool()
    with patch.object(db_queries, "get_pool", AsyncMock(return_value=pool)), \
         patch.object(db_queries, "publish", MagicMock()):
        await db_queries.store_memory_outcome(
            "11111111-1111-1111-1111-111111111111",
            True,
            result_rank=0,
        )

    args = _captured_insert_args(pool.conn.fetchrow)
    assert args[7] is None


async def test_store_memory_outcome_trims_long_query_text():
    """Query text is diagnostic, not content — 500 chars is plenty."""
    pool = _FakePool()
    long_q = "x" * 2000
    with patch.object(db_queries, "get_pool", AsyncMock(return_value=pool)), \
         patch.object(db_queries, "publish", MagicMock()):
        await db_queries.store_memory_outcome(
            "11111111-1111-1111-1111-111111111111",
            True,
            query_text=long_q,
        )

    args = _captured_insert_args(pool.conn.fetchrow)
    assert args[6] is not None
    assert len(args[6]) == 500


async def test_store_memory_outcome_backward_compatible():
    """Old callers with no trace args still work and get traced=False."""
    pool = _FakePool()
    with patch.object(db_queries, "get_pool", AsyncMock(return_value=pool)), \
         patch.object(db_queries, "publish", MagicMock()):
        out = await db_queries.store_memory_outcome(
            "11111111-1111-1111-1111-111111111111",
            True,
        )

    assert out["traced"] is False
    args = _captured_insert_args(pool.conn.fetchrow)
    assert args[5] is None
    assert args[6] is None
    assert args[7] is None


# ──────────────────────────────────────────────
# memory_search trace injection
# ──────────────────────────────────────────────

async def test_memory_search_injects_shared_trace_id_and_one_indexed_rank():
    """Every result row gets search_trace_id (same across rows) + 1-indexed search_rank + search_query."""
    fake_results = [
        {"id": "aaa", "content": "A", "similarity": 0.9},
        {"id": "bbb", "content": "B", "similarity": 0.8},
        {"id": "ccc", "content": "C", "similarity": 0.7},
    ]

    embed_mock = AsyncMock(return_value=[[0.1] * 1024])
    search_mock = AsyncMock(return_value=list(fake_results))
    expand_mock = AsyncMock(side_effect=lambda rows, window: rows)
    record_interest_mock = AsyncMock()

    # Disable reranker so we don't need a real model
    with (
        patch("nobrainr.embeddings.ollama.embed_batch", embed_mock),
        patch.object(mcp_server.queries, "search_memories", search_mock),
        patch.object(mcp_server.queries, "expand_chunk_context", expand_mock),
        patch.object(mcp_server.queries, "record_interest_signal", record_interest_mock),
        patch.object(mcp_server.settings, "reranker_enabled", False),
        patch.object(mcp_server.settings, "interest_tracking_enabled", False),
        patch.object(mcp_server.settings, "chunk_context_window", 0),
    ):
        # Unwrap the FastMCP-registered tool to call the underlying coroutine
        fn = mcp_server.memory_search
        if hasattr(fn, "fn"):       # FastMCP wraps the original on .fn
            fn = fn.fn
        elif hasattr(fn, "__wrapped__"):
            fn = fn.__wrapped__
        results = await fn("coolify networking fix", limit=5)

    assert len(results) == 3
    trace_ids = {r["search_trace_id"] for r in results}
    assert len(trace_ids) == 1, "all results must share a single trace_id per search"
    # Valid UUID
    UUID(next(iter(trace_ids)))

    ranks = [r["search_rank"] for r in results]
    assert ranks == [1, 2, 3], "rank must be 1-indexed and follow result order"

    for r in results:
        assert r["search_query"] == "coolify networking fix"


async def test_memory_search_distinct_trace_id_per_call():
    """Two calls → two distinct trace_ids (so feedback from call A can't bleed into call B)."""
    from copy import deepcopy
    fake_results = [{"id": "aaa", "content": "A", "similarity": 0.9}]

    embed_mock = AsyncMock(return_value=[[0.1] * 1024])
    # Deep-copy per call so the first call's mutation of search_trace_id
    # doesn't alias into the second call's return value.
    search_mock = AsyncMock(side_effect=lambda **_: deepcopy(fake_results))
    expand_mock = AsyncMock(side_effect=lambda rows, window: rows)

    with (
        patch("nobrainr.embeddings.ollama.embed_batch", embed_mock),
        patch.object(mcp_server.queries, "search_memories", search_mock),
        patch.object(mcp_server.queries, "expand_chunk_context", expand_mock),
        patch.object(mcp_server.settings, "reranker_enabled", False),
        patch.object(mcp_server.settings, "interest_tracking_enabled", False),
        patch.object(mcp_server.settings, "chunk_context_window", 0),
    ):
        fn = mcp_server.memory_search
        if hasattr(fn, "fn"):
            fn = fn.fn
        elif hasattr(fn, "__wrapped__"):
            fn = fn.__wrapped__
        r1 = await fn("query one")
        r2 = await fn("query two")

    assert r1[0]["search_trace_id"] != r2[0]["search_trace_id"]


# ──────────────────────────────────────────────
# memory_feedback tool signature
# ──────────────────────────────────────────────

def test_memory_feedback_tool_signature_has_trace_fields():
    """The MCP tool definition must expose the new optional params."""
    fn = mcp_server.memory_feedback
    if hasattr(fn, "fn"):
        fn = fn.fn
    elif hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    sig = inspect.signature(fn)
    assert "query_trace_id" in sig.parameters
    assert "result_rank" in sig.parameters
    assert "query_text" in sig.parameters
    # All new params must default to None so old callers don't break
    for name in ("query_trace_id", "result_rank", "query_text"):
        assert sig.parameters[name].default is None, f"{name} must default to None"
