"""Golden-set v2: expand eval to ~200 stratified queries (2026-07-05).

Why: the 30-query v1 set has ±0.10 run-to-run variance — too small to
tune retrieval against (recall@10 regressed 0.70→0.51 as the corpus
grew 48k→72k and we couldn't attribute it). memory_outcomes holds
query_text on only 2 of 101k rows, so real-query mining is not yet
viable (search_traces will fix that going forward); synthetic
generation with *paraphrase pressure* is the interim source.

Five ability types (LongMemEval-inspired, tagged on each row):
  lookup      — specific fact query, natural wording
  paraphrase  — vocabulary-mismatched: NO distinctive content words
  temporal    — "when did we ..." phrasing anchored on event framing
  multihop    — two memories sharing an entity; query needs the link;
                expected_ids carries BOTH memories
  procedural  — "how do we/how to" phrasing on ops/debugging memories

Usage:
    docker cp scripts/seed_golden_queries_v2.py <container>:/tmp/seed2.py
    docker exec <container> python3 /tmp/seed2.py --per-type 40 [--dry-run]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

_here = Path(__file__).resolve()
for candidate in [_here.parents[1] / "src", Path("/app/src"), Path("/app")]:
    if candidate.exists() and str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))
        break

from nobrainr.db.pool import get_pool  # noqa: E402
from nobrainr.extraction.llm import ollama_chat  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("seed_golden_v2")

CANDIDATE_SQL = """
    SELECT id::text AS id, content, summary, tags, category, source_type,
           to_char(created_at, 'Month YYYY') AS created_month
    FROM memories
    WHERE tier <= 2
      AND quality_score >= $1
      AND length(content) BETWEEN 250 AND 6000
      AND extraction_status = 'done'
      AND category = ANY($2::text[])
    ORDER BY random()
    LIMIT $3
"""

MULTIHOP_SQL = """
    SELECT e.name AS entity,
           m1.id::text AS id1, left(m1.content, 1200) AS c1,
           m2.id::text AS id2, left(m2.content, 1200) AS c2
    FROM entities e
    JOIN entity_memories em1 ON em1.entity_id = e.id
    JOIN entity_memories em2 ON em2.entity_id = e.id AND em2.memory_id > em1.memory_id
    JOIN memories m1 ON m1.id = em1.memory_id AND m1.tier <= 2 AND m1.quality_score >= 0.5
    JOIN memories m2 ON m2.id = em2.memory_id AND m2.tier <= 2 AND m2.quality_score >= 0.5
    WHERE e.community_id IS NOT NULL
      AND m1.metadata->>'conversation_id' IS DISTINCT FROM m2.metadata->>'conversation_id'
      AND length(m1.content) BETWEEN 250 AND 4000
      AND length(m2.content) BETWEEN 250 AND 4000
    ORDER BY random()
    LIMIT $1
"""

_SCHEMA = {
    "type": "object",
    "properties": {"query": {"type": "string"}},
    "required": ["query"],
}

PROMPTS = {
    "lookup": (
        "Write one natural 6-14 word search query an engineer would type to "
        "find this specific memory. Use the topic's real terms but phrase it "
        "as a question or need, not a copy of the text.",
    ),
    "paraphrase": (
        "Write one 6-14 word search query for this memory WITHOUT reusing "
        "any distinctive word from it — use synonyms and different framing "
        "(e.g. 'container' for 'docker', 'broke' for 'failed'). Entity names "
        "(products, hosts, people) may be kept. The query must still "
        "unambiguously target this memory's content.",
    ),
    "temporal": (
        "Write one 6-14 word search query with temporal framing, like "
        "'when did we <event>' or 'what changed with <topic> in <month>'. "
        "Anchor on the event in the memory. The memory is from "
        "{created_month}.",
    ),
    "procedural": (
        "Write one 6-14 word 'how do we ...' or 'how to ...' search query "
        "asking for the procedure/recipe this memory describes.",
    ),
}

MULTIHOP_PROMPT = (
    "Two memory notes both involve '{entity}'.\n\nNOTE A:\n{c1}\n\n"
    "NOTE B:\n{c2}\n\nWrite one 8-16 word search query whose full answer "
    "needs BOTH notes (connect them through {entity}). Do not copy "
    "distinctive phrases verbatim."
)

CATEGORIES = [
    "debugging", "infrastructure", "architecture", "tooling", "ops",
    "deployment", "pattern", "decision", "insight", "documentation",
]


async def _gen(system_extra: str, content: str) -> str | None:
    try:
        resp = await ollama_chat(
            system=(
                "You write realistic search queries to test a memory "
                "retrieval system. Output only the query. " + system_extra
            ),
            user=f"Memory content:\n{content[:1500]}",
            schema=_SCHEMA,
            temperature=0.4,
        )
        text = str((resp or {}).get("query") or "").strip().strip("\"' ")
        return text if 10 <= len(text) <= 200 else None
    except Exception:
        logger.exception("query gen failed")
        return None


async def seed(per_type: int, dry_run: bool) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        existing = {
            r["query"].lower()
            for r in await conn.fetch("SELECT query FROM eval_golden_queries")
        }

    inserted = 0

    async def _insert(query: str, expected: list[str], ability: str, note: str) -> None:
        nonlocal inserted
        if query.lower() in existing:
            return
        existing.add(query.lower())
        if dry_run:
            logger.info("[dry] %-11s %s -> %s", ability, query, expected)
            inserted += 1
            return
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO eval_golden_queries (query, expected_ids, notes, tags)
                VALUES ($1, $2::uuid[], $3, $4)
                """,
                query, expected, note, ["synthetic-v2", ability],
            )
        inserted += 1
        logger.info("[%3d] %-11s %s", inserted, ability, query)

    # Single-memory types
    for ability in ("lookup", "paraphrase", "temporal", "procedural"):
        cats = (
            ["debugging", "infrastructure", "ops", "deployment", "tooling"]
            if ability == "procedural" else CATEGORIES
        )
        async with pool.acquire() as conn:
            rows = await conn.fetch(CANDIDATE_SQL, 0.5, cats, per_type * 2)
        done = 0
        for r in rows:
            if done >= per_type:
                break
            prompt = PROMPTS[ability][0]
            if ability == "temporal":
                prompt = prompt.format(created_month=(r["created_month"] or "").strip())
            body = (r["summary"] or "") + "\n" + r["content"]
            q = await _gen(prompt, body)
            if q:
                await _insert(q, [r["id"]], ability, f"v2 {ability}; src={r['category']}")
                done += 1

    # Multi-hop pairs
    async with pool.acquire() as conn:
        pairs = await conn.fetch(MULTIHOP_SQL, per_type * 2)
    done = 0
    for p in pairs:
        if done >= per_type:
            break
        try:
            resp = await ollama_chat(
                system="You write realistic search queries to test a memory retrieval system. Output only the query.",
                user=MULTIHOP_PROMPT.format(entity=p["entity"], c1=p["c1"], c2=p["c2"]),
                schema=_SCHEMA,
                temperature=0.4,
            )
            q = str((resp or {}).get("query") or "").strip().strip("\"' ")
        except Exception:
            logger.exception("multihop gen failed")
            continue
        if q and 10 <= len(q) <= 200:
            await _insert(
                q, [p["id1"], p["id2"]], "multihop", f"v2 multihop via {p['entity']}"
            )
            done += 1

    logger.info("inserted %d golden queries (dry_run=%s)", inserted, dry_run)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-type", type=int, default=40)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    asyncio.run(seed(args.per_type, args.dry_run))
