"""Graph-proximity signal in recompute_importance (Phase B G1, v6.6).

The v6.6 bump adds a fourth term to the importance formula: how many
"hot entities" (those linked to any memory accessed in the last 7 days)
are linked to this memory. This is the Graphiti-inspired rerank-by-
graph-distance-to-recent-episodes pattern, baked into importance so the
query-time ranker (memory_relevance) gets the signal for free.

These tests lock in the SQL shape — the new CTEs are present, the
weights are 0.3 / 0.3 / 0.2 / 0.2 (down from 0.4 / 0.3 / 0.3), and the
graph proximity computation looks at last_accessed_at within 7 days.

We don't hit a real DB; we capture the SQL string passed to conn.execute
and assert invariants. Runtime behaviour (actual score values) is tested
indirectly by scheduler runs in production.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nobrainr.db import queries as db_queries


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────


async def _capture_execute_sql() -> str:
    """Run recompute_importance() against a mock pool and return the SQL
    string passed to conn.execute()."""
    captured = {}

    async def fake_execute(sql, *args):
        captured["sql"] = sql
        # Return a string that matches "UPDATE <n>" so recompute_importance
        # can split()[-1] it into an int count.
        return "UPDATE 42"

    mock_conn = AsyncMock()
    mock_conn.execute = AsyncMock(side_effect=fake_execute)
    # The `async with pool.acquire() as conn:` pattern needs an async context
    # manager that yields conn.
    acquire_ctx = MagicMock()
    acquire_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
    acquire_ctx.__aexit__ = AsyncMock(return_value=None)
    mock_pool = MagicMock()
    mock_pool.acquire = MagicMock(return_value=acquire_ctx)

    with patch.object(db_queries, "get_pool", AsyncMock(return_value=mock_pool)):
        n = await db_queries.recompute_importance()

    assert n == 42, "recompute_importance should parse the row count from 'UPDATE 42'"
    return captured["sql"]


# ──────────────────────────────────────────────
# SQL shape tests
# ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_recompute_importance_includes_hot_entities_cte():
    """The new CTE 'hot_entities' must be present — it's the anchor set for
    the graph-proximity signal."""
    sql = await _capture_execute_sql()
    assert "WITH hot_entities AS" in sql, \
        "hot_entities CTE must be the first WITH block"
    assert "last_accessed_at > NOW() - INTERVAL '7 days'" in sql, \
        "hot_entities must filter to memories accessed in the last 7 days"


@pytest.mark.asyncio
async def test_recompute_importance_includes_memory_hot_counts_cte():
    """The second CTE computes per-memory hot entity counts."""
    sql = await _capture_execute_sql()
    assert "memory_hot_counts AS" in sql, \
        "memory_hot_counts CTE must aggregate hot entities per memory"
    assert "hot_entity_count" in sql, \
        "memory_hot_counts must expose hot_entity_count column"
    assert "count(DISTINCT em.entity_id)" in sql, \
        "we count distinct entities, not rows, to avoid inflation"


@pytest.mark.asyncio
async def test_recompute_importance_weights_sum_to_one():
    """The new formula: 30% entity + 30% quality + 20% confidence + 20% proximity."""
    sql = await _capture_execute_sql()

    # New weights
    assert "(0.3 * LEAST(1.0, COALESCE((" in sql, \
        "entity connectivity should be at 30% (down from 40%)"
    assert "(0.3 * COALESCE(quality_score, 0.5))" in sql, \
        "quality weight should remain at 30%"
    assert "(0.2 * COALESCE(confidence, 0.7))" in sql, \
        "confidence should drop to 20% (down from 30%)"
    # Graph proximity term — the new one
    assert "0.2 * LEAST(1.0, COALESCE((" in sql, \
        "graph proximity should be added at 20%"
    assert "hot_entity_count::real / 10.0" in sql, \
        "graph proximity normalizes hot count by 10"


@pytest.mark.asyncio
async def test_recompute_importance_old_weights_not_present():
    """The pre-v6.6 formula (40% / 30% / 30%) must be GONE.

    Catches accidental reverts where someone edits the weights without
    understanding the composition — if any of these strings come back,
    it means entity connectivity snapped back to 40% or confidence to 30%.
    """
    sql = await _capture_execute_sql()
    # 0.4 * LEAST is the old entity connectivity weight
    assert "(0.4 * LEAST(1.0, COALESCE((" not in sql, \
        "v6.5 and earlier used 0.4 for entity connectivity — that's the old weight"
    # 0.3 * COALESCE(confidence is the old confidence weight
    assert "(0.3 * COALESCE(confidence, 0.7))" not in sql, \
        "v6.5 and earlier used 0.3 for confidence — that's the old weight"


@pytest.mark.asyncio
async def test_recompute_importance_preserves_embedding_filter():
    """Must keep the 'WHERE embedding IS NOT NULL' guard — we never score
    memories that haven't been embedded yet, because they can't be searched."""
    sql = await _capture_execute_sql()
    assert "WHERE embedding IS NOT NULL" in sql, \
        "safety guard: don't compute importance on pre-embedding rows"


@pytest.mark.asyncio
async def test_recompute_importance_least_1_cap():
    """Importance must still be capped at 1.0 — the sum of weights could
    theoretically exceed 1.0 if quality + confidence + connectivity all
    max out simultaneously."""
    sql = await _capture_execute_sql()
    # Outer LEAST(1.0, ...)
    assert sql.count("LEAST(1.0,") >= 3, \
        "LEAST(1.0, ...) guards should cap each sub-term and the sum"
    assert "UPDATE memories m SET importance = LEAST(1.0," in sql, \
        "the outer cap must be on the total SET assignment"


@pytest.mark.asyncio
async def test_recompute_importance_returns_row_count():
    """The function should parse the UPDATE count from the 'UPDATE N' status."""
    # _capture_execute_sql already asserts n == 42; this is a smoke re-test
    sql = await _capture_execute_sql()
    # Just make sure the UPDATE statement is there
    assert "UPDATE memories" in sql
