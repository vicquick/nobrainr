"""API endpoints — pure JSON responses."""

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from nobrainr.db import queries
from nobrainr.embeddings.ollama import embed_text


async def api_graph(request: Request) -> JSONResponse:
    """Full knowledge graph data for Cytoscape.js."""
    data = await queries.get_all_entities_for_graph()
    return JSONResponse(data)


async def api_memories(request: Request) -> JSONResponse:
    """Search/list memories."""
    q = request.query_params.get("q", "").strip()
    category = request.query_params.get("category") or None
    source_machine = request.query_params.get("source_machine") or None
    tags_param = request.query_params.get("tags", "").strip()
    tags = [t.strip() for t in tags_param.split(",") if t.strip()] if tags_param else None
    limit = int(request.query_params.get("limit", "50"))
    offset = int(request.query_params.get("offset", "0"))

    if q:
        embedding = await embed_text(q)
        memories = await queries.search_memories(
            embedding=embedding,
            limit=limit,
            threshold=0.2,
            tags=tags,
            category=category,
            source_machine=source_machine,
        )
    else:
        memories = await queries.query_memories(
            tags=tags,
            category=category,
            source_machine=source_machine,
            limit=limit,
            offset=offset,
        )

    return JSONResponse(memories)


async def api_memory_detail(request: Request) -> JSONResponse:
    """Single memory detail."""
    memory_id = request.path_params["memory_id"]
    memory = await queries.get_memory(memory_id)
    if not memory:
        return JSONResponse({"error": "Memory not found"}, status_code=404)

    entities = await queries.get_memory_entities(memory_id)

    return JSONResponse({"memory": memory, "entities": entities})


async def api_memory_update(request: Request) -> JSONResponse:
    """Update a memory via POST (JSON body)."""
    memory_id = request.path_params["memory_id"]
    body = await request.json()

    content = body.get("content")
    summary = body.get("summary")
    category = body.get("category")
    tags_raw = body.get("tags", "")
    if isinstance(tags_raw, list):
        tags = tags_raw if tags_raw else None
    else:
        tags = [t.strip() for t in tags_raw.split(",") if t.strip()] if tags_raw else None

    embedding = None
    if content:
        embedding = await embed_text(content)

    updated = await queries.update_memory(
        memory_id,
        content=content or None,
        summary=summary or None,
        embedding=embedding,
        tags=tags,
        category=category or None,
    )
    if not updated:
        return JSONResponse({"error": "Memory not found"}, status_code=404)

    return JSONResponse(updated)


async def api_memory_delete(request: Request) -> JSONResponse:
    """Delete a memory."""
    memory_id = request.path_params["memory_id"]
    deleted = await queries.delete_memory(memory_id)
    if deleted:
        return JSONResponse({"status": "deleted"})
    return JSONResponse({"error": "Memory not found"}, status_code=404)


async def api_timeline(request: Request) -> JSONResponse:
    """Timeline data — memories ordered by date."""
    category = request.query_params.get("category") or None
    source_machine = request.query_params.get("source_machine") or None
    limit = int(request.query_params.get("limit", "100"))
    offset = int(request.query_params.get("offset", "0"))

    memories = await queries.get_timeline_memories(
        limit=limit,
        offset=offset,
        category=category,
        source_machine=source_machine,
    )
    return JSONResponse(memories)


async def api_node_detail(request: Request) -> JSONResponse:
    """Entity detail + connections for graph node click."""
    entity_id = request.path_params["entity_id"]
    entity = await queries.get_entity_by_id(entity_id)
    if not entity:
        return JSONResponse({"error": "Entity not found"}, status_code=404)

    connections = await queries.get_entity_connections(entity_id)
    memories = await queries.get_entity_memories(entity_id)

    return JSONResponse({
        "entity": entity,
        "connections": connections,
        "memories": memories,
    })


async def api_stats(request: Request) -> JSONResponse:
    """Dashboard statistics including feedback and event counts."""
    stats = await queries.get_stats()
    feedback_stats = await queries.get_feedback_stats()
    stats.update(feedback_stats)
    return JSONResponse(stats)


async def api_scheduler(request: Request) -> JSONResponse:
    """Scheduler status, recent events, and feedback summary."""
    from nobrainr.config import settings
    from nobrainr.scheduler import scheduler

    events = await queries.get_scheduler_events(limit=50)
    feedback_stats = await queries.get_feedback_stats()

    # Build jobs list with all registered jobs
    jobs = [
        {"name": "maintenance", "interval_hours": settings.maintenance_interval_hours, "type": "sql"},
        {"name": "feedback_integration", "interval_hours": settings.feedback_interval_hours, "type": "sql"},
        {"name": "auto_summarize", "interval_hours": settings.summarize_interval_hours, "type": "llm"},
        {"name": "insight_extraction", "interval_hours": settings.insight_extraction_interval_hours, "type": "llm"},
        {"name": "entity_enrichment", "interval_hours": settings.entity_enrichment_interval_hours, "type": "llm"},
        {"name": "consolidation", "interval_hours": settings.consolidation_interval_hours, "type": "llm"},
        {"name": "synthesis", "interval_hours": settings.synthesis_interval_hours, "type": "llm"},
    ]

    # Enrich with last_run and run_count from events
    for job in jobs:
        job_events = [e for e in events if e.get("metadata", {}).get("job") == job["name"]]
        job["last_run"] = job_events[0]["created_at"] if job_events else None
        job["run_count"] = len(job_events)

    # Map feedback stats to frontend-expected shape
    total = feedback_stats.get("feedback_total", 0)
    positive = feedback_stats.get("feedback_positive", 0)
    negative = total - positive
    feedback = {
        "total": total,
        "positive": positive,
        "negative": negative,
        "positive_rate": positive / total if total > 0 else 0,
        "archived_memories": feedback_stats.get("archived_memories", 0),
        "events_24h": feedback_stats.get("events_24h", 0),
    }

    # Map events to frontend-expected shape
    mapped_events = []
    for e in events:
        mapped_events.append({
            "id": e["id"],
            "event_type": e.get("event_type", ""),
            "event_data": e.get("metadata", {}),
            "source": e.get("agent_id"),
            "created_at": e.get("created_at", ""),
        })

    return JSONResponse({
        "scheduler_running": scheduler.running,
        "scheduler_enabled": settings.scheduler_enabled,
        "maintenance_interval_hours": settings.maintenance_interval_hours,
        "feedback_interval_hours": settings.feedback_interval_hours,
        "jobs": jobs,
        "feedback": feedback,
        "recent_events": mapped_events,
    })


async def api_categories(request: Request) -> JSONResponse:
    """Unique categories for filter dropdowns."""
    categories = await queries.get_categories()
    return JSONResponse(categories)


async def api_tags(request: Request) -> JSONResponse:
    """Unique tags for filter dropdowns."""
    tags = await queries.get_all_tags()
    return JSONResponse(tags)


api_routes = [
    Route("/api/graph", api_graph),
    Route("/api/memories", api_memories),
    Route("/api/memories/{memory_id}", api_memory_detail, methods=["GET"]),
    Route("/api/memories/{memory_id}", api_memory_update, methods=["POST"]),
    Route("/api/memories/{memory_id}", api_memory_delete, methods=["DELETE"]),
    Route("/api/timeline", api_timeline),
    Route("/api/node/{entity_id}", api_node_detail),
    Route("/api/stats", api_stats),
    Route("/api/scheduler", api_scheduler),
    Route("/api/categories", api_categories),
    Route("/api/tags", api_tags),
]
