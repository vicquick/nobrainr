"""Procedural memory tools (Phase C G4, v6.8).

G4 adds Letta + LangGraph-inspired procedural memory — agent-writable
rules and instructions that affect future behavior. Retrieved by scope,
not similarity. Separate from the memories table so rules never compete
with facts in search results.

Three MCP tools are added:
  - memory_store_procedural
  - memory_get_procedural
  - memory_delete_procedural

Plus the DB layer in ``queries.store_procedural_memory`` /
``queries.get_procedural_memories`` / ``queries.delete_procedural_memory``.

These tests exercise the validation logic (scope + id consistency,
priority bounds), the scope-merging query builder (global + specific
id), the soft-delete path, and the ISO datetime parser.
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


# ──────────────────────────────────────────────
# store_procedural_memory — validation
# ──────────────────────────────────────────────


class TestStoreProceduralValidation:
    """Validation runs before any DB interaction, so we don't need to mock
    the pool — invalid inputs raise ValueError immediately."""

    @pytest.mark.asyncio
    async def test_invalid_scope_rejected(self):
        with pytest.raises(ValueError, match="scope must be one of"):
            await db_queries.store_procedural_memory(
                "rule", scope="bogus"
            )

    @pytest.mark.asyncio
    async def test_agent_scope_requires_agent_id(self):
        with pytest.raises(ValueError, match="agent_id is required"):
            await db_queries.store_procedural_memory(
                "rule", scope="agent"
            )

    @pytest.mark.asyncio
    async def test_project_scope_requires_project_id(self):
        with pytest.raises(ValueError, match="project_id is required"):
            await db_queries.store_procedural_memory(
                "rule", scope="project"
            )

    @pytest.mark.asyncio
    async def test_session_scope_requires_session_id(self):
        with pytest.raises(ValueError, match="session_id is required"):
            await db_queries.store_procedural_memory(
                "rule", scope="session"
            )

    @pytest.mark.asyncio
    async def test_priority_below_zero_rejected(self):
        with pytest.raises(ValueError, match=r"priority must be in \[0, 100\]"):
            await db_queries.store_procedural_memory(
                "rule", scope="global", priority=-1
            )

    @pytest.mark.asyncio
    async def test_priority_above_100_rejected(self):
        with pytest.raises(ValueError, match=r"priority must be in \[0, 100\]"):
            await db_queries.store_procedural_memory(
                "rule", scope="global", priority=101
            )

    @pytest.mark.asyncio
    async def test_global_scope_needs_no_id(self):
        """Global rules don't require any id — they apply everywhere."""
        captured = {}

        async def fake_fetchrow(sql, *args):
            captured["sql"] = sql
            captured["args"] = args
            # Minimal row dict that _row_to_dict can handle
            return {
                "id": "11111111-1111-1111-1111-111111111111",
                "content": args[0],
                "title": args[1],
                "scope": args[2],
                "agent_id": args[3],
                "project_id": args[4],
                "session_id": args[5],
                "priority": args[6],
                "active": True,
                "tags": args[7],
                "metadata": "{}",
                "created_at": datetime(2026, 4, 12, tzinfo=timezone.utc),
                "updated_at": datetime(2026, 4, 12, tzinfo=timezone.utc),
                "expires_at": args[9],
            }

        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(side_effect=fake_fetchrow)
        acquire_ctx = MagicMock()
        acquire_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
        acquire_ctx.__aexit__ = AsyncMock(return_value=None)
        mock_pool = MagicMock()
        mock_pool.acquire = MagicMock(return_value=acquire_ctx)

        with patch.object(db_queries, "get_pool", AsyncMock(return_value=mock_pool)):
            result = await db_queries.store_procedural_memory(
                "always run tests before committing",
                scope="global",
            )

        assert result["scope"] == "global"
        assert result["content"] == "always run tests before committing"
        assert captured["args"][2] == "global"
        assert captured["args"][3] is None  # agent_id
        assert captured["args"][4] is None  # project_id


# ──────────────────────────────────────────────
# get_procedural_memories — scope merging
# ──────────────────────────────────────────────


class TestGetProceduralScopeMerging:
    """Verify the SQL WHERE clause is built correctly under each scope
    combination. We capture the SQL + params and assert the composition."""

    async def _capture_fetch(self, **kwargs):
        captured = {}

        async def fake_fetch(sql, *args):
            captured["sql"] = sql
            captured["args"] = args
            return []

        mock_conn = AsyncMock()
        mock_conn.fetch = AsyncMock(side_effect=fake_fetch)
        acquire_ctx = MagicMock()
        acquire_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
        acquire_ctx.__aexit__ = AsyncMock(return_value=None)
        mock_pool = MagicMock()
        mock_pool.acquire = MagicMock(return_value=acquire_ctx)

        with patch.object(db_queries, "get_pool", AsyncMock(return_value=mock_pool)):
            await db_queries.get_procedural_memories(**kwargs)
        return captured

    @pytest.mark.asyncio
    async def test_no_filter_returns_all_active(self):
        """No scope, no ids → all active non-expired rules."""
        c = await self._capture_fetch()
        assert "active = true" in c["sql"]
        assert "expires_at IS NULL OR expires_at > now()" in c["sql"]
        # No scope clause
        assert "scope = 'global' OR" not in c["sql"]
        assert "scope = $" not in c["sql"]

    @pytest.mark.asyncio
    async def test_agent_id_merges_with_global(self):
        """agent_id only → global + agent rules for that agent."""
        c = await self._capture_fetch(agent_id="session-log-agent")
        assert "scope = 'global'" in c["sql"]
        assert "scope = 'agent' AND agent_id =" in c["sql"]
        assert "session-log-agent" in c["args"]

    @pytest.mark.asyncio
    async def test_project_id_merges_with_global(self):
        c = await self._capture_fetch(project_id="nobrainr")
        assert "scope = 'global'" in c["sql"]
        assert "scope = 'project' AND project_id =" in c["sql"]
        assert "nobrainr" in c["args"]

    @pytest.mark.asyncio
    async def test_session_id_merges_with_global(self):
        c = await self._capture_fetch(session_id="sess-abc")
        assert "scope = 'global'" in c["sql"]
        assert "scope = 'session' AND session_id =" in c["sql"]
        assert "sess-abc" in c["args"]

    @pytest.mark.asyncio
    async def test_agent_plus_project_merge(self):
        """Both agent_id AND project_id → global + agent + project."""
        c = await self._capture_fetch(
            agent_id="claude",
            project_id="nobrainr",
        )
        assert "scope = 'agent' AND agent_id =" in c["sql"]
        assert "scope = 'project' AND project_id =" in c["sql"]

    @pytest.mark.asyncio
    async def test_explicit_scope_overrides_merge(self):
        """Explicit scope='global' + agent_id → just global, no merge."""
        c = await self._capture_fetch(scope="global", agent_id="ignored")
        assert "scope = $" in c["sql"], "explicit scope should be a direct filter"
        # agent_id should NOT have been used (merge is suppressed)
        assert "ignored" not in c["args"]

    @pytest.mark.asyncio
    async def test_include_expired_skips_expiry_filter(self):
        c = await self._capture_fetch(include_expired=True)
        assert "active = true" in c["sql"]
        assert "expires_at IS NULL OR expires_at > now()" not in c["sql"]

    @pytest.mark.asyncio
    async def test_include_inactive_skips_active_filter(self):
        c = await self._capture_fetch(include_inactive=True)
        assert "active = true" not in c["sql"]

    @pytest.mark.asyncio
    async def test_priority_desc_ordering(self):
        c = await self._capture_fetch()
        assert "ORDER BY priority DESC, created_at DESC" in c["sql"], \
            "rules must come back with highest priority first"

    @pytest.mark.asyncio
    async def test_invalid_scope_rejected(self):
        with pytest.raises(ValueError, match="scope must be one of"):
            await db_queries.get_procedural_memories(scope="bogus")


# ──────────────────────────────────────────────
# delete_procedural_memory
# ──────────────────────────────────────────────


class TestDeleteProcedural:
    """Soft vs hard delete."""

    async def _capture_execute(self, memory_id: str, hard: bool):
        captured = {}

        async def fake_execute(sql, *args):
            captured["sql"] = sql
            captured["args"] = args
            return "UPDATE 1" if "UPDATE" in sql else "DELETE 1"

        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock(side_effect=fake_execute)
        acquire_ctx = MagicMock()
        acquire_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
        acquire_ctx.__aexit__ = AsyncMock(return_value=None)
        mock_pool = MagicMock()
        mock_pool.acquire = MagicMock(return_value=acquire_ctx)

        with patch.object(db_queries, "get_pool", AsyncMock(return_value=mock_pool)):
            ok = await db_queries.delete_procedural_memory(memory_id, hard=hard)
        return captured, ok

    @pytest.mark.asyncio
    async def test_soft_delete_sets_active_false(self):
        c, ok = await self._capture_execute(
            "11111111-1111-1111-1111-111111111111", hard=False
        )
        assert ok is True
        assert "UPDATE procedural_memories" in c["sql"]
        assert "active = false" in c["sql"]
        assert "DELETE" not in c["sql"]

    @pytest.mark.asyncio
    async def test_hard_delete_removes_row(self):
        c, ok = await self._capture_execute(
            "11111111-1111-1111-1111-111111111111", hard=True
        )
        assert ok is True
        assert "DELETE FROM procedural_memories" in c["sql"]
        assert "UPDATE" not in c["sql"]

    @pytest.mark.asyncio
    async def test_invalid_uuid_returns_false(self):
        """Garbage UUIDs shouldn't hit the DB at all — just return False."""
        ok = await db_queries.delete_procedural_memory("not-a-uuid")
        assert ok is False


# ──────────────────────────────────────────────
# MCP tool layer
# ──────────────────────────────────────────────


class TestMCPToolLayer:
    """Verify the MCP wrappers parse datetimes + catch validation errors +
    delegate to the DB layer."""

    def test_parse_iso_datetime_plain_date(self):
        dt = mcp_server._parse_iso_datetime("2026-04-12")
        assert dt is not None
        assert dt.year == 2026
        assert dt.month == 4
        assert dt.day == 12

    def test_parse_iso_datetime_with_z(self):
        dt = mcp_server._parse_iso_datetime("2026-04-12T09:55:00Z")
        assert dt is not None
        assert dt.tzinfo is not None

    def test_parse_iso_datetime_with_offset(self):
        dt = mcp_server._parse_iso_datetime("2026-04-12T09:55:00+02:00")
        assert dt is not None

    def test_parse_iso_datetime_garbage_returns_none(self):
        assert mcp_server._parse_iso_datetime("not a date") is None
        assert mcp_server._parse_iso_datetime("") is None
        assert mcp_server._parse_iso_datetime(None) is None

    @pytest.mark.asyncio
    async def test_store_tool_returns_error_on_invalid_scope(self):
        """MCP layer should wrap ValueError into an error dict so the tool
        call doesn't blow up the MCP client."""
        fn = _unwrap(mcp_server.memory_store_procedural)
        result = await fn(content="rule", scope="bogus")
        assert "error" in result
        assert "scope must be one of" in result["error"]

    @pytest.mark.asyncio
    async def test_store_tool_happy_path_delegates(self):
        """Valid input → call reaches queries.store_procedural_memory."""
        fn = _unwrap(mcp_server.memory_store_procedural)
        mock_store = AsyncMock(return_value={"id": "stub", "scope": "global"})
        with patch.object(mcp_server.queries, "store_procedural_memory", mock_store):
            result = await fn(content="rule", scope="global", priority=75)
        assert result == {"id": "stub", "scope": "global"}
        mock_store.assert_called_once()
        kwargs = mock_store.call_args.kwargs
        assert kwargs["content"] == "rule"
        assert kwargs["scope"] == "global"
        assert kwargs["priority"] == 75
        assert kwargs["expires_at"] is None  # parsed from None

    @pytest.mark.asyncio
    async def test_store_tool_parses_expires_at(self):
        """String expires_at → parsed datetime reaches the DB layer."""
        fn = _unwrap(mcp_server.memory_store_procedural)
        mock_store = AsyncMock(return_value={"id": "stub"})
        with patch.object(mcp_server.queries, "store_procedural_memory", mock_store):
            await fn(
                content="rule",
                scope="session",
                session_id="sess-1",
                expires_at="2026-04-12T12:00:00Z",
            )
        kwargs = mock_store.call_args.kwargs
        assert kwargs["expires_at"] is not None
        assert kwargs["expires_at"].year == 2026

    @pytest.mark.asyncio
    async def test_get_tool_delegates(self):
        fn = _unwrap(mcp_server.memory_get_procedural)
        mock_get = AsyncMock(return_value=[{"id": "a"}, {"id": "b"}])
        with patch.object(mcp_server.queries, "get_procedural_memories", mock_get):
            result = await fn(agent_id="claude")
        assert len(result) == 2
        mock_get.assert_called_once()
        kwargs = mock_get.call_args.kwargs
        assert kwargs["agent_id"] == "claude"

    @pytest.mark.asyncio
    async def test_get_tool_returns_error_on_invalid_scope(self):
        fn = _unwrap(mcp_server.memory_get_procedural)
        result = await fn(scope="bogus")
        assert len(result) == 1
        assert "error" in result[0]

    @pytest.mark.asyncio
    async def test_delete_tool_soft_by_default(self):
        fn = _unwrap(mcp_server.memory_delete_procedural)
        mock_del = AsyncMock(return_value=True)
        with patch.object(mcp_server.queries, "delete_procedural_memory", mock_del):
            result = await fn(memory_id="11111111-1111-1111-1111-111111111111")
        assert result["status"] == "deactivated"
        assert mock_del.call_args.kwargs["hard"] is False

    @pytest.mark.asyncio
    async def test_delete_tool_hard_flag(self):
        fn = _unwrap(mcp_server.memory_delete_procedural)
        mock_del = AsyncMock(return_value=True)
        with patch.object(mcp_server.queries, "delete_procedural_memory", mock_del):
            result = await fn(
                memory_id="11111111-1111-1111-1111-111111111111",
                hard=True,
            )
        assert result["status"] == "deleted"
        assert mock_del.call_args.kwargs["hard"] is True

    @pytest.mark.asyncio
    async def test_delete_tool_not_found(self):
        fn = _unwrap(mcp_server.memory_delete_procedural)
        mock_del = AsyncMock(return_value=False)
        with patch.object(mcp_server.queries, "delete_procedural_memory", mock_del):
            result = await fn(memory_id="11111111-1111-1111-1111-111111111111")
        assert result["status"] == "not_found"
