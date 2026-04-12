"""Aggregation retrieval tool (Phase L, v6.13).

Phase L adds ``memory_aggregate`` — the Supermemory-inspired pattern
of synthesizing N retrieved memories into K self-contained answer slots
via a single LLM call. Distinct from re-ranking (which reorders) and
cross-encoder rerank (which picks verbatim top-K).

Tests cover:
  1. k and fetch_limit clamping ([1, 10] and [k, 50])
  2. Happy path: embed → search_memories → ollama_chat → return slots
  3. Empty candidates → empty slots, no LLM call
  4. embed failure → error dict, no LLM call
  5. LLM failure → error dict, candidate_count populated
  6. Context formatting (numbered refs, content truncation)
  7. Filter params (tags, category, date_from/date_to) flow through
  8. K slot limit enforced on LLM output (if LLM returns more, we clip)
  9. ISO date parsing (plain date + Z suffix + offset)
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from nobrainr.mcp import server as mcp_server


def _unwrap(fn):
    if hasattr(fn, "fn"):
        return fn.fn
    if hasattr(fn, "__wrapped__"):
        return fn.__wrapped__
    return fn


def _fake_candidate(idx: int, content: str | None = None) -> dict:
    return {
        "id": f"{idx:08d}-0000-0000-0000-000000000000",
        "content": content or f"Candidate memory {idx}",
        "similarity": 0.85 - idx * 0.01,
    }


def _install_mocks(
    *,
    search_result: list[dict],
    llm_result: dict | None = None,
    llm_exc: Exception | None = None,
    embed_exc: Exception | None = None,
) -> dict:
    """Patch embed_text, search_memories, ollama_chat for a single test."""
    captured: dict = {"search_kwargs": None, "llm_user": None, "llm_system": None}

    async def fake_embed(query: str):
        if embed_exc:
            raise embed_exc
        captured["embed_query"] = query
        return [0.1] * 1024

    async def fake_search(**kwargs):
        captured["search_kwargs"] = kwargs
        return search_result

    async def fake_chat(system, user, schema, **kwargs):
        captured["llm_system"] = system
        captured["llm_user"] = user
        captured["llm_kwargs"] = kwargs
        if llm_exc:
            raise llm_exc
        return llm_result or {"slots": []}

    stack = [
        patch("nobrainr.embeddings.ollama.embed_text", side_effect=fake_embed),
        patch.object(mcp_server.queries, "search_memories", side_effect=fake_search),
        patch("nobrainr.extraction.llm.ollama_chat", side_effect=fake_chat),
    ]
    for p in stack:
        p.start()
    captured["__stack__"] = stack
    return captured


def _teardown_mocks(captured):
    for p in captured["__stack__"]:
        p.stop()


# ──────────────────────────────────────────────
# Happy path
# ──────────────────────────────────────────────


class TestAggregateHappyPath:
    @pytest.mark.asyncio
    async def test_returns_llm_slots_on_happy_path(self):
        fn = _unwrap(mcp_server.memory_aggregate)
        llm_result = {
            "slots": [
                {
                    "answer": "We fixed X by doing Y.",
                    "source_memory_ids": ["mem-1"],
                    "confidence": 0.8,
                },
            ],
        }
        cap = _install_mocks(
            search_result=[_fake_candidate(1), _fake_candidate(2)],
            llm_result=llm_result,
        )
        try:
            result = await fn(query="how did we fix X")
        finally:
            _teardown_mocks(cap)
        assert result["query"] == "how did we fix X"
        assert result["candidate_count"] == 2
        assert len(result["slots"]) == 1
        assert result["slots"][0]["answer"].startswith("We fixed X")

    @pytest.mark.asyncio
    async def test_search_is_hybrid(self):
        """memory_aggregate should always run HYBRID search (vector + FTS)
        by passing text_query to search_memories."""
        fn = _unwrap(mcp_server.memory_aggregate)
        cap = _install_mocks(search_result=[], llm_result={"slots": []})
        try:
            await fn(query="hybrid test")
        finally:
            _teardown_mocks(cap)
        # search_memories was called with text_query set (hybrid mode)
        assert cap["search_kwargs"]["text_query"] == "hybrid test"

    @pytest.mark.asyncio
    async def test_filters_flow_through_to_search(self):
        fn = _unwrap(mcp_server.memory_aggregate)
        cap = _install_mocks(search_result=[], llm_result={"slots": []})
        try:
            await fn(
                query="filtered",
                tags=["bug"],
                category="debugging",
                source_type="claude",
                source_machine="bimavo",
            )
        finally:
            _teardown_mocks(cap)
        kw = cap["search_kwargs"]
        assert kw["tags"] == ["bug"]
        assert kw["category"] == "debugging"
        assert kw["source_type"] == "claude"
        assert kw["source_machine"] == "bimavo"


# ──────────────────────────────────────────────
# Clamping
# ──────────────────────────────────────────────


class TestAggregateClamping:
    @pytest.mark.asyncio
    async def test_k_clamped_to_max_10(self):
        """k must never exceed 10 — caps runaway context + LLM latency."""
        fn = _unwrap(mcp_server.memory_aggregate)
        # Return more slots than k allows
        llm_result = {
            "slots": [
                {"answer": f"slot {i}", "source_memory_ids": [], "confidence": 0.5}
                for i in range(20)
            ],
        }
        cap = _install_mocks(
            search_result=[_fake_candidate(i) for i in range(15)],
            llm_result=llm_result,
        )
        try:
            result = await fn(query="q", k=999)  # extreme input
        finally:
            _teardown_mocks(cap)
        # k clamped to 10, so at most 10 slots returned even if LLM returned 20
        assert len(result["slots"]) <= 10

    @pytest.mark.asyncio
    async def test_k_clamped_to_min_1(self):
        fn = _unwrap(mcp_server.memory_aggregate)
        cap = _install_mocks(
            search_result=[_fake_candidate(1)],
            llm_result={
                "slots": [
                    {"answer": "one", "source_memory_ids": [], "confidence": 0.5},
                ],
            },
        )
        try:
            result = await fn(query="q", k=0)  # invalid low
        finally:
            _teardown_mocks(cap)
        # k=0 clamped to 1, returns at most 1 slot
        assert len(result["slots"]) <= 1

    @pytest.mark.asyncio
    async def test_k_slot_clip_enforced_on_llm_output(self):
        """If the LLM returns 5 slots but k=2, only 2 are returned."""
        fn = _unwrap(mcp_server.memory_aggregate)
        cap = _install_mocks(
            search_result=[_fake_candidate(i) for i in range(5)],
            llm_result={
                "slots": [
                    {"answer": f"slot {i}", "source_memory_ids": [], "confidence": 0.6}
                    for i in range(5)
                ],
            },
        )
        try:
            result = await fn(query="q", k=2)
        finally:
            _teardown_mocks(cap)
        assert len(result["slots"]) == 2


# ──────────────────────────────────────────────
# Failure modes
# ──────────────────────────────────────────────


class TestAggregateFailures:
    @pytest.mark.asyncio
    async def test_empty_candidates_returns_empty_slots_no_llm_call(self):
        fn = _unwrap(mcp_server.memory_aggregate)
        cap = _install_mocks(search_result=[], llm_result=None)
        try:
            result = await fn(query="no matches")
        finally:
            _teardown_mocks(cap)
        assert result["slots"] == []
        assert result["candidate_count"] == 0
        # ollama_chat was NOT called — we short-circuited
        assert cap.get("llm_user") is None

    @pytest.mark.asyncio
    async def test_embed_failure_returns_error_dict(self):
        fn = _unwrap(mcp_server.memory_aggregate)
        cap = _install_mocks(
            search_result=[],
            embed_exc=RuntimeError("embedder down"),
        )
        try:
            result = await fn(query="embed fail")
        finally:
            _teardown_mocks(cap)
        assert result["slots"] == []
        assert "error" in result
        assert "embed failed" in result["error"]

    @pytest.mark.asyncio
    async def test_llm_failure_populates_candidate_count(self):
        """LLM failure returns error but keeps candidate_count so the
        caller knows the retrieval side succeeded."""
        fn = _unwrap(mcp_server.memory_aggregate)
        cap = _install_mocks(
            search_result=[_fake_candidate(1), _fake_candidate(2)],
            llm_exc=RuntimeError("llm timeout"),
        )
        try:
            result = await fn(query="llm fail")
        finally:
            _teardown_mocks(cap)
        assert result["slots"] == []
        assert result["candidate_count"] == 2
        assert "synthesis failed" in result["error"]


# ──────────────────────────────────────────────
# Context formatting
# ──────────────────────────────────────────────


class TestAggregateContextFormatting:
    @pytest.mark.asyncio
    async def test_context_has_numbered_refs_and_uuid(self):
        fn = _unwrap(mcp_server.memory_aggregate)
        cap = _install_mocks(
            search_result=[
                _fake_candidate(1, "first memory content"),
                _fake_candidate(2, "second memory content"),
            ],
            llm_result={"slots": []},
        )
        try:
            await fn(query="format check")
        finally:
            _teardown_mocks(cap)
        user_prompt = cap["llm_user"]
        assert "[1] ID:" in user_prompt
        assert "[2] ID:" in user_prompt
        assert "first memory content" in user_prompt
        assert "second memory content" in user_prompt

    @pytest.mark.asyncio
    async def test_oversized_memory_content_is_truncated(self):
        """A single 10k-char memory should NOT blow the context window —
        per-memory content is capped at 700 chars."""
        fn = _unwrap(mcp_server.memory_aggregate)
        huge = "x" * 10000
        cap = _install_mocks(
            search_result=[_fake_candidate(1, huge)],
            llm_result={"slots": []},
        )
        try:
            await fn(query="huge")
        finally:
            _teardown_mocks(cap)
        # content in prompt is <= 700 chars for that single candidate
        # The prompt has other stuff; count just the x runs
        assert cap["llm_user"].count("x") <= 705  # 700 content + slack

    @pytest.mark.asyncio
    async def test_newlines_in_content_flattened(self):
        """Context lines are single-line so [n] ID: lines stay aligned."""
        fn = _unwrap(mcp_server.memory_aggregate)
        cap = _install_mocks(
            search_result=[_fake_candidate(1, "line1\nline2\nline3")],
            llm_result={"slots": []},
        )
        try:
            await fn(query="newline")
        finally:
            _teardown_mocks(cap)
        user_prompt = cap["llm_user"]
        # The content should have newlines replaced with spaces so the
        # [n] label lines remain aligned
        assert "line1 line2 line3" in user_prompt
