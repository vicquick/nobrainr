"""Bi-temporal validity windows on memory_facts (Phase K, v6.15).

Phase K adds ``valid_from`` and ``valid_to`` columns to ``memory_facts``,
plus:
  - ``search_facts`` filters to ``valid_to IS NULL`` by default
  - ``search_facts`` accepts ``date_asof`` for point-in-time queries
  - ``supersede_fact`` function to soft-supersede a fact + optionally
    insert a replacement atomically
  - ``fact_search`` MCP tool exposes the ``date_asof`` param with ISO
    parsing

Zep / Graphiti pattern: soft-delete via valid_to timestamp means the
superseded facts stay in the table for audit + point-in-time queries,
without polluting normal current-state search results.

Tests cover the SQL shape, the default behavior change (currently-valid
only), the MCP tool's ISO parsing, and the supersede flow.
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
# search_facts — SQL shape
# ──────────────────────────────────────────────


async def _capture_search_facts_sql(date_asof=None, text_query=None):
    captured: dict = {"queries": []}

    async def fake_fetchval(sql, *args):
        # existence check
        return True

    async def fake_fetch(sql, *args):
        captured["queries"].append({"sql": sql, "args": args})
        return []

    mock_conn = AsyncMock()
    mock_conn.fetchval = AsyncMock(side_effect=fake_fetchval)
    mock_conn.fetch = AsyncMock(side_effect=fake_fetch)
    acquire_ctx = MagicMock()
    acquire_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
    acquire_ctx.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquire_ctx)

    with patch.object(db_queries, "get_pool", AsyncMock(return_value=pool)):
        await db_queries.search_facts(
            embedding=[0.1] * 1024,
            date_asof=date_asof,
            text_query=text_query,
        )
    return captured["queries"]


class TestSearchFactsSQL:
    @pytest.mark.asyncio
    async def test_default_filters_to_currently_valid_only(self):
        """Without date_asof: WHERE valid_to IS NULL — superseded facts hidden."""
        queries = await _capture_search_facts_sql()
        assert queries, "search_facts should have executed at least one query"
        sql = queries[0]["sql"]
        assert "valid_to IS NULL" in sql

    @pytest.mark.asyncio
    async def test_date_asof_uses_bi_temporal_bounds(self):
        """With date_asof: WHERE valid_from <= $N AND (valid_to IS NULL OR valid_to > $N)."""
        asof = datetime(2026, 3, 1, tzinfo=timezone.utc)
        queries = await _capture_search_facts_sql(date_asof=asof)
        sql = queries[0]["sql"]
        assert "valid_from <= $" in sql
        assert "valid_to IS NULL OR f.valid_to > $" in sql
        # The date itself is in the params
        assert asof in queries[0]["args"]

    @pytest.mark.asyncio
    async def test_date_asof_both_bounds_reference_same_value(self):
        """valid_from <= X and valid_to > X must reference the SAME X."""
        asof = datetime(2026, 2, 15, tzinfo=timezone.utc)
        queries = await _capture_search_facts_sql(date_asof=asof)
        args = queries[0]["args"]
        # asof appears at least twice (once per bound)
        assert args.count(asof) == 2

    @pytest.mark.asyncio
    async def test_text_query_path_also_has_temporal_filter(self):
        """The hybrid text+vector path must carry the temporal filter."""
        queries = await _capture_search_facts_sql(text_query="stale")
        sql = queries[0]["sql"]
        assert "valid_to IS NULL" in sql
        # Text query clauses also present
        assert "nb_unaccent(f.content)" in sql

    @pytest.mark.asyncio
    async def test_select_returns_validity_columns(self):
        """The SELECT must include valid_from and valid_to so callers can
        see the validity window on returned rows."""
        queries = await _capture_search_facts_sql()
        sql = queries[0]["sql"]
        assert "f.valid_from" in sql
        assert "f.valid_to" in sql


# ──────────────────────────────────────────────
# supersede_fact — SQL + transaction
# ──────────────────────────────────────────────


class TestSupersedeFact:
    @pytest.mark.asyncio
    async def test_invalid_uuid_returns_none_without_db(self):
        """Garbage UUID must fail fast — no pool, no connection attempt."""
        result = await db_queries.supersede_fact("not-a-uuid")
        assert result is None

    @pytest.mark.asyncio
    async def test_sets_valid_to_on_existing_fact(self):
        captured: dict = {"executes": []}

        async def fake_fetchrow(sql, *args):
            # First call: SELECT ... FOR UPDATE — return existing fact
            if "FOR UPDATE" in sql:
                return {
                    "id": args[0],
                    "memory_id": "00000000-0000-0000-0000-000000000000",
                    "content": "old content",
                }
            # Second call: RETURNING id from INSERT
            return {"id": "99999999-9999-9999-9999-999999999999"}

        async def fake_execute(sql, *args):
            captured["executes"].append({"sql": sql, "args": args})
            return "UPDATE 1"

        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(side_effect=fake_fetchrow)
        mock_conn.execute = AsyncMock(side_effect=fake_execute)

        tx_ctx = MagicMock()
        tx_ctx.__aenter__ = AsyncMock(return_value=None)
        tx_ctx.__aexit__ = AsyncMock(return_value=None)
        mock_conn.transaction = MagicMock(return_value=tx_ctx)

        acquire_ctx = MagicMock()
        acquire_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
        acquire_ctx.__aexit__ = AsyncMock(return_value=None)
        pool = MagicMock()
        pool.acquire = MagicMock(return_value=acquire_ctx)

        with patch.object(db_queries, "get_pool", AsyncMock(return_value=pool)):
            result = await db_queries.supersede_fact(
                "11111111-1111-1111-1111-111111111111"
            )

        # The UPDATE to set valid_to=now() was issued
        update_sql = captured["executes"][0]["sql"]
        assert "UPDATE memory_facts" in update_sql
        assert "valid_to = now()" in update_sql

        assert result is not None
        assert result["superseded_id"] == "11111111-1111-1111-1111-111111111111"
        assert result["new_id"] is None  # no replacement requested

    @pytest.mark.asyncio
    async def test_inserts_replacement_when_new_content_provided(self):
        captured: dict = {"fetchrows": [], "executes": []}

        async def fake_fetchrow(sql, *args):
            captured["fetchrows"].append(sql)
            if "FOR UPDATE" in sql:
                return {
                    "id": args[0],
                    "memory_id": "00000000-0000-0000-0000-000000000000",
                    "content": "old content",
                }
            if "INSERT INTO memory_facts" in sql:
                return {"id": "99999999-9999-9999-9999-999999999999"}
            return None

        async def fake_execute(sql, *args):
            captured["executes"].append(sql)
            return "UPDATE 1"

        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(side_effect=fake_fetchrow)
        mock_conn.execute = AsyncMock(side_effect=fake_execute)
        tx_ctx = MagicMock()
        tx_ctx.__aenter__ = AsyncMock(return_value=None)
        tx_ctx.__aexit__ = AsyncMock(return_value=None)
        mock_conn.transaction = MagicMock(return_value=tx_ctx)
        acquire_ctx = MagicMock()
        acquire_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
        acquire_ctx.__aexit__ = AsyncMock(return_value=None)
        pool = MagicMock()
        pool.acquire = MagicMock(return_value=acquire_ctx)

        with patch.object(db_queries, "get_pool", AsyncMock(return_value=pool)):
            result = await db_queries.supersede_fact(
                "11111111-1111-1111-1111-111111111111",
                new_content="replacement content",
                reason="corrected",
            )

        # Both the UPDATE (old) and INSERT (new) happened
        assert any("UPDATE memory_facts" in s for s in captured["executes"])
        assert any("INSERT INTO memory_facts" in s for s in captured["fetchrows"])
        assert result["superseded_id"] == "11111111-1111-1111-1111-111111111111"
        assert result["new_id"] == "99999999-9999-9999-9999-999999999999"
        assert result["reason"] == "corrected"

    @pytest.mark.asyncio
    async def test_returns_none_for_already_superseded_fact(self):
        """If the old fact is not found (already superseded, valid_to != NULL),
        SELECT FOR UPDATE returns None → we return None."""
        async def fake_fetchrow(sql, *args):
            return None  # fact not found or already superseded

        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(side_effect=fake_fetchrow)
        mock_conn.execute = AsyncMock(return_value="UPDATE 0")
        tx_ctx = MagicMock()
        tx_ctx.__aenter__ = AsyncMock(return_value=None)
        tx_ctx.__aexit__ = AsyncMock(return_value=None)
        mock_conn.transaction = MagicMock(return_value=tx_ctx)
        acquire_ctx = MagicMock()
        acquire_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
        acquire_ctx.__aexit__ = AsyncMock(return_value=None)
        pool = MagicMock()
        pool.acquire = MagicMock(return_value=acquire_ctx)

        with patch.object(db_queries, "get_pool", AsyncMock(return_value=pool)):
            result = await db_queries.supersede_fact(
                "11111111-1111-1111-1111-111111111111"
            )
        assert result is None

    @pytest.mark.asyncio
    async def test_select_query_filters_to_currently_valid_only(self):
        """The SELECT FOR UPDATE must include valid_to IS NULL so we don't
        double-supersede an already-superseded fact."""
        captured = {"sql": None}

        async def fake_fetchrow(sql, *args):
            captured["sql"] = sql
            return None

        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(side_effect=fake_fetchrow)
        mock_conn.execute = AsyncMock(return_value="UPDATE 0")
        tx_ctx = MagicMock()
        tx_ctx.__aenter__ = AsyncMock(return_value=None)
        tx_ctx.__aexit__ = AsyncMock(return_value=None)
        mock_conn.transaction = MagicMock(return_value=tx_ctx)
        acquire_ctx = MagicMock()
        acquire_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
        acquire_ctx.__aexit__ = AsyncMock(return_value=None)
        pool = MagicMock()
        pool.acquire = MagicMock(return_value=acquire_ctx)

        with patch.object(db_queries, "get_pool", AsyncMock(return_value=pool)):
            await db_queries.supersede_fact(
                "11111111-1111-1111-1111-111111111111"
            )
        assert "valid_to IS NULL" in captured["sql"]
        assert "FOR UPDATE" in captured["sql"]


# ──────────────────────────────────────────────
# fact_search MCP tool — ISO date parsing + passthrough
# ──────────────────────────────────────────────


class TestFactSearchMCP:
    @pytest.mark.asyncio
    async def test_iso_date_parsed_and_passed_through(self):
        fn = _unwrap(mcp_server.fact_search)
        mock_embed = AsyncMock(return_value=[0.1] * 1024)
        mock_search = AsyncMock(return_value=[])
        with patch.object(mcp_server, "embed_text", mock_embed), \
             patch.object(mcp_server.queries, "search_facts", mock_search):
            result = await fn(query="test", date_asof="2026-03-01T12:00:00Z")
        # Parsed asof lands in search_facts kwargs
        kw = mock_search.call_args.kwargs
        assert kw["date_asof"] is not None
        assert kw["date_asof"].year == 2026
        assert kw["date_asof"].month == 3
        # Response echoes the parsed date for caller confirmation
        assert "2026-03-01" in (result["date_asof"] or "")

    @pytest.mark.asyncio
    async def test_garbage_date_degrades_to_none(self):
        """Unparseable date_asof should NOT error — default to current-valid."""
        fn = _unwrap(mcp_server.fact_search)
        mock_embed = AsyncMock(return_value=[0.1] * 1024)
        mock_search = AsyncMock(return_value=[])
        with patch.object(mcp_server, "embed_text", mock_embed), \
             patch.object(mcp_server.queries, "search_facts", mock_search):
            result = await fn(query="test", date_asof="not a date")
        assert mock_search.call_args.kwargs["date_asof"] is None
        assert result["date_asof"] is None

    @pytest.mark.asyncio
    async def test_none_date_passes_through(self):
        fn = _unwrap(mcp_server.fact_search)
        mock_embed = AsyncMock(return_value=[0.1] * 1024)
        mock_search = AsyncMock(return_value=[])
        with patch.object(mcp_server, "embed_text", mock_embed), \
             patch.object(mcp_server.queries, "search_facts", mock_search):
            await fn(query="test")
        assert mock_search.call_args.kwargs["date_asof"] is None

    @pytest.mark.asyncio
    async def test_plain_date_parsed(self):
        """'2026-03-01' (no time) should also parse."""
        fn = _unwrap(mcp_server.fact_search)
        mock_embed = AsyncMock(return_value=[0.1] * 1024)
        mock_search = AsyncMock(return_value=[])
        with patch.object(mcp_server, "embed_text", mock_embed), \
             patch.object(mcp_server.queries, "search_facts", mock_search):
            await fn(query="test", date_asof="2026-03-01")
        assert mock_search.call_args.kwargs["date_asof"] is not None
