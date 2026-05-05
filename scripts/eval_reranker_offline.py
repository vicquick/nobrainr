"""Offline reranker quality eval — recall@k against historical feedback.

Replays real (query_text, useful_memory_id) pairs from memory_outcomes
through the live search + rerank pipeline and measures where the
known-good memory landed. Output:

  recall@1    fraction of queries where useful memory was top-1
  recall@3    fraction where it was in top-3
  recall@10   fraction where it was in top-10
  mrr         mean reciprocal rank (1/rank averaged)

Use as a regression gate before changing reranker params/model.

Run: python -m scripts.eval_reranker_offline [--limit N] [--min-rank 5]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("eval_reranker")


async def main(limit: int, min_rank_threshold: int, store: bool) -> dict:
    from nobrainr.db.pool import get_pool
    from nobrainr.db import queries
    from nobrainr.embeddings.ollama import embed_text
    from nobrainr.config import settings

    pool = await get_pool()

    # Pull positive feedback rows where we know which memory was useful for
    # which query. Limit to manual feedback (was_useful=true and not auto:*)
    # so the ground truth is real signal, not bootstrapping our own bias.
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT memory_id, query_text, result_rank, created_at
            FROM memory_outcomes
            WHERE was_useful = true
              AND query_text IS NOT NULL
              AND length(query_text) > 3
              AND (context IS NULL OR context NOT LIKE 'auto:%')
            ORDER BY created_at DESC
            LIMIT $1
            """,
            limit,
        )
    logger.info("Loaded %d positive feedback rows", len(rows))

    if not rows:
        logger.warning("No real positive feedback to evaluate. Run after users have given some.")
        return {"queries": 0}

    rank_hits = {1: 0, 3: 0, 10: 0}
    not_found = 0
    rr_sum = 0.0
    evaluated = 0

    for r in rows:
        target_id = str(r["memory_id"])
        q = r["query_text"]
        try:
            emb = await embed_text(q)
            results = await queries.search_memories(
                embedding=emb, limit=20, threshold=0.2, text_query=q,
            )
            if settings.reranker_enabled and results:
                from nobrainr.services.reranker import rerank
                results = await rerank(q, results, limit=20)
        except Exception:
            logger.exception("eval search failed for %s", q[:60])
            continue

        rank = None
        for i, m in enumerate(results, start=1):
            if str(m.get("id")) == target_id:
                rank = i
                break

        if rank is None:
            not_found += 1
        else:
            rr_sum += 1.0 / rank
            for k in rank_hits:
                if rank <= k:
                    rank_hits[k] += 1
        evaluated += 1
        if evaluated % 10 == 0:
            logger.info("Evaluated %d/%d queries", evaluated, len(rows))

    metrics = {
        "queries": evaluated,
        "recall@1": rank_hits[1] / evaluated if evaluated else 0,
        "recall@3": rank_hits[3] / evaluated if evaluated else 0,
        "recall@10": rank_hits[10] / evaluated if evaluated else 0,
        "mrr": rr_sum / evaluated if evaluated else 0,
        "not_found_count": not_found,
        "ran_at": datetime.now().isoformat(),
        "reranker_model": settings.reranker_model,
        "reranker_max_candidates": settings.reranker_max_candidates,
        "reranker_device": settings.reranker_device,
    }
    print(json.dumps(metrics, indent=2))

    if store:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO extraction_eval_runs (run_at, kind, metrics)
                VALUES (now(), 'reranker_offline', $1::jsonb)
                """,
                json.dumps(metrics),
            )

    return metrics


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=200,
                        help="max feedback rows to evaluate")
    parser.add_argument("--min-rank", type=int, default=5,
                        help="(reserved) only count queries where target appears within top-N")
    parser.add_argument("--store", action="store_true",
                        help="persist run metrics to extraction_eval_runs table")
    args = parser.parse_args()
    sys.exit(0 if asyncio.run(main(args.limit, args.min_rank, args.store)) else 1)
