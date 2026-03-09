"""Shared memory storage service — used by MCP tools, scheduler, and crawler.

Provides store_memory_with_extraction() which handles:
  1. Context-enriched embedding
  2. Mem0-style write path (ADD/UPDATE/SUPERSEDE/NOOP)
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
    """Store a memory with embedding, write-path decision, and async entity extraction.

    This is the canonical way to store memories — both MCP tools and internal
    code (scheduler, crawler) should use this instead of queries.store_memory().

    Write-path actions (Mem0-style):
      - ADD: store as new memory
      - UPDATE: merge into existing memory
      - SUPERSEDE: archive old, store new
      - NOOP: exact duplicate, skip

    Returns:
        {"status": "stored"|"updated"|"superseded"|"skipped", ...}
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

    # Write-path decision: ADD / UPDATE / SUPERSEDE / NOOP
    if settings.extraction_enabled and not skip_dedup:
        try:
            from nobrainr.extraction.dedup import decide_write_action

            decision = await decide_write_action(content, embedding)
            action = decision.get("action", "ADD")

            if action == "NOOP":
                logger.info("Write path NOOP: %s", decision.get("reason"))
                return {
                    "status": "skipped",
                    "reason": decision.get("reason", "Duplicate"),
                }

            if action == "UPDATE":
                target_id = decision["target_id"]
                merged_content = decision["content"]
                new_embedding = await embed_text(merged_content)
                # Trigger snapshots old state automatically
                await queries.update_memory(
                    target_id,
                    content=merged_content,
                    embedding=new_embedding,
                    tags=tags,
                    category=category,
                    metadata=metadata,
                    _changed_by="mcp",
                    _change_type="dedup_update",
                    _change_reason=decision.get("reason", ""),
                )
                logger.info("Write path UPDATE %s: %s", target_id, decision.get("reason"))
                return {
                    "status": "updated",
                    "updated_id": target_id,
                    "reason": decision.get("reason", ""),
                }

            if action == "SUPERSEDE":
                target_id = decision["target_id"]
                # Trigger snapshots old state automatically
                await queries.update_memory(
                    target_id,
                    category="_archived",
                    metadata={"archived_reason": "superseded", "superseded_by": "pending"},
                    _changed_by="mcp",
                    _change_type="dedup_supersede",
                    _change_reason=decision.get("reason", ""),
                )
                if metadata is None:
                    metadata = {}
                metadata["supersedes"] = target_id
                logger.info("Write path SUPERSEDE %s: %s", target_id, decision.get("reason"))
                # Fall through to store new memory below

        except Exception:
            logger.exception("Write path decision failed, storing as new")

    # Store new memory (ADD or SUPERSEDE fall-through)
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

    # For SUPERSEDE, backlink the archived memory
    if metadata and metadata.get("supersedes"):
        old_id = metadata["supersedes"]
        try:
            await queries.update_memory(
                old_id,
                metadata={"superseded_by": result["id"]},
            )
        except Exception:
            logger.warning("Failed to backlink superseded memory %s", old_id)
        _schedule_extraction(result["id"], content, tags)
        return {"status": "superseded", "new_id": result["id"], "archived_id": old_id}

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
