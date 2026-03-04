"""nobrainr MCP server — collective agent memory with knowledge graph."""

import asyncio
import logging

from mcp.server.fastmcp import FastMCP

from nobrainr.config import settings
from nobrainr.db import queries
from nobrainr.embeddings.ollama import embed_text

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("nobrainr")

# ──────────────────────────────────────────────
# FastMCP instance (no lifespan — parent app handles it)
# ──────────────────────────────────────────────

mcp = FastMCP(
    "nobrainr",
    host=settings.host,
    port=settings.port,
    instructions=(
        "nobrainr is a collective memory service for AI agents with a knowledge graph. "
        "Use memory_store to save learnings, decisions, patterns, and context. "
        "Use memory_search for semantic search across all stored knowledge (now relevance-ranked). "
        "Use memory_query for structured filtering by tags, category, source. "
        "Use entity_search to find entities in the knowledge graph. "
        "Use entity_graph to explore entity connections. "
        "Use memory_maintenance to run periodic intelligence tasks. "
        "Always tag memories well so they can be found later."
    ),
)


# ──────────────────────────────────────────────
# Tool: memory_store (with dedup + async extraction)
# ──────────────────────────────────────────────
@mcp.tool()
async def memory_store(
    content: str,
    summary: str | None = None,
    tags: list[str] | None = None,
    category: str | None = None,
    source_type: str = "manual",
    source_machine: str | None = None,
    source_ref: str | None = None,
    confidence: float = 1.0,
    metadata: dict | None = None,
) -> dict:
    """Store a new memory with automatic embedding, dedup check, and entity extraction.

    Args:
        content: The knowledge/learning/decision to remember.
        summary: One-line summary for quick scanning.
        tags: List of tags for categorization (e.g. ["python", "debugging", "asyncio"]).
        category: High-level category (e.g. "architecture", "debugging", "ops", "pattern").
        source_type: Where this came from ("manual", "chatgpt", "claude", "agent").
        source_machine: Which VPN host generated this (e.g. "myserver", "workserver").
        source_ref: Reference to original source (conversation ID, file path, etc.).
        confidence: How reliable is this knowledge (0.0-1.0, default 1.0).
        metadata: Any additional structured data.
    """
    embedding = await embed_text(content)

    # Dedup check: see if this memory should merge with an existing one
    if settings.extraction_enabled:
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

    # Store new memory
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
        try:
            from nobrainr.extraction.pipeline import process_memory
            asyncio.create_task(process_memory(result["id"], content, tags))
        except Exception:
            logger.exception("Failed to start extraction task")

    return {"status": "stored", **result}


# ──────────────────────────────────────────────
# Tool: memory_search
# ──────────────────────────────────────────────
@mcp.tool()
async def memory_search(
    query: str,
    limit: int = 10,
    threshold: float = 0.3,
    tags: list[str] | None = None,
    category: str | None = None,
    source_type: str | None = None,
    source_machine: str | None = None,
    hybrid: bool = False,
) -> list[dict]:
    """Semantic search across all memories, ranked by relevance (similarity + recency + importance).

    Args:
        query: Natural language search query (e.g. "How did we fix the Docker networking issue?").
        limit: Max results to return (default 10).
        threshold: Minimum similarity score 0.0-1.0 (default 0.3).
        tags: Filter to memories with any of these tags.
        category: Filter to specific category.
        source_type: Filter by source ("chatgpt", "claude", "manual", "agent").
        source_machine: Filter to specific VPN host.
        hybrid: Also apply text search on the query for hybrid results.
    """
    embedding = await embed_text(query)
    return await queries.search_memories(
        embedding=embedding,
        limit=limit,
        threshold=threshold,
        tags=tags,
        category=category,
        source_type=source_type,
        source_machine=source_machine,
        text_query=query if hybrid else None,
    )


# ──────────────────────────────────────────────
# Tool: memory_query
# ──────────────────────────────────────────────
@mcp.tool()
async def memory_query(
    tags: list[str] | None = None,
    category: str | None = None,
    source_type: str | None = None,
    source_machine: str | None = None,
    text_query: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    """Structured query for memories with filters. No semantic search, just filtering.

    Args:
        tags: Filter to memories with any of these tags.
        category: Filter to specific category.
        source_type: Filter by source ("chatgpt", "claude", "manual", "agent").
        source_machine: Filter to specific VPN host.
        text_query: Full-text search on content.
        limit: Max results (default 50).
        offset: Pagination offset.
    """
    return await queries.query_memories(
        tags=tags,
        category=category,
        source_type=source_type,
        source_machine=source_machine,
        text_query=text_query,
        limit=limit,
        offset=offset,
    )


# ──────────────────────────────────────────────
# Tool: memory_get
# ──────────────────────────────────────────────
@mcp.tool()
async def memory_get(memory_id: str) -> dict | None:
    """Get a specific memory by its ID.

    Args:
        memory_id: The UUID of the memory to retrieve.
    """
    return await queries.get_memory(memory_id)


# ──────────────────────────────────────────────
# Tool: memory_update
# ──────────────────────────────────────────────
@mcp.tool()
async def memory_update(
    memory_id: str,
    content: str | None = None,
    summary: str | None = None,
    tags: list[str] | None = None,
    category: str | None = None,
    confidence: float | None = None,
    metadata: dict | None = None,
) -> dict | None:
    """Update an existing memory. Re-embeds if content changes.

    Args:
        memory_id: The UUID of the memory to update.
        content: New content (triggers re-embedding).
        summary: New summary.
        tags: New tags (replaces existing).
        category: New category.
        confidence: New confidence score.
        metadata: Additional metadata to merge.
    """
    embedding = None
    if content is not None:
        embedding = await embed_text(content)

    return await queries.update_memory(
        memory_id,
        content=content,
        summary=summary,
        embedding=embedding,
        tags=tags,
        category=category,
        confidence=confidence,
        metadata=metadata,
    )


# ──────────────────────────────────────────────
# Tool: memory_delete
# ──────────────────────────────────────────────
@mcp.tool()
async def memory_delete(memory_id: str) -> dict:
    """Delete a memory by its ID.

    Args:
        memory_id: The UUID of the memory to delete.
    """
    deleted = await queries.delete_memory(memory_id)
    if deleted:
        return {"status": "deleted", "id": memory_id}
    return {"status": "not_found", "id": memory_id}


# ──────────────────────────────────────────────
# Tool: memory_stats
# ──────────────────────────────────────────────
@mcp.tool()
async def memory_stats() -> dict:
    """Get statistics about the memory database including knowledge graph stats.

    Returns counts by source, category, machine, top tags, entity/relation counts.
    """
    return await queries.get_stats()


# ──────────────────────────────────────────────
# Tool: entity_search
# ──────────────────────────────────────────────
@mcp.tool()
async def entity_search(
    query: str,
    entity_type: str | None = None,
    limit: int = 10,
) -> list[dict]:
    """Semantic search on knowledge graph entities.

    Args:
        query: Natural language query to find entities (e.g. "postgresql", "docker networking").
        entity_type: Filter by type (person/project/technology/concept/file/config/error/location/organization).
        limit: Max results (default 10).
    """
    embedding = await embed_text(query)
    return await queries.search_entities(
        embedding=embedding,
        entity_type=entity_type,
        limit=limit,
    )


# ──────────────────────────────────────────────
# Tool: entity_graph
# ──────────────────────────────────────────────
@mcp.tool()
async def entity_graph(
    entity_name: str,
    depth: int = 2,
) -> dict:
    """Traverse the knowledge graph from a named entity.

    Returns connected entities and relationships up to the specified depth.

    Args:
        entity_name: Name of the entity to start from (e.g. "nobrainr", "PostgreSQL").
        depth: How many hops to traverse (default 2, max 5).
    """
    depth = min(depth, 5)
    return await queries.get_entity_graph(entity_name, depth=depth)


# ──────────────────────────────────────────────
# Tool: memory_maintenance
# ──────────────────────────────────────────────
@mcp.tool()
async def memory_maintenance() -> dict:
    """Run periodic intelligence maintenance tasks.

    - Recomputes importance scores for all memories
    - Decays stability for stale memories (not accessed in 7+ days)

    Call this periodically (e.g. daily) to keep relevance scoring fresh.
    """
    importance_count = await queries.recompute_importance()
    decay_count = await queries.decay_stability()
    return {
        "status": "done",
        "importance_recomputed": importance_count,
        "stability_decayed": decay_count,
    }


# ──────────────────────────────────────────────
# Tool: memory_extract
# ──────────────────────────────────────────────
@mcp.tool()
async def memory_extract(memory_id: str) -> dict:
    """Manually trigger entity extraction for a specific memory.

    Args:
        memory_id: The UUID of the memory to extract entities from.
    """
    memory = await queries.get_memory(memory_id)
    if not memory:
        return {"status": "not_found", "id": memory_id}

    from nobrainr.extraction.pipeline import process_memory
    await process_memory(memory_id, memory["content"], memory.get("tags"))
    return {"status": "extracted", "id": memory_id}


# ──────────────────────────────────────────────
# Import tools (kept for backwards compat)
# ──────────────────────────────────────────────
@mcp.tool()
async def memory_import_chatgpt(file_path: str, distill: bool = False) -> dict:
    """Import ChatGPT conversations from an OpenAI export file.

    Args:
        file_path: Path to conversations.json from ChatGPT export.
        distill: If true, also extract key learnings into memories (slower).
    """
    from nobrainr.importers.chatgpt import import_chatgpt_export
    return await import_chatgpt_export(file_path, distill=distill)


@mcp.tool()
async def memory_import_claude(directory: str, machine_name: str | None = None) -> dict:
    """Import Claude memory files from a .claude directory.

    Args:
        directory: Path to the .claude directory (e.g. /root/.claude).
        machine_name: Name of the machine this came from.
    """
    from nobrainr.importers.claude import import_claude_memory
    return await import_claude_memory(directory, machine_name=machine_name)


# ──────────────────────────────────────────────
# Entry points
# ──────────────────────────────────────────────

def main():
    """Entry point — run as parent ASGI app with dashboard + MCP."""
    import uvicorn
    from nobrainr.dashboard.app import create_app

    app = create_app()
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
