"""decay_stability must not rewrite rows that are already at the floor.

Measured on the live database on 2026-11-17: 101,434 of 109,393 candidate
rows already had stability = 0.1. GREATEST(0.1, stability * 0.95) cannot
lower them, but the UPDATE rewrote each of them on every maintenance run
(about four times a day), firing the updated_at and version triggers and
taking 454 s. The select now leaves those rows out.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nobrainr.db import queries as db_queries


async def _run(ids):
    """Run decay_stability() against a mock pool; return (select_sql, update_calls, result)."""
    captured = {"updates": []}

    async def fake_fetch(sql, *args):
        captured["select"] = sql
        return [{"id": i} for i in ids]

    async def fake_execute(sql, *args):
        captured["updates"].append((sql, args))
        return f"UPDATE {len(args[0])}"

    conn = AsyncMock()
    conn.fetch = AsyncMock(side_effect=fake_fetch)
    conn.execute = AsyncMock(side_effect=fake_execute)
    txn = MagicMock()
    txn.__aenter__ = AsyncMock(return_value=None)
    txn.__aexit__ = AsyncMock(return_value=None)
    conn.transaction = MagicMock(return_value=txn)
    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=conn)
    acquire.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquire)
    with patch.object(db_queries, "get_pool", AsyncMock(return_value=pool)):
        n = await db_queries.decay_stability()
    return captured.get("select", ""), captured["updates"], n


@pytest.mark.asyncio
async def test_select_leaves_out_rows_at_the_floor():
    select, _, _ = await _run(["a"])
    assert "stability > 0.1::real" in select, (
        "the select must skip rows already at the floor — otherwise 93 % of "
        "the run rewrites unchanged rows"
    )


@pytest.mark.asyncio
async def test_null_stability_is_still_picked_up():
    """GREATEST(0.1, NULL) is 0.1 in Postgres, so a NULL row did get a value
    before; the filter must not silently drop that behaviour."""
    select, _, _ = await _run(["a"])
    assert "stability IS NULL OR stability > 0.1::real" in select


@pytest.mark.asyncio
async def test_floor_in_filter_matches_floor_in_update():
    select, updates, _ = await _run(["a"])
    assert "GREATEST(0.1, stability * 0.95)" in updates[0][0], "the UPDATE formula must stay as it was"
    assert "0.1::real" in select


@pytest.mark.asyncio
async def test_other_filters_unchanged():
    select, _, _ = await _run(["a"])
    assert "interval '7 days'" in select
    assert "claim_kind NOT IN ('reference', 'historical', 'creative')" in select


@pytest.mark.asyncio
async def test_no_candidates_means_no_update():
    _, updates, n = await _run([])
    assert n == 0 and updates == []


@pytest.mark.asyncio
async def test_batches_and_count():
    ids = [f"id-{i}" for i in range(4500)]
    _, updates, n = await _run(ids)
    assert n == 4500 and [len(u[1][0]) for u in updates] == [2000, 2000, 500]
