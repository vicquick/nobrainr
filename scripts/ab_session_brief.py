"""M3: session-brief serving A/B — old raw hook path vs /api/brief (2026-07-24).

Mechanical, no LLM: for golden queries (known expected_ids), compare what
each hook serving strategy would have injected into the agent's prompt:

  RAW   — the v2 hook path: plain hybrid top-6 (api/smart-recall shape)
  BRIEF — the v3 hook path: trust-floored memories (cards counted
          separately — they answer from synthesis, not id match)

Metrics per strategy:
  coverage  — fraction of golden queries where at least one expected id
              was served (the agent was primed with the right context)
  noise     — fraction of served memories with trust_score < 0.6
              (the "80% filler" problem, measured at the serving layer)

Run in-container:  python3 /tmp/ab_brief.py --sample 60
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

from nobrainr.db import queries  # noqa: E402
from nobrainr.db.pool import get_pool  # noqa: E402
from nobrainr.embeddings.ollama import embed_text  # noqa: E402


async def main(sample: int) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        goldens = await conn.fetch(
            """
            SELECT query, expected_ids FROM eval_golden_queries
            WHERE active AND expected_ids IS NOT NULL
              AND array_length(expected_ids, 1) >= 1
              AND NOT ('abstention' = ANY(tags))
            ORDER BY random() LIMIT $1
            """,
            sample,
        )

    raw_cov = brief_cov = 0
    raw_served = raw_noise = brief_served = brief_noise = 0

    for g in goldens:
        expected = {str(e) for e in g["expected_ids"]}
        try:
            emb = await embed_text(g["query"])
            hits = await queries.search_memories(
                embedding=emb, limit=18, threshold=0.25, text_query=g["query"],
            )
        except Exception:
            continue

        raw = hits[:6]                       # v2 hook: top-6, no floor
        brief = [
            h for h in hits
            if (h.get("trust_score") is None or h["trust_score"] >= 0.6)
        ][:5]                                # v3 hook: trust-floored top-5

        if any(str(h["id"]) in expected for h in raw):
            raw_cov += 1
        if any(str(h["id"]) in expected for h in brief):
            brief_cov += 1
        raw_served += len(raw)
        brief_served += len(brief)
        raw_noise += sum(1 for h in raw
                         if (h.get("trust_score") or 0) < 0.6)
        brief_noise += sum(1 for h in brief
                           if (h.get("trust_score") or 0) < 0.6)

    n = len(goldens) or 1
    print(json.dumps({
        "n": len(goldens),
        "raw_hook":   {"coverage": round(raw_cov / n, 3),
                       "noise_rate": round(raw_noise / max(raw_served, 1), 3),
                       "served_per_q": round(raw_served / n, 1)},
        "brief_hook": {"coverage": round(brief_cov / n, 3),
                       "noise_rate": round(brief_noise / max(brief_served, 1), 3),
                       "served_per_q": round(brief_served / n, 1)},
        "note": "cards not counted for coverage (they answer via synthesis); "
                "this measures the memory-serving half of hook v3 only",
    }, indent=1))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=60)
    args = ap.parse_args()
    asyncio.run(main(args.sample))
