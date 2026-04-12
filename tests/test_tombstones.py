"""Tombstones for deleted content (Phase H, v6.10).

When a memory is deleted via ``delete_memory``, a hash of its content is
recorded in the ``memory_tombstones`` table. The write-queue dedup
classifier (``decide_write_action``) checks this table FIRST — if the
hash matches an existing tombstone, it short-circuits to NOOP with
``reason='tombstoned'`` before doing any expensive similarity search or
LLM decision.

This is the doobidoo/mcp-memory-service pattern: it prevents silent
re-ingestion of a memory the user explicitly deleted (same document
re-crawled, same ChatGPT export re-imported, etc.).

Tests cover:
  1. _compute_content_hash normalization (trim, lowercase, unicode)
  2. create_tombstone SQL shape (ON CONFLICT idempotency)
  3. is_tombstoned SQL shape (single indexed lookup)
  4. get_tombstone SQL shape
  5. decide_write_action tombstone short-circuit
     - hit → immediate NOOP return, no similarity search, no LLM call
     - miss → normal decision path runs
     - is_tombstoned exception → logged + normal path runs (never block writes)
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nobrainr.db import queries as db_queries
from nobrainr.extraction import dedup


# ──────────────────────────────────────────────
# _compute_content_hash — pure function
# ──────────────────────────────────────────────


class TestContentHashNormalization:
    def test_trims_leading_and_trailing_whitespace(self):
        a = db_queries._compute_content_hash("hello world")
        b = db_queries._compute_content_hash("  hello world  ")
        c = db_queries._compute_content_hash("\n\thello world\n")
        assert a == b == c

    def test_lowercases(self):
        a = db_queries._compute_content_hash("Hello World")
        b = db_queries._compute_content_hash("hello world")
        c = db_queries._compute_content_hash("HELLO WORLD")
        assert a == b == c

    def test_different_content_produces_different_hash(self):
        a = db_queries._compute_content_hash("memory A")
        b = db_queries._compute_content_hash("memory B")
        assert a != b

    def test_empty_string_produces_stable_hash(self):
        """Empty + None + whitespace-only all normalize to the empty-hash."""
        a = db_queries._compute_content_hash("")
        b = db_queries._compute_content_hash(None)  # type: ignore[arg-type]
        c = db_queries._compute_content_hash("   ")
        assert a == b == c
        # Sanity: sha256("") hex
        assert a == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

    def test_unicode_content(self):
        """Non-ASCII content hashes stably (UTF-8 round-trip)."""
        a = db_queries._compute_content_hash("Café du Monde")
        b = db_queries._compute_content_hash("CAFÉ DU MONDE")
        assert a == b  # lowercase normalization covers unicode case too

    def test_hash_is_64_hex_chars(self):
        """SHA256 = 256 bits = 64 hex chars."""
        h = db_queries._compute_content_hash("anything")
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)


# ──────────────────────────────────────────────
# create_tombstone — SQL shape
# ──────────────────────────────────────────────


async def _capture_fetchrow():
    captured: dict = {}

    async def fake_fetchrow(sql, *args):
        captured["sql"] = sql
        captured["args"] = args
        # Return created_at as a real datetime so _row_to_dict's .isoformat()
        # call on it succeeds (the real asyncpg driver returns datetime objects
        # for timestamptz columns).
        return {
            "id": "11111111-1111-1111-1111-111111111111",
            "content_hash": args[0],
            "original_memory_id": args[1],
            "reason": args[2],
            "created_at": datetime(2026, 4, 12, 2, 0, 0, tzinfo=timezone.utc),
        }

    mock_conn = AsyncMock()
    mock_conn.fetchrow = AsyncMock(side_effect=fake_fetchrow)
    acquire_ctx = MagicMock()
    acquire_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
    acquire_ctx.__aexit__ = AsyncMock(return_value=None)
    mock_pool = MagicMock()
    mock_pool.acquire = MagicMock(return_value=acquire_ctx)
    return mock_pool, captured


class TestCreateTombstone:
    @pytest.mark.asyncio
    async def test_inserts_with_on_conflict_noop(self):
        pool, cap = await _capture_fetchrow()
        with patch.object(db_queries, "get_pool", AsyncMock(return_value=pool)):
            result = await db_queries.create_tombstone(
                "sample content", reason="manual_delete"
            )
        sql = cap["sql"]
        assert "INSERT INTO memory_tombstones" in sql
        assert "ON CONFLICT (content_hash)" in sql
        # ON CONFLICT DO UPDATE with a no-op SET so RETURNING still fires
        assert "DO UPDATE" in sql
        assert "RETURNING" in sql
        assert result["content_hash"] == db_queries._compute_content_hash("sample content")

    @pytest.mark.asyncio
    async def test_default_reason_is_manual_delete(self):
        pool, cap = await _capture_fetchrow()
        with patch.object(db_queries, "get_pool", AsyncMock(return_value=pool)):
            await db_queries.create_tombstone("x")
        assert cap["args"][2] == "manual_delete"

    @pytest.mark.asyncio
    async def test_original_memory_id_nullable(self):
        pool, cap = await _capture_fetchrow()
        with patch.object(db_queries, "get_pool", AsyncMock(return_value=pool)):
            await db_queries.create_tombstone("x")
        assert cap["args"][1] is None


# ──────────────────────────────────────────────
# is_tombstoned — SQL shape
# ──────────────────────────────────────────────


async def _capture_fetchval(return_value):
    captured: dict = {}

    async def fake_fetchval(sql, *args):
        captured["sql"] = sql
        captured["args"] = args
        return return_value

    mock_conn = AsyncMock()
    mock_conn.fetchval = AsyncMock(side_effect=fake_fetchval)
    acquire_ctx = MagicMock()
    acquire_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
    acquire_ctx.__aexit__ = AsyncMock(return_value=None)
    mock_pool = MagicMock()
    mock_pool.acquire = MagicMock(return_value=acquire_ctx)
    return mock_pool, captured


class TestIsTombstoned:
    @pytest.mark.asyncio
    async def test_returns_true_when_row_exists(self):
        pool, _ = await _capture_fetchval(1)
        with patch.object(db_queries, "get_pool", AsyncMock(return_value=pool)):
            assert await db_queries.is_tombstoned("sample") is True

    @pytest.mark.asyncio
    async def test_returns_false_when_row_missing(self):
        pool, _ = await _capture_fetchval(None)
        with patch.object(db_queries, "get_pool", AsyncMock(return_value=pool)):
            assert await db_queries.is_tombstoned("sample") is False

    @pytest.mark.asyncio
    async def test_query_uses_content_hash_index(self):
        pool, cap = await _capture_fetchval(None)
        with patch.object(db_queries, "get_pool", AsyncMock(return_value=pool)):
            await db_queries.is_tombstoned("sample")
        sql = cap["sql"]
        assert "memory_tombstones" in sql
        assert "content_hash = $1" in sql
        assert "LIMIT 1" in sql

    @pytest.mark.asyncio
    async def test_query_passes_normalized_hash(self):
        pool, cap = await _capture_fetchval(None)
        with patch.object(db_queries, "get_pool", AsyncMock(return_value=pool)):
            await db_queries.is_tombstoned("  SAMPLE  ")
        # The hash passed to the query must be the normalized one
        assert cap["args"][0] == db_queries._compute_content_hash("sample")


# ──────────────────────────────────────────────
# decide_write_action — tombstone short-circuit
# ──────────────────────────────────────────────


class TestDecideWriteActionTombstoneShortCircuit:
    @pytest.mark.asyncio
    async def test_tombstone_hit_returns_noop_without_similarity_search(self):
        """If is_tombstoned → True, we return NOOP immediately — no call
        to find_similar_memories, no call to ollama_chat."""
        find_sim_mock = AsyncMock()  # shouldn't be called
        ollama_mock = AsyncMock()    # shouldn't be called

        with patch.object(dedup, "find_similar_memories", find_sim_mock), \
             patch.object(dedup, "ollama_chat", ollama_mock), \
             patch("nobrainr.db.queries.is_tombstoned", AsyncMock(return_value=True)):
            result = await dedup.decide_write_action("deleted content", [0.1] * 768)

        assert result["action"] == "NOOP"
        assert "tombstoned" in result["reason"]
        find_sim_mock.assert_not_called()
        ollama_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_tombstone_miss_runs_normal_path(self):
        """If is_tombstoned → False, fall through to find_similar_memories."""
        find_sim_mock = AsyncMock(return_value=[])  # no similar → ADD path
        with patch.object(dedup, "find_similar_memories", find_sim_mock), \
             patch("nobrainr.db.queries.is_tombstoned", AsyncMock(return_value=False)):
            result = await dedup.decide_write_action("new content", [0.1] * 768)
        assert result["action"] == "ADD"
        find_sim_mock.assert_called_once()

    @pytest.mark.asyncio
    async def test_is_tombstoned_exception_does_not_block_writes(self):
        """If the tombstone check itself raises, we MUST continue with the
        normal decision path — a DB hiccup on the tombstone lookup must
        never silently drop user writes."""
        find_sim_mock = AsyncMock(return_value=[])
        with patch.object(dedup, "find_similar_memories", find_sim_mock), \
             patch("nobrainr.db.queries.is_tombstoned", AsyncMock(side_effect=RuntimeError("db down"))):
            result = await dedup.decide_write_action("content", [0.1] * 768)
        # Fall-through succeeded — action is ADD (from empty candidates)
        assert result["action"] == "ADD"
        find_sim_mock.assert_called_once()
