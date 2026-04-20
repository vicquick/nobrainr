"""Extraction eval harness — qwen3.6 vs qwen3.5 A/B on entity/relationship overlap.

Why this exists: swapping the extraction LLM (e.g. qwen3.5 → qwen3.6) touches the
knowledge-graph quality AND the memory tags/categories — but we had no way to tell
if a new model is better, same, or worse before committing. This harness re-extracts
a fixed sample under a candidate model, compares entities + relationships against
the on-disk extraction produced by the incumbent, and uses the candidate itself as
an LLM judge for semantic equivalence (Qwen as self-judge keeps us off external
APIs per data-sovereignty policy).

Metrics per memory:
  * entity_f1:     token-set F1 on normalised canonical entity names
  * relation_f1:   F1 on (head, predicate, tail) triplet match
  * judge_score:   0-1 LLM-judge for semantic equivalence of the whole extraction

Means are recorded to extraction_eval_runs for dashboard comparison over time.
Gate: skip if a run already exists within one interval window — this is a SLOW
job (~1-2 min per sample) and there's no value running it every scheduler tick.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from nobrainr.config import settings
from nobrainr.db.pool import get_pool
from nobrainr.extraction.llm import ollama_chat

logger = logging.getLogger("nobrainr")


JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "semantic_equivalence": {
            "type": "number", "minimum": 0, "maximum": 1,
            "description": "0 = extractions describe totally different content, 1 = same meaning",
        },
        "reason": {"type": "string"},
    },
    "required": ["semantic_equivalence"],
}


@dataclass
class MemoryEval:
    memory_id: str
    entity_f1: float
    relation_f1: float
    judge_score: float


def _norm(name: str) -> str:
    """Lowercase + strip non-alnum for canonical name comparison."""
    return "".join(ch for ch in name.lower() if ch.isalnum() or ch.isspace()).strip()


def _f1(expected: set[str], got: set[str]) -> float:
    if not expected and not got:
        return 1.0
    if not expected or not got:
        return 0.0
    tp = len(expected & got)
    if tp == 0:
        return 0.0
    p = tp / len(got)
    r = tp / len(expected)
    return 2 * p * r / (p + r)


async def _judge_equivalence(
    content: str, incumbent_json: dict, candidate_json: dict, model: str,
) -> float:
    """Ask the LLM itself whether the two extractions describe the same memory."""
    try:
        result = await ollama_chat(
            system=(
                "You compare two structured extractions of the same source text "
                "and return a single score 0-1 for semantic equivalence. "
                "Ignore ordering, casing, and minor wording differences. "
                "1 = same facts captured; 0 = disjoint extractions."
            ),
            user=(
                f"SOURCE:\n{content[:2000]}\n\n"
                f"EXTRACTION A:\n{json.dumps(incumbent_json)[:2000]}\n\n"
                f"EXTRACTION B:\n{json.dumps(candidate_json)[:2000]}"
            ),
            schema=JUDGE_SCHEMA,
            model=model,
            timeout=300.0,
            think=False,
        )
        return float(result.get("semantic_equivalence", 0.0))
    except Exception:
        logger.exception("judge_equivalence failed")
        return 0.0


async def _extract_with(model: str, content: str) -> dict:
    """Call the extraction LLM with a specific model, bypassing the scheduler.

    Mirrors the logic of extraction.extractor._extract_single_page but lets us
    pin the model per-call for A/B — the production extractor is hardcoded to
    settings.extraction_model.
    """
    from nobrainr.extraction.extractor import SYSTEM_PROMPT
    from nobrainr.extraction.models import ExtractionResult

    user_prompt = f"Extract entities and relationships from this memory:\n\n{content[:6000]}"
    try:
        parsed = await ollama_chat(
            system=SYSTEM_PROMPT,
            user=user_prompt,
            schema=ExtractionResult.model_json_schema(),
            model=model,
            timeout=300.0,
            think=False,
        )
        result = ExtractionResult.model_validate(parsed)
        return {
            "entities": [
                {"name": e.name, "type": e.entity_type} for e in result.entities
            ],
            "relationships": [
                {"head": r.source, "predicate": r.relationship_type, "tail": r.target}
                for r in result.relationships
            ],
        }
    except Exception:
        logger.exception("extract_with(%s) failed", model)
        return {"entities": [], "relationships": []}


async def _fetch_incumbent(conn, memory_id: str) -> dict | None:
    """Pull the currently-stored extraction for a memory from the knowledge graph."""
    row = await conn.fetchrow(
        """
        SELECT json_build_object(
          'entities', (
            SELECT coalesce(jsonb_agg(DISTINCT jsonb_build_object(
              'name', e.canonical_name,
              'type', e.entity_type
            )), '[]'::jsonb)
            FROM entity_memories em
            JOIN entities e ON e.id = em.entity_id
            WHERE em.memory_id = $1
          ),
          'relationships', (
            SELECT coalesce(jsonb_agg(DISTINCT jsonb_build_object(
              'head', h.canonical_name,
              'predicate', er.relationship_type,
              'tail', t.canonical_name
            )), '[]'::jsonb)
            FROM entity_relations er
            JOIN entities h ON h.id = er.source_entity_id
            JOIN entities t ON t.id = er.target_entity_id
            WHERE er.source_memory = $1
          )
        ) AS extraction
        """,
        memory_id,
    )
    if row and row["extraction"]:
        data = row["extraction"]
        return json.loads(data) if isinstance(data, str) else data
    return None


async def run_extraction_eval(
    *,
    candidate_model: str,
    incumbent_model: str,
    sample_size: int = 10,
) -> dict:
    """Run an A/B extraction eval and record the sweep to extraction_eval_runs."""
    pool = await get_pool()
    per_memory: list[dict] = []

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, content FROM memories
            WHERE extraction_status = 'done'
              AND length(content) BETWEEN 200 AND 4000
              AND category != '_archived'
            ORDER BY random()
            LIMIT $1
            """,
            sample_size,
        )

        for row in rows:
            memory_id = str(row["id"])
            content = row["content"]
            incumbent = await _fetch_incumbent(conn, memory_id) or {
                "entities": [], "relationships": [],
            }
            try:
                candidate = await _extract_with(candidate_model, content)
            except Exception:
                logger.exception("candidate extraction failed for %s", memory_id[:8])
                continue

            inc_entities = {_norm(e.get("name", "")) for e in incumbent.get("entities", [])
                            if e.get("name")}
            cand_entities = {_norm(e.get("name", "")) for e in candidate.get("entities", [])
                             if e.get("name")}
            inc_rels = {
                (_norm(r.get("head", "")), r.get("predicate", ""), _norm(r.get("tail", "")))
                for r in incumbent.get("relationships", [])
                if r.get("head") and r.get("tail")
            }
            cand_rels = {
                (_norm(r.get("head", "")), r.get("predicate", ""), _norm(r.get("tail", "")))
                for r in candidate.get("relationships", [])
                if r.get("head") and r.get("tail")
            }
            ent_f1 = _f1(inc_entities, cand_entities)
            rel_f1 = _f1(inc_rels, cand_rels)
            judge = await _judge_equivalence(content, incumbent, candidate, candidate_model)

            per_memory.append({
                "memory_id": memory_id,
                "entity_f1": round(ent_f1, 3),
                "relation_f1": round(rel_f1, 3),
                "judge_score": round(judge, 3),
            })

        # Means across sample
        n = max(1, len(per_memory))
        mean_ent = sum(m["entity_f1"] for m in per_memory) / n
        mean_rel = sum(m["relation_f1"] for m in per_memory) / n
        mean_judge = sum(m["judge_score"] for m in per_memory) / n

        run_id = await conn.fetchval(
            """
            INSERT INTO extraction_eval_runs (
                candidate_model, incumbent_model, sample_size,
                entity_f1, relation_f1, judge_score,
                per_memory, config
            ) VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8::jsonb)
            RETURNING id
            """,
            candidate_model, incumbent_model, len(per_memory),
            mean_ent, mean_rel, mean_judge,
            json.dumps(per_memory),
            json.dumps({"judge": "self-candidate"}),
        )

    logger.info(
        "extraction_eval: candidate=%s ent_f1=%.3f rel_f1=%.3f judge=%.3f (n=%d)",
        candidate_model, mean_ent, mean_rel, mean_judge, len(per_memory),
    )
    return {
        "run_id": str(run_id),
        "ran_at": datetime.now().isoformat(),
        "candidate_model": candidate_model,
        "incumbent_model": incumbent_model,
        "sample_size": len(per_memory),
        "entity_f1": round(mean_ent, 3),
        "relation_f1": round(mean_rel, 3),
        "judge_score": round(mean_judge, 3),
    }


async def list_runs(limit: int = 20) -> list[dict]:
    """Recent extraction eval runs for dashboard display."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, ran_at, candidate_model, incumbent_model, sample_size,
                   entity_f1, relation_f1, judge_score, notes
            FROM extraction_eval_runs
            ORDER BY ran_at DESC
            LIMIT $1
            """,
            limit,
        )
    return [
        {
            "id": str(r["id"]), "ran_at": r["ran_at"].isoformat() if r["ran_at"] else None,
            "candidate_model": r["candidate_model"], "incumbent_model": r["incumbent_model"],
            "sample_size": r["sample_size"],
            "entity_f1": r["entity_f1"], "relation_f1": r["relation_f1"],
            "judge_score": r["judge_score"], "notes": r["notes"],
        }
        for r in rows
    ]
