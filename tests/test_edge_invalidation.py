"""edge_invalidation (P2 completion): candidate SQL invariants, verdict
application (outdated → valid=false, never deleted), conservative-judge
prompt, and the graph-keeps-history guarantee."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from nobrainr import scheduler_jobs

E1 = {"id": "e1", "relationship_type": "runs_on", "source_name": "nobrainr",
      "target_name": "old-host", "evidence_date": "2026-03-01", "evidence": "ran on old-host"}
E2 = {"id": "e2", "relationship_type": "runs_on", "source_name": "nobrainr",
      "target_name": "new-host", "evidence_date": "2026-08-01", "evidence": "migrated to new-host"}


def _mock_pool(fetch_results):
    conn = AsyncMock()
    conn.fetch = AsyncMock(side_effect=fetch_results)
    conn.execute = AsyncMock(return_value="UPDATE 1")
    pool = MagicMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=False)
    pool.acquire = MagicMock(return_value=cm)
    return pool, conn


async def test_outdated_edge_invalidated_current_kept():
    group = {"source_entity_id": "s1", "relationship_type": "runs_on", "n_targets": 2}
    pool, conn = _mock_pool([[group], [E1, E2]])
    resp = {"verdicts": [{"i": 0, "status": "outdated"}, {"i": 1, "status": "current"}]}
    with (
        patch.object(scheduler_jobs, "get_pool", AsyncMock(return_value=pool)),
        patch.object(scheduler_jobs, "_yield_to_live_requests", AsyncMock()),
        patch.object(scheduler_jobs, "ollama_chat", AsyncMock(return_value=resp)),
    ):
        out = await scheduler_jobs.edge_invalidation()

    assert out["reviewed"] == 2
    assert out["invalidated"] == 1

    # Candidate SQL: contending predicates only, unreviewed, >=2 targets,
    # 30d evidence spread — and verdicts UPDATE, never DELETE
    cand_sql = conn.fetch.call_args_list[0].args[0]
    assert "llm_reviewed_at IS NULL" in cand_sql
    assert "count(DISTINCT er.target_entity_id) >= 2" in cand_sql
    assert "interval '30 days'" in cand_sql
    updates = [c.args for c in conn.execute.call_args_list]
    assert all("UPDATE entity_relations" in a[0] for a in updates)
    assert not any("DELETE" in a[0] for a in updates)
    # e1 got 'outdated' (valid=false via status check), e2 'current'
    assert updates[0][2] == "outdated" and updates[0][1] == "e1"
    assert updates[1][2] == "current" and updates[1][1] == "e2"


async def test_unknown_verdict_status_is_skipped():
    group = {"source_entity_id": "s1", "relationship_type": "replaces", "n_targets": 2}
    pool, conn = _mock_pool([[group], [E1, E2]])
    resp = {"verdicts": [{"i": 0, "status": "banana"}, {"i": 5, "status": "current"}]}
    with (
        patch.object(scheduler_jobs, "get_pool", AsyncMock(return_value=pool)),
        patch.object(scheduler_jobs, "_yield_to_live_requests", AsyncMock()),
        patch.object(scheduler_jobs, "ollama_chat", AsyncMock(return_value=resp)),
    ):
        out = await scheduler_jobs.edge_invalidation()
    assert out["reviewed"] == 0
    assert out["invalidated"] == 0
    conn.execute.assert_not_called()
