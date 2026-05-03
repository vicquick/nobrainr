#!/usr/bin/env python3
"""Custom golden-set benchmark for nobrainr trust-score weighting.

Validates the 0.40 freshness + 0.25 contradiction + 0.20 outcome + 0.10
source-tier + 0.05 stability formula against a small (~30 query) hand-
curated golden set of real questions actually asked of the system.
Published benchmarks (LongMemEval/LoCoMo) don't measure trust-aware
reordering — this is the empirical validation of our unique IP.

Methodology:
  - Read /opt/nobrainr/scripts/golden_set/golden.jsonl
    Each line: {"q": "...", "expected_top3": ["mem_id_1", "mem_id_2", "mem_id_3"]}
  - Run search_memories two ways:
      A) "trust-on"  — current behaviour (RRF + trust-score tie-break)
      B) "trust-off" — RRF only, ignore trust_score
  - Score nDCG@3 + MRR + top-1 hit rate for each ablation.
  - Print delta + write JSON.

Edit golden.jsonl manually after running the curator script
(extract_real_queries.sql + manual annotation) for ground truth.

Usage:
  docker exec <nb> python3 /tmp/golden_set/run_golden.py
"""
import argparse
import asyncio
import json
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

for p in ("/app/src", "/opt/nobrainr/src"):
    if Path(p).is_dir() and p not in sys.path:
        sys.path.insert(0, p)

from nobrainr.db import queries  # noqa: E402
from nobrainr.embeddings.ollama import embed_text  # noqa: E402

GOLDEN_FILE = Path(__file__).parent / "golden.jsonl"
OUT_DIR = Path(__file__).parent / "results"


def dcg(relevances: list[float]) -> float:
    """Discounted cumulative gain."""
    return sum(r / (1.0 + i) for i, r in enumerate(relevances))


def score_one(retrieved_ids: list[str], expected_top3: list[str]) -> dict:
    """Compute MRR, top1, nDCG@3 for a single result list against ground truth."""
    expected_set = set(expected_top3)
    ranks = [i + 1 for i, mid in enumerate(retrieved_ids) if mid in expected_set]
    mrr = (1.0 / ranks[0]) if ranks else 0.0
    top1 = 1.0 if retrieved_ids and retrieved_ids[0] in expected_set else 0.0

    # nDCG@3 with binary relevance (1 if in expected, 0 otherwise).
    rel_at_k = [1.0 if mid in expected_set else 0.0 for mid in retrieved_ids[:3]]
    ideal_at_k = [1.0] * min(3, len(expected_top3))
    dcg_score = dcg(rel_at_k)
    idcg_score = dcg(ideal_at_k) if ideal_at_k else 1.0
    ndcg = dcg_score / idcg_score if idcg_score else 0.0
    return {"mrr": mrr, "top1": top1, "ndcg@3": ndcg}


async def search(query: str, k: int = 10, trust_aware: bool = True) -> list[str]:
    """Direct DB call mirroring smart-recall."""
    embedding = await embed_text(query)
    rows = await queries.search_memories(
        embedding=embedding, limit=k, threshold=0.15, text_query=query,
    )
    if not trust_aware:
        # Re-sort by RRF score only (drop trust tie-break). queries.search
        # _memories already returns rrf_score in the row dict.
        rows = sorted(rows, key=lambda r: r.get("rrf_score") or r.get("relevance", 0), reverse=True)
    return [str(r["id"]) for r in rows]


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--golden", default=str(GOLDEN_FILE))
    ap.add_argument("--k", type=int, default=10)
    args = ap.parse_args()

    if not Path(args.golden).is_file():
        print(f"  golden file not found: {args.golden}")
        print(f"  expected JSONL like: {{\"q\": \"...\", \"expected_top3\": [\"mem_id\", ...]}}")
        sys.exit(1)

    queries_list: list = []
    with open(args.golden) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            queries_list.append(json.loads(line))

    print(f"golden queries: {len(queries_list)}")

    runs = {"trust-on": [], "trust-off": []}
    for item in queries_list:
        q = item["q"]
        expected = item["expected_top3"]
        for mode in ("trust-on", "trust-off"):
            ids = await search(q, k=args.k, trust_aware=(mode == "trust-on"))
            score = score_one(ids, expected)
            runs[mode].append({"q": q, "expected": expected, "got": ids[:5], **score})

    print("\n═══ aggregated metrics ═══")
    print(f"{'mode':<10}  {'mrr':>6}  {'top1':>6}  {'ndcg@3':>8}")
    for mode, items in runs.items():
        n = len(items) or 1
        mrr = sum(i["mrr"] for i in items) / n
        top1 = sum(i["top1"] for i in items) / n
        ndcg = sum(i["ndcg@3"] for i in items) / n
        print(f"{mode:<10}  {mrr:>6.3f}  {top1:>6.3f}  {ndcg:>8.3f}")

    OUT_DIR.mkdir(exist_ok=True)
    out = {
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "n_queries": len(queries_list), "k": args.k,
        "runs": runs,
    }
    fn = OUT_DIR / f"golden_{int(time.time())}.json"
    fn.write_text(json.dumps(out, indent=2, default=str))
    print(f"wrote {fn}")


if __name__ == "__main__":
    asyncio.run(main())
