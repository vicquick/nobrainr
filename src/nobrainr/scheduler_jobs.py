"""LLM-powered scheduler jobs for autonomous knowledge growth."""

import asyncio
import logging
import re
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


async def chatgpt_quality_archive() -> dict:
    """Archive low-importance ChatGPT memories based on conversation age.

    Runs daily. Only touches memories older than 30 days so fresh imports
    have time to accumulate retrieval reinforcement before being judged.
    """
    count = await queries.archive_chatgpt_low_quality(limit=500)
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


# Packed variant — one LLM call scores several memories. The dominant cost
# per call is llama-swap queue wait (~90s under extraction contention), not
# generation, so packing 8 memories per call is a ~7x throughput win.
PACKED_QUALITY_SCHEMA = {
    "type": "object",
    "properties": {
        "scores": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "idx": {
                        "type": "integer",
                        "description": "The [n] index of the entry being scored",
                    },
                    "specificity": {"type": "integer"},
                    "actionability": {"type": "integer"},
                    "self_containment": {"type": "integer"},
                },
                "required": ["idx", "specificity", "actionability", "self_containment"],
            },
        }
    },
    "required": ["scores"],
}

QUALITY_PACK_SIZE = 8


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

    PERSONAL_SYSTEM = (
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
        "Personal notes are valuable EVEN IF they aren't technical. Only "
        "score 1-2 when content is genuinely fragmentary or unclear. Do NOT "
        "penalize for non-technical content.\n"
        "You will receive several entries labeled [1], [2], … — return one "
        "scores object per entry with its idx."
    )
    TECHNICAL_SYSTEM = (
        "You assess the quality of knowledge base entries for AI coding agents. "
        "Rate each dimension 1-5:\n"
        "- specificity: 1=vague/generic ('Python is useful'), 5=concrete details "
        "(commands, file paths, error messages, version numbers)\n"
        "- actionability: 1=trivia/opinion/personal, 5=an agent can directly use "
        "this to solve a problem or make a technical decision\n"
        "- self_containment: 1=needs original conversation context to understand, "
        "5=fully self-contained and clear\n"
        "Be strict. Generic programming tips are 1-2. Specific bug fixes with "
        "root cause are 4-5.\n"
        "You will receive several entries labeled [1], [2], … — return one "
        "scores object per entry with its idx."
    )

    # Group by rubric so one system prompt fits the whole pack
    personal = [m for m in batch if m.get("source_type") in PERSONAL_SOURCES]
    technical = [m for m in batch if m.get("source_type") not in PERSONAL_SOURCES]

    scored = 0
    for mems, system in ((technical, TECHNICAL_SYSTEM), (personal, PERSONAL_SYSTEM)):
        for i in range(0, len(mems), QUALITY_PACK_SIZE):
            pack = mems[i : i + QUALITY_PACK_SIZE]
            try:
                lines = []
                for n, mem in enumerate(pack, start=1):
                    content = (mem.get("summary") or mem["content"])[:500]
                    lines.append(
                        f"[{n}] Source: {mem.get('source_type', 'unknown')} | "
                        f"Category: {mem.get('category', 'uncategorized')}\n{content}"
                    )
                result = await ollama_chat(
                    system=system,
                    user="\n\n---\n\n".join(lines),
                    schema=PACKED_QUALITY_SCHEMA,
                    model=model,
                    timeout=600.0,
                    think=False,
                )

                by_idx = {}
                for s in result.get("scores", []):
                    idx = s.get("idx")
                    if isinstance(idx, int) and 1 <= idx <= len(pack):
                        by_idx[idx] = s

                for n, mem in enumerate(pack, start=1):
                    s = by_idx.get(n)
                    if not s:
                        continue  # stays unscored — retried next run
                    spec = max(1, min(5, s.get("specificity", 3)))
                    act = max(1, min(5, s.get("actionability", 3)))
                    self_c = max(1, min(5, s.get("self_containment", 3)))
                    await queries.update_quality_score(
                        mem["id"],
                        quality_score=(spec + act + self_c) / 15.0,
                        specificity=spec,
                        actionability=act,
                        self_containment=self_c,
                    )
                    scored += 1
            except Exception:
                logger.exception(
                    "quality_scoring pack failed (%d memories)", len(pack)
                )
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

    result = await detect_communities(min_community_size=3, resolution=1.5)
    if result["communities"] > 0:
        summary_result = await generate_community_summaries(max_communities=500)
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


# ──────────────────────────────────────────────
# Community assignment — incremental (2026-07-05)
# ──────────────────────────────────────────────
async def community_assign() -> dict:
    """Assign new entities to communities via label propagation (SQL-only)."""
    from nobrainr.services.communities import assign_new_entities_incremental

    result = await assign_new_entities_incremental()
    result["ran_at"] = datetime.now().isoformat()
    return result


# ──────────────────────────────────────────────
# Memory observability (2026-07-05)
# ──────────────────────────────────────────────
# The 2026 survey (arxiv 2603.07670 §7) imports database-engineering
# practice into agent memory: analyze written-but-never-read records and
# consistently-empty queries to find write-path waste and retrieval
# blind spots. This job computes both and prunes old search traces.
async def memory_observability() -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        never_read = await conn.fetch("""
            SELECT source_type, count(*) AS n
            FROM memories
            WHERE tier < 3 AND access_count = 0
              AND inserted_at < now() - interval '14 days'
            GROUP BY source_type ORDER BY n DESC LIMIT 10
        """)
        empty_queries = await conn.fetch("""
            SELECT query, count(*) AS n
            FROM search_traces
            WHERE result_count = 0
              AND created_at > now() - interval '7 days'
            GROUP BY query ORDER BY n DESC LIMIT 15
        """)
        thin = await conn.fetchrow("""
            SELECT count(*) FILTER (WHERE result_count = 0) AS empty,
                   count(*) FILTER (WHERE quality_tier NOT IN ('A','B')) AS degraded,
                   count(*) AS total,
                   percentile_cont(0.95) WITHIN GROUP (ORDER BY elapsed_ms) AS p95_ms
            FROM search_traces
            WHERE created_at > now() - interval '7 days'
        """)
        pruned = await conn.fetchval(
            """
            WITH del AS (
                DELETE FROM search_traces
                WHERE created_at < now() - make_interval(days => $1)
                RETURNING 1
            ) SELECT count(*) FROM del
            """,
            settings.search_trace_retention_days,
        )
        # ASI06 posture (C2, 2026-07-14): memory-poisoning early warning.
        # sanitized_injections = crawled pages caught trying to program a
        # future agent; low_trust_served = fraction of recent retrievals
        # whose top hit was below the trust floor (rising = corpus being
        # diluted by untrusted content). Both should stay near zero.
        asi06 = await conn.fetchrow("""
            SELECT
              (SELECT count(*) FROM memories
               WHERE tags @> ARRAY['sanitized-injection']
                 AND inserted_at > now() - interval '7 days') AS sanitized_injections_7d,
              (SELECT count(*) FROM memories
               WHERE tags @> ARRAY['sanitized-injection']) AS sanitized_injections_total,
              (SELECT round(avg((top_score < 0.5)::int)::numeric, 3)
               FROM search_traces
               WHERE created_at > now() - interval '7 days'
                 AND top_score IS NOT NULL) AS low_trust_top_rate_7d
        """)
    # HEART metrics pulse (M4, 2026-07-24): the one row that answers
    # "is the knowledge base getting more correct?" — card accuracy,
    # staleness flow (gate + sweeper vs inflow), search latency, the
    # abstention rate from the latest eval run, and the feedback split
    # (post-H2: sparse-true explicit signals only).
    async with pool.acquire() as conn:
        heart = await conn.fetchrow("""
            SELECT
              (SELECT round(avg(published_accuracy)::numeric, 3)
               FROM context_cards WHERE published_accuracy IS NOT NULL) AS card_accuracy_avg,
              (SELECT count(*) FROM context_cards
               WHERE published_accuracy < 0.7) AS cards_below_bar,
              (SELECT count(*) FROM memories
               WHERE superseded_by IS NOT NULL
                 AND updated_at > now() - interval '7 days') AS superseded_7d,
              (SELECT count(*) FROM memories
               WHERE created_at > now() - interval '7 days') AS new_7d,
              (SELECT count(*) FROM memories
               WHERE superseded_by IS NOT NULL
                 AND metadata->>'superseded_reason' LIKE 'write-time contradiction gate%'
                 AND updated_at > now() - interval '7 days') AS gate_supersedes_7d,
              (SELECT round(percentile_cont(0.95) WITHIN GROUP (ORDER BY elapsed_ms)::numeric)
               FROM search_traces
               WHERE created_at > now() - interval '7 days') AS search_p95_ms_7d,
              (SELECT config->>'abstention_rate' FROM eval_runs
               ORDER BY ran_at DESC LIMIT 1) AS latest_abstention_rate,
              (SELECT count(*) FILTER (WHERE was_useful)
               FROM memory_outcomes
               WHERE created_at > now() - interval '7 days'
                 AND context NOT LIKE 'auto:%') AS explicit_useful_7d
        """)
    return {
        "never_read_by_source": {r["source_type"]: r["n"] for r in never_read},
        "empty_queries_7d": [dict(r) for r in empty_queries],
        "search_7d": dict(thin) if thin else {},
        "traces_pruned": int(pruned or 0),
        "asi06": dict(asi06) if asi06 else {},
        "heart_metrics": dict(heart) if heart else {},
        "ran_at": datetime.now().isoformat(),
    }


# ──────────────────────────────────────────────
# Procedural memory distillation (2026-07-05)
# ──────────────────────────────────────────────
# Memp (arxiv 2508.06433) + MS Foundry (Build 2026) pattern: distill
# lesson-like memories into structured procedures capturing "when to use"
# (task context, preconditions, signals) and "what to do" (ordered steps,
# required checks, tool usage). Procedures built by a strong model
# transfer their gains to weaker models — the strategic point of the
# whole exercise. Sources are flagged via metadata.procedural_reviewed
# so each memory is considered exactly once.
PROCEDURAL_SCHEMA = {
    "type": "object",
    "properties": {
        "procedures": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "when_to_use": {"type": "string"},
                    "steps": {"type": "array", "items": {"type": "string"}},
                    "checks": {"type": "array", "items": {"type": "string"}},
                    "source_index": {"type": "integer"},
                    "confidence": {"type": "number"},
                },
                "required": ["title", "when_to_use", "steps", "source_index", "confidence"],
            },
        }
    },
    "required": ["procedures"],
}

_PROCEDURAL_SYSTEM = (
    "You review an engineer's memory notes and extract repeatable PROCEDURES "
    "— only when a note describes a multi-step way of doing something that "
    "will recur (deploys, recoveries, migrations, debugging recipes, config "
    "rituals). Most notes contain NO procedure; return them in no entry. "
    "Never invent steps not grounded in the note. Do not duplicate any "
    "EXISTING PROCEDURE title. steps are imperative commands/actions in "
    "execution order; checks are verifications that confirm success. "
    "confidence: 0.9 = note is explicit step-by-step, 0.5 = steps inferred."
)


async def procedural_distill() -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, content, summary, category, tags
            FROM memories
            WHERE tier < 3
              AND quality_score >= 0.5
              AND category IN ('debugging','infrastructure','ops','tooling',
                               'deployment','architecture','pattern')
              AND (metadata->>'procedural_reviewed') IS NULL
              AND length(content) BETWEEN 200 AND 6000
            ORDER BY inserted_at DESC
            LIMIT $1
            """,
            settings.procedural_distill_batch_size,
        )
        existing_titles = [
            r["title"] for r in await conn.fetch(
                "SELECT title FROM procedural_memories WHERE active AND title IS NOT NULL"
            )
        ]
    if not rows:
        return {"reviewed": 0, "created": 0, "ran_at": datetime.now().isoformat()}

    created = 0
    reviewed_ids: list = []
    pack_size = 5
    for start in range(0, len(rows), pack_size):
        pack = rows[start:start + pack_size]
        notes = "\n\n".join(
            f"[{i}] ({r['category']}) {(r['summary'] or '')[:200]}\n{r['content'][:1500]}"
            for i, r in enumerate(pack)
        )
        user = (
            f"EXISTING PROCEDURES (do not duplicate):\n"
            f"{chr(10).join('- ' + t for t in existing_titles[-80:]) or '(none)'}\n\n"
            f"MEMORY NOTES:\n{notes}\n\n"
            "Extract procedures. source_index = the [N] of the note each "
            "procedure came from."
        )
        try:
            await _yield_to_live_requests()
            resp = await ollama_chat(
                system=_PROCEDURAL_SYSTEM, user=user,
                schema=PROCEDURAL_SCHEMA, temperature=0.2,
            )
        except Exception:
            logger.exception("procedural_distill LLM call failed")
            continue
        reviewed_ids.extend(r["id"] for r in pack)
        for proc in (resp or {}).get("procedures", []):
            if created >= settings.procedural_distill_max_new:
                break
            title = (proc.get("title") or "").strip()[:200]
            steps = [s.strip() for s in proc.get("steps", []) if s.strip()]
            if not title or len(steps) < 2 or float(proc.get("confidence", 0)) < 0.5:
                continue
            if any(title.lower() == t.lower() for t in existing_titles):
                continue
            idx = int(proc.get("source_index", 0))
            src_id = str(pack[idx]["id"]) if 0 <= idx < len(pack) else None
            checks = [c.strip() for c in proc.get("checks", []) if c.strip()]
            content = (
                f"WHEN TO USE: {proc.get('when_to_use','').strip()}\n\n"
                "STEPS:\n" + "\n".join(f"{n}. {s}" for n, s in enumerate(steps, 1))
                + ("\n\nCHECKS:\n" + "\n".join(f"- {c}" for c in checks) if checks else "")
            )
            try:
                await queries.store_procedural_memory(
                    content, title=title, scope="global", priority=40,
                    tags=["auto-distilled", "procedure"],
                    metadata={
                        "source_memory_id": src_id,
                        "distill_confidence": proc.get("confidence"),
                        "distilled_by": "procedural_distill",
                    },
                )
                existing_titles.append(title)
                created += 1
            except Exception:
                logger.exception("procedural store failed for %r", title)

    if reviewed_ids:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE memories
                SET metadata = COALESCE(metadata,'{}'::jsonb)
                    || '{"procedural_reviewed": true}'::jsonb
                WHERE id = ANY($1::uuid[])
                """,
                reviewed_ids,
            )
    return {
        "reviewed": len(reviewed_ids), "created": created,
        "ran_at": datetime.now().isoformat(),
    }


# ──────────────────────────────────────────────
# Claim-kind classifier (L1 lifecycle, 2026-07-09)
# ──────────────────────────────────────────────
# 61% of active memories had claim_kind NULL (the 2026-04-27 trust layer
# shipped the column + consumers but the backfill script never existed).
# claim_kind drives per-kind staleness TTLs, probe targeting, and the
# reference-class disuse-decay exemption — this job feeds all of it.
CLAIM_KIND_SCHEMA = {
    "type": "object",
    "properties": {
        "classifications": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "claim_kind": {
                        "type": "string",
                        "enum": [
                            "code-state", "infra-state", "preference",
                            "incident-fix", "design-decision", "historical",
                            "reference", "fact", "plan", "creative",
                        ],
                    },
                },
                "required": ["index", "claim_kind"],
            },
        }
    },
    "required": ["classifications"],
}

_CLAIM_KIND_SYSTEM = (
    "Classify each memory note into exactly one claim_kind:\n"
    "- code-state: how code/config IS right now (changes when code changes)\n"
    "- infra-state: how infrastructure IS right now (ports, containers, versions)\n"
    "- preference: a person's preference or working style\n"
    "- incident-fix: a problem that WAS diagnosed/fixed (past event + solution)\n"
    "- design-decision: a choice that was made and why\n"
    "- historical: a record of something that happened (no current-state claim)\n"
    "- reference: external knowledge/documentation/research (true regardless of our systems)\n"
    "- fact: a standalone verifiable fact not covered above\n"
    "- plan: prescriptive FUTURE intent (will/schedule/day-7/phase-2 "
    "wording) — something to be done, not something that is\n"
    "- creative: the author's own personal writing — poetry, ideas, "
    "aphorisms, reflections, personal goals, formulations, philosophy. "
    "Not a system fact; never goes stale; its value is that it exists\n"
    "Pick the kind whose STALENESS MODEL fits: would this become wrong when "
    "our systems change (code/infra-state), or is it a permanent record "
    "(incident-fix/historical/reference)?"
)


async def claim_kind_classifier() -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, left(COALESCE(summary, '') || ' | ' || content, 500) AS text
            FROM memories
            WHERE claim_kind IS NULL AND tier < 3
              AND length(content) > 50
            ORDER BY tier ASC, access_count DESC
            LIMIT $1
            """,
            settings.claim_kind_batch_size,
        )
    if not rows:
        return {"classified": 0, "ran_at": datetime.now().isoformat()}

    classified = 0
    pack_size = 10
    for start in range(0, len(rows), pack_size):
        pack = rows[start:start + pack_size]
        notes = "\n".join(f"[{i}] {r['text']}" for i, r in enumerate(pack))
        try:
            await _yield_to_live_requests()
            resp = await ollama_chat(
                system=_CLAIM_KIND_SYSTEM,
                user=f"Memory notes:\n{notes}",
                schema=CLAIM_KIND_SCHEMA,
                temperature=0.1,
            )
        except Exception:
            logger.exception("claim_kind_classifier LLM call failed")
            continue
        updates = []
        for c in (resp or {}).get("classifications", []):
            idx = int(c.get("index", -1))
            kind = c.get("claim_kind")
            if 0 <= idx < len(pack) and kind:
                updates.append((pack[idx]["id"], kind))
        if updates:
            async with pool.acquire() as conn:
                for mid, kind in updates:
                    await conn.execute(
                        "UPDATE memories SET claim_kind = $1 WHERE id = $2 AND claim_kind IS NULL",
                        kind, mid,
                    )
            classified += len(updates)
    return {"classified": classified, "ran_at": datetime.now().isoformat()}


# ──────────────────────────────────────────────
# Verification probe generator (L1 lifecycle, 2026-07-09)
# ──────────────────────────────────────────────
# The probe pool was frozen at 262 hand-seeded rows — no code path ever
# created probes, so verified coverage sat at 0.7% for months while the
# corpus grew. This job proposes probes for the WORKING SET: checkable
# claims (infra-state / code-state / fact) with real usage and no
# existing probe coverage.
#
# SAFETY: probe_command strings are LLM-generated and executed by the
# hourly nobrainr-verify cron as root. Auto-enabled types are limited to
# http (curl GET), file (cat), and sql (SELECT-only, and the verify cron
# runs sql probes read-only). shell probes are stored DISABLED with
# notes='auto-generated, pending operator review' — never auto-run.
PROBE_SCHEMA = {
    "type": "object",
    "properties": {
        "probes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "source_index": {"type": "integer"},
                    "probe_name": {"type": "string"},
                    "claim_pattern": {"type": "string"},
                    "probe_type": {"type": "string", "enum": ["http", "file", "sql", "shell"]},
                    "probe_command": {"type": "string"},
                    "expected_regex": {"type": "string"},
                    "max_staleness_days": {"type": "integer"},
                    "checkable": {"type": "boolean"},
                },
                "required": ["source_index", "probe_name", "claim_pattern",
                             "probe_type", "probe_command", "expected_regex",
                             "checkable"],
            },
        }
    },
    "required": ["probes"],
}

_PROBE_SYSTEM = (
    "You design verification probes for a knowledge base on host 'bimavo' "
    "(Ubuntu, Docker via Coolify, PostgreSQL 'nobrainr' db, services on "
    "<vpn-host>). Given memory notes stating CURRENT system facts, emit a "
    "probe that mechanically re-checks the claim:\n"
    "- http: a curl-able GET URL (VPN/localhost only) — probe_command is the URL\n"
    "- file: an absolute HOST filesystem path; the probe CATS the file and "
    "matches expected_regex against its CONTENT (never ls/stat output). "
    "Container-internal paths like /app/... are NOT reachable\n"
    "- sql: a read-only SELECT against the nobrainr db\n"
    "- shell: ONLY when nothing else works (stored disabled for review)\n"
    "expected_regex must match the probe output IF the claim still holds. "
    "claim_pattern is a case-insensitive regex matching the memory text that "
    "makes the claim (so future memories with the same claim inherit the "
    "probe). Set checkable=false when the note isn't mechanically checkable "
    "(opinions, history, external-world facts) — including claims about "
    "OTHER machines, other users' home directories, or paths inside "
    "containers, none of which this host can check. NEVER write commands "
    "that modify anything."
)


async def probe_generator() -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT m.id, left(COALESCE(m.summary,'') || ' | ' || m.content, 600) AS text
            FROM memories m
            WHERE m.claim_kind IN ('infra-state', 'code-state', 'fact')
              AND m.tier <= 1
              AND m.superseded_by IS NULL
              AND m.verified_at IS NULL
              AND NOT EXISTS (
                  SELECT 1 FROM verification_log vl WHERE vl.memory_id = m.id
              )
            ORDER BY m.access_count DESC, m.importance DESC
            LIMIT $1
            """,
            settings.probe_generator_batch_size,
        )
    if not rows:
        return {"proposed": 0, "auto_enabled": 0, "ran_at": datetime.now().isoformat()}

    proposed = auto_enabled = 0
    pack_size = 5
    for start in range(0, len(rows), pack_size):
        pack = rows[start:start + pack_size]
        notes = "\n\n".join(f"[{i}] {r['text']}" for i, r in enumerate(pack))
        try:
            await _yield_to_live_requests()
            resp = await ollama_chat(
                system=_PROBE_SYSTEM,
                user=f"Memory notes:\n{notes}",
                schema=PROBE_SCHEMA,
                temperature=0.2,
            )
        except Exception:
            logger.exception("probe_generator LLM call failed")
            continue
        for p in (resp or {}).get("probes", []):
            if not p.get("checkable"):
                continue
            name = (p.get("probe_name") or "").strip()[:100]
            cmd = (p.get("probe_command") or "").strip()
            ptype = p.get("probe_type")
            pattern = (p.get("claim_pattern") or "").strip()
            regex = (p.get("expected_regex") or "").strip()
            if not (name and cmd and pattern and regex):
                continue
            # Hard safety gate beyond the prompt.
            is_safe = (
                (ptype == "http" and cmd.startswith(("http://10.", "http://127.", "http://localhost")))
                or (ptype == "file" and cmd.startswith("/") and " " not in cmd)
                or (ptype == "sql" and cmd.lstrip().lower().startswith("select"))
            )
            enabled = bool(is_safe)
            note = ("auto-generated " + datetime.now().date().isoformat()
                    + ("" if is_safe else " — UNSAFE TYPE, pending operator review"))
            idx = int(p.get("source_index", 0))
            kind_row = pack[idx]["id"] if 0 <= idx < len(pack) else None
            try:
                async with pool.acquire() as conn:
                    inserted = await conn.fetchval(
                        """
                        INSERT INTO verification_probes
                            (probe_name, claim_pattern, probe_type, probe_command,
                             expected_regex, claim_kind, max_staleness_days,
                             enabled, notes)
                        SELECT $1, $2, $3, $4, $5, m.claim_kind,
                               COALESCE($6, 30), $7, $8
                        FROM memories m WHERE m.id = $9
                        ON CONFLICT (probe_name) DO NOTHING
                        RETURNING id
                        """,
                        name, pattern, ptype, cmd, regex,
                        p.get("max_staleness_days"), enabled, note, kind_row,
                    )
                if inserted:
                    proposed += 1
                    if enabled:
                        auto_enabled += 1
            except Exception:
                logger.exception("probe insert failed for %r", name)
    return {"proposed": proposed, "auto_enabled": auto_enabled,
            "ran_at": datetime.now().isoformat()}


# ──────────────────────────────────────────────
# Stability reinforcement (L1 lifecycle, 2026-07-09)
# ──────────────────────────────────────────────
async def stability_reinforce() -> dict:
    """Retrieval-through-use reinforcement: top-ranked hits gain stability."""
    n = await queries.reinforce_stability_from_traces(hours=24)
    return {"reinforced": n, "ran_at": datetime.now().isoformat()}


# ──────────────────────────────────────────────
# Reconciliation sweeper (L1.5 lifecycle, 2026-07-09)
# ──────────────────────────────────────────────
# The plan-vs-reality class: a memory describes intended-or-past state,
# reality moved on, and nothing reconciles them — probes can't verify
# intent and contradiction detection doesn't fire (reality ignores a
# plan, it doesn't contradict it). Discovered live when a 2026-05
# pre-flash plan (Seedvault/Syncthing, never executed) surfaced as
# "memory of the day" while the actual stack (rsync) lived in a newer
# memory sharing the same entities. This job walks old, unverified,
# unsuperseded stale-prone memories, gathers NEWER memories sharing
# their entities, and asks the LLM which is current — writing real
# superseded_by chains (queries.supersede_memory) so the trust formula
# and search filters see it.
RECONCILE_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["still_current", "superseded", "executed_plan", "unclear"],
        },
        "newer_index": {
            "type": "integer",
            "description": "When superseded: which newer note [N] replaces it.",
        },
        "reason": {"type": "string"},
    },
    "required": ["status", "reason"],
}

_RECONCILE_SYSTEM = (
    "You reconcile an OLD memory note against NEWER notes that mention the "
    "same entities. Verdicts:\n"
    "- superseded: a newer note describes the same topic's CURRENT state "
    "and the old note is outdated (pick newer_index)\n"
    "- executed_plan: the old note was a plan/intent whose outcome (done, "
    "changed, or abandoned) the newer notes show — it is now history\n"
    "- still_current: the old note remains accurate; newer notes are about "
    "different aspects\n"
    "- unclear: cannot tell from these notes\n"
    "Be conservative: only 'superseded' when the newer note genuinely "
    "covers the same claim."
)


async def reconciliation_sweep() -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        candidates = await conn.fetch(
            """
            SELECT m.id, left(COALESCE(m.summary,'') || ' | ' || m.content, 700) AS text,
                   m.created_at::date AS cdate
            FROM memories m
            WHERE m.tier < 3
              AND m.superseded_by IS NULL
              AND m.verified_at IS NULL
              AND m.claim_kind IN ('plan', 'infra-state', 'code-state', 'fact')
              AND m.created_at < now() - interval '30 days'
              AND COALESCE((m.metadata->>'last_reconciled')::date, '1970-01-01')
                  < (now() - interval '60 days')::date
            ORDER BY m.access_count DESC, m.created_at ASC
            LIMIT $1
            """,
            settings.reconciliation_batch_size,
        )
    if not candidates:
        return {"checked": 0, "superseded": 0, "historicized": 0,
                "ran_at": datetime.now().isoformat()}

    checked = superseded = historicized = 0
    for cand in candidates:
        async with pool.acquire() as conn:
            newer = await conn.fetch(
                """
                SELECT n.id, left(COALESCE(n.summary,'') || ' | ' || n.content, 500) AS text,
                       n.created_at::date AS cdate, count(*) AS shared
                FROM entity_memories em_old
                JOIN entity_memories em_new ON em_new.entity_id = em_old.entity_id
                JOIN memories n ON n.id = em_new.memory_id
                WHERE em_old.memory_id = $1
                  AND n.id <> $1
                  AND n.superseded_by IS NULL
                  AND n.created_at > (SELECT created_at + interval '14 days'
                                      FROM memories WHERE id = $1)
                GROUP BY n.id, n.summary, n.content, n.created_at
                HAVING count(*) >= 2
                ORDER BY count(*) DESC, n.created_at DESC
                LIMIT 3
                """,
                cand["id"],
            )
        if not newer:
            continue
        newer_txt = "\n\n".join(
            f"[{i}] ({n['cdate']}) {n['text']}" for i, n in enumerate(newer)
        )
        try:
            await _yield_to_live_requests()
            verdict = await ollama_chat(
                system=_RECONCILE_SYSTEM,
                user=(f"OLD note ({cand['cdate']}):\n{cand['text']}\n\n"
                      f"NEWER notes sharing its entities:\n{newer_txt}"),
                schema=RECONCILE_SCHEMA,
                temperature=0.1,
            )
        except Exception:
            logger.exception("reconciliation LLM call failed")
            continue
        checked += 1
        status = (verdict or {}).get("status")
        reason = ((verdict or {}).get("reason") or "")[:300]
        async with pool.acquire() as conn:
            if status == "superseded":
                idx = int(verdict.get("newer_index", 0))
                if 0 <= idx < len(newer):
                    ok = await queries.supersede_memory(
                        str(cand["id"]), str(newer[idx]["id"]),
                        reason=f"reconciliation sweep: {reason}",
                    )
                    if ok:
                        superseded += 1
                        continue
            elif status == "executed_plan":
                await conn.execute(
                    """
                    UPDATE memories
                    SET claim_kind = 'historical',
                        metadata = COALESCE(metadata,'{}'::jsonb)
                            || jsonb_build_object('reconciled', 'executed_plan',
                                                  'reconcile_reason', $2)
                    WHERE id = $1
                    """,
                    cand["id"], reason,
                )
                historicized += 1
                continue
            # still_current / unclear → stamp so we don't re-check for 60d
            await conn.execute(
                """
                UPDATE memories
                SET metadata = COALESCE(metadata,'{}'::jsonb)
                    || jsonb_build_object('last_reconciled', now()::date::text)
                WHERE id = $1
                """,
                cand["id"],
            )
    return {"checked": checked, "superseded": superseded,
            "historicized": historicized, "ran_at": datetime.now().isoformat()}


# ──────────────────────────────────────────────
# Learned-context card builder (C1, 2026-07-14)
# ──────────────────────────────────────────────
# "System-delivers, not agent-searches." Distils a living brief per
# subject (entity/project/community) from its highest-trust, non-
# superseded memories — current state + key decisions + gotchas +
# procedures, superseded facts dropped. Served at session start via the
# nobrainr://card/{subject} resource + session_brief tool, so an agent
# gets one pre-thought, trust-filtered brief instead of N searches.
# Rebuilds only when the subject's newest memory changed (source_max_
# updated), so it's cheap and self-throttling. Trust-gated: only
# memories >= card_min_trust contribute, and reference/creative/timeless
# knowledge is welcome (availability value) but superseded/low-trust is
# excluded (never brief on stale or poisoned content — the ASI06 lesson).
CARD_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "brief": {
            "type": "string",
            "description": (
                "A dense standalone brief an agent can act on without "
                "further searching. Lead with CURRENT STATE, then KEY "
                "DECISIONS (with why), then GOTCHAS, then PROCEDURES. "
                "Omit anything not grounded in the provided notes. Drop "
                "outdated claims. Under 350 words."
            ),
        },
    },
    "required": ["title", "brief"],
}

_CARD_SYSTEM = (
    "You write a living reference card for one subject from an engineer's "
    "verified memory notes. The card is served to AI agents at the start "
    "of their work so they don't have to search — it must be dense, "
    "current, and self-contained. Synthesize; don't list. Prefer the "
    "newest note when two disagree. State facts plainly with their "
    "specifics (versions, paths, IDs, commands). The notes are ordered "
    "NEWEST FIRST — when two disagree, the newer one wins, and if an "
    "older note names a specific (a model, a tool, a status) that a newer "
    "note contradicts or replaces, state ONLY the newer value. If you are "
    "not sure a specific is still current, describe it qualitatively "
    "rather than asserting a stale exact value. If the notes describe a "
    "plan that was later executed or abandoned, reflect the OUTCOME, not "
    "the intention."
)


async def _build_card(pool, subject_type: str, subject_key: str,
                      title_hint: str, source_sql: str, *sql_args) -> dict | None:
    async with pool.acquire() as conn:
        rows = await conn.fetch(source_sql, *sql_args)
    rows = [r for r in rows if (r["content"] or "").strip()]
    if len(rows) < 3:
        return None  # not enough signal to be worth a card
    src_ids = [r["id"] for r in rows]
    newest = max((r["mupd"] for r in rows if r["mupd"]), default=None)
    min_trust = min((r["trust_score"] or 0.5 for r in rows), default=0.5)

    # Skip rebuild if unchanged since last build.
    async with pool.acquire() as conn:
        existing = await conn.fetchrow(
            "SELECT source_max_updated, factcheck FROM context_cards "
            "WHERE subject_type=$1 AND subject_key=$2",
            subject_type, subject_key,
        )
    if existing and existing["source_max_updated"] and newest and \
            existing["source_max_updated"] >= newest:
        return {"skipped": True}

    # Self-heal (M1): claims the fact-checker refuted on the previous
    # version must not be restated — inject them as hard negatives.
    refuted: list[str] = []
    if existing and existing["factcheck"]:
        import json as _json

        fc = existing["factcheck"]
        if isinstance(fc, str):
            try:
                fc = _json.loads(fc)
            except ValueError:
                fc = {}
        refuted = [
            c["claim"] for c in (fc or {}).get("claims", [])
            if c.get("verdict") == "contradicted" and c.get("claim")
        ][:8]

    notes = "\n\n".join(
        f"[{i}] ({(r['mupd'] or r['created_at']).date()}) {(r['summary'] or '')[:120]}\n{r['content'][:900]}"
        for i, r in enumerate(rows[:20])
    )
    user_prompt = f"Subject: {title_hint}\n\nVerified notes:\n{notes}"
    if refuted:
        user_prompt += (
            "\n\nREFUTED CLAIMS — a fact-checker found these statements from "
            "the previous card version to be WRONG. Do not restate them; "
            "state the corrected fact if the notes support one, otherwise "
            "omit the topic:\n- " + "\n- ".join(refuted)
        )
    try:
        resp = await ollama_chat(
            system=_CARD_SYSTEM,
            user=user_prompt,
            schema=CARD_SCHEMA, temperature=0.2,
        )
    except Exception:
        logger.exception("card build LLM failed for %s/%s", subject_type, subject_key)
        return None
    title = (resp or {}).get("title", "").strip()[:200] or title_hint
    brief = (resp or {}).get("brief", "").strip()
    if len(brief) < 40:
        return None
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO context_cards
                (subject_type, subject_key, title, body, source_ids,
                 source_max_updated, trust_score, built_at)
            VALUES ($1,$2,$3,$4,$5::uuid[],$6,$7, now())
            ON CONFLICT (subject_type, subject_key) DO UPDATE
                SET title=EXCLUDED.title, body=EXCLUDED.body,
                    source_ids=EXCLUDED.source_ids,
                    source_max_updated=EXCLUDED.source_max_updated,
                    trust_score=EXCLUDED.trust_score, built_at=now()
            """,
            subject_type, subject_key, title, brief, src_ids, newest, min_trust,
        )
    return {"built": True}


async def card_builder() -> dict:
    pool = await get_pool()
    built = skipped = 0

    # 1) Top active entities by memory linkage (the graph's centers of mass).
    async with pool.acquire() as conn:
        entities = await conn.fetch(
            """
            SELECT e.canonical_name, e.name, count(*) AS n
            FROM entities e
            JOIN entity_memories em ON em.entity_id = e.id
            JOIN memories m ON m.id = em.memory_id
            WHERE m.tier < 3 AND m.superseded_by IS NULL
              AND COALESCE(m.trust_score, 0.5) >= $1
            GROUP BY e.id, e.canonical_name, e.name
            HAVING count(*) >= $2
            ORDER BY count(*) DESC
            LIMIT $3
            """,
            settings.card_min_trust, settings.card_min_sources,
            settings.card_builder_batch_size,
        )
    ent_sql = """
        SELECT m.id, m.content, m.summary, m.created_at, m.updated_at AS mupd, m.trust_score
        FROM memories m
        JOIN entity_memories em ON em.memory_id = m.id
        JOIN entities e ON e.id = em.entity_id
        WHERE e.canonical_name = $1
          AND m.tier < 3 AND m.superseded_by IS NULL
          AND COALESCE(m.trust_score, 0.5) >= $2
        -- Recency-forward for state cards: a newer memory outranks an
        -- older higher-trust one so the card reflects CURRENT reality, not
        -- the best-trusted stale fact. Trust still gates entry (WHERE).
        ORDER BY m.updated_at DESC, COALESCE(m.trust_score,0.5) DESC
        LIMIT 20
    """
    for e in entities:
        await _yield_to_live_requests()
        r = await _build_card(pool, "entity", e["canonical_name"], e["name"],
                              ent_sql, e["canonical_name"], settings.card_min_trust)
        if r and r.get("built"):
            built += 1
        elif r and r.get("skipped"):
            skipped += 1

    return {"built": built, "skipped_unchanged": skipped,
            "ran_at": datetime.now().isoformat()}


# ──────────────────────────────────────────────
# Card fact-checker (M1 HEART PLAN, 2026-07-14)
# ──────────────────────────────────────────────
# Cards get a published_accuracy NUMBER. Motivation: the first C1 cards
# asserted stale specifics ("Gemma 4 26B", "Hetzner CX Gen3") as current —
# a session-start feature that states wrong facts is worse than none.
# Two verification lanes per checkable claim:
#   1. probe lane (mechanical, free): if an enabled verification_probe's
#      claim_pattern matches the claim and its latest run said
#      verified/mismatch, that IS the verdict — live-state ground truth.
#   2. evidence lane (LLM): hybrid-retrieve the newest relevant memories
#      and judge supported/contradicted/unverifiable. Newest evidence wins.
# published_accuracy = supported / (supported + contradicted).
# Below card_min_accuracy → source_max_updated reset so card_builder
# rebuilds, and the refuted claims are injected as "do not restate".
CLAIMS_SCHEMA = {
    "type": "object",
    "properties": {
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "claim": {"type": "string"},
                    "checkable": {"type": "boolean"},
                },
                "required": ["claim", "checkable"],
            },
        }
    },
    "required": ["claims"],
}

_CLAIMS_SYSTEM = (
    "Extract the atomic factual claims from a reference card. Each claim "
    "must be self-contained (name its subject explicitly, no pronouns). "
    "checkable=true only for claims that are objectively true or false "
    "against infrastructure state or recorded notes (versions, names, "
    "counts, statuses, locations, configurations). checkable=false for "
    "opinions, priorities, style, intentions, and vague qualitative "
    "statements. Keep each claim under 30 words."
)

VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {
            "type": "string",
            "enum": ["supported", "contradicted", "unverifiable"],
        },
        "reason": {"type": "string"},
    },
    "required": ["verdict", "reason"],
}

_VERDICT_SYSTEM = (
    "You judge whether a claim is still true given evidence notes from a "
    "knowledge base, ordered NEWEST FIRST. Rules: 'contradicted' only if "
    "the evidence explicitly conflicts with the claim — when notes "
    "disagree, the newer note is the truth. 'supported' if the evidence "
    "affirms it. 'unverifiable' if the evidence neither confirms nor "
    "denies. Judge the claim as a statement about CURRENT state."
)


def _probe_verdict(claim: str, probes: list[dict]) -> str | None:
    """Mechanical lane: match the claim against enabled probes' claim_patterns.

    A probe whose latest run verified → 'supported'; mismatch →
    'contradicted'. probe-error or no match → None (falls through to the
    LLM evidence lane). Patterns are LLM-generated — invalid regexes are
    skipped, never fatal.
    """
    import re

    for p in probes:
        try:
            if not re.search(p["claim_pattern"], claim, re.IGNORECASE):
                continue
        except re.error:
            continue
        if p.get("last_result") == "verified":
            return "supported"
        if p.get("last_result") == "mismatch":
            return "contradicted"
    return None


def _accuracy(supported: int, contradicted: int) -> float | None:
    """supported/(supported+contradicted); None when nothing was decidable."""
    denom = supported + contradicted
    return round(supported / denom, 3) if denom else None


async def _factcheck_card(pool, card, probes: list[dict]) -> dict:
    """Verify one card's claims. Returns the factcheck result dict."""
    import json as _json

    resp = await ollama_chat(
        system=_CLAIMS_SYSTEM,
        user=f"Card: {card['title']}\n\n{card['body']}",
        schema=CLAIMS_SCHEMA, temperature=0.1,
    )
    claims = [
        c for c in (resp or {}).get("claims", [])
        if isinstance(c, dict) and (c.get("claim") or "").strip()
    ][: settings.card_factcheck_max_claims]

    supported = contradicted = unverifiable = 0
    results: list[dict] = []
    for c in claims:
        claim = c["claim"].strip()
        if not c.get("checkable"):
            results.append({"claim": claim, "verdict": "skipped", "via": "uncheckable"})
            continue
        await _yield_to_live_requests()

        # Lane 1: mechanical — probe ground truth.
        verdict = _probe_verdict(claim, probes)
        via = "probe"
        reason = "verification_probe latest run"

        # Lane 2: LLM judge vs newest evidence.
        if verdict is None:
            via = "evidence"
            try:
                emb = await embed_text(claim)
                evidence = await queries.search_memories(
                    embedding=emb, limit=settings.card_factcheck_evidence_k,
                    threshold=0.25, text_query=claim,
                )
            except Exception:
                evidence = []
            if not evidence:
                verdict, reason = "unverifiable", "no evidence retrieved"
            else:
                evidence.sort(key=lambda m: str(m.get("updated_at") or ""), reverse=True)
                notes = "\n\n".join(
                    f"[{i}] ({str(m.get('updated_at') or '')[:10]}) "
                    f"{(m.get('summary') or '')[:100]}\n{(m.get('content') or '')[:600]}"
                    for i, m in enumerate(evidence)
                )
                try:
                    j = await ollama_chat(
                        system=_VERDICT_SYSTEM,
                        user=f"Claim: {claim}\n\nEvidence (newest first):\n{notes}",
                        schema=VERDICT_SCHEMA, temperature=0.1,
                    )
                    verdict = (j or {}).get("verdict", "unverifiable")
                    reason = (j or {}).get("reason", "")[:200]
                except Exception:
                    verdict, reason = "unverifiable", "judge LLM failed"

        if verdict == "supported":
            supported += 1
        elif verdict == "contradicted":
            contradicted += 1
        else:
            unverifiable += 1
        results.append({"claim": claim, "verdict": verdict, "via": via, "reason": reason})

    accuracy = _accuracy(supported, contradicted)
    factcheck = {
        "supported": supported, "contradicted": contradicted,
        "unverifiable": unverifiable, "claims": results,
    }
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE context_cards
            SET published_accuracy = $2::real, factcheck = $3::jsonb,
                factchecked_at = now(),
                -- below the bar → clear the staleness stamp so
                -- card_builder rebuilds this card on its next run
                -- (explicit ::real casts: $2 appears in both an assignment
                -- and a comparison — asyncpg can't deduce one type for it)
                source_max_updated = CASE
                    WHEN $2::real IS NOT NULL AND $2::real < $4::real THEN NULL
                    ELSE source_max_updated END
            WHERE id = $1
            """,
            card["id"], accuracy, _json.dumps(factcheck),
            settings.card_min_accuracy,
        )
    return {"accuracy": accuracy, **factcheck}


async def card_factcheck() -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        cards = await conn.fetch(
            """
            SELECT id, subject_key, title, body FROM context_cards
            ORDER BY factchecked_at ASC NULLS FIRST
            LIMIT $1
            """,
            settings.card_factcheck_batch_size,
        )
        probe_rows = await conn.fetch(
            """
            SELECT p.claim_pattern, l.result AS last_result
            FROM verification_probes p
            LEFT JOIN LATERAL (
                SELECT result FROM verification_log
                WHERE probe_id = p.id ORDER BY ran_at DESC LIMIT 1
            ) l ON true
            WHERE p.enabled
            """
        )
    probes = [dict(r) for r in probe_rows]

    checked = 0
    accuracies: dict[str, float | None] = {}
    for card in cards:
        await _yield_to_live_requests()
        try:
            r = await _factcheck_card(pool, card, probes)
        except Exception:
            logger.exception("card_factcheck failed for %s", card["subject_key"])
            continue
        checked += 1
        accuracies[card["subject_key"]] = r["accuracy"]

    return {"checked": checked, "accuracies": accuracies,
            "ran_at": datetime.now().isoformat()}


# ──────────────────────────────────────────────
# external_verify (E1, 2026-08-11) — third verification lane.
# probe lane checks live HOST state, evidence lane checks claims against
# OTHER MEMORIES — neither can catch an external-world claim that was
# wrong at ingestion (the ChatGPT-era layer) or has since been overtaken
# by reality. This lane checks claim_kind='fact' memories against the
# live web: Brave discovers sources, Crawl4AI fetches them (evidence
# quotes come from OUR crawl — Brave storage-rights forbid persisting
# SERP snippets), an LLM judge issues supported/refuted/inconclusive.
#
# Quota discipline: Brave free tier is a hard monthly cap shared with
# interactive /gpt-researcher use. The job reads the month's counter
# WITHOUT incrementing and refuses to run past
# external_verify_quota_ceiling, reserving the remainder for humans.
# ──────────────────────────────────────────────

# Live-fire lesson (2026-08-11, first run): a memory describing OUR
# workserver's hook config slipped triage ("Claude Code hooks" pattern-
# matched software docs) and the judge REFUTED it with a generic doc
# quote — halving trust on a true memory. Claims scoped to the user's
# own machines/infra are unverifiable by construction: the public web
# cannot confirm or deny private state. Cheap regex catches them before
# any LLM or quota is spent; the judge prompt is the backstop.
_EXT_INTERNAL_RE = re.compile(
    r"\b(workserver|worklaptop|gis-admin|bimavo|budinic|nobrainr|paperclip"
    r"|hetzner-vps|rpiubuntu|ubuntupi|privateubuntu|coolify-db|llama-swap"
    r"|metamcp|crawl4ai)\b"
    r"|\b10\.(?:10|0)\.\d{1,3}\.\d{1,3}\b",
    re.IGNORECASE,
)

_EXT_TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "i": {"type": "integer"},
                    "checkable": {"type": "boolean"},
                    "query": {"type": "string"},
                },
                "required": ["i", "checkable", "query"],
            },
        }
    },
    "required": ["claims"],
}

_EXT_TRIAGE_SYSTEM = (
    "You triage knowledge-base notes for WEB fact-checking. For each note "
    "decide if its core claim is checkable against the public web: stable "
    "external-world facts (software capabilities, release states, specs, "
    "standards, published prices, documented APIs) are checkable; personal "
    "notes, private-project trivia, opinions, and ANY claim about the "
    "user's own machines, servers, self-hosted services, or project "
    "configuration are NOT (the public web cannot know private state). "
    "For checkable claims write ONE precise search "
    "query (include a year only for time-sensitive claims). For "
    "non-checkable claims set query to an empty string."
)

_EXT_JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["supported", "refuted", "inconclusive"]},
        "evidence_quote": {"type": "string"},
        "reason": {"type": "string"},
    },
    "required": ["verdict", "evidence_quote", "reason"],
}

_EXT_JUDGE_SYSTEM = (
    "You fact-check ONE claim against crawled web-page excerpts. "
    "'supported' only when an excerpt clearly confirms the claim's core "
    "assertion; 'refuted' only when an excerpt clearly contradicts it; "
    "otherwise 'inconclusive'. Nuance: a claim that was true for an old "
    "software version but is no longer true for current versions is "
    "'refuted' (the knowledge base serves CURRENT reality). An excerpt "
    "that merely DISCUSSES the claim's topic without contradicting its "
    "specific assertion is NOT a refutation — verdict inconclusive. If "
    "the claim describes the user's own environment or configuration, "
    "the web cannot refute it — verdict inconclusive. evidence_quote "
    "is a verbatim excerpt (<=400 chars) from the pages that grounds your "
    "verdict; empty string when inconclusive."
)


async def _ext_month_usage() -> int:
    """This month's Brave query count — read-only, no increment."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT COALESCE((SELECT queries FROM web_search_usage "
            "WHERE month = to_char(now(), 'YYYY-MM')), 0)"
        ) or 0


async def external_verify() -> dict:
    from nobrainr.crawler.client import crawl4ai_request
    from nobrainr.mcp.server import (
        _brave_search_request,
        _count_web_search_use,
        _sanitize_crawled_text,
    )

    out = {"triaged": 0, "searched": 0, "supported": 0, "refuted": 0,
           "inconclusive": 0, "unverifiable": 0, "skipped_quota": 0,
           "ran_at": datetime.now().isoformat()}

    used = await _ext_month_usage()
    if used >= settings.external_verify_quota_ceiling:
        out["skipped_quota"] = 1
        return out

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, left(COALESCE(summary,'') || ' | ' || content, 500) AS text
            FROM memories
            WHERE claim_kind = 'fact'
              AND tier <= 2
              AND superseded_by IS NULL
              AND external_verified_at IS NULL
              AND COALESCE((metadata->>'ext_verify_attempts')::int, 0) < 2
            ORDER BY access_count DESC, created_at DESC
            LIMIT $1
            """,
            settings.external_verify_batch_size,
        )
    if not rows:
        return out

    # Triage the whole batch in one LLM call — checkability must be decided
    # BEFORE any Brave quota is spent.
    notes = "\n\n".join(f"[{i}] {r['text']}" for i, r in enumerate(rows))
    await _yield_to_live_requests()
    try:
        triage = await ollama_chat(
            system=_EXT_TRIAGE_SYSTEM,
            user=f"Notes:\n{notes}",
            schema=_EXT_TRIAGE_SCHEMA,
            temperature=0.2,
        )
    except Exception:
        logger.exception("external_verify triage failed")
        return out
    by_idx = {c.get("i"): c for c in triage.get("claims", [])}
    out["triaged"] = len(by_idx)

    for i, row in enumerate(rows):
        cl = by_idx.get(i)
        if cl is None:
            continue
        internal_scope = bool(_EXT_INTERNAL_RE.search(row["text"]))
        if internal_scope or not cl.get("checkable") or not (cl.get("query") or "").strip():
            # Never re-picked, never costs quota.
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE memories
                    SET external_verdict = 'unverifiable', external_verified_at = now()
                    WHERE id = $1
                    """,
                    row["id"],
                )
            out["unverifiable"] += 1
            continue

        # Re-check the ceiling inside the loop — interactive use shares the pool.
        if await _ext_month_usage() >= settings.external_verify_quota_ceiling:
            out["skipped_quota"] = 1
            break

        try:
            await _count_web_search_use()
            data = await _brave_search_request(
                {"q": cl["query"], "count": 5},
            )
        except Exception:
            logger.warning("external_verify search failed for %s", row["id"])
            continue
        out["searched"] += 1
        urls = [r.get("url") for r in data.get("web", {}).get("results", [])
                if r.get("url")][:3]

        # Evidence from OUR crawl, never from the SERP.
        excerpts: list[tuple[str, str]] = []
        for url in urls:
            if len(excerpts) >= 2:
                break
            try:
                res = await crawl4ai_request(url, timeout=60.0)
            except Exception:
                continue
            if res.get("error") or not res.get("results"):
                continue
            md = (res["results"][0].get("markdown") or {})
            text = md.get("fit_markdown") or md.get("raw_markdown") or ""
            text, _ = _sanitize_crawled_text(text[:3000])
            if len(text) > 200:
                excerpts.append((url, text))

        if not excerpts:
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE memories
                    SET metadata = COALESCE(metadata,'{}'::jsonb) ||
                        jsonb_build_object('ext_verify_attempts',
                            COALESCE((metadata->>'ext_verify_attempts')::int, 0) + 1)
                    WHERE id = $1
                    """,
                    row["id"],
                )
            out["inconclusive"] += 1
            continue

        evidence_block = "\n\n".join(
            f"[source {n+1}: {u}]\n{t}" for n, (u, t) in enumerate(excerpts)
        )
        await _yield_to_live_requests()
        try:
            verdict = await ollama_chat(
                system=_EXT_JUDGE_SYSTEM,
                user=f"Claim:\n{row['text']}\n\nWeb excerpts:\n{evidence_block}",
                schema=_EXT_JUDGE_SCHEMA,
                temperature=0.2,
            )
        except Exception:
            logger.exception("external_verify judge failed for %s", row["id"])
            continue

        v = verdict.get("verdict")
        quote = (verdict.get("evidence_quote") or "")[:400]
        if v == "supported":
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE memories
                    SET external_verdict = 'supported',
                        external_verified_at = now(),
                        external_truth_url = $2,
                        external_evidence = $3,
                        trust_score = LEAST(1.0, COALESCE(trust_score, 0.5) + 0.15)
                    WHERE id = $1
                    """,
                    row["id"], excerpts[0][0], quote,
                )
            out["supported"] += 1
        elif v == "refuted":
            # Hard trust cut: refuted-by-live-web is the strongest negative
            # signal a memory can receive. Serving trust floors (0.5/0.6)
            # then hide it; reconciliation/digest surface it for supersede.
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE memories
                    SET external_verdict = 'refuted',
                        external_verified_at = now(),
                        external_truth_url = $2,
                        external_evidence = $3,
                        trust_score = GREATEST(0.05, COALESCE(trust_score, 0.5) * 0.5)
                    WHERE id = $1
                    """,
                    row["id"], excerpts[0][0], quote,
                )
            out["refuted"] += 1
        else:
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE memories
                    SET metadata = COALESCE(metadata,'{}'::jsonb) ||
                        jsonb_build_object('ext_verify_attempts',
                            COALESCE((metadata->>'ext_verify_attempts')::int, 0) + 1)
                    WHERE id = $1
                    """,
                    row["id"],
                )
            out["inconclusive"] += 1

    return out
