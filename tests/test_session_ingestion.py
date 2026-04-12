"""Session-level ingestion path (Phase I, v6.11).

doobidoo/mcp-memory-service published +5.6% R@5 on LongMemEval when they
switched ingestion from per-turn/per-learning to whole-session. Phase I
adds ``store_conversations_as_sessions`` — a path ORTHOGONAL to
``distill_conversations`` that stores each raw conversation as ONE memory
in the memories table.

These tests cover:
  1. ``_build_session_text`` rendering (header, turn formatting, per-turn
     truncation, whole-text truncation)
  2. SQL shape: selects undistilled raw conversations, processes ``limit``,
     marks via ``session_stored`` metadata flag (NOT ``distilled`` — the
     two paths are independent)
  3. Short-conversation skip behavior (< ``min_turns``)
  4. Happy-path storage (embedding called, store_memory called with the
     right args, metadata carries storage_mode + turn_count + original
     conversation_id)
  5. Idempotency: once a row is marked ``session_stored``, it does not
     get picked up again
  6. MCP tool delegation (``memory_import_chatgpt_sessions`` → function)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nobrainr.importers import chatgpt as chatgpt_importer
from nobrainr.mcp import server as mcp_server


def _unwrap(fn):
    if hasattr(fn, "fn"):
        return fn.fn
    if hasattr(fn, "__wrapped__"):
        return fn.__wrapped__
    return fn


# ──────────────────────────────────────────────
# _build_session_text — pure rendering
# ──────────────────────────────────────────────


class TestBuildSessionText:
    def test_header_and_two_turns(self):
        msgs = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ]
        text, turns = chatgpt_importer._build_session_text(
            "Demo", msgs, max_chars=10000,
        )
        assert turns == 2
        assert text.startswith("# Conversation: Demo")
        assert "## USER" in text
        assert "## ASSISTANT" in text
        assert "hello" in text
        assert "hi there" in text

    def test_non_user_assistant_roles_filtered(self):
        """System/tool messages shouldn't count toward turn_count."""
        msgs = [
            {"role": "system", "content": "sys prompt"},
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": "a"},
            {"role": "tool", "content": "tool output"},
        ]
        _, turns = chatgpt_importer._build_session_text(
            "X", msgs, max_chars=10000,
        )
        assert turns == 2  # only user + assistant

    def test_per_turn_truncation(self):
        """Individual overlong turns (code dumps) are truncated."""
        huge = "x" * 10000
        msgs = [
            {"role": "user", "content": huge},
            {"role": "assistant", "content": "ok"},
        ]
        text, turns = chatgpt_importer._build_session_text(
            "Huge", msgs, max_chars=100000, per_turn_max=1000,
        )
        assert "[...truncated...]" in text
        assert text.count("x") < 1100  # turn was shortened
        assert turns == 2

    def test_whole_text_truncation(self):
        """If the assembled text still exceeds max_chars, the tail is cut."""
        msgs = [
            {"role": "user", "content": "a" * 2000},
            {"role": "assistant", "content": "b" * 2000},
        ]
        text, _ = chatgpt_importer._build_session_text(
            "Small cap", msgs, max_chars=500,
        )
        assert "[...session truncated...]" in text
        assert len(text) <= 500

    def test_empty_messages_returns_zero(self):
        text, turns = chatgpt_importer._build_session_text(
            "Empty", [], max_chars=1000,
        )
        assert turns == 0
        assert text == ""

    def test_missing_content_field_is_skipped_cleanly(self):
        msgs = [
            {"role": "user"},  # no content key
            {"role": "assistant", "content": "fallback"},
        ]
        text, turns = chatgpt_importer._build_session_text(
            "Weird", msgs, max_chars=1000,
        )
        # The missing-content user still counts as a turn (role matches),
        # just with empty content in the rendered text
        assert turns == 2
        assert "fallback" in text


# ──────────────────────────────────────────────
# store_conversations_as_sessions — SQL + flow
# ──────────────────────────────────────────────


def _fake_conversation_row(
    convo_id: str = "11111111-1111-1111-1111-111111111111",
    title: str = "Hello World",
    turns: int = 4,
    source_type: str = "chatgpt",
    metadata: dict | None = None,
):
    messages = []
    for i in range(turns // 2):
        messages.append({"role": "user", "content": f"user turn {i}"})
        messages.append({"role": "assistant", "content": f"assistant turn {i}"})
    return {
        "id": convo_id,
        "title": title,
        "messages": messages,
        "metadata": metadata or {"source_machine": "bimavo"},
        "source_type": source_type,
    }


async def _run_with_mocks(rows: list[dict], *, store_memory_result=None):
    """Run store_conversations_as_sessions against mocked pool + embedder +
    store_memory, and return (result, captures)."""
    captured: dict = {"mark_calls": [], "store_calls": []}

    async def fake_fetch(sql, *args):
        captured["select_sql"] = sql
        captured["select_args"] = args
        return rows

    async def fake_execute(sql, *args):
        captured["mark_calls"].append({"sql": sql, "args": args})
        return "UPDATE 1"

    mock_conn = AsyncMock()
    mock_conn.fetch = AsyncMock(side_effect=fake_fetch)
    mock_conn.execute = AsyncMock(side_effect=fake_execute)
    acquire_ctx = MagicMock()
    acquire_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
    acquire_ctx.__aexit__ = AsyncMock(return_value=None)
    mock_pool = MagicMock()
    mock_pool.acquire = MagicMock(return_value=acquire_ctx)

    async def fake_embed(text):
        captured.setdefault("embed_texts", []).append(text)
        return [0.01] * 1024

    async def fake_store_memory(**kwargs):
        captured["store_calls"].append(kwargs)
        return store_memory_result or {
            "id": "22222222-2222-2222-2222-222222222222",
            "status": "stored",
        }

    with patch.object(chatgpt_importer, "get_pool", AsyncMock(return_value=mock_pool)), \
         patch.object(chatgpt_importer, "embed_text", side_effect=fake_embed), \
         patch.object(chatgpt_importer.queries, "store_memory", side_effect=fake_store_memory):
        result = await chatgpt_importer.store_conversations_as_sessions(limit=10)
    return result, captured


class TestStoreConversationsAsSessions:
    @pytest.mark.asyncio
    async def test_happy_path_stores_one_session_memory(self):
        result, cap = await _run_with_mocks([_fake_conversation_row()])
        assert result["status"] == "complete"
        assert result["processed"] == 1
        assert result["stored"] == 1
        assert result["skipped"] == 0
        assert result["errors"] == 0
        # store_memory was called exactly once with session metadata
        assert len(cap["store_calls"]) == 1
        store = cap["store_calls"][0]
        assert "Conversation: Hello World" in store["content"]
        assert "session-log" == store["category"]
        assert "session-level" in store["tags"]
        assert "imported" in store["tags"]
        assert store["metadata"]["storage_mode"] == "session"
        assert store["metadata"]["turn_count"] == 4
        assert store["metadata"]["source_type_original"] == "chatgpt"
        assert store["source_type"] == "chatgpt_session"

    @pytest.mark.asyncio
    async def test_selects_only_undistilled_sessions(self):
        """The SELECT must filter to rows where session_stored IS NULL so we
        don't reprocess."""
        result, cap = await _run_with_mocks([])
        assert "session_stored" in cap["select_sql"]
        assert "IS NULL" in cap["select_sql"]
        assert "conversations_raw" in cap["select_sql"]
        assert result["processed"] == 0

    @pytest.mark.asyncio
    async def test_short_conversation_is_skipped(self):
        """< min_turns (default 2) = skip but still mark as session_stored."""
        short = _fake_conversation_row(title="Greeting", turns=0)
        result, cap = await _run_with_mocks([short])
        assert result["skipped"] == 1
        assert result["stored"] == 0
        # No store_memory call for the skipped row
        assert len(cap["store_calls"]) == 0
        # But we DID mark it as session_stored (so next call doesn't
        # re-scan the same short row)
        assert len(cap["mark_calls"]) == 1

    @pytest.mark.asyncio
    async def test_stores_session_memory_id_in_raw_metadata(self):
        """After a successful store, the raw row gets session_stored + the
        returned memory_id so we have a back-reference."""
        result, cap = await _run_with_mocks(
            [_fake_conversation_row()],
            store_memory_result={"id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "status": "stored"},
        )
        assert result["stored"] == 1
        mark_payload = cap["mark_calls"][0]["args"][0]
        assert "aaaaaaaa" in mark_payload
        assert "session_stored" in mark_payload

    @pytest.mark.asyncio
    async def test_mark_flag_is_distinct_from_distilled(self):
        """CRITICAL: session_stored must NOT conflict with the distilled flag —
        both can be set on the same row for conversations that go through
        both paths."""
        await _run_with_mocks([_fake_conversation_row()])
        # Verify the two paths use distinct jsonb keys — direct test
        # against the _mark_session_stored payload shape:
        import json as _json
        captured: dict = {}
        async def fake_exec(sql, *args):
            captured["sql"] = sql
            captured["args"] = args
            return "UPDATE 1"
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock(side_effect=fake_exec)
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=mock_conn)
        ctx.__aexit__ = AsyncMock(return_value=None)
        pool = MagicMock()
        pool.acquire = MagicMock(return_value=ctx)
        with patch.object(chatgpt_importer, "get_pool", AsyncMock(return_value=pool)):
            await chatgpt_importer._mark_session_stored(
                "11111111-1111-1111-1111-111111111111", stored=True,
            )
        payload_json = _json.loads(captured["args"][0])
        assert "session_stored" in payload_json
        assert "distilled" not in payload_json  # distinct flag!


# ──────────────────────────────────────────────
# MCP tool — memory_import_chatgpt_sessions
# ──────────────────────────────────────────────


class TestMCPSessionTool:
    @pytest.mark.asyncio
    async def test_tool_delegates_to_importer(self):
        fn = _unwrap(mcp_server.memory_import_chatgpt_sessions)
        mock_fn = AsyncMock(return_value={
            "status": "complete",
            "processed": 2,
            "stored": 2,
            "skipped": 0,
            "errors": 0,
        })
        with patch(
            "nobrainr.importers.chatgpt.store_conversations_as_sessions",
            mock_fn,
        ):
            result = await fn(source_machine="bimavo", limit=25)
        assert result["stored"] == 2
        mock_fn.assert_called_once()
        kwargs = mock_fn.call_args.kwargs
        assert kwargs["source_machine"] == "bimavo"
        assert kwargs["limit"] == 25

    @pytest.mark.asyncio
    async def test_tool_defaults(self):
        fn = _unwrap(mcp_server.memory_import_chatgpt_sessions)
        mock_fn = AsyncMock(return_value={"status": "complete", "processed": 0})
        with patch(
            "nobrainr.importers.chatgpt.store_conversations_as_sessions",
            mock_fn,
        ):
            await fn()
        kwargs = mock_fn.call_args.kwargs
        # Defaults should match the tool signature
        assert kwargs["limit"] == 50
        assert kwargs["max_content_chars"] == 30000
        assert kwargs["min_turns"] == 2
