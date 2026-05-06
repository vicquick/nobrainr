"""LLM-powered scheduler jobs for autonomous knowledge growth."""

import asyncio
import logging
import socket
from datetime import datetime
from uuid import UUID

import numpy as np

from nobrainr.config import settings
from nobrainr.db import queries
from nobrainr.db.pool import get_pool
from nobrainr.embeddings.ollama import embed_text
from nobrainr.extraction.dedup import DEDUP_SCHEMA
from nobrainr.extraction.llm import ollama_chat

logger = logging.getLogger("nobrainr")


async def _yield_to_live_requests():
    """Brief pause between batch LLM calls to let live requests through."""
    await asyncio.sleep(settings.scheduler_inter_request_delay)


def _hostname() -> str:
    return socket.gethostname()

SUMMARIZE_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": "One-sentence summary, max 15 words",
        },
    },
    "required": ["summary"],
}

LESSON_CLASSIFIER_SCHEMA = {
    "type": "object",
    "properties": {
        "is_lesson": {
            "type": "boolean",
            "description": (
                "True if this memory documents a MISTAKE surfaced, a FIX "
                "applied, a CORRECTION of prior understanding, an INCIDENT "
                "and its resolution, or LEARNING from an experience that "
                "went wrong. Session logs, architecture references, business "
                "plans, and neutral research notes are NOT lessons."
            ),
        },
        "confidence": {
            "type": "integer",
            "minimum": 1,
            "maximum": 5,
            "description": "1=very uncertain, 5=highly confident",
        },
        "reason": {
            "type": "string",
            "description": "One short sentence explaining why (max 20 words)",
        },
    },
    "required": ["is_lesson", "confidence", "reason"],
}

SYNTHESIS_SCHEMA = {
    "type": "object",
    "properties": {
        "insight": {
            "type": "string",
            "description": "A higher-level insight synthesized from the memories",
        },
        "confidence": {
            "type": "number",
            "description": "Confidence in the insight (0-1)",
        },
    },
    "required": ["insight", "confidence"],
}

COOCCURRENCE_SCHEMA = {
    "type": "object",
    "properties": {
        "has_relationship": {
            "type": "boolean",
            "description": "Whether a meaningful relationship exists between the two entities",
        },
        "relationship_type": {
            "type": "string",
            "enum": [
                "uses", "depends_on", "fixes", "part_of", "created_by",
                "deployed_on", "configured_with", "replaces", "conflicts_with",
                "runs_on", "implements",
            ],
            "description": "The relationship type if has_relationship is true",
        },
        "direction": {
            "type": "string",
            "enum": ["a_to_b", "b_to_a"],
            "description": "Direction: a_to_b means A->B, b_to_a means B->A",
        },
        "confidence": {
            "type": "number",
            "description": "Confidence in the relationship (0.5-1.0)",
        },
        "reason": {
            "type": "string",
            "description": "Brief explanation of why this relationship exists",
        },
    },
    "required": ["has_relationship"],
}

ENTITY_DESC_SCHEMA = {
    "type": "object",
    "properties": {
        "description": {
            "type": "string",
            "description": "A 2-sentence description of the entity based on its context",
        },
    },
    "required": ["description"],
}

CONTRADICTION_SCHEMA = {
    "type": "object",
    "properties": {
        "contradicts": {
            "type": "boolean",
            "description": "Whether the two memories contradict each other",
        },
        "explanation": {
            "type": "string",
            "description": "Brief explanation of the contradiction or why they don't contradict",
        },
        "resolution": {
            "type": "string",
            "description": "Which memory is likely more accurate, or 'unclear'",
        },
    },
    "required": ["contradicts", "explanation", "resolution"],
}

INSIGHT_SCHEMA = {
    "type": "object",
    "properties": {
        "is_useful": {
            "type": "boolean",
            "description": "Whether a reusable learning can be extracted",
        },
        "learning": {
            "type": "string",
            "description": "The reusable learning or insight (empty if not useful)",
        },
        "category": {
            "type": "string",
            "description": "Category: debugging, architecture, tooling, infrastructure, patterns",
        },
        "tags": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Relevant tags for the learning",
        },
    },
    "required": ["is_useful", "learning", "category", "tags"],
}


async def auto_summarize() -> dict:
    """Generate summaries for memories that lack them."""
    model = settings.scheduler_llm_model
    batch = await queries.get_unsummarized_memories(settings.summarize_batch_size)
    if not batch:
        return {"summarized": 0, "ran_at": datetime.now().isoformat()}

    count = 0
    for mem in batch:
        try:
            result = await ollama_chat(
                system="You are a concise summarizer. Summarize the given text in one sentence, max 15 words.",
                user=mem["content"][:3000],
                schema=SUMMARIZE_SCHEMA,
                model=model,
                timeout=600.0,
                think=False,
            )
            summary = result.get("summary", "").strip()
            if summary:
                await queries.update_memory(
                    mem["id"], summary=summary,
                    _changed_by="scheduler:auto_summarize",
                    _change_type="auto_summarize",
                )
                count += 1
        except Exception:
            logger.exception("auto_summarize failed for memory %s", mem["id"][:8])
        await _yield_to_live_requests()

    return {"summarized": count, "batch_size": len(batch), "ran_at": datetime.now().isoformat()}


async def consolidation() -> dict:
    """Find and merge highly similar memory pairs."""
    model = settings.scheduler_llm_model
    pairs = await queries.get_similar_memory_pairs(
        threshold=0.88, limit=settings.consolidation_batch_size,
    )
    if not pairs:
        return {"merged": 0, "checked": 0, "ran_at": datetime.now().isoformat()}

    merged = 0
    checked = 0
    for pair in pairs:
        try:
            result = await ollama_chat(
                system=(
                    "You are a deduplication assistant. Compare two memories and decide "
                    "if they should be merged. If yes, produce a merged version that "
                    "combines all unique information from both."
                ),
                user=(
                    "Should these two memories be merged?\n\n"
                    f"Memory A:\n{pair['content_a'][:1500]}\n\n"
                    f"Memory B (similarity {pair.get('similarity', 0):.4f}):\n{pair['content_b'][:1500]}"
                ),
                schema=DEDUP_SCHEMA,
                model=model,
                timeout=90.0,
            )

            id_a = str(pair["id_a"])
            id_b = str(pair["id_b"])

            if result.get("should_merge") and result.get("merged_content"):
                # Triggers snapshot old state automatically
                merged_content = result["merged_content"]
                embedding = await embed_text(merged_content)
                await queries.update_memory(
                    id_a, content=merged_content, embedding=embedding,
                    _changed_by="scheduler:consolidation",
                    _change_type="consolidation",
                    _change_reason=result.get("reason", "Merged with " + id_b),
                )
                await queries.update_memory(
                    id_b, category="_archived",
                    _changed_by="scheduler:consolidation",
                    _change_type="consolidation",
                    _change_reason=f"Archived: merged into {id_a}",
                )
                merged += 1
            else:
                await queries.mark_memories_consolidation_checked(id_a, id_b)

            checked += 1
        except Exception:
            logger.exception("consolidation failed for pair %s/%s", str(pair["id_a"])[:8], str(pair["id_b"])[:8])
        await _yield_to_live_requests()

    return {"merged": merged, "checked": checked, "ran_at": datetime.now().isoformat()}


async def synthesis() -> dict:
    """Synthesize higher-level insights from entity-linked memory clusters."""
    model = settings.scheduler_llm_model
    candidates = await queries.get_synthesis_candidates(settings.synthesis_batch_size)
    if not candidates:
        return {"synthesized": 0, "ran_at": datetime.now().isoformat()}

    count = 0
    for cand in candidates:
        try:
            # Limit memory content to avoid blowing context
            contents = cand.get("memory_contents", [])
            truncated = [c[:500] for c in contents[:5]]
            memories_text = "\n---\n".join(truncated)

            result = await ollama_chat(
                system=(
                    "You are a knowledge synthesizer. Given multiple memories about an entity, "
                    "produce a single higher-level insight that captures the key pattern or lesson."
                ),
                user=(
                    f"Entity: {cand['entity_name']} ({cand['entity_type']})\n\n"
                    f"Related memories:\n{memories_text}\n\n"
                    "Synthesize a higher-level insight from these memories."
                ),
                schema=SYNTHESIS_SCHEMA,
                model=model,
                timeout=120.0,
            )

            insight = result.get("insight", "").strip()
            if insight and result.get("confidence", 0) >= 0.4:
                embedding = await embed_text(insight)
                await queries.store_memory(
                    content=insight,
                    embedding=embedding,
                    summary=f"Synthesis: {cand['entity_name']}",
                    source_type="synthesis",
                    source_machine=settings.source_machine or _hostname(),
                    category="insight",
                    tags=["synthesized", cand["entity_type"]],
                    confidence=result.get("confidence", 0.7),
                    metadata={"source_entity": cand["entity_name"]},
                )
                # Log synthesis event for cooldown tracking
                await queries.log_agent_event(
                    event_type="synthesis",
                    description=f"Synthesized insight for {cand['entity_name']}",
                    agent_id="scheduler",
                    category="system",
                    metadata={"entity_id": cand["entity_id"]},
                )
                count += 1
        except Exception:
            logger.exception("synthesis failed for entity %s", cand["entity_name"])
        await _yield_to_live_requests()

    return {"synthesized": count, "candidates": len(candidates), "ran_at": datetime.now().isoformat()}


ENTITY_MERGE_SCHEMA = {
    "type": "object",
    "properties": {
        "should_merge": {
            "type": "boolean",
            "description": "Whether these two entities refer to the same real-world thing",
        },
        "winner_name": {
            "type": "string",
            "description": "The best canonical name to keep (most specific and commonly used)",
        },
        "winner_type": {
            "type": "string",
            "description": "The correct entity type for the merged entity",
            "enum": [
                "person", "project", "technology", "concept", "file", "config",
                "error", "location", "organization", "service", "database",
                "command", "port", "container",
            ],
        },
        "reason": {
            "type": "string",
            "description": "Brief reason for the decision",
        },
    },
    "required": ["should_merge", "winner_name", "winner_type", "reason"],
}


async def entity_merging() -> dict:
    """Find and merge duplicate entities (same name different type, or high embedding similarity)."""
    model = settings.scheduler_llm_model
    pairs = await queries.get_duplicate_entities(limit=settings.entity_merging_batch_size)
    if not pairs:
        return {"merged": 0, "checked": 0, "ran_at": datetime.now().isoformat()}

    merged = 0
    checked = 0
    for pair in pairs:
        try:
            result = await ollama_chat(
                system=(
                    "You are a knowledge graph curator. Determine if two entities refer to "
                    "the same real-world thing and should be merged. Consider: same software, "
                    "same person, same concept just typed differently. If merging, pick the best "
                    "name and most accurate type."
                ),
                user=(
                    f"Entity A: \"{pair['name_a']}\" (type: {pair['type_a']}, "
                    f"linked to {pair['mem_count_a']} memories, {pair['mentions_a']} mentions)\n"
                    f"Entity B: \"{pair['name_b']}\" (type: {pair['type_b']}, "
                    f"linked to {pair['mem_count_b']} memories, {pair['mentions_b']} mentions)\n"
                    f"Embedding similarity: {pair.get('similarity', 0):.3f}\n\n"
                    "Are these the same thing? If so, which name and type to keep?"
                ),
                schema=ENTITY_MERGE_SCHEMA,
                model=model,
                timeout=600.0,
                think=False,
            )

            id_a = str(pair["id_a"])
            id_b = str(pair["id_b"])

            if result.get("should_merge"):
                # Pick winner: the one with more memory links, or the one matching the LLM's preferred name
                a_is_winner = pair["mem_count_a"] >= pair["mem_count_b"]
                winner_id = id_a if a_is_winner else id_b
                loser_id = id_b if a_is_winner else id_a

                await queries.merge_entities(winner_id, loser_id)

                # Update winner's type if LLM suggested a better one
                winner_type = result.get("winner_type")
                winner_name = result.get("winner_name")
                if winner_type or winner_name:
                    pool = await get_pool()
                    async with pool.acquire() as conn:
                        if winner_type:
                            await conn.execute(
                                "UPDATE entities SET entity_type = $1 WHERE id = $2",
                                winner_type, UUID(winner_id),
                            )
                        if winner_name:
                            await conn.execute(
                                "UPDATE entities SET name = $1, canonical_name = $2 WHERE id = $3",
                                winner_name, winner_name.lower().strip(), UUID(winner_id),
                            )

                merged += 1
                logger.info(
                    "Merged entity '%s' (%s) into '%s' (%s)",
                    pair["name_b"] if a_is_winner else pair["name_a"],
                    pair["type_b"] if a_is_winner else pair["type_a"],
                    pair["name_a"] if a_is_winner else pair["name_b"],
                    pair["type_a"] if a_is_winner else pair["type_b"],
                )
            else:
                await queries.mark_entity_merge_checked(id_a, id_b)

            checked += 1
        except Exception:
            logger.exception("entity_merging failed for pair %s/%s", pair["name_a"], pair["name_b"])
        await _yield_to_live_requests()

    return {"merged": merged, "checked": checked, "ran_at": datetime.now().isoformat()}


async def entity_enrichment() -> dict:
    """Generate descriptions for entities that lack them."""
    model = settings.scheduler_llm_model
    entities = await queries.get_underdescribed_entities(settings.entity_enrichment_batch_size)
    if not entities:
        return {"enriched": 0, "ran_at": datetime.now().isoformat()}

    count = 0
    for ent in entities:
        try:
            contents = ent.get("memory_contents", [])
            context_text = "\n---\n".join(c[:300] for c in contents[:5])

            result = await ollama_chat(
                system=(
                    "You are a knowledge graph curator. Write a concise 2-sentence description "
                    "for the given entity based on the context from related memories."
                ),
                user=(
                    f"Entity: {ent['name']} (type: {ent['entity_type']})\n\n"
                    f"Context from related memories:\n{context_text}\n\n"
                    "Write a 2-sentence description."
                ),
                schema=ENTITY_DESC_SCHEMA,
                model=model,
                timeout=600.0,
                think=False,
            )

            desc = result.get("description", "").strip()
            if desc and len(desc) > 10:
                await queries.update_entity_description(ent["id"], desc)
                # Re-embed the entity with its new description
                embedding = await embed_text(f"{ent['name']}: {desc}")
                pool = await get_pool()
                async with pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE entities SET embedding = $1 WHERE id = $2",
                        np.array(embedding, dtype=np.float32),
                        UUID(ent["id"]),
                    )
                count += 1
        except Exception:
            logger.exception("entity_enrichment failed for %s", ent["name"])
        await _yield_to_live_requests()

    return {"enriched": count, "candidates": len(entities), "ran_at": datetime.now().isoformat()}


async def insight_extraction() -> dict:
    """Extract reusable learnings from agent events."""
    model = settings.scheduler_llm_model
    events = await queries.get_unprocessed_events(settings.insight_extraction_batch_size)
    if not events:
        return {"extracted": 0, "processed": 0, "ran_at": datetime.now().isoformat()}

    extracted = 0
    processed = 0
    for event in events:
        try:
            desc = event.get("description", "")
            meta = event.get("metadata", {})
            event_text = f"Event type: {event['event_type']}\n"
            event_text += f"Description: {desc}\n"
            if meta:
                # Include relevant metadata fields
                for key in ("files_edited", "edit_count", "machine", "error", "task"):
                    if key in meta:
                        event_text += f"{key}: {meta[key]}\n"

            result = await ollama_chat(
                system=(
                    "You are a learning extractor. Given an agent activity event, determine if "
                    "there's a reusable learning or insight worth remembering. Be selective — "
                    "only extract genuinely useful patterns, not routine activities."
                ),
                user=(
                    f"Analyze this agent event for reusable learnings:\n\n{event_text}\n\n"
                    "Return is_useful=false if this is routine or not noteworthy."
                ),
                schema=INSIGHT_SCHEMA,
                model=model,
                timeout=600.0,
                think=False,
            )

            if result.get("is_useful") and result.get("learning"):
                learning = result["learning"].strip()
                embedding = await embed_text(learning)
                tags = result.get("tags", [])
                tags.append("auto-extracted")
                await queries.store_memory(
                    content=learning,
                    embedding=embedding,
                    source_type="insight",
                    source_machine=settings.source_machine or _hostname(),
                    category=result.get("category", "learned-pattern"),
                    tags=tags,
                    confidence=0.7,
                    metadata={"source_event_id": event["id"]},
                )
                extracted += 1

            await queries.mark_event_processed(event["id"])
            processed += 1
        except Exception:
            logger.exception("insight_extraction failed for event %s", event["id"][:8])
        await _yield_to_live_requests()

    return {
        "extracted": extracted,
        "processed": processed,
        "ran_at": datetime.now().isoformat(),
    }


async def memory_decay() -> dict:
    """Archive stale, low-value memories that are never accessed."""
    count = await queries.archive_stale_memories(settings.decay_batch_size)
    return {"archived": count, "ran_at": datetime.now().isoformat()}


async def contradiction_detection() -> dict:
    """Find and flag contradicting memories."""
    model = settings.scheduler_llm_model
    candidates = await queries.get_potential_contradictions(
        settings.contradiction_batch_size
    )
    if not candidates:
        return {"contradictions_found": 0, "checked": 0, "ran_at": datetime.now().isoformat()}

    found = 0
    checked = 0
    for pair in candidates:
        try:
            result = await ollama_chat(
                system=(
                    "You are a contradiction detector. Compare two knowledge entries and determine "
                    "if they contain conflicting information. Minor differences in wording are NOT "
                    "contradictions — only flag genuine factual conflicts."
                ),
                user=(
                    f"Memory A (from {pair.get('machine_a', 'unknown')}):\n{pair['content_a'][:500]}\n\n"
                    f"Memory B (from {pair.get('machine_b', 'unknown')}):\n{pair['content_b'][:500]}\n\n"
                    "Do these memories contradict each other?"
                ),
                schema=CONTRADICTION_SCHEMA,
                model=model,
                timeout=600.0,
            )

            if result.get("contradicts"):
                explanation = result.get("explanation", "")
                resolution = result.get("resolution", "unclear")
                embedding = await embed_text(
                    f"Contradiction: {explanation}"
                )
                await queries.store_memory(
                    content=f"Contradiction detected:\n\nMemory A: {pair['content_a'][:200]}\n\nMemory B: {pair['content_b'][:200]}\n\nExplanation: {explanation}\n\nResolution: {resolution}",
                    embedding=embedding,
                    source_type="contradiction",
                    source_machine=settings.source_machine or _hostname(),
                    category="contradiction",
                    # `lesson` tag marks this as a "mistake surfaced + needs fix"
                    # memory, the orthogonal concept to `confidence`. See
                    # nobrainr memory `discipline-*` entries for rationale.
                    tags=["auto-detected", "needs-review", "lesson"],
                    confidence=0.8,
                    metadata={
                        "memory_a": str(pair["id_a"]),
                        "memory_b": str(pair["id_b"]),
                        "resolution": resolution,
                    },
                )
                found += 1

            checked += 1
        except Exception:
            logger.exception("contradiction_detection failed for pair")
        await _yield_to_live_requests()

    return {"contradictions_found": found, "checked": checked, "ran_at": datetime.now().isoformat()}


CROSS_MACHINE_SCHEMA = {
    "type": "object",
    "properties": {
        "has_insight": {
            "type": "boolean",
            "description": "Whether a meaningful cross-machine pattern exists",
        },
        "insight": {
            "type": "string",
            "description": "The cross-machine insight or pattern discovered",
        },
        "machines_involved": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Which machines contributed to this insight",
        },
    },
    "required": ["has_insight", "insight", "machines_involved"],
}

EXTRACTION_QUALITY_SCHEMA = {
    "type": "object",
    "properties": {
        "is_valid": {
            "type": "boolean",
            "description": "Whether the entity was correctly extracted from the memory",
        },
        "correct_type": {
            "type": "string",
            "enum": [
                "person", "project", "technology", "concept", "file", "config",
                "error", "location", "organization", "service", "database",
                "command", "port", "container",
            ],
            "description": "The correct entity type if mistyped, or same if correct",
        },
        "confidence": {
            "type": "number",
            "description": "Confidence that this entity belongs to this memory (0-1)",
        },
    },
    "required": ["is_valid", "correct_type", "confidence"],
}


async def cross_machine_insights() -> dict:
    """Discover patterns that span multiple machines/agents."""
    model = settings.scheduler_llm_model
    clusters = await queries.get_cross_machine_clusters(
        settings.cross_machine_batch_size
    )
    if not clusters:
        return {"insights": 0, "checked": 0, "ran_at": datetime.now().isoformat()}

    count = 0
    for cluster in clusters:
        try:
            machines = cluster.get("machines", [])
            contents = cluster.get("memory_contents", [])
            memories_text = "\n---\n".join(c[:300] for c in contents[:6])

            result = await ollama_chat(
                system=(
                    "You are a cross-system analyst. Given memories about the same entity from "
                    "different machines/agents, identify patterns, discrepancies, or insights that "
                    "only become visible when comparing across sources. Focus on actionable findings."
                ),
                user=(
                    f"Entity: {cluster['entity_name']} ({cluster['entity_type']})\n"
                    f"Seen on machines: {', '.join(str(m) for m in machines)}\n\n"
                    f"Memories:\n{memories_text}\n\n"
                    "What cross-machine patterns or insights emerge?"
                ),
                schema=CROSS_MACHINE_SCHEMA,
                model=model,
                timeout=120.0,
            )

            if result.get("has_insight") and result.get("insight"):
                insight = result["insight"].strip()
                embedding = await embed_text(insight)
                await queries.store_memory(
                    content=insight,
                    embedding=embedding,
                    summary=f"Cross-machine: {cluster['entity_name']}",
                    source_type="cross_machine_insight",
                    source_machine=settings.source_machine or _hostname(),
                    category="insight",
                    tags=["cross-machine", cluster["entity_type"]] + [str(m) for m in machines],
                    confidence=0.75,
                    metadata={
                        "entity_id": str(cluster["entity_id"]),
                        "machines": [str(m) for m in machines],
                    },
                )
                count += 1
        except Exception:
            logger.exception("cross_machine_insights failed for %s", cluster["entity_name"])
        await _yield_to_live_requests()

    return {"insights": count, "checked": len(clusters), "ran_at": datetime.now().isoformat()}


async def extraction_quality() -> dict:
    """Validate extraction quality by sampling recent entities."""
    model = settings.scheduler_llm_model
    samples = await queries.get_extraction_samples(
        settings.quality_batch_size
    )
    if not samples:
        return {"validated": 0, "invalid": 0, "ran_at": datetime.now().isoformat()}

    validated = 0
    invalid = 0
    for sample in samples:
        try:
            result = await ollama_chat(
                system=(
                    "You are an extraction quality validator. Given a memory and an entity "
                    "extracted from it, verify if the extraction is correct. Check if the entity "
                    "name, type, and association are accurate."
                ),
                user=(
                    f"Memory content:\n{sample['memory_content'][:3000]}\n\n"
                    f"Extracted entity: {sample['entity_name']} (type: {sample['entity_type']})\n\n"
                    "Is this entity correctly extracted from this memory?"
                ),
                schema=EXTRACTION_QUALITY_SCHEMA,
                model=model,
                timeout=600.0,
                think=False,
            )

            confidence = result.get("confidence", 0.5)
            await queries.update_entity_confidence(
                str(sample["entity_id"]),
                str(sample["memory_id"]),
                confidence,
            )

            if not result.get("is_valid"):
                invalid += 1
                # If confidence is very low, remove the entity-memory link
                if confidence < 0.2:
                    pool = await get_pool()
                    async with pool.acquire() as conn:
                        await conn.execute(
                            "DELETE FROM entity_memories WHERE entity_id = $1 AND memory_id = $2",
                            UUID(str(sample["entity_id"])),
                            UUID(str(sample["memory_id"])),
                        )

            # Log validation
            await queries.log_agent_event(
                event_type="extraction_validated",
                description=f"Validated entity '{sample['entity_name']}' ({sample['entity_type']}): valid={result.get('is_valid')}, confidence={confidence:.2f}",
                agent_id="scheduler",
                category="system",
                metadata={
                    "entity_id": str(sample["entity_id"]),
                    "memory_id": str(sample["memory_id"]),
                    "is_valid": result.get("is_valid"),
                    "confidence": confidence,
                },
            )
            validated += 1
        except Exception:
            logger.exception("extraction_quality failed for entity %s", sample["entity_name"])
        await _yield_to_live_requests()

    return {"validated": validated, "invalid": invalid, "ran_at": datetime.now().isoformat()}


FACT_EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "facts": {
            "type": "array",
            "items": {"type": "string"},
            "description": "3-5 atomic, self-contained facts extracted from the text",
        },
    },
    "required": ["facts"],
}

FACT_SYSTEM_PROMPT = """\
You are a precise fact extractor. Given a text, extract 3-5 atomic facts.

Each fact MUST be:
- Self-contained (understandable without the original text)
- Specific (include names, versions, exact details)
- Actionable (useful for someone looking up this information)
- Outcome-focused (what happened or what IS, not what was planned or intended)
- Temporally anchored when dates are available (e.g. "As of March 2026, X uses Y")

Do NOT extract:
- Vague statements ("things went well", "it was discussed")
- Opinions or subjective assessments
- Plans or intentions that may not have been executed ("we should...", "planning to...")
- Facts that require the original context to understand
- Duplicate facts with slight rewording

If the text is too short or trivial for meaningful facts, return an empty array.
"""


async def fact_extraction() -> dict:
    """Extract atomic facts from memories that don't have facts yet (Mem0-style)."""
    from nobrainr.db.pool import get_pool
    from nobrainr.extraction.llm import ollama_chat
    from nobrainr.embeddings.ollama import embed_text

    pool = await get_pool()
    batch_size = settings.fact_extraction_batch_size

    # Find memories without facts
    async with pool.acquire() as conn:
        # Auto-create table if not exists. Canonical schema lives in
        # db/schema.py — keep this DDL in sync (embedding_model added
        # 2026-04-09 so the embedding-safeguard works across re-embeds).
        await conn.execute(f"""
            CREATE TABLE IF NOT EXISTS memory_facts (
                id UUID PRIMARY KEY DEFAULT uuidv7(),
                memory_id UUID NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
                content TEXT NOT NULL,
                embedding vector(1024),
                embedding_model text DEFAULT '{settings.embedding_model}',
                quality_score REAL,
                created_at TIMESTAMPTZ DEFAULT now()
            )
        """)
        await conn.execute(
            f"""ALTER TABLE memory_facts
                ADD COLUMN IF NOT EXISTS embedding_model text
                DEFAULT '{settings.embedding_model}'"""
        )
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_facts_memory ON memory_facts(memory_id)")

        rows = await conn.fetch("""
            SELECT m.id, m.content, m.summary
            FROM memories m
            WHERE m.tier < 3
              AND NOT EXISTS (SELECT 1 FROM memory_facts f WHERE f.memory_id = m.id)
            ORDER BY m.importance DESC NULLS LAST, m.created_at DESC
            LIMIT $1
        """, batch_size)

    if not rows:
        return {"extracted": 0, "facts_created": 0, "ran_at": datetime.now().isoformat()}

    extracted = 0
    facts_created = 0

    for row in rows:
        content = row["content"]
        if len(content) < 50:  # too short for meaningful facts
            continue

        try:
            result = await ollama_chat(
                system=FACT_SYSTEM_PROMPT,
                user=content[:4000],
                schema=FACT_EXTRACTION_SCHEMA,
                num_ctx=8192,
                timeout=600.0,
                think=False,
            )
            facts = result.get("facts", [])
            if not facts:
                # Store empty marker so we don't re-process
                async with pool.acquire() as conn:
                    await conn.execute(
                        "INSERT INTO memory_facts (memory_id, content) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                        row["id"], "(no facts extracted)",
                    )
                extracted += 1
                continue

            async with pool.acquire() as conn:
                for fact_text in facts[:7]:  # max 7 facts per memory
                    if len(fact_text) < 10:
                        continue
                    try:
                        embedding = await embed_text(fact_text)
                        await conn.execute(
                            """INSERT INTO memory_facts (memory_id, content, embedding)
                               VALUES ($1, $2, $3) ON CONFLICT DO NOTHING""",
                            row["id"], fact_text, embedding,
                        )
                        facts_created += 1
                    except Exception:
                        logger.debug("Failed to store fact for memory %s", row["id"])

            extracted += 1
            await _yield_to_live_requests()

        except Exception:
            logger.debug("Fact extraction failed for memory %s", row["id"], exc_info=True)

    logger.info("Fact extraction: %d memories processed, %d facts created", extracted, facts_created)
    return {"extracted": extracted, "facts_created": facts_created, "ran_at": datetime.now().isoformat()}


async def entity_pruning() -> dict:
    """Prune low-value noise entities (<=1 memory link, older than 72h)."""
    result = await queries.prune_noise_entities(min_age_hours=72)  # 3 days for cross-session knowledge
    return {
        "entities_pruned": result["entities_pruned"],
        "orphan_relations_removed": result["orphan_relations_removed"],
        "ran_at": datetime.now().isoformat(),
    }


async def chatgpt_distill() -> dict:
    """Distill raw ChatGPT conversations into memory learnings."""
    from nobrainr.importers.chatgpt import distill_conversations

    result = await distill_conversations(
        batch_size=settings.chatgpt_distill_batch_size,
        llm_model=settings.chatgpt_distill_model,
        concurrency=settings.chatgpt_distill_concurrency,
    )
    return {
        "distilled": result["distilled"],
        "processed": result["processed"],
        "skipped": result["skipped"],
        "ran_at": datetime.now().isoformat(),
    }


async def knowledge_crawl() -> dict:
    """Crawl documentation URLs and store as memories."""
    from nobrainr.crawler.knowledge import knowledge_crawl as _crawl
    return await _crawl()


async def freshness_recrawl() -> dict:
    """Re-crawl stale documentation and update changed content."""
    if not settings.freshness_enabled:
        return {"skipped": True, "reason": "disabled", "ran_at": datetime.now().isoformat()}
    from nobrainr.crawler.knowledge import freshness_recrawl as _recrawl
    return await _recrawl()


# ──────────────────────────────────────────────
# Phase 3: Entity web research
# ──────────────────────────────────────────────

RESEARCH_QUERY_SCHEMA = {
    "type": "object",
    "properties": {
        "should_research": {
            "type": "boolean",
            "description": "Whether this entity would benefit from web research",
        },
        "search_url": {
            "type": "string",
            "description": "A single authoritative documentation URL to crawl (official docs preferred). Must be a full https:// URL.",
        },
        "reason": {
            "type": "string",
            "description": "Brief reason for the research recommendation",
        },
    },
    "required": ["should_research", "search_url", "reason"],
}


async def entity_web_research() -> dict:
    """Research underdescribed entities by crawling authoritative web sources.

    Finds important entities (5+ mentions) that lack good descriptions or
    web-sourced knowledge, asks the LLM to suggest a documentation URL,
    then crawls and stores it.
    """
    if not settings.entity_research_enabled:
        return {"skipped": True, "reason": "disabled", "ran_at": datetime.now().isoformat()}

    model = settings.scheduler_llm_model
    candidates = await queries.get_research_candidates(
        min_mentions=settings.entity_research_min_mentions,
        cooldown_days=settings.entity_research_cooldown_days,
        limit=settings.entity_research_batch_size,
    )
    if not candidates:
        return {"researched": 0, "stored": 0, "ran_at": datetime.now().isoformat()}

    from nobrainr.crawler.knowledge import _crawl_url, _is_already_crawled
    from nobrainr.services.memory import store_document_chunked

    researched = 0
    stored = 0

    for entity in candidates:
        try:
            # Build context from existing memories
            contents = entity.get("memory_contents", [])
            context = "\n".join(c[:200] for c in contents[:5]) if contents else "No existing context"

            # Ask LLM to suggest a documentation URL
            result = await ollama_chat(
                system=(
                    "You are a research assistant. Given an entity from a knowledge graph, "
                    "determine if it would benefit from web research and suggest a single "
                    "authoritative documentation URL to crawl. Prefer official documentation "
                    "sites (docs.*, github.com, MDN, etc). Only suggest URLs you're confident "
                    "exist and are publicly accessible. Return should_research=false for generic "
                    "concepts that don't have specific documentation pages."
                ),
                user=(
                    f"Entity: {entity['name']} (type: {entity['entity_type']})\n"
                    f"Current description: {entity.get('description', 'none')}\n"
                    f"Mentions: {entity['mention_count']}\n\n"
                    f"Context from related memories:\n{context}\n\n"
                    "Should we research this entity? If yes, suggest the best documentation URL."
                ),
                schema=RESEARCH_QUERY_SCHEMA,
                model=model,
                timeout=600.0,
                think=False,
            )

            researched += 1

            if not result.get("should_research"):
                await queries.log_agent_event(
                    event_type="web_research",
                    description=f"Skipped web research for {entity['name']}: {result.get('reason', '')}",
                    agent_id="scheduler",
                    category="system",
                    metadata={"entity_id": entity["id"], "skipped": True},
                )
                await _yield_to_live_requests()
                continue

            url = result.get("search_url", "").strip()
            if not url or not url.startswith("http"):
                await _yield_to_live_requests()
                continue

            # Skip if already crawled
            if await _is_already_crawled(url):
                await queries.log_agent_event(
                    event_type="web_research",
                    description=f"URL already crawled for {entity['name']}: {url}",
                    agent_id="scheduler",
                    category="system",
                    metadata={"entity_id": entity["id"], "url": url, "already_crawled": True},
                )
                await _yield_to_live_requests()
                continue

            # Crawl the URL with BM25 query-aware filtering + async job API
            crawl_result = await _crawl_url(
                url,
                use_async_job=True,
                query=f"{entity['name']} {entity['entity_type']} documentation",
            )
            if not crawl_result:
                await _yield_to_live_requests()
                continue

            markdown = crawl_result["markdown"][:50000]
            if len(markdown.strip()) < 100:
                await _yield_to_live_requests()
                continue

            # Store via chunked ingestion (handles long pages properly)
            tags = ["crawled", "entity-research", entity["entity_type"], entity["canonical_name"]]
            store_result = await store_document_chunked(
                content=markdown,
                title=crawl_result.get("title", url),
                summary=f"Research: {entity['name']} — {crawl_result['title']}"[:200],
                source_type="crawl",
                source_machine=settings.source_machine or "unknown",
                source_ref=url,
                tags=tags,
                category="documentation",
                confidence=0.8,
                metadata={"researched_entity": entity["name"], "entity_id": entity["id"]},
            )

            if store_result.get("status") in ("stored", "updated"):
                stored += store_result.get("chunks", 1)
                logger.info(
                    "Entity research stored: %s → %s (%s, %d chunks)",
                    entity["name"], url, crawl_result["title"], store_result.get("chunks", 1),
                )

            # Log the research event (for cooldown tracking)
            await queries.log_agent_event(
                event_type="web_research",
                description=f"Researched {entity['name']}: {url}",
                agent_id="scheduler",
                category="system",
                metadata={"entity_id": entity["id"], "url": url, "title": crawl_result["title"]},
            )

        except Exception:
            logger.exception("entity_web_research failed for %s", entity["name"])

        await _yield_to_live_requests()

    return {
        "researched": researched,
        "stored": stored,
        "ran_at": datetime.now().isoformat(),
    }


# ──────────────────────────────────────────────
# Phase 5: Interest-based expansion
# ──────────────────────────────────────────────

INTEREST_RESEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "should_research": {
            "type": "boolean",
            "description": "Whether this topic warrants web research",
        },
        "search_url": {
            "type": "string",
            "description": "A documentation URL to crawl for this topic (full https:// URL)",
        },
        "refined_topic": {
            "type": "string",
            "description": "A more specific version of the topic for storage tags",
        },
    },
    "required": ["should_research", "search_url", "refined_topic"],
}


async def interest_expansion() -> dict:
    """Research hot topics based on accumulated interest signals.

    Looks at what agents have been searching for and working on,
    identifies knowledge gaps, and proactively crawls relevant documentation.
    """
    if not settings.interest_tracking_enabled:
        return {"skipped": True, "reason": "disabled", "ran_at": datetime.now().isoformat()}

    model = settings.scheduler_llm_model
    hot_topics = await queries.get_hot_topics(
        decay_days=settings.interest_signal_decay_days,
        limit=settings.interest_expansion_batch_size * 2,  # fetch more, filter later
    )
    if not hot_topics:
        return {"researched": 0, "stored": 0, "ran_at": datetime.now().isoformat()}

    from nobrainr.crawler.knowledge import _crawl_url, _is_already_crawled
    from nobrainr.services.memory import store_document_chunked

    researched = 0
    stored = 0

    for topic_data in hot_topics[:settings.interest_expansion_batch_size]:
        topic = topic_data["topic"]
        score = topic_data["score"]

        try:
            # Check if recently researched
            status = await queries.get_topic_research_status(topic)
            if status:
                continue

            # Ask LLM to suggest a research URL
            result = await ollama_chat(
                system=(
                    "You are a research assistant. Given a topic that AI agents have been "
                    "frequently searching for, suggest the best authoritative URL to crawl "
                    "for up-to-date documentation or knowledge. Only suggest URLs you're "
                    "confident exist. Return should_research=false for vague or overly broad topics."
                ),
                user=(
                    f"Hot topic: \"{topic}\" (interest score: {score:.2f}, "
                    f"signals: {topic_data['signal_count']})\n\n"
                    "Should we research this? If yes, suggest the best documentation URL."
                ),
                schema=INTEREST_RESEARCH_SCHEMA,
                model=model,
                timeout=600.0,
                think=False,
            )

            if not result.get("should_research"):
                await _yield_to_live_requests()
                continue

            url = result.get("search_url", "").strip()
            if not url or not url.startswith("http"):
                await _yield_to_live_requests()
                continue

            if await _is_already_crawled(url):
                await _yield_to_live_requests()
                continue

            # Crawl with BM25 query-aware filtering + async job API
            crawl_result = await _crawl_url(
                url,
                use_async_job=True,
                query=topic,
            )
            if not crawl_result:
                await _yield_to_live_requests()
                continue

            markdown = crawl_result["markdown"][:50000]
            if len(markdown.strip()) < 100:
                await _yield_to_live_requests()
                continue

            refined = result.get("refined_topic", topic)
            tags = ["crawled", "interest-research", refined.lower().replace(" ", "-")]
            store_result = await store_document_chunked(
                content=markdown,
                title=crawl_result.get("title", url),
                summary=f"Interest research: {refined} — {crawl_result['title']}"[:200],
                source_type="crawl",
                source_machine=settings.source_machine or "unknown",
                source_ref=url,
                tags=tags,
                category="documentation",
                confidence=0.75,
                metadata={"interest_topic": topic, "interest_score": score},
            )

            if store_result.get("status") in ("stored", "updated"):
                stored += store_result.get("chunks", 1)
                logger.info("Interest research stored: %s → %s (%d chunks)", topic, url, store_result.get("chunks", 1))

            # Log for cooldown
            await queries.log_agent_event(
                event_type="interest_research",
                description=f"Researched interest topic: {topic} → {url}",
                agent_id="scheduler",
                category="system",
                metadata={"topic": topic, "url": url, "score": score},
            )

            researched += 1

        except Exception:
            logger.exception("interest_expansion failed for topic %s", topic)

        await _yield_to_live_requests()

    return {
        "researched": researched,
        "stored": stored,
        "ran_at": datetime.now().isoformat(),
    }


MEMORY_QUALITY_SCHEMA = {
    "type": "object",
    "properties": {
        "specificity": {
            "type": "integer",
            "description": "1-5: Does this contain concrete details (commands, paths, versions, error messages)?",
        },
        "actionability": {
            "type": "integer",
            "description": "1-5: Can an AI agent use this to make a decision or take an action?",
        },
        "self_containment": {
            "type": "integer",
            "description": "1-5: Is this understandable without the original conversation context?",
        },
    },
    "required": ["specificity", "actionability", "self_containment"],
}


PERSONAL_SOURCES = {"manual", "affine_memos", "docx", "sticky_notes", "keep"}


async def quality_scoring() -> dict:
    """LLM-assess quality of unscored memories on dimensions appropriate to source.

    Two rubrics:
      - Technical (chatgpt/github/crawl/session/agent_learning/synthesis):
        specificity / actionability / self_containment — what an AI coding
        agent needs.
      - Personal (manual/affine_memos/docx/sticky_notes/keep): clarity /
        completeness / connection_density — what makes a personal note
        valuable for reflection. The technical rubric used to score these
        as 1s because it asked for commands and file paths — wrong frame
        for a journaling note.

    Both rubrics return three 1-5 scores stored in the same columns; the
    averaged 0-1 score now means the right thing per source.
    """
    model = settings.scheduler_llm_model
    batch = await queries.get_unscored_memories(settings.quality_scoring_batch_size)
    if not batch:
        return {"scored": 0, "ran_at": datetime.now().isoformat()}

    scored = 0
    for mem in batch:
        try:
            content = mem.get("summary") or mem["content"][:800]
            source = mem.get("source_type", "unknown")
            category = mem.get("category", "uncategorized")
            is_personal = source in PERSONAL_SOURCES

            if is_personal:
                system = (
                    "You assess personal notes / journal entries / hand-written memos. "
                    "Rate three dimensions 1-5. The output schema names are "
                    "specificity / actionability / self_containment but for personal "
                    "notes treat them as:\n"
                    "- specificity → CLARITY: 1=fragmentary unintelligible, "
                    "5=clear and well-formed thought\n"
                    "- actionability → COMPLETENESS: 1=cut off mid-thought / single "
                    "keyword, 5=fully developed idea\n"
                    "- self_containment → CONNECTION_DENSITY: 1=isolated thought with "
                    "no anchor, 5=rich in named people / projects / dates / ideas\n"
                    "Personal notes are valuable EVEN IF they aren't technical. A short "
                    "personal reflection like 'realized I work better in mornings' is "
                    "4-5 on clarity, 5 on completeness if that's the whole thought. Only "
                    "score 1-2 when content is genuinely fragmentary or unclear (e.g. "
                    "'todo: ?', 'see also'). Do NOT penalize for non-technical content."
                )
            else:
                system = (
                    "You assess the quality of knowledge base entries for AI coding agents. "
                    "Rate each dimension 1-5:\n"
                    "- specificity: 1=vague/generic ('Python is useful'), 5=concrete details "
                    "(commands, file paths, error messages, version numbers)\n"
                    "- actionability: 1=trivia/opinion/personal, 5=an agent can directly use "
                    "this to solve a problem or make a technical decision\n"
                    "- self_containment: 1=needs original conversation context to understand, "
                    "5=fully self-contained and clear\n"
                    "Be strict. Generic programming tips are 1-2. Specific bug fixes with "
                    "root cause are 4-5."
                )

            result = await ollama_chat(
                system=system,
                user=f"Source: {source} | Category: {category}\n\n{content}",
                schema=MEMORY_QUALITY_SCHEMA,
                model=model,
                timeout=600.0,
                think=False,
            )

            spec = max(1, min(5, result.get("specificity", 3)))
            act = max(1, min(5, result.get("actionability", 3)))
            self_c = max(1, min(5, result.get("self_containment", 3)))
            quality = (spec + act + self_c) / 15.0

            await queries.update_quality_score(
                mem["id"],
                quality_score=quality,
                specificity=spec,
                actionability=act,
                self_containment=self_c,
            )
            scored += 1
        except Exception:
            logger.exception("quality_scoring failed for memory %s", mem["id"][:8])
        await _yield_to_live_requests()

    return {
        "scored": scored,
        "batch_size": len(batch),
        "ran_at": datetime.now().isoformat(),
    }


KEY_EXPANSION_SCHEMA = {
    "type": "object",
    "properties": {
        "keyphrases": {
            "type": "array",
            "items": {"type": "string"},
            "description": "3-5 alternative search keyphrases for this memory",
        }
    },
    "required": ["keyphrases"],
}


async def key_expansion() -> dict:
    """Generate alternative search keyphrases for memories without search_keys.

    LongMemEval fact-augmented key expansion: for each memory, generate 3-5
    alternative phrasings that someone might search to find it. Stored in
    search_keys and included in the FTS GIN index so memories are findable
    from more query angles — directly addresses the ~30% recall miss rate
    from single-query retrieval.
    """
    model = settings.scheduler_llm_model
    pool = await get_pool()

    async with pool.acquire() as conn:
        batch = await conn.fetch(
            """
            SELECT id, content, summary, category, source_type
            FROM memories
            WHERE search_keys IS NULL
              AND tier < 3
              AND LENGTH(content) >= 80
            ORDER BY importance DESC, created_at DESC
            LIMIT $1
            """,
            settings.key_expansion_batch_size,
        )

    if not batch:
        return {"expanded": 0, "ran_at": datetime.now().isoformat()}

    expanded = 0
    for row in batch:
        try:
            text = row["summary"] or row["content"][:600]
            category = row["category"] or "general"
            source = row["source_type"] or "unknown"

            result = await ollama_chat(
                system=(
                    "You generate alternative search keyphrases for a knowledge base. "
                    "Given a memory, produce 3-5 short phrases (4-12 words each) that "
                    "someone might type to find this memory. Think about:\n"
                    "- The problem or symptom described\n"
                    "- The tools, files, or commands involved\n"
                    "- The solution or decision made\n"
                    "- Common synonyms for technical terms used\n"
                    "Phrases should be terse and search-like, not full sentences."
                ),
                user=f"Category: {category} | Source: {source}\n\n{text}",
                schema=KEY_EXPANSION_SCHEMA,
                model=model,
                timeout=60.0,
                think=False,
            )

            phrases = result.get("keyphrases", [])
            # Filter: keep only non-empty, reasonably short phrases
            clean = [p.strip() for p in phrases if isinstance(p, str) and 3 <= len(p.strip()) <= 120]
            if not clean:
                # Store empty string so we don't re-process
                clean_str = ""
            else:
                clean_str = " | ".join(clean[:5])

            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE memories SET search_keys = $1 WHERE id = $2",
                    clean_str, row["id"],
                )
            if clean:
                expanded += 1
        except Exception:
            logger.exception("key_expansion failed for memory %s", str(row["id"])[:8])
        await _yield_to_live_requests()

    return {
        "expanded": expanded,
        "batch_size": len(batch),
        "ran_at": datetime.now().isoformat(),
    }


# ---------------------------------------------------------------------------
# Non-LLM monitoring jobs (thin wrappers — logic lives in monitoring.py)
# ---------------------------------------------------------------------------


async def monitor_health() -> dict:
    """Check Docker container health and system resources, store anomalies."""
    from nobrainr.monitoring import monitor_health

    return await monitor_health()


async def send_email_digest() -> dict:
    """Send daily email digest of monitoring anomalies."""
    from nobrainr.monitoring import send_email_digest

    return await send_email_digest()


async def send_knowledge_digest() -> dict:
    """Send daily knowledge digest — insights, memory of the day, progress, bridges."""
    from nobrainr.monitoring import send_knowledge_digest

    return await send_knowledge_digest()


async def reranker_eval() -> dict:
    """Run offline reranker eval against historical feedback — regression gate.

    Replays known (query, useful_memory_id) pairs through search + rerank
    and measures recall@1/3/10 + MRR. Persists run to extraction_eval_runs
    so we can spot regressions when changing reranker model or candidates.
    """
    import json
    from nobrainr.db.pool import get_pool
    from nobrainr.db import queries
    from nobrainr.embeddings.ollama import embed_text

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT memory_id, query_text, result_rank, created_at
            FROM memory_outcomes
            WHERE was_useful = true
              AND query_text IS NOT NULL
              AND length(query_text) > 3
              AND (context IS NULL OR context NOT LIKE 'auto:%')
            ORDER BY created_at DESC
            LIMIT 200
            """
        )
    if not rows:
        return {"status": "no_data", "ran_at": datetime.now().isoformat()}

    rank_hits = {1: 0, 3: 0, 10: 0}
    not_found = 0
    rr_sum = 0.0
    evaluated = 0
    for r in rows:
        target = str(r["memory_id"])
        q = r["query_text"]
        try:
            emb = await embed_text(q)
            results = await queries.search_memories(
                embedding=emb, limit=20, threshold=0.2, text_query=q,
            )
            if settings.reranker_enabled and results:
                from nobrainr.services.reranker import rerank
                results = await rerank(q, results, limit=20)
        except Exception:
            continue
        rank = None
        for i, m in enumerate(results, start=1):
            if str(m.get("id")) == target:
                rank = i
                break
        if rank is None:
            not_found += 1
        else:
            rr_sum += 1.0 / rank
            for k in rank_hits:
                if rank <= k:
                    rank_hits[k] += 1
        evaluated += 1
        await _yield_to_live_requests()

    metrics = {
        "queries": evaluated,
        "recall@1": rank_hits[1] / evaluated if evaluated else 0,
        "recall@3": rank_hits[3] / evaluated if evaluated else 0,
        "recall@10": rank_hits[10] / evaluated if evaluated else 0,
        "mrr": rr_sum / evaluated if evaluated else 0,
        "not_found_count": not_found,
        "reranker_model": settings.reranker_model,
        "reranker_max_candidates": settings.reranker_max_candidates,
        "reranker_device": settings.reranker_device,
        "ran_at": datetime.now().isoformat(),
    }
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS extraction_eval_runs (
                    id        uuid DEFAULT gen_random_uuid() PRIMARY KEY,
                    run_at    timestamptz DEFAULT now(),
                    kind      text NOT NULL,
                    metrics   jsonb NOT NULL
                );
                INSERT INTO extraction_eval_runs (run_at, kind, metrics)
                VALUES (now(), 'reranker_offline', $1::jsonb);
                """,
                json.dumps(metrics),
            )
    except Exception:
        logger.exception("Failed to persist reranker_eval run")
    return metrics


# ---------------------------------------------------------------------------
# System pulse — autonomous health transmissions (inspired by OpusDelta)
# ---------------------------------------------------------------------------

async def system_pulse() -> dict:
    """Generate a daily system health transmission.

    Collects memory system metrics (counts, growth, entity health, search quality)
    and stores a structured health report as a memory. Agents can discover these
    to understand system state without manual checks.
    """
    pool = await get_pool()

    # Gather stats
    stats = {}
    try:
        async with pool.acquire() as conn:
            stats["total_memories"] = await conn.fetchval(
                "SELECT count(*) FROM memories WHERE category != '_archived'"
            )
            stats["total_entities"] = await conn.fetchval("SELECT count(*) FROM entities")
            stats["total_relations"] = await conn.fetchval("SELECT count(*) FROM entity_relations")
            stats["archived_memories"] = await conn.fetchval(
                "SELECT count(*) FROM memories WHERE category = '_archived'"
            )

            # Growth in last 24h
            stats["new_memories_24h"] = await conn.fetchval(
                "SELECT count(*) FROM memories WHERE created_at > now() - interval '24 hours'"
            )
            stats["new_entities_24h"] = await conn.fetchval(
                "SELECT count(*) FROM entities WHERE created_at > now() - interval '24 hours'"
            )

            # Category distribution
            rows = await conn.fetch(
                "SELECT category, count(*) as cnt FROM memories "
                "WHERE category != '_archived' GROUP BY category ORDER BY cnt DESC LIMIT 10"
            )
            stats["top_categories"] = {r["category"]: r["cnt"] for r in rows}

            # Source machine distribution
            rows = await conn.fetch(
                "SELECT source_machine, count(*) as cnt FROM memories "
                "WHERE source_machine IS NOT NULL GROUP BY source_machine ORDER BY cnt DESC"
            )
            stats["machines"] = {r["source_machine"]: r["cnt"] for r in rows}

            # Search feedback quality
            feedback_row = await conn.fetchrow(
                "SELECT count(*) as total, "
                "count(*) FILTER (WHERE was_useful = true) as helpful, "
                "count(*) FILTER (WHERE was_useful = false) as unhelpful "
                "FROM memory_outcomes WHERE created_at > now() - interval '7 days'"
            )
            if feedback_row:
                total = feedback_row["total"]
                stats["feedback_7d"] = {
                    "total": total,
                    "helpful": feedback_row["helpful"],
                    "unhelpful": feedback_row["unhelpful"],
                    "hit_rate": round(feedback_row["helpful"] / max(1, total), 2),
                }

            # Entity graph density
            stats["avg_relations_per_entity"] = float(await conn.fetchval(
                "SELECT coalesce(avg(cnt), 0) FROM ("
                "  SELECT count(*) as cnt FROM entity_relations "
                "  GROUP BY source_entity_id"
                ") sub"
            ) or 0)

            # Embedding model distribution
            rows = await conn.fetch(
                "SELECT embedding_model, count(*) as cnt FROM memories "
                "GROUP BY embedding_model ORDER BY cnt DESC"
            )
            stats["embedding_models"] = {r["embedding_model"] or "unknown": r["cnt"] for r in rows}

    except Exception:
        logger.exception("system_pulse stats collection failed")
        return {"error": "stats collection failed", "ran_at": datetime.now().isoformat()}

    # Build human-readable report
    report_parts = [
        f"System Pulse — {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}",
        f"Memories: {stats['total_memories']} active, {stats['archived_memories']} archived",
        f"Knowledge Graph: {stats['total_entities']} entities, {stats['total_relations']} relations",
        f"Growth (24h): +{stats['new_memories_24h']} memories, +{stats['new_entities_24h']} entities",
        f"Graph density: {stats['avg_relations_per_entity']:.1f} relations/entity",
    ]
    if stats.get("feedback_7d"):
        fb = stats["feedback_7d"]
        report_parts.append(
            f"Search quality (7d): {fb['hit_rate']:.0%} hit rate ({fb['helpful']}/{fb['total']} helpful)"
        )
    if stats.get("top_categories"):
        cats = ", ".join(f"{k}={v}" for k, v in list(stats["top_categories"].items())[:5])
        report_parts.append(f"Top categories: {cats}")
    if stats.get("machines"):
        machines = ", ".join(f"{k}={v}" for k, v in stats["machines"].items())
        report_parts.append(f"Machines: {machines}")

    report = "\n".join(report_parts)

    # Store as a memory
    from nobrainr.services.memory import store_memory_with_extraction

    await store_memory_with_extraction(
        content=report,
        summary=f"System pulse {datetime.now().strftime('%Y-%m-%d')}",
        tags=["system", "pulse", "health", "metrics"],
        category="infrastructure",
        source_type="system",
        source_machine=settings.source_machine or _hostname(),
        confidence=1.0,
        metadata=stats,
        skip_dedup=True,
    )

    return {
        "status": "transmitted",
        "total_memories": stats["total_memories"],
        "total_entities": stats["total_entities"],
        "new_24h": stats["new_memories_24h"],
        "ran_at": datetime.now().isoformat(),
    }


# ---------------------------------------------------------------------------
# Auto-optimize — autonomous self-improvement loop (inspired by autoresearch)
# ---------------------------------------------------------------------------

AUTO_OPTIMIZE_SCHEMA = {
    "type": "object",
    "properties": {
        "analysis": {
            "type": "string",
            "description": "Brief analysis of current search quality based on feedback signals",
        },
        "suggestion": {
            "type": "string",
            "description": "One concrete suggestion to improve search/retrieval quality",
        },
        "experiment_type": {
            "type": "string",
            "enum": ["threshold", "weights", "tags", "none"],
            "description": "Type of experiment to try, or 'none' if current config is good",
        },
    },
    "required": ["analysis", "suggestion", "experiment_type"],
}


async def auto_optimize() -> dict:
    """Analyze search quality feedback and suggest improvements.

    Inspired by karpathy/autoresearch — this is the experiment loop for search
    quality. Reads feedback signals, analyzes patterns, and stores improvement
    insights for the system to act on.
    """
    pool = await get_pool()

    # Gather recent feedback data
    try:
        async with pool.acquire() as conn:
            # Get recent feedback with context
            feedback_rows = await conn.fetch(
                "SELECT mo.memory_id, mo.was_useful, mo.context, mo.created_at, "
                "m.summary "
                "FROM memory_outcomes mo "
                "LEFT JOIN memories m ON m.id = mo.memory_id "
                "WHERE mo.created_at > now() - interval '7 days' "
                "ORDER BY mo.created_at DESC LIMIT 50"
            )

            total = len(feedback_rows)
            helpful = sum(1 for r in feedback_rows if r["was_useful"])
            unhelpful = total - helpful

            if total < 3:
                return {
                    "status": "insufficient_data",
                    "feedback_count": total,
                    "ran_at": datetime.now().isoformat(),
                }

            # Build context for LLM analysis
            feedback_summary = []
            for r in feedback_rows[:20]:
                status = "helpful" if r["was_useful"] else "NOT helpful"
                summary = r["summary"] or r["memory_id"][:8]
                context = f" — {r['context']}" if r["context"] else ""
                feedback_summary.append(f"  Memory: '{summary}' → {status}{context}")

            feedback_text = "\n".join(feedback_summary)

    except Exception:
        logger.exception("auto_optimize feedback collection failed")
        return {"error": "feedback collection failed", "ran_at": datetime.now().isoformat()}

    # Ask LLM to analyze patterns
    try:
        result = await ollama_chat(
            system=(
                "You are a search quality analyst. Analyze memory search feedback to "
                "identify patterns in what works and what doesn't. Focus on actionable "
                "improvements to search relevance, not cosmetic changes."
            ),
            user=(
                f"Search feedback summary (last 7 days):\n"
                f"Total: {total}, Helpful: {helpful} ({helpful/max(1,total):.0%}), "
                f"Unhelpful: {unhelpful}\n\n"
                f"Recent queries and results:\n{feedback_text}\n\n"
                f"Analyze the patterns and suggest one concrete improvement."
            ),
            schema=AUTO_OPTIMIZE_SCHEMA,
            model=settings.scheduler_llm_model,
            timeout=600.0,
            think=False,
        )

        analysis = result.get("analysis", "")
        suggestion = result.get("suggestion", "")
        exp_type = result.get("experiment_type", "none")

        # Store the insight as a memory
        if analysis and suggestion:
            from nobrainr.services.memory import store_memory_with_extraction

            await store_memory_with_extraction(
                content=f"Search Quality Analysis: {analysis}\n\nSuggestion: {suggestion}",
                summary=f"Auto-optimize: {suggestion[:80]}",
                tags=["system", "optimization", "search-quality", "auto-optimize"],
                category="insight",
                source_type="system",
                source_machine=settings.source_machine or _hostname(),
                confidence=0.7,
                metadata={
                    "feedback_total": total,
                    "feedback_helpful": helpful,
                    "hit_rate": round(helpful / max(1, total), 2),
                    "experiment_type": exp_type,
                },
                skip_dedup=False,  # Allow dedup to merge with previous insights
            )

        return {
            "status": "analyzed",
            "feedback_count": total,
            "hit_rate": round(helpful / max(1, total), 2),
            "experiment_type": exp_type,
            "suggestion": suggestion[:200],
            "ran_at": datetime.now().isoformat(),
        }

    except Exception:
        logger.exception("auto_optimize LLM analysis failed")
        return {"error": "analysis failed", "ran_at": datetime.now().isoformat()}


# ──────────────────────────────────────────────
# Community detection (GraphRAG)
# ──────────────────────────────────────────────

async def community_detection() -> dict:
    """Detect entity communities using Louvain and generate summaries."""
    from nobrainr.services.communities import detect_communities, generate_community_summaries

    result = await detect_communities(min_community_size=3, resolution=1.0)
    if result["communities"] > 0:
        summary_result = await generate_community_summaries(max_communities=50)
        result["summaries"] = summary_result

    # Invalidate graph layout cache — communities changed, layout needs recomputation
    import os
    _cache = "/tmp/nobrainr_graph_cache.json"
    if os.path.exists(_cache):
        os.remove(_cache)
        logger.info("Graph cache invalidated after community detection")

    result["ran_at"] = datetime.now().isoformat()
    return result


# ──────────────────────────────────────────────
# Co-occurrence relationship inference
# ──────────────────────────────────────────────

async def cooccurrence_linking() -> dict:
    """Find entity pairs co-occurring in 3+ memories without edges, use LLM to classify."""
    model = settings.scheduler_llm_model
    pairs = await queries.get_unlinked_cooccurrences(min_shared=3, limit=30)
    if not pairs:
        return {"status": "no_pairs", "ran_at": datetime.now().isoformat()}

    created = 0
    skipped = 0
    errors = 0

    for pair in pairs:
        await _yield_to_live_requests()

        # Build context from sample memories
        context_snippets = "\n---\n".join(
            snippet for snippet in pair["sample_contents"] if snippet
        )
        if not context_snippets:
            skipped += 1
            continue

        prompt = (
            f"Entity A: {pair['entity_a_name']} (type: {pair['entity_a_type']})\n"
            f"Entity B: {pair['entity_b_name']} (type: {pair['entity_b_type']})\n"
            f"These two entities co-occur in {pair['shared_count']} memories.\n\n"
            f"Sample memories where both appear:\n{context_snippets}\n\n"
            f"Based on the evidence above, determine if a meaningful, specific "
            f"relationship exists between Entity A and Entity B. "
            f"Only say yes if the evidence clearly supports it."
        )

        try:
            result = await ollama_chat(
                system=(
                    "You classify relationships between entities in a knowledge graph. "
                    "Given two entities and sample memories where both appear, determine if "
                    "a specific relationship exists. Be precise — only confirm relationships "
                    "that the evidence clearly supports. Choose the most specific relationship "
                    "type that fits."
                ),
                user=prompt,
                schema=COOCCURRENCE_SCHEMA,
                model=model,
                think=False,
            )

            if result.get("has_relationship") and result.get("relationship_type"):
                rel_type = result["relationship_type"]
                confidence = min(max(result.get("confidence", 0.7), 0.5), 1.0)
                direction = result.get("direction", "a_to_b")

                if direction == "b_to_a":
                    source_id = pair["entity_b_id"]
                    target_id = pair["entity_a_id"]
                else:
                    source_id = pair["entity_a_id"]
                    target_id = pair["entity_b_id"]

                await queries.store_entity_relation(
                    source_id,
                    target_id,
                    rel_type,
                    confidence=confidence,
                    properties={
                        "source": "cooccurrence_inference",
                        "shared_memories": pair["shared_count"],
                        "reason": result.get("reason", ""),
                    },
                )
                created += 1
                logger.info(
                    "Co-occurrence link: %s -[%s]-> %s (confidence=%.2f, shared=%d)",
                    pair["entity_a_name"] if direction == "a_to_b" else pair["entity_b_name"],
                    rel_type,
                    pair["entity_b_name"] if direction == "a_to_b" else pair["entity_a_name"],
                    confidence,
                    pair["shared_count"],
                )
            else:
                skipped += 1

        except Exception:
            logger.exception(
                "Co-occurrence classification failed for %s / %s",
                pair["entity_a_name"], pair["entity_b_name"],
            )
            errors += 1

    return {
        "status": "completed",
        "pairs_evaluated": len(pairs),
        "relationships_created": created,
        "skipped": skipped,
        "errors": errors,
        "ran_at": datetime.now().isoformat(),
    }


# ──────────────────────────────────────────────
# Hub dampening (entity specificity scoring)
# ──────────────────────────────────────────────


async def hub_dampening() -> dict:
    """Compute IDF-like specificity scores for all entities.

    Entities appearing in many memories (Python, Docker) get low specificity.
    Niche entities (pgRouting, IfcOpenShell) get high specificity.
    Used by co-occurrence linking to skip hub-hub pairs and by graph search
    to weight results by specificity.
    """
    result = await queries.compute_entity_specificity()
    result["ran_at"] = datetime.now().isoformat()
    return result


# ──────────────────────────────────────────────
# Cross-community bridge detection
# ──────────────────────────────────────────────


async def bridge_detection() -> dict:
    """Find entities that bridge multiple communities in the knowledge graph.

    Bridge entities are the most valuable nodes — they connect different topic
    clusters and enable cross-domain knowledge discovery. Requires community_detection
    to have run at least once.
    """
    bridges = await queries.find_bridge_entities(min_communities=2, limit=200)
    return {
        "bridges_found": len(bridges),
        "top_bridges": [
            {"name": b["name"], "type": b["entity_type"], "communities": b["communities_bridged"]}
            for b in bridges[:10]
        ],
        "ran_at": datetime.now().isoformat(),
    }


# ──────────────────────────────────────────────
# Lesson classifier — tier-2 LLM pass to catch subtle lessons
# that the tier-1 SQL backfill (keyword + category + commit-prefix)
# missed. Idempotent: only processes memories without the `lesson`
# tag. Conservative: only applies the tag when is_lesson=True AND
# confidence>=4. See nobrainr memories tagged `lesson` for examples.
# ──────────────────────────────────────────────

async def lesson_classifier() -> dict:
    """Classify untagged memories for the `lesson` tag via qwen.

    `lesson` is the orthogonal axis to `confidence` — marks memories
    documenting a mistake surfaced / fix applied / correction / incident.
    Tier-1 (SQL backfill 2026-04-11) already tagged 8196 memories with
    exact markers. This tier-2 pass picks up subtle cases in categories
    that CAN contain lessons but don't use obvious keywords.
    """
    from nobrainr.db.pool import get_pool

    model = settings.scheduler_llm_model
    batch_size = settings.lesson_classifier_batch_size

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, content, summary, category, tags, source_type
            FROM memories
            WHERE NOT ('lesson' = ANY(tags))
              AND category IN (
                  'patterns','architecture','tooling','agent_learning','insight'
              )
              AND tier < 3
              AND (extraction_status = 'done' OR extraction_status IS NULL)
            ORDER BY importance DESC NULLS LAST, created_at DESC
            LIMIT $1
            """,
            batch_size,
        )

    if not rows:
        return {
            "status": "idle",
            "classified": 0,
            "tagged": 0,
            "ran_at": datetime.now().isoformat(),
        }

    classified = 0
    tagged = 0
    for row in rows:
        memory_id = str(row["id"])
        try:
            content_preview = (row["content"] or "")[:1500]
            category = row["category"] or "unknown"
            existing_tags = list(row["tags"] or [])[:10]

            result = await ollama_chat(
                system=(
                    "You classify memories in an engineering knowledge base. "
                    "A 'lesson' documents a mistake surfaced, a fix applied, "
                    "a correction of prior understanding, an incident and its "
                    "resolution, or a learning from an experience that went "
                    "wrong. Session logs, neutral architecture references, "
                    "business plans, research notes, and pure documentation "
                    "are NOT lessons. Be strict — false positives are worse "
                    "than false negatives. Only return is_lesson=true when "
                    "the memory clearly fits one of the categories above."
                ),
                user=(
                    f"Category: {category}\n"
                    f"Existing tags: {', '.join(existing_tags) or '(none)'}\n\n"
                    f"Content:\n{content_preview}\n\n"
                    "Is this a lesson?"
                ),
                schema=LESSON_CLASSIFIER_SCHEMA,
                model=model,
                timeout=600.0,
                think=False,
            )
            classified += 1

            if result.get("is_lesson") and int(result.get("confidence", 0)) >= 4:
                new_tags = existing_tags + ["lesson"]
                await queries.update_memory(
                    memory_id,
                    tags=new_tags,
                    _changed_by="scheduler:lesson_classifier",
                    _change_type="lesson_tag_tier2",
                    _change_reason=result.get("reason", "")[:200],
                )
                tagged += 1
        except Exception:
            logger.exception(
                "lesson_classifier failed for memory %s", memory_id[:8]
            )
        await _yield_to_live_requests()

    return {
        "classified": classified,
        "tagged": tagged,
        "batch_size": batch_size,
        "ran_at": datetime.now().isoformat(),
    }


# ──────────────────────────────────────────────
# GitHub incremental sync
# ──────────────────────────────────────────────

async def github_sync() -> dict:
    """Incrementally sync new commits, PRs, and issues from GitHub.

    Relies on source_ref dedup in the importer — only new items are stored.
    """
    if not settings.github_owner:
        return {"skipped": True, "reason": "NOBRAINR_GITHUB_OWNER not configured"}
    try:
        from nobrainr.importers.github import import_github

        result = await import_github(
            owner=settings.github_owner,
            source_machine=settings.source_machine or _hostname(),
            include_commits=True,
            include_issues=True,
            include_code_structure=False,  # skip on incremental — only on full import
            include_source_code=False,     # skip on incremental — only on full import
            include_closed_issues=False,   # skip closed on incremental
            concurrency=2,
        )
        result["ran_at"] = datetime.now().isoformat()
        return result
    except Exception:
        logger.exception("GitHub sync failed")
        return {"error": "sync failed", "ran_at": datetime.now().isoformat()}


# ──────────────────────────────────────────────
# Contextual-prefix backfill (Anthropic Contextual Retrieval)
# ──────────────────────────────────────────────

async def contextual_prefix_backfill() -> dict:
    """Backfill contextual prefixes + re-embed chunks that predate the feature.

    Anthropic's Contextual Retrieval research shows a 35-49% failure reduction
    when each chunk's embedding is prefixed with a short "where does this chunk
    live in the document" sentence. ``services/memory.py`` generates these
    prefixes for new chunked writes but a backlog of ~1200 chunks exists from
    before the feature was enabled. This job works through that backlog in
    batches and auto-stops when nothing is left.

    Runs every 2h with batch_size=25. Safe to re-run; idempotent on prefixes
    (checks metadata ? 'contextual_prefix').
    """
    from nobrainr.db.pool import get_pool
    from nobrainr.embeddings.ollama import embed_text
    from nobrainr.services.memory import _generate_chunk_context

    pool = await get_pool()
    batch_size = 25

    async with pool.acquire() as conn:
        # Chunks missing a contextual prefix
        candidates = await conn.fetch(
            """
            SELECT id, content, tags, category, metadata, source_ref
            FROM memories
            WHERE metadata ? 'chunk_index'
              AND metadata ? 'document_id'
              AND NOT (metadata ? 'contextual_prefix')
              AND tier < 3
            ORDER BY created_at DESC
            LIMIT $1
            """,
            batch_size,
        )

    if not candidates:
        return {"status": "idle", "processed": 0, "ran_at": datetime.now().isoformat()}

    import json as _json

    processed = 0
    failed = 0
    for row in candidates:
        memory_id = row["id"]
        content = row["content"]
        # asyncpg returns JSONB as a string; the rest of the codebase uses
        # json.loads(d["metadata"]) in _row_to_dict. Do the same here.
        raw_meta = row["metadata"]
        if isinstance(raw_meta, str):
            try:
                meta = _json.loads(raw_meta) or {}
            except Exception:
                meta = {}
        elif isinstance(raw_meta, dict):
            meta = dict(raw_meta)
        else:
            meta = {}
        doc_title = meta.get("document_title") or row["source_ref"] or "Document"

        try:
            # Build a document summary from the first chunk of the same document
            async with pool.acquire() as conn:
                first_chunk = await conn.fetchval(
                    """
                    SELECT content
                    FROM memories
                    WHERE metadata->>'document_id' = $1
                    ORDER BY (metadata->>'chunk_index')::int ASC
                    LIMIT 1
                    """,
                    meta["document_id"],
                )
            doc_summary = f"{doc_title}. {(first_chunk or content)[:500]}"

            prefix = await _generate_chunk_context(doc_summary, content)
            if not prefix:
                failed += 1
                continue

            # Re-embed in context-enriched form (mirrors store_memory_with_extraction)
            tags = list(row["tags"] or [])
            category = row["category"]
            embed_parts = [prefix]
            if category:
                embed_parts.append(category)
            if tags:
                embed_parts.append(", ".join(tags))
            embed_input = ". ".join(embed_parts) + ". " + content
            new_embedding = await embed_text(embed_input)

            meta["contextual_prefix"] = prefix
            # Contextual BM25 (2026-04-19): also push the prefix into
            # the dedicated fts_context column so the FTS GIN index
            # sees it — the embedding was already enriched above.
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE memories SET fts_context = $1 WHERE id = $2::uuid",
                    prefix, str(memory_id),
                )
            await queries.update_memory(
                str(memory_id),
                embedding=new_embedding,
                metadata=meta,
                _changed_by="scheduler:contextual_prefix_backfill",
                _change_type="contextual_prefix_backfill",
                _change_reason="Backfilled Anthropic-style contextual prefix and re-embedded",
            )
            processed += 1
        except Exception:
            logger.exception("Contextual prefix backfill failed for memory %s", memory_id)
            failed += 1

        await _yield_to_live_requests()

    return {
        "status": "processed",
        "processed": processed,
        "failed": failed,
        "batch_size": batch_size,
        "ran_at": datetime.now().isoformat(),
    }


async def conversation_embedding_backfill() -> dict:
    """Backfill embeddings on conversations_raw for two-layer commonplace.

    Embeds the title + first 4000 chars of message content per conversation.
    That window is what a user typically remembers ("the thread where I
    asked about X"). Full-conversation re-embed isn't worth the cost — the
    distilled memories already cover deep retrieval.

    Runs every 1h with batch=10. CPU embed is ~7s each, so 10 takes ~70s.
    With 2362 total conversations, full backfill takes ~4h.
    """
    from nobrainr.db.pool import get_pool
    from nobrainr.embeddings.ollama import embed_text

    pool = await get_pool()
    batch_size = 10

    async with pool.acquire() as conn:
        candidates = await conn.fetch(
            """
            SELECT id, title, messages
            FROM conversations_raw
            WHERE embedding IS NULL
            ORDER BY imported_at DESC
            LIMIT $1
            """,
            batch_size,
        )

        if not candidates:
            return {"status": "idle", "embedded": 0, "ran_at": datetime.now().isoformat()}

    embedded = 0
    failed = 0
    for row in candidates:
        try:
            title = row["title"] or ""
            msgs = row["messages"] or []
            # Concat first 4000 chars of all message content
            text_parts = [title] if title else []
            running = 0
            for m in msgs:
                if running >= 4000:
                    break
                content = (m.get("content") if isinstance(m, dict) else None) or ""
                if isinstance(content, list):
                    content = " ".join(str(p) for p in content if isinstance(p, (str, int, float)))
                content = str(content)[:4000 - running]
                if content:
                    text_parts.append(content)
                    running += len(content)
            text = "\n".join(text_parts).strip()
            if not text:
                # Mark as embedded with empty vector to skip on next pass
                async with pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE conversations_raw SET embedded_at = now() WHERE id = $1",
                        row["id"],
                    )
                continue

            emb = await embed_text(text[:8000])
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE conversations_raw
                    SET embedding = $1::vector, embedded_at = now(), title_text = $2
                    WHERE id = $3
                    """,
                    emb, title[:500], row["id"],
                )
            embedded += 1
        except Exception:
            logger.exception("conversation_embedding_backfill failed for %s", row["id"])
            failed += 1
        await _yield_to_live_requests()

    return {
        "status": "processed",
        "embedded": embedded,
        "failed": failed,
        "batch_size": batch_size,
        "ran_at": datetime.now().isoformat(),
    }


# ──────────────────────────────────────────────
# Observational Memory · Reflector job (Mastra-style)
# ──────────────────────────────────────────────

OBSERVATION_MERGE_SCHEMA = {
    "type": "object",
    "properties": {
        "merged_observation": {
            "type": "string",
            "description": "Single dense paraphrase combining the unique facts from the inputs",
        },
        "should_merge": {
            "type": "boolean",
            "description": "Whether the inputs are near-duplicates worth merging",
        },
    },
    "required": ["should_merge", "merged_observation"],
}


async def observation_consolidate() -> dict:
    """Reflector: consolidate near-duplicate observations per thread.

    For each thread with 5+ fresh observations, asks the LLM to merge the
    last batch into a single denser observation, marks the originals as
    superseded. Keeps the observation log compact and prefix-cacheable.
    See docs/proposals/MASTRA_OM_PROPOSAL.md.
    """
    model = settings.scheduler_llm_model
    threads = await queries.get_threads_with_fresh_observations(min_count=5)
    if not threads:
        return {"status": "idle", "ran_at": datetime.now().isoformat()}

    consolidated_threads = 0
    superseded_total = 0
    for t in threads:
        thread_id = t["thread_id"]
        try:
            log = await queries.fetch_observation_log(thread_id, limit=20)
            if len(log) < 5:
                continue
            joined = "\n".join(f"- {o['body']}" for o in log[:10])
            result = await ollama_chat(
                system=(
                    "You are a knowledge consolidator. Given a list of "
                    "observations from a single conversation thread, write a "
                    "single dense paraphrase (≤120 tokens) that captures all "
                    "unique facts, preferences, and goals. Drop redundant or "
                    "trivial observations. Do not invent details."
                ),
                user=f"Observations to consolidate:\n{joined}",
                schema=OBSERVATION_MERGE_SCHEMA,
                model=model,
                timeout=180.0,
                think=False,
            )
            if result.get("should_merge") and result.get("merged_observation"):
                merged = result["merged_observation"]
                emb = await embed_text(merged)
                parent_id = await queries.store_observation(
                    thread_id, merged, embedding=emb,
                    metadata={"reflector": True, "merged_from": len(log[:10])},
                )
                n = await queries.supersede_observations(
                    parent_id, [o["id"] for o in log[:10]],
                )
                superseded_total += n
                consolidated_threads += 1
            await _yield_to_live_requests()
        except Exception:
            logger.exception("observation_consolidate failed for thread %s", thread_id[:8])

    return {
        "consolidated_threads": consolidated_threads,
        "superseded": superseded_total,
        "candidates": len(threads),
        "ran_at": datetime.now().isoformat(),
    }
