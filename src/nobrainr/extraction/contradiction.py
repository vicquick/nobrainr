"""Write-time contradiction gate (T4, HEART PLAN, 2026-07-24).

The dedup gate (decide_write_action) catches near-duplicates at
similarity >= 0.78 and is prompt-biased toward ADD — by design. But
CONTRADICTIONS are rarely near-duplicates: "the LLM is gemma3" vs
"migrated to qwen3.6-27b" sits around 0.6 similarity, sails through as
ADD, and the stale fact keeps ranking as current truth until the 12h
reconciliation sweep maybe reaches it (measured 2026-07-24: supersede
throughput 389 per 1,918 new memories in 14d — inflow outruns cleanup
~5:1; 9/21 context cards below the 0.7 accuracy bar from inherited
staleness).

This gate runs AFTER an ADD decision, in the 0.55–0.78 similarity band,
against HIGH-TRUST (>= 0.7) working-state memories only, and supersedes
the old row immediately via the canonical supersede_memory() (column,
not metadata — the 2026-07-09 lesson).

Design verified against the field (2026-07-24 research pass):
- arxiv 2606.27472 "Supersede": the supersession gap is a memory-
  MAINTENANCE failure that model scale does not fix (gpt-5.4 drops
  92%→77% with self-maintained memory); the fix is a verifiable,
  DB-level which-version-is-current signal — exactly this gate + the
  superseded_by column.
- Mem0's ADD/UPDATE/DELETE is likewise a prompted (not learned) policy —
  production-standard.
- Zep/Graphiti invalidate temporal edges on contradiction at write time;
  this is the memory-level analogue.

Cost control: skipped entirely when no candidate is in band (one vector
query); at most one LLM call per write, judging up to 3 candidates in a
single batched prompt (under GPU contention, N sequential calls each
starve in the queue — 2026-07-22 lesson).
"""

from __future__ import annotations

import logging

from nobrainr.config import settings
from nobrainr.db.pool import get_pool
from nobrainr.extraction.llm import ollama_chat

logger = logging.getLogger("nobrainr")

#: claim kinds whose truth changes with the world — the only ones a new
#: write can make stale. Timeless kinds (creative/reference/historical)
#: are never superseded by this gate.
WORKING_STATE_KINDS = (
    "infra-state", "code-state", "incident-fix", "design-decision", "plan", "fact",
)

CONTRADICTION_SCHEMA = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "i": {"type": "integer"},
                    "verdict": {
                        "type": "string",
                        "enum": ["supersedes", "compatible", "unrelated"],
                    },
                    "reason": {"type": "string"},
                },
                "required": ["i", "verdict"],
            },
        }
    },
    "required": ["verdicts"],
}

_CONTRADICTION_SYSTEM = (
    "A NEW note was just written to a knowledge base. For each numbered "
    "OLD note, decide:\n"
    "- supersedes: NEW and OLD assert about the SAME subject and NEW "
    "makes OLD outdated or wrong (a migration happened, a value changed, "
    "a plan was replaced, a fix changed the state). The newer note is "
    "the truth.\n"
    "- compatible: both can be true (different subjects, different "
    "aspects, OLD is historical context NEW builds on).\n"
    "- unrelated: not about the same thing.\n"
    "Be conservative: 'supersedes' ONLY when keeping OLD as current "
    "truth would mislead someone. Additions of detail are compatible."
)


async def check_and_supersede(
    new_id: str,
    content: str,
    embedding: list[float],
) -> list[dict]:
    """Find high-trust working-state memories the new write contradicts
    and supersede them immediately. Returns the superseded rows' info.

    Never raises — a gate failure must not block or delay the write path.
    """
    if not settings.contradiction_gate_enabled:
        return []
    try:
        return await _run_gate(new_id, content, embedding)
    except Exception:
        logger.exception("contradiction gate failed (write %s unaffected)", new_id)
        return []


async def _run_gate(new_id: str, content: str, embedding: list[float]) -> list[dict]:
    pool = await get_pool()
    emb_str = "[" + ",".join(f"{x:.6f}" for x in embedding) + "]"
    async with pool.acquire() as conn:
        candidates = await conn.fetch(
            """
            SELECT id, summary, content, claim_kind, trust_score,
                   1 - (embedding <=> $1::halfvec) AS similarity
            FROM memories
            WHERE superseded_by IS NULL
              AND id <> $4::uuid
              AND claim_kind = ANY($5::text[])
              AND COALESCE(trust_score, 0) >= $6
              AND 1 - (embedding <=> $1::halfvec) BETWEEN $2 AND $3
            ORDER BY embedding <=> $1::halfvec
            LIMIT 3
            """,
            emb_str,
            settings.contradiction_gate_sim_min,
            settings.contradiction_gate_sim_max,
            new_id,
            list(WORKING_STATE_KINDS),
            settings.contradiction_gate_min_trust,
        )
    if not candidates:
        return []

    numbered = "\n\n".join(
        f"[{i}] ({c['claim_kind']}) {(c['summary'] or '')[:100]}\n{c['content'][:450]}"
        for i, c in enumerate(candidates)
    )
    resp = await ollama_chat(
        system=_CONTRADICTION_SYSTEM,
        user=f"NEW note:\n{content[:800]}\n\nOLD notes:\n{numbered}",
        schema=CONTRADICTION_SCHEMA,
        temperature=0.1,
        caller_kind="scheduler",  # queued write path — yields to live GPU use
        think=False,
    )

    from nobrainr.db.queries import supersede_memory

    superseded: list[dict] = []
    for v in (resp or {}).get("verdicts", []):
        if v.get("verdict") != "supersedes":
            continue
        idx = v.get("i")
        if not isinstance(idx, int) or not (0 <= idx < len(candidates)):
            continue
        old = candidates[idx]
        ok = await supersede_memory(
            str(old["id"]), new_id,
            reason=f"write-time contradiction gate: {(v.get('reason') or '')[:150]}",
        )
        if ok:
            superseded.append({
                "id": str(old["id"]),
                "reason": (v.get("reason") or "")[:150],
            })
            logger.info(
                "contradiction gate: %s superseded by %s (%s)",
                old["id"], new_id, (v.get("reason") or "")[:80],
            )
    return superseded
