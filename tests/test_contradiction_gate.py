"""T4 write-time contradiction gate: candidate banding, verdict fold,
canonical supersede via the column, and never-blocks-the-write."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from nobrainr.config import settings
from nobrainr.extraction import contradiction
from nobrainr.extraction.contradiction import check_and_supersede


def _mock_pool(rows):
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=rows)
    pool = MagicMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=False)
    pool.acquire = MagicMock(return_value=cm)
    return pool, conn


NEW_ID = "99999999-aaaa-bbbb-cccc-000000000009"
OLD = {
    "id": "11111111-aaaa-bbbb-cccc-000000000001",
    "summary": "current LLM is gemma3",
    "content": "The extraction LLM is gemma3:12b via Ollama",
    "claim_kind": "infra-state",
    "trust_score": 0.9,
    "similarity": 0.64,
}


async def test_supersedes_verdict_calls_canonical_op():
    pool, _ = _mock_pool([OLD])
    resp = {"verdicts": [{"i": 0, "verdict": "supersedes", "reason": "migrated to qwen"}]}
    sup = AsyncMock(return_value=True)
    with (
        patch.object(contradiction, "get_pool", AsyncMock(return_value=pool)),
        patch.object(contradiction, "ollama_chat", AsyncMock(return_value=resp)),
        patch("nobrainr.db.queries.supersede_memory", sup),
    ):
        out = await check_and_supersede(NEW_ID, "we migrated extraction to qwen3.6-27b", [0.1] * 4)
    assert out == [{"id": OLD["id"], "reason": "migrated to qwen"}]
    sup.assert_awaited_once()
    args = sup.await_args
    assert args.args == (OLD["id"], NEW_ID)
    assert "contradiction gate" in args.kwargs["reason"]


async def test_compatible_and_unrelated_do_nothing():
    pool, _ = _mock_pool([OLD])
    resp = {"verdicts": [{"i": 0, "verdict": "compatible"}]}
    sup = AsyncMock()
    with (
        patch.object(contradiction, "get_pool", AsyncMock(return_value=pool)),
        patch.object(contradiction, "ollama_chat", AsyncMock(return_value=resp)),
        patch("nobrainr.db.queries.supersede_memory", sup),
    ):
        out = await check_and_supersede(NEW_ID, "extra detail about ollama", [0.1] * 4)
    assert out == []
    sup.assert_not_awaited()


async def test_no_candidates_skips_llm():
    pool, _ = _mock_pool([])
    llm = AsyncMock()
    with (
        patch.object(contradiction, "get_pool", AsyncMock(return_value=pool)),
        patch.object(contradiction, "ollama_chat", llm),
    ):
        out = await check_and_supersede(NEW_ID, "totally new topic", [0.1] * 4)
    assert out == []
    llm.assert_not_awaited()


async def test_gate_failure_never_raises():
    with patch.object(contradiction, "get_pool", AsyncMock(side_effect=RuntimeError("db down"))):
        out = await check_and_supersede(NEW_ID, "anything", [0.1] * 4)
    assert out == []


async def test_disabled_via_config():
    llm = AsyncMock()
    with (
        patch.object(settings, "contradiction_gate_enabled", False),
        patch.object(contradiction, "ollama_chat", llm),
    ):
        out = await check_and_supersede(NEW_ID, "anything", [0.1] * 4)
    assert out == []
    llm.assert_not_awaited()


async def test_bad_index_ignored():
    pool, _ = _mock_pool([OLD])
    resp = {"verdicts": [{"i": 7, "verdict": "supersedes", "reason": "hallucinated index"}]}
    sup = AsyncMock()
    with (
        patch.object(contradiction, "get_pool", AsyncMock(return_value=pool)),
        patch.object(contradiction, "ollama_chat", AsyncMock(return_value=resp)),
        patch("nobrainr.db.queries.supersede_memory", sup),
    ):
        out = await check_and_supersede(NEW_ID, "x", [0.1] * 4)
    assert out == []
    sup.assert_not_awaited()


async def test_candidate_query_uses_band_and_kinds():
    pool, conn = _mock_pool([])
    with patch.object(contradiction, "get_pool", AsyncMock(return_value=pool)):
        await check_and_supersede(NEW_ID, "x", [0.1] * 4)
    sql = conn.fetch.await_args.args[0]
    assert "BETWEEN" in sql and "claim_kind = ANY" in sql
    args = conn.fetch.await_args.args
    assert args[2] == settings.contradiction_gate_sim_min
    assert args[3] == settings.contradiction_gate_sim_max
