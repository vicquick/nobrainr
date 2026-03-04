"""API endpoints — JSON + HTMX fragment responses."""

import json
from pathlib import Path

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route
from jinja2 import Environment, FileSystemLoader

from nobrainr.db import queries
from nobrainr.embeddings.ollama import embed_text

TEMPLATE_DIR = Path(__file__).parent / "templates"
jinja_env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)), autoescape=True)


async def api_graph(request: Request) -> JSONResponse:
    """Full knowledge graph data for Cytoscape.js."""
    data = await queries.get_all_entities_for_graph()
    return JSONResponse(data)


async def api_memories(request: Request) -> HTMLResponse | JSONResponse:
    """Search/list memories. Returns HTMX fragment if HX-Request header present."""
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

    # HTMX request → return HTML fragment
    if request.headers.get("HX-Request"):
        template = jinja_env.get_template("partials/memory_list.html")
        return HTMLResponse(template.render(memories=memories, query=q))

    return JSONResponse(memories)


async def api_memory_detail(request: Request) -> HTMLResponse | JSONResponse:
    """Single memory detail."""
    memory_id = request.path_params["memory_id"]
    memory = await queries.get_memory(memory_id)
    if not memory:
        return Response(status_code=404, content="Memory not found")

    # Get linked entities
    from nobrainr.db.pool import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        entity_rows = await conn.fetch(
            """
            SELECT e.id, e.name, e.entity_type, em.role, em.confidence
            FROM entities e
            JOIN entity_memories em ON em.entity_id = e.id
            WHERE em.memory_id = $1
            ORDER BY em.confidence DESC
            """,
            __import__("uuid").UUID(memory_id),
        )
    entities = [dict(r) for r in entity_rows]
    for e in entities:
        e["id"] = str(e["id"])

    if request.headers.get("HX-Request"):
        template = jinja_env.get_template("partials/memory_detail.html")
        return HTMLResponse(template.render(memory=memory, entities=entities))

    return JSONResponse({"memory": memory, "entities": entities})


async def api_memory_update(request: Request) -> HTMLResponse | JSONResponse:
    """Update a memory via POST."""
    memory_id = request.path_params["memory_id"]
    form = await request.form()

    content = form.get("content")
    summary = form.get("summary")
    category = form.get("category")
    tags_str = form.get("tags", "")
    tags = [t.strip() for t in tags_str.split(",") if t.strip()] if tags_str else None

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
        return Response(status_code=404, content="Memory not found")

    if request.headers.get("HX-Request"):
        template = jinja_env.get_template("partials/memory_detail.html")
        return HTMLResponse(template.render(memory=updated, entities=[]))

    return JSONResponse(updated)


async def api_memory_delete(request: Request) -> Response:
    """Delete a memory."""
    memory_id = request.path_params["memory_id"]
    deleted = await queries.delete_memory(memory_id)
    if deleted:
        if request.headers.get("HX-Request"):
            return HTMLResponse("")
        return JSONResponse({"status": "deleted"})
    return Response(status_code=404, content="Memory not found")


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


async def api_node_detail(request: Request) -> HTMLResponse | JSONResponse:
    """Entity detail + connections for graph node click."""
    entity_id = request.path_params["entity_id"]
    entity = await queries.get_entity_by_id(entity_id)
    if not entity:
        return Response(status_code=404, content="Entity not found")

    connections = await queries.get_entity_connections(entity_id)
    memories = await queries.get_entity_memories(entity_id)

    if request.headers.get("HX-Request"):
        template = jinja_env.get_template("partials/node_detail.html")
        return HTMLResponse(template.render(
            entity=entity, connections=connections, memories=memories
        ))

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
    return JSONResponse({
        "scheduler_running": scheduler.running,
        "scheduler_enabled": settings.scheduler_enabled,
        "maintenance_interval_hours": settings.maintenance_interval_hours,
        "feedback_interval_hours": settings.feedback_interval_hours,
        "feedback": feedback_stats,
        "recent_events": events,
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
