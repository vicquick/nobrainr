"""API endpoints — pure JSON responses + SSE stream."""

import base64
import logging
import time
from collections import defaultdict
from datetime import datetime, timezone
from uuid import UUID

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from nobrainr.config import settings
from nobrainr.db import queries
from nobrainr.embeddings.ollama import embed_text
from nobrainr.events import subscribe

log = logging.getLogger(__name__)

# Simple in-memory rate limiters
_chat_rate: dict[str, list[float]] = defaultdict(list)
_CHAT_RATE_LIMIT = 10  # requests per minute
_CHAT_RATE_WINDOW = 60.0  # seconds

_transcribe_rate: dict[str, list[float]] = defaultdict(list)
_TRANSCRIBE_RATE_LIMIT = 10  # requests per minute
_TRANSCRIBE_RATE_WINDOW = 60.0  # seconds


def _valid_uuid(value: str) -> bool:
    try:
        UUID(value)
        return True
    except ValueError:
        return False


async def api_graph(request: Request) -> JSONResponse:
    """Full knowledge graph with server-computed layout (Louvain + spring)."""
    try:
        min_conn = max(0, int(request.query_params.get("min_connections", "0")))
    except ValueError:
        min_conn = 0
    data = await queries.get_all_entities_for_graph(min_connections=min_conn)

    # Filter to connected nodes only (nodes with at least one edge)
    connected_only = request.query_params.get("connected_only", "true").lower() != "false"
    if connected_only:
        node_ids_in_edges: set[str] = set()
        for edge in data["edges"]:
            node_ids_in_edges.add(edge["data"]["source"])
            node_ids_in_edges.add(edge["data"]["target"])
        data["nodes"] = [n for n in data["nodes"] if n["data"]["id"] in node_ids_in_edges]

    # Compute layout server-side
    from nobrainr.layout import compute_graph_layout

    layout = compute_graph_layout(data["nodes"], data["edges"])

    # Inject positions + community into node data
    for node in data["nodes"]:
        nid = node["data"]["id"]
        if nid in layout:
            node["data"]["x"] = layout[nid]["x"]
            node["data"]["y"] = layout[nid]["y"]
            node["data"]["community"] = layout[nid]["community"]
        else:
            node["data"]["x"] = 0.0
            node["data"]["y"] = 0.0
            node["data"]["community"] = -1

    return JSONResponse(data)


async def api_memories(request: Request) -> JSONResponse:
    """Search/list memories."""
    q = request.query_params.get("q", "").strip()
    category = request.query_params.get("category") or None
    source_machine = request.query_params.get("source_machine") or None
    tags_param = request.query_params.get("tags", "").strip()
    tags = [t.strip() for t in tags_param.split(",") if t.strip()] if tags_param else None
    try:
        limit = min(int(request.query_params.get("limit", "50")), 200)
        offset = max(int(request.query_params.get("offset", "0")), 0)
    except ValueError:
        return JSONResponse({"error": "Invalid limit/offset"}, status_code=400)

    min_quality_param = request.query_params.get("min_quality", "").strip()
    min_quality: float | None = None
    if min_quality_param:
        try:
            min_quality = max(0.0, min(1.0, float(min_quality_param)))
        except ValueError:
            pass

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
        if min_quality is not None:
            memories = [m for m in memories if (m.get("quality_score") or 0) >= min_quality]
    else:
        memories = await queries.query_memories(
            tags=tags,
            category=category,
            source_machine=source_machine,
            min_quality=min_quality,
            limit=limit,
            offset=offset,
        )

    return JSONResponse(memories)


async def api_memory_detail(request: Request) -> JSONResponse:
    """Single memory detail."""
    memory_id = request.path_params["memory_id"]
    if not _valid_uuid(memory_id):
        return JSONResponse({"error": "Invalid memory_id"}, status_code=400)
    memory = await queries.get_memory(memory_id)
    if not memory:
        return JSONResponse({"error": "Memory not found"}, status_code=404)

    entities = await queries.get_memory_entities(memory_id)

    return JSONResponse({"memory": memory, "entities": entities})


async def api_memory_update(request: Request) -> JSONResponse:
    """Update a memory via POST (JSON body)."""
    memory_id = request.path_params["memory_id"]
    if not _valid_uuid(memory_id):
        return JSONResponse({"error": "Invalid memory_id"}, status_code=400)
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
    if not _valid_uuid(memory_id):
        return JSONResponse({"error": "Invalid memory_id"}, status_code=400)
    deleted = await queries.delete_memory(memory_id)
    if deleted:
        return JSONResponse({"status": "deleted"})
    return JSONResponse({"error": "Memory not found"}, status_code=404)


async def api_timeline(request: Request) -> JSONResponse:
    """Timeline data — memories ordered by date."""
    category = request.query_params.get("category") or None
    source_machine = request.query_params.get("source_machine") or None
    limit = min(int(request.query_params.get("limit", "100")), 500)
    offset = max(int(request.query_params.get("offset", "0")), 0)

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
    if not _valid_uuid(entity_id):
        return JSONResponse({"error": "Invalid entity_id"}, status_code=400)
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

    events = await queries.get_scheduler_events(limit=100)
    feedback_stats = await queries.get_feedback_stats()

    # Dynamic job discovery from scheduler registry
    jobs = scheduler.get_jobs()

    # Enrich with last_run and run_count from events
    for job in jobs:
        job_events = [e for e in events if e.get("metadata", {}).get("job") == job["name"]]
        job["last_run"] = job_events[0]["created_at"] if job_events else None
        job["run_count"] = len(job_events)

    # System health stats for dashboard
    from nobrainr.db.pool import get_pool
    health = {}
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            health["total_memories"] = await conn.fetchval("SELECT COUNT(*) FROM memories")
            health["extraction_done"] = await conn.fetchval(
                "SELECT COUNT(*) FROM memories WHERE extraction_status = 'done'"
            )
            health["extraction_pending"] = await conn.fetchval(
                "SELECT COUNT(*) FROM memories WHERE extraction_status IS NULL OR extraction_status = 'failed'"
            )
            health["total_entities"] = await conn.fetchval("SELECT COUNT(*) FROM entities")
            health["total_relations"] = await conn.fetchval("SELECT COUNT(*) FROM entity_relations")
            health["undistilled"] = await conn.fetchval(
                "SELECT COUNT(*) FROM conversations_raw "
                "WHERE source_type IN ('chatgpt', 'claude_web') "
                "AND (metadata->>'distilled') IS NULL"
            )
            health["quality_scored"] = await conn.fetchval(
                "SELECT COUNT(*) FROM memories WHERE quality_score IS NOT NULL"
            )
            health["quality_unscored"] = await conn.fetchval(
                "SELECT COUNT(*) FROM memories WHERE quality_score IS NULL"
            )
    except Exception:
        pass

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
        "health": health,
    })


async def api_recall(request: Request) -> JSONResponse:
    """Fast text-only memory search (PostgreSQL full-text, no embedding call).

    Uses OR semantics so any matching word returns results, ranked by relevance.
    """
    q = request.query_params.get("q", "").strip()
    if not q:
        return JSONResponse([])

    limit = min(int(request.query_params.get("limit", "5")), 100)

    from nobrainr.db.pool import get_pool

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, content, summary, source_type, source_machine, tags, category,
                   confidence, metadata, created_at, updated_at, importance, stability,
                   quality_score,
                   ts_rank(to_tsvector('english', content), websearch_to_tsquery('english', $1)) AS rank
            FROM memories
            WHERE to_tsvector('english', content) @@ websearch_to_tsquery('english', $1)
            ORDER BY rank DESC, importance DESC
            LIMIT $2
            """,
            q,
            limit,
        )
        results = [queries._row_to_dict(row) for row in rows]
        # Track access on recalled memories
        if results:
            result_ids = [UUID(r["id"]) for r in results]
            await conn.execute(
                "UPDATE memories SET last_accessed_at = now(), access_count = access_count + 1 WHERE id = ANY($1)",
                result_ids,
            )
        return JSONResponse(results)


async def api_smart_recall(request: Request) -> JSONResponse:
    """Semantic memory search via embedding — richer than /api/recall but needs Ollama.

    Used by hooks to brief agents with contextually relevant memories.
    Falls back to /api/recall on embedding failure.
    """
    q = request.query_params.get("q", "").strip()
    if not q:
        return JSONResponse([])
    limit = min(int(request.query_params.get("limit", "5")), 20)
    try:
        embedding = await embed_text(q)
    except Exception:
        # Fallback to FTS if embedding fails
        return await api_recall(request)
    results = await queries.search_memories(
        embedding=embedding, limit=limit, threshold=0.25,
        text_query=q,  # hybrid mode: vector + FTS
    )
    return JSONResponse(results)


async def api_memory_feedback(request: Request) -> JSONResponse:
    """Record feedback on a memory (was it useful?)."""
    memory_id = request.path_params["memory_id"]
    if not _valid_uuid(memory_id):
        return JSONResponse({"error": "Invalid memory_id"}, status_code=400)
    body = await request.json()
    was_useful = body.get("was_useful", True)
    context = body.get("context")
    agent_id = body.get("agent_id")
    session_id = body.get("session_id")

    result = await queries.store_memory_outcome(
        memory_id,
        was_useful,
        context=context,
        agent_id=agent_id,
        session_id=session_id,
    )
    return JSONResponse(result)


async def api_events(request: Request) -> StreamingResponse:
    """SSE stream for real-time dashboard updates."""
    return StreamingResponse(
        subscribe(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


async def api_entities(request: Request) -> JSONResponse:
    """List entities with optional type filter."""
    entity_type = request.query_params.get("type") or None
    try:
        limit = min(int(request.query_params.get("limit", "100")), 500)
        offset = max(int(request.query_params.get("offset", "0")), 0)
    except ValueError:
        return JSONResponse({"error": "Invalid limit/offset"}, status_code=400)
    entities = await queries.list_entities(
        entity_type=entity_type,
        limit=limit,
        offset=offset,
    )
    return JSONResponse(entities)


async def api_categories(request: Request) -> JSONResponse:
    """Unique categories for filter dropdowns."""
    categories = await queries.get_categories()
    return JSONResponse(categories)


async def api_tags(request: Request) -> JSONResponse:
    """Unique tags for filter dropdowns."""
    tags = await queries.get_all_tags()
    return JSONResponse(tags)


async def api_monitoring(request: Request) -> JSONResponse:
    """Current server health: Docker containers + system resources."""
    from nobrainr.monitoring import check_docker_health, check_system_resources

    docker = await check_docker_health(track_state=False)
    resources = await check_system_resources()

    return JSONResponse({
        "docker": docker,
        "resources": resources,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    })


async def api_chat(request: Request) -> StreamingResponse | JSONResponse:
    """RAG chatbot — streams SSE tokens from Ollama with memory context."""
    if not settings.chat_enabled:
        return JSONResponse({"error": "Chat is disabled"}, status_code=503)

    # Rate limit by IP
    ip = request.client.host if request.client else "unknown"
    now = time.monotonic()
    _chat_rate[ip] = [t for t in _chat_rate[ip] if now - t < _CHAT_RATE_WINDOW]
    if len(_chat_rate[ip]) >= _CHAT_RATE_LIMIT:
        return JSONResponse({"error": "Rate limit exceeded. Try again in a minute."}, status_code=429)
    _chat_rate[ip].append(now)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    message = body.get("message", "")
    if not isinstance(message, str) or not message.strip():
        return JSONResponse({"error": "Message required"}, status_code=400)
    if len(message) > settings.chat_max_message_length:
        return JSONResponse({"error": f"Message too long (max {settings.chat_max_message_length})"}, status_code=400)

    history = body.get("history", [])
    if not isinstance(history, list):
        history = []
    history = history[-settings.chat_max_history_length:]

    # Optional base64-encoded images for multimodal (vision) support
    images_raw = body.get("images")
    images: list[str] | None = None
    if isinstance(images_raw, list):
        images = [img for img in images_raw if isinstance(img, str) and img]
        if not images:
            images = None

    # Validate images server-side
    if images:
        _MAX_IMAGES = 5
        _MAX_IMAGE_BYTES = 10 * 1024 * 1024  # 10 MB per image
        if len(images) > _MAX_IMAGES:
            return JSONResponse(
                {"error": f"Too many images ({len(images)}). Maximum is {_MAX_IMAGES}."},
                status_code=400,
            )
        for i, img in enumerate(images):
            # Strip optional data URL prefix before decoding
            raw_b64 = img.split(",", 1)[-1] if img.startswith("data:") else img
            try:
                decoded = base64.b64decode(raw_b64, validate=True)
            except (ValueError, Exception):
                return JSONResponse(
                    {"error": f"Image {i + 1} is not valid base64."},
                    status_code=400,
                )
            if len(decoded) > _MAX_IMAGE_BYTES:
                size_mb = len(decoded) / (1024 * 1024)
                return JSONResponse(
                    {"error": f"Image {i + 1} is too large ({size_mb:.1f} MB). Maximum is 10 MB per image."},
                    status_code=400,
                )

    from nobrainr.chat.rag import stream_chat_response

    return StreamingResponse(
        stream_chat_response(message, history, images=images),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def api_memory_history(request):
    """GET /api/memories/{memory_id}/history — full version audit trail."""
    memory_id = request.path_params["memory_id"]
    history = await queries.get_memory_history(memory_id)
    return JSONResponse(history)


async def api_memory_restore(request):
    """POST /api/memories/{memory_id}/restore — restore to a previous version."""
    memory_id = request.path_params["memory_id"]
    body = await request.json()
    version = body.get("version")
    if version is None:
        return JSONResponse({"error": "version is required"}, status_code=400)
    result = await queries.restore_memory_version(memory_id, int(version))
    if result is None:
        return JSONResponse({"error": "Version not found"}, status_code=404)
    return JSONResponse(result)


async def api_transcribe(request: Request) -> JSONResponse:
    """Proxy audio to Speaches (OpenAI-compatible whisper API) for transcription."""
    # Rate limit by IP
    ip = request.client.host if request.client else "unknown"
    now = time.monotonic()
    _transcribe_rate[ip] = [t for t in _transcribe_rate[ip] if now - t < _TRANSCRIBE_RATE_WINDOW]
    if len(_transcribe_rate[ip]) >= _TRANSCRIBE_RATE_LIMIT:
        return JSONResponse({"error": "Rate limit exceeded. Try again in a minute."}, status_code=429)
    _transcribe_rate[ip].append(now)

    content_type = request.headers.get("content-type", "")
    if "multipart/form-data" not in content_type:
        return JSONResponse({"error": "Multipart form data required"}, status_code=400)

    # Parse the multipart form
    form = await request.form(max_part_size=10 * 1024 * 1024)
    try:
        audio_file = form.get("file")
        if audio_file is None:
            return JSONResponse({"error": "No audio file provided"}, status_code=400)

        audio_bytes = await audio_file.read()  # type: ignore[union-attr]
        if not audio_bytes:
            return JSONResponse({"error": "Empty audio file"}, status_code=400)

        filename = getattr(audio_file, "filename", "audio.webm") or "audio.webm"
        file_content_type = getattr(audio_file, "content_type", "audio/webm") or "audio/webm"

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    f"{settings.speaches_url}/v1/audio/transcriptions",
                    files={"file": (filename, audio_bytes, file_content_type)},
                    data={"model": settings.speaches_model, "response_format": "json"},
                )
            if resp.status_code != 200:
                log.warning("Speaches transcription failed: %s %s", resp.status_code, resp.text[:200])
                return JSONResponse(
                    {"error": "Transcription service error"},
                    status_code=502,
                )
            result = resp.json()
            return JSONResponse({"text": result.get("text", "")})
        except httpx.TimeoutException:
            log.warning("Speaches transcription timed out at %s", settings.speaches_url)
            return JSONResponse(
                {"error": "Transcription timed out"},
                status_code=504,
            )
        except httpx.ConnectError:
            log.error("Cannot connect to Speaches at %s", settings.speaches_url)
            return JSONResponse(
                {"error": "Transcription service unavailable"},
                status_code=503,
            )
        except Exception:
            log.exception("Transcription proxy error")
            return JSONResponse({"error": "Transcription failed"}, status_code=500)
    finally:
        await form.close()


api_routes = [
    Route("/api/transcribe", api_transcribe, methods=["POST"]),
    Route("/api/chat", api_chat, methods=["POST"]),
    Route("/api/graph", api_graph),
    Route("/api/memories", api_memories),
    Route("/api/memories/{memory_id}", api_memory_detail, methods=["GET"]),
    Route("/api/memories/{memory_id}", api_memory_update, methods=["POST"]),
    Route("/api/memories/{memory_id}", api_memory_delete, methods=["DELETE"]),
    Route("/api/memories/{memory_id}/feedback", api_memory_feedback, methods=["POST"]),
    Route("/api/memories/{memory_id}/history", api_memory_history, methods=["GET"]),
    Route("/api/memories/{memory_id}/restore", api_memory_restore, methods=["POST"]),
    Route("/api/timeline", api_timeline),
    Route("/api/node/{entity_id}", api_node_detail),
    Route("/api/stats", api_stats),
    Route("/api/scheduler", api_scheduler),
    Route("/api/recall", api_recall),
    Route("/api/smart-recall", api_smart_recall),
    Route("/api/entities", api_entities),
    Route("/api/categories", api_categories),
    Route("/api/tags", api_tags),
    Route("/api/events", api_events),
    Route("/api/monitoring", api_monitoring),
]
