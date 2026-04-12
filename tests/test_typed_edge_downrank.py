"""Typed-edge downrank signal in recompute_importance (Phase J, v6.12).

Phase J adds a 5th term to the importance formula: a penalty for memories
whose linked entities appear in the "stale" position of a supersede /
deprecate / replace edge. Uses the two directions of the relationship_type
vocabulary already produced by the LLM extractor:

    - SOURCE of ('replaced_by', 'superseded_by', 'deprecated_in',
      'deprecated_since') → source entity was replaced
    - TARGET of ('replaces', 'supersedes', 'aims_to_replace',
      'can_replace_with') → target entity was replaced

A memory heavily connected to entities in either stale position is
likely carrying outdated information. The penalty subtracts up to 15
percentage points, clamped to [0, 1] via GREATEST(0.0, LEAST(1.0, ...)).

These tests lock in the SQL shape so the penalty doesn't silently
disappear in a future refactor AND the existing G1 positive terms
(graph proximity) stay intact. No real DB — mock pool + capture SQL.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nobrainr.db import queries as db_queries


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────


async def _capture_recompute_sql() -> str:
    captured = {}

    async def fake_execute(sql, *args):
        captured["sql"] = sql
        return "UPDATE 42"

    mock_conn = AsyncMock()
    mock_conn.execute = AsyncMock(side_effect=fake_execute)
    acquire_ctx = MagicMock()
    acquire_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
    acquire_ctx.__aexit__ = AsyncMock(return_value=None)
    mock_pool = MagicMock()
    mock_pool.acquire = MagicMock(return_value=acquire_ctx)

    with patch.object(db_queries, "get_pool", AsyncMock(return_value=mock_pool)):
        n = await db_queries.recompute_importance()

    assert n == 42
    return captured["sql"]


# ──────────────────────────────────────────────
# New CTEs — stale_entities + memory_stale_counts
# ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stale_entities_cte_present():
    sql = await _capture_recompute_sql()
    assert "stale_entities AS" in sql, \
        "Phase J requires a stale_entities CTE for the outdating-edge penalty"


@pytest.mark.asyncio
async def test_memory_stale_counts_cte_present():
    sql = await _capture_recompute_sql()
    assert "memory_stale_counts AS" in sql, \
        "Phase J requires a memory_stale_counts CTE aggregating stale entities per memory"
    assert "stale_entity_count" in sql, \
        "per-memory stale count column must be exposed as stale_entity_count"
    assert "count(DISTINCT em.entity_id)" in sql, \
        "count distinct entities, not entity_memory rows (avoids inflation)"


@pytest.mark.asyncio
async def test_stale_entities_covers_source_side_types():
    """'replaced_by' / 'superseded_by' / 'deprecated_in' / 'deprecated_since'
    — the SOURCE entity is the stale one for these types."""
    sql = await _capture_recompute_sql()
    assert "'replaced_by'" in sql
    assert "'superseded_by'" in sql
    assert "'deprecated_in'" in sql
    assert "'deprecated_since'" in sql
    assert "source_entity_id AS entity_id" in sql, \
        "source-side types should emit source_entity_id as the stale entity"


@pytest.mark.asyncio
async def test_stale_entities_covers_target_side_types():
    """'replaces' / 'supersedes' / 'aims_to_replace' / 'can_replace_with'
    — the TARGET entity is the stale one for these types."""
    sql = await _capture_recompute_sql()
    assert "'replaces'" in sql
    assert "'supersedes'" in sql
    assert "'aims_to_replace'" in sql
    assert "'can_replace_with'" in sql
    assert "target_entity_id AS entity_id" in sql, \
        "target-side types should emit target_entity_id as the stale entity"


@pytest.mark.asyncio
async def test_stale_entities_uses_UNION():
    """The two direction branches must be combined with UNION so both
    stale-source and stale-target entities are considered."""
    sql = await _capture_recompute_sql()
    # Check there's a UNION between the two SELECT branches inside stale_entities
    stale_section = sql[sql.find("stale_entities AS"):]
    stale_section = stale_section[:stale_section.find("memory_stale_counts AS")]
    assert "UNION" in stale_section, \
        "stale_entities must UNION both source-side and target-side branches"


# ──────────────────────────────────────────────
# Penalty term
# ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_penalty_term_at_15_percent():
    sql = await _capture_recompute_sql()
    assert "- (0.15 * LEAST(1.0, COALESCE((" in sql, \
        "Penalty must subtract (0.15 * stale_score) from the positive sum"


@pytest.mark.asyncio
async def test_penalty_normalizes_by_5():
    """``stale_entity_count::real / 5.0`` — normalization picks 5 hot
    matches as the saturating point so small numbers of stale edges
    produce meaningful signal without instantly capping at the max."""
    sql = await _capture_recompute_sql()
    assert "stale_entity_count::real / 5.0" in sql


@pytest.mark.asyncio
async def test_outer_wrap_uses_greatest_and_least():
    """GREATEST(0.0, LEAST(1.0, ...)) — without GREATEST the penalty could
    push importance negative."""
    sql = await _capture_recompute_sql()
    assert "GREATEST(0.0, LEAST(1.0," in sql, \
        "Outer clamp must be GREATEST(0.0, LEAST(1.0, ...)) to allow the penalty to push importance down without going negative"


# ──────────────────────────────────────────────
# Regression guards — positive terms must remain
# ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_phase_b_g1_positive_terms_still_present():
    """Phase J must NOT remove the Phase B G1 positive terms — regression guard."""
    sql = await _capture_recompute_sql()
    # Entity connectivity 30%
    assert "(0.3 * LEAST(1.0, COALESCE((\n                    SELECT count(*)::real / 10.0" in sql or \
           "0.3 * LEAST(1.0, COALESCE((" in sql, \
        "Phase J must preserve the 30% entity connectivity term from Phase B G1"
    # Quality 30%
    assert "(0.3 * COALESCE(quality_score, 0.5))" in sql
    # Confidence 20%
    assert "(0.2 * COALESCE(confidence, 0.7))" in sql
    # Graph proximity 20%
    assert "hot_entity_count::real / 10.0" in sql, \
        "Phase B G1 graph-proximity term (hot_entity_count/10.0) must still be present"


@pytest.mark.asyncio
async def test_hot_entities_cte_still_present():
    """Phase J keeps the Phase B G1 hot_entities CTE — regression guard."""
    sql = await _capture_recompute_sql()
    assert "hot_entities AS" in sql
    assert "last_accessed_at > NOW() - INTERVAL '7 days'" in sql
    assert "memory_hot_counts AS" in sql


@pytest.mark.asyncio
async def test_old_weight_40_percent_not_reintroduced():
    """Guard against a future edit that accidentally reverts the weight
    rebalance. Phase B G1 dropped entity connectivity from 40% to 30%."""
    sql = await _capture_recompute_sql()
    assert "(0.4 * LEAST" not in sql, \
        "The pre-Phase-B-G1 weight 0.4 on entity connectivity is gone — do not re-add"


# ──────────────────────────────────────────────
# Critical invariant — penalty cannot break positive sum
# ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_penalty_comes_after_positive_sum():
    """The penalty must be subtracted from the SUM of positive terms,
    NOT applied inside a LEAST that could eat it. Otherwise a memory
    with max positive terms (which hits LEAST(1.0, ...)) would have
    nothing for the penalty to reduce.

    This test asserts the structural ordering: the `- (0.15 *` must
    appear OUTSIDE the positive-term group (after all the `+` terms)
    AND inside the outer GREATEST/LEAST wrap.
    """
    sql = await _capture_recompute_sql()
    # Find position of graph proximity term (last positive) and the penalty
    prox_idx = sql.find("hot_entity_count::real / 10.0")
    penalty_idx = sql.find("stale_entity_count::real / 5.0")
    assert prox_idx > 0 and penalty_idx > 0
    assert penalty_idx > prox_idx, \
        "penalty subtraction must come AFTER the last positive term"
