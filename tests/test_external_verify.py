"""external_verify (E1): quota ceiling gates the run, non-checkable claims
are stamped unverifiable WITHOUT spending Brave quota, and candidate
selection only picks unverified 'fact' claims under the attempt cap."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from nobrainr import scheduler_jobs
from nobrainr.config import settings


def _mock_pool(rows):
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=rows)
    conn.execute = AsyncMock(return_value="UPDATE 1")
    pool = MagicMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=False)
    pool.acquire = MagicMock(return_value=cm)
    return pool, conn


async def test_quota_ceiling_skips_run_when_no_searxng():
    """Without SearXNG the Brave ceiling gates the whole run; with SearXNG
    configured discovery is quota-free and the gate must NOT fire."""
    with (
        patch.object(settings, "searxng_url", ""),
        patch.object(scheduler_jobs, "_ext_month_usage",
                     AsyncMock(return_value=settings.external_verify_quota_ceiling)),
        patch.object(scheduler_jobs, "get_pool") as gp,
        patch.object(scheduler_jobs, "ollama_chat", AsyncMock()) as chat,
    ):
        out = await scheduler_jobs.external_verify()
    assert out["skipped_quota"] == 1
    assert out["searched"] == 0
    gp.assert_not_called()
    chat.assert_not_called()


async def test_searxng_primary_never_touches_brave_quota():
    """Checkable claim + healthy SearXNG: discovery succeeds with zero Brave
    calls and zero quota increments, even when quota is at the ceiling."""
    rows = [{"id": "m1", "text": "The Python requests library is deprecated"}]
    pool, conn = _mock_pool(rows)
    triage = {"claims": [{"i": 0, "checkable": True, "query": "python requests deprecated"}]}
    judge = {"verdict": "refuted", "evidence_quote": "not deprecated at all", "reason": "docs"}
    brave = AsyncMock()
    searx = AsyncMock(return_value=[{"url": "https://example.org/faq", "title": "t", "content": "c"}])
    crawl = AsyncMock(return_value={"results": [{"markdown": {"fit_markdown": "The requests library " + "is actively maintained and not deprecated. " * 20}}]})
    with (
        patch.object(scheduler_jobs, "_ext_month_usage",
                     AsyncMock(return_value=settings.external_verify_quota_ceiling)),
        patch.object(scheduler_jobs, "get_pool", AsyncMock(return_value=pool)),
        patch.object(scheduler_jobs, "_yield_to_live_requests", AsyncMock()),
        patch.object(scheduler_jobs, "ollama_chat",
                     AsyncMock(side_effect=[triage, judge])),
        patch("nobrainr.mcp.server._searxng_search_request", searx),
        patch("nobrainr.mcp.server._brave_search_request", brave),
        patch("nobrainr.mcp.server._count_web_search_use", AsyncMock()) as counter,
        patch("nobrainr.crawler.client.crawl4ai_request", crawl),
    ):
        out = await scheduler_jobs.external_verify()
    assert out["searched"] == 1
    assert out["refuted"] == 1
    brave.assert_not_called()
    counter.assert_not_called()


async def test_candidate_sql_invariants_and_unverifiable_stamp():
    rows = [{"id": "m1", "text": "personal note about my cat"}]
    pool, conn = _mock_pool(rows)
    triage = {"claims": [{"i": 0, "checkable": False, "query": ""}]}
    brave = AsyncMock()
    with (
        patch.object(scheduler_jobs, "_ext_month_usage", AsyncMock(return_value=0)),
        patch.object(scheduler_jobs, "get_pool", AsyncMock(return_value=pool)),
        patch.object(scheduler_jobs, "_yield_to_live_requests", AsyncMock()),
        patch.object(scheduler_jobs, "ollama_chat", AsyncMock(return_value=triage)),
        patch("nobrainr.mcp.server._brave_search_request", brave),
        patch("nobrainr.mcp.server._count_web_search_use", AsyncMock()) as counter,
    ):
        out = await scheduler_jobs.external_verify()

    sel = conn.fetch.call_args.args[0]
    assert "claim_kind = 'fact'" in sel
    assert "external_verified_at IS NULL" in sel
    assert "ext_verify_attempts" in sel and "< 2" in sel
    assert "tier <= 2" in sel

    assert out["unverifiable"] == 1
    stamp = conn.execute.call_args.args[0]
    assert "external_verdict = 'unverifiable'" in stamp
    brave.assert_not_called()
    counter.assert_not_called()


async def test_internal_scope_never_reaches_the_web():
    """The 2026-08-11 false-refutation case: a memory about OUR machines
    must be stamped unverifiable even when the LLM triage says checkable."""
    rows = [{"id": "m1", "text": "Workserver hooks: session-start, enhance-prompt run on 10.10.10.10"}]
    pool, conn = _mock_pool(rows)
    triage = {"claims": [{"i": 0, "checkable": True, "query": "claude code hooks"}]}
    brave = AsyncMock()
    with (
        patch.object(scheduler_jobs, "_ext_month_usage", AsyncMock(return_value=0)),
        patch.object(scheduler_jobs, "get_pool", AsyncMock(return_value=pool)),
        patch.object(scheduler_jobs, "_yield_to_live_requests", AsyncMock()),
        patch.object(scheduler_jobs, "ollama_chat", AsyncMock(return_value=triage)),
        patch("nobrainr.mcp.server._brave_search_request", brave),
        patch("nobrainr.mcp.server._count_web_search_use", AsyncMock()) as counter,
    ):
        out = await scheduler_jobs.external_verify()

    assert out["unverifiable"] == 1
    assert out["searched"] == 0
    brave.assert_not_called()
    counter.assert_not_called()

    # Regex sanity on the exact production markers
    assert scheduler_jobs._EXT_INTERNAL_RE.search("bimavo wizard uses qwen3-8b")
    assert scheduler_jobs._EXT_INTERNAL_RE.search("endpoint at 10.10.10.12:8080")
    assert not scheduler_jobs._EXT_INTERNAL_RE.search(
        "Qwen3.6-27B scores 77.2 on SWE-bench Verified"
    )
