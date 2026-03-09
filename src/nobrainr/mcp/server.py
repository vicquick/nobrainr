"""nobrainr MCP server — collective agent memory with knowledge graph."""

import logging
from uuid import UUID

from mcp.server.fastmcp import FastMCP

from nobrainr.config import settings
from nobrainr.db import queries
from nobrainr.embeddings.ollama import embed_text
from nobrainr.services.memory import store_memory_with_extraction
from nobrainr.utils.categories import normalize_category


def _validate_uuid(value: str) -> str:
    """Validate and return a UUID string. Raises ValueError on invalid input."""
    UUID(value)
    return value

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("nobrainr")

# ──────────────────────────────────────────────
# FastMCP instance (no lifespan — parent app handles it)
# ──────────────────────────────────────────────

mcp = FastMCP(
    "nobrainr",
    host=settings.host,
    port=settings.port,
    stateless_http=True,
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

    category = normalize_category(category)

    return await store_memory_with_extraction(
        content=content,
        summary=summary,
        tags=tags,
        category=category,
        source_type=source_type,
        source_machine=source_machine,
        source_ref=source_ref,
        confidence=confidence,
        metadata=metadata,
    )


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
    hybrid: bool = True,
) -> list[dict]:
    """Semantic search across all memories, ranked by relevance (similarity + recency + importance).

    Uses hybrid search by default: combines vector similarity with full-text
    keyword matching via Reciprocal Rank Fusion (RRF) for best recall.
    Set hybrid=False for pure vector search only.

    Args:
        query: Natural language search query (e.g. "How did we fix the Docker networking issue?").
        limit: Max results to return (default 10).
        threshold: Minimum similarity score 0.0-1.0 (default 0.3).
        tags: Filter to memories with any of these tags.
        category: Filter to specific category.
        source_type: Filter by source ("chatgpt", "claude", "manual", "agent").
        source_machine: Filter to specific host.
        hybrid: Combine vector + full-text search via RRF (default True).
    """
    limit = max(1, min(limit, 100))
    threshold = max(0.0, min(threshold, 1.0))
    embedding = await embed_text(query)

    # Overfetch if reranking is enabled, so reranker has candidates to work with
    fetch_limit = limit * 3 if settings.reranker_enabled else limit

    results = await queries.search_memories(
        embedding=embedding,
        limit=fetch_limit,
        threshold=threshold,
        tags=tags,
        category=category,
        source_type=source_type,
        source_machine=source_machine,
        text_query=query if hybrid else None,
    )

    # Rerank with cross-encoder if enabled
    if settings.reranker_enabled and len(results) > 1:
        try:
            from nobrainr.services.reranker import rerank
            results = await rerank(query, results, limit=limit)
        except Exception:
            import logging
            logging.getLogger("nobrainr").exception("Reranker failed, using original ranking")
            results = results[:limit]
    else:
        results = results[:limit]

    # Expand chunk context: fetch adjacent chunks for continuity
    if settings.chunk_context_window > 0:
        results = await queries.expand_chunk_context(results, window=settings.chunk_context_window)

    # Record interest signal for the search query
    if settings.interest_tracking_enabled and query and len(query) > 5:
        try:
            await queries.record_interest_signal(
                topic=query[:200],
                signal_type="search",
                strength=1.0,
                source_machine=source_machine,
            )
        except Exception:
            pass

    return results


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

    # Trigger snapshots old state automatically
    return await queries.update_memory(
        memory_id,
        content=content,
        summary=summary,
        embedding=embedding,
        tags=tags,
        category=category,
        confidence=confidence,
        metadata=metadata,
        _changed_by="mcp",
        _change_type="manual_update",
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
    # Trigger snapshots old state automatically before deletion
    deleted = await queries.delete_memory(
        memory_id,
        _changed_by="mcp",
        _change_type="manual_delete",
    )
    if deleted:
        return {"status": "deleted", "id": memory_id}
    return {"status": "not_found", "id": memory_id}


# ──────────────────────────────────────────────
# Tool: memory_history
# ──────────────────────────────────────────────
@mcp.tool()
async def memory_history(memory_id: str) -> list[dict]:
    """Get the full version history of a memory (audit trail / time machine).

    Returns all recorded versions ordered newest-first.
    Each version includes: version number, change_type, content snapshot,
    tags, category, change_reason, and who made the change.

    Args:
        memory_id: The UUID of the memory.
    """
    try:
        _validate_uuid(memory_id)
    except ValueError:
        return [{"error": "Invalid memory_id format"}]
    return await queries.get_memory_history(memory_id)


# ──────────────────────────────────────────────
# Tool: memory_restore
# ──────────────────────────────────────────────
@mcp.tool()
async def memory_restore(memory_id: str, version: int) -> dict:
    """Restore a memory to a previous version from its history.

    This reverts the memory's content, tags, category, and confidence
    to the state captured in the specified version snapshot. A new
    version record is created with change_type='restore'.

    Args:
        memory_id: The UUID of the memory to restore.
        version: The version number to restore to (from memory_history).
    """
    try:
        _validate_uuid(memory_id)
    except ValueError:
        return {"error": "Invalid memory_id format"}
    result = await queries.restore_memory_version(memory_id, version)
    if result is None:
        return {"error": "Version not found or memory does not exist"}
    return result


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
# Tool: crawl_page
# ──────────────────────────────────────────────
@mcp.tool()
async def crawl_page(
    url: str,
    extract_markdown: bool = True,
    extract_links: bool = False,
    wait_for_selector: str | None = None,
) -> dict:
    """Crawl a web page and return its content as clean markdown.

    Uses a local Crawl4AI instance with headless Chromium for JS-rendered pages.

    Args:
        url: The URL to crawl.
        extract_markdown: Return cleaned markdown content (default True).
        extract_links: Include extracted links in response.
        wait_for_selector: CSS selector to wait for before extracting (for JS-heavy pages).
    """
    import httpx

    payload: dict = {
        "urls": [url],
        "cache_mode": "bypass",
        "word_count_threshold": 20,
        "excluded_tags": ["nav", "footer", "header", "aside"],
        "exclude_external_links": True,
    }
    if wait_for_selector:
        payload["wait_for"] = f"css:{wait_for_selector}"

    headers = {"Content-Type": "application/json"}
    if settings.crawl4ai_api_token:
        headers["Authorization"] = f"Bearer {settings.crawl4ai_api_token}"

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                f"{settings.crawl4ai_url}/crawl",
                json=payload,
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        return {"error": f"Crawl failed: {e}", "url": url}

    if not data.get("success") or not data.get("results"):
        return {"error": "Crawl returned no results", "url": url, "raw": str(data)[:500]}

    result = data["results"][0]
    output: dict = {
        "url": result.get("url", url),
        "status_code": result.get("status_code"),
        "title": result.get("metadata", {}).get("title"),
    }

    if extract_markdown:
        md = result.get("markdown", {})
        output["markdown"] = md.get("fit_markdown") or md.get("raw_markdown", "") if isinstance(md, dict) else str(md)

    if extract_links:
        links = result.get("links", {})
        output["links"] = {
            "internal": [link.get("href") for link in links.get("internal", [])[:50]],
            "external": [link.get("href") for link in links.get("external", [])[:50]],
        }

    return output


# ──────────────────────────────────────────────
# Tool: crawl_and_store
# ──────────────────────────────────────────────
@mcp.tool()
async def crawl_and_store(
    url: str,
    tags: list[str] | None = None,
    category: str = "documentation",
    source_machine: str | None = None,
    max_content_chars: int = 50000,
    chunked: bool = True,
) -> dict:
    """Crawl a web page and store its content as a memory in nobrainr.

    Fetches the page via Crawl4AI, extracts clean markdown, and stores it
    with embedding + entity extraction for the knowledge graph.

    Long pages are automatically split into overlapping chunks so no content
    is lost.  Set chunked=False to store as a single memory (truncated to
    max_content_chars).

    Args:
        url: The URL to crawl and store.
        tags: Tags for the stored memory.
        category: Memory category (default "documentation").
        source_machine: Which machine initiated this crawl.
        max_content_chars: Max chars to keep from the page (default 50000).
        chunked: Split long content into overlapping chunks (default True).
    """
    from nobrainr.services.memory import store_document_chunked

    # First crawl
    crawl_result = await crawl_page(url)
    if "error" in crawl_result:
        return crawl_result

    markdown = crawl_result.get("markdown", "")
    if not markdown or len(markdown.strip()) < 50:
        return {"error": "Page returned too little content", "url": url}

    title = crawl_result.get("title", url)
    content = markdown[:max_content_chars]

    all_tags = list(tags or []) + ["crawled"]
    norm_category = normalize_category(category)

    if chunked:
        store_result = await store_document_chunked(
            content=content,
            title=title,
            summary=f"Crawled: {title}"[:200],
            tags=all_tags,
            category=norm_category,
            source_type="crawl",
            source_machine=source_machine,
            source_ref=url,
        )
    else:
        # Legacy single-memory mode (truncates)
        store_result = await memory_store(
            content=content[:settings.chunk_threshold],
            summary=f"Crawled: {title}"[:200],
            tags=all_tags,
            category=norm_category,
            source_type="crawl",
            source_machine=source_machine,
            source_ref=url,
        )

    # Record interest signal for the crawled domain/topic
    if settings.interest_tracking_enabled:
        try:
            from urllib.parse import urlparse
            domain = urlparse(url).netloc
            await queries.record_interest_signal(
                topic=domain,
                signal_type="crawl",
                strength=2.0,
                source_machine=source_machine,
                metadata={"url": url, "title": title},
            )
        except Exception:
            pass

    return {
        "url": url,
        "title": title,
        "chars_total": len(content),
        "chunked": chunked,
        "result": store_result,
    }


# ──────────────────────────────────────────────
# Tool: memory_store_document (chunked ingestion)
# ──────────────────────────────────────────────
@mcp.tool()
async def memory_store_document(
    content: str,
    title: str | None = None,
    tags: list[str] | None = None,
    category: str = "documentation",
    source_type: str = "document",
    source_machine: str | None = None,
    source_ref: str | None = None,
    confidence: float = 0.8,
) -> dict:
    """Store a long document as chunked memories with overlapping context.

    For content shorter than the chunk threshold (~4000 chars), stores as a
    single memory.  For longer content, splits into overlapping chunks that
    preserve context at boundaries, links them via a shared document_id in
    metadata, and runs entity extraction on each chunk.

    Use this for architecture docs, ADRs, meeting notes, specs, or any text
    too long for a single memory_store call.

    Args:
        content: The full document text.
        title: Document title (used in chunk summaries and metadata).
        tags: Tags for all chunks.
        category: Category for all chunks (default "documentation").
        source_type: Source type (default "document").
        source_machine: Which machine generated this.
        source_ref: Reference (file path, URL, etc.).
        confidence: Confidence score (default 0.8).
    """
    from nobrainr.services.memory import store_document_chunked

    if len(content) > settings.max_content_length * 5:
        return {"error": f"Content too large ({len(content)} chars, max {settings.max_content_length * 5})"}

    return await store_document_chunked(
        content=content,
        title=title,
        tags=tags,
        category=normalize_category(category),
        source_type=source_type,
        source_machine=source_machine,
        source_ref=source_ref,
        confidence=confidence,
    )


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


@mcp.tool()
async def memory_import_claude_web(
    file_path: str, source_machine: str | None = None,
) -> dict:
    """Import Claude.ai web export (conversations.json from Settings → Export Data).

    Args:
        file_path: Path to conversations.json from Claude.ai export ZIP.
        source_machine: Machine identifier.
    """
    from pathlib import Path
    resolved = Path(file_path).resolve()
    if not resolved.is_file():
        return {"error": f"File not found: {file_path}"}
    from nobrainr.importers.claude_web import import_claude_web_export
    return await import_claude_web_export(str(resolved), source_machine=source_machine)


@mcp.tool()
async def memory_import_claude_memories(
    file_path: str, source_machine: str | None = None,
) -> dict:
    """Import Claude.ai memories.json (built-in user memory from export).

    Args:
        file_path: Path to memories.json from Claude.ai export ZIP.
        source_machine: Machine identifier.
    """
    from pathlib import Path
    resolved = Path(file_path).resolve()
    if not resolved.is_file():
        return {"error": f"File not found: {file_path}"}
    from nobrainr.importers.claude_web import import_claude_memories
    return await import_claude_memories(str(resolved), source_machine=source_machine)


@mcp.tool()
async def memory_import_claude_projects(
    file_path: str, source_machine: str | None = None,
) -> dict:
    """Import Claude.ai projects.json (project descriptions from export).

    Args:
        file_path: Path to projects.json from Claude.ai export ZIP.
        source_machine: Machine identifier.
    """
    from pathlib import Path
    resolved = Path(file_path).resolve()
    if not resolved.is_file():
        return {"error": f"File not found: {file_path}"}
    from nobrainr.importers.claude_web import import_claude_projects
    return await import_claude_projects(str(resolved), source_machine=source_machine)


@mcp.tool()
async def memory_import_sticky_notes(
    file_path: str, source_machine: str | None = None,
) -> dict:
    """Import Windows Sticky Notes from CSV export.

    Args:
        file_path: Path to stickynotes.CSV file.
        source_machine: Machine identifier (default: workpc).
    """
    from pathlib import Path
    resolved = Path(file_path).resolve()
    if not resolved.is_file():
        return {"error": f"File not found: {file_path}"}
    from nobrainr.importers.sticky_notes import import_sticky_notes
    return await import_sticky_notes(str(resolved), source_machine=source_machine)


@mcp.tool()
async def memory_import_markdown_notes(
    directory: str,
    source_type: str = "google_keep",
    source_machine: str | None = None,
) -> dict:
    """Import markdown notes with YAML frontmatter from a directory.

    Args:
        directory: Path to directory containing .md files.
        source_type: Type identifier (google_keep, affine_memos).
        source_machine: Machine identifier.
    """
    from pathlib import Path
    resolved = Path(directory).resolve()
    if not resolved.is_dir():
        return {"error": f"Directory not found: {directory}"}
    from nobrainr.importers.markdown_notes import import_markdown_notes
    return await import_markdown_notes(str(resolved), source_type=source_type, source_machine=source_machine)


@mcp.tool()
async def memory_import_docx(
    directory: str, source_machine: str | None = None,
) -> dict:
    """Import .docx files from a directory (Google Docs, Nextcloud documents).

    Args:
        directory: Path to directory containing .docx files (searched recursively).
        source_machine: Machine identifier.
    """
    from pathlib import Path
    resolved = Path(directory).resolve()
    if not resolved.is_dir():
        return {"error": f"Directory not found: {directory}"}
    from nobrainr.importers.docx_importer import import_docx_files
    return await import_docx_files(str(resolved), source_machine=source_machine)


@mcp.tool()
async def memory_import_website(
    directory: str,
    website_name: str = "my-website",
    source_machine: str | None = None,
) -> dict:
    """Import website content from PHP files.

    Args:
        directory: Path to directory containing PHP files.
        website_name: Name of the website for tagging.
        source_machine: Machine identifier.
    """
    from pathlib import Path
    resolved = Path(directory).resolve()
    if not resolved.is_dir():
        return {"error": f"Directory not found: {directory}"}
    from nobrainr.importers.website import import_website_content
    return await import_website_content(str(resolved), source_machine=source_machine, website_name=website_name)


@mcp.tool()
async def memory_import_documents(
    directory: str,
    source_machine: str | None = None,
    use_vision: bool = True,
    category: str = "documentation",
    tags: list[str] | None = None,
    recursive: bool = True,
) -> dict:
    """Import documents from a directory (PDF, images, DOCX, markdown, text).

    Extracts text from all supported file types. For scanned PDFs and images,
    uses gemma3 vision to OCR/extract content. All content is stored with
    entity extraction for the knowledge graph.

    Args:
        directory: Path to directory containing documents.
        source_machine: Machine identifier for provenance.
        use_vision: Use gemma3 vision for scanned PDFs and image files (default: True).
        category: Category for stored memories (default: "documentation").
        tags: Additional tags to apply (always includes "imported", "document").
        recursive: Search subdirectories (default: True).
    """
    from pathlib import Path as P
    resolved = P(directory).resolve()
    if not resolved.is_dir():
        return {"error": f"Directory not found: {directory}"}
    from nobrainr.importers.documents import import_documents
    return await import_documents(
        str(resolved),
        source_machine=source_machine,
        use_vision=use_vision,
        category=category,
        tags=tags,
        recursive=recursive,
    )


@mcp.tool()
async def memory_import_github(
    owner: str = "vicquick",
    repos: list[str] | None = None,
    source_machine: str | None = None,
    include_commits: bool = True,
    include_issues: bool = True,
    include_code_structure: bool = True,
    include_source_code: bool = True,
    include_closed_issues: bool = True,
) -> dict:
    """Import knowledge from GitHub repositories into the memory system.

    Fetches repo metadata, README, commit history, file structure, key config
    files, source code files, issues, and PRs — all with entity extraction
    for the knowledge graph. Requires the `gh` CLI to be installed and authenticated.

    Args:
        owner: GitHub username or organization (default: "vicquick").
        repos: Specific repo names to import (default: all repos).
        source_machine: Machine identifier for provenance.
        include_commits: Import commit history grouped by week.
        include_issues: Import issues and pull requests with comments.
        include_code_structure: Import file tree and key config files.
        include_source_code: Import actual source code files (*.py, *.ts, *.vue, etc.).
        include_closed_issues: Include closed issues and merged PRs.
    """
    from nobrainr.importers.github import import_github
    return await import_github(
        owner,
        repos=repos,
        source_machine=source_machine,
        include_commits=include_commits,
        include_issues=include_issues,
        include_code_structure=include_code_structure,
        include_source_code=include_source_code,
        include_closed_issues=include_closed_issues,
    )


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
