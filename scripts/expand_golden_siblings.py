"""Expand eval_golden_queries.expected_ids with near-duplicate siblings.

Why: UUIDs in uuidv7() share a millisecond prefix, so chunked imports
produce many near-duplicate memories from the same document. The seeder
picks ONE as the gold expected_id, but the retriever honestly returns
a sibling chunk of the same concept. That scores as a miss even though
the retrieval is correct. Before this script baseline Recall@10 = 0.70;
the 9 apparent misses were near-duplicate siblings within top-3.

This walks every active golden query, finds memories whose embedding
has cosine similarity >= threshold to ANY expected_id, and adds them
to expected_ids. Idempotent — re-running converges.

Relevance is content-level (embedding cosine), not UUID identity, so
the eval measures "did the retriever find the right concept" instead
of "did it find this arbitrary chunk". That's the honest metric.

Usage:
    docker cp scripts/expand_golden_siblings.py <container>:/tmp/expand.py
    docker exec <container> python3 /tmp/expand.py --threshold 0.85 [--dry-run]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

_here = Path(__file__).resolve()
_candidates: list[Path] = []
try:
    _candidates.append(_here.parents[1] / "src")
except IndexError:
    pass
_candidates.extend([Path("/app/src"), Path("/app")])
for candidate in _candidates:
    if candidate.exists() and str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))
        break

from nobrainr.config import settings  # noqa: E402
from nobrainr.db.pool import get_pool  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("expand_golden")


# Find all memories whose embedding is within `threshold` cosine sim of ANY
# seed memory in `expected_ids`. Uses halfvec + HNSW so this scales to
# hundreds of golden queries without hurting. Model-alias guarded so a
# tag mismatch doesn't silently exclude 95% of the store like on 2026-04-08.
EXPAND_SQL = """
    WITH seed AS (
        SELECT embedding, embedding_model
        FROM memories
        WHERE id = ANY($1::uuid[])
          AND embedding IS NOT NULL
    ),
    neighbors AS (
        SELECT DISTINCT m.id
        FROM memories m, seed s
        WHERE m.embedding IS NOT NULL
          AND (m.embedding_model IS NULL
               OR m.embedding_model = ANY($3::text[]))
          AND 1 - (m.embedding <=> s.embedding) >= $2
    )
    SELECT ARRAY_AGG(id) FROM neighbors
"""


async def expand(threshold: float, dry_run: bool, max_per_query: int) -> None:
    pool = await get_pool()
    aliases = list(
        settings.embedding_model_aliases or [settings.embedding_model]
    )

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, query, expected_ids
            FROM eval_golden_queries
            WHERE active
            ORDER BY created_at ASC
            """
        )
        logger.info("scanning %d golden queries at threshold=%.2f", len(rows), threshold)

        total_before = 0
        total_after = 0
        total_added = 0

        for r in rows:
            current = list(r["expected_ids"] or [])
            before = len(current)
            total_before += before
            if not current:
                continue

            neighbors = await conn.fetchval(
                EXPAND_SQL, current, threshold, aliases
            )
            neighbor_ids = list(neighbors or [])

            # Union with the existing set. Cap to max_per_query so a
            # runaway cluster doesn't flood the golden set with a thousand
            # siblings — if a concept has >50 near-dupes, the retrieval
            # problem is dedup, not eval.
            merged_set = set(str(x) for x in current) | set(
                str(x) for x in neighbor_ids
            )
            if len(merged_set) > max_per_query:
                # Keep seeds + top-(max-seed) neighbors by similarity.
                seed_set = set(str(x) for x in current)
                non_seed = [mid for mid in merged_set if mid not in seed_set]
                # Re-rank non-seed by similarity to seed embedding.
                ranked = await conn.fetch(
                    """
                    SELECT m.id::text AS id,
                           MAX(1 - (m.embedding <=> s.embedding)) AS sim
                    FROM memories m, memories s
                    WHERE m.id = ANY($1::uuid[])
                      AND s.id = ANY($2::uuid[])
                      AND m.embedding IS NOT NULL
                      AND s.embedding IS NOT NULL
                    GROUP BY m.id
                    ORDER BY sim DESC
                    LIMIT $3
                    """,
                    non_seed, current, max_per_query - len(seed_set),
                )
                merged_set = seed_set | {row["id"] for row in ranked}

            after = len(merged_set)
            added = after - before
            total_after += after
            total_added += added

            if added > 0:
                logger.info(
                    "  %s  +%d siblings (%d → %d)   %s",
                    str(r["id"])[:8],
                    added,
                    before,
                    after,
                    r["query"][:70],
                )
                if not dry_run:
                    # Use set_config so audit trail distinguishes this from
                    # manual curation.
                    await conn.execute(
                        """
                        UPDATE eval_golden_queries
                        SET expected_ids = $1::uuid[], updated_at = now()
                        WHERE id = $2
                        """,
                        list(merged_set),
                        r["id"],
                    )

        logger.info(
            "done. expected_ids: before=%d after=%d added=%d (avg %.1f→%.1f per query)  dry_run=%s",
            total_before,
            total_after,
            total_added,
            total_before / len(rows) if rows else 0,
            total_after / len(rows) if rows else 0,
            dry_run,
        )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold", type=float, default=0.85,
                    help="cosine similarity threshold (default 0.85)")
    ap.add_argument("--max-per-query", type=int, default=50,
                    help="cap siblings per query (default 50)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    asyncio.run(expand(args.threshold, args.dry_run, args.max_per_query))


if __name__ == "__main__":
    main()
