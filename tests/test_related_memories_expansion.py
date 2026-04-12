"""Related-memories expansion on memory_search (Phase Q, v6.16).

Phase Q adds ``include_related: bool = False`` to ``memory_search``.
When True, after the main search + rerank pipeline, a single batched
SQL query attaches a ``related_memories`` field to each result listing
top-3 memories that share at least one entity via ``entity_memories``.

Tests cover:
  1. ``get_related_memories_batch`` SQL shape (window function ordering,
     self-reference filter, entity-memory join, uuid[] param)
  2. Invalid UUIDs in input are silently dropped (no crash)
  3. Empty input → empty result dict
  4. Result dict groups related memories by source_id correctly
  5. Top-N respected (``limit_per_memory``)
  6. ``memory_search(include_related=True)`` attaches the field
  7. ``include_related=False`` (default) does NOT attach the field
  8. Exception in batch fetch gracefully falls back to empty related
     (never breaks the main search)
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
# get_related_memories_batch — SQL shape
# ──────────────────────────────────────────────


async def _capture_fetch(return_rows=None):
    captured: dict = {}

    async def fake_fetch(sql, *args):
        captured["sql"] = sql
        captured["args"] = args
        return return_rows or []

    mock_conn = AsyncMock()
    mock_conn.fetch = AsyncMock(side_effect=fake_fetch)
    acquire_ctx = MagicMock()
    acquire_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
    acquire_ctx.__aexit__ = AsyncMock(return_value=None)
    mock_pool = MagicMock()
    mock_pool.acquire = MagicMock(return_value=acquire_ctx)
    return mock_pool, captured


class TestGetRelatedMemoriesBatchSQL:
    @pytest.mark.asyncio
    async def test_sql_uses_window_function_and_rank_filter(self):
        pool, cap = await _capture_fetch()
        with patch.object(db_queries, "get_pool", AsyncMock(return_value=pool)):
            await db_queries.get_related_memories_batch(
                ["11111111-1111-1111-1111-111111111111"],
                limit_per_memory=3,
            )
        sql = cap["sql"]
        assert "ROW_NUMBER() OVER (" in sql
        assert "PARTITION BY source_id" in sql
        # The ranker now uses shared_entity_count first, then importance,
        # then created_at. shared_entity_count is the correct primary
        # signal (more shared entities = more related) and was the fix
        # for the duplicate-row bug observed in the 2026-04-12 dry-run.
        assert "shared_entity_count DESC" in sql
        assert "importance DESC" in sql
        assert "WHERE rank <= $2" in sql

    @pytest.mark.asyncio
    async def test_sql_dedupes_via_group_by(self):
        """Regression guard: without GROUP BY, a memory that shares
        multiple entities with a source would appear in the join multiple
        times and get multiple ranks — a real bug observed in the live
        dry-run. The GROUP BY is what dedupes (source_id, related_id)."""
        pool, cap = await _capture_fetch()
        with patch.object(db_queries, "get_pool", AsyncMock(return_value=pool)):
            await db_queries.get_related_memories_batch(
                ["11111111-1111-1111-1111-111111111111"]
            )
        sql = cap["sql"]
        assert "GROUP BY" in sql
        assert "count(*) AS shared_entity_count" in sql

    @pytest.mark.asyncio
    async def test_sql_filters_self_references(self):
        """A memory must NEVER be its own related memory — the join
        must exclude ``m.id = se.source_id``."""
        pool, cap = await _capture_fetch()
        with patch.object(db_queries, "get_pool", AsyncMock(return_value=pool)):
            await db_queries.get_related_memories_batch(
                ["11111111-1111-1111-1111-111111111111"]
            )
        assert "m.id != se.source_id" in cap["sql"]

    @pytest.mark.asyncio
    async def test_sql_uses_uuid_array_param(self):
        """The input list is passed as a single uuid[] array parameter
        so the query is a single round-trip regardless of batch size."""
        pool, cap = await _capture_fetch()
        with patch.object(db_queries, "get_pool", AsyncMock(return_value=pool)):
            await db_queries.get_related_memories_batch(
                [
                    "11111111-1111-1111-1111-111111111111",
                    "22222222-2222-2222-2222-222222222222",
                ]
            )
        sql = cap["sql"]
        assert "ANY($1::uuid[])" in sql
        # First positional arg should be the list of UUID objects
        assert len(cap["args"][0]) == 2

    @pytest.mark.asyncio
    async def test_sql_joins_entity_memories_twice(self):
        """Once to fetch the source memory's entities, again to find
        other memories sharing those entities."""
        pool, cap = await _capture_fetch()
        with patch.object(db_queries, "get_pool", AsyncMock(return_value=pool)):
            await db_queries.get_related_memories_batch(
                ["11111111-1111-1111-1111-111111111111"]
            )
        sql = cap["sql"]
        # Two separate references to entity_memories (aliases em and em2)
        assert sql.count("entity_memories") >= 2

    @pytest.mark.asyncio
    async def test_only_returns_embedded_memories(self):
        """Related memories must have embedding — prevents surfacing
        memories that haven't been processed yet."""
        pool, cap = await _capture_fetch()
        with patch.object(db_queries, "get_pool", AsyncMock(return_value=pool)):
            await db_queries.get_related_memories_batch(
                ["11111111-1111-1111-1111-111111111111"]
            )
        assert "m.embedding IS NOT NULL" in cap["sql"]


# ──────────────────────────────────────────────
# Input validation + empty cases
# ──────────────────────────────────────────────


class TestGetRelatedMemoriesBatchInputHandling:
    @pytest.mark.asyncio
    async def test_empty_input_returns_empty_dict_no_pool_call(self):
        """Empty list → empty dict, no DB round-trip."""
        with patch.object(db_queries, "get_pool", AsyncMock(side_effect=RuntimeError("should not be called"))):
            result = await db_queries.get_related_memories_batch([])
        assert result == {}

    @pytest.mark.asyncio
    async def test_all_invalid_uuids_returns_empty_dict(self):
        """All garbage UUIDs → empty dict without touching the pool."""
        with patch.object(db_queries, "get_pool", AsyncMock(side_effect=RuntimeError("should not be called"))):
            result = await db_queries.get_related_memories_batch(
                ["garbage-1", "garbage-2", "not-a-uuid"]
            )
        assert result == {}

    @pytest.mark.asyncio
    async def test_mixed_valid_and_invalid_uuids(self):
        """Invalid UUIDs silently dropped, valid ones processed."""
        pool, cap = await _capture_fetch()
        with patch.object(db_queries, "get_pool", AsyncMock(return_value=pool)):
            result = await db_queries.get_related_memories_batch(
                [
                    "not-a-uuid",
                    "11111111-1111-1111-1111-111111111111",
                    "garbage",
                ]
            )
        # Valid UUIDs in the result dict + passed to the SQL
        assert "11111111-1111-1111-1111-111111111111" in result
        assert len(cap["args"][0]) == 1  # Only one valid UUID reached the query


# ──────────────────────────────────────────────
# Result grouping
# ──────────────────────────────────────────────


def _related_row(source_id, related_id, importance=0.5, shared=1):
    return {
        "source_id": source_id,
        "related_id": related_id,
        "content": f"Related content for {related_id}",
        "summary": f"Related summary for {related_id}",
        "category": "insight",
        "tags": ["related"],
        "importance": importance,
        "created_at": datetime(2026, 4, 12, tzinfo=timezone.utc),
        "shared_entity_count": shared,
    }


class TestGetRelatedMemoriesBatchResults:
    @pytest.mark.asyncio
    async def test_groups_by_source_id(self):
        """The result dict maps source_id → list of related memories."""
        import uuid as _uuid
        s1 = _uuid.UUID("11111111-1111-1111-1111-111111111111")
        s2 = _uuid.UUID("22222222-2222-2222-2222-222222222222")
        r1 = _uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
        r2 = _uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
        r3 = _uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")

        pool, _ = await _capture_fetch(return_rows=[
            _related_row(s1, r1, importance=0.9),
            _related_row(s1, r2, importance=0.8),
            _related_row(s2, r3, importance=0.7),
        ])
        with patch.object(db_queries, "get_pool", AsyncMock(return_value=pool)):
            result = await db_queries.get_related_memories_batch(
                [str(s1), str(s2)]
            )

        assert str(s1) in result
        assert str(s2) in result
        assert len(result[str(s1)]) == 2
        assert len(result[str(s2)]) == 1
        # Each related memory has the expected fields
        assert result[str(s1)][0]["id"] == str(r1)
        assert result[str(s1)][0]["importance"] == 0.9

    @pytest.mark.asyncio
    async def test_missing_source_returns_empty_list(self):
        """If a source has no related memories, the key still exists
        in the dict with an empty list (simpler for callers)."""
        import uuid as _uuid
        s1 = _uuid.UUID("11111111-1111-1111-1111-111111111111")
        # No rows returned from the query
        pool, _ = await _capture_fetch(return_rows=[])
        with patch.object(db_queries, "get_pool", AsyncMock(return_value=pool)):
            result = await db_queries.get_related_memories_batch([str(s1)])
        assert result[str(s1)] == []


# ──────────────────────────────────────────────
# memory_search include_related integration
# ──────────────────────────────────────────────


class TestMemorySearchIncludeRelated:
    @pytest.mark.asyncio
    async def test_include_related_false_no_field_attached(self):
        """Default behavior: results do NOT have a related_memories field."""
        fn = _unwrap(mcp_server.memory_search)
        fake_results = [{"id": "mem-1", "content": "x"}, {"id": "mem-2", "content": "y"}]

        async def fake_search(**kwargs):
            return fake_results

        async def fake_embed_batch(queries):
            return [[0.1] * 1024 for _ in queries]

        with patch.object(mcp_server.queries, "search_memories", side_effect=fake_search), \
             patch("nobrainr.embeddings.ollama.embed_batch", side_effect=fake_embed_batch), \
             patch.object(mcp_server.queries, "expand_chunk_context", AsyncMock(return_value=fake_results)), \
             patch.object(mcp_server.queries, "record_interest_signal", AsyncMock(return_value=None)), \
             patch.object(mcp_server.queries, "get_related_memories_batch", AsyncMock(return_value={})):
            results = await fn(query="test", include_related=False)
        for r in results:
            assert "related_memories" not in r

    @pytest.mark.asyncio
    async def test_include_related_true_attaches_field(self):
        """include_related=True → every result has a related_memories list."""
        fn = _unwrap(mcp_server.memory_search)
        fake_results = [{"id": "mem-1", "content": "x"}, {"id": "mem-2", "content": "y"}]

        async def fake_search(**kwargs):
            return fake_results

        async def fake_embed_batch(queries):
            return [[0.1] * 1024 for _ in queries]

        related_map = {
            "mem-1": [{"id": "rel-a"}, {"id": "rel-b"}],
            "mem-2": [{"id": "rel-c"}],
        }

        with patch.object(mcp_server.queries, "search_memories", side_effect=fake_search), \
             patch("nobrainr.embeddings.ollama.embed_batch", side_effect=fake_embed_batch), \
             patch.object(mcp_server.queries, "expand_chunk_context", AsyncMock(return_value=fake_results)), \
             patch.object(mcp_server.queries, "record_interest_signal", AsyncMock(return_value=None)), \
             patch.object(mcp_server.queries, "get_related_memories_batch", AsyncMock(return_value=related_map)):
            results = await fn(query="test", include_related=True)

        assert results[0]["related_memories"] == [{"id": "rel-a"}, {"id": "rel-b"}]
        assert results[1]["related_memories"] == [{"id": "rel-c"}]

    @pytest.mark.asyncio
    async def test_batch_fetch_failure_falls_back_to_empty(self):
        """A DB failure in the expansion must NOT break the main search —
        results come back without related_memories populated but WITH the
        field (empty list) for consistency."""
        fn = _unwrap(mcp_server.memory_search)
        fake_results = [{"id": "mem-1", "content": "x"}]

        async def fake_search(**kwargs):
            return fake_results

        async def fake_embed_batch(queries):
            return [[0.1] * 1024 for _ in queries]

        with patch.object(mcp_server.queries, "search_memories", side_effect=fake_search), \
             patch("nobrainr.embeddings.ollama.embed_batch", side_effect=fake_embed_batch), \
             patch.object(mcp_server.queries, "expand_chunk_context", AsyncMock(return_value=fake_results)), \
             patch.object(mcp_server.queries, "record_interest_signal", AsyncMock(return_value=None)), \
             patch.object(mcp_server.queries, "get_related_memories_batch", AsyncMock(side_effect=RuntimeError("boom"))):
            results = await fn(query="test", include_related=True)

        # Field exists (empty) rather than missing, so callers don't
        # need None-vs-missing branching
        assert results[0]["related_memories"] == []
