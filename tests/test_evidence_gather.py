"""evidence_gather (LME-V2 AgentRunbook-C pattern): SQL guard + loop contract."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from nobrainr.mcp import server as mcp_server
from nobrainr.mcp.server import _guard_sql


def _unwrap(fn):
    if hasattr(fn, "fn"):
        return fn.fn
    return fn


# ── _guard_sql (pure) — defense layer 1 of 2 (txn is also read-only)


def test_plain_select_wrapped_with_cap():
    out = _guard_sql("SELECT id FROM memories ORDER BY created_at", 50)
    assert out == "SELECT * FROM (SELECT id FROM memories ORDER BY created_at) _eg LIMIT 50"


def test_dml_rejected():
    for bad in (
        "UPDATE memories SET tier=0",
        "DELETE FROM memories",
        "INSERT INTO memories VALUES (1)",
        "DROP TABLE memories",
        "TRUNCATE memories",
    ):
        assert _guard_sql(bad, 50) is None


def test_statement_chaining_rejected():
    assert _guard_sql("SELECT 1; DROP TABLE memories", 50) is None


def test_sneaky_cte_with_dml_rejected():
    assert _guard_sql("WITH x AS (DELETE FROM memories RETURNING id) SELECT * FROM x", 50) is None


def test_empty_rejected():
    assert _guard_sql("", 50) is None
    assert _guard_sql("   ", 50) is None


# ── loop contract (mocked LLM + search)


async def test_done_first_step_returns_picked_evidence():
    eg = _unwrap(mcp_server.evidence_gather)
    search_hits = [
        {"id": "11111111-aaaa-bbbb-cccc-000000000001", "summary": "hit one"},
        {"id": "22222222-aaaa-bbbb-cccc-000000000002", "summary": "hit two"},
    ]

    decisions = [
        {"action": "search", "query": "docker aliases"},
        {"action": "done", "ids": ["11111111"], "notes": "answered"},
    ]

    async def fake_search(**kwargs):
        return search_hits

    with (
        patch("nobrainr.extraction.llm.ollama_chat",
              AsyncMock(side_effect=decisions)),
        patch.object(mcp_server.memory_search, "fn", new=fake_search, create=True),
        patch("nobrainr.db.pool.get_pool", AsyncMock()),
    ):
        out = await eg(question="what links docker aliases to deploys?")

    assert out["notes"] == "answered"
    assert [s["action"] for s in out["steps"]] == ["search", "done"]
    # picked prefix 11111111 resolved to the full id and kept only that row
    assert [e["id"] for e in out["evidence"]] == ["11111111-aaaa-bbbb-cccc-000000000001"]


async def test_llm_failure_returns_gathered_so_far():
    eg = _unwrap(mcp_server.evidence_gather)
    with (
        patch("nobrainr.extraction.llm.ollama_chat",
              AsyncMock(side_effect=RuntimeError("gpu busy"))),
        patch("nobrainr.db.pool.get_pool", AsyncMock()),
    ):
        out = await eg(question="anything")
    assert out["evidence"] == []
    assert out["steps"] == []
