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


async def search(query: str, k: int = 10, trust_aware: bool = True) -> list:
    """Direct DB call mirroring smart-recall.

    Returns full row dicts (not just ids) so the self-eval mode can pass
    snippets to the LLM judge for preference comparison.
    """
    embedding = await embed_text(query)
    rows = await queries.search_memories(
        embedding=embedding, limit=k, threshold=0.15, text_query=query,
    )
    if not trust_aware:
        # Re-sort by RRF score only (drop trust tie-break).
        rows = sorted(rows, key=lambda r: r.get("rrf_score") or r.get("relevance", 0), reverse=True)
    return rows


async def run_self_eval(queries_list: list, k: int) -> None:
    """Self-evaluation: for each query, get trust-on and trust-off result
    lists and ask the LLM judge which is more relevant. Output preference
    rate. No ground truth needed — useful when manual labels don't exist.
    """
    from nobrainr.extraction.llm import ollama_chat

    JUDGE_SCHEMA = {
        "type": "object",
        "properties": {
            "preferred": {"type": "string", "enum": ["A", "B", "TIE"]},
            "reason": {"type": "string"},
        },
        "required": ["preferred"],
    }

    PROMPT = """Two retrieval systems returned the following top-{k} results
for the same query. Which list is more relevant to the query? Be strict —
relevance means the memory directly answers or supports the query, not just
shares keywords. If both are equally good (or equally bad), answer TIE.

QUERY: {q}

LIST A (top {k}):
{a}

LIST B (top {k}):
{b}"""

    pref_a = pref_b = tie = errors = 0
    out = []
    for item in queries_list:
        q = item["q"]
        rows_on = await search(q, k=k, trust_aware=True)
        rows_off = await search(q, k=k, trust_aware=False)
        # Render summaries — shorter than full content keeps the judge focused
        def render(rows):
            return "\n".join(
                f"{i+1}. {r.get('summary') or (r.get('content','')[:120])}"
                for i, r in enumerate(rows[:k])
            )
        try:
            v = await ollama_chat(
                system="You judge which retrieval result list is more relevant.",
                user=PROMPT.format(q=q, k=k, a=render(rows_on), b=render(rows_off)),
                schema=JUDGE_SCHEMA, timeout=60.0, num_ctx=4096, think=False,
                caller_kind="scheduler",
            )
        except Exception as exc:
            errors += 1
            v = {"preferred": "TIE", "reason": f"err: {exc}"}
        # Trust-on is randomized to A or B per query? Simpler: always A=trust-on.
        result = v.get("preferred", "TIE")
        if result == "A":
            pref_a += 1
        elif result == "B":
            pref_b += 1
        else:
            tie += 1
        out.append({"q": q, "preferred": result, "reason": v.get("reason", "")[:160]})
        print(f"  preferred={result}  q={q[:60]}")

    n = len(queries_list) or 1
    print(f"\n═══ self-eval result (A=trust-on, B=trust-off) ═══")
    print(f"  trust-on win:  {pref_a}/{n}  ({pref_a/n*100:.0f}%)")
    print(f"  trust-off win: {pref_b}/{n}  ({pref_b/n*100:.0f}%)")
    print(f"  tie:           {tie}/{n}  ({tie/n*100:.0f}%)")
    print(f"  judge errors:  {errors}")

    OUT_DIR.mkdir(exist_ok=True)
    fn = OUT_DIR / f"self_eval_{int(time.time())}.json"
    fn.write_text(json.dumps({
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "n": n, "trust_on_win": pref_a, "trust_off_win": pref_b, "tie": tie,
        "errors": errors, "details": out,
    }, indent=2, default=str))
    print(f"wrote {fn}")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--golden", default=str(GOLDEN_FILE))
    ap.add_argument("--queries-txt", help="Plain queries.txt to use for self-eval mode (no ground truth)")
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--mode", choices=["ground-truth", "self-eval"], default="ground-truth")
    args = ap.parse_args()

    queries_list: list = []
    if args.mode == "self-eval":
        # Self-eval mode: read plain query strings, use LLM-as-judge for
        # preference between trust-on vs trust-off result lists per query.
        # No ground truth needed — useful for ablation when manual labels
        # don't exist yet. Output is the LLM's preference rate.
        src = args.queries_txt or str(Path(__file__).parent / "queries.txt")
        with open(src) as f:
            for line in f:
                q = line.strip()
                if q and not q.startswith("#"):
                    queries_list.append({"q": q})
        print(f"self-eval mode: {len(queries_list)} queries from {src}")
        await run_self_eval(queries_list, args.k)
        return

    if not Path(args.golden).is_file():
        print(f"  golden file not found: {args.golden}")
        print(f"  expected JSONL like: {{\"q\": \"...\", \"expected_top3\": [\"mem_id\", ...]}}")
        print(f"  or run with --mode self-eval --queries-txt queries.txt")
        sys.exit(1)

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
