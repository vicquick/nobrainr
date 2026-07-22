"""Retrieval eval harness — Recall@10, MRR, nDCG@10 over a golden set.

Why this exists: up through 2026-04-18 we had no way to tell whether a
config change (embedding model swap, reranker tweak, RRF weights, HNSW
ef_search) actually improved retrieval or silently regressed it. The
harness runs a hand-labeled query set through the same path the MCP
exposes to agents, computes three standard IR metrics, and writes each
sweep to eval_runs so the dashboard can chart the trend.

Metrics (binary relevance — a memory is either in the expected set or
not):
  * Recall@K  — fraction of expected memories recovered in top-K.
  * MRR       — mean reciprocal rank of the FIRST relevant hit across
                queries. Rewards getting at least one relevant memory
                high in the list.
  * nDCG@K    — discounted cumulative gain normalized by ideal, so it
                rewards stacking multiple relevants near the top.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from typing import Any

from nobrainr.config import settings
from nobrainr.db.pool import get_pool

logger = logging.getLogger("nobrainr")


@dataclass
class QueryEval:
    query_id: str
    query: str
    expected_ids: set[str]
    returned_ids: list[str]
    recall_at_k: float
    reciprocal_rank: float
    ndcg_at_k: float

    def to_json(self) -> dict[str, Any]:
        return {
            "query_id": self.query_id,
            "query": self.query,
            "expected": sorted(self.expected_ids),
            "returned": self.returned_ids,
            "recall_at_k": round(self.recall_at_k, 4),
            "reciprocal_rank": round(self.reciprocal_rank, 4),
            "ndcg_at_k": round(self.ndcg_at_k, 4),
        }


def _dcg(rels: list[int]) -> float:
    """Discounted cumulative gain — binary relevance gains, log2 discount."""
    return sum(rel / math.log2(idx + 2) for idx, rel in enumerate(rels))


def _ndcg_at_k(returned: list[str], expected: set[str], k: int) -> float:
    topk = returned[:k]
    rels = [1 if rid in expected else 0 for rid in topk]
    dcg = _dcg(rels)
    # Ideal: min(k, |expected|) ones stacked at the top.
    ideal_rels = [1] * min(k, len(expected)) + [0] * max(0, k - len(expected))
    idcg = _dcg(ideal_rels)
    return dcg / idcg if idcg > 0 else 0.0


def _reciprocal_rank(returned: list[str], expected: set[str]) -> float:
    for i, rid in enumerate(returned, start=1):
        if rid in expected:
            return 1.0 / i
    return 0.0


#: Top-hit similarity/relevance above this = the system is "confident" —
#: an abstention question answered confidently is a fabrication risk.
ABSTENTION_CONFIDENCE_BAR = 0.55


def _abstention_passes(results: list[dict]) -> bool:
    """True if the system correctly does NOT confidently answer.

    Pass: empty results, or the best hit's similarity/relevance stays
    under ABSTENTION_CONFIDENCE_BAR. A high-confidence top hit on a
    fact that never existed is the failure this metric exists to catch.
    """
    if not results:
        return True
    top = results[0]
    score = top.get("similarity") or top.get("relevance") or 0.0
    return score < ABSTENTION_CONFIDENCE_BAR


def _recall_at_k(returned: list[str], expected: set[str], k: int) -> float:
    if not expected:
        return 0.0
    hits = sum(1 for rid in returned[:k] if rid in expected)
    return hits / len(expected)


async def load_active_golden_queries() -> list[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id::text AS id, query, expected_ids, tags
            FROM eval_golden_queries
            WHERE active
            ORDER BY created_at ASC
            """
        )
        return [
            {
                "id": r["id"],
                "query": r["query"],
                "expected_ids": [str(eid) for eid in (r["expected_ids"] or [])],
                "tags": list(r["tags"] or []),
            }
            for r in rows
        ]


async def run_retrieval_eval(
    *,
    k: int = 10,
    model_tag: str | None = None,
    notes: str | None = None,
    search_fn=None,
) -> dict[str, Any]:
    """Run the full golden set, compute metrics, persist to eval_runs.

    search_fn lets callers inject a search function for A/B testing
    (e.g. swap the MCP memory_search for a variant). Default calls the
    live memory_search tool.
    """
    if search_fn is None:
        from nobrainr.mcp.server import memory_search as _live_search

        async def _default_search(query: str, limit: int) -> list[dict]:
            return await _live_search(query=query, limit=limit)

        search_fn = _default_search

    goldens = await load_active_golden_queries()
    if not goldens:
        return {"status": "no_golden_set", "query_count": 0}

    per_query: list[QueryEval] = []
    abstention_pass = abstention_total = 0
    for g in goldens:
        expected = set(g["expected_ids"])
        is_abstention = "abstention" in (g.get("tags") or [])
        try:
            results = await search_fn(g["query"], k)
        except Exception:
            logger.exception("eval search failed for query %s", g["id"])
            if is_abstention:
                # a failed search abstains by definition — but count it as
                # an error-pass so a broken pipeline can't ace abstention
                abstention_total += 1
                continue
            # Record a zeroed row so the query isn't silently skipped.
            per_query.append(
                QueryEval(
                    query_id=g["id"],
                    query=g["query"],
                    expected_ids=expected,
                    returned_ids=[],
                    recall_at_k=0.0,
                    reciprocal_rank=0.0,
                    ndcg_at_k=0.0,
                )
            )
            continue
        if is_abstention:
            # BEAM/LoCoMo `_abs` pattern: the fact does NOT exist in the
            # corpus. Correct behavior = return nothing confident. Pass if
            # no results, or the top hit is below the confidence bar.
            abstention_total += 1
            if _abstention_passes(results):
                abstention_pass += 1
            continue
        returned_ids = [r["id"] for r in results]
        per_query.append(
            QueryEval(
                query_id=g["id"],
                query=g["query"],
                expected_ids=expected,
                returned_ids=returned_ids,
                recall_at_k=_recall_at_k(returned_ids, expected, k),
                reciprocal_rank=_reciprocal_rank(returned_ids, expected),
                ndcg_at_k=_ndcg_at_k(returned_ids, expected, k),
            )
        )

    n = len(per_query)
    recall = sum(q.recall_at_k for q in per_query) / n if n else 0.0
    mrr = sum(q.reciprocal_rank for q in per_query) / n if n else 0.0
    ndcg = sum(q.ndcg_at_k for q in per_query) / n if n else 0.0

    config = {
        "k": k,
        "reranker_enabled": settings.reranker_enabled,
        "reranker_backend": settings.reranker_backend,
        "embedding_model": settings.embedding_model,
        "chat_model": settings.chat_model or settings.extraction_model,
        # abstention (golden-v3): correct-decline rate on facts that do
        # not exist in the corpus. None until abstention goldens are seeded.
        "abstention_total": abstention_total,
        "abstention_rate": (
            round(abstention_pass / abstention_total, 4)
            if abstention_total else None
        ),
    }

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO eval_runs (
                model_tag, embedding_model, reranker_model,
                query_count, recall_at_10, mrr, ndcg_at_10,
                per_query, config, notes
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9::jsonb, $10)
            RETURNING id, ran_at
            """,
            model_tag or settings.extraction_model,
            settings.embedding_model,
            settings.reranker_model,
            n,
            recall,
            mrr,
            ndcg,
            json.dumps([q.to_json() for q in per_query]),
            json.dumps(config),
            notes,
        )
        run_id = str(row["id"])
        ran_at = row["ran_at"].isoformat()

    return {
        "status": "ok",
        "run_id": run_id,
        "ran_at": ran_at,
        "query_count": n,
        "recall_at_10": round(recall, 4),
        "mrr": round(mrr, 4),
        "ndcg_at_10": round(ndcg, 4),
        "model_tag": model_tag,
    }


async def latest_eval_runs(limit: int = 20) -> list[dict]:
    """Small helper for the dashboard trend view."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id::text, ran_at, model_tag, embedding_model,
                   reranker_model, query_count,
                   recall_at_10, mrr, ndcg_at_10
            FROM eval_runs
            ORDER BY ran_at DESC
            LIMIT $1
            """,
            limit,
        )
        return [dict(r) for r in rows]
