"""LLM-powered scheduler jobs for autonomous knowledge growth."""

import logging
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
                    source_machine="myserver",
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
                    source_machine="myserver",
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
