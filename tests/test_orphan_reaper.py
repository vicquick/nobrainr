"""Write-queue orphan reaper (Phase G, v6.9).

When the nobrainr container is killed mid-task (Coolify rotation, OOM,
manual stop), any row the old worker claimed stays in ``status='processing'``
with no alive worker to finish it. The new worker's claim loop only looks
at ``status='pending'`` rows, so orphans would sit forever.

Phase G adds ``write_queue.reset_stale_processing()``, called once at
worker startup in ``scheduler._memory_write_worker``.

Tests lock in:
  1. the SQL shape (only touches 'processing' rows older than the threshold)
  2. the return value (number of rows reset)
  3. the ``attempts`` counter is NOT decremented (crash still counts against
     retry budget — otherwise a flaky Coolify could let a broken task retry
     forever)
  4. the threshold is parameterizable
  5. no orphans → returns 0 cleanly
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nobrainr.db import write_queue


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────


async def _capture_execute(update_count: int = 0, stale_minutes: int = 10):
    """Run reset_stale_processing against a mocked pool and return
    the captured SQL + params + the function's return value."""
    captured: dict = {}

    async def fake_execute(sql, *args):
        captured["sql"] = sql
        captured["args"] = args
        return f"UPDATE {update_count}"

    mock_conn = AsyncMock()
    mock_conn.execute = AsyncMock(side_effect=fake_execute)
    acquire_ctx = MagicMock()
    acquire_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
    acquire_ctx.__aexit__ = AsyncMock(return_value=None)
    mock_pool = MagicMock()
    mock_pool.acquire = MagicMock(return_value=acquire_ctx)

    with patch.object(write_queue, "get_pool", AsyncMock(return_value=mock_pool)):
        n = await write_queue.reset_stale_processing(stale_minutes=stale_minutes)

    return captured, n


# ──────────────────────────────────────────────
# SQL shape
# ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reset_targets_only_processing_rows():
    """The UPDATE must be filtered to status='processing' — any other status
    is untouched."""
    captured, _ = await _capture_execute(update_count=3)
    sql = captured["sql"]
    assert "WHERE status = 'processing'" in sql
    assert "status = 'pending'" not in sql.split("WHERE")[1]
    assert "status = 'failed'" not in sql.split("WHERE")[1]
    assert "status = 'done'" not in sql.split("WHERE")[1]


@pytest.mark.asyncio
async def test_reset_targets_only_stale_rows():
    """The UPDATE must include a started_at age filter so fresh in-flight
    work is NOT touched — only rows genuinely orphaned."""
    captured, _ = await _capture_execute()
    sql = captured["sql"]
    assert "started_at IS NOT NULL" in sql
    assert "started_at <" in sql
    # The exact interval form is ``($1 || ' minutes')::interval``.
    # Match on the suffix that distinguishes it from other interval shapes.
    assert "minutes')::interval" in sql


@pytest.mark.asyncio
async def test_reset_flips_to_pending():
    """The UPDATE must set status back to 'pending' so the new worker
    claims the row."""
    captured, _ = await _capture_execute()
    sql = captured["sql"]
    assert "SET status = 'pending'" in sql


@pytest.mark.asyncio
async def test_reset_clears_started_at():
    """started_at must be cleared so the new worker's claim_next_pending
    sets a fresh timestamp — otherwise the row would look immediately
    stale on the next pass and loop forever."""
    captured, _ = await _capture_execute()
    sql = captured["sql"]
    assert "started_at = NULL" in sql


@pytest.mark.asyncio
async def test_reset_does_NOT_decrement_attempts():
    """Critical: the ``attempts`` counter is intentionally NOT decremented
    on recovery. The crash still counts against the retry budget — a
    flaky Coolify must not let a broken task retry forever. If this test
    starts failing, someone has likely introduced an attempts -= 1 which
    reintroduces the infinite-retry footgun."""
    captured, _ = await _capture_execute()
    sql = captured["sql"]
    assert "attempts = attempts - 1" not in sql
    assert "attempts = 0" not in sql
    assert "attempts =" not in sql  # No attempts mutation at all
    assert "attempts" not in sql.split("SET")[1].split("WHERE")[0], \
        "attempts must not appear in the SET clause"


# ──────────────────────────────────────────────
# Return value + threshold
# ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_return_value_matches_update_count():
    """Whatever Postgres returns in 'UPDATE <n>' is what we return."""
    _, n = await _capture_execute(update_count=7)
    assert n == 7


@pytest.mark.asyncio
async def test_no_orphans_returns_zero():
    """Zero rows reset is a perfectly normal return, not an error."""
    _, n = await _capture_execute(update_count=0)
    assert n == 0


@pytest.mark.asyncio
async def test_threshold_parameterized():
    """The stale_minutes param flows through to the SQL."""
    captured, _ = await _capture_execute(stale_minutes=30)
    args = captured["args"]
    assert "30" in args  # Threshold passed as interval string


@pytest.mark.asyncio
async def test_default_threshold_is_10_minutes():
    """Default is 10 min (2x the p95 work duration, safe for multi-worker)."""
    captured: dict = {}

    async def fake_execute(sql, *args):
        captured["args"] = args
        return "UPDATE 0"

    mock_conn = AsyncMock()
    mock_conn.execute = AsyncMock(side_effect=fake_execute)
    acquire_ctx = MagicMock()
    acquire_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
    acquire_ctx.__aexit__ = AsyncMock(return_value=None)
    mock_pool = MagicMock()
    mock_pool.acquire = MagicMock(return_value=acquire_ctx)

    with patch.object(write_queue, "get_pool", AsyncMock(return_value=mock_pool)):
        await write_queue.reset_stale_processing()  # No args → default

    assert "10" in captured["args"]


@pytest.mark.asyncio
async def test_garbage_update_result_returns_zero():
    """If Postgres returns something we can't parse (edge case), we return
    0 rather than crashing the worker startup."""
    captured: dict = {}

    async def fake_execute(sql, *args):
        captured["sql"] = sql
        return "SOME GARBAGE"

    mock_conn = AsyncMock()
    mock_conn.execute = AsyncMock(side_effect=fake_execute)
    acquire_ctx = MagicMock()
    acquire_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
    acquire_ctx.__aexit__ = AsyncMock(return_value=None)
    mock_pool = MagicMock()
    mock_pool.acquire = MagicMock(return_value=acquire_ctx)

    with patch.object(write_queue, "get_pool", AsyncMock(return_value=mock_pool)):
        n = await write_queue.reset_stale_processing()

    assert n == 0  # Don't crash, don't propagate the parse error


# ──────────────────────────────────────────────
# Scheduler integration — verify the call is wired
# ──────────────────────────────────────────────


def test_scheduler_calls_reset_stale_processing_on_worker_startup():
    """The _memory_write_worker body must contain a call to
    reset_stale_processing BEFORE the while loop. This is a static check
    against the source — spinning up the actual async worker against a
    mocked pool is more test machinery than it's worth.

    If this test fails after a refactor, make sure the orphan reset is
    still called on worker startup, or Phase G's correctness is gone.
    """
    import inspect
    from nobrainr import scheduler as scheduler_mod

    src = inspect.getsource(scheduler_mod.Scheduler._memory_write_worker)
    assert "reset_stale_processing" in src, \
        "_memory_write_worker must call write_queue.reset_stale_processing on startup"

    # The call must come BEFORE the `while self._running:` loop —
    # otherwise it runs every iteration, not once at startup.
    reset_idx = src.find("reset_stale_processing")
    while_idx = src.find("while self._running:")
    assert reset_idx > 0 and while_idx > 0
    assert reset_idx < while_idx, \
        "reset_stale_processing must be called BEFORE the while loop (once at startup, not every iteration)"
