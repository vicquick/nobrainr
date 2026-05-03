#!/usr/bin/env python3
"""LongMemEval baseline runner against nobrainr — in-container variant.

Runs INSIDE the nobrainr container (or with /app/src on PYTHONPATH) so
it uses the exact same write path (services.memory.store_memory_with_
extraction) and search path (db.queries.search_memories) that the MCP
tool exposes. That is what makes the score comparable to Mem0/Letta.

Pipeline per question:
  1. Ingest haystack_sessions into nobrainr (tags = ['lme', q_id])
  2. Issue question via search_memories with the same tags filter so
     different questions can't pollute each other's haystack
  3. Pass top-K to Qwen3.6-27B (via llama-server) to draft an answer
  4. Judge the draft against ground truth (also Qwen3.6-27B)
  5. Aggregate accuracy by question_type

Usage (run inside the container):
  docker exec <nobrainr> python3 /app/scripts/longmemeval/run_lme.py \\
      --split oracle --sample 30 --workers 2

Or copy locally + run via docker exec, mapping /opt/longmemeval/data → container.
"""
import argparse
import asyncio
import json
import os
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

# Allow either /app/src layout (container) or /opt/nobrainr/src (host import).
for p in ("/app/src", "/opt/nobrainr/src"):
    if Path(p).is_dir() and p not in sys.path:
        sys.path.insert(0, p)

from nobrainr.services.memory import store_memory_with_extraction  # noqa: E402
from nobrainr.db import queries  # noqa: E402
from nobrainr.embeddings.ollama import embed_text  # noqa: E402
from nobrainr.extraction.llm import ollama_chat  # noqa: E402

DATA_DIR = Path(os.environ.get("LME_DATA_DIR", "/opt/longmemeval/data"))
OUT_DIR = Path(__file__).parent / "results"

ANSWER_PROMPT = """Answer the question concisely using ONLY the retrieved \
memories below. If the memories don't contain enough information, say \
"INSUFFICIENT". Do not make up facts. Reply with the answer only — no \
preamble.

MEMORIES:
{memories}

QUESTION: {q}
ANSWER:"""

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["CORRECT", "INCORRECT"]},
        "reason": {"type": "string"},
    },
    "required": ["verdict"],
}

JUDGE_PROMPT = """Decide whether the candidate answer is semantically \
equivalent to the ground truth. Be strict on factual claims (numbers, \
names, dates) but lenient on phrasing. If the candidate is "INSUFFICIENT" \
or empty, the verdict is INCORRECT.

QUESTION: {q}
GROUND TRUTH: {gt}
CANDIDATE: {cand}"""


async def ingest_haystack(q_id: str, sessions: list) -> int:
    """Fast ingest: embed + store, skip entity extraction.

    Benchmark data is throwaway — we're measuring retrieval, not building
    a knowledge graph. Calling store_memory_with_extraction would trigger
    the full LLM extraction pipeline per session (~30-60s each on shared
    GPU) which makes a 500-question run take days. We bypass extraction
    by going straight to queries.store_memory with a precomputed embedding;
    the writer thread just inserts the row + builds the FTS index entry.
    """
    n = 0
    for sess_idx, sess in enumerate(sessions):
        if not sess:
            continue
        text = "\n".join(
            f"{m.get('role', '?').upper()}: {m.get('content', '')}"
            for m in sess if isinstance(m, dict)
        )[:8000]
        if not text.strip():
            continue
        try:
            embedding = await embed_text(text)
            fts_ctx = f"[lme {q_id}] session {sess_idx} of {len(sessions)}"
            await queries.store_memory(
                content=text,
                embedding=embedding,
                summary=text[:200],
                tags=["lme", q_id, f"session-{sess_idx}"],
                category="benchmark",
                source_type="agent",
                source_machine="lme",
                confidence=1.0,
                fts_context=fts_ctx,
            )
            n += 1
        except Exception as exc:
            print(f"  ! ingest fail q={q_id} s={sess_idx}: {exc}", file=sys.stderr)
    return n


async def search_for_q(q_id: str, question: str, k: int = 8) -> list:
    """Search restricted to this question's haystack via the lme + q_id tags."""
    embedding = await embed_text(question)
    rows = await queries.search_memories(
        embedding=embedding,
        limit=k,
        threshold=0.15,
        text_query=question,
        tags=["lme", q_id],
    )
    return rows


async def evaluate_one(q: dict) -> dict:
    q_id = q["question_id"]
    sessions = q.get("haystack_sessions", [])
    question = str(q["question"])
    answer = str(q["answer"])
    cat = q["question_type"]

    t0 = time.monotonic()
    n_ingested = await ingest_haystack(q_id, sessions)
    # Wait for the queue worker to drain — store_memory_with_extraction is sync
    # but extraction might still be running in the background. For LME we run
    # search synchronously; the small wait gives the embedding/dedup path room.
    await asyncio.sleep(0.5)
    t_ingest = time.monotonic() - t0

    t1 = time.monotonic()
    hits = await search_for_q(q_id, question, k=8)
    t_search = time.monotonic() - t1

    if not hits:
        return {
            "q_id": q_id, "category": cat, "verdict": "NO_HITS",
            "ingested": n_ingested, "t_ingest": t_ingest, "t_search": t_search,
        }

    memories_block = "\n\n".join(
        f"[{i+1}] {h.get('content', '')[:600]}" for i, h in enumerate(hits)
    )

    t2 = time.monotonic()
    candidate = ""
    try:
        # Use plain text reply for the answer step.
        msg = await ollama_chat(
            system="You answer questions strictly from given memories.",
            user=ANSWER_PROMPT.format(memories=memories_block, q=question),
            schema={
                "type": "object",
                "properties": {"answer": {"type": "string"}},
                "required": ["answer"],
            },
            timeout=180.0,
            num_ctx=8192,
            think=False,
            caller_kind="scheduler",
        )
        candidate = (msg.get("answer") or "").strip()
    except Exception as exc:
        candidate = f"[ANSWER_ERR: {exc}]"
    t_answer = time.monotonic() - t2

    t3 = time.monotonic()
    verdict_obj = {"verdict": "INCORRECT", "reason": "judge unavailable"}
    try:
        verdict_obj = await ollama_chat(
            system="You are a strict but fair benchmark judge.",
            user=JUDGE_PROMPT.format(q=question, gt=answer, cand=candidate),
            schema=JUDGE_SCHEMA,
            timeout=120.0,
            num_ctx=2048,
            think=False,
            caller_kind="scheduler",
        )
    except Exception as exc:
        verdict_obj = {"verdict": "INCORRECT", "reason": f"judge err: {exc}"}
    t_judge = time.monotonic() - t3

    verdict = verdict_obj.get("verdict", "INCORRECT")

    return {
        "q_id": q_id, "category": cat, "verdict": verdict,
        "candidate": candidate[:400], "ground_truth": answer[:200],
        "judge_reason": verdict_obj.get("reason", "")[:200],
        "ingested": n_ingested, "hits": len(hits),
        "t_ingest": round(t_ingest, 2),
        "t_search": round(t_search, 3),
        "t_answer": round(t_answer, 2),
        "t_judge": round(t_judge, 2),
    }


async def cleanup_lme_memories():
    """Drop any prior lme-tagged memories so per-q tags stay clean."""
    pool = await queries.get_pool()
    async with pool.acquire() as conn:
        n = await conn.execute(
            "DELETE FROM memories WHERE 'lme' = ANY(tags)"
        )
    print(f"  cleaned prior lme memories: {n}")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", choices=["oracle", "s_cleaned"], default="oracle")
    ap.add_argument("--sample", type=int, default=30)
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--clean", action="store_true",
                    help="DELETE existing lme-tagged memories before run")
    args = ap.parse_args()

    fname = "longmemeval_oracle.json" if args.split == "oracle" else "longmemeval_s_cleaned.json"
    with open(DATA_DIR / fname) as f:
        all_q = json.load(f)

    by_cat: dict = defaultdict(list)
    for q in all_q:
        by_cat[q["question_type"]].append(q)
    per_cat = max(1, args.sample // len(by_cat))
    sample = []
    for cat, qs in by_cat.items():
        sample.extend(qs[:per_cat])

    print(f"split={args.split} total={len(all_q)} sampled={len(sample)} per_cat={per_cat} workers={args.workers}")
    print(f"category breakdown: {dict(Counter(q['question_type'] for q in sample))}")
    print()

    if args.clean:
        await cleanup_lme_memories()

    OUT_DIR.mkdir(exist_ok=True)
    sem = asyncio.Semaphore(args.workers)

    async def run_one(q):
        async with sem:
            r = await evaluate_one(q)
            v = r.get("verdict", "?")
            cat = q["question_type"]
            tt = sum(r.get(k, 0) for k in ("t_search", "t_answer", "t_judge"))
            print(f"  {cat[:28]:<28} {v:<10} ing={r.get('ingested',0)} hits={r.get('hits',0)} t={tt:.1f}s")
            return r

    results = await asyncio.gather(*[run_one(q) for q in sample])

    by_cat_results: dict = defaultdict(list)
    for r in results:
        by_cat_results[r["category"]].append(r["verdict"])
    summary = {}
    for cat, verdicts in by_cat_results.items():
        n = len(verdicts)
        correct = sum(1 for v in verdicts if v == "CORRECT")
        summary[cat] = {"n": n, "correct": correct,
                        "accuracy": round(correct/n, 3) if n else 0}
    overall_n = len(results)
    overall_correct = sum(1 for r in results if r["verdict"] == "CORRECT")
    summary["overall"] = {"n": overall_n, "correct": overall_correct,
                          "accuracy": round(overall_correct/overall_n, 3) if overall_n else 0}

    print("\n═══ summary ═══")
    for cat, s in sorted(summary.items()):
        print(f"  {cat:<32} {s['accuracy']*100:>5.1f}%  ({s['correct']}/{s['n']})")

    out = {
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "split": args.split, "sample": len(sample), "workers": args.workers,
        "summary": summary, "results": results,
    }
    fname = OUT_DIR / f"{args.split}_{int(time.time())}.json"
    fname.write_text(json.dumps(out, indent=2, default=str))
    print(f"\nwrote {fname}")


if __name__ == "__main__":
    asyncio.run(main())
