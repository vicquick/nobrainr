"""API endpoints — pure JSON responses + SSE stream."""

import base64
import logging
import time
from collections import defaultdict
from datetime import datetime, timezone
from uuid import UUID

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
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


_GRAPH_CACHE_PATH = "/tmp/nobrainr_graph_cache.json"


async def api_graph(request: Request) -> JSONResponse:
    """Full knowledge graph with server-computed layout (Louvain + spring).
    Uses cached layout if available — cache rebuilt after community_detection runs."""
    import os
    import time as _time

    # Serve from cache if available and not stale (max 24h)
    force_refresh = request.query_params.get("refresh", "").lower() == "true"
    if not force_refresh and os.path.exists(_GRAPH_CACHE_PATH):
        cache_age = _time.time() - os.path.getmtime(_GRAPH_CACHE_PATH)
        if cache_age < 86400:  # 24 hours max
            try:
                with open(_GRAPH_CACHE_PATH, "r") as f:
                    import json
                    cached = json.load(f)
                log.info("Serving graph from cache (%.0fs old, %d nodes)", cache_age, len(cached.get("nodes", [])))
                return JSONResponse(cached)
            except Exception:
                pass  # cache corrupt, fall through to recompute

    log.info("Computing graph layout (no cache or refresh requested)...")
    t0 = _time.monotonic()

    try:
        min_conn = max(0, int(request.query_params.get("min_connections", "1")))
    except ValueError:
        min_conn = 1

    # Cap node count for performance. Default: top 3500 by mention_count.
    # 38k+ nodes → 3.5k nodes = 10x faster layout + smooth Sigma rendering.
    try:
        max_nodes = int(request.query_params.get("max_nodes", "3500"))
    except ValueError:
        max_nodes = 3500

    data = await queries.get_all_entities_for_graph(min_connections=min_conn)

    # Cap to top N by mention_count before any layout work
    if len(data["nodes"]) > max_nodes:
        data["nodes"].sort(key=lambda n: n["data"].get("mention_count") or 0, reverse=True)
        kept_ids = {n["data"]["id"] for n in data["nodes"][:max_nodes]}
        data["nodes"] = data["nodes"][:max_nodes]
        data["edges"] = [e for e in data["edges"]
                         if e["data"]["source"] in kept_ids and e["data"]["target"] in kept_ids]

    # Filter to connected nodes only (nodes with at least one edge)
    connected_only = request.query_params.get("connected_only", "true").lower() != "false"
    if connected_only:
        node_ids_in_edges: set[str] = set()
        for edge in data["edges"]:
            node_ids_in_edges.add(edge["data"]["source"])
            node_ids_in_edges.add(edge["data"]["target"])
        data["nodes"] = [n for n in data["nodes"] if n["data"]["id"] in node_ids_in_edges]

    # Drop tiny disconnected components (< 6 nodes) — they form a ring in the layout
    import networkx as nx
    _G = nx.Graph()
    _node_set = {n["data"]["id"] for n in data["nodes"]}
    for n in data["nodes"]:
        _G.add_node(n["data"]["id"])
    for e in data["edges"]:
        s, t = e["data"]["source"], e["data"]["target"]
        if s in _node_set and t in _node_set:
            _G.add_edge(s, t)
    keep_ids: set[str] = set()
    for comp in nx.connected_components(_G):
        if len(comp) >= 6:
            keep_ids |= comp
    if keep_ids:
        data["nodes"] = [n for n in data["nodes"] if n["data"]["id"] in keep_ids]
        data["edges"] = [e for e in data["edges"]
                         if e["data"]["source"] in keep_ids and e["data"]["target"] in keep_ids]

    # Compute layout server-side. spring_layout / kamada_kawai are pure-Python
    # CPU work that blocks for 10+ minutes on the full graph — push them off
    # the event loop so the ASGI worker can keep serving requests during
    # startup warmup and manual refreshes.
    import asyncio
    from nobrainr.layout import compute_graph_layout

    layout = await asyncio.to_thread(
        compute_graph_layout, data["nodes"], data["edges"],
    )

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

    elapsed = _time.monotonic() - t0
    log.info("Graph layout computed in %.1fs (%d nodes, %d edges)", elapsed, len(data["nodes"]), len(data["edges"]))

    # Save to cache for fast subsequent loads
    try:
        import json
        with open(_GRAPH_CACHE_PATH, "w") as f:
            json.dump(data, f)
        log.info("Graph cache saved to %s", _GRAPH_CACHE_PATH)
    except Exception:
        log.warning("Failed to save graph cache", exc_info=True)

    return JSONResponse(data)


_COMMUNITY_CACHE_PATH = "/tmp/nobrainr_community_graph_cache.json"


async def api_graph_communities(request: Request) -> JSONResponse:
    """Community-level meta-graph — each node is a community bubble.
    Returns positions computed from entity centroids, inter-community edges,
    and community metadata (title, summary, member_count, top entities)."""
    import os
    import time as _time
    import json as _json

    # Serve from cache if available (max 6h — matches community_detection interval)
    if os.path.exists(_COMMUNITY_CACHE_PATH):
        cache_age = _time.time() - os.path.getmtime(_COMMUNITY_CACHE_PATH)
        if cache_age < 21600:
            try:
                with open(_COMMUNITY_CACHE_PATH, "r") as f:
                    return JSONResponse(_json.load(f))
            except Exception:
                pass

    # Build from full graph cache + community summaries
    from nobrainr.db.pool import get_pool
    pool = await get_pool()

    # Get community summaries
    async with pool.acquire() as conn:
        summaries = await conn.fetch(
            "SELECT community_id, title, summary, member_count, key_topics "
            "FROM community_summaries ORDER BY member_count DESC"
        )
        summary_map = {}
        for s in summaries:
            summary_map[s["community_id"]] = {
                "title": s["title"] or f"Community {s['community_id']}",
                "summary": s["summary"] or "",
                "member_count": s["member_count"],
                "key_topics": s["key_topics"] if s["key_topics"] else [],
            }

        # Get top entities per community (top 5 by mention_count)
        top_entities = await conn.fetch("""
            SELECT e.community_id, e.canonical_name, e.entity_type, e.mention_count
            FROM entities e
            WHERE e.community_id IS NOT NULL
            ORDER BY e.community_id, e.mention_count DESC
        """)
        community_top = {}
        for row in top_entities:
            c = row["community_id"]
            if c not in community_top:
                community_top[c] = []
            if len(community_top[c]) < 5:
                community_top[c].append({
                    "name": row["canonical_name"],
                    "type": row["entity_type"],
                    "mentions": row["mention_count"],
                })

    # Compute community centroids from the full graph cache
    if not os.path.exists(_GRAPH_CACHE_PATH):
        return JSONResponse({"nodes": [], "edges": [], "error": "Graph cache not ready"})

    with open(_GRAPH_CACHE_PATH, "r") as f:
        full_graph = _json.load(f)

    # Group nodes by community, compute centroids
    from collections import defaultdict
    community_nodes = defaultdict(list)
    for node in full_graph.get("nodes", []):
        c = node["data"].get("community", -1)
        if c >= 0:
            community_nodes[c].append(node["data"])

    # Build community meta-nodes
    TYPE_COLORS = {
        "person": "#7b8ec8", "project": "#6ba87a", "technology": "#9585c4",
        "concept": "#c4a46a", "file": "#7a8290", "config": "#b09060",
        "error": "#c46b6b", "location": "#6b9e8f", "organization": "#7d92b0",
    }
    nodes = []
    for c_id, members in community_nodes.items():
        if len(members) < 3:
            continue
        # Centroid position
        cx = sum(m["x"] for m in members) / len(members)
        cy = sum(m["y"] for m in members) / len(members)
        # Dominant type
        type_counts = defaultdict(int)
        for m in members:
            type_counts[m.get("type", "concept")] += 1
        dominant_type = max(type_counts, key=type_counts.get)

        meta = summary_map.get(c_id, {})
        title = meta.get("title", f"Cluster {c_id}")
        top = community_top.get(c_id, [])

        nodes.append({
            "data": {
                "id": f"c{c_id}",
                "community_id": c_id,
                "label": title,
                "x": cx,
                "y": cy,
                "size": len(members),
                "type": dominant_type,
                "color": TYPE_COLORS.get(dominant_type, "#6b7280"),
                "summary": meta.get("summary", ""),
                "member_count": len(members),
                "top_entities": top,
                "key_topics": meta.get("key_topics", []),
            }
        })

    # Build inter-community edges from the full graph edges
    edge_weights = defaultdict(int)
    node_community = {}
    for node in full_graph.get("nodes", []):
        c = node["data"].get("community", -1)
        if c >= 0:
            node_community[node["data"]["id"]] = c
    for edge in full_graph.get("edges", []):
        sc = node_community.get(edge["data"]["source"], -1)
        tc = node_community.get(edge["data"]["target"], -1)
        if sc >= 0 and tc >= 0 and sc != tc:
            pair = (min(sc, tc), max(sc, tc))
            edge_weights[pair] += 1

    edges = []
    for (c1, c2), weight in edge_weights.items():
        if weight >= 3:  # only show meaningful inter-community connections
            edges.append({
                "data": {
                    "id": f"e-c{c1}-c{c2}",
                    "source": f"c{c1}",
                    "target": f"c{c2}",
                    "weight": weight,
                }
            })

    result = {"nodes": nodes, "edges": edges}

    # Cache
    try:
        with open(_COMMUNITY_CACHE_PATH, "w") as f:
            _json.dump(result, f)
    except Exception:
        pass

    return JSONResponse(result)


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
        # Hybrid search (vector + BM25 RRF) + cross-encoder rerank, matching
        # the MCP memory_search code path. The dashboard previously did
        # vector-only with no rerank, so the same query against the API
        # returned worse results than against MCP — confusing for any agent
        # comparing the two.
        memories = await queries.search_memories(
            embedding=embedding,
            limit=limit,
            threshold=0.2,
            tags=tags,
            category=category,
            source_machine=source_machine,
            text_query=q,
        )
        if min_quality is not None:
            memories = [m for m in memories if (m.get("quality_score") or 0) >= min_quality]
        if memories and settings.reranker_enabled:
            try:
                from nobrainr.services.reranker import rerank
                memories = await rerank(q, memories, limit=limit)
            except Exception:
                log.exception("Reranker failed in dashboard search; returning unranked")
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
    """Record feedback on a memory (was it useful?).

    v6 (2026-04-11): accepts optional query_trace_id / result_rank / query_text
    so dashboard-submitted feedback can carry the same search-context signal
    that MCP-submitted feedback does.
    """
    memory_id = request.path_params["memory_id"]
    if not _valid_uuid(memory_id):
        return JSONResponse({"error": "Invalid memory_id"}, status_code=400)
    body = await request.json()
    was_useful = body.get("was_useful", True)
    context = body.get("context")
    agent_id = body.get("agent_id")
    session_id = body.get("session_id")
    query_trace_id = body.get("query_trace_id")
    query_text = body.get("query_text")
    result_rank = body.get("result_rank")
    if isinstance(result_rank, str):
        try:
            result_rank = int(result_rank)
        except ValueError:
            result_rank = None

    result = await queries.store_memory_outcome(
        memory_id,
        was_useful,
        context=context,
        agent_id=agent_id,
        session_id=session_id,
        query_trace_id=query_trace_id,
        query_text=query_text,
        result_rank=result_rank,
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


async def api_tts(request: Request) -> Response:
    """POST /api/tts — proxy text-to-speech via Speaches (OpenAI-compatible)."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    text = body.get("text", "").strip()
    if not text:
        return JSONResponse({"error": "No text provided"}, status_code=400)
    if len(text) > 5000:
        text = text[:5000]

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{settings.speaches_url}/v1/audio/speech",
                json={
                    "model": settings.speaches_tts_model,
                    "input": text,
                    "voice": body.get("voice", settings.speaches_tts_voice),
                    "response_format": "mp3",
                },
            )
        if resp.status_code != 200:
            log.warning("Speaches TTS failed: %s %s", resp.status_code, resp.text[:200])
            return JSONResponse({"error": "TTS service error"}, status_code=502)

        return Response(
            content=resp.content,
            media_type="audio/mpeg",
            headers={"Content-Disposition": "inline"},
        )
    except httpx.TimeoutException:
        return JSONResponse({"error": "TTS timed out"}, status_code=504)
    except httpx.ConnectError:
        log.error("Cannot connect to Speaches TTS at %s", settings.speaches_url)
        return JSONResponse({"error": "TTS service unavailable"}, status_code=503)
    except Exception:
        log.exception("TTS proxy error")
        return JSONResponse({"error": "TTS failed"}, status_code=500)


async def api_memory_facts(request: Request) -> JSONResponse:
    """GET /api/memories/{memory_id}/facts — atomic facts extracted from this memory."""
    memory_id = request.path_params["memory_id"]
    try:
        facts = await queries.get_memory_facts(memory_id)
        return JSONResponse({"facts": facts})
    except Exception:
        return JSONResponse({"facts": []})


async def api_facts_search(request: Request) -> JSONResponse:
    """GET /api/facts — search atomic facts by query."""
    q = request.query_params.get("q", "").strip()
    if not q:
        return JSONResponse({"facts": [], "query": ""})
    try:
        from nobrainr.embeddings.ollama import embed_text
        embedding = await embed_text(q)
        facts = await queries.search_facts(
            embedding=embedding, limit=15, threshold=0.3, text_query=q,
        )
        return JSONResponse({"facts": facts, "query": q})
    except Exception:
        log.exception("Fact search failed")
        return JSONResponse({"facts": [], "query": q, "error": "search failed"})


async def api_facts_stats(request: Request) -> JSONResponse:
    """GET /api/facts/stats — fact extraction progress."""
    try:
        from nobrainr.db.pool import get_pool
        pool = await get_pool()
        async with pool.acquire() as conn:
            total = await conn.fetchval("SELECT COUNT(*) FROM memory_facts")
            real = await conn.fetchval("SELECT COUNT(*) FROM memory_facts WHERE LENGTH(content) > 30")
            memories_with = await conn.fetchval("SELECT COUNT(DISTINCT memory_id) FROM memory_facts")
            memories_total = await conn.fetchval("SELECT COUNT(*) FROM memories WHERE tier < 3")
        return JSONResponse({
            "total_facts": real,
            "total_markers": total,
            "memories_processed": memories_with,
            "memories_total": memories_total,
            "coverage_pct": round(memories_with / max(memories_total, 1) * 100, 1),
        })
    except Exception:
        return JSONResponse({"total_facts": 0, "coverage_pct": 0})


_GALAXY_CACHE_PATH = "/tmp/nobrainr_galaxy_cache.json"


async def api_galaxy(request: Request) -> JSONResponse:
    """3D galaxy visualization data — PCA-reduced embeddings for all memories.

    Returns flat arrays for efficient Three.js InstancedBufferGeometry consumption.
    Caches the PCA result for 1 hour.
    """
    import json
    import os
    import time as _time

    # Serve from cache if available and fresh (default 30min).
    # Both graph and galaxy are pre-warmed on startup so the first
    # dashboard load never triggers a slow UMAP computation.
    cache_ttl = 1800  # 30 minutes
    force = request.query_params.get("refresh", "").lower() == "true"
    if not force and os.path.exists(_GALAXY_CACHE_PATH):
        age = _time.time() - os.path.getmtime(_GALAXY_CACHE_PATH)
        if age < cache_ttl:
            with open(_GALAXY_CACHE_PATH) as f:
                return JSONResponse(json.load(f))

    from nobrainr.db.pool import get_pool

    pool = await get_pool()
    # Limit to manageable size for UMAP — sample by importance
    limit = int(request.query_params.get("limit", "10000"))
    limit = min(limit, 50000)

    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, embedding::float4[]::float8[] as emb, category, tier,
                   COALESCE(summary, LEFT(content, 80)) as summary,
                   source_type, importance
            FROM memories
            WHERE embedding IS NOT NULL
            ORDER BY COALESCE(importance, 0.5) DESC, created_at DESC
            LIMIT $1
        """, limit)

    if not rows:
        return JSONResponse({"count": 0, "positions": [], "categories": [], "ids": []})

    import numpy as np

    count = len(rows)
    dim = len(rows[0]["emb"])
    matrix = np.zeros((count, dim), dtype=np.float32)
    categories = []
    tiers = []
    ids = []
    summaries = []
    importances = []

    for i, row in enumerate(rows):
        emb = row["emb"]
        if emb and len(emb) == dim:
            matrix[i] = emb
        categories.append(row["category"] or "other")
        tiers.append(row["tier"] if row["tier"] is not None else 2)
        ids.append(str(row["id"]))
        summaries.append(row["summary"] or "")
        importances.append(float(row["importance"]) if row["importance"] else 0.5)

    # Dimensionality reduction: PCA → 50d → UMAP → 3d
    log.info("Galaxy: reducing %d points from %dd to 3d...", count, dim)

    # Step 1: PCA to 50d (fast, removes noise dimensions)
    from sklearn.decomposition import PCA as SkPCA

    pca_dim = min(50, dim, count - 1)
    pca = SkPCA(n_components=pca_dim)
    pca50 = pca.fit_transform(matrix)
    log.info("Galaxy: PCA %dd → %dd (%.1f%% variance)", dim, pca_dim,
             pca.explained_variance_ratio_.sum() * 100)

    # Step 2: UMAP to 3d (preserves local + global structure)
    try:
        from umap import UMAP
        reducer = UMAP(
            n_components=3,
            n_neighbors=15,
            min_dist=0.1,
            metric="euclidean",  # faster than cosine on PCA-reduced data
            random_state=42,
            n_epochs=200,  # default 500, trade some quality for speed
            n_jobs=-1,  # use all CPU cores
            low_memory=True,
        )
        coords = reducer.fit_transform(pca50)
        log.info("Galaxy: UMAP → 3d complete")
    except ImportError:
        log.warning("Galaxy: umap-learn not installed, falling back to PCA-only 3d")
        pca3 = SkPCA(n_components=3)
        coords = pca3.fit_transform(pca50)
    except Exception as exc:
        log.warning("Galaxy: UMAP failed (%s), falling back to PCA-only 3d", exc)
        pca3 = SkPCA(n_components=3)
        coords = pca3.fit_transform(pca50)

    # Normalize to [-1, 1] range
    max_abs = np.abs(coords).max()
    if max_abs > 0:
        coords = (coords / max_abs).astype(np.float32)

    # Flatten for JSON transfer
    positions = coords.flatten().tolist()

    result = {
        "count": count,
        "positions": positions,
        "categories": categories,
        "tiers": tiers,
        "ids": ids,
        "summaries": summaries,
        "importances": importances,
    }

    # Cache
    try:
        with open(_GALAXY_CACHE_PATH, "w") as f:
            json.dump(result, f)
    except Exception:
        pass

    return JSONResponse(result)


api_routes = [
    Route("/api/transcribe", api_transcribe, methods=["POST"]),
    Route("/api/tts", api_tts, methods=["POST"]),
    Route("/api/chat", api_chat, methods=["POST"]),
    Route("/api/galaxy", api_galaxy),
    Route("/api/graph", api_graph),
    Route("/api/graph/communities", api_graph_communities),
    Route("/api/memories", api_memories),
    Route("/api/memories/{memory_id}", api_memory_detail, methods=["GET"]),
    Route("/api/memories/{memory_id}", api_memory_update, methods=["POST"]),
    Route("/api/memories/{memory_id}", api_memory_delete, methods=["DELETE"]),
    Route("/api/memories/{memory_id}/feedback", api_memory_feedback, methods=["POST"]),
    Route("/api/memories/{memory_id}/history", api_memory_history, methods=["GET"]),
    Route("/api/memories/{memory_id}/facts", api_memory_facts, methods=["GET"]),
    Route("/api/memories/{memory_id}/restore", api_memory_restore, methods=["POST"]),
    Route("/api/facts", api_facts_search),
    Route("/api/facts/stats", api_facts_stats),
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
