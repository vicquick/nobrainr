"""Benchmarks run their LLM calls on the remote leg only (vic 2026-10-17).

The overnight bench (23:00-06:00) used to answer + judge on the local 27b,
colliding with GPU work in the same window. Now every chat call goes to
the remote OpenAI-compatible endpoint (Hetzner Experiments); embeddings
and rerank stay local (pinned in VRAM, nothing gets loaded).

No local fallback, ever: a local chat call raises BenchAbort, a
BaseException, so the scripts' per-item ``except Exception`` handlers
cannot swallow it.

Remote failures are graded. Some prompts hang reproducibly at Hetzner's
gateway (2026-10-17: a 364-char judge prompt timed out on every retry,
with or without schema, at any temperature, while every other call took
~1s), so one stuck item is scored as that item's error (RemoteItemError,
caught per item) — never re-routed locally. Only CONSECUTIVE_FAILS_ABORT
failed calls in a row (remote really down) abort the run.

Results carry ``llm`` = LLM_LABEL so they are never compared against older
runs made with the local model.
"""

from __future__ import annotations

import asyncio
import sys

REMOTE_RETRIES = 1
CONSECUTIVE_FAILS_ABORT = 3


class BenchAbort(BaseException):
    """Stop the benchmark: the remote-only contract was broken."""


class RemoteItemError(RuntimeError):
    """One remote call failed after retries; the item is scored as error."""


LLM_LABEL = "unset"


def enforce_remote_only() -> str:
    global LLM_LABEL
    from nobrainr.config import settings
    from nobrainr.extraction import llm

    if not (settings.llm_remote_url and settings.llm_remote_model):
        print("bench: no remote LLM configured — aborting (no local fallback)",
              file=sys.stderr)
        raise SystemExit(2)

    # Force the routing regardless of the container's env: every non-live
    # call goes remote, no night window, no local fallback.
    settings.llm_remote_mode = "split"
    settings.llm_local_batch_hours = ""
    settings.llm_batch_local_fallback = False

    async def _no_local(*_a, **_kw):
        raise BenchAbort("bench tried a LOCAL LLM call — aborting")

    remote = llm._remote_chat

    consecutive = 0

    async def _remote_or_abort(*a, **kw):
        nonlocal consecutive
        last = None
        for attempt in range(1 + REMOTE_RETRIES):
            try:
                out = await remote(*a, **kw)
                consecutive = 0
                return out
            except Exception as exc:
                last = exc
                print(f"bench: remote attempt {attempt + 1} failed "
                      f"({type(exc).__name__}: {exc})", file=sys.stderr, flush=True)
                await asyncio.sleep(10 * (attempt + 1))
        consecutive += 1
        if consecutive >= CONSECUTIVE_FAILS_ABORT:
            raise BenchAbort(
                f"remote LLM failed on {consecutive} calls in a row "
                f"({type(last).__name__}: {last}) — aborting, no local fallback"
            ) from last
        raise RemoteItemError(
            f"remote call failed {1 + REMOTE_RETRIES}x "
            f"({type(last).__name__}) — item scored as error"
        ) from last

    llm._ollama_chat_local = _no_local
    llm._remote_chat = _remote_or_abort

    host = "hetzner" if "hetzner" in settings.llm_remote_url else "remote"
    LLM_LABEL = f"{host}-{settings.llm_remote_model.lower()}"
    print(f"bench: LLM remote-only, llm={LLM_LABEL}", flush=True)
    return LLM_LABEL


SCRATCH_DB = "nobrainr_lme"


def require_scratch_db() -> None:
    """Ingesting benches must never write into the production DB.

    The overnight bench used to ingest into prod and delete afterwards;
    the prod scheduler could extract entities from those rows in between
    (2026-10-17). Run with NOBRAINR_DATABASE_URL pointing at nobrainr_lme.
    """
    from urllib.parse import urlparse

    from nobrainr.config import settings

    db = urlparse(settings.database_url).path.lstrip("/")
    if db != SCRATCH_DB:
        print(f"bench: refusing to ingest into database {db!r} — set "
              f"NOBRAINR_DATABASE_URL to the {SCRATCH_DB} scratch DB",
              file=sys.stderr)
        raise SystemExit(3)
    print(f"bench: ingest DB = {db}", flush=True)
