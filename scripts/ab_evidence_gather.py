"""A/B: evidence_gather vs deep_recall on multihop goldens (2026-07-22).

Runs in-container. Samples N multihop golden queries, runs both tools,
scores recall@10 against expected_ids. The LME-V2 question: does bounded
agentic gathering (search+SQL+read) beat the judge-gated re-query loop?

    docker exec <nb> python3 /tmp/ab_eg.py --sample 15
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

for c in [Path(__file__).resolve().parents[1] / "src", Path("/app/src"), Path("/app")]:
    if c.exists() and str(c) not in sys.path:
        sys.path.insert(0, str(c))
        break

from nobrainr.db.pool import get_pool  # noqa: E402


def _recall(returned: list[str], expected: set[str]) -> float:
    if not expected:
        return 0.0
    return sum(1 for r in returned[:10] if r in expected) / len(expected)


async def main(sample: int) -> None:
    from nobrainr.mcp import server

    dr = getattr(server.deep_recall, "fn", server.deep_recall)
    eg = getattr(server.evidence_gather, "fn", server.evidence_gather)

    pool = await get_pool()
    async with pool.acquire() as conn:
        goldens = await conn.fetch(
            """
            SELECT id, query, expected_ids FROM eval_golden_queries
            WHERE active AND 'multihop' = ANY(tags)
            ORDER BY random() LIMIT $1
            """,
            sample,
        )

    dr_scores, eg_scores = [], []
    for i, g in enumerate(goldens):
        expected = {str(e) for e in (g["expected_ids"] or [])}
        q = g["query"]
        try:
            r1 = await dr(query=q, limit=10, min_hops=2)
            s1 = _recall([str(m["id"]) for m in r1["memories"]], expected)
        except Exception as e:
            print(f"[{i}] deep_recall ERR {e}", flush=True)
            s1 = 0.0
        try:
            r2 = await eg(question=q, limit=10)
            s2 = _recall([str(m["id"]) for m in r2["evidence"]], expected)
        except Exception as e:
            print(f"[{i}] evidence_gather ERR {e}", flush=True)
            s2 = 0.0
        dr_scores.append(s1)
        eg_scores.append(s2)
        print(f"[{i+1}/{len(goldens)}] dr={s1:.2f} eg={s2:.2f} | {q[:70]}", flush=True)

    n = len(goldens) or 1
    print(json.dumps({
        "n": len(goldens),
        "deep_recall_recall@10": round(sum(dr_scores) / n, 3),
        "evidence_gather_recall@10": round(sum(eg_scores) / n, 3),
    }, indent=1))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=15)
    args = ap.parse_args()
    asyncio.run(main(args.sample))
