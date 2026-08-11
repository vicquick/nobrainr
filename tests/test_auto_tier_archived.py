"""auto_tier_memories must never promote category='_archived' rows and must
force-align them to tier 3 — the label/tier drift that left 6.5K archived
rows searchable (2026-08-11)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from nobrainr.db import queries


def _mock_pool():
    conn = AsyncMock()
    conn.execute = AsyncMock(return_value="UPDATE 0")
    pool = MagicMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=False)
    pool.acquire = MagicMock(return_value=cm)
    return pool, conn


async def test_promotions_exclude_archived_and_alignment_runs():
    pool, conn = _mock_pool()
    with patch.object(queries, "get_pool", AsyncMock(return_value=pool)):
        counts = await queries.auto_tier_memories()

    statements = [c.args[0] for c in conn.execute.call_args_list]
    promotions = [s for s in statements if "SET tier = 0" in s or "SET tier = 1" in s or "SET tier = 2" in s]
    assert len(promotions) == 3
    for sql in promotions:
        assert "category IS DISTINCT FROM '_archived'" in sql

    alignment = [s for s in statements if "category = '_archived'" in s and "SET tier = 3" in s]
    assert len(alignment) == 1
    assert "tier < 3" in alignment[0]

    assert "archived_aligned" in counts
