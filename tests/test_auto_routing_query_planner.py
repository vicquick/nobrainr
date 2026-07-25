"""Auto-routing query planner for memory_search (Phase B G2, v6.7).

G2 adds ``auto_route: bool = False`` to the ``memory_search`` MCP tool.
When True, a lightweight heuristic picks the best retrieval strategy
based on query shape alone — no LLM call, no embedding, no async.

These tests lock in the routing rules so agents can rely on them:

    - short query (<= 3 words)         → vector + expand
    - long query (>= 12 words) OR
      multi-clause (2+ commas or ands) → hybrid + decompose
    - why/how/when question (5+ words) → hybrid + hyde
    - default                          → hybrid RRF only

Contract updated 2026-07-05 (auto_route became default-on): the router
applies ONLY when the caller left all strategy flags (hybrid/expand/
hyde/decompose) at their defaults. An explicitly-set flag is a
deliberate choice and always wins over the router.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from nobrainr.mcp import server as mcp_server


def _unwrap(fn):
    if hasattr(fn, "fn"):
        return fn.fn
    if hasattr(fn, "__wrapped__"):
        return fn.__wrapped__
    return fn


# ──────────────────────────────────────────────
# _auto_route_query — pure heuristic
# ──────────────────────────────────────────────


class TestAutoRouteHeuristic:
    """Direct tests for the _auto_route_query helper.

    This is a pure function — no DB, no async, no mocks.
    """

    def test_empty_query_defaults_to_hybrid(self):
        """Empty or whitespace-only query returns the safe default."""
        assert mcp_server._auto_route_query("") == {"hybrid": True}
        assert mcp_server._auto_route_query("   ") == {"hybrid": True}
        assert mcp_server._auto_route_query(None) == {"hybrid": True}  # type: ignore[arg-type]

    def test_short_query_picks_vector_and_expand(self):
        """1-3 word queries get pure vector + expand (no FTS because short
        queries lose precision in plainto_tsquery)."""
        assert mcp_server._auto_route_query("flux") == {"hybrid": False, "expand": True}
        assert mcp_server._auto_route_query("flux mcp") == {"hybrid": False, "expand": True}
        assert mcp_server._auto_route_query("docker compose down") == {"hybrid": False, "expand": True}

    def test_why_question_picks_hyde(self):
        """'why ... ' with 8+ words → hybrid + HyDE (retuned 2026-07-22)."""
        routing = mcp_server._auto_route_query("why did we stop the flux container on the server tonight")
        assert routing["hybrid"] is True
        assert routing["hyde"] is True
        # Not decompose — 10 words is below the 24-word threshold
        assert routing.get("decompose", False) is False

    def test_how_question_picks_hyde(self):
        """'how ... ' with 5+ words → hybrid + HyDE."""
        routing = mcp_server._auto_route_query("how does the nobrainr queue worker handle concurrency")
        assert routing["hybrid"] is True
        assert routing["hyde"] is True

    def test_when_did_question_picks_hyde(self):
        """'when did ... ' with 5+ words → hybrid + HyDE."""
        routing = mcp_server._auto_route_query("when did we ship the temporal filters for the search path")
        assert routing["hybrid"] is True
        assert routing["hyde"] is True

    def test_why_with_too_few_words_is_default(self):
        """'why X' with <5 words is NOT a HyDE candidate (too little to
        hallucinate against). Falls through to short-query rule."""
        # "why did we" = 3 words, short-query rule applies
        routing = mcp_server._auto_route_query("why did we")
        assert routing == {"hybrid": False, "expand": True}

    def test_long_query_picks_decompose(self):
        """12+ word queries → hybrid + decompose (break into sub-queries)."""
        long_q = (
            "how did we handle the nobrainr deploy plus the flux-mcp stop and "
            "also the temporal filter changes and the reranker rollout and the "
            "trust flywheel updates across all the affected machines this month"
        )
        # Sanity: at least 24 words (retuned threshold)
        assert len(long_q.split()) >= 24
        routing = mcp_server._auto_route_query(long_q)
        assert routing["hybrid"] is True
        assert routing["decompose"] is True
        # Long query rule wins over "how" rule (rule 1 before rule 2)
        assert routing.get("hyde", False) is False

    def test_multi_comma_picks_decompose(self):
        """2+ commas → decompose (treat as comma-separated list of sub-intents)."""
        routing = mcp_server._auto_route_query("bug fix, test, deploy, review")
        assert routing["hybrid"] is True
        assert routing["decompose"] is True

    def test_multi_and_picks_decompose(self):
        """3+ ' and ' → decompose (retuned 2026-07-22)."""
        routing = mcp_server._auto_route_query("deploy flux and fix queue and test ranker and update docs")
        assert routing["hybrid"] is True
        assert routing["decompose"] is True

    def test_medium_query_picks_hybrid_default(self):
        """Medium (4-11 word) non-question query → plain hybrid RRF."""
        routing = mcp_server._auto_route_query("nobrainr graph proximity ranker tests")
        # 5 words, not a question, no commas/ands, not short → default
        assert routing == {"hybrid": True}

    def test_rule_ordering_long_wins_over_why(self):
        """Rule 1 (length) must win over rule 2 (question shape) when both
        match, otherwise 'why ...' long questions would get HyDE instead
        of decomposition, which is wrong for complex questions."""
        # "why" + 13 words
        q = ("why did we pick this particular concurrency setting for the scheduler "
             "and llama-server and the write queue and the reranker and what were "
             "the alternatives we rejected at the time for each one")
        assert len(q.split()) >= 24
        routing = mcp_server._auto_route_query(q)
        assert routing.get("decompose") is True
        # Not hyde — decompose takes priority
        assert routing.get("hyde", False) is False


# ──────────────────────────────────────────────
# memory_search — auto_route integration
# ──────────────────────────────────────────────


class TestMemorySearchAutoRouteIntegration:
    """memory_search with auto_route=True must actually OVERRIDE the explicit
    hybrid/expand/hyde/decompose flags and call search_memories with the
    heuristic-selected ones.

    We patch search_memories (and the embedding/decompose dependencies) and
    inspect the resulting calls.
    """

    @pytest.mark.asyncio
    async def test_auto_route_short_query_routes_when_flags_default(self):
        """Contract since 2026-07-05 (auto_route default-on): the router
        applies only when the caller left every strategy flag at its
        default. Short query + default flags → vector-only."""
        fn = _unwrap(mcp_server.memory_search)

        captured_kwargs = []

        async def fake_search(*, embedding, **kwargs):
            captured_kwargs.append(kwargs)
            return []

        async def fake_embed_batch(queries):
            return [[0.1] * 768 for _ in queries]

        with patch("nobrainr.db.queries.search_memories", side_effect=fake_search), \
             patch("nobrainr.embeddings.ollama.embed_batch", side_effect=fake_embed_batch), \
             patch.object(mcp_server.queries, "expand_chunk_context", AsyncMock(return_value=[])), \
             patch.object(mcp_server.queries, "record_interest_signal", AsyncMock(return_value=None)):
            await fn(query="flux", auto_route=True)

        assert captured_kwargs, "search_memories should have been called"
        kw = captured_kwargs[0]
        # Short query rule: hybrid=False, expand=True
        # text_query is None when hybrid is False
        assert kw["text_query"] is None, \
            "short query auto-routed to vector-only should not pass text_query"

    @pytest.mark.asyncio
    async def test_auto_route_explicit_flags_beat_the_router(self):
        """Contract since 2026-07-05: an explicitly-set strategy flag is a
        deliberate caller choice and wins over the router. With auto_route
        defaulting ON, silently overriding explicit flags would break every
        caller that passes expand=True/decompose=True (our own MCP prompt
        templates instruct exactly that)."""
        fn = _unwrap(mcp_server.memory_search)

        captured_kwargs = []

        async def fake_search(*, embedding, **kwargs):
            captured_kwargs.append(kwargs)
            return []

        async def fake_embed_batch(queries):
            return [[0.1] * 768 for _ in queries]

        async def fake_decompose(query):
            return []  # no sub-queries — keep the decompose path inert

        with patch("nobrainr.services.search_enhancements.decompose_query", side_effect=fake_decompose), \
             patch("nobrainr.db.queries.search_memories", side_effect=fake_search), \
             patch("nobrainr.embeddings.ollama.embed_batch", side_effect=fake_embed_batch), \
             patch.object(mcp_server.queries, "expand_chunk_context", AsyncMock(return_value=[])), \
             patch.object(mcp_server.queries, "record_interest_signal", AsyncMock(return_value=None)):
            # A short query WOULD route to hybrid=False, but explicit
            # decompose=True marks the caller as having chosen a strategy —
            # the router stands down and hybrid stays True.
            await fn(query="flux", auto_route=True, decompose=True)

        assert captured_kwargs, "search_memories should have been called"
        kw = captured_kwargs[0]
        assert kw["text_query"] is not None, \
            "explicit decompose=True must disable the router; hybrid stays on"

    @pytest.mark.asyncio
    async def test_auto_route_long_query_triggers_decompose_path(self):
        """Long query → decompose=True → decompose_query gets called."""
        fn = _unwrap(mcp_server.memory_search)

        decompose_called = []

        async def fake_decompose(query):
            decompose_called.append(query)
            return ["sub1", "sub2"]

        async def fake_search(**kwargs):
            return []

        async def fake_embed_batch(queries):
            return [[0.1] * 768 for _ in queries]

        with patch("nobrainr.services.search_enhancements.decompose_query", side_effect=fake_decompose), \
             patch("nobrainr.db.queries.search_memories", side_effect=fake_search), \
             patch("nobrainr.embeddings.ollama.embed_batch", side_effect=fake_embed_batch), \
             patch.object(mcp_server.queries, "expand_chunk_context", AsyncMock(return_value=[])), \
             patch.object(mcp_server.queries, "record_interest_signal", AsyncMock(return_value=None)):
            long_q = (
                "how did we handle the nobrainr deploy plus the flux-mcp stop "
                "and also the temporal filter changes and the reranker rollout "
                "and the trust flywheel updates across all machines this month"
            )
            await fn(query=long_q, auto_route=True)

        assert decompose_called, \
            "auto_route should have flipped decompose=True and invoked decompose_query"

    @pytest.mark.asyncio
    async def test_auto_route_false_preserves_explicit_flags(self):
        """When auto_route is False (default), explicit flags are respected."""
        fn = _unwrap(mcp_server.memory_search)

        captured_kwargs = []

        async def fake_search(*, embedding, **kwargs):
            captured_kwargs.append(kwargs)
            return []

        async def fake_embed_batch(queries):
            return [[0.1] * 768 for _ in queries]

        with patch("nobrainr.db.queries.search_memories", side_effect=fake_search), \
             patch("nobrainr.embeddings.ollama.embed_batch", side_effect=fake_embed_batch), \
             patch.object(mcp_server.queries, "expand_chunk_context", AsyncMock(return_value=[])), \
             patch.object(mcp_server.queries, "record_interest_signal", AsyncMock(return_value=None)):
            # Explicit hybrid=False on a short query — without auto_route
            # the user's explicit choice should hold
            await fn(query="flux", auto_route=False, hybrid=False)

        kw = captured_kwargs[0]
        assert kw["text_query"] is None, \
            "auto_route=False should respect the explicit hybrid=False"
