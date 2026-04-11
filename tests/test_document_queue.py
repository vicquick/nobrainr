"""enqueue_document_chunks + memory_store_document MCP tool (v7 follow-up).

PR #18 queued the single-write memory_store path. This PR covers the
document and crawl paths — memory_store_document and crawl_and_store
used to hit store_document_chunked which loops per-chunk on
store_memory_with_extraction, each chunk firing its own 600s decide_write_action
LLM call plus an additional _generate_chunk_context prefix LLM call.
That's up to N × 2 blocking LLM calls on the hot path of a single tool call.

These tests lock in the new behaviour: every chunk path lands a row in
memory_write_queue via enqueue_memory_write. Prefixes are intentionally
deferred (contextual_prefix_backfill scheduler job handles them later).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from nobrainr.db import write_queue
from nobrainr.mcp import server as mcp_server


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────


def _unwrap(fn):
    """Unwrap a FastMCP-decorated tool so we can call it directly."""
    if hasattr(fn, "fn"):
        return fn.fn
    if hasattr(fn, "__wrapped__"):
        return fn.__wrapped__
    return fn


def _mk_chunk(index: int, total: int, text: str, offset: int = 0):
    class _C:
        pass

    c = _C()
    c.index = index
    c.total = total
    c.text = text
    c.char_offset = offset
    return c


# ──────────────────────────────────────────────
# enqueue_document_chunks
# ──────────────────────────────────────────────


async def test_enqueue_document_chunks_short_content_single_row():
    """Content under chunk_threshold → one enqueue_memory_write call, no chunking."""
    enqueue_mock = AsyncMock(
        return_value={"queue_id": str(uuid4()), "enqueued_at": "2026-04-11T20:00:00+00:00"}
    )

    with (
        patch("nobrainr.db.write_queue.enqueue_memory_write", enqueue_mock),
        patch.object(mcp_server.settings, "chunk_threshold", 10_000),
    ):
        result = await write_queue.enqueue_document_chunks(
            content="short doc",
            title="My Doc",
            tags=["a"],
            category="documentation",
        )

    assert result["status"] == "queued"
    assert result["chunks"] == 1
    assert result["document_id"] is None
    assert len(result["queue_ids"]) == 1
    enqueue_mock.assert_awaited_once()
    # The summary fell back to f"Document: {title}" since no explicit summary given.
    kwargs = enqueue_mock.await_args.kwargs
    assert kwargs["summary"] == "Document: My Doc"
    assert kwargs["tags"] == ["a"]


async def test_enqueue_document_chunks_empty_content_errors():
    result = await write_queue.enqueue_document_chunks(content="")
    assert "error" in result
    assert result["error"] == "Empty content"

    result = await write_queue.enqueue_document_chunks(content="   \n\t  ")
    assert "error" in result
    assert result["error"] == "Empty content"


async def test_enqueue_document_chunks_long_content_multi_chunk():
    """Long content → 3 chunk enqueues with shared document_id and skip_dedup=True."""
    chunks = [
        _mk_chunk(0, 3, "chunk zero text", offset=0),
        _mk_chunk(1, 3, "chunk one text", offset=12),
        _mk_chunk(2, 3, "chunk two text", offset=24),
    ]
    chunk_mock = AsyncMock(return_value=chunks)
    # chunk_text is sync in real code; for the purposes of this test we patch
    # it with an AsyncMock wrapper since the source calls it synchronously, so
    # we need to patch with a regular callable that returns the list.
    def _chunk_sync(*args, **kwargs):
        return chunks

    enqueue_results = [
        {"queue_id": "aaaaaaaa-0000-0000-0000-000000000001", "enqueued_at": "t1"},
        {"queue_id": "aaaaaaaa-0000-0000-0000-000000000002", "enqueued_at": "t2"},
        {"queue_id": "aaaaaaaa-0000-0000-0000-000000000003", "enqueued_at": "t3"},
    ]
    enqueue_mock = AsyncMock(side_effect=enqueue_results)

    # Force chunking path by setting a tiny threshold
    long_content = "x" * 5000
    with (
        patch("nobrainr.services.chunking.chunk_text", _chunk_sync),
        patch("nobrainr.db.write_queue.enqueue_memory_write", enqueue_mock),
        patch.object(mcp_server.settings, "chunk_threshold", 100),
    ):
        result = await write_queue.enqueue_document_chunks(
            content=long_content,
            title="Big Doc",
            tags=["arch"],
            category="documentation",
            source_ref="/opt/docs/big.md",
        )

    assert result["status"] == "queued"
    assert result["chunks"] == 3
    assert result["document_id"] is not None
    assert len(result["queue_ids"]) == 3
    assert result["queue_ids"] == [
        "aaaaaaaa-0000-0000-0000-000000000001",
        "aaaaaaaa-0000-0000-0000-000000000002",
        "aaaaaaaa-0000-0000-0000-000000000003",
    ]
    assert enqueue_mock.await_count == 3

    # Each chunk must carry shared document_id + skip_dedup=True
    all_calls = enqueue_mock.await_args_list
    document_ids = {c.kwargs["metadata"]["document_id"] for c in all_calls}
    assert len(document_ids) == 1  # all chunks share one document_id
    assert next(iter(document_ids)) == result["document_id"]

    for i, call in enumerate(all_calls):
        md = call.kwargs["metadata"]
        assert md["chunk_index"] == i
        assert md["chunk_total"] == 3
        assert md["document_title"] == "Big Doc"
        assert call.kwargs["skip_dedup"] is True
        # Summary encodes chunk position
        assert f"[{i + 1}/3]" in call.kwargs["summary"]


async def test_enqueue_document_chunks_respects_existing_metadata():
    """Caller-supplied metadata is merged into each chunk, not overwritten."""
    chunks = [_mk_chunk(0, 2, "a", 0), _mk_chunk(1, 2, "b", 1)]

    def _chunk_sync(*args, **kwargs):
        return chunks

    enqueue_mock = AsyncMock(
        side_effect=[
            {"queue_id": "a", "enqueued_at": "t"},
            {"queue_id": "b", "enqueued_at": "t"},
        ]
    )

    with (
        patch("nobrainr.services.chunking.chunk_text", _chunk_sync),
        patch("nobrainr.db.write_queue.enqueue_memory_write", enqueue_mock),
        patch.object(mcp_server.settings, "chunk_threshold", 0),
    ):
        await write_queue.enqueue_document_chunks(
            content="x" * 100,
            metadata={"source_project": "bimavo", "version": 2},
        )

    for call in enqueue_mock.await_args_list:
        md = call.kwargs["metadata"]
        assert md["source_project"] == "bimavo"
        assert md["version"] == 2
        # Chunk-specific keys are also present
        assert "document_id" in md
        assert "chunk_index" in md


async def test_enqueue_document_chunks_chunking_failure_errors_cleanly():
    """If chunk_text returns no chunks, we return an error, not a crash."""

    def _chunk_empty(*args, **kwargs):
        return []

    with (
        patch("nobrainr.services.chunking.chunk_text", _chunk_empty),
        patch.object(mcp_server.settings, "chunk_threshold", 0),
    ):
        result = await write_queue.enqueue_document_chunks(content="x" * 200)

    assert "error" in result
    assert result["error"] == "Chunking produced no output"


# ──────────────────────────────────────────────
# memory_store_document MCP tool
# ──────────────────────────────────────────────


async def test_memory_store_document_rejects_oversized_content():
    max_len = 100
    with patch.object(mcp_server.settings, "max_content_length", max_len):
        fn = _unwrap(mcp_server.memory_store_document)
        result = await fn(content="x" * (max_len * 5 + 1))

    assert "error" in result
    assert "Content too large" in result["error"]


async def test_memory_store_document_delegates_to_enqueue_document_chunks():
    """The MCP tool is now a thin wrapper around enqueue_document_chunks."""
    enq_mock = AsyncMock(
        return_value={
            "status": "queued",
            "queue_ids": ["fake-queue-id"],
            "chunks": 1,
            "document_id": None,
        }
    )

    with patch("nobrainr.db.write_queue.enqueue_document_chunks", enq_mock):
        fn = _unwrap(mcp_server.memory_store_document)
        result = await fn(
            content="doc body",
            title="My Doc",
            tags=["a"],
            category="documentation",
            source_machine="bimavo",
        )

    assert result["status"] == "queued"
    assert result["queue_ids"] == ["fake-queue-id"]
    enq_mock.assert_awaited_once()
    kwargs = enq_mock.await_args.kwargs
    assert kwargs["content"] == "doc body"
    assert kwargs["title"] == "My Doc"
    assert kwargs["tags"] == ["a"]
    assert kwargs["source_machine"] == "bimavo"


# ──────────────────────────────────────────────
# crawl_and_store MCP tool
# ──────────────────────────────────────────────


async def test_crawl_and_store_chunked_uses_enqueue_document_chunks():
    """The chunked path goes through enqueue_document_chunks, not store_document_chunked."""
    crawl_mock = AsyncMock(
        return_value={
            "markdown": "# Some Page\n" + ("real content " * 200),
            "title": "Some Page",
        }
    )
    enq_doc_mock = AsyncMock(
        return_value={
            "status": "queued",
            "queue_ids": ["q1", "q2"],
            "chunks": 2,
            "document_id": "doc-id",
        }
    )
    interest_mock = AsyncMock()

    with (
        patch.object(mcp_server, "crawl_page", crawl_mock),
        patch("nobrainr.db.write_queue.enqueue_document_chunks", enq_doc_mock),
        patch.object(mcp_server.queries, "record_interest_signal", interest_mock),
        patch.object(mcp_server.settings, "interest_tracking_enabled", True),
    ):
        fn = _unwrap(mcp_server.crawl_and_store)
        result = await fn(
            url="https://example.com/docs",
            tags=["python"],
            category="documentation",
            chunked=True,
        )

    assert result["url"] == "https://example.com/docs"
    assert result["title"] == "Some Page"
    assert result["chunked"] is True
    assert result["result"]["status"] == "queued"
    assert result["result"]["queue_ids"] == ["q1", "q2"]
    assert result["result"]["chunks"] == 2

    enq_doc_mock.assert_awaited_once()
    call_kwargs = enq_doc_mock.await_args.kwargs
    assert call_kwargs["source_type"] == "crawl"
    assert call_kwargs["source_ref"] == "https://example.com/docs"
    assert "crawled" in call_kwargs["tags"]
    assert "python" in call_kwargs["tags"]


async def test_crawl_and_store_unchunked_still_queued():
    """The chunked=False path also goes through the queue (not the old sync path)."""
    # Content must be >= 50 chars after strip (crawl_and_store short-circuits otherwise)
    crawl_mock = AsyncMock(
        return_value={
            "markdown": "# Short page\n" + "This is content that is clearly longer than fifty characters in total.",
            "title": "Short",
        }
    )
    enq_mock = AsyncMock(
        return_value={"queue_id": "singleq", "enqueued_at": "t"}
    )

    with (
        patch.object(mcp_server, "crawl_page", crawl_mock),
        patch("nobrainr.db.write_queue.enqueue_memory_write", enq_mock),
        patch.object(mcp_server.settings, "interest_tracking_enabled", False),
        patch.object(mcp_server.settings, "chunk_threshold", 10_000),
    ):
        fn = _unwrap(mcp_server.crawl_and_store)
        result = await fn(url="https://example.com/short", chunked=False)

    assert result["chunked"] is False
    assert result["result"]["status"] == "queued"
    assert result["result"]["queue_ids"] == ["singleq"]
    assert result["result"]["chunks"] == 1
    enq_mock.assert_awaited_once()


async def test_crawl_and_store_handles_empty_crawl_result():
    """If the page returns too little content, we short-circuit with an error."""
    crawl_mock = AsyncMock(return_value={"markdown": "   ", "title": "Empty"})

    with patch.object(mcp_server, "crawl_page", crawl_mock):
        fn = _unwrap(mcp_server.crawl_and_store)
        result = await fn(url="https://example.com/empty")

    assert "error" in result
    assert "too little" in result["error"]


async def test_crawl_and_store_propagates_crawl_error():
    """If Crawl4AI returns an error dict, we return it directly."""
    crawl_mock = AsyncMock(
        return_value={"error": "Crawl4AI timed out", "url": "https://bad.example"}
    )

    with patch.object(mcp_server, "crawl_page", crawl_mock):
        fn = _unwrap(mcp_server.crawl_and_store)
        result = await fn(url="https://bad.example")

    assert result["error"] == "Crawl4AI timed out"
