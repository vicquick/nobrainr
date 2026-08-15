"""memory_write_queue (v7) — queue-based write path for memory_store.

Covers:
  * Schema contract: queue table columns + partial indexes exist in SCHEMA_SQL
  * enqueue_memory_write: INSERT shape + wake-up signal
  * claim_next_pending: FIFO + FOR UPDATE SKIP LOCKED + status flip
  * mark_done: populates memory_id / result_status
  * mark_failed retry path: exponential backoff, attempts increment
  * mark_failed give-up path: exhausted retries → status=failed
  * memory_store MCP tool: returns queued shape, enqueues once
  * memory_store wait=True: polls status and returns when done
  * memory_store_status MCP tool: valid/invalid/missing queue_id
  * Scheduler worker: claim → process → mark_done happy path (mocked pipeline)
  * Scheduler worker: exception → mark_failed → retry path
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from nobrainr.db import write_queue
from nobrainr.mcp import server as mcp_server


# ──────────────────────────────────────────────
# Schema contract
# ──────────────────────────────────────────────

def test_schema_contains_write_queue_table_and_indexes():
    from nobrainr.db.schema import SCHEMA_SQL

    assert "CREATE TABLE IF NOT EXISTS memory_write_queue" in SCHEMA_SQL
    # Payload columns
    for col in (
        "content", "summary", "tags", "category", "source_type",
        "source_machine", "source_ref", "confidence", "metadata",
        "skip_dedup", "contextual_prefix",
    ):
        assert col in SCHEMA_SQL, f"{col} column missing from queue schema"
    # State columns
    for col in ("status", "attempts", "max_attempts", "error_message"):
        assert col in SCHEMA_SQL, f"{col} column missing from queue schema"
    # Partial index on pending rows — the worker's hot query
    assert "idx_memory_write_queue_pending" in SCHEMA_SQL
    assert "WHERE status = 'pending'" in SCHEMA_SQL


# ──────────────────────────────────────────────
# Pool / connection doubles
# ──────────────────────────────────────────────


class _FakeTxn:
    """Emulates pool.acquire().transaction() async context manager."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _FakeConn:
    def __init__(self):
        self.fetchrow = AsyncMock()
        self.fetchval = AsyncMock()
        self.execute = AsyncMock()

    def transaction(self):
        return _FakeTxn()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _FakePool:
    def __init__(self):
        self.conn = _FakeConn()

    def acquire(self):
        return self.conn


def _now_utc():
    return datetime.now(tz=timezone.utc)


# ──────────────────────────────────────────────
# Wake-up signalling
# ──────────────────────────────────────────────

async def test_wait_for_pending_times_out_when_no_signal():
    # Reset the module-level event
    write_queue._write_pending_event = None
    got = await write_queue.wait_for_pending(timeout=0.05)
    assert got is False


async def test_wait_for_pending_returns_true_when_signalled():
    write_queue._write_pending_event = None
    # Schedule a signal after a short delay
    async def _signaller():
        await asyncio.sleep(0.01)
        write_queue.signal_pending()
    asyncio.create_task(_signaller())
    got = await write_queue.wait_for_pending(timeout=1.0)
    assert got is True


async def test_signal_pending_outside_loop_does_not_raise():
    # signal_pending is called from enqueue; if (somehow) there's no loop,
    # it must degrade gracefully so callers never have to try/except.
    write_queue._write_pending_event = None
    write_queue.signal_pending()  # Should not raise even without a prior event


# ──────────────────────────────────────────────
# enqueue_memory_write
# ──────────────────────────────────────────────

async def test_enqueue_returns_queue_id_and_wakes_worker():
    """enqueue_memory_write now does TWO dedup probes (queue + memories)
    before the INSERT. Provide three sequential fetchrow returns:
    None (no queue dup), None (no memory dup), insert-row.
    """
    pool = _FakePool()
    fake_id = uuid4()
    pool.conn.fetchrow.side_effect = [
        None,
        None,
        {"id": fake_id, "enqueued_at": _now_utc()},
    ]

    # Reset event so we can observe the signal
    write_queue._write_pending_event = None
    evt = write_queue._get_event()
    assert evt.is_set() is False

    with patch.object(write_queue, "get_pool", AsyncMock(return_value=pool)):
        out = await write_queue.enqueue_memory_write(
            content="hello world",
            summary="short",
            tags=["foo", "bar"],
            category="insight",
            source_type="claude",
            metadata={"nested": {"k": 1}},
        )

    assert out["queue_id"] == str(fake_id)
    assert "enqueued_at" in out

    # The INSERT (third fetchrow call) received the payload in the right shape.
    insert_call = pool.conn.fetchrow.await_args_list[-1]
    sql, *args = insert_call.args
    assert "INSERT INTO memory_write_queue" in sql
    assert args[0] == "hello world"
    assert args[1] == "short"
    assert args[2] == ["foo", "bar"]
    assert args[3] == "insight"
    assert args[4] == "claude"
    # metadata serialised as JSON string
    assert json.loads(args[8]) == {"nested": {"k": 1}}

    # And the wake-up event was set
    assert evt.is_set() is True


async def test_enqueue_handles_none_metadata():
    """Same dedup-probe sequence as above; metadata=None survives the JSON
    wrap as a literal NULL."""
    pool = _FakePool()
    pool.conn.fetchrow.side_effect = [
        None,
        None,
        {"id": uuid4(), "enqueued_at": _now_utc()},
    ]

    with patch.object(write_queue, "get_pool", AsyncMock(return_value=pool)):
        await write_queue.enqueue_memory_write(content="x", metadata=None)

    insert_call = pool.conn.fetchrow.await_args_list[-1]
    args = insert_call.args[1:]
    assert args[8] is None  # metadata arg (9th positional after sql)


# ──────────────────────────────────────────────
# claim_next_pending
# ──────────────────────────────────────────────

async def test_claim_next_pending_returns_none_when_empty():
    pool = _FakePool()
    pool.conn.fetchrow.return_value = None

    with patch.object(write_queue, "get_pool", AsyncMock(return_value=pool)):
        out = await write_queue.claim_next_pending()

    assert out is None
    # Only the SELECT ran; no UPDATE
    pool.conn.execute.assert_not_awaited()


async def test_claim_next_pending_flips_to_processing_and_returns_payload():
    pool = _FakePool()
    row_id = uuid4()
    pool.conn.fetchrow.return_value = {
        "id": row_id,
        "content": "x",
        "summary": None,
        "tags": ["t1"],
        "category": None,
        "source_type": "manual",
        "source_machine": None,
        "source_ref": None,
        "confidence": 1.0,
        "metadata": '{"k": "v"}',  # asyncpg jsonb as str
        "skip_dedup": False,
        "contextual_prefix": None,
        "status": "pending",
        "attempts": 0,
        "max_attempts": 3,
    }

    with patch.object(write_queue, "get_pool", AsyncMock(return_value=pool)):
        out = await write_queue.claim_next_pending()

    assert out is not None
    assert out["content"] == "x"
    assert out["metadata"] == {"k": "v"}  # parsed from JSON string

    # The SELECT must be the FOR UPDATE SKIP LOCKED variant
    select_sql = pool.conn.fetchrow.await_args.args[0]
    assert "FOR UPDATE SKIP LOCKED" in select_sql
    assert "ORDER BY enqueued_at ASC" in select_sql

    # And the row must be flipped to 'processing' in the same transaction
    update_sql = pool.conn.execute.await_args.args[0]
    assert "status = 'processing'" in update_sql
    assert "attempts = attempts + 1" in update_sql


# ──────────────────────────────────────────────
# mark_done / mark_failed
# ──────────────────────────────────────────────

async def test_mark_done_sets_memory_id_and_status():
    pool = _FakePool()
    qid = uuid4()
    mid = uuid4()

    with patch.object(write_queue, "get_pool", AsyncMock(return_value=pool)):
        await write_queue.mark_done(
            str(qid), memory_id=str(mid), result_status="stored",
        )

    sql, *args = pool.conn.execute.await_args.args
    assert "status = 'done'" in sql
    assert "completed_at = now()" in sql
    assert args[0] == qid
    assert args[1] == mid
    assert args[2] == "stored"


async def test_mark_done_accepts_none_memory_id():
    pool = _FakePool()

    with patch.object(write_queue, "get_pool", AsyncMock(return_value=pool)):
        await write_queue.mark_done(str(uuid4()), memory_id=None, result_status="skipped")

    args = pool.conn.execute.await_args.args
    assert args[2] is None  # memory_id
    assert args[3] == "skipped"


async def test_mark_failed_retries_when_under_max_attempts():
    pool = _FakePool()
    qid = uuid4()
    # attempt 1 of 3 → retry with 30s backoff
    pool.conn.fetchrow.return_value = {"attempts": 1, "max_attempts": 3}

    with patch.object(write_queue, "get_pool", AsyncMock(return_value=pool)):
        status = await write_queue.mark_failed(
            str(qid), error="llama-server down", retry=True,
        )

    assert status == "pending"
    update_sql, *args = pool.conn.execute.await_args.args
    assert "status = 'pending'" in update_sql
    assert "next_attempt_at" in update_sql
    assert args[0] == qid
    assert args[1] == "llama-server down"
    # attempt 1 → 30 * 4^0 = 30s
    assert args[2] == "30"


async def test_mark_failed_escalates_backoff():
    pool = _FakePool()
    # attempt 2 of 3 → retry with 120s backoff
    pool.conn.fetchrow.return_value = {"attempts": 2, "max_attempts": 3}

    with patch.object(write_queue, "get_pool", AsyncMock(return_value=pool)):
        await write_queue.mark_failed(str(uuid4()), error="e", retry=True)

    # execute args: (sql, qid, error, backoff)
    args = pool.conn.execute.await_args.args
    # attempt 2 → 30 * 4^1 = 120s (args[3] is the backoff param)
    assert args[3] == "120"


async def test_mark_failed_gives_up_at_max_attempts():
    pool = _FakePool()
    # attempt 3 of 3 → no more retries, mark failed permanently
    pool.conn.fetchrow.return_value = {"attempts": 3, "max_attempts": 3}

    with patch.object(write_queue, "get_pool", AsyncMock(return_value=pool)):
        status = await write_queue.mark_failed(
            str(uuid4()), error="boom", retry=True,
        )

    assert status == "failed"
    sql = pool.conn.execute.await_args.args[0]
    assert "status = 'failed'" in sql


async def test_mark_failed_transient_retries_past_max_attempts():
    """Transient infra faults (DNS blip, connection refused) retry against
    the higher TRANSIENT_MAX_ATTEMPTS ceiling instead of dying at
    max_attempts — 27 rows were permanently lost to a short embedding-DNS
    outage on 2026-08-14."""
    pool = _FakePool()
    # attempt 3 of 3 — a NON-transient failure would give up here
    pool.conn.fetchrow.return_value = {"attempts": 3, "max_attempts": 3}

    with patch.object(write_queue, "get_pool", AsyncMock(return_value=pool)):
        status = await write_queue.mark_failed(
            str(uuid4()), error="[Errno -2] Name or service not known",
            retry=True, transient=True,
        )

    assert status == "pending"
    sql = pool.conn.execute.await_args.args[0]
    assert "status = 'pending'" in sql


async def test_mark_failed_transient_still_bounded():
    """The transient ceiling is a ceiling, not an infinite loop."""
    pool = _FakePool()
    pool.conn.fetchrow.return_value = {"attempts": 10, "max_attempts": 3}

    with patch.object(write_queue, "get_pool", AsyncMock(return_value=pool)):
        status = await write_queue.mark_failed(
            str(uuid4()), error="still down", retry=True, transient=True,
        )

    assert status == "failed"


async def test_mark_failed_respects_retry_false():
    pool = _FakePool()
    pool.conn.fetchrow.return_value = {"attempts": 1, "max_attempts": 3}

    with patch.object(write_queue, "get_pool", AsyncMock(return_value=pool)):
        status = await write_queue.mark_failed(
            str(uuid4()), error="permanent error", retry=False,
        )

    assert status == "failed"


async def test_mark_failed_trims_long_error_messages():
    pool = _FakePool()
    pool.conn.fetchrow.return_value = {"attempts": 1, "max_attempts": 3}
    long_err = "x" * 5000

    with patch.object(write_queue, "get_pool", AsyncMock(return_value=pool)):
        await write_queue.mark_failed(str(uuid4()), error=long_err, retry=True)

    # execute args: (sql, qid, error, backoff) — error is args[2]
    args = pool.conn.execute.await_args.args
    assert len(args[2]) == 2000


# ──────────────────────────────────────────────
# MCP memory_store tool
# ──────────────────────────────────────────────


def _unwrap(fn):
    """Unwrap a FastMCP-decorated tool function so we can call it directly."""
    if hasattr(fn, "fn"):
        return fn.fn
    if hasattr(fn, "__wrapped__"):
        return fn.__wrapped__
    return fn


async def test_memory_store_returns_queued_status_by_default():
    enqueue_mock = AsyncMock(return_value={
        "queue_id": "aaaa1111-2222-3333-4444-555566667777",
        "enqueued_at": "2026-04-11T23:59:59+00:00",
    })

    with patch("nobrainr.db.write_queue.enqueue_memory_write", enqueue_mock):
        fn = _unwrap(mcp_server.memory_store)
        out = await fn(content="small content", tags=["test"])

    assert out["status"] == "queued"
    assert out["queue_id"] == "aaaa1111-2222-3333-4444-555566667777"
    assert "message" in out
    enqueue_mock.assert_awaited_once()


async def test_memory_store_rejects_content_over_limit():
    with patch.object(mcp_server.settings, "max_content_length", 100):
        fn = _unwrap(mcp_server.memory_store)
        out = await fn(content="x" * 200)

    assert "error" in out
    assert "Content too large" in out["error"]


async def test_memory_store_wait_true_polls_and_returns_when_done():
    enqueue_mock = AsyncMock(return_value={
        "queue_id": "aaaa1111-2222-3333-4444-555566667777",
        "enqueued_at": "2026-04-11T23:59:59+00:00",
    })
    # First poll returns pending, second returns done.
    status_mock = AsyncMock(side_effect=[
        {"queue_id": "aaaa1111-2222-3333-4444-555566667777", "status": "pending",
         "attempts": 1, "max_attempts": 3, "memory_id": None, "result_status": None,
         "error_message": None, "enqueued_at": "", "started_at": None,
         "completed_at": None, "next_attempt_at": ""},
        {"queue_id": "aaaa1111-2222-3333-4444-555566667777", "status": "done",
         "attempts": 1, "max_attempts": 3, "memory_id": "bbbb2222-3333-4444-5555-666677778888",
         "result_status": "stored", "error_message": None, "enqueued_at": "",
         "started_at": "", "completed_at": "", "next_attempt_at": ""},
    ])

    with (
        patch("nobrainr.db.write_queue.enqueue_memory_write", enqueue_mock),
        patch("nobrainr.db.write_queue.get_queue_status", status_mock),
    ):
        fn = _unwrap(mcp_server.memory_store)
        out = await fn(content="hi", wait=True)

    assert out["status"] == "done"
    assert out["memory_id"] == "bbbb2222-3333-4444-5555-666677778888"
    assert out["result_status"] == "stored"
    # wait path polled status twice (pending → done)
    assert status_mock.await_count == 2


async def test_memory_store_status_validates_uuid():
    fn = _unwrap(mcp_server.memory_store_status)
    out = await fn(queue_id="not-a-uuid")
    assert "error" in out
    assert "Invalid queue_id format" in out["error"]


async def test_memory_store_status_handles_missing_row():
    status_mock = AsyncMock(return_value=None)
    with patch("nobrainr.db.write_queue.get_queue_status", status_mock):
        fn = _unwrap(mcp_server.memory_store_status)
        out = await fn(queue_id="00000000-0000-0000-0000-000000000000")
    assert "error" in out
    assert out["error"] == "queue_id not found"


# ──────────────────────────────────────────────
# Worker loop integration (happy path + retry)
# ──────────────────────────────────────────────


async def test_worker_happy_path_claims_processes_and_marks_done():
    """One iteration of _memory_write_worker: claim → process → mark_done."""
    from nobrainr.scheduler import Scheduler

    scheduler = Scheduler()
    scheduler._running = True  # one-iteration run

    row = {
        "id": uuid4(),
        "content": "queued content",
        "summary": None,
        "tags": ["foo"],
        "category": "insight",
        "source_type": "claude",
        "source_machine": "bimavo",
        "source_ref": None,
        "confidence": 0.9,
        "metadata": None,
        "skip_dedup": False,
        "contextual_prefix": None,
        "attempts": 1,
        "max_attempts": 3,
        "enqueued_at": _now_utc(),
    }

    async def _stop_scheduler(*args, **kwargs):
        scheduler._running = False
        return False

    # Worker uses a dedicated lifecycle flag (renamed 2026-04-20) so a
    # /api/scheduler/pause doesn't silently kill the queue.
    scheduler._write_queue_running = True

    async def _stop_via_write_queue_flag(*args, **kwargs):
        scheduler._write_queue_running = False
        return False

    claim_mock = AsyncMock(side_effect=[row, None])  # 1st returns row, 2nd stops loop
    mark_done_mock = AsyncMock()
    mark_failed_mock = AsyncMock()
    wait_mock = AsyncMock(side_effect=_stop_via_write_queue_flag)
    # Worker startup runs an orphan reset (Phase G, 2026-04-12) before the
    # main loop. Mocked so no real DB is required.
    reset_stale_mock = AsyncMock(return_value=0)
    store_mock = AsyncMock(return_value={
        "status": "stored",
        "id": "bbbb2222-3333-4444-5555-666677778888",
    })

    with (
        patch("nobrainr.db.write_queue.claim_next_pending", claim_mock),
        patch("nobrainr.db.write_queue.mark_done", mark_done_mock),
        patch("nobrainr.db.write_queue.mark_failed", mark_failed_mock),
        patch("nobrainr.db.write_queue.wait_for_pending", wait_mock),
        patch("nobrainr.db.write_queue.reset_stale_processing", reset_stale_mock),
        patch("nobrainr.services.memory.store_memory_with_extraction", store_mock),
    ):
        await asyncio.wait_for(scheduler._memory_write_worker(), timeout=5.0)

    # The pipeline was called once with the payload from the claimed row
    store_mock.assert_awaited_once()
    call_kwargs = store_mock.await_args.kwargs
    assert call_kwargs["content"] == "queued content"
    assert call_kwargs["tags"] == ["foo"]
    assert call_kwargs["category"] == "insight"
    assert call_kwargs["source_machine"] == "bimavo"

    # And we marked the row done with the returned memory_id
    mark_done_mock.assert_awaited_once()
    done_kwargs = mark_done_mock.await_args.kwargs
    assert done_kwargs["memory_id"] == "bbbb2222-3333-4444-5555-666677778888"
    assert done_kwargs["result_status"] == "stored"

    # No failure path triggered
    mark_failed_mock.assert_not_awaited()


async def test_worker_failure_path_calls_mark_failed_with_retry():
    """Exception from store_memory_with_extraction → mark_failed(retry=True)."""
    from nobrainr.scheduler import Scheduler

    scheduler = Scheduler()
    scheduler._running = True

    row = {
        "id": uuid4(),
        "content": "will fail",
        "summary": None,
        "tags": None,
        "category": None,
        "source_type": "manual",
        "source_machine": None,
        "source_ref": None,
        "confidence": 1.0,
        "metadata": None,
        "skip_dedup": False,
        "contextual_prefix": None,
        "attempts": 1,
        "max_attempts": 3,
        "enqueued_at": _now_utc(),
    }

    scheduler._write_queue_running = True

    async def _stop_via_write_queue_flag(timeout):
        scheduler._write_queue_running = False
        return False

    claim_mock = AsyncMock(side_effect=[row, None])
    mark_done_mock = AsyncMock()
    mark_failed_mock = AsyncMock(return_value="pending")
    wait_mock = AsyncMock(side_effect=_stop_via_write_queue_flag)
    reset_stale_mock = AsyncMock(return_value=0)
    store_mock = AsyncMock(side_effect=RuntimeError("llama-server down"))

    with (
        patch("nobrainr.db.write_queue.claim_next_pending", claim_mock),
        patch("nobrainr.db.write_queue.mark_done", mark_done_mock),
        patch("nobrainr.db.write_queue.mark_failed", mark_failed_mock),
        patch("nobrainr.db.write_queue.wait_for_pending", wait_mock),
        patch("nobrainr.db.write_queue.reset_stale_processing", reset_stale_mock),
        patch("nobrainr.services.memory.store_memory_with_extraction", store_mock),
    ):
        await asyncio.wait_for(scheduler._memory_write_worker(), timeout=5.0)

    mark_done_mock.assert_not_awaited()
    mark_failed_mock.assert_awaited_once()
    fail_kwargs = mark_failed_mock.await_args.kwargs
    assert "llama-server down" in fail_kwargs["error"]
    assert fail_kwargs["retry"] is True
