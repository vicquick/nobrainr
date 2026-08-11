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


async def test_quota_ceiling_skips_run_entirely():
    with (
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
