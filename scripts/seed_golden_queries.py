"""Seed the retrieval eval golden set.

Picks high-quality, high-importance memories (tier <= 1, quality_score
filled, decent length) across diverse source_types and asks the LLM to
draft a natural-language question whose answer is that memory. The
memory_id becomes the expected hit. Hand-review recommended afterwards
to weed out queries that are too generic, too specific, or ambiguous.

Caveats:
  - LLM-generated queries are a starting point, not ground truth. Plan
    to replace ~half with real questions pulled from session logs once
    we have a few weeks of trace_id data.
  - We only set ONE expected_id per query here. If the memory has near-
    duplicates in the store the eval will under-credit hits that
    surface the twin instead of the exact id. Add siblings to
    expected_ids during review.

Usage:
    docker cp scripts/seed_golden_queries.py <container>:/tmp/seed.py
    docker exec <container> python3 /tmp/seed.py --limit 30
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

# Make nobrainr importable both from repo root and container paths.
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

from nobrainr.db.pool import get_pool  # noqa: E402
from nobrainr.extraction.llm import ollama_chat  # noqa: E402

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("seed_golden")


CANDIDATE_SQL = """
    SELECT id::text AS id, content, summary, tags, category, source_type,
           quality_score, importance, tier, length(content) AS clen
    FROM memories
    WHERE tier <= $1
      AND quality_score IS NOT NULL
      AND quality_score >= $2
      AND length(content) BETWEEN $3 AND $4
      AND extraction_status = 'done'
    ORDER BY random()
    LIMIT $5
"""


PROMPT_SYSTEM = (
    "You write realistic search queries an engineer or researcher would type "
    "when looking up a specific memory. Output 5-12 words. "
    "Favor the specific terms in the memory over generic paraphrases so a "
    "semantic search has a real target to hit."
)


PROMPT_USER_TMPL = (
    "Memory content:\n{content}\n\n"
    "Memory tags: {tags}\n"
    "Memory category: {category}\n\n"
    "Write one natural-language question or phrase someone would search "
    "to find this specific memory."
)


_QUERY_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "5-12 word natural-language search query for the memory.",
        }
    },
    "required": ["query"],
}


async def _generate_query(memory: dict) -> str | None:
    content = (memory.get("summary") or memory["content"])[:1500]
    tags = ", ".join(memory.get("tags") or []) or "(none)"
    category = memory.get("category") or "(none)"
    user_prompt = PROMPT_USER_TMPL.format(
        content=content, tags=tags, category=category
    )
    try:
        resp = await ollama_chat(
            system=PROMPT_SYSTEM,
            user=user_prompt,
            schema=_QUERY_SCHEMA,
            temperature=0.3,
        )
        text = ""
        if isinstance(resp, dict):
            text = str(resp.get("query") or "").strip()
        if not text:
            return None
        text = text.strip("\"' ")
        if len(text) < 5 or len(text) > 200:
            return None
        return text
    except Exception:
        logger.exception("query gen failed for memory %s", memory["id"])
        return None


async def seed(
    *,
    limit: int,
    tier_max: int,
    quality_min: float,
    content_min: int,
    content_max: int,
    dry_run: bool,
) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            CANDIDATE_SQL,
            tier_max,
            quality_min,
            content_min,
            content_max,
            # Over-fetch so we can skip memories that fail LLM gen.
            limit * 2,
        )

        existing = await conn.fetchval(
            "SELECT COUNT(*) FROM eval_golden_queries WHERE active"
        )
        logger.info(
            "candidates=%d existing_active_golden=%d target_new=%d",
            len(rows),
            existing,
            limit,
        )

        inserted = 0
        for r in rows:
            if inserted >= limit:
                break
            mem = dict(r)
            q = await _generate_query(mem)
            if not q:
                continue
            payload = {
                "query": q,
                "expected_ids": [mem["id"]],
                "notes": f"seeded from {mem['source_type']} tier={mem['tier']} qs={mem['quality_score']:.2f}",
                "tags": ["seeded"] + list(mem.get("tags") or [])[:3],
            }
            logger.info(
                "candidate memory=%s → query=%r", mem["id"][:8], payload["query"]
            )
            if not dry_run:
                await conn.execute(
                    """
                    INSERT INTO eval_golden_queries (
                        query, expected_ids, notes, tags
                    )
                    VALUES ($1, $2::uuid[], $3, $4)
                    """,
                    payload["query"],
                    payload["expected_ids"],
                    payload["notes"],
                    payload["tags"],
                )
            inserted += 1

        print(
            json.dumps(
                {
                    "inserted": inserted,
                    "candidates_seen": len(rows),
                    "dry_run": dry_run,
                    "existing_active": existing,
                },
                indent=2,
            )
        )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--tier-max", type=int, default=1)
    ap.add_argument("--quality-min", type=float, default=0.6)
    ap.add_argument("--content-min", type=int, default=200)
    ap.add_argument("--content-max", type=int, default=6000)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    asyncio.run(
        seed(
            limit=args.limit,
            tier_max=args.tier_max,
            quality_min=args.quality_min,
            content_min=args.content_min,
            content_max=args.content_max,
            dry_run=args.dry_run,
        )
    )


if __name__ == "__main__":
    main()
