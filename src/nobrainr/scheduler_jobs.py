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
                user=mem["content"][:3000],
                schema=SUMMARIZE_SCHEMA,
                model=model,
                timeout=60.0,
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
                timeout=60.0,
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
        await _yield_to_live_requests()

    return {"validated": validated, "invalid": invalid, "ran_at": datetime.now().isoformat()}


async def entity_pruning() -> dict:
    """Prune low-value noise entities (1 memory, no relations, older than 24h)."""
    result = await queries.prune_noise_entities(min_age_hours=24)
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
    from nobrainr.services.memory import store_memory_with_extraction

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
                timeout=60.0,
                think=False,
            )

            researched += 1

            if not result.get("should_research"):
                # Log that we checked but skipped
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

            # Crawl the URL
            crawl_result = await _crawl_url(url)
            if not crawl_result:
                await _yield_to_live_requests()
                continue

            markdown = crawl_result["markdown"][:8000]
            if len(markdown.strip()) < 100:
                await _yield_to_live_requests()
                continue

            # Store the research
            tags = ["crawled", "entity-research", entity["entity_type"], entity["canonical_name"]]
            store_result = await store_memory_with_extraction(
                content=markdown,
                summary=f"Research: {entity['name']} — {crawl_result['title']}"[:200],
                source_type="crawl",
                source_machine=settings.source_machine or "unknown",
                source_ref=url,
                tags=tags,
                category="documentation",
                confidence=0.8,
                metadata={"researched_entity": entity["name"], "entity_id": entity["id"]},
            )

            if store_result.get("status") in ("stored", "merged"):
                stored += 1
                logger.info(
                    "Entity research stored: %s → %s (%s)",
                    entity["name"], url, crawl_result["title"],
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
    from nobrainr.services.memory import store_memory_with_extraction

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
                timeout=60.0,
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

            # Crawl it
            crawl_result = await _crawl_url(url)
            if not crawl_result:
                await _yield_to_live_requests()
                continue

            markdown = crawl_result["markdown"][:8000]
            if len(markdown.strip()) < 100:
                await _yield_to_live_requests()
                continue

            refined = result.get("refined_topic", topic)
            tags = ["crawled", "interest-research", refined.lower().replace(" ", "-")]
            store_result = await store_memory_with_extraction(
                content=markdown,
                summary=f"Interest research: {refined} — {crawl_result['title']}"[:200],
                source_type="crawl",
                source_machine=settings.source_machine or "unknown",
                source_ref=url,
                tags=tags,
                category="documentation",
                confidence=0.75,
                metadata={"interest_topic": topic, "interest_score": score},
            )

            if store_result.get("status") in ("stored", "merged"):
                stored += 1
                logger.info("Interest research stored: %s → %s", topic, url)

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


async def quality_scoring() -> dict:
    """LLM-assess quality of unscored memories (specificity, actionability, self-containment)."""
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

            result = await ollama_chat(
                system=(
                    "You assess the quality of knowledge base entries for AI coding agents. "
                    "Rate each dimension 1-5:\n"
                    "- specificity: 1=vague/generic ('Python is useful'), 5=concrete details "
                    "(commands, file paths, error messages, version numbers)\n"
                    "- actionability: 1=trivia/opinion/personal, 5=an agent can directly use "
                    "this to solve a problem or make a technical decision\n"
                    "- self_containment: 1=needs original conversation context to understand, "
                    "5=fully self-contained and clear\n"
                    "Be strict. Generic programming tips are 1-2. Specific bug fixes with "
                    "root cause are 4-5. Personal/non-technical content is 1."
                ),
                user=f"Source: {source} | Category: {category}\n\n{content}",
                schema=MEMORY_QUALITY_SCHEMA,
                model=model,
                timeout=60.0,
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
