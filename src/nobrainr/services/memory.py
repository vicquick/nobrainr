"""Shared memory storage service — used by MCP tools, scheduler, and crawler.

Provides store_memory_with_extraction() which handles:
  1. Context-enriched embedding
  2. Dedup check (LLM-based)
  3. Storage via queries.store_memory()
  4. Fire-and-forget entity extraction
"""

import asyncio
import logging

from nobrainr.config import settings
from nobrainr.db import queries
from nobrainr.embeddings.ollama import embed_text

logger = logging.getLogger("nobrainr")

# Rate-limit extraction: 1 at a time with 30s cooldown
_extraction_semaphore = asyncio.Semaphore(1)


async def store_memory_with_extraction(
    content: str,
    *,
    summary: str | None = None,
    tags: list[str] | None = None,
    category: str | None = None,
    source_type: str = "manual",
    source_machine: str | None = None,
    source_ref: str | None = None,
    confidence: float = 1.0,
    metadata: dict | None = None,
    skip_dedup: bool = False,
) -> dict:
    """Store a memory with embedding, dedup check, and async entity extraction.

    This is the canonical way to store memories — both MCP tools and internal
    code (scheduler, crawler) should use this instead of queries.store_memory().

    Returns:
        {"status": "stored", "id": ..., "created_at": ...}
        or {"status": "merged", "merged_with": ..., "reason": ...}
    """
    confidence = max(0.0, min(confidence, 1.0))

    # Context-enriched embedding
    embed_parts = []
    if category:
        embed_parts.append(category)
    if tags:
        embed_parts.append(", ".join(tags))
    embed_input = ". ".join(embed_parts) + ". " + content if embed_parts else content
    embedding = await embed_text(embed_input)

    # Dedup check
    if settings.extraction_enabled and not skip_dedup:
        try:
            from nobrainr.extraction.dedup import check_memory_dedup

            dedup_result = await check_memory_dedup(content, embedding)
            if dedup_result and dedup_result.get("should_merge"):
                target_id = dedup_result["target_id"]
                merged_content = dedup_result["merged_content"]
                new_embedding = await embed_text(merged_content)
                await queries.update_memory(
                    target_id,
                    content=merged_content,
                    embedding=new_embedding,
                    tags=tags,
                    metadata=metadata,
                )
                logger.info("Merged memory into %s: %s", target_id, dedup_result.get("reason"))
                return {
                    "status": "merged",
                    "merged_with": target_id,
                    "reason": dedup_result.get("reason", ""),
                }
        except Exception:
            logger.exception("Dedup check failed, storing as new")

    # Store
    result = await queries.store_memory(
        content=content,
        embedding=embedding,
        summary=summary,
        source_type=source_type,
        source_machine=source_machine,
        source_ref=source_ref,
        tags=tags,
        category=category,
        confidence=confidence,
        metadata=metadata,
    )

    # Fire-and-forget entity extraction
    if settings.extraction_enabled:
        _schedule_extraction(result["id"], content, tags)

    return {"status": "stored", **result}


def _schedule_extraction(memory_id: str, content: str, tags: list[str] | None) -> None:
    """Schedule entity extraction as a background task."""

    async def _run():
        async with _extraction_semaphore:
            try:
                from nobrainr.extraction.pipeline import process_memory

                await process_memory(memory_id, content, tags)
            except Exception:
                logger.exception("Extraction failed for %s", memory_id)
            await asyncio.sleep(30)

    try:
        asyncio.create_task(_run())
    except Exception:
        logger.exception("Failed to start extraction task for %s", memory_id)
