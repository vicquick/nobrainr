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


async def store_document_chunked(
    content: str,
    *,
    title: str | None = None,
    summary: str | None = None,
    tags: list[str] | None = None,
    category: str | None = None,
    source_type: str = "document",
    source_machine: str | None = None,
    source_ref: str | None = None,
    confidence: float = 0.8,
    metadata: dict | None = None,
    max_chars: int | None = None,
    overlap: int | None = None,
) -> dict:
    """Store a long document as chunked memories with linking metadata.

    If the content is shorter than the chunk threshold, stores as a single
    memory via the normal path.  Otherwise splits into overlapping chunks,
    stores each one, and links them via metadata.

    Returns:
        {"status": "stored", "chunks": N, "memory_ids": [...], "document_id": "..."}
    """
    from nobrainr.services.chunking import chunk_text

    content = content.strip()
    if not content:
        return {"error": "Empty content"}

    # Short content — store as single memory
    if len(content) <= settings.chunk_threshold:
        result = await store_memory_with_extraction(
            content=content,
            summary=summary or (f"Document: {title}" if title else None),
            tags=tags,
            category=category,
            source_type=source_type,
            source_machine=source_machine,
            source_ref=source_ref,
            confidence=confidence,
            metadata=metadata,
        )
        return {**result, "chunks": 1, "memory_ids": [result.get("id") or result.get("updated_id", "")]}

    # Chunk the content
    chunks = chunk_text(content, max_chars=max_chars, overlap=overlap)
    if not chunks:
        return {"error": "Chunking produced no output"}

    # Generate a document ID to link all chunks
    import uuid
    document_id = str(uuid.uuid4())

    memory_ids: list[str] = []
    stored = 0
    skipped = 0

    for chunk in chunks:
        chunk_meta = dict(metadata or {})
        chunk_meta.update({
            "document_id": document_id,
            "chunk_index": chunk.index,
            "chunk_total": chunk.total,
            "chunk_offset": chunk.char_offset,
        })
        if title:
            chunk_meta["document_title"] = title

        chunk_summary = title or summary or source_ref or "Document chunk"
        if chunk.total > 1:
            chunk_summary = f"{chunk_summary} [{chunk.index + 1}/{chunk.total}]"

        result = await store_memory_with_extraction(
            content=chunk.text,
            summary=chunk_summary[:200],
            tags=tags,
            category=category,
            source_type=source_type,
            source_machine=source_machine,
            source_ref=source_ref,
            confidence=confidence,
            metadata=chunk_meta,
            skip_dedup=True,  # Don't dedup individual chunks
        )

        mid = result.get("id") or result.get("updated_id", "")
        if mid:
            memory_ids.append(mid)
            stored += 1
        else:
            skipped += 1

    return {
        "status": "stored",
        "document_id": document_id,
        "chunks": stored,
        "skipped": skipped,
        "memory_ids": memory_ids,
    }


def _schedule_extraction(memory_id: str, content: str, tags: list[str] | None) -> None:
    """Schedule entity extraction as a background task."""

    async def _run():
        async with _extraction_semaphore:
            try:
                from nobrainr.extraction.pipeline import process_memory

                await process_memory(memory_id, content, tags)
            except Exception:
                logger.exception("Extraction failed for %s", memory_id)
            await asyncio.sleep(1)  # Brief cooldown between extractions

    try:
        asyncio.create_task(_run())
    except Exception:
        logger.exception("Failed to start extraction task for %s", memory_id)
