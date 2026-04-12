"""Dual-layer user profile tool (Phase M, v6.14).

Phase M adds ``memory_get_user_profile`` — the Supermemory-inspired
pattern of combining static facts + recent activity + procedural rules
into a one-shot "everything an agent needs at session start" dict.

Tests cover:
  1. get_user_profile_layers SQL shape (static + recent)
  2. static_importance_floor parameter flows through
  3. source_machine filter flows through both queries
  4. recent window days parameterized
  5. MCP tool composition: both SQL layers + procedural merge
  6. Procedural fetch failure doesn't poison the whole profile (layers
     still returned, procedural_rules = [])
  7. Limit clamping: static_limit, recent_limit, rule_limit,
     recent_window_days, static_importance_floor
  8. counts dict reflects actual returned sizes

All SQL-level tests mock the pool; the MCP composition test mocks the
two DB functions.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nobrainr.db import queries as db_queries
from nobrainr.mcp import server as mcp_server


def _unwrap(fn):
    if hasattr(fn, "fn"):
        return fn.fn
    if hasattr(fn, "__wrapped__"):
        return fn.__wrapped__
    return fn


def _mock_row(**overrides):
    row = {
        "id": "11111111-1111-1111-1111-111111111111",
        "content": "A fact",
        "summary": "A summary",
        "category": "insight",
        "tags": ["foo"],
        "importance": 0.9,
        "stability": 0.8,
        "confidence": 0.9,
        "source_type": "manual",
        "source_machine": "bimavo",
        "created_at": datetime(2026, 4, 12, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 4, 12, tzinfo=timezone.utc),
        "last_accessed_at": datetime(2026, 4, 12, tzinfo=timezone.utc),
    }
    row.update(overrides)
    return row


async def _mock_db_query(static_rows=None, recent_rows=None):
    """Mock pool that returns static_rows for the first fetch and
    recent_rows for the second."""
    captured: dict = {"fetches": []}
    call_count = {"n": 0}

    async def fake_fetch(sql, *args):
        captured["fetches"].append({"sql": sql, "args": args})
        call_count["n"] += 1
        if call_count["n"] == 1:
            return static_rows or []
        return recent_rows or []

    mock_conn = AsyncMock()
    mock_conn.fetch = AsyncMock(side_effect=fake_fetch)
    acquire_ctx = MagicMock()
    acquire_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
    acquire_ctx.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquire_ctx)
    return pool, captured


# ──────────────────────────────────────────────
# get_user_profile_layers — SQL shape
# ──────────────────────────────────────────────


class TestGetUserProfileLayersSQL:
    @pytest.mark.asyncio
    async def test_static_query_uses_importance_floor(self):
        pool, cap = await _mock_db_query(static_rows=[_mock_row()])
        with patch.object(db_queries, "get_pool", AsyncMock(return_value=pool)):
            await db_queries.get_user_profile_layers()
        static_sql = cap["fetches"][0]["sql"]
        assert "importance >= $1" in static_sql
        assert "FROM memories" in static_sql
        assert "ORDER BY importance DESC" in static_sql

    @pytest.mark.asyncio
    async def test_static_query_passes_importance_floor(self):
        pool, cap = await _mock_db_query()
        with patch.object(db_queries, "get_pool", AsyncMock(return_value=pool)):
            await db_queries.get_user_profile_layers(static_importance_floor=0.9)
        args = cap["fetches"][0]["args"]
        assert 0.9 in args

    @pytest.mark.asyncio
    async def test_recent_query_uses_window_days_interval(self):
        pool, cap = await _mock_db_query()
        with patch.object(db_queries, "get_pool", AsyncMock(return_value=pool)):
            await db_queries.get_user_profile_layers(recent_window_days=14)
        recent_sql = cap["fetches"][1]["sql"]
        assert "INTERVAL '1 day'" in recent_sql
        # recent_window_days is the first param ($1) in the recent query
        args = cap["fetches"][1]["args"]
        assert 14 in args

    @pytest.mark.asyncio
    async def test_source_machine_filter_in_both_queries(self):
        pool, cap = await _mock_db_query()
        with patch.object(db_queries, "get_pool", AsyncMock(return_value=pool)):
            await db_queries.get_user_profile_layers(source_machine="bimavo")
        for fetch in cap["fetches"]:
            assert "bimavo" in fetch["args"]
            assert "source_machine = $" in fetch["sql"]

    @pytest.mark.asyncio
    async def test_recent_ordered_by_freshest_first(self):
        pool, cap = await _mock_db_query()
        with patch.object(db_queries, "get_pool", AsyncMock(return_value=pool)):
            await db_queries.get_user_profile_layers()
        recent_sql = cap["fetches"][1]["sql"]
        # GREATEST(last_accessed_at, created_at) DESC is the freshness order
        assert "GREATEST(" in recent_sql
        assert "last_accessed_at" in recent_sql
        assert "created_at" in recent_sql
        assert "DESC" in recent_sql

    @pytest.mark.asyncio
    async def test_static_ordered_by_importance_then_stability(self):
        pool, cap = await _mock_db_query()
        with patch.object(db_queries, "get_pool", AsyncMock(return_value=pool)):
            await db_queries.get_user_profile_layers()
        static_sql = cap["fetches"][0]["sql"]
        # Primary sort is importance, secondary is stability
        assert "ORDER BY importance DESC, stability DESC" in static_sql

    @pytest.mark.asyncio
    async def test_returns_dict_with_static_and_recent_keys(self):
        pool, _ = await _mock_db_query(
            static_rows=[_mock_row(id="22222222-2222-2222-2222-222222222222")],
            recent_rows=[_mock_row(id="33333333-3333-3333-3333-333333333333")],
        )
        with patch.object(db_queries, "get_pool", AsyncMock(return_value=pool)):
            result = await db_queries.get_user_profile_layers()
        assert "static" in result
        assert "recent" in result
        assert len(result["static"]) == 1
        assert len(result["recent"]) == 1
        # _row_to_dict converts id to string
        assert result["static"][0]["id"] == "22222222-2222-2222-2222-222222222222"
        assert result["recent"][0]["id"] == "33333333-3333-3333-3333-333333333333"


# ──────────────────────────────────────────────
# MCP tool — composition + clamping + procedural fetch safety
# ──────────────────────────────────────────────


class TestMemoryGetUserProfileMCP:
    @pytest.mark.asyncio
    async def test_tool_composes_all_three_layers(self):
        fn = _unwrap(mcp_server.memory_get_user_profile)
        mock_layers = AsyncMock(return_value={
            "static": [{"id": "s1"}, {"id": "s2"}],
            "recent": [{"id": "r1"}, {"id": "r2"}, {"id": "r3"}],
        })
        mock_proc = AsyncMock(return_value=[
            {"id": "p1", "priority": 90},
            {"id": "p2", "priority": 80},
        ])
        with patch.object(mcp_server.queries, "get_user_profile_layers", mock_layers), \
             patch.object(mcp_server.queries, "get_procedural_memories", mock_proc):
            result = await fn(source_machine="bimavo", agent_id="claude")
        assert result["source_machine"] == "bimavo"
        assert result["agent_id"] == "claude"
        assert len(result["static_facts"]) == 2
        assert len(result["recent_activity"]) == 3
        assert len(result["procedural_rules"]) == 2
        assert result["counts"]["static_facts"] == 2
        assert result["counts"]["recent_activity"] == 3
        assert result["counts"]["procedural_rules"] == 2
        assert "generated_at" in result

    @pytest.mark.asyncio
    async def test_procedural_fetch_failure_does_not_poison_profile(self):
        """A broken procedural_memories query must NOT cause the whole
        profile call to fail. Return an empty list and keep going."""
        fn = _unwrap(mcp_server.memory_get_user_profile)
        mock_layers = AsyncMock(return_value={
            "static": [{"id": "s1"}],
            "recent": [{"id": "r1"}],
        })
        mock_proc = AsyncMock(side_effect=RuntimeError("procedural table down"))
        with patch.object(mcp_server.queries, "get_user_profile_layers", mock_layers), \
             patch.object(mcp_server.queries, "get_procedural_memories", mock_proc):
            result = await fn()
        # Static/recent layers still returned
        assert len(result["static_facts"]) == 1
        assert len(result["recent_activity"]) == 1
        # Procedural gracefully empty
        assert result["procedural_rules"] == []
        assert result["counts"]["procedural_rules"] == 0

    @pytest.mark.asyncio
    async def test_agent_id_flows_to_procedural_query(self):
        fn = _unwrap(mcp_server.memory_get_user_profile)
        mock_layers = AsyncMock(return_value={"static": [], "recent": []})
        mock_proc = AsyncMock(return_value=[])
        with patch.object(mcp_server.queries, "get_user_profile_layers", mock_layers), \
             patch.object(mcp_server.queries, "get_procedural_memories", mock_proc):
            await fn(agent_id="claude-code")
        assert mock_proc.call_args.kwargs["agent_id"] == "claude-code"

    @pytest.mark.asyncio
    async def test_limits_clamped_to_safe_bounds(self):
        """static_limit 0-100, recent_limit 0-100, rule_limit 0-200,
        recent_window_days 1-90, static_importance_floor 0.0-1.0."""
        fn = _unwrap(mcp_server.memory_get_user_profile)
        mock_layers = AsyncMock(return_value={"static": [], "recent": []})
        mock_proc = AsyncMock(return_value=[])
        with patch.object(mcp_server.queries, "get_user_profile_layers", mock_layers), \
             patch.object(mcp_server.queries, "get_procedural_memories", mock_proc):
            await fn(
                static_limit=9999,         # clamp → 100
                recent_limit=9999,         # clamp → 100
                rule_limit=9999,           # clamp → 200
                recent_window_days=365,    # clamp → 90
                static_importance_floor=2.5,  # clamp → 1.0
            )
        kwargs = mock_layers.call_args.kwargs
        assert kwargs["static_limit"] == 100
        assert kwargs["recent_limit"] == 100
        assert kwargs["recent_window_days"] == 90
        assert kwargs["static_importance_floor"] == 1.0
        assert mock_proc.call_args.kwargs["limit"] == 200

    @pytest.mark.asyncio
    async def test_zero_limits_allowed(self):
        """Zero is valid — caller may want only one layer."""
        fn = _unwrap(mcp_server.memory_get_user_profile)
        mock_layers = AsyncMock(return_value={"static": [], "recent": []})
        mock_proc = AsyncMock(return_value=[])
        with patch.object(mcp_server.queries, "get_user_profile_layers", mock_layers), \
             patch.object(mcp_server.queries, "get_procedural_memories", mock_proc):
            await fn(static_limit=0, recent_limit=0, rule_limit=0)
        kwargs = mock_layers.call_args.kwargs
        assert kwargs["static_limit"] == 0
        assert kwargs["recent_limit"] == 0
        assert mock_proc.call_args.kwargs["limit"] == 0

    @pytest.mark.asyncio
    async def test_negative_recent_window_clamped_to_min_1(self):
        fn = _unwrap(mcp_server.memory_get_user_profile)
        mock_layers = AsyncMock(return_value={"static": [], "recent": []})
        mock_proc = AsyncMock(return_value=[])
        with patch.object(mcp_server.queries, "get_user_profile_layers", mock_layers), \
             patch.object(mcp_server.queries, "get_procedural_memories", mock_proc):
            await fn(recent_window_days=-5)
        assert mock_layers.call_args.kwargs["recent_window_days"] == 1

    @pytest.mark.asyncio
    async def test_default_params(self):
        """Defaults: static=20, recent=15, rule=30, window=7, floor=0.75."""
        fn = _unwrap(mcp_server.memory_get_user_profile)
        mock_layers = AsyncMock(return_value={"static": [], "recent": []})
        mock_proc = AsyncMock(return_value=[])
        with patch.object(mcp_server.queries, "get_user_profile_layers", mock_layers), \
             patch.object(mcp_server.queries, "get_procedural_memories", mock_proc):
            await fn()
        kwargs = mock_layers.call_args.kwargs
        assert kwargs["static_limit"] == 20
        assert kwargs["recent_limit"] == 15
        assert kwargs["recent_window_days"] == 7
        assert kwargs["static_importance_floor"] == 0.75
        assert mock_proc.call_args.kwargs["limit"] == 30
