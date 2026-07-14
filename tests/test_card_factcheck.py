"""card_factcheck (M1 HEART PLAN): cards get a published_accuracy number.

Locks in:
- _probe_verdict: mechanical lane maps probe results to verdicts,
  skips invalid regexes and probe-errors (falls through to LLM lane)
- _accuracy: supported/(supported+contradicted), None when undecidable
- _factcheck_card: claim extraction -> two-lane verdicts -> accuracy
  stamp + rebuild trigger (source_max_updated reset below threshold)
- uncheckable claims are skipped, never counted against accuracy
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

from nobrainr import scheduler_jobs
from nobrainr.scheduler_jobs import _accuracy, _factcheck_card, _probe_verdict


# ──────────────────────────────────────────────
# _probe_verdict (pure)
# ──────────────────────────────────────────────


def test_probe_verified_supports():
    probes = [{"claim_pattern": r"llama-swap.*port 8080", "last_result": "verified"}]
    assert _probe_verdict("llama-swap serves on port 8080", probes) == "supported"


def test_probe_mismatch_contradicts():
    probes = [{"claim_pattern": r"gemma 4", "last_result": "mismatch"}]
    assert _probe_verdict("The current LLM is Gemma 4 26B", probes) == "contradicted"


def test_probe_error_falls_through():
    probes = [{"claim_pattern": r"gemma", "last_result": "probe-error"}]
    assert _probe_verdict("gemma is deployed", probes) is None


def test_no_match_falls_through():
    probes = [{"claim_pattern": r"postgres 17", "last_result": "verified"}]
    assert _probe_verdict("redis runs on 6379", probes) is None


def test_invalid_regex_skipped_not_fatal():
    probes = [
        {"claim_pattern": r"([bad", "last_result": "verified"},
        {"claim_pattern": r"redis", "last_result": "verified"},
    ]
    assert _probe_verdict("redis runs on 6379", probes) == "supported"


# ──────────────────────────────────────────────
# _accuracy (pure)
# ──────────────────────────────────────────────


def test_accuracy_math():
    assert _accuracy(3, 1) == 0.75
    assert _accuracy(0, 2) == 0.0
    assert _accuracy(5, 0) == 1.0


def test_accuracy_none_when_undecidable():
    assert _accuracy(0, 0) is None


# ──────────────────────────────────────────────
# _factcheck_card (mocked LLM + pool)
# ──────────────────────────────────────────────


def _mock_pool():
    conn = AsyncMock()
    pool = MagicMock()
    acquire_cm = MagicMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__ = AsyncMock(return_value=False)
    pool.acquire = MagicMock(return_value=acquire_cm)
    return pool, conn


CARD = {"id": "00000000-0000-0000-0000-000000000001",
        "subject_key": "bimavo", "title": "Bimavo", "body": "..."}


async def test_factcheck_probe_lane_no_llm_judge():
    """A probe-decided claim never reaches embed/search/judge."""
    claims_resp = {"claims": [{"claim": "gemma 4 is the current LLM", "checkable": True}]}
    probes = [{"claim_pattern": r"gemma 4", "last_result": "mismatch"}]
    pool, conn = _mock_pool()

    with (
        patch.object(scheduler_jobs, "ollama_chat", AsyncMock(return_value=claims_resp)) as llm,
        patch.object(scheduler_jobs, "embed_text", AsyncMock()) as emb,
    ):
        out = await _factcheck_card(pool, CARD, probes)

    assert out["accuracy"] == 0.0
    assert out["contradicted"] == 1
    assert out["claims"][0]["via"] == "probe"
    llm.assert_awaited_once()  # extraction only, no judge call
    emb.assert_not_awaited()
    # below card_min_accuracy (0.7) -> UPDATE ran with the rebuild trigger
    sql = conn.execute.await_args.args[0]
    assert "source_max_updated = CASE" in sql
    assert conn.execute.await_args.args[2] == 0.0  # accuracy param


async def test_factcheck_evidence_lane_supported():
    claims_resp = {"claims": [{"claim": "server is GEX44", "checkable": True}]}
    judge_resp = {"verdict": "supported", "reason": "newest note says GEX44"}
    evidence = [{"summary": "s", "content": "GEX44 Hetzner", "updated_at": "2026-07-01"}]
    pool, conn = _mock_pool()

    with (
        patch.object(
            scheduler_jobs, "ollama_chat",
            AsyncMock(side_effect=[claims_resp, judge_resp]),
        ),
        patch.object(scheduler_jobs, "embed_text", AsyncMock(return_value=[0.1])),
        patch.object(
            scheduler_jobs.queries, "search_memories",
            AsyncMock(return_value=evidence),
        ),
    ):
        out = await _factcheck_card(pool, CARD, [])

    assert out["accuracy"] == 1.0
    assert out["claims"][0]["via"] == "evidence"
    fc = json.loads(conn.execute.await_args.args[3])
    assert fc["supported"] == 1


async def test_factcheck_uncheckable_skipped():
    claims_resp = {"claims": [
        {"claim": "quality is a priority", "checkable": False},
        {"claim": "port is 8420", "checkable": True},
    ]}
    probes = [{"claim_pattern": r"port is 8420", "last_result": "verified"}]
    pool, _conn = _mock_pool()

    with patch.object(scheduler_jobs, "ollama_chat", AsyncMock(return_value=claims_resp)):
        out = await _factcheck_card(pool, CARD, probes)

    assert out["accuracy"] == 1.0  # uncheckable claim not in denominator
    assert out["claims"][0]["verdict"] == "skipped"


async def test_factcheck_no_evidence_unverifiable():
    claims_resp = {"claims": [{"claim": "obscure fact", "checkable": True}]}
    pool, conn = _mock_pool()

    with (
        patch.object(scheduler_jobs, "ollama_chat", AsyncMock(return_value=claims_resp)),
        patch.object(scheduler_jobs, "embed_text", AsyncMock(return_value=[0.1])),
        patch.object(
            scheduler_jobs.queries, "search_memories", AsyncMock(return_value=[])
        ),
    ):
        out = await _factcheck_card(pool, CARD, [])

    assert out["accuracy"] is None  # nothing decidable
    assert out["unverifiable"] == 1
    # accuracy None -> no rebuild trigger fires (CASE guards on NOT NULL)
    assert conn.execute.await_args.args[2] is None
