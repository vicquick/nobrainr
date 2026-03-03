"""nobrainr MCP server — collective agent memory."""

import asyncio
import json
import logging
from contextlib import asynccontextmanager

from mcp.server.fastmcp import FastMCP

from nobrainr.config import settings
from nobrainr.db.pool import get_pool, close_pool
from nobrainr.db.schema import init_schema
from nobrainr.db import queries
from nobrainr.embeddings.ollama import embed_text, embed_batch, check_model

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("nobrainr")


@asynccontextmanager
async def lifespan(server: FastMCP):
    logger.info("nobrainr starting up...")
    pool = await get_pool()
    await init_schema(pool)
    model_ok = await check_model()
    if model_ok:
        logger.info(f"Embedding model '{settings.embedding_model}' ready")
    else:
        logger.warning(
            f"Embedding model '{settings.embedding_model}' not found in Ollama. "
            f"Run: ollama pull {settings.embedding_model}"
        )
    yield
    await close_pool()
    logger.info("nobrainr shut down.")


mcp = FastMCP(
    "nobrainr",
    instructions=(
        "nobrainr is a collective memory service for AI agents. "
        "Use memory_store to save learnings, decisions, patterns, and context. "
        "Use memory_search for semantic search across all stored knowledge. "
        "Use memory_query for structured filtering by tags, category, source. "
        "Always tag memories well so they can be found later."
    ),
    lifespan=lifespan,
)


# ──────────────────────────────────────────────
# Tool: memory_store
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
    """Store a new memory with automatic embedding.

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
    """Semantic search across all memories. Use natural language queries.

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
# Tool: memory_stats
# ──────────────────────────────────────────────
@mcp.tool()
async def memory_stats() -> dict:
    """Get statistics about the memory database.

    Returns counts by source, category, machine, and top tags.
    """
    return await queries.get_stats()


# ──────────────────────────────────────────────
# Tool: memory_import_chatgpt
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


# ──────────────────────────────────────────────
# Tool: memory_import_claude
# ──────────────────────────────────────────────
@mcp.tool()
async def memory_import_claude(directory: str, machine_name: str | None = None) -> dict:
    """Import Claude memory files from a .claude directory.

    Args:
        directory: Path to the .claude directory (e.g. /root/.claude).
        machine_name: Name of the machine this came from.
    """
    from nobrainr.importers.claude import import_claude_memory
    return await import_claude_memory(directory, machine_name=machine_name)


def main():
    """Entry point for the MCP server."""
    mcp.run(transport="sse", host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
