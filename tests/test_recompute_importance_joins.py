"""recompute_importance must compute each per-memory count once and join it.

Until 2026-11-17 the compute SELECT looked the counts up with one scalar
subquery per memory row. Postgres inlined the single-use CTEs into those
subqueries and rebuilt the whole hot-entity set for every memory: plan
cost 15.5 billion, 906-3581 s per run on 156k memories, and the long
snapshot blocked REINDEX CONCURRENTLY for 54 minutes. With the counts
materialised once and hash-joined the same run takes 5 s and returns the
same values (0 of 399 sampled rows differed on the live database).

These tests lock the shape that keeps it fast. Like the neighbouring
files they read the SQL string; the live comparison is in the PR text.
"""

from __future__ import annotations

import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nobrainr.db import queries as db_queries


async def _capture_execute_sql() -> str:
    """Run recompute_importance() against a mock pool; return the compute SELECT."""
    captured = {}

    async def fake_fetch(sql, *args):
        captured["sql"] = sql
        return [{"id": "00000000-0000-0000-0000-000000000001", "new_importance": 0.42, "old_importance": 0.10}]

    conn = AsyncMock()
    conn.fetch = AsyncMock(side_effect=fake_fetch)
    conn.execute = AsyncMock(return_value="UPDATE 1")
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
        await db_queries.recompute_importance()
    return captured["sql"]

ZAEHLER = ("memory_hot_counts", "memory_stale_counts", "memory_entity_counts")


def _hauptteil(sql: str) -> str:
    """The final SELECT after the last CTE."""
    return sql[sql.rindex("SELECT m.id"):]


@pytest.mark.asyncio
async def test_every_cte_is_materialized():
    sql = await _capture_execute_sql()
    for name in ("hot_entities", "stale_entities", *ZAEHLER):
        assert re.search(rf"\b{name} AS MATERIALIZED \(", sql), (
            f"{name} must be AS MATERIALIZED — otherwise Postgres may inline it "
            "into a per-row subplan again"
        )


@pytest.mark.asyncio
async def test_counts_are_joined_not_looked_up_per_row():
    haupt = _hauptteil(await _capture_execute_sql())
    for name, alias in zip(ZAEHLER, ("mhc", "msc", "mec")):
        assert re.search(rf"LEFT JOIN {name} {alias} ON {alias}\.memory_id = m\.id", haupt), (
            f"{name} must be LEFT JOINed on memory_id"
        )


@pytest.mark.asyncio
async def test_no_scalar_subquery_in_the_final_select():
    haupt = _hauptteil(await _capture_execute_sql())
    assert "SELECT" not in haupt[len("SELECT m.id"):].upper(), (
        "the final SELECT must not contain a nested SELECT: one scalar subquery "
        "per memory row is what made this run take up to an hour"
    )


@pytest.mark.asyncio
async def test_left_join_keeps_memories_without_entities():
    """A memory with no entity rows must still get a value (COALESCE to 0), so
    the joins have to be LEFT joins and every count wrapped in COALESCE."""
    haupt = _hauptteil(await _capture_execute_sql())
    assert " JOIN " in haupt and not re.search(r"(?<!LEFT )JOIN ", haupt), "only LEFT JOINs allowed"
    for spalte in ("mec.entity_count", "mhc.hot_entity_count", "msc.stale_entity_count"):
        assert f"COALESCE({spalte}::real" in haupt, f"{spalte} must be wrapped in COALESCE"
