"""LLM-powered scheduler jobs for autonomous knowledge growth."""

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
                user=mem["content"],
                schema=SUMMARIZE_SCHEMA,
                model=model,
                timeout=60.0,
                think=False,
            )
            summary = result.get("summary", "").strip()
            if summary:
                await queries.update_memory(mem["id"], summary=summary)
                count += 1
        except Exception:
            logger.exception("auto_summarize failed for memory %s", mem["id"][:8])

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
                    f"Memory A:\n{pair['content_a']}\n\n"
                    f"Memory B (similarity {pair.get('similarity', 0):.4f}):\n{pair['content_b']}"
                ),
                schema=DEDUP_SCHEMA,
                model=model,
                timeout=90.0,
            )

            if result.get("should_merge") and result.get("merged_content"):
                # Update the winner with merged content, re-embed
                merged_content = result["merged_content"]
                embedding = await embed_text(merged_content)
                await queries.update_memory(
                    pair["id_a"], content=merged_content, embedding=embedding,
                )
                # Soft-delete the loser by archiving
                await queries.update_memory(pair["id_b"], category="_archived")
                merged += 1
            else:
                await queries.mark_memories_consolidation_checked(pair["id_a"], pair["id_b"])

            checked += 1
        except Exception:
            logger.exception("consolidation failed for pair %s/%s", pair["id_a"][:8], pair["id_b"][:8])

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

    return {"synthesized": count, "candidates": len(candidates), "ran_at": datetime.now().isoformat()}


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
                timeout=60.0,
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
                timeout=60.0,
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
                timeout=90.0,
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
                    tags=["auto-detected", "needs-review"],
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

QUALITY_SCHEMA = {
    "type": "object",
    "properties": {
        "is_valid": {
            "type": "boolean",
            "description": "Whether the entity was correctly extracted from the memory",
        },
        "correct_type": {
            "type": "string",
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
                    f"Memory content:\n{sample['memory_content']}\n\n"
                    f"Extracted entity: {sample['entity_name']} (type: {sample['entity_type']})\n\n"
                    "Is this entity correctly extracted from this memory?"
                ),
                schema=QUALITY_SCHEMA,
                model=model,
                timeout=60.0,
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

    return {"validated": validated, "invalid": invalid, "ran_at": datetime.now().isoformat()}


async def chatgpt_distill() -> dict:
    """Distill raw ChatGPT conversations into memory learnings."""
    from nobrainr.importers.chatgpt import distill_conversations

    result = await distill_conversations(
        batch_size=settings.chatgpt_distill_batch_size,
        llm_model=settings.chatgpt_distill_model,
    )
    return {
        "distilled": result["distilled"],
        "processed": result["processed"],
        "skipped": result["skipped"],
        "ran_at": datetime.now().isoformat(),
    }
