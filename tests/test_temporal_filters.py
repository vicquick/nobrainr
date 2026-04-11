"""Temporal filters on memory_search (query intent parser phase 1).

Phase 1 adds ``date_from`` / ``date_to`` params to ``memory_search`` (MCP
tool) and threads them through ``search_memories`` + ``_build_filter_clause``
+ ``_hybrid_search_rrf``. Phase 2 (future) will add an LLM-based intent
parser that converts natural-language temporal expressions ("last week",
"before Thursday") into these absolute date bounds.

These tests lock in the wire shape — agents can pass temporal filters
today, the SQL actually bounds created_at, and bogus input degrades
silently to no-filter instead of blowing up.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from nobrainr.db import queries as db_queries
from nobrainr.mcp import server as mcp_server


def _unwrap(fn):
    if hasattr(fn, "fn"):
        return fn.fn
    if hasattr(fn, "__wrapped__"):
        return fn.__wrapped__
    return fn


# ──────────────────────────────────────────────
# _build_filter_clause — the SQL fragment builder
# ──────────────────────────────────────────────


def test_build_filter_clause_appends_date_conditions():
    """date_from and date_to both land as SQL conditions with sequential param indexes."""
    df = datetime(2026, 3, 1, tzinfo=timezone.utc)
    dt = datetime(2026, 4, 1, tzinfo=timezone.utc)

    clause, params, next_idx = db_queries._build_filter_clause(
        start_idx=5,
        tags=None,
        category=None,
        source_type=None,
        source_machine=None,
        date_from=df,
        date_to=dt,
    )

    assert "created_at >= $5" in clause
    assert "created_at <= $6" in clause
    assert params == [df, dt]
    assert next_idx == 7


def test_build_filter_clause_date_conditions_come_after_existing_filters():
    """When tags + category + date are all set, dates get the LATER param indexes."""
    df = datetime(2026, 3, 1, tzinfo=timezone.utc)

    clause, params, next_idx = db_queries._build_filter_clause(
        start_idx=3,
        tags=["foo"],
        category="insight",
        source_type=None,
        source_machine=None,
        date_from=df,
        date_to=None,
    )

    # tags=$3, category=$4, created_at=$5
    assert "tags && $3::text[]" in clause
    assert "category = $4" in clause
    assert "created_at >= $5" in clause
    assert params == [["foo"], "insight", df]
    assert next_idx == 6


def test_build_filter_clause_date_omitted_when_none():
    """No date filters → no SQL fragments + no params."""
    clause, params, next_idx = db_queries._build_filter_clause(
        start_idx=3,
        tags=None,
        category=None,
        source_type=None,
        source_machine=None,
    )

    assert "created_at" not in clause
    assert params == []
    assert next_idx == 3


def test_build_filter_clause_only_date_from():
    """Only the lower bound — upper bound omitted."""
    df = datetime(2026, 3, 1, tzinfo=timezone.utc)

    clause, params, next_idx = db_queries._build_filter_clause(
        start_idx=5,
        tags=None,
        category=None,
        source_type=None,
        source_machine=None,
        date_from=df,
    )

    assert "created_at >= $5" in clause
    assert "created_at <= " not in clause
    assert params == [df]
    assert next_idx == 6


# ──────────────────────────────────────────────
# memory_search MCP tool — ISO parsing + passthrough
# ──────────────────────────────────────────────


async def test_memory_search_parses_iso_date_from_and_passes_to_queries():
    """Plain ISO date string ("2026-03-01") is parsed and passed to search_memories."""
    search_mock = AsyncMock(return_value=[])
    embed_mock = AsyncMock(return_value=[[0.1] * 1024])

    with (
        patch("nobrainr.embeddings.ollama.embed_batch", embed_mock),
        patch.object(mcp_server.queries, "search_memories", search_mock),
        patch.object(mcp_server.queries, "expand_chunk_context", AsyncMock(side_effect=lambda r, window: r)),
        patch.object(mcp_server.settings, "reranker_enabled", False),
        patch.object(mcp_server.settings, "interest_tracking_enabled", False),
        patch.object(mcp_server.settings, "chunk_context_window", 0),
    ):
        fn = _unwrap(mcp_server.memory_search)
        await fn(
            query="docker networking fix",
            date_from="2026-03-01",
            date_to="2026-04-01",
        )

    search_mock.assert_awaited_once()
    kwargs = search_mock.await_args.kwargs
    # Parsed to real datetime objects
    assert kwargs["date_from"] == datetime(2026, 3, 1)
    assert kwargs["date_to"] == datetime(2026, 4, 1)


async def test_memory_search_parses_iso_datetime_with_z_suffix():
    """'Z' suffix is normalised to '+00:00' (Python 3.11+ quirk handled)."""
    search_mock = AsyncMock(return_value=[])
    embed_mock = AsyncMock(return_value=[[0.1] * 1024])

    with (
        patch("nobrainr.embeddings.ollama.embed_batch", embed_mock),
        patch.object(mcp_server.queries, "search_memories", search_mock),
        patch.object(mcp_server.queries, "expand_chunk_context", AsyncMock(side_effect=lambda r, window: r)),
        patch.object(mcp_server.settings, "reranker_enabled", False),
        patch.object(mcp_server.settings, "interest_tracking_enabled", False),
        patch.object(mcp_server.settings, "chunk_context_window", 0),
    ):
        fn = _unwrap(mcp_server.memory_search)
        await fn(
            query="what happened in march",
            date_from="2026-03-01T00:00:00Z",
            date_to="2026-03-31T23:59:59Z",
        )

    kwargs = search_mock.await_args.kwargs
    assert kwargs["date_from"].tzinfo is not None
    assert kwargs["date_from"].year == 2026
    assert kwargs["date_from"].month == 3
    assert kwargs["date_to"].day == 31


async def test_memory_search_invalid_date_falls_back_to_none():
    """Garbage date strings degrade to 'no filter' — search still runs."""
    search_mock = AsyncMock(return_value=[])
    embed_mock = AsyncMock(return_value=[[0.1] * 1024])

    with (
        patch("nobrainr.embeddings.ollama.embed_batch", embed_mock),
        patch.object(mcp_server.queries, "search_memories", search_mock),
        patch.object(mcp_server.queries, "expand_chunk_context", AsyncMock(side_effect=lambda r, window: r)),
        patch.object(mcp_server.settings, "reranker_enabled", False),
        patch.object(mcp_server.settings, "interest_tracking_enabled", False),
        patch.object(mcp_server.settings, "chunk_context_window", 0),
    ):
        fn = _unwrap(mcp_server.memory_search)
        await fn(
            query="test",
            date_from="not-a-date",
            date_to="also garbage",
        )

    kwargs = search_mock.await_args.kwargs
    assert kwargs["date_from"] is None
    assert kwargs["date_to"] is None
    # And the search still happened — didn't error out
    search_mock.assert_awaited_once()


async def test_memory_search_no_temporal_filter_keeps_defaults():
    """Omitting date args → None passed through cleanly."""
    search_mock = AsyncMock(return_value=[])
    embed_mock = AsyncMock(return_value=[[0.1] * 1024])

    with (
        patch("nobrainr.embeddings.ollama.embed_batch", embed_mock),
        patch.object(mcp_server.queries, "search_memories", search_mock),
        patch.object(mcp_server.queries, "expand_chunk_context", AsyncMock(side_effect=lambda r, window: r)),
        patch.object(mcp_server.settings, "reranker_enabled", False),
        patch.object(mcp_server.settings, "interest_tracking_enabled", False),
        patch.object(mcp_server.settings, "chunk_context_window", 0),
    ):
        fn = _unwrap(mcp_server.memory_search)
        await fn(query="test")

    kwargs = search_mock.await_args.kwargs
    assert kwargs["date_from"] is None
    assert kwargs["date_to"] is None


def test_memory_search_signature_exposes_date_params():
    """Regression guard: the MCP tool signature must advertise date_from/date_to."""
    import inspect

    fn = _unwrap(mcp_server.memory_search)
    sig = inspect.signature(fn)
    assert "date_from" in sig.parameters
    assert "date_to" in sig.parameters
    assert sig.parameters["date_from"].default is None
    assert sig.parameters["date_to"].default is None


def test_search_memories_signature_exposes_date_params():
    """queries.search_memories is the internal API; it must accept the date fields too."""
    import inspect

    sig = inspect.signature(db_queries.search_memories)
    assert "date_from" in sig.parameters
    assert "date_to" in sig.parameters
    assert sig.parameters["date_from"].default is None
    assert sig.parameters["date_to"].default is None
