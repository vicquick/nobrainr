"""Benchmarks run their LLM calls on the remote leg only (vic 2026-10-17).

The overnight bench (23:00-06:00) used to answer + judge on the local 27b,
colliding with GPU work in the same window. Now every chat call goes to
the remote OpenAI-compatible endpoint (Hetzner Experiments); embeddings
and rerank stay local (pinned in VRAM, nothing gets loaded).

No local fallback: a local chat call or a failed remote call raises
BenchAbort, a BaseException, so the scripts' per-item ``except Exception``
handlers cannot swallow it — the whole run stops instead of quietly
scoring a half-local result.

Results carry ``llm`` = LLM_LABEL so they are never compared against older
runs made with the local model.
"""

from __future__ import annotations

import asyncio
import sys

REMOTE_RETRIES = 2


class BenchAbort(BaseException):
    """Stop the benchmark: the remote-only contract was broken."""


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

    async def _remote_or_abort(*a, **kw):
        # Hetzner ReadTimeouts are sporadic (~3% of calls in prod), so one
        # miss must not end a 7h run: retry the REMOTE leg, then abort.
        last = None
        for attempt in range(1 + REMOTE_RETRIES):
            try:
                return await remote(*a, **kw)
            except Exception as exc:
                last = exc
                print(f"bench: remote attempt {attempt + 1} failed "
                      f"({type(exc).__name__}: {exc})", file=sys.stderr, flush=True)
                await asyncio.sleep(10 * (attempt + 1))
        raise BenchAbort(
            f"remote LLM failed {1 + REMOTE_RETRIES}x ({type(last).__name__}: "
            f"{last}) — aborting, no local fallback"
        ) from last

    llm._ollama_chat_local = _no_local
    llm._remote_chat = _remote_or_abort

    host = "hetzner" if "hetzner" in settings.llm_remote_url else "remote"
    LLM_LABEL = f"{host}-{settings.llm_remote_model.lower()}"
    print(f"bench: LLM remote-only, llm={LLM_LABEL}", flush=True)
    return LLM_LABEL
