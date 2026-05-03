#!/usr/bin/env python3
"""LoCoMo benchmark runner against nobrainr.

Letta reports 74% with gpt-4o-mini + filesystem grep. Mem0 reports 66.9%
(vector) / 68.4% (graph). LoCoMo is 10 multi-session conversations with
~200 QA each — we sample stratified across samples and categories.

Pipeline:
  1. For each sample: ingest the entire conversation as separate memories
     (one per session) tagged ['locomo', sample_id]. Once per sample.
  2. For each question: search_memories with tags filter; pass to 27B as
     answer; judge against ground truth.

Usage (in container):
  docker exec <nb> python3 /tmp/locomo/run_locomo.py --sample 30 --workers 2
"""
import argparse
import asyncio
import json
import os
import sys
import time
from collections import defaultdict, Counter
from datetime import datetime, timezone
from pathlib import Path

for p in ("/app/src", "/opt/nobrainr/src"):
    if Path(p).is_dir() and p not in sys.path:
        sys.path.insert(0, p)

from nobrainr.db import queries  # noqa: E402
from nobrainr.embeddings.ollama import embed_text  # noqa: E402
from nobrainr.extraction.llm import ollama_chat  # noqa: E402

DATA_FILE = Path(os.environ.get("LOCOMO_FILE", "/tmp/locomo/locomo10.json"))
OUT_DIR = Path(__file__).parent / "results"

ANSWER_PROMPT = """Answer the question concisely from the memories below.
If the memories don't contain enough info, say "INSUFFICIENT".

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

JUDGE_PROMPT = """Decide whether the candidate matches the ground truth.
Be strict on factual claims (numbers, names, dates) but lenient on phrasing.

QUESTION: {q}
GROUND TRUTH: {gt}
CANDIDATE: {cand}"""


def conversation_to_sessions(conv: dict) -> list[tuple[str, str]]:
    """Walk a LoCoMo `conversation` dict, return [(session_id, joined_text), ...]."""
    out = []
    for k, v in conv.items():
        if not k.startswith("session_"):
            continue
        if isinstance(v, list):
            text = "\n".join(
                f"{m.get('speaker', '?')}: {m.get('text', '') or m.get('clean_text', '')}"
                for m in v if isinstance(m, dict)
            )[:8000]
            if text.strip():
                out.append((k, text))
    return out


async def ingest_sample(sample_id: str, conversation: dict) -> int:
    sessions = conversation_to_sessions(conversation)
    n = 0
    for sess_id, text in sessions:
        try:
            embedding = await embed_text(text)
            await queries.store_memory(
                content=text,
                embedding=embedding,
                summary=text[:200],
                tags=["locomo", sample_id, sess_id],
                category="benchmark",
                source_type="agent",
                source_machine="locomo",
                confidence=1.0,
                fts_context=f"[locomo {sample_id}] {sess_id}",
            )
            n += 1
        except Exception as exc:
            print(f"  ! ingest fail {sample_id}/{sess_id}: {exc}", file=sys.stderr)
    return n


async def search_for_q(sample_id: str, question: str, k: int = 8) -> list:
    embedding = await embed_text(question)
    return await queries.search_memories(
        embedding=embedding, limit=k, threshold=0.15,
        text_query=question, tags=["locomo", sample_id],
    )


async def evaluate_one(sample_id: str, q: dict) -> dict:
    question = q["question"]
    answer = q["answer"]
    cat = str(q.get("category", "?"))

    t0 = time.monotonic()
    hits = await search_for_q(sample_id, question, k=8)
    t_search = time.monotonic() - t0
    if not hits:
        return {"sample": sample_id, "category": cat, "verdict": "NO_HITS",
                "t_search": round(t_search, 3)}

    memories_block = "\n\n".join(
        f"[{i+1}] {h.get('content','')[:600]}" for i, h in enumerate(hits)
    )

    t1 = time.monotonic()
    candidate = ""
    try:
        msg = await ollama_chat(
            system="You answer strictly from given memories.",
            user=ANSWER_PROMPT.format(memories=memories_block, q=question),
            schema={"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]},
            timeout=60.0, num_ctx=8192, think=False, caller_kind="scheduler",
        )
        candidate = (msg.get("answer") or "").strip()
    except Exception as exc:
        candidate = f"[ANSWER_ERR: {exc}]"
    t_answer = time.monotonic() - t1

    t2 = time.monotonic()
    try:
        v = await ollama_chat(
            system="You are a strict but fair judge.",
            user=JUDGE_PROMPT.format(q=question, gt=str(answer), cand=candidate),
            schema=JUDGE_SCHEMA, timeout=30.0, num_ctx=2048, think=False,
            caller_kind="scheduler",
        )
    except Exception as exc:
        v = {"verdict": "INCORRECT", "reason": f"judge err: {exc}"}
    t_judge = time.monotonic() - t2

    return {
        "sample": sample_id, "category": cat,
        "verdict": v.get("verdict", "INCORRECT"),
        "candidate": candidate[:300], "ground_truth": str(answer)[:200],
        "judge_reason": v.get("reason", "")[:160],
        "hits": len(hits),
        "t_search": round(t_search, 3),
        "t_answer": round(t_answer, 2),
        "t_judge": round(t_judge, 2),
    }


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=30, help="total questions")
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--per-sample-q", type=int, default=10,
                    help="max questions per LoCoMo sample (some are 200+)")
    args = ap.parse_args()

    with open(DATA_FILE) as f:
        data = json.load(f)
    print(f"locomo samples: {len(data)}")

    # Ingest all samples (cheap with fast path).
    t0 = time.monotonic()
    total_ingested = 0
    for s in data:
        sid = s["sample_id"]
        n = await ingest_sample(sid, s.get("conversation", {}))
        total_ingested += n
    print(f"ingested {total_ingested} sessions across {len(data)} samples in {time.monotonic()-t0:.1f}s")

    # Stratified sample: take first N from each sample, cap to args.sample total.
    qs = []
    per_sample = max(1, args.sample // len(data))
    for s in data:
        sid = s["sample_id"]
        for q in s.get("qa", [])[:min(args.per_sample_q, per_sample)]:
            qs.append((sid, q))
            if len(qs) >= args.sample:
                break
        if len(qs) >= args.sample:
            break

    print(f"evaluating {len(qs)} questions, workers={args.workers}")
    print(f"category breakdown: {dict(Counter(str(q.get('category','?')) for _, q in qs))}")
    print()

    sem = asyncio.Semaphore(args.workers)

    async def run_one(item):
        sid, q = item
        async with sem:
            r = await evaluate_one(sid, q)
            v = r.get("verdict", "?")
            tt = sum(r.get(k, 0) for k in ("t_search", "t_answer", "t_judge"))
            print(f"  {r['category'][:20]:<20} {v:<10} hits={r.get('hits',0)} t={tt:.1f}s")
            return r

    results = await asyncio.gather(*[run_one(it) for it in qs])

    # Aggregate
    by_cat = defaultdict(list)
    for r in results:
        by_cat[r["category"]].append(r["verdict"])
    summary = {}
    for cat, verdicts in by_cat.items():
        n = len(verdicts)
        correct = sum(1 for v in verdicts if v == "CORRECT")
        summary[cat] = {"n": n, "correct": correct, "accuracy": round(correct/n,3) if n else 0}
    overall = sum(1 for r in results if r["verdict"] == "CORRECT")
    summary["overall"] = {"n": len(results), "correct": overall,
                           "accuracy": round(overall/len(results),3) if results else 0}

    print("\n═══ summary ═══")
    for cat, s in sorted(summary.items()):
        print(f"  {cat:<28} {s['accuracy']*100:>5.1f}%  ({s['correct']}/{s['n']})")

    OUT_DIR.mkdir(exist_ok=True)
    out = {
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "sample": len(qs), "summary": summary, "results": results,
    }
    fn = OUT_DIR / f"locomo_{int(time.time())}.json"
    fn.write_text(json.dumps(out, indent=2, default=str))
    print(f"wrote {fn}")


if __name__ == "__main__":
    asyncio.run(main())
