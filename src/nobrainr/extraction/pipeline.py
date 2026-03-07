"""Orchestrator — runs extraction pipeline on memories."""

import asyncio
import logging
from typing import Callable

from nobrainr.db.queries import (
    find_or_create_entity,
    get_unextracted_memories,
    link_entity_to_memory,
    set_extraction_status,
    store_entity_relation,
)
from nobrainr.embeddings.ollama import embed_batch, embed_text
from nobrainr.extraction.extractor import extract_entities

logger = logging.getLogger("nobrainr")


async def process_memory(
    memory_id: str,
    content: str,
    tags: list[str] | None = None,
) -> None:
    """Run the full extraction pipeline on a single memory.

    1. Mark extraction as pending
    2. Extract entities and relationships via LLM
    3. Create/find entities with embeddings, link to memory
    4. Store relationships between entities
    5. Mark extraction as done (or failed on error)
    """
    try:
        await set_extraction_status(memory_id, "pending")

        result = await extract_entities(content)

        # Map entity names to their resolved IDs for relationship linking
        entity_id_map: dict[str, str] = {}

        # Batch-embed all entities at once (context-enriched: "type: name - description")
        if result.entities:
            embed_texts = []
            for entity in result.entities:
                desc = entity.description or entity.name
                embed_texts.append(f"{entity.entity_type}: {entity.name} - {desc}")
            embeddings = await embed_batch(embed_texts)

            for entity, embedding in zip(result.entities, embeddings):
                entity_id = await find_or_create_entity(
                    entity.name,
                    entity.entity_type,
                    description=entity.description,
                    embedding=embedding,
                )
                entity_id_map[entity.name] = entity_id

                await link_entity_to_memory(
                    memory_id, entity_id, role="mention", confidence=1.0,
                )

        for rel in result.relationships:
            source_id = entity_id_map.get(rel.source)
            target_id = entity_id_map.get(rel.target)

            if not source_id or not target_id:
                logger.warning(
                    "Skipping relationship %s -> %s: entity not found in extraction",
                    rel.source, rel.target,
                )
                continue

            await store_entity_relation(
                source_id,
                target_id,
                rel.relationship_type,
                confidence=rel.confidence,
                source_memory=memory_id,
            )

        await set_extraction_status(memory_id, "done")

        logger.info(
            "Extracted %d entities, %d relationships from memory %s",
            len(result.entities), len(result.relationships), memory_id,
        )

    except Exception:
        logger.exception("Extraction pipeline failed for memory %s", memory_id)
        await set_extraction_status(memory_id, "failed")


async def backfill(
    batch_size: int = 5,
    concurrency: int = 4,
    on_progress: Callable[[int, dict], None] | None = None,
) -> int:
    """Process all unextracted memories in batches with concurrency.

    Args:
        batch_size: Number of memories to fetch per batch.
        concurrency: Max concurrent extraction tasks (match Ollama NUM_PARALLEL).
        on_progress: Optional callback(processed_count, memory_dict) for CLI progress.

    Returns:
        Total number of memories processed.
    """
    total = 0
    sem = asyncio.Semaphore(concurrency)

    async def _process_one(memory: dict) -> bool:
        async with sem:
            try:
                await process_memory(
                    memory_id=memory["id"],
                    content=memory["content"],
                    tags=memory.get("tags"),
                )
                return True
            except Exception:
                return False

    while True:
        batch = await get_unextracted_memories(batch_size)
        if not batch:
            break

        tasks = [asyncio.create_task(_process_one(m)) for m in batch]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for memory, result in zip(batch, results):
            if result is True:
                total += 1
            if on_progress:
                on_progress(total, memory)

    logger.info("Backfill complete: %d memories processed", total)
    return total
