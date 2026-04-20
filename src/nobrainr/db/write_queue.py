"""memory_store write queue — decouples acceptance from processing.

Design rationale
----------------
``store_memory_with_extraction`` synchronously calls ``decide_write_action``,
which calls ``ollama_chat`` with the default ``DEFAULT_LLM_TIMEOUT = 600s``.
On a contended GPU (scheduler LLM jobs + reranker + dedup all fighting one
llama-server) the hot path blows past the MCP client's timeout budget and
memory_store silently drops writes — the exact failure observed on
2026-04-11 when ``lesson_classifier`` fired its initial run.

The queue fixes this by splitting the flow:

1. **Enqueue** — one INSERT into ``memory_write_queue``. No LLM, no
   embedding, no dedup. Typical latency <50ms. The MCP tool returns a
   ``queue_id`` and the caller is free.

2. **Drain** — a dedicated scheduler loop (``_memory_write_worker`` in
   ``scheduler.py``) claims the oldest pending row via ``FOR UPDATE SKIP
   LOCKED``, runs it through ``store_memory_with_extraction`` under the
   shared LLM semaphore, and updates the row to ``done`` or ``failed``
   (with exponential-backoff retry).

3. **Poll** — callers that care about the result can call
   ``memory_store_status(queue_id)`` to see ``{status, memory_id, ...}``.

Wake-up signalling
------------------
The worker would otherwise poll every ~2s, which is fine for idle latency
but wastes one query per cycle. ``enqueue_memory_write`` sets a module-
level ``asyncio.Event`` after a successful INSERT, and the worker's idle
path ``await``-s that event. Fresh writes pop the worker into action
immediately. Multi-worker / multi-process safety is preserved by
``FOR UPDATE SKIP LOCKED`` in the claim query — the event is just a
low-latency hint, not a lock.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from uuid import UUID

from nobrainr.db.pool import get_pool

logger = logging.getLogger("nobrainr.write_queue")


# ──────────────────────────────────────────────
# Wake-up signalling
# ──────────────────────────────────────────────

_write_pending_event: asyncio.Event | None = None


def _get_event() -> asyncio.Event:
    global _write_pending_event
    if _write_pending_event is None:
        _write_pending_event = asyncio.Event()
    return _write_pending_event


def signal_pending() -> None:
    """Wake the worker loop. Called by enqueue after a successful INSERT.

    Safe to call from any coroutine in the same event loop. Outside the
    loop (e.g. during test teardown) it degrades to a no-op — the worker
    will pick up the pending row on its next poll cycle anyway.
    """
    try:
        _get_event().set()
    except RuntimeError:
        pass


async def wait_for_pending(timeout: float = 2.0) -> bool:
    """Sleep until a write is signalled, or ``timeout`` seconds — whichever
    comes first. Returns True if signalled, False on timeout.
    """
    evt = _get_event()
    try:
        await asyncio.wait_for(evt.wait(), timeout=timeout)
        evt.clear()
        return True
    except asyncio.TimeoutError:
        return False


# ──────────────────────────────────────────────
# Queue operations
# ──────────────────────────────────────────────


async def enqueue_memory_write(
    content: str,
    *,
    summary: str | None = None,
    tags: list[str] | None = None,
    category: str | None = None,
    source_type: str = "manual",
    source_machine: str | None = None,
    source_ref: str | None = None,
    confidence: float = 1.0,
    metadata: dict | None = None,
    skip_dedup: bool = False,
    contextual_prefix: str | None = None,
) -> dict[str, str]:
    """Enqueue a pending memory write. Fast path — single INSERT, no LLM.

    Cheap SHA256 pre-check (2026-04-20): if an identical-content row
    already exists as a pending/processing queue row OR was stored as a
    memory in the last 7 days, short-circuit and return that row's id
    instead of enqueuing a true duplicate. Saves an entire LLM dedup call
    (30-90s under n_parallel=1) per near-instant dupe. Same-content detection
    is strict byte equality — the LLM semantic dedup remains for true
    rewording / paraphrase cases.

    Returns ``{queue_id, enqueued_at, deduped?}``. Wakes the worker loop
    via ``signal_pending`` so the write starts processing immediately
    instead of waiting for the next poll tick.
    """
    import hashlib

    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

    pool = await get_pool()
    async with pool.acquire() as conn:
        # Content-hash dedup: existing queue row (any active status)?
        existing = await conn.fetchrow(
            """
            SELECT id, status, enqueued_at
            FROM memory_write_queue
            WHERE encode(sha256(content::bytea), 'hex') = $1
              AND status IN ('pending', 'processing', 'done')
              AND enqueued_at > now() - interval '7 days'
            ORDER BY enqueued_at ASC
            LIMIT 1
            """,
            content_hash,
        )
        if existing:
            logger.info(
                "enqueue dedup: identical content already in queue (%s, status=%s) — returning existing queue_id",
                existing["id"], existing["status"],
            )
            return {
                "queue_id": str(existing["id"]),
                "enqueued_at": existing["enqueued_at"].isoformat(),
                "deduped": True,
            }
        # Content-hash dedup: already stored as a memory recently?
        stored = await conn.fetchrow(
            """
            SELECT id, created_at
            FROM memories
            WHERE encode(sha256(content::bytea), 'hex') = $1
              AND created_at > now() - interval '7 days'
            ORDER BY created_at DESC
            LIMIT 1
            """,
            content_hash,
        )
        if stored:
            logger.info(
                "enqueue dedup: identical content already stored as memory %s — skipping",
                stored["id"],
            )
            return {
                "queue_id": str(stored["id"]),
                "enqueued_at": stored["created_at"].isoformat(),
                "deduped": True,
            }

        row = await conn.fetchrow(
            """
            INSERT INTO memory_write_queue (
                content, summary, tags, category, source_type,
                source_machine, source_ref, confidence, metadata,
                skip_dedup, contextual_prefix
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10, $11)
            RETURNING id, enqueued_at
            """,
            content,
            summary,
            tags,
            category,
            source_type,
            source_machine,
            source_ref,
            confidence,
            json.dumps(metadata) if metadata else None,
            skip_dedup,
            contextual_prefix,
        )
    signal_pending()
    return {
        "queue_id": str(row["id"]),
        "enqueued_at": row["enqueued_at"].isoformat(),
    }


async def enqueue_document_chunks(
    content: str,
    *,
    title: str | None = None,
    summary: str | None = None,
    tags: list[str] | None = None,
    category: str | None = None,
    source_type: str = "document",
    source_machine: str | None = None,
    source_ref: str | None = None,
    confidence: float = 0.8,
    metadata: dict | None = None,
    max_chars: int | None = None,
    overlap: int | None = None,
) -> dict[str, Any]:
    """Enqueue every chunk of a long document as individual memory writes.

    Mirrors :func:`nobrainr.services.memory.store_document_chunked` but uses the
    queue path — each chunk lands as its own row in ``memory_write_queue``
    with a shared ``document_id`` in metadata so the chunks can be rejoined
    downstream.

    Contextual prefixes are deliberately NOT generated on the hot path. The
    old ``store_document_chunked`` would fire one ``ollama_chat`` call per
    chunk to produce an Anthropic-style contextual prefix — each one with a
    600s timeout — which is exactly the 600s hot-path bug PR #18 fixed for
    ``memory_store``. The existing ``contextual_prefix_backfill`` scheduler
    job already backfills prefixes for chunks stored without one, so we
    accept zero-latency enqueue now + async prefix enrichment later.

    Returns::

        {
            "status": "queued",
            "queue_ids": [...],           # one per chunk (or one total for short content)
            "chunks": int,                # number of chunks (1 if content was short)
            "document_id": str | None,    # None for single-chunk writes
        }

    For empty or unchunkable input, returns ``{"error": "..."}``.
    """
    from nobrainr.config import settings
    from nobrainr.services.chunking import chunk_text
    import uuid as _uuid

    trimmed = (content or "").strip()
    if not trimmed:
        return {"error": "Empty content"}

    # Short content — still goes through the queue but as a single row so
    # callers get a consistent ``queue_ids`` shape.
    if len(trimmed) <= settings.chunk_threshold:
        enq = await enqueue_memory_write(
            content=trimmed,
            summary=summary or (f"Document: {title}" if title else None),
            tags=tags,
            category=category,
            source_type=source_type,
            source_machine=source_machine,
            source_ref=source_ref,
            confidence=confidence,
            metadata=metadata,
        )
        return {
            "status": "queued",
            "queue_ids": [enq["queue_id"]],
            "chunks": 1,
            "document_id": None,
        }

    chunks = chunk_text(trimmed, max_chars=max_chars, overlap=overlap)
    if not chunks:
        return {"error": "Chunking produced no output"}

    document_id = str(_uuid.uuid4())
    queue_ids: list[str] = []

    for chunk in chunks:
        chunk_meta = dict(metadata or {})
        chunk_meta.update(
            {
                "document_id": document_id,
                "chunk_index": chunk.index,
                "chunk_total": chunk.total,
                "chunk_offset": chunk.char_offset,
            }
        )
        if title:
            chunk_meta["document_title"] = title

        chunk_summary_base = title or summary or source_ref or "Document chunk"
        chunk_summary = (
            f"{chunk_summary_base} [{chunk.index + 1}/{chunk.total}]"
            if chunk.total > 1
            else chunk_summary_base
        )

        enq = await enqueue_memory_write(
            content=chunk.text,
            summary=chunk_summary[:200],
            tags=tags,
            category=category,
            source_type=source_type,
            source_machine=source_machine,
            source_ref=source_ref,
            confidence=confidence,
            metadata=chunk_meta,
            # Chunks are unique by offset within a document; deduping them
            # would cause false merges across documents that happen to share
            # a common snippet.
            skip_dedup=True,
        )
        queue_ids.append(enq["queue_id"])

    return {
        "status": "queued",
        "queue_ids": queue_ids,
        "chunks": len(chunks),
        "document_id": document_id,
    }


async def reset_stale_processing(stale_minutes: int = 10) -> int:
    """Reset orphan 'processing' rows whose worker died mid-task.

    When the nobrainr container is killed (Coolify redeploy, OOM, manual
    stop) any row claimed by the old worker stays in ``status='processing'``
    with no heartbeat and no alive worker to finish it. The new worker's
    claim query at :func:`claim_next_pending` only inspects rows with
    ``status='pending'``, so orphans sit in the queue forever unless
    someone manually flips them back (observed 4+ times in the 2026-04-12
    overnight mission when Coolify auto-deploys cascaded).

    This function is meant to be called ONCE at worker startup (from
    ``scheduler._memory_write_worker``). It finds rows that have been
    in ``status='processing'`` for longer than ``stale_minutes`` minutes
    and flips them back to ``pending``, clearing ``started_at`` so the
    new worker's claim sets a fresh one. The ``attempts`` counter is
    intentionally NOT decremented — the crash still counts against the
    retry budget because otherwise a flaky Coolify could let a genuinely
    broken task retry forever.

    The ``stale_minutes`` threshold defaults to 10 minutes, which is
    generous compared to p95 work duration (observed ~160s). Setting it
    too aggressive would risk resetting legitimately-running work in
    future multi-worker deployments; too lax would leave orphans sitting
    across container lifecycles. 10 minutes is the current compromise.

    Returns the number of rows reset.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE memory_write_queue
            SET status = 'pending',
                started_at = NULL
            WHERE status = 'processing'
              AND started_at IS NOT NULL
              AND started_at < now() - ($1 || ' minutes')::interval
            """,
            str(stale_minutes),
        )
    try:
        n = int(result.split()[-1])
    except (IndexError, ValueError):
        return 0
    if n > 0:
        logger.warning(
            "reset_stale_processing: reset %d orphan rows (worker crash recovery)", n
        )
    return n


async def claim_next_pending() -> dict[str, Any] | None:
    """Claim the oldest pending row whose ``next_attempt_at`` has passed.

    Uses ``FOR UPDATE SKIP LOCKED`` so a second worker (if one is ever
    added, or multi-instance deploy) can claim a different row without
    blocking. Returns the row as a dict with the payload ready to pass
    to ``store_memory_with_extraction``, or ``None`` if nothing to claim.

    On claim, the row is flipped to ``status='processing'`` and
    ``attempts`` is incremented by 1, all inside the same transaction
    so an interrupted worker never holds a row in a half-claimed state.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT *
                FROM memory_write_queue
                WHERE status = 'pending'
                  AND next_attempt_at <= now()
                ORDER BY enqueued_at ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
                """
            )
            if row is None:
                return None
            await conn.execute(
                """
                UPDATE memory_write_queue
                SET status = 'processing',
                    started_at = now(),
                    attempts = attempts + 1
                WHERE id = $1
                """,
                row["id"],
            )

    result: dict[str, Any] = dict(row)
    # asyncpg returns jsonb as a str — parse it back to dict for the worker.
    md = result.get("metadata")
    if isinstance(md, str):
        try:
            result["metadata"] = json.loads(md)
        except json.JSONDecodeError:
            result["metadata"] = None
    return result


async def mark_done(
    queue_id: str | UUID,
    *,
    memory_id: str | UUID | None,
    result_status: str,
) -> None:
    """Mark a claimed row as completed successfully."""
    pool = await get_pool()
    qid = UUID(queue_id) if isinstance(queue_id, str) else queue_id
    mid: UUID | None = None
    if memory_id is not None:
        mid = UUID(memory_id) if isinstance(memory_id, str) else memory_id
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE memory_write_queue
            SET status = 'done',
                completed_at = now(),
                memory_id = $2,
                result_status = $3,
                error_message = NULL
            WHERE id = $1
            """,
            qid,
            mid,
            result_status,
        )


async def mark_failed(
    queue_id: str | UUID,
    *,
    error: str,
    retry: bool = True,
) -> str:
    """Mark a claimed row as failed.

    If ``retry=True`` AND ``attempts < max_attempts``, the row is reset to
    ``pending`` with an exponential-backoff delay on ``next_attempt_at``
    (30s, 2m, 8m). Otherwise it's marked ``failed`` permanently.

    Returns the final status (`'pending'` on retry or `'failed'` on give-up)
    so the caller can log it.
    """
    pool = await get_pool()
    qid = UUID(queue_id) if isinstance(queue_id, str) else queue_id
    trimmed_error = (error or "")[:2000]

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT attempts, max_attempts FROM memory_write_queue WHERE id = $1",
            qid,
        )
        if row is None:
            return "missing"

        should_retry = retry and row["attempts"] < row["max_attempts"]

        if should_retry:
            # Exponential backoff: attempt 1 → 30s, 2 → 120s, 3 → 480s
            backoff_seconds = 30 * (4 ** (row["attempts"] - 1))
            await conn.execute(
                """
                UPDATE memory_write_queue
                SET status = 'pending',
                    error_message = $2,
                    next_attempt_at = now() + ($3 || ' seconds')::interval
                WHERE id = $1
                """,
                qid,
                trimmed_error,
                str(backoff_seconds),
            )
            return "pending"

        await conn.execute(
            """
            UPDATE memory_write_queue
            SET status = 'failed',
                completed_at = now(),
                error_message = $2
            WHERE id = $1
            """,
            qid,
            trimmed_error,
        )
        return "failed"


async def get_queue_status(queue_id: str | UUID) -> dict[str, Any] | None:
    """Return the current state of a queue row, or ``None`` if not found."""
    pool = await get_pool()
    qid = UUID(queue_id) if isinstance(queue_id, str) else queue_id
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, status, attempts, max_attempts,
                   memory_id, result_status, error_message,
                   enqueued_at, started_at, completed_at, next_attempt_at
            FROM memory_write_queue
            WHERE id = $1
            """,
            qid,
        )
        if row is None:
            return None
        return {
            "queue_id": str(row["id"]),
            "status": row["status"],
            "attempts": row["attempts"],
            "max_attempts": row["max_attempts"],
            "memory_id": str(row["memory_id"]) if row["memory_id"] else None,
            "result_status": row["result_status"],
            "error_message": row["error_message"],
            "enqueued_at": row["enqueued_at"].isoformat() if row["enqueued_at"] else None,
            "started_at": row["started_at"].isoformat() if row["started_at"] else None,
            "completed_at": row["completed_at"].isoformat() if row["completed_at"] else None,
            "next_attempt_at": row["next_attempt_at"].isoformat() if row["next_attempt_at"] else None,
        }


async def get_queue_stats() -> dict[str, int]:
    """Return queue depth counts by status for the dashboard widget."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                count(*) FILTER (WHERE status = 'pending')    AS pending,
                count(*) FILTER (WHERE status = 'processing') AS processing,
                count(*) FILTER (WHERE status = 'done')       AS done,
                count(*) FILTER (WHERE status = 'failed')     AS failed,
                count(*) FILTER (WHERE status = 'pending' AND attempts > 0) AS retrying
            FROM memory_write_queue
            """
        )
        return {k: int(v or 0) for k, v in dict(row).items()}


async def retry_failed(queue_id: str | UUID) -> bool:
    """Fully reset a failed (or stuck) queue row back to pending.

    Clears ALL timestamp fields so the worker's claim query picks it up
    immediately. Returns True if the row was found and reset, False if not found.

    This is the correct way to manually retry — partial resets that leave
    ``started_at``/``completed_at`` set cause the row to be stuck even when
    ``status='pending'``.
    """
    pool = await get_pool()
    qid = UUID(queue_id) if isinstance(queue_id, str) else queue_id
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE memory_write_queue
            SET status         = 'pending',
                attempts       = 0,
                error_message  = NULL,
                next_attempt_at = now(),
                started_at     = NULL,
                completed_at   = NULL
            WHERE id = $1
            """,
            qid,
        )
    updated = int(result.split()[-1])
    if updated:
        signal_pending()
    return updated > 0


async def prune_old_completed(older_than_hours: int = 72) -> int:
    """Retention sweep — delete done/failed rows older than N hours.

    Called by the existing maintenance job so the queue table stays
    small. Returns the number of rows deleted.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            DELETE FROM memory_write_queue
            WHERE status IN ('done', 'failed')
              AND completed_at IS NOT NULL
              AND completed_at < now() - ($1 || ' hours')::interval
            """,
            str(older_than_hours),
        )
    try:
        return int(result.split()[-1])
    except (IndexError, ValueError):
        return 0
