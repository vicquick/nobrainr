"""nobrainr MCP server — collective agent memory with knowledge graph."""

import asyncio
import logging
from uuid import UUID

from mcp.server.fastmcp import FastMCP

from nobrainr.config import settings
from nobrainr.db import queries
from nobrainr.embeddings.ollama import embed_text
from nobrainr.utils.categories import normalize_category


def _validate_uuid(value: str) -> str:
    """Validate and return a UUID string. Raises ValueError on invalid input."""
    UUID(value)
    return value

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("nobrainr")

# Rate-limit entity extraction: max 1 concurrent, 30s cooldown between
_extraction_semaphore = asyncio.Semaphore(1)

# ──────────────────────────────────────────────
# FastMCP instance (no lifespan — parent app handles it)
# ──────────────────────────────────────────────

mcp = FastMCP(
    "nobrainr",
    host=settings.host,
    port=settings.port,
    instructions=(
        "nobrainr is a self-improving collective memory service for AI agents with a knowledge graph.\n\n"
        "## Core workflow\n"
        "1. ALWAYS call `memory_search` before starting any task — check what's already known. "
        "This is CRITICAL: past sessions may have solved the same problem, established conventions, "
        "or documented gotchas. Searching first prevents duplicate work and repeated mistakes.\n"
        "2. Use `memory_store` to save learnings, decisions, patterns, and context.\n"
        "3. Call `memory_feedback` after using search results — report if they were helpful.\n"
        "4. Call `memory_reflect` at session end with a batch of learnings from the session.\n"
        "5. Use `log_event` to record significant agent activity (session starts, decisions, completions).\n\n"
        "## Search & retrieval\n"
        "- `memory_search` — semantic search, relevance-ranked (similarity + recency + importance + access). "
        "Set `hybrid=True` to combine vector + text search for better results.\n"
        "- `memory_query` — structured filtering by tags, category, source.\n"
        "- `entity_search` / `entity_graph` — knowledge graph exploration.\n\n"
        "## Best practices\n"
        "- Always tag memories well so they can be found later.\n"
        "- Set `source_machine` to identify which host generated the memory.\n"
        "- Use canonical categories: architecture, debugging, deployment, infrastructure, patterns, "
        "tooling, security, frontend, backend, data, business, documentation, session-log, insight.\n"
        "- Feedback improves future search ranking — always report usefulness.\n"
        "- Maintenance runs automatically; `memory_maintenance` is available for manual runs."
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
        source_machine: Which host generated this (e.g. "my-server", "laptop").
        source_ref: Reference to original source (conversation ID, file path, etc.).
        confidence: How reliable is this knowledge (0.0-1.0, default 1.0).
        metadata: Any additional structured data.
    """
    if len(content) > settings.max_content_length:
        return {"error": f"Content too large ({len(content)} chars, max {settings.max_content_length})"}

    confidence = max(0.0, min(confidence, 1.0))
    category = normalize_category(category)

    # Context-enriched embedding: prepend category + tags for better retrieval
    embed_parts = []
    if category:
        embed_parts.append(category)
    if tags:
        embed_parts.append(", ".join(tags))
    if embed_parts:
        embed_input = ". ".join(embed_parts) + ". " + content
    else:
        embed_input = content
    embedding = await embed_text(embed_input)

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

    # Fire-and-forget entity extraction (rate-limited: 1 at a time, 30s cooldown)
    if settings.extraction_enabled:
        async def _rate_limited_extract(mem_id, mem_content, mem_tags):
            async with _extraction_semaphore:
                try:
                    from nobrainr.extraction.pipeline import process_memory
                    await process_memory(mem_id, mem_content, mem_tags)
                except Exception:
                    logger.exception("Extraction failed for %s", mem_id)
                await asyncio.sleep(30)

        try:
            asyncio.create_task(_rate_limited_extract(result["id"], content, tags))
        except Exception:
            logger.exception("Failed to start extraction task")

    return {"status": "stored", **result}


# ──────────────────────────────────────────────
# Tool: memory_search
# ──────────────────────────────────────────────
@mcp.tool()
async def memory_search(
    query: str,
    limit: int = settings.default_search_limit,
    threshold: float = settings.default_similarity_threshold,
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
        source_machine: Filter to specific host.
        hybrid: Also apply text search on the query for hybrid results.
    """
    limit = max(1, min(limit, 100))
    threshold = max(0.0, min(threshold, 1.0))
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
        source_machine: Filter to specific host.
        text_query: Full-text search on content.
        limit: Max results (default 50).
        offset: Pagination offset.
    """
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
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
    try:
        _validate_uuid(memory_id)
    except ValueError:
        return {"error": "Invalid memory_id format"}
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
    try:
        _validate_uuid(memory_id)
    except ValueError:
        return {"error": "Invalid memory_id format"}
    category = normalize_category(category)
    embedding = None
    if content is not None:
        embed_parts = []
        if category:
            embed_parts.append(category)
        if tags:
            embed_parts.append(", ".join(tags))
        if embed_parts:
            embed_input = ". ".join(embed_parts) + ". " + content
        else:
            embed_input = content
        embedding = await embed_text(embed_input)

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
    try:
        _validate_uuid(memory_id)
    except ValueError:
        return {"error": "Invalid memory_id format"}
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
# Tool: entity_list
# ──────────────────────────────────────────────
@mcp.tool()
async def entity_list(
    entity_type: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    """List entities in the knowledge graph, optionally filtered by type.

    Args:
        entity_type: Filter by type (person/project/technology/concept/service/database/etc).
        limit: Max results (default 100).
        offset: Pagination offset.
    """
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    return await queries.list_entities(
        entity_type=entity_type,
        limit=limit,
        offset=offset,
    )


# ──────────────────────────────────────────────
# Tool: entity_memories
# ──────────────────────────────────────────────
@mcp.tool()
async def entity_memories(entity_id: str) -> list[dict]:
    """Get all memories linked to a specific entity.

    Args:
        entity_id: The UUID of the entity.
    """
    try:
        _validate_uuid(entity_id)
    except ValueError:
        return [{"error": "Invalid entity_id format"}]
    return await queries.get_entity_memories(entity_id)


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
    try:
        _validate_uuid(memory_id)
    except ValueError:
        return {"error": "Invalid memory_id format"}
    memory = await queries.get_memory(memory_id)
    if not memory:
        return {"status": "not_found", "id": memory_id}

    from nobrainr.extraction.pipeline import process_memory
    await process_memory(memory_id, memory["content"], memory.get("tags"))
    return {"status": "extracted", "id": memory_id}


# ──────────────────────────────────────────────
# Tool: memory_feedback
# ──────────────────────────────────────────────
@mcp.tool()
async def memory_feedback(
    memory_id: str,
    was_useful: bool,
    context: str | None = None,
) -> dict:
    """Report whether a memory search result was useful. This feedback improves future search ranking.

    Call this after using results from memory_search to close the feedback loop.

    Args:
        memory_id: The UUID of the memory to give feedback on.
        was_useful: True if the memory was helpful, False if not.
        context: Optional context about how/why it was or wasn't useful.
    """
    try:
        _validate_uuid(memory_id)
    except ValueError:
        return {"error": "Invalid memory_id format"}
    result = await queries.store_memory_outcome(
        memory_id, was_useful, context=context,
    )
    return {"status": "recorded", **result}


# ──────────────────────────────────────────────
# Tool: memory_reflect
# ──────────────────────────────────────────────
@mcp.tool()
async def memory_reflect(
    learnings: list[dict],
) -> dict:
    """Batch-store session learnings. More efficient than individual memory_store calls.

    Call this at session end to capture what was learned. Each entry goes through
    the full pipeline (embedding, dedup check, entity extraction).

    Args:
        learnings: List of learning entries. Each dict should have:
            - content (str, required): The learning/insight to store.
            - summary (str, optional): One-line summary.
            - tags (list[str], optional): Tags for categorization.
            - category (str, optional): High-level category.
            - source_type (str, optional): Defaults to "agent".
            - source_machine (str, optional): Which host generated this.
    """
    results = []
    for entry in learnings:
        content = entry.get("content")
        if not content:
            results.append({"status": "skipped", "reason": "no content"})
            continue
        try:
            result = await memory_store(
                content=content,
                summary=entry.get("summary"),
                tags=entry.get("tags"),
                category=entry.get("category"),
                source_type=entry.get("source_type", "agent"),
                source_machine=entry.get("source_machine"),
            )
            results.append(result)
        except Exception as e:
            logger.exception("Failed to store learning: %s", content[:80])
            results.append({"status": "error", "error": str(e)})
    stored = sum(1 for r in results if r.get("status") in ("stored", "merged"))
    return {"total": len(learnings), "stored": stored, "results": results}


# ──────────────────────────────────────────────
# Tool: log_event
# ──────────────────────────────────────────────
@mcp.tool()
async def log_event(
    event_type: str,
    description: str,
    agent_id: str | None = None,
    session_id: str | None = None,
    category: str | None = None,
    related_memory_ids: list[str] | None = None,
    metadata: dict | None = None,
) -> dict:
    """Log an agent activity event for tracking and analytics.

    Use this to record session starts, task completions, important decisions, errors, etc.

    Args:
        event_type: Type of event (e.g. "session_start", "task_complete", "decision", "error").
        description: Human-readable description of what happened.
        agent_id: Identifier for the agent logging the event.
        session_id: Current session identifier.
        category: Event category for filtering.
        related_memory_ids: UUIDs of related memories.
        metadata: Additional structured data.
    """
    if related_memory_ids:
        try:
            for mid in related_memory_ids:
                _validate_uuid(mid)
        except ValueError:
            return {"error": "Invalid UUID in related_memory_ids"}
    result = await queries.log_agent_event(
        event_type=event_type,
        description=description,
        agent_id=agent_id,
        session_id=session_id,
        category=category,
        related_memory_ids=related_memory_ids,
        metadata=metadata,
    )
    return {"status": "logged", **result}


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
    from pathlib import Path
    resolved = Path(file_path).resolve()
    if not resolved.is_file():
        return {"error": f"File not found: {file_path}"}
    from nobrainr.importers.chatgpt import import_chatgpt_export
    return await import_chatgpt_export(str(resolved), distill=distill)


@mcp.tool()
async def memory_import_claude(directory: str, machine_name: str | None = None) -> dict:
    """Import Claude memory files from a .claude directory.

    Args:
        directory: Path to the .claude directory (e.g. /root/.claude).
        machine_name: Name of the machine this came from.
    """
    from pathlib import Path
    resolved = Path(directory).resolve()
    if not resolved.is_dir():
        return {"error": f"Directory not found: {directory}"}
    from nobrainr.importers.claude import import_claude_memory
    return await import_claude_memory(str(resolved), machine_name=machine_name)


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
