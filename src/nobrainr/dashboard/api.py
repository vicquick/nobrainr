"""API endpoints — pure JSON responses + SSE stream."""

import asyncio
import base64
import json
import logging
import time
from collections import defaultdict
from datetime import datetime, timezone
from uuid import UUID, uuid4

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from nobrainr.config import settings
from nobrainr.db import queries
from nobrainr.db import write_queue
from nobrainr.embeddings.ollama import embed_text, embed_text_with_timeout, EmbedTimeout
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


async def _log_auto_negative_outcomes(
    results: list[dict], trace_id: str, query: str,
) -> None:
    """Mirror of mcp/server.py:_log_auto_negative_outcomes for dashboard search.

    Two triggers:
      1. low_recall: fewer than auto_negative_low_recall_threshold results
         came back. Every surfaced hit gets one negative.
      2. low_rerank: top-1 rerank score below threshold. Targeted negative.

    Fire-and-forget. Never raises. Includes matched_branches in metadata so
    the feedback loop can later answer "which branch's hits are weakest?".
    """
    try:
        negatives: list[tuple[str, int, str, list]] = []
        low_recall = len(results) < settings.auto_negative_low_recall_threshold
        if low_recall:
            for r in results:
                negatives.append(
                    (r["id"], r.get("search_rank") or 1, "low_recall",
                     r.get("matched_branches") or []),
                )
        if results:
            top = results[0]
            top_rerank = top.get("rerank_score")
            if (
                top_rerank is not None
                and top_rerank < settings.auto_negative_low_rerank_threshold
            ):
                negatives.append((
                    top["id"], 1, "low_rerank",
                    top.get("matched_branches") or [],
                ))
        if not negatives:
            return
        merged: dict[tuple[str, int], tuple[list[str], list]] = {}
        for mid, rank, ctx, branches in negatives:
            cur = merged.setdefault((mid, rank), ([], branches))
            cur[0].append(ctx)
        for (mid, rank), (reasons, branches) in merged.items():
            context_str = (
                settings.auto_negative_context_prefix + ",".join(reasons)
            )
            await queries.store_memory_outcome(
                mid,
                False,
                context=context_str,
                agent_id="auto-negative-dashboard",
                query_trace_id=trace_id,
                query_text=query,
                result_rank=rank,
            )
    except Exception:
        log.exception("auto-negative outcome logging failed")


_GRAPH_CACHE_PATH = "/app/graph_cache/graph.json"


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

    # Cap node count — but keep organic structure by selecting hubs + their neighbors.
    # Pure top-N by mention_count = all hubs, avg_deg ~16, spring_layout rings.
    # Star-expand: take top hub nodes, add all immediate neighbors up to cap.
    # This gives dense cores + sparse periphery = organic Louvain blob shapes.
    if len(data["nodes"]) > max_nodes:
        # Build adjacency from edges
        from collections import defaultdict as _dd
        _adj: dict = _dd(set)
        for e in data["edges"]:
            _adj[e["data"]["source"]].add(e["data"]["target"])
            _adj[e["data"]["target"]].add(e["data"]["source"])

        # Sort all nodes by mention_count
        data["nodes"].sort(key=lambda n: n["data"].get("mention_count") or 0, reverse=True)
        all_node_map = {n["data"]["id"]: n for n in data["nodes"]}

        # Phase 1: seed hubs (top 25% or up to 800)
        n_hubs = min(800, max_nodes // 4)
        hub_ids: set[str] = {n["data"]["id"] for n in data["nodes"][:n_hubs]}

        # Phase 2: add neighbors of hubs until cap reached
        kept_ids: set[str] = set(hub_ids)
        for hub_id in hub_ids:
            if len(kept_ids) >= max_nodes:
                break
            for nbr in _adj[hub_id]:
                if nbr in all_node_map:
                    kept_ids.add(nbr)
                    if len(kept_ids) >= max_nodes:
                        break

        # Fill remaining slots with next highest mention_count if still under cap
        if len(kept_ids) < max_nodes:
            for n in data["nodes"]:
                if n["data"]["id"] not in kept_ids:
                    kept_ids.add(n["data"]["id"])
                    if len(kept_ids) >= max_nodes:
                        break

        data["nodes"] = [n for n in data["nodes"] if n["data"]["id"] in kept_ids]
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


_COMMUNITY_CACHE_PATH = "/app/graph_cache/communities.json"


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
    COMMUNITY_PALETTE = [
        "#5c7cfa",  # indigo
        "#f76707",  # deep orange
        "#2f9e44",  # forest green
        "#e03131",  # crimson
        "#7048e8",  # violet
        "#0ca678",  # teal
        "#1971c2",  # ocean blue
        "#c2255c",  # magenta
        "#d9480f",  # burnt orange
        "#5f3dc4",  # dark purple
        "#1098ad",  # cyan
        "#2b8a3e",  # deep green
        "#9c36b5",  # purple
        "#e67700",  # amber
        "#1864ab",  # navy
        "#087f5b",  # dark teal
        "#a61e4d",  # dark rose
        "#364fc7",  # cobalt
        "#5c940d",  # olive
        "#862e9c",  # dark violet
        "#e8590c",  # orange-red
        "#0b7285",  # deep cyan
        "#c92a2a",  # deep red
        "#2d6a4f",  # emerald
    ]
    nodes = []
    # Sort communities by size descending so stable palette assignment (largest = most distinct color)
    sorted_community_ids = sorted(community_nodes.keys(), key=lambda c: -len(community_nodes[c]))
    palette_index = {c_id: i for i, c_id in enumerate(sorted_community_ids)}
    for c_id, members in community_nodes.items():
        if len(members) < 3:
            continue
        # Centroid position
        cx = sum(m["x"] for m in members) / len(members)
        cy = sum(m["y"] for m in members) / len(members)
        # Dominant type (kept for metadata, not coloring)
        type_counts = defaultdict(int)
        for m in members:
            type_counts[m.get("type", "concept")] += 1
        dominant_type = max(type_counts, key=type_counts.get)

        meta = summary_map.get(c_id, {})
        # Top entities from graph cache (by mention_count) — DB community_id
        # doesn't match layout community_id so we can't use community_top here
        top_members = sorted(members, key=lambda m: -(m.get("mention_count") or 0))[:5]
        top = [
            {"name": m["label"], "type": m.get("type", ""), "mentions": m.get("mention_count", 0)}
            for m in top_members
        ]
        # Use LLM title if available, otherwise top entity names
        title = meta.get("title") or ", ".join(e["name"] for e in top[:3]) or f"Cluster {c_id}"
        color = COMMUNITY_PALETTE[palette_index.get(c_id, c_id) % len(COMMUNITY_PALETTE)]

        nodes.append({
            "data": {
                "id": f"c{c_id}",
                "community_id": c_id,
                "label": title,
                "x": cx,
                "y": cy,
                "size": len(members),
                "type": dominant_type,
                "color": color,
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

    # Recompute positions using a dedicated spring layout on the meta-graph.
    # Raw entity centroids cluster badly — the meta-graph needs its own layout
    # so communities spread out proportionally to their sizes and connections.
    import networkx as nx
    import math as _math
    meta_g = nx.Graph()
    node_ids = [n["data"]["id"] for n in nodes]
    sizes = {n["data"]["id"]: n["data"]["size"] for n in nodes}
    for nid in node_ids:
        meta_g.add_node(nid)
    for e in edges:
        meta_g.add_edge(e["data"]["source"], e["data"]["target"],
                        weight=e["data"]["weight"])
    if len(meta_g) > 1:
        # Scale layout canvas proportional to largest community radius so
        # big bubbles don't overlap.  Largest size ~839, smallest ~90.
        max_size = max(sizes.values()) if sizes else 1
        scale = _math.sqrt(max_size) * 12  # empirically good spread
        pos = nx.spring_layout(
            meta_g,
            weight="weight",
            scale=scale,
            seed=42,
            iterations=200,
            k=scale * 0.6 / max(1, _math.sqrt(len(meta_g))),
        )
        for node in nodes:
            nid = node["data"]["id"]
            if nid in pos:
                node["data"]["x"] = float(pos[nid][0])
                node["data"]["y"] = float(pos[nid][1])

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
        # Use timeout variant: if Ollama is backed up (background embeds queued),
        # fall back to FTS-only rather than blocking the UI for 50s+.
        embedding: list[float] | None = None
        try:
            embedding = await embed_text_with_timeout(q, timeout_s=15.0)
        except EmbedTimeout:
            log.warning("embed_text timed out for dashboard search — using FTS fallback")
        except Exception:
            log.exception("embed_text failed for dashboard search — using FTS fallback")

        if embedding is not None:
            memories = await queries.search_memories(
                embedding=embedding,
                limit=limit,
                threshold=0.2,
                tags=tags,
                category=category,
                source_machine=source_machine,
                text_query=q,
            )
        else:
            # FTS-only fallback: zero vector disables vector branch, FTS still runs
            memories = await queries.search_memories(
                embedding=[0.0] * 1024,
                limit=limit,
                threshold=1.1,  # impossibly high threshold skips vector matches
                tags=tags,
                category=category,
                source_machine=source_machine,
                text_query=q,
            )
        if min_quality is not None:
            memories = [m for m in memories if (m.get("quality_score") or 0) >= min_quality]
        if memories and settings.reranker_enabled and embedding is not None:
            try:
                from nobrainr.services.reranker import rerank
                memories = await rerank(q, memories, limit=limit)
            except Exception:
                log.exception("Reranker failed in dashboard search; returning unranked")

        # Attach trace fields so feedback can close the loop. Caller posts
        # back search_trace_id + search_rank when marking a result useful or
        # not — see /api/memories/{id}/feedback handler.
        trace_id = str(uuid4())
        for rank, row in enumerate(memories, start=1):
            row["search_trace_id"] = trace_id
            row["search_rank"] = rank
            row["search_query"] = q

        # Auto-negative outcome capture (parity with MCP search). Fire-and-
        # forget so the response stays fast. Without this the dashboard's
        # 100K+ daily searches contribute zero training signal — the same
        # bug fixed in MCP on 2026-04-18.
        if settings.auto_negative_outcomes_enabled and memories:
            asyncio.create_task(
                _log_auto_negative_outcomes(memories, trace_id, q)
            )
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


async def api_queue_retry(request: Request) -> JSONResponse:
    """Retry a failed or stuck write-queue item.

    Fully resets the row (clears started_at, completed_at, attempts, error)
    so the worker picks it up immediately.
    """
    queue_id = request.path_params["queue_id"]
    if not _valid_uuid(queue_id):
        return JSONResponse({"error": "Invalid queue_id"}, status_code=400)
    found = await write_queue.retry_failed(queue_id)
    if found:
        return JSONResponse({"status": "pending"})
    return JSONResponse({"error": "Queue item not found"}, status_code=404)


async def api_timeline(request: Request) -> JSONResponse:
    """Timeline data — memories ordered by date.

    When include_queue=1 and offset=0, pending/failed write-queue items
    are merged in as ghost entries so the timeline shows memories that
    are about to crystallize.
    """
    category = request.query_params.get("category") or None
    source_machine = request.query_params.get("source_machine") or None
    limit = min(int(request.query_params.get("limit", "100")), 500)
    offset = max(int(request.query_params.get("offset", "0")), 0)
    include_queue = request.query_params.get("include_queue") in {"1", "true", "yes"}

    memories = await queries.get_timeline_memories(
        limit=limit,
        offset=offset,
        category=category,
        source_machine=source_machine,
    )

    if include_queue and offset == 0:
        queued = await queries.get_timeline_write_queue(
            limit=min(limit, 50),
            category=category,
            source_machine=source_machine,
        )
        # Merge by created_at desc; queued keeps queue_status marker, stored rows don't.
        merged = sorted(
            queued + memories,
            key=lambda r: r.get("created_at") or "",
            reverse=True,
        )
        return JSONResponse(merged)

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


async def api_scheduler_pause(request: Request) -> JSONResponse:
    """Soft-pause the scheduler's background LLM maintenance jobs.

    Does NOT stop the memory_write_worker or the stale_processing_reaper —
    queue drain keeps flowing. Previous behaviour (calling scheduler.stop)
    killed those as collateral because they shared the _running flag, which
    silently froze the write queue any time a user tried to "pause scheduler
    to free GPU". See decision `nobrainr/scheduler` pause-bug 2026-04-20.
    """
    from nobrainr.scheduler import scheduler

    if not scheduler.running:
        return JSONResponse({"ok": False, "message": "Scheduler not running"})
    if scheduler._llm_jobs_paused:
        return JSONResponse({"ok": False, "message": "LLM jobs already paused"})
    scheduler._llm_jobs_paused = True
    return JSONResponse({"ok": True, "message": "Scheduler LLM jobs paused (queue worker still active)"})


async def api_scheduler_resume(request: Request) -> JSONResponse:
    """Resume the scheduler's background LLM maintenance jobs."""
    from nobrainr.scheduler import scheduler

    if not scheduler.running:
        # Full cold-start path: scheduler was fully stopped (not soft-paused)
        scheduler.start()
        return JSONResponse({"ok": True, "message": "Scheduler started"})
    if not scheduler._llm_jobs_paused:
        return JSONResponse({"ok": False, "message": "LLM jobs already running"})
    scheduler._llm_jobs_paused = False
    return JSONResponse({"ok": True, "message": "Scheduler LLM jobs resumed"})


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


async def api_health_detailed(request: Request) -> JSONResponse:
    """Real health signals for ops — not just "container up".

    Returns search latency quantiles over the last minute, reranker
    queue depth, DB pool usage, write queue depth, and scheduler job
    overdueness. Cheap — every field is a short SQL or in-memory read,
    no LLM calls, no rerank.
    """
    from nobrainr.extraction.llm import llm_activity_snapshot as _llm_activity_snapshot
    from nobrainr.services.metrics import (
        rerank_queue_stats,
        search_latency_stats,
    )
    from nobrainr.db.pool import get_pool

    pool = await get_pool()
    db_stats: dict = {}
    try:
        db_stats = {
            "pool_size": pool.get_size(),
            "pool_in_use": pool.get_size() - pool.get_idle_size(),
            "pool_idle": pool.get_idle_size(),
            "pool_max": pool.get_max_size(),
            "pool_min": pool.get_min_size(),
        }
    except Exception:
        pass

    async with pool.acquire() as conn:
        write_queue_depth = await conn.fetchval(
            "SELECT COUNT(*) FROM memory_write_queue WHERE status IN ('pending','processing')"
        )
        write_queue_stale = await conn.fetchval(
            """
            SELECT COUNT(*) FROM memory_write_queue
            WHERE status = 'processing'
              AND started_at < now() - interval '10 minutes'
            """
        )
        # Live view of what the worker is actively chewing on. Helps users
        # see the queue is actually moving when the bare depth counter
        # looks stuck for a few minutes on a heavy row.
        wq_processing_rows = await conn.fetch(
            """
            SELECT id, category, source_machine, skip_dedup, attempts,
                   extract(epoch from (now()-started_at))::int AS age_s,
                   left(summary, 80) AS summary_preview,
                   left(content, 180) AS content_preview
            FROM memory_write_queue
            WHERE status='processing'
            ORDER BY started_at ASC
            LIMIT 5
            """
        )
        # Category breakdown so users can see *what kind* of work is queued
        # (decisions/insights that do full LLM pipeline vs session-log noise).
        wq_by_category = await conn.fetch(
            """
            SELECT COALESCE(category, '(uncategorised)') AS category, COUNT(*) AS n
            FROM memory_write_queue
            WHERE status='pending'
            GROUP BY category
            ORDER BY n DESC
            LIMIT 12
            """
        )
        wq_recent_done = await conn.fetch(
            """
            SELECT id, category, result_status,
                   extract(epoch from (completed_at - COALESCE(started_at, enqueued_at)))::int AS duration_s,
                   left(summary, 60) AS summary_preview
            FROM memory_write_queue
            WHERE status='done' AND completed_at > now() - interval '5 minutes'
            ORDER BY completed_at DESC
            LIMIT 10
            """
        )
        overdue_jobs = await conn.fetch(
            """
            SELECT metadata->>'job' AS job,
                   MAX(created_at) AS last_run,
                   now() - MAX(created_at) AS since
            FROM agent_events
            WHERE event_type = 'scheduler'
              AND created_at > now() - interval '48 hours'
            GROUP BY metadata->>'job'
            HAVING now() - MAX(created_at) > interval '6 hours'
            ORDER BY since DESC
            LIMIT 10
            """
        )
        extraction_pending = await conn.fetchval(
            "SELECT COUNT(*) FROM memories WHERE extraction_status IS NULL OR extraction_status = 'failed'"
        )
        quality_unscored = await conn.fetchval(
            "SELECT COUNT(*) FROM memories WHERE quality_score IS NULL"
        )

    payload = {
        "search": search_latency_stats(),
        "rerank_queue": rerank_queue_stats(),
        "db": db_stats,
        "write_queue": {
            "depth": int(write_queue_depth or 0),
            "stale_processing": int(write_queue_stale or 0),
            "currently_processing": [
                {
                    "id": str(r["id"]),
                    "category": r["category"],
                    "source_machine": r["source_machine"],
                    "skip_dedup": bool(r["skip_dedup"]),
                    "attempts": r["attempts"],
                    "age_s": int(r["age_s"] or 0),
                    "summary": r["summary_preview"] or "",
                    "content_preview": r["content_preview"] or "",
                }
                for r in wq_processing_rows
            ],
            "pending_by_category": [
                {"category": r["category"], "count": int(r["n"])}
                for r in wq_by_category
            ],
            "recent_completions": [
                {
                    "id": str(r["id"]),
                    "category": r["category"],
                    "result_status": r["result_status"],
                    "duration_s": int(r["duration_s"] or 0),
                    "summary": r["summary_preview"] or "",
                }
                for r in wq_recent_done
            ],
        },
        "llm_activity": _llm_activity_snapshot(),
        "scheduler": {
            "overdue_jobs_6h": [
                {"job": r["job"], "last_run": r["last_run"].isoformat(), "since_s": int(r["since"].total_seconds())}
                for r in overdue_jobs if r["job"]
            ],
        },
        "backlog": {
            "extraction_pending": int(extraction_pending or 0),
            "quality_unscored": int(quality_unscored or 0),
        },
        "config": {
            "search_hard_timeout_s": settings.search_hard_timeout_s,
            "reranker_concurrency": settings.reranker_concurrency,
            "reranker_queue_timeout_s": settings.reranker_queue_timeout_s,
        },
    }

    # Simple traffic-light for ops
    p99 = payload["search"].get("p99_ms", 0)
    status = "ok"
    if p99 > 10000:
        status = "degraded"
    if (payload["write_queue"]["stale_processing"] > 0
            or payload["rerank_queue"].get("current", 0) > 5):
        status = "degraded"
    payload["status"] = status

    return JSONResponse(payload)


async def api_eval_runs(request: Request) -> JSONResponse:
    """Last N retrieval eval sweeps — for the dashboard quality trend."""
    from nobrainr.services.eval_retrieval import latest_eval_runs

    try:
        limit = int(request.query_params.get("limit", 30))
    except ValueError:
        limit = 30
    runs = await latest_eval_runs(limit=max(1, min(limit, 200)))
    for r in runs:
        if r.get("ran_at"):
            r["ran_at"] = r["ran_at"].isoformat()
    return JSONResponse({"runs": runs})


async def api_eval_run_now(request: Request) -> JSONResponse:
    """Trigger a retrieval eval sweep immediately. Used for A/B comparisons
    after a config change without waiting for the weekly scheduler tick.
    """
    from nobrainr.services.eval_retrieval import run_retrieval_eval

    body: dict = {}
    try:
        body = await request.json()
    except Exception:
        pass
    result = await run_retrieval_eval(
        model_tag=body.get("model_tag"),
        notes=body.get("notes") or "manual",
    )
    return JSONResponse(result)


async def api_extraction_eval_runs(request: Request) -> JSONResponse:
    """Last N extraction-eval sweeps (A/B current vs prior LLM)."""
    from nobrainr.services.eval_extraction import list_runs

    try:
        limit = int(request.query_params.get("limit", 30))
    except ValueError:
        limit = 30
    runs = await list_runs(limit=max(1, min(limit, 200)))
    return JSONResponse({"runs": runs})


async def api_extraction_eval_run_now(request: Request) -> JSONResponse:
    """Trigger an extraction eval sweep immediately."""
    from nobrainr.config import settings
    from nobrainr.services.eval_extraction import run_extraction_eval

    body: dict = {}
    try:
        body = await request.json()
    except Exception:
        pass
    result = await run_extraction_eval(
        candidate_model=body.get("candidate_model") or settings.extraction_model,
        incumbent_model=body.get("incumbent_model")
            or settings.extraction_eval_incumbent_model,
        sample_size=int(body.get("sample_size", settings.extraction_eval_sample_size)),
    )
    return JSONResponse(result)


async def api_memory_origin(request: Request) -> JSONResponse:
    """GET /api/memories/{memory_id}/origin — full source for the origin tab.

    origin_kind:
      conversation   — chatgpt/claude_web: full messages from conversations_raw,
                       window_start/end mark the distillation window
      document_chunk — docx/crawl: adjacent chunks (±2) from same document_id
      self           — affine/github/sticky/manual/session/claude: memory IS source
      derived        — synthesis/cross_machine/agent: no recoverable raw
      none           — unrecognised source_type or missing linkage
    """
    from nobrainr.db.pool import get_pool
    from uuid import UUID

    memory_id = request.path_params["memory_id"]
    if not _valid_uuid(memory_id):
        return JSONResponse({"error": "Invalid memory_id"}, status_code=400)

    memory = await queries.get_memory(memory_id)
    if not memory:
        return JSONResponse({"error": "Memory not found"}, status_code=404)

    source_type = memory.get("source_type") or ""
    metadata = memory.get("metadata") or {}
    pool = await get_pool()

    result: dict = {
        "memory_id": memory_id,
        "source_type": source_type,
        "origin_kind": "none",
    }

    # ── Conversation (chatgpt / claude_web) ──────────────────────────────────
    if source_type in ("chatgpt", "claude_web"):
        conv_id = metadata.get("conversation_id")
        if conv_id and _valid_uuid(conv_id):
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT id, title, messages, message_count, metadata "
                    "FROM conversations_raw WHERE id = $1",
                    UUID(conv_id),
                )
            if row:
                _cm = row["metadata"] or {}
                conv_meta = json.loads(_cm) if isinstance(_cm, str) else _cm
                _msgs = row["messages"] or []
                messages = json.loads(_msgs) if isinstance(_msgs, str) else _msgs
                n = len(messages)
                # Sliding-window params (size=8, overlap=2 → stride=6)
                WINDOW_SIZE, STRIDE = 8, 6
                win_idx = metadata.get("window_index", 0)
                win_start = win_idx * STRIDE
                win_end = min(win_start + WINDOW_SIZE - 1, n - 1)
                result["origin_kind"] = "conversation"
                result["conversation"] = {
                    "id": str(row["id"]),
                    "title": row["title"] or "Untitled",
                    "model": conv_meta.get("model"),
                    "original_date": conv_meta.get("original_date"),
                    "message_count": row["message_count"] or n,
                    "messages": messages,
                    "window_index": win_idx,
                    "total_windows": metadata.get("total_windows", 1),
                    "window_start": win_start,
                    "window_end": win_end,
                }

    # ── Document chunks (docx / crawl) ───────────────────────────────────────
    elif source_type in ("docx", "crawl") and metadata.get("document_id"):
        doc_id = metadata["document_id"]
        chunk_idx = int(metadata.get("chunk_index", 0))
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, content, summary,
                       (metadata->>'chunk_index')::int AS chunk_index,
                       metadata->>'contextual_prefix' AS contextual_prefix
                FROM memories
                WHERE metadata->>'document_id' = $1
                  AND (metadata->>'chunk_index')::int BETWEEN $2 AND $3
                ORDER BY (metadata->>'chunk_index')::int
                """,
                doc_id,
                max(0, chunk_idx - 2),
                chunk_idx + 2,
            )
        result["origin_kind"] = "document_chunk"
        result["document"] = {
            "file_path": metadata.get("file_path") or metadata.get("document_title") or doc_id,
            "document_title": metadata.get("document_title") or metadata.get("file_path") or "",
            "document_id": doc_id,
            "chunk_index": chunk_idx,
            "chunk_total": int(metadata.get("chunk_total", 1)),
            "contextual_prefix": metadata.get("contextual_prefix"),
            "chunks": [
                {
                    "memory_id": str(r["id"]),
                    "chunk_index": r["chunk_index"],
                    "content": r["content"],
                    "summary": r["summary"],
                    "contextual_prefix": r["contextual_prefix"],
                    "is_current": r["chunk_index"] == chunk_idx,
                }
                for r in rows
            ],
        }

    # ── Self-contained ────────────────────────────────────────────────────────
    elif source_type in (
        "affine_memos", "github", "sticky_notes", "manual",
        "claude", "session", "agent",
    ):
        result["origin_kind"] = "self"
        result["self_content"] = memory.get("content", "")
        result["self_metadata"] = {
            k: v for k, v in metadata.items()
            if k not in ("supersedes", "superseded_by")
        }

    # ── Derived (no recoverable raw) ─────────────────────────────────────────
    elif source_type in ("synthesis", "cross_machine_insight"):
        result["origin_kind"] = "derived"
        result["self_content"] = memory.get("content", "")
        result["self_metadata"] = metadata

    return JSONResponse(result)


async def api_commonplace(request: Request) -> JSONResponse:
    """GET /api/commonplace — community summaries for the commonplace view.

    Returns all communities sorted by member_count DESC, enriched with
    the count of distinct memories reachable via entity_memories.
    Optional semantic search via ?q= (embedding similarity on community summary).
    """
    from nobrainr.db.pool import get_pool

    q = request.query_params.get("q", "").strip()
    try:
        limit = min(int(request.query_params.get("limit", "200")), 500)
    except ValueError:
        limit = 200

    pool = await get_pool()
    async with pool.acquire() as conn:
        if q:
            embedding = await embed_text(q)
            # Semantic search on community_summaries where embedding exists,
            # fall back to all entity-communities ordered by memory_count
            rows = await conn.fetch(
                """
                WITH entity_communities AS (
                    SELECT e.community_id,
                           COUNT(DISTINCT e.id) AS entity_count,
                           COUNT(DISTINCT em.memory_id) AS memory_count,
                           (SELECT string_agg(sub.n, ' · ' ORDER BY sub.mc DESC)
                            FROM (SELECT DISTINCT e2.canonical_name AS n,
                                         MAX(e2.mention_count) AS mc
                                  FROM entities e2
                                  WHERE e2.community_id = e.community_id
                                    AND e2.canonical_name IS NOT NULL
                                  GROUP BY e2.canonical_name
                                  ORDER BY mc DESC LIMIT 3) sub) AS top_names
                    FROM entities e
                    JOIN entity_memories em ON em.entity_id = e.id
                    WHERE e.community_id IS NOT NULL
                    GROUP BY e.community_id
                    HAVING COUNT(DISTINCT em.memory_id) > 0
                )
                SELECT ec.community_id,
                       COALESCE(cs.title, ec.top_names) AS title,
                       COALESCE(cs.summary, '') AS summary,
                       COALESCE(cs.key_topics, ARRAY[]::text[]) AS key_topics,
                       ec.entity_count AS member_count,
                       cs.updated_at,
                       ec.memory_count,
                       CASE WHEN cs.embedding IS NOT NULL
                           THEN 1 - (cs.embedding <=> $1::vector)
                           ELSE NULL END AS score
                FROM entity_communities ec
                LEFT JOIN community_summaries cs ON cs.community_id = ec.community_id
                ORDER BY
                    CASE WHEN cs.embedding IS NOT NULL
                         THEN cs.embedding <=> $1::vector ELSE 1.0 END ASC,
                    ec.memory_count DESC
                LIMIT $2
                """,
                str(embedding),
                limit,
            )
        else:
            rows = await conn.fetch(
                """
                WITH entity_communities AS (
                    SELECT e.community_id,
                           COUNT(DISTINCT e.id) AS entity_count,
                           COUNT(DISTINCT em.memory_id) AS memory_count,
                           (SELECT string_agg(sub.n, ' · ' ORDER BY sub.mc DESC)
                            FROM (SELECT DISTINCT e2.canonical_name AS n,
                                         MAX(e2.mention_count) AS mc
                                  FROM entities e2
                                  WHERE e2.community_id = e.community_id
                                    AND e2.canonical_name IS NOT NULL
                                  GROUP BY e2.canonical_name
                                  ORDER BY mc DESC LIMIT 3) sub) AS top_names
                    FROM entities e
                    JOIN entity_memories em ON em.entity_id = e.id
                    WHERE e.community_id IS NOT NULL
                    GROUP BY e.community_id
                    HAVING COUNT(DISTINCT em.memory_id) > 0
                )
                SELECT ec.community_id,
                       COALESCE(cs.title, ec.top_names) AS title,
                       COALESCE(cs.summary, '') AS summary,
                       COALESCE(cs.key_topics, ARRAY[]::text[]) AS key_topics,
                       ec.entity_count AS member_count,
                       cs.updated_at,
                       ec.memory_count,
                       NULL::float AS score
                FROM entity_communities ec
                LEFT JOIN community_summaries cs ON cs.community_id = ec.community_id
                ORDER BY ec.memory_count DESC
                LIMIT $1
                """,
                limit,
            )

    return JSONResponse([
        {
            "community_id": r["community_id"],
            "title": r["title"] or f"Community {r['community_id']}",
            "summary": r["summary"] or "",
            "key_topics": list(r["key_topics"] or []),
            "member_count": r["member_count"] or 0,
            "memory_count": r["memory_count"],
            "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
            "score": float(r["score"]) if r["score"] is not None else None,
        }
        for r in rows
    ])


async def api_commonplace_memories(request: Request) -> JSONResponse:
    """GET /api/commonplace/{community_id}/memories — memories for a community.

    Returns the top memories linked via entity_memories, ordered by
    importance DESC then created_at DESC.
    """
    from nobrainr.db.pool import get_pool

    try:
        community_id = int(request.path_params["community_id"])
    except (ValueError, KeyError):
        return JSONResponse({"error": "Invalid community_id"}, status_code=400)

    try:
        limit = min(int(request.query_params.get("limit", "100")), 500)
        offset = max(int(request.query_params.get("offset", "0")), 0)
    except ValueError:
        limit, offset = 100, 0

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT DISTINCT ON (m.id)
                m.id, m.content, m.summary, m.source_type, m.source_machine,
                m.tags, m.category, m.importance, m.quality_score,
                m.created_at, m.updated_at, m.tier, m.metadata
            FROM memories m
            JOIN entity_memories em ON em.memory_id = m.id
            JOIN entities e ON e.id = em.entity_id
            WHERE e.community_id = $1
            ORDER BY m.id, COALESCE(m.importance, 0.5) DESC, m.created_at DESC
            LIMIT $2 OFFSET $3
            """,
            community_id,
            limit,
            offset,
        )
        # Re-sort after DISTINCT ON flattens ordering
        result = sorted(
            [queries._row_to_dict(r) for r in rows],
            key=lambda m: (-(m.get("importance") or 0.5), m.get("created_at") or ""),
        )
    return JSONResponse(result)


async def api_commonplace_search(request: Request) -> JSONResponse:
    """GET /api/commonplace/search?q= — full hybrid search across memories, grouped by chapter.

    Uses the same vector + FTS + graph RRF pipeline as MCP memory_search.
    Returns:
      { query, total_hits, chapters: [...sorted by top RRF score...], hits: [...flat list...] }
    In search mode the frontend uses `hits` filtered by community_id to populate
    the entry panel without a second API round-trip.
    """
    from nobrainr.db.pool import get_pool
    from collections import defaultdict

    q = request.query_params.get("q", "").strip()
    if not q:
        return JSONResponse({"query": "", "total_hits": 0, "chapters": [], "hits": []})

    try:
        limit = min(int(request.query_params.get("limit", "80")), 200)
    except ValueError:
        limit = 80

    vec_threshold = 0.15
    try:
        embedding: list[float] = await embed_text_with_timeout(q, timeout_s=15.0)
    except (EmbedTimeout, Exception):
        embedding = [0.0] * 1024  # FTS-only fallback
        vec_threshold = 1.1       # disable vector branch
    raw_hits = await queries.search_memories(
        embedding,
        text_query=q,
        limit=limit,
        threshold=vec_threshold,
        include_cold=True,
    )

    if not raw_hits:
        return JSONResponse({"query": q, "total_hits": 0, "chapters": [], "hits": []})

    pool = await get_pool()
    memory_ids = [UUID(h["id"]) for h in raw_hits]

    async with pool.acquire() as conn:
        # Batch-fetch the primary community_id for each hit memory
        comm_rows = await conn.fetch(
            """
            SELECT DISTINCT ON (em.memory_id)
                em.memory_id::text,
                e.community_id
            FROM entity_memories em
            JOIN entities e ON e.id = em.entity_id
            WHERE em.memory_id = ANY($1::uuid[])
              AND e.community_id IS NOT NULL
            ORDER BY em.memory_id, e.community_id
            """,
            memory_ids,
        )
        mid_to_comm: dict = {r["memory_id"]: r["community_id"] for r in comm_rows}

        # Fetch chapter metadata for relevant communities
        comm_ids = list(set(mid_to_comm.values()))
        chapter_meta: dict = {}
        if comm_ids:
            ch_rows = await conn.fetch(
                """
                SELECT e.community_id,
                       COALESCE(cs.title,
                           (SELECT string_agg(sub.n, ' · ' ORDER BY sub.mc DESC)
                            FROM (SELECT DISTINCT e2.canonical_name AS n,
                                         MAX(e2.mention_count) AS mc
                                  FROM entities e2
                                  WHERE e2.community_id = e.community_id
                                    AND e2.canonical_name IS NOT NULL
                                  GROUP BY e2.canonical_name
                                  ORDER BY mc DESC LIMIT 3) sub)
                       ) AS title,
                       COALESCE(cs.summary, '') AS summary,
                       COALESCE(cs.key_topics, ARRAY[]::text[]) AS key_topics
                FROM (SELECT DISTINCT community_id FROM entities WHERE community_id = ANY($1)) e
                LEFT JOIN community_summaries cs ON cs.community_id = e.community_id
                """,
                comm_ids,
            )
            for r in ch_rows:
                chapter_meta[r["community_id"]] = {
                    "title": r["title"] or f"Community {r['community_id']}",
                    "summary": r["summary"] or "",
                    "key_topics": list(r["key_topics"] or []),
                }

    # Build flat hits list — use -1 as sentinel for uncategorised
    NO_COMMUNITY = -1
    hits = []
    for h in raw_hits:
        comm_id = mid_to_comm.get(h["id"], NO_COMMUNITY)
        hits.append({
            "id": h["id"],
            "summary": h.get("summary") or "",
            "content": h.get("content") or "",
            "source_type": h.get("source_type") or "",
            "source_machine": h.get("source_machine") or "",
            "tags": h.get("tags") or [],
            "category": h.get("category") or "",
            "importance": h.get("importance"),
            "quality_score": h.get("quality_score"),
            "created_at": h.get("created_at"),
            "rrf_score": round(float(h.get("rrf_score") or 0), 5),
            "community_id": comm_id,
        })

    # Group by chapter, ordered by best RRF score
    chapter_hits: dict = defaultdict(list)
    for h in hits:
        chapter_hits[h["community_id"]].append(h)

    chapters = []
    for comm_id, ch_hits in chapter_hits.items():
        top_score = max(h["rrf_score"] for h in ch_hits)
        if comm_id == NO_COMMUNITY:
            meta = {"title": "Uncategorised", "summary": "", "key_topics": []}
        else:
            meta = chapter_meta.get(comm_id, {"title": f"Community {comm_id}", "summary": "", "key_topics": []})
        chapters.append({
            "community_id": comm_id,
            "title": meta["title"],
            "summary": meta["summary"],
            "key_topics": meta["key_topics"],
            "hit_count": len(ch_hits),
            "top_score": top_score,
            "memory_count": len(ch_hits),
            "member_count": 0,
            "updated_at": None,
            "score": top_score,
        })

    chapters.sort(key=lambda c: c["top_score"], reverse=True)

    return JSONResponse({
        "query": q,
        "total_hits": len(hits),
        "chapters": chapters,
        "hits": hits,
    })


async def api_eval_golden(request: Request) -> JSONResponse:
    """List active golden queries so reviewers can vet/adjust them."""
    from nobrainr.db.pool import get_pool

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id::text, query, expected_ids, notes, tags, active, created_at
            FROM eval_golden_queries
            ORDER BY created_at DESC
            LIMIT 500
            """
        )
    return JSONResponse({
        "queries": [
            {
                "id": r["id"],
                "query": r["query"],
                "expected_ids": [str(eid) for eid in (r["expected_ids"] or [])],
                "notes": r["notes"],
                "tags": list(r["tags"] or []),
                "active": r["active"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ]
    })


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
    Route("/api/memories/{memory_id}/origin", api_memory_origin, methods=["GET"]),
    Route("/api/facts", api_facts_search),
    Route("/api/facts/stats", api_facts_stats),
    Route("/api/queue/{queue_id}/retry", api_queue_retry, methods=["POST"]),
    Route("/api/timeline", api_timeline),
    Route("/api/node/{entity_id}", api_node_detail),
    Route("/api/stats", api_stats),
    Route("/api/scheduler", api_scheduler),
    Route("/api/scheduler/pause", api_scheduler_pause, methods=["POST"]),
    Route("/api/scheduler/resume", api_scheduler_resume, methods=["POST"]),
    Route("/api/recall", api_recall),
    Route("/api/smart-recall", api_smart_recall),
    Route("/api/entities", api_entities),
    Route("/api/categories", api_categories),
    Route("/api/tags", api_tags),
    Route("/api/events", api_events),
    Route("/api/monitoring", api_monitoring),
    Route("/api/eval/runs", api_eval_runs),
    Route("/api/eval/run", api_eval_run_now, methods=["POST"]),
    Route("/api/eval/golden", api_eval_golden),
    Route("/api/eval/extraction/runs", api_extraction_eval_runs),
    Route("/api/eval/extraction/run", api_extraction_eval_run_now, methods=["POST"]),
    Route("/api/health/detailed", api_health_detailed),
    Route("/api/commonplace", api_commonplace),
    Route("/api/commonplace/search", api_commonplace_search),
    Route("/api/commonplace/{community_id}/memories", api_commonplace_memories),
]
