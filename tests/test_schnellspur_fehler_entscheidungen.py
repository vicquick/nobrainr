"""error_store and decision_store write inside the tool call (fast lane).

Measured 08.-15.11.2026 on memory_write_queue: 491 writes, median 4.5 h from
enqueue to stored (up to 7.4 h), the work itself 21 s — the queue worker
parks while the chat model serves live use. An error pattern that sits in
the queue warns nobody. Both tools now store the way msg_send does: one
embedding, one INSERT, no LLM; the queue remains the fallback.
"""

from __future__ import annotations

import asyncio
import inspect
from unittest.mock import AsyncMock, patch

from nobrainr.db import queries
from nobrainr.mcp import server

FEHLER = dict(
    error_signature="asyncpg AmbiguousParameterError: inconsistent types for $2",
    root_cause="one parameter in an assignment and a comparison",
    fix="cast every occurrence: $2::real",
    prevention="run SQL once against a real database before merge",
)


def _lane(store=None, existing=None, enqueue=None):
    """Patch the three things the lane touches; return the mocks."""
    store = store or AsyncMock(return_value={"id": "m-1", "status": "stored"})
    find = AsyncMock(return_value=existing)
    enqueue = enqueue or AsyncMock(
        return_value={"queue_id": "q-1", "enqueued_at": "2026-11-15T09:00:00+00:00"})
    return (
        patch.object(server, "store_memory_with_extraction", store),
        patch.object(server.queries, "find_recent_memory_by_content", find),
        patch("nobrainr.db.write_queue.enqueue_memory_write", enqueue),
        store, find, enqueue,
    )


async def test_error_pattern_is_stored_in_the_call_without_an_llm_step():
    p1, p2, p3, store, _, enqueue = _lane()
    with p1, p2, p3:
        out = await server.error_store(**FEHLER, tags=["asyncpg"], source_machine="bimavo")

    assert out == {"status": "stored", "memory_id": "m-1", "fast_lane": True}
    enqueue.assert_not_awaited()
    kw = store.await_args.kwargs
    assert kw["skip_dedup"] is True and kw["defer_extraction"] is True, \
        "dedup classifier and extraction need the chat model — not on this path"
    assert kw["category"] == "debugging" and "error-pattern" in kw["tags"]
    assert kw["metadata"]["error_signature"] == FEHLER["error_signature"]
    assert kw["source_machine"] == "bimavo"


async def test_decision_is_stored_in_the_call_and_names_its_scope():
    p1, p2, p3, store, _, enqueue = _lane()
    with p1, p2, p3:
        out = await server.decision_store(
            scope="nobrainr/write-path", decision="Store errors on the fast lane",
            rationale="queued writes waited hours", alternatives_rejected=["priority column"],
        )

    assert out["status"] == "stored" and out["memory_id"] == "m-1"
    assert out["scope"] == "nobrainr/write-path" and "finds it now" in out["message"]
    enqueue.assert_not_awaited()
    kw = store.await_args.kwargs
    assert kw["category"] == "decision" and {"decision", "nobrainr"} <= set(kw["tags"])
    assert kw["skip_dedup"] is True and kw["defer_extraction"] is True


async def test_identical_content_of_the_last_week_is_not_stored_twice():
    p1, p2, p3, store, find, enqueue = _lane(existing="m-alt")
    with p1, p2, p3:
        out = await server.error_store(**FEHLER)

    assert out == {"status": "skipped", "memory_id": "m-alt", "deduped": True, "fast_lane": True}
    store.assert_not_awaited()
    enqueue.assert_not_awaited()
    assert find.await_args.args[0].startswith("## Error Pattern")


async def test_slow_embedding_falls_back_to_the_queue_and_loses_nothing(monkeypatch):
    monkeypatch.setattr(server.settings, "fast_lane_timeout_s", 0.05)

    async def haengt(**kw):
        await asyncio.sleep(5)

    p1, p2, p3, _, _, enqueue = _lane(store=AsyncMock(side_effect=haengt))
    with p1, p2, p3:
        out = await server.error_store(**FEHLER)

    assert out["status"] == "queued" and out["queue_id"] == "q-1" and out["fast_lane"] is False
    kw = enqueue.await_args.kwargs
    assert kw["content"].startswith("## Error Pattern") and kw["category"] == "debugging"
    assert kw["skip_dedup"] is False, "on the queue an error pattern keeps its dedup"


async def test_broken_embedding_falls_back_and_a_queued_decision_keeps_skip_dedup():
    p1, p2, p3, _, _, enqueue = _lane(store=AsyncMock(side_effect=RuntimeError("embed down")))
    with p1, p2, p3:
        out = await server.decision_store(
            scope="nobrainr/write-path", decision="Store errors on the fast lane",
            rationale="queued writes waited hours",
        )

    assert out["status"] == "queued" and "memory_store_status" in out["message"]
    assert enqueue.await_args.kwargs["skip_dedup"] is True


async def test_lane_can_be_switched_off_and_long_content_goes_to_the_queue(monkeypatch):
    monkeypatch.setattr(server.settings, "fast_lane_timeout_s", 0)
    p1, p2, p3, store, find, enqueue = _lane()
    with p1, p2, p3:
        out = await server.error_store(**FEHLER)
    assert out["status"] == "queued"
    store.assert_not_awaited()
    find.assert_not_awaited()

    monkeypatch.setattr(server.settings, "fast_lane_timeout_s", 10.0)
    lang = dict(FEHLER, fix="x" * (server.settings.chunk_threshold + 1))
    p1, p2, p3, store, _, enqueue = _lane()
    with p1, p2, p3:
        out = await server.error_store(**lang)
    assert out["status"] == "queued", "the queue chunks long content; one embedding would cut it off"
    store.assert_not_awaited()


async def test_oversized_content_is_refused_as_before(monkeypatch):
    monkeypatch.setattr(server.settings, "max_content_length", 200)
    p1, p2, p3, store, _, enqueue = _lane()
    with p1, p2, p3:
        out = await server.error_store(**dict(FEHLER, fix="x" * 500))
    assert "Content too large" in out["error"]
    store.assert_not_awaited()
    enqueue.assert_not_awaited()


async def test_oversized_decision_returns_the_error_instead_of_raising(monkeypatch):
    monkeypatch.setattr(server.settings, "max_content_length", 50)
    p1, p2, p3, store, _, enqueue = _lane()
    with p1, p2, p3:
        out = await server.decision_store(
            scope="nobrainr/write-path", decision="Store errors on the fast lane",
            rationale="queued writes waited hours " * 5,
        )
    assert "Content too large" in out["error"]
    store.assert_not_awaited()
    enqueue.assert_not_awaited()


async def test_meeting_close_references_the_memory_id():
    quelle = inspect.getsource(server.meeting_close)
    assert 'stored.get("memory_id") or stored.get("queue_id")' in quelle


class _Verbindung:
    def __init__(self, zeile):
        self.zeile, self.aufrufe = zeile, []

    async def fetchrow(self, sql, *params):
        self.aufrufe.append((sql, params))
        return self.zeile

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Pool:
    def __init__(self, verbindung):
        self.verbindung = verbindung

    def acquire(self):
        return self.verbindung


async def test_duplicate_lookup_hashes_like_the_queue_pre_check():
    import hashlib

    verbindung = _Verbindung({"id": "m-7"})
    with patch.object(queries, "get_pool", AsyncMock(return_value=_Pool(verbindung))):
        assert await queries.find_recent_memory_by_content("gleicher Text") == "m-7"
    (sql, params), = verbindung.aufrufe
    assert "encode(sha256(convert_to(content, 'UTF8')), 'hex') = $1" in sql
    assert "make_interval(days => $2::int)" in sql
    assert params == (hashlib.sha256("gleicher Text".encode()).hexdigest(), 7)

    verbindung = _Verbindung(None)
    with patch.object(queries, "get_pool", AsyncMock(return_value=_Pool(verbindung))):
        assert await queries.find_recent_memory_by_content("neu") is None
