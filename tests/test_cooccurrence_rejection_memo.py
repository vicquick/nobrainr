"""cooccurrence_linking must not re-judge pairs it has already rejected.

Before the rejection memo existed, a "no" verdict left no trace anywhere.
get_unlinked_cooccurrences orders by shared_count DESC, so the same top-N
came back every run — measured: two consecutive calls returned an identical
30 pairs — while ~77k candidate pairs sat behind an immovable wall. The job
burned ~270 LLM calls/day re-answering the same question.
"""

import asyncio
from unittest.mock import AsyncMock

import pytest

from nobrainr import scheduler_jobs
from nobrainr.config import settings


def _pair(a="11111111-1111-4111-8111-111111111111",
          b="22222222-2222-4222-8222-222222222222",
          shared=7):
    return {
        "entity_a_id": a,
        "entity_b_id": b,
        "entity_a_name": "Crawl4AI",
        "entity_a_type": "technology",
        "entity_b_name": "GHSA-cj2c-9jx8-j427",
        "entity_b_type": "concept",
        "shared_count": shared,
        "sample_contents": ["both appear in this memory"],
    }


@pytest.fixture
def wired(monkeypatch):
    """Stub the DB layer and LLM so only the job's control flow is tested."""
    q = scheduler_jobs.queries
    monkeypatch.setattr(scheduler_jobs, "_yield_to_live_requests", AsyncMock())
    monkeypatch.setattr(q, "store_entity_pair_rejection", AsyncMock(), raising=False)
    monkeypatch.setattr(q, "store_entity_relation", AsyncMock(), raising=False)
    return q


def test_rejected_pair_is_recorded(monkeypatch, wired):
    """A 'no' verdict must be persisted, or it will be asked again forever."""
    monkeypatch.setattr(
        wired, "get_unlinked_cooccurrences", AsyncMock(return_value=[_pair()])
    )
    monkeypatch.setattr(
        scheduler_jobs, "ollama_chat",
        AsyncMock(return_value={"has_relationship": False, "reason": "co-mention only"}),
    )

    res = asyncio.run(scheduler_jobs.cooccurrence_linking())

    assert res["skipped"] == 1
    assert res["relationships_created"] == 0
    wired.store_entity_pair_rejection.assert_awaited_once()
    kwargs = wired.store_entity_pair_rejection.await_args.kwargs
    assert kwargs["reason"] == "co-mention only", "reason must be kept for audit"
    assert kwargs["shared_count"] == 7


def test_accepted_pair_is_not_recorded_as_rejected(monkeypatch, wired):
    monkeypatch.setattr(
        wired, "get_unlinked_cooccurrences", AsyncMock(return_value=[_pair()])
    )
    monkeypatch.setattr(
        scheduler_jobs, "ollama_chat",
        AsyncMock(return_value={
            "has_relationship": True, "relationship_type": "affects",
            "confidence": 0.9, "direction": "a_to_b",
        }),
    )

    res = asyncio.run(scheduler_jobs.cooccurrence_linking())

    assert res["relationships_created"] == 1
    assert res["skipped"] == 0
    wired.store_entity_pair_rejection.assert_not_awaited()
    wired.store_entity_relation.assert_awaited_once()


def test_missing_context_is_not_memoized(monkeypatch, wired):
    """Empty sample content is missing data, not a verdict.

    Memoizing it would permanently exclude a pair that might be judgeable
    once its memory content is available.
    """
    blank = _pair()
    blank["sample_contents"] = ["", None]
    monkeypatch.setattr(
        wired, "get_unlinked_cooccurrences", AsyncMock(return_value=[blank])
    )
    llm = AsyncMock()
    monkeypatch.setattr(scheduler_jobs, "ollama_chat", llm)

    res = asyncio.run(scheduler_jobs.cooccurrence_linking())

    assert res["no_context"] == 1
    assert res["skipped"] == 0, "no_context must not be conflated with a rejection"
    wired.store_entity_pair_rejection.assert_not_awaited()
    llm.assert_not_awaited(), "must not spend an LLM call on empty context"


def test_batch_size_comes_from_settings(monkeypatch, wired):
    """The limit was hardcoded to 30 while this setting sat unused."""
    monkeypatch.setattr(settings, "cooccurrence_batch_size", 123)
    getter = AsyncMock(return_value=[])
    monkeypatch.setattr(wired, "get_unlinked_cooccurrences", getter)

    asyncio.run(scheduler_jobs.cooccurrence_linking())

    assert getter.await_args.kwargs["limit"] == 123


def test_llm_error_does_not_memoize_a_rejection(monkeypatch, wired):
    """A crash is not a verdict — the pair must stay eligible for retry."""
    monkeypatch.setattr(
        wired, "get_unlinked_cooccurrences", AsyncMock(return_value=[_pair()])
    )
    monkeypatch.setattr(
        scheduler_jobs, "ollama_chat", AsyncMock(side_effect=RuntimeError("boom"))
    )

    res = asyncio.run(scheduler_jobs.cooccurrence_linking())

    assert res["errors"] == 1
    assert res["skipped"] == 0
    wired.store_entity_pair_rejection.assert_not_awaited()
