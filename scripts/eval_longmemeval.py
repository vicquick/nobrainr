"""LongMemEval-S CI gate runner.

Scores nobrainr's hybrid retrieval + reranker against the LongMemEval-S
dataset (500 chatbot-memory questions across 6 categories). Public
leaderboard for comparison: Mastra OM 84% / OMEGA 95.4% / Zep 71.2%
(retrieval-only recall@k differs from end-to-end accuracy — we measure
recall@1/3/10 + MRR).

Usage:
    python -m scripts.eval_longmemeval --sample 50 --tag lme:test
    python -m scripts.eval_longmemeval --full       # all 500
    python -m scripts.eval_longmemeval --cleanup --tag lme:test

Design notes:
- Ingests each question's haystack_sessions as nobrainr memories tagged
  'lme:eval' (or --tag), embedded + extracted via the regular pipeline.
- Searches with that tag filter so results are scoped to LME's data.
- A retrieval is "correct" if any memory in top-k has metadata.session_id
  in the question's answer_session_ids set.
- Persists run summary to extraction_eval_runs (kind='longmemeval_s') for
  trend tracking. Run weekly via the scheduler.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import sys
import time
from collections import defaultdict
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("eval_longmemeval")

DEFAULT_PATH = Path("/tmp/longmemeval/longmemeval_s_cleaned.json")
HF_URL = "https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/longmemeval_s_cleaned.json"


async def ensure_dataset(path: Path) -> list[dict]:
    """Download from HF on first run; cache locally."""
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        import httpx
        logger.info("Downloading LongMemEval-S to %s (~250MB)…", path)
        async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as c:
            resp = await c.get(HF_URL)
            resp.raise_for_status()
            path.write_bytes(resp.content)
    return json.loads(path.read_text())


def session_to_text(messages: list[dict]) -> str:
    """Flatten a chat session into a single text blob for ingest."""
    parts = []
    for m in messages:
        role = m.get("role", "")
        content = m.get("content", "")
        if isinstance(content, list):
            content = "\n".join(
                p.get("text", "") if isinstance(p, dict) else str(p)
                for p in content
            )
        parts.append(f"[{role}] {content}")
    return "\n".join(parts)


async def ingest_question(q: dict, tag: str) -> int:
    """Ingest a single question's haystack as memories. Returns count."""
    from nobrainr.embeddings.ollama import embed_text
    from nobrainr.db import queries

    sessions = q.get("haystack_sessions", [])
    session_ids = q.get("haystack_session_ids", [])
    dates = q.get("haystack_dates", [])
    question_id = q["question_id"]

    n = 0
    for i, msgs in enumerate(sessions):
        if not msgs:
            continue
        session_id = session_ids[i] if i < len(session_ids) else f"sess_{i}"
        date = dates[i] if i < len(dates) else None
        text = session_to_text(msgs)
        if not text.strip() or len(text) < 30:
            continue
        try:
            emb = await embed_text(text[:8000])
            await queries.store_memory(
                content=text,
                embedding=emb,
                summary=f"LME[{question_id}] session {session_id}",
                source_type="lme_session",
                source_machine="lme-eval",
                source_ref=session_id,
                category="longmemeval",
                tags=[tag, f"lme:{q['question_type']}"],
                metadata={
                    "lme_question_id": question_id,
                    "session_id": session_id,
                    "session_date": date,
                    "question_type": q["question_type"],
                },
            )
            n += 1
        except Exception:
            logger.exception("ingest failed for %s session %s", question_id, session_id)
    return n


async def score_question(q: dict, tag: str) -> dict:
    """Run search for a question, compute rank of correct session."""
    from nobrainr.embeddings.ollama import embed_text
    from nobrainr.db import queries
    from nobrainr.config import settings

    answer_ids = set(q.get("answer_session_ids") or [])
    if not answer_ids:
        return {"question_id": q["question_id"], "skipped": "no_answer_ids"}

    try:
        emb = await embed_text(q["question"])
        results = await queries.search_memories(
            embedding=emb, text_query=q["question"],
            limit=20, threshold=0.15, tags=[tag],
        )
        if settings.reranker_enabled and results:
            from nobrainr.services.reranker import rerank
            results = await rerank(q["question"], results, limit=20)
    except Exception as exc:
        return {"question_id": q["question_id"], "error": str(exc)}

    rank = None
    for i, r in enumerate(results, start=1):
        meta = r.get("metadata") or {}
        if isinstance(meta, str):
            try: meta = json.loads(meta)
            except: meta = {}
        sid = meta.get("session_id")
        if sid in answer_ids:
            rank = i
            break

    return {
        "question_id": q["question_id"],
        "question_type": q["question_type"],
        "rank": rank,
        "results_count": len(results),
    }


async def cleanup(tag: str) -> int:
    """Delete all memories tagged with the eval marker."""
    from nobrainr.db.pool import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        n = await conn.fetchval(
            "DELETE FROM memories WHERE $1 = ANY(tags) RETURNING (SELECT COUNT(*) FROM memories WHERE $1 = ANY(tags))",
            tag,
        )
    return int(n or 0)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=50, help="how many questions to evaluate")
    ap.add_argument("--full", action="store_true", help="evaluate all 500")
    ap.add_argument("--tag", default="lme:eval")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--skip-ingest", action="store_true",
                    help="data already ingested, just score")
    ap.add_argument("--cleanup", action="store_true",
                    help="delete all memories with --tag and exit")
    ap.add_argument("--store-run", action="store_true",
                    help="persist metrics to extraction_eval_runs")
    args = ap.parse_args()

    if args.cleanup:
        n = await cleanup(args.tag)
        print(f"deleted {n} memories tagged {args.tag}")
        return

    data = await ensure_dataset(DEFAULT_PATH)
    if args.full:
        questions = data
    else:
        # Stratified sample: roughly equal per question_type
        random.seed(args.seed)
        by_type = defaultdict(list)
        for q in data:
            by_type[q["question_type"]].append(q)
        per_type = max(1, args.sample // len(by_type))
        questions = []
        for qs in by_type.values():
            random.shuffle(qs)
            questions.extend(qs[:per_type])
        questions = questions[:args.sample]

    logger.info("Evaluating %d questions (tag=%s)", len(questions), args.tag)

    if not args.skip_ingest:
        t0 = time.time()
        for i, q in enumerate(questions, 1):
            n = await ingest_question(q, args.tag)
            if i % 5 == 0:
                logger.info("[%d/%d] ingested %d sessions for %s",
                            i, len(questions), n, q["question_id"])
        logger.info("Ingest finished in %.1fs", time.time() - t0)

    t0 = time.time()
    results = []
    for i, q in enumerate(questions, 1):
        r = await score_question(q, args.tag)
        results.append(r)
        if i % 10 == 0:
            logger.info("[%d/%d] scored", i, len(questions))
    logger.info("Score finished in %.1fs", time.time() - t0)

    # Compute metrics
    by_type = defaultdict(list)
    for r in results:
        by_type[r.get("question_type", "?")].append(r)

    def metrics(rs: list[dict]) -> dict:
        ranked = [r for r in rs if r.get("rank") is not None]
        not_found = [r for r in rs if r.get("rank") is None and "error" not in r and "skipped" not in r]
        n = len(rs)
        if n == 0:
            return {}
        ranks = [r["rank"] for r in ranked]
        return {
            "n": n,
            "recall@1": sum(1 for r in ranks if r <= 1) / n,
            "recall@3": sum(1 for r in ranks if r <= 3) / n,
            "recall@10": sum(1 for r in ranks if r <= 10) / n,
            "mrr": sum(1.0 / r for r in ranks) / n if n else 0,
            "not_found": len(not_found),
        }

    summary = {
        "overall": metrics(results),
        "by_type": {t: metrics(rs) for t, rs in by_type.items()},
        "config": {
            "tag": args.tag,
            "sample": len(questions),
            "questions_total": len(data),
        },
    }
    print(json.dumps(summary, indent=2))

    if args.store_run:
        from nobrainr.db.pool import get_pool
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO extraction_eval_runs (run_at, kind, metrics)
                VALUES (now(), 'longmemeval_s', $1::jsonb)
                """,
                json.dumps(summary),
            )
            logger.info("Run persisted to extraction_eval_runs")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()) or 0)
