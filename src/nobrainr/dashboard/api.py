"""API endpoints — pure JSON responses + SSE stream."""

import asyncio
import base64
import json
import logging
import time
from collections import defaultdict
from datetime import UTC, datetime, timezone
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
from nobrainr.extraction.llm import _direct_slots_url

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


class BadParam(ValueError):
    """A query parameter that cannot be read. Answered as 400 {"error": ...}."""


def _int_param(request: Request, name: str, default: int, *, lo: int | None = None, hi: int | None = None) -> int:
    """Whole-number query parameter, clamped to [lo, hi]. Garbage is a 400, not a 500."""
    roh = request.query_params.get(name)
    if roh is None or roh.strip() == "":
        wert = default
    else:
        try:
            wert = int(roh)
        except ValueError:
            raise BadParam(f"Invalid {name}: expected a whole number") from None
    if lo is not None:
        wert = max(lo, wert)
    if hi is not None:
        wert = min(hi, wert)
    return wert


def _float_param(request: Request, name: str, default: float, *, lo: float | None = None, hi: float | None = None) -> float:
    """Numeric query parameter, clamped to [lo, hi]. Garbage (and NaN) is a 400."""
    roh = request.query_params.get(name)
    if roh is None or roh.strip() == "":
        wert = default
    else:
        try:
            wert = float(roh)
        except ValueError:
            raise BadParam(f"Invalid {name}: expected a number") from None
        if wert != wert:  # NaN compares unequal to itself
            raise BadParam(f"Invalid {name}: expected a number")
    if lo is not None:
        wert = max(lo, wert)
    if hi is not None:
        wert = min(hi, wert)
    return wert


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
_graph_erneuerung: asyncio.Task | None = None


def _datei_bytes(pfad: str) -> bytes:
    with open(pfad, "rb") as f:
        return f.read()


_GRAPH_KOMPAKT_FORMAT = "compact-1"


def graph_kompakt(data: dict) -> dict:
    """The graph as parallel arrays instead of one object per node and edge.

    Measured 2026-11-10 on 3,498 nodes / 63,167 edges: the object form is
    12.4 MB (2.9 MB gzipped), 8.6 MB of it the three UUIDs every edge carries.
    Here an edge is two node indexes, an index into ``edge_labels`` and a
    confidence. The dashboard never reads the edge id, so it is dropped; edges
    whose endpoint is not among the nodes are dropped too (the client skipped
    them anyway). Coordinates keep one decimal — the layout spans thousands.
    """
    nodes = [n["data"] for n in data.get("nodes", [])]
    index = {n["id"]: i for i, n in enumerate(nodes)}
    labels: dict[str, int] = {}
    source: list[int] = []
    target: list[int] = []
    label: list[int] = []
    confidence: list[float | None] = []
    for edge in data.get("edges", []):
        d = edge["data"]
        s, t = index.get(d.get("source")), index.get(d.get("target"))
        if s is None or t is None:
            continue
        source.append(s)
        target.append(t)
        label.append(labels.setdefault(d.get("label") or "", len(labels)))
        c = d.get("confidence")
        confidence.append(round(c, 2) if isinstance(c, (int, float)) else None)

    def spalte(feld: str, standard=None) -> list:
        return [n.get(feld, standard) for n in nodes]

    def koordinate(feld: str) -> list[float]:
        return [round(float(n.get(feld) or 0.0), 1) for n in nodes]

    return {
        "format": _GRAPH_KOMPAKT_FORMAT,
        "nodes": {
            "id": spalte("id"),
            "label": spalte("label"),
            "type": spalte("type"),
            "description": spalte("description"),
            "mention_count": spalte("mention_count", 0),
            "x": koordinate("x"),
            "y": koordinate("y"),
            "community": spalte("community", -1),
        },
        "edges": {"source": source, "target": target, "label": label, "confidence": confidence},
        "edge_labels": list(labels),
    }


def _graph_kompakt_bytes(pfad: str) -> bytes:
    """Compact form of the cached graph, kept in a file beside the cache.

    Rebuilt when the cache is newer than it; written to a temp file first so
    a reader never sees half a file.
    """
    import os

    kompakt = pfad + ".compact"
    try:
        if os.path.getmtime(kompakt) >= os.path.getmtime(pfad):
            roh = _datei_bytes(kompakt)
            if roh[:1] == b"{" and roh.rstrip()[-1:] == b"}":
                return roh
    except OSError:
        pass
    with open(pfad, "rb") as f:
        data = json.load(f)
    roh = json.dumps(graph_kompakt(data), separators=(",", ":"), ensure_ascii=False).encode()
    try:
        tmp = f"{kompakt}.{os.getpid()}.tmp"
        with open(tmp, "wb") as f:
            f.write(roh)
        os.replace(tmp, kompakt)
    except OSError:
        log.warning("Could not write %s; serving the compact graph uncached", kompakt, exc_info=True)
    return roh


def _graph_im_hintergrund_erneuern() -> None:
    """Recompute the graph layout without making a request wait for it.

    At most one refresh at a time; a failure leaves the old cache in place.
    """
    global _graph_erneuerung
    if _graph_erneuerung is not None and not _graph_erneuerung.done():
        return

    async def _lauf() -> None:
        try:
            scope = {"type": "http", "method": "GET", "path": "/api/graph",
                     "query_string": b"refresh=true", "headers": []}
            await api_graph(Request(scope))
        except Exception:
            log.warning("Background graph refresh failed; keeping the old cache", exc_info=True)

    _graph_erneuerung = asyncio.create_task(_lauf())


async def api_graph(request: Request) -> JSONResponse:
    """Full knowledge graph with server-computed layout (Louvain + spring).
    Uses cached layout if available — cache rebuilt after community_detection runs."""
    import os
    import time as _time

    # Serve from cache whenever there is one. A cache older than 24h is still
    # served — the layout takes about a minute to compute (measured 60s on
    # 2026-11-09) and the dashboard used to sit on a blank graph for that
    # long. The refresh then runs in the background (stale-while-revalidate,
    # same idea as /api/galaxy). The file is sent as bytes: parsing 12 MB of
    # JSON just to serialise it again cost ~0.5s of event-loop time per call.
    force_refresh = request.query_params.get("refresh", "").lower() == "true"
    # ?format=compact: parallel arrays, about a seventh of the size (see
    # graph_kompakt). The object form stays the default for other callers.
    kompakt = request.query_params.get("format", "").lower() == "compact"
    if not force_refresh and os.path.exists(_GRAPH_CACHE_PATH):
        cache_age = _time.time() - os.path.getmtime(_GRAPH_CACHE_PATH)
        try:
            roh = await asyncio.to_thread(_graph_kompakt_bytes if kompakt else _datei_bytes, _GRAPH_CACHE_PATH)
            if roh[:1] == b"{" and roh.rstrip()[-1:] == b"}":
                if cache_age >= 86400:
                    _graph_im_hintergrund_erneuern()
                log.info("Serving graph from cache (%.0fs old, %d bytes)", cache_age, len(roh))
                return Response(content=roh, media_type="application/json",
                                headers={"X-Cache-Age": str(int(cache_age))})
        except Exception:
            pass  # cache unreadable, fall through to recompute

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

    return JSONResponse(graph_kompakt(data) if kompakt else data)


_COMMUNITY_CACHE_PATH = "/app/graph_cache/communities.json"


async def api_graph_live(request: Request) -> JSONResponse:
    """Raw graph arrays for the client-side GPU force engine (cosmos.gl).

    No server layout — the Constellarium simulates positions live on the
    user's GPU, so this endpoint only ships compact parallel arrays:
    names/communities/degrees plus links as index pairs. valid edges only
    (edge_invalidation losers stay out). min_mentions trims leaf noise;
    the default cap keeps payloads phone-friendly.
    """
    from nobrainr.db.pool import get_pool

    try:
        max_nodes = min(int(request.query_params.get("max_nodes", "12000")), 40000)
    except ValueError:
        max_nodes = 12000
    try:
        min_degree = max(1, int(request.query_params.get("min_degree", "2")))
    except ValueError:
        min_degree = 2

    pool = await get_pool()
    async with pool.acquire() as conn:
        nodes = await conn.fetch(
            """
            SELECT e.id, e.name, COALESCE(e.community_id::int, -1) AS community,
                   count(er.id) AS degree
            FROM entities e
            JOIN entity_relations er
              ON er.valid AND (er.source_entity_id = e.id OR er.target_entity_id = e.id)
            GROUP BY e.id, e.name, e.community_id
            HAVING count(er.id) >= $1
            ORDER BY count(er.id) DESC
            LIMIT $2
            """,
            min_degree, max_nodes,
        )
        idx = {r["id"]: i for i, r in enumerate(nodes)}
        edges = await conn.fetch(
            """
            SELECT source_entity_id AS s, target_entity_id AS t
            FROM entity_relations
            WHERE valid AND source_entity_id = ANY($1::uuid[])
              AND target_entity_id = ANY($1::uuid[])
            """,
            list(idx.keys()),
        )
    links: list[int] = []
    for e in edges:
        si, ti = idx.get(e["s"]), idx.get(e["t"])
        if si is not None and ti is not None and si != ti:
            links.append(si)
            links.append(ti)
    return JSONResponse({
        "names": [r["name"] for r in nodes],
        "ids": [str(r["id"]) for r in nodes],
        "communities": [r["community"] for r in nodes],
        "degrees": [r["degree"] for r in nodes],
        "links": links,
        "node_count": len(nodes),
        "link_count": len(links) // 2,
    })


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

    # Build community meta-nodes.
    # Illuminated-manuscript pigments — muted jewel tones in one luminance
    # band, ordered so adjacent palette indices stay maximally distinct.
    # Matches the dashboard codex aesthetic (TYPE_COLORS in GraphView).
    COMMUNITY_PALETTE = [
        "#c9a96e",  # gilt gold
        "#7fa3c2",  # ultramarine wash
        "#bd5a52",  # madder red
        "#74a48d",  # verdigris
        "#a98bc0",  # amethyst
        "#c98a6d",  # terracotta
        "#6f81ab",  # indigo
        "#a8b072",  # sage bronze
        "#c0788f",  # rose madder
        "#5f9ea0",  # patina teal
        "#b3924f",  # ochre
        "#8d7ab5",  # violet ash
        "#7da06b",  # moss
        "#b8685a",  # sienna
        "#6b93b8",  # cerulean wash
        "#b89d83",  # parchment tan
        "#9a6b8f",  # plum
        "#86a895",  # celadon
        "#c2925a",  # amber
        "#7a86c2",  # lavender blue
        "#a0795d",  # umber
        "#5d8a78",  # pine verdigris
        "#b07070",  # faded carmine
        "#94a3b0",  # silverpoint
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


# Time budgets for the interactive dashboard search. The embedding and the
# reranker share one GPU with the scheduler and the chat model; measured on
# 2026-11-09 the same query took 3s and then 49s. A person typing into a
# search box is better served by the fused vector+FTS order now than by the
# reranked order in a minute, so both steps give up after a few seconds:
# no embedding -> FTS only, no rerank -> keep the RRF order.
_SUCHE_EMBED_BUDGET_S = 5.0
_SUCHE_RERANK_BUDGET_S = 6.0
_SUCHE_RERANK_KOPF = 20  # only the head of the list is worth a cross-encoder pass


async def _rerank_im_budget(q: str, memories: list[dict], limit: int) -> list[dict]:
    """Rerank the first ``_SUCHE_RERANK_KOPF`` results within the time budget.

    The tail keeps its order behind the reranked head. On timeout or error the
    list comes back unchanged.
    """
    from nobrainr.services.reranker import rerank

    kopf, rest = memories[:_SUCHE_RERANK_KOPF], memories[_SUCHE_RERANK_KOPF:]
    try:
        kopf = await asyncio.wait_for(
            rerank(q, kopf, limit=min(limit, len(kopf))), timeout=_SUCHE_RERANK_BUDGET_S
        )
    except TimeoutError:
        log.warning("Reranker exceeded %.0fs in dashboard search; returning fused order", _SUCHE_RERANK_BUDGET_S)
        return memories
    except Exception:
        log.exception("Reranker failed in dashboard search; returning unranked")
        return memories
    return (kopf + rest)[:limit]


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
            embedding = await embed_text_with_timeout(q, timeout_s=_SUCHE_EMBED_BUDGET_S)
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
            memories = await _rerank_im_budget(q, memories, limit)

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
    limit = _int_param(request, "limit", 100, hi=500)
    offset = _int_param(request, "offset", 0, lo=0)
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


# /api/stats runs ~15 count queries (1.8-2.3s measured on 2026-11-09) and is
# polled by the container healthcheck, status lines and every open dashboard
# (151 calls/hour). The numbers do not change meaningfully within half a
# minute, so one computation serves all callers in that window.
_STATS_TTL_S = 30.0
_stats_cache: dict = {"ts": 0.0, "data": None}
_stats_lock = asyncio.Lock()


async def api_stats(request: Request) -> JSONResponse:
    """Dashboard statistics including feedback and event counts.

    Cached for ``_STATS_TTL_S`` seconds; ``?refresh=true`` bypasses the cache.
    """
    frisch = request.query_params.get("refresh", "").lower() == "true"
    if not frisch and _stats_cache["data"] is not None and time.monotonic() - _stats_cache["ts"] < _STATS_TTL_S:
        return JSONResponse(_stats_cache["data"])
    async with _stats_lock:
        # A second caller that waited for the lock finds the fresh value.
        if not frisch and _stats_cache["data"] is not None and time.monotonic() - _stats_cache["ts"] < _STATS_TTL_S:
            return JSONResponse(_stats_cache["data"])
        stats = await queries.get_stats()
        feedback_stats = await queries.get_feedback_stats()
        stats.update(feedback_stats)
        _stats_cache["data"] = stats
        _stats_cache["ts"] = time.monotonic()
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


async def api_scheduler_task_runs(request: Request) -> JSONResponse:
    """Return the last N scheduler_runs rows for one task — backs the
    dashboard's task drawer (sparkline + status tally + last error tail).

    Path param: task_name (URL-encoded).
    Query params: limit (1..200, default 50).
    """
    name = request.path_params.get("task_name", "").strip()
    if not name:
        return JSONResponse({"error": "task_name required"}, status_code=400)
    try:
        limit = int(request.query_params.get("limit", "50"))
    except ValueError:
        limit = 50
    runs = await queries.list_scheduler_runs(name, limit=limit)
    # Friendly tally for the drawer header. Counted server-side so the
    # client doesn't have to walk the whole list.
    tally = {"ok": 0, "failed": 0, "timeout": 0, "running": 0}
    for r in runs:
        tally[r["status"]] = tally.get(r["status"], 0) + 1
    last_error = next(
        (r for r in runs if r["status"] in ("failed", "timeout") and r["error_msg"]),
        None,
    )
    return JSONResponse({
        "task_name": name,
        "runs": runs,
        "tally": tally,
        "last_error": last_error,
    })


async def api_recall(request: Request) -> JSONResponse:
    """Fast text-only memory search (PostgreSQL full-text, no embedding call).

    Until 2026-11-09 this used the English text-search config on the bare
    content, which no index covers — every call was a sequential scan over all
    memories (10-15s on 147k rows). It now runs ``queries.fts_memories``: the
    predicate ``idx_memories_content_fts`` covers, all words must match, and
    frequent words are ranked on a capped candidate set (see there).
    Superseded, expired and cold memories are no longer returned.
    """
    q = request.query_params.get("q", "").strip()
    if not q:
        return JSONResponse([])

    try:
        limit = max(1, min(int(request.query_params.get("limit", "5")), 100))
    except ValueError:
        return JSONResponse({"error": "Invalid limit"}, status_code=400)

    results = await queries.fts_memories(q, limit=limit)
    # Track access on recalled memories
    if results:
        from nobrainr.db.pool import get_pool

        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE memories SET last_accessed_at = now(), access_count = access_count + 1 WHERE id = ANY($1)",
                [UUID(r["id"]) for r in results],
            )
    return JSONResponse(results)


async def api_search_preview(request: Request) -> JSONResponse:
    """Full-text results for the dashboard to show while the full search runs.

    /api/memories?q= embeds the query, fuses five branches and reranks: 1-3s
    on a quiet GPU, up to the 11s budget on a busy one. This answers the same
    question from the text index alone, with the same filters, so the list is
    on screen at once and the full answer replaces it when it arrives.

    A preview is not a search: no access tracking, no trace id, no
    auto-negative outcomes — nothing here may become training signal.
    """
    q = request.query_params.get("q", "").strip()
    if not q:
        return JSONResponse([])
    category = request.query_params.get("category") or None
    source_machine = request.query_params.get("source_machine") or None
    tags_param = request.query_params.get("tags", "").strip()
    tags = [t.strip() for t in tags_param.split(",") if t.strip()] if tags_param else None
    try:
        limit = max(1, min(int(request.query_params.get("limit", "50")), 200))
    except ValueError:
        return JSONResponse({"error": "Invalid limit"}, status_code=400)

    memories = await queries.fts_memories(
        q, limit=limit, tags=tags, category=category, source_machine=source_machine,
    )
    min_quality_param = request.query_params.get("min_quality", "").strip()
    if min_quality_param:
        try:
            min_quality = max(0.0, min(1.0, float(min_quality_param)))
            memories = [m for m in memories if (m.get("quality_score") or 0) >= min_quality]
        except ValueError:
            pass
    return JSONResponse(memories)


async def api_smart_recall(request: Request) -> JSONResponse:
    """Semantic memory search via embedding — richer than /api/recall but needs Ollama.

    Used by hooks to brief agents with contextually relevant memories.
    Falls back to /api/recall on embedding failure.
    """
    q = request.query_params.get("q", "").strip()
    if not q:
        return JSONResponse([])
    limit = _int_param(request, "limit", 5, hi=20)
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


async def api_brief(request: Request) -> JSONResponse:
    """One-call session brief for hooks (hook v3, 2026-07-22).

    Returns fact-checked context cards + trust-floored memories in a single
    response so the UserPromptSubmit hook makes ONE request instead of two
    raw searches. Cards are the accuracy-stamped layer (card_factcheck);
    memories are hybrid hits post-filtered by trust_score. No LLM, no
    auto-route — this path must stay instant.

    Params: q (task/prompt text), cards (max, default 2), memories (max,
    default 5), trust_floor (default 0.6), min_accuracy (default 0.5 —
    cards below it are excluded; unchecked cards pass with accuracy null).
    """
    q = request.query_params.get("q", "").strip()
    if not q:
        return JSONResponse({"cards": [], "memories": []})
    n_cards = _int_param(request, "cards", 2, hi=5)
    n_mem = _int_param(request, "memories", 5, hi=12)
    trust_floor = _float_param(request, "trust_floor", 0.6)
    min_accuracy = _float_param(request, "min_accuracy", 0.5)

    from nobrainr.db.pool import get_pool
    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            card_rows = await conn.fetch(
                """
                SELECT title, body, subject_key, trust_score, published_accuracy,
                       GREATEST(
                         similarity(lower(subject_key), lower($1)),
                         similarity(lower(title), lower($1)),
                         ts_rank(to_tsvector('simple', body),
                                 plainto_tsquery('simple', $1)) * 4
                       ) AS score
                FROM context_cards
                WHERE published_accuracy IS NULL OR published_accuracy >= $3
                ORDER BY score DESC
                LIMIT $2
                """,
                q, n_cards, min_accuracy,
            )
    except Exception:
        card_rows = []
    cards = [
        {"title": r["title"], "brief": r["body"], "subject": r["subject_key"],
         "accuracy": r["published_accuracy"]}
        for r in card_rows if (r["score"] or 0) > 0.05
    ]

    memories: list = []
    embedding: list[float] | None = None
    try:
        embedding = await embed_text(q)
        hits = await queries.search_memories(
            embedding=embedding, limit=n_mem * 3, threshold=0.25, text_query=q,
        )
        hit_pool = [
            h for h in hits
            if (h.get("trust_score") is None or h["trust_score"] >= trust_floor)
        ]
        # M2 fix (2026-08-11): this path served RAW hybrid rank — injection
        # precision measured 0.20 (v2 era) and 0.225 (v3, trust floor alone:
        # trust ≠ relevance). The cross-encoder is what makes top-K precise;
        # ~0.3-0.9s for ≤15 docs on the pinned GPU reranker stays inside the
        # hook's 3s curl budget. Rerank failure degrades to hybrid order.
        try:
            from nobrainr.services.reranker import rerank as _rerank
            hit_pool = await _rerank(q, hit_pool, limit=n_mem)
        except Exception:
            pass
        memories = hit_pool[:n_mem]
    except Exception:
        pass  # cards alone still brief the agent

    # Failed-fix lane (2026-08-14): error-pattern memories matching the
    # task, so an agent is warned BEFORE repeating a fix that already
    # failed (the error_store data existed; nothing delivered it at task
    # start). Cold tier included — errors are always worth finding. High
    # similarity bar: a wrong warning is worse than none.
    errors: list = []
    if embedding is not None:
        try:
            err_hits = await queries.search_memories(
                embedding=embedding, limit=6, threshold=0.45, text_query=q,
                tags=["error-pattern"], include_cold=True,
            )
            errors = [
                {"summary": h.get("summary"),
                 "signature": (h.get("metadata") or {}).get("error_signature"),
                 "content": (h.get("content") or "")[:600],
                 "id": h.get("id")}
                for h in err_hits[:2]
                if (h.get("similarity") or 0) >= 0.6
            ]
        except Exception:
            pass

    # Procedural lane (2026-08-14): when-to-use/what-to-do procedures
    # matched to the task — the injection half of the Memp pattern (the
    # distill half runs as procedural_distill + the host session distiller).
    # procedural_memories has no embeddings: FTS + trigram over the title,
    # active + unexpired only, priority breaks ties.
    procedures: list = []
    try:
        async with pool.acquire() as conn:
            proc_rows = await conn.fetch(
                """
                SELECT title, content, scope, priority,
                       GREATEST(
                         similarity(lower(title), lower($1)),
                         ts_rank(to_tsvector('simple', title || ' ' || content),
                                 plainto_tsquery('simple', $1)) * 4
                       ) AS score
                FROM procedural_memories
                WHERE active
                  AND (expires_at IS NULL OR expires_at > now())
                ORDER BY score DESC, priority DESC
                LIMIT 2
                """,
                q,
            )
        procedures = [
            {"title": r["title"], "procedure": r["content"][:800],
             "scope": r["scope"]}
            for r in proc_rows if (r["score"] or 0) > 0.08
        ]
    except Exception:
        pass

    # Colleagues lane (2026-08-15): active agents whose presence task is
    # related to THIS prompt — continuous awareness without polling.
    # Trigram+FTS on short task strings; own agent excluded client-side.
    colleagues: list = []
    try:
        async with pool.acquire() as conn:
            col_rows = await conn.fetch(
                """
                SELECT agent, machine, status, task,
                       extract(epoch FROM (now() - last_seen))::int AS age_s,
                       GREATEST(
                         similarity(lower(COALESCE(task,'')), lower($1)),
                         ts_rank(to_tsvector('simple', COALESCE(task,'')),
                                 plainto_tsquery('simple', $1)) * 4
                       ) AS score
                FROM agent_presence
                WHERE last_seen > now() - interval '30 minutes'
                  AND task IS NOT NULL
                ORDER BY score DESC
                LIMIT 3
                """,
                q,
            )
        colleagues = [
            {"agent": r["agent"], "machine": r["machine"],
             "status": r["status"], "task": r["task"], "age_s": r["age_s"]}
            for r in col_rows if (r["score"] or 0) > 0.12
        ]
    except Exception:
        pass

    return JSONResponse({"cards": cards, "memories": memories,
                         "errors": errors, "procedures": procedures,
                         "colleagues": colleagues})


async def api_library(request: Request) -> JSONResponse:
    """Document registry (library layer, 2026-07-27).

    Groups chunked memories into DOCUMENTS — the answer to "chunk soup":
    each row is one source file (study doc, Affine memo, markdown note)
    with its chunk count, size, and ordered chunk ids. Filter with ?q=
    (substring on the filename) and ?type= (source_type).
    """
    q = request.query_params.get("q", "").strip()
    stype = request.query_params.get("type", "").strip()
    types = [stype] if stype else list(settings.library_source_types)

    from nobrainr.db.pool import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT COALESCE(metadata->>'file_path', source_ref, 'untitled') AS ref,
                   source_type,
                   count(*) AS chunks,
                   sum(length(content)) AS chars,
                   min(created_at) AS imported_at,
                   (array_agg(summary ORDER BY created_at, id))[1] AS first_summary
            FROM memories
            WHERE source_type = ANY($1::text[])
              AND superseded_by IS NULL
              AND ($2 = '' OR COALESCE(metadata->>'file_path', source_ref, '') ILIKE '%' || $2 || '%')
            GROUP BY 1, 2
            ORDER BY 1
            """,
            types, q,
        )
    base = settings.library_original_base_url
    docs = [
        {
            "ref": r["ref"], "source_type": r["source_type"],
            "chunks": r["chunks"], "chars": r["chars"],
            "imported_at": str(r["imported_at"]),
            "first_summary": r["first_summary"],
            "original_url": (base + r["ref"]) if base else None,
        }
        for r in rows
    ]
    return JSONResponse({"documents": docs, "count": len(docs)})


async def api_library_doc(request: Request) -> JSONResponse:
    """One document, whole: ordered chunks stitched for the reader view.

    Chunk order falls back to (created_at, id) — the 2026-03 docx import
    predates per-chunk index metadata, and insertion order tracked the
    original document order.
    """
    ref = request.query_params.get("ref", "").strip()
    if not ref:
        return JSONResponse({"error": "ref required"}, status_code=400)
    from nobrainr.db.pool import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, content, summary, created_at, trust_score
            FROM memories
            WHERE source_type = ANY($1::text[])
              AND superseded_by IS NULL
              AND COALESCE(metadata->>'file_path', source_ref, '') = $2
            ORDER BY created_at, id
            """,
            list(settings.library_source_types), ref,
        )
    if not rows:
        return JSONResponse({"error": "document not found"}, status_code=404)
    base = settings.library_original_base_url
    return JSONResponse({
        "ref": ref,
        "original_url": (base + ref) if base else None,
        "chunks": [
            {"id": str(r["id"]), "content": r["content"],
             "summary": r["summary"], "trust_score": r["trust_score"]}
            for r in rows
        ],
    })


def _library_scope_filter(hits: list[dict], ref: str) -> list[dict]:
    """Restrict hybrid hits to library source types (and one doc if ref).

    metadata arrives as dict OR JSON string depending on the path —
    normalize before reading file_path (the 2026-07-09 lesson).
    """
    import json as _json

    def _fp(h: dict):
        m = h.get("metadata")
        if isinstance(m, str):
            try:
                m = _json.loads(m)
            except Exception:
                m = {}
        return (m or {}).get("file_path") or h.get("source_ref")

    out = [h for h in hits if h.get("source_type") in settings.library_source_types]
    if ref:
        out = [h for h in out if _fp(h) == ref]
    return out


async def api_library_search(request: Request) -> JSONResponse:
    """Doc-scoped hybrid search — 'find it in MY documents, fast'.

    Same embed+hybrid as smart-recall but restricted to library source
    types, optionally to ONE document (?ref=), and reranked by the
    cross-encoder (the M2 lesson: hybrid rank alone is noisy — the
    pinned GPU reranker is what makes the top hits precise).
    """
    q = request.query_params.get("q", "").strip()
    if not q:
        return JSONResponse({"hits": []})
    ref = request.query_params.get("ref", "").strip()
    limit = _int_param(request, "limit", 8, hi=20)

    try:
        embedding = await embed_text(q)
    except Exception:
        return JSONResponse({"hits": [], "error": "embedding unavailable"}, status_code=503)
    hits = await queries.search_memories(
        embedding=embedding, limit=limit * 4, threshold=0.2, text_query=q,
        source_type=list(settings.library_source_types),
    )
    scoped = _library_scope_filter(hits, ref)  # ref-scope + safety net
    try:
        from nobrainr.services.reranker import rerank
        scoped = await rerank(q, scoped, limit=limit)
    except Exception:
        scoped = scoped[:limit]
    return JSONResponse({"hits": scoped[:limit], "count": len(scoped[:limit])})


async def api_memory_source(request: Request) -> JSONResponse:
    """Resolve a memory's raw source — the "view origin" link.

    Returns whatever URL / file / conversation the memory was extracted from
    so the user can open the raw context. Different shapes per source_type:
      - docx → {kind:'docx', file:<source_ref>, url:'/api/files/docx/<ref>'}
      - affine_memos → {kind:'affine', file:<source_ref>}
      - crawl → {kind:'web', url:<source_ref>}
      - github → {kind:'github', ref:<source_ref>, url:'https://github.com/<owner>/<repo>'}
      - chatgpt / claude → {kind:'thread', conversation_id, url:'/api/conversations/<id>'}
      - sticky_notes / session / manual → {kind:'inline', content:<excerpt>}
    """
    memory_id = request.path_params["memory_id"]
    if not _valid_uuid(memory_id):
        return JSONResponse({"error": "Invalid memory_id"}, status_code=400)
    mem = await queries.get_memory(memory_id)
    if not mem:
        return JSONResponse({"error": "Not found"}, status_code=404)

    source_type = mem.get("source_type") or "unknown"
    source_ref = mem.get("source_ref")
    metadata = mem.get("metadata") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except Exception:
            metadata = {}

    if source_type == "docx" and source_ref:
        return JSONResponse({
            "kind": "docx", "file": source_ref,
            "url": f"/api/files/docx/{source_ref}",
        })
    if source_type == "affine_memos" and source_ref:
        return JSONResponse({
            "kind": "affine", "file": source_ref,
            "url": f"/api/files/affine/{source_ref}",
        })
    if source_type == "crawl" and source_ref:
        return JSONResponse({"kind": "web", "url": source_ref})
    if source_type == "github" and source_ref:
        # Format like "github:owner/repo/path"
        gh_url = source_ref
        if source_ref.startswith("github:"):
            parts = source_ref[len("github:"):].split("/", 2)
            if len(parts) >= 2:
                gh_url = f"https://github.com/{parts[0]}/{parts[1]}"
                if len(parts) == 3:
                    gh_url += f"/blob/main/{parts[2]}"
        return JSONResponse({"kind": "github", "ref": source_ref, "url": gh_url})
    if source_type in ("chatgpt", "claude", "claude_web"):
        conv_id = metadata.get("conversation_id") if isinstance(metadata, dict) else None
        if conv_id:
            return JSONResponse({
                "kind": "thread", "conversation_id": conv_id,
                "url": f"/api/conversations/{conv_id}",
            })
    # Fallback: show source_ref as plain string
    return JSONResponse({
        "kind": "inline",
        "source_type": source_type,
        "source_ref": source_ref,
        "content": (mem.get("content") or "")[:2000],
    })


async def api_files_docx(request: Request) -> Response:
    """Serve a docx file from the /opt or import directory.

    Read-only; only files under known import roots are served (no path
    traversal). Returns the raw .docx bytes with proper MIME type.
    """
    import os
    fname = request.path_params["filename"]
    if "/" in fname or ".." in fname:
        return Response("Bad filename", status_code=400)
    candidates = [
        f"/tmp/docx_remaining/{fname}",
        f"/data/imports/docx/{fname}",
        f"/app/imports/docx/{fname}",
    ]
    for path in candidates:
        if os.path.exists(path):
            with open(path, "rb") as fh:
                return Response(
                    fh.read(),
                    media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    headers={"Content-Disposition": f'inline; filename="{fname}"'},
                )
    return Response("Not found", status_code=404)


async def api_files_affine(request: Request) -> Response:
    """Serve an affine memo markdown file (read-only)."""
    import os
    fname = request.path_params["filename"]
    if "/" in fname or ".." in fname:
        return Response("Bad filename", status_code=400)
    candidates = [
        f"/tmp/memos_extracted/{fname}",
        f"/data/imports/affine/{fname}",
        f"/app/imports/affine/{fname}",
    ]
    for path in candidates:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                return Response(fh.read(), media_type="text/markdown; charset=utf-8")
    return Response("Not found", status_code=404)


async def api_memory_click(request: Request) -> JSONResponse:
    """Record a click as auto-positive feedback.

    Fires when user opens/expands a search result. Writes was_useful=true
    with context='auto:click' so the feedback loop has positive signal
    that comes from real engagement, not just absence of negative.
    Body: {query_trace_id, search_rank, query_text} — all optional but
    recommended.
    """
    memory_id = request.path_params["memory_id"]
    if not _valid_uuid(memory_id):
        return JSONResponse({"error": "Invalid memory_id"}, status_code=400)
    try:
        body = await request.json()
    except Exception:
        body = {}
    query_trace_id = body.get("query_trace_id")
    query_text = body.get("query_text")
    result_rank = body.get("search_rank") or body.get("result_rank")
    if isinstance(result_rank, str):
        try:
            result_rank = int(result_rank)
        except ValueError:
            result_rank = None

    result = await queries.store_memory_outcome(
        memory_id,
        True,
        context="auto:click",
        agent_id="auto-click-dashboard",
        query_trace_id=query_trace_id,
        query_text=query_text,
        result_rank=result_rank,
    )
    return JSONResponse(result)


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


async def api_graph_search(request: Request) -> JSONResponse:
    """GET /api/graph/search?q= — hybrid entity search for the graph view.

    Two legs fused with Reciprocal Rank Fusion:
      lexical  — pg_trgm similarity on canonical_name (typo-tolerant
                 fuzzy name match, GIN-indexed, ~7ms)
      semantic — pgvector cosine over entity embeddings via the halfvec
                 HNSW index (finds conceptually related entities that
                 share no keyword with the query)

    Returns ranked entity ids; the client highlights whichever of them
    are present in the rendered graph.
    """
    q = request.query_params.get("q", "").strip()
    if len(q) < 2:
        return JSONResponse({"results": []})
    try:
        limit = min(int(request.query_params.get("limit", "40")), 100)
    except ValueError:
        limit = 40

    from nobrainr.db.pool import get_pool
    pool = await get_pool()

    # Lexical leg — trigram similarity, typo-tolerant
    async with pool.acquire() as conn:
        lex_rows = await conn.fetch(
            """
            SELECT id::text, canonical_name, entity_type,
                   similarity(canonical_name, $1) AS sim
            FROM entities
            WHERE canonical_name % $1 OR canonical_name ILIKE '%' || $1 || '%'
            ORDER BY sim DESC NULLS LAST
            LIMIT 40
            """,
            q,
        )

    # Semantic leg — embed the query, cosine over the HNSW index
    sem_rows: list = []
    try:
        embedding = await embed_text_with_timeout(q, timeout_s=8.0)
        async with pool.acquire() as conn:
            sem_rows = await conn.fetch(
                """
                SELECT id::text, canonical_name, entity_type,
                       1 - (embedding::halfvec(1024) <=> $1::halfvec(1024)) AS sim
                FROM entities
                WHERE embedding IS NOT NULL
                ORDER BY embedding::halfvec(1024) <=> $1::halfvec(1024)
                LIMIT 40
                """,
                embedding,  # raw list — the pool's pgvector codec encodes it
            )
    except EmbedTimeout:
        log.warning("graph search: embed timed out — lexical-only results")
    except Exception:
        log.exception("graph search: semantic leg failed")

    # Reciprocal Rank Fusion (k=60) — robust to incomparable score scales
    K = 60
    fused: dict[str, dict] = {}
    for rank, row in enumerate(lex_rows):
        e = fused.setdefault(row["id"], {
            "id": row["id"], "name": row["canonical_name"],
            "type": row["entity_type"], "score": 0.0, "via": set(),
        })
        e["score"] += 1.0 / (K + rank + 1)
        e["via"].add("name")
    for rank, row in enumerate(sem_rows):
        # Drop weak semantic tails — below ~0.45 cosine the matches are noise
        if row["sim"] is not None and row["sim"] < 0.45:
            continue
        e = fused.setdefault(row["id"], {
            "id": row["id"], "name": row["canonical_name"],
            "type": row["entity_type"], "score": 0.0, "via": set(),
        })
        e["score"] += 1.0 / (K + rank + 1)
        e["via"].add("semantic")

    results = sorted(fused.values(), key=lambda e: -e["score"])[:limit]
    return JSONResponse({
        "results": [
            {"id": e["id"], "name": e["name"], "type": e["type"],
             "score": round(e["score"], 5), "via": sorted(e["via"])}
            for e in results
        ],
    })


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


# The full tag list is ~1.4 MB (every per-message routing tag counts as a tag)
# and took 1-4s; a filter dropdown needs the common ones and a way to find the
# rest. Default: the 2000 most used. ``q`` searches all tags by substring,
# ``limit=0`` returns everything (old behaviour).
_TAGS_STANDARD = 2000
_TAGS_TTL_S = 60.0
_tags_cache: dict = {}


async def api_tags(request: Request) -> JSONResponse:
    """Tags with usage counts for filter dropdowns, most used first.

    Query params: ``limit`` (default 2000, 0 = all), ``q`` (substring search).
    """
    q = request.query_params.get("q", "").strip() or None
    try:
        limit = int(request.query_params.get("limit", str(_TAGS_STANDARD)))
    except ValueError:
        return JSONResponse({"error": "Invalid limit"}, status_code=400)
    limit = None if limit <= 0 else min(limit, 50000)

    schluessel = (q, limit)
    treffer = _tags_cache.get(schluessel)
    if treffer is not None and time.monotonic() - treffer[0] < _TAGS_TTL_S:
        return JSONResponse(treffer[1])
    tags = await queries.get_all_tags(limit=limit, q=q)
    if len(_tags_cache) > 64:
        _tags_cache.clear()
    _tags_cache[schluessel] = (time.monotonic(), tags)
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


# ── LLM queue status ──────────────────────────────────────────────────────
# Pollable by any frontend (other apps, OpenWebUI) to show "you are Nth in
# line" instead of an opaque spinner. Added 2026-09-06.
#
# Signals are aggregated from llama-swap because no single endpoint has them:
#   GET /running                     -> which models are resident, and their state
#   GET /upstream/<model>/slots      -> per-slot is_processing (the actual queue)
# llama-swap's own /metrics carries no queue depth, and /slots is 404 at the
# top level — it must be addressed per-model via /upstream/<model>/.
#
# The GPU chat slot is EXCLUSIVE: only one of the chat-group models is resident
# at a time and requesting another evicts it (~35s cold load). That swap is
# invisible in slot counts, so it is surfaced separately as `swap_pending` —
# a frontend showing only "0 busy" during a swap would be lying to the user.
_QUEUE_CACHE: dict = {"ts": 0.0, "data": None}
_QUEUE_TTL = 2.0  # seconds; frontends may poll hard, llama-swap should not wear it


async def api_llm_queue(request: Request) -> JSONResponse:
    """GET /api/llm-queue — aggregated LLM slot/queue state for frontends."""
    now = time.monotonic()
    if _QUEUE_CACHE["data"] is not None and (now - _QUEUE_CACHE["ts"]) < _QUEUE_TTL:
        return JSONResponse(_QUEUE_CACHE["data"])

    base = settings.llm_server_url.rstrip("/")
    out: dict = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "backend": base,
        "models": {},
        "resident": None,
        "swap_pending": False,
        "busy_total": 0,
        "capacity_total": 0,
        "error": None,
    }

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{base}/running")
            r.raise_for_status()
            running = r.json().get("running", [])

            for entry in running:
                model = entry.get("model")
                state = entry.get("state")
                if not model:
                    continue
                # "ready" is resident and serving; anything else (loading,
                # stopping) means a swap is in flight and requests will block.
                if state != "ready":
                    out["swap_pending"] = True
                # Tier matters to a frontend: the GPU chat slot is the only
                # CONTENDED resource (exclusive group, one model resident at a
                # time). The CPU model runs independently and is never evicted,
                # so folding its slots into one capacity number would tell a
                # user "3 free" when the GPU they actually need is full.
                tier = "cpu" if model.endswith("-cpu") else (
                    "embed" if ("embed" in model or "rerank" in model) else "gpu_chat")
                info = {"state": state, "tier": tier,
                        "slots_total": None, "slots_busy": None}

                # Embedding/rerank servers have no chat slots — skip the probe
                # rather than emit misleading nulls for them. The upstream's
                # own port, never /upstream/<model>/ (that route loads the
                # model; a model stopping between /running and here came back).
                slots_url = _direct_slots_url(entry.get("proxy")) if state == "ready" else None
                if slots_url and "embed" not in model and "rerank" not in model:
                    try:
                        sr = await client.get(slots_url)
                        if sr.status_code == 200:
                            slots = sr.json()
                            if isinstance(slots, list):
                                info["slots_total"] = len(slots)
                                info["slots_busy"] = sum(
                                    1 for sl in slots if sl.get("is_processing")
                                )
                                info["ctx_per_slot"] = slots[0].get("n_ctx") if slots else None
                                if tier == "gpu_chat":
                                    out["capacity_total"] += info["slots_total"]
                                    out["busy_total"] += info["slots_busy"]
                                    if state == "ready":
                                        out["resident"] = model
                                else:
                                    out.setdefault("cpu_slots_total", 0)
                                    out.setdefault("cpu_slots_busy", 0)
                                    out["cpu_slots_total"] += info["slots_total"]
                                    out["cpu_slots_busy"] += info["slots_busy"]
                    except Exception:
                        # A model can be evicted between /running and this probe.
                        # Report what we know instead of failing the whole call.
                        pass
                out["models"][model] = info
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"[:160]

    # Everything below is about the GPU chat slot specifically — see tier note.
    free = max(0, out["capacity_total"] - out["busy_total"])
    out["slots_free"] = free
    # queue_depth is what a frontend renders. llama.cpp does not expose a
    # pending-request count, so this is capacity pressure, not a true backlog:
    # 0 free slots means the next request waits, and a swap means it waits ~35s.
    out["queue_depth"] = 0 if free > 0 else 1
    out["accepting_now"] = free > 0 and not out["swap_pending"]
    out["hint"] = (
        "swapping model (~35s)" if out["swap_pending"]
        else ("ready" if free > 0 else "all slots busy")
    )

    # nobrainr's own write backlog, so one poll answers "is anything queued
    # anywhere" rather than the frontend hitting two endpoints.
    try:
        from nobrainr.db.pool import get_pool
        pool = await get_pool()
        async with pool.acquire() as conn:
            pending = await conn.fetchval(
                "SELECT count(*) FROM memory_write_queue "
                "WHERE status IN ('pending','processing')"
            )
        out["write_queue_pending"] = int(pending or 0)
    except Exception:
        out["write_queue_pending"] = None

    _QUEUE_CACHE["ts"] = now
    _QUEUE_CACHE["data"] = out
    return JSONResponse(out)

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


async def api_insights(request: Request) -> JSONResponse:
    """GET /api/insights — surface synthesis memories (LLM-generated insights).

    The synthesis scheduler job has been producing insight memories at
    source_type='synthesis' for months but they were invisible. This endpoint
    returns the most recent / highest-confidence ones so the UI can show:
    - "Insight of the day" card
    - Insights tab in commonplace
    - Top items in daily email

    Filters: ?since=24h|7d|30d (default 7d), ?limit (default 20),
    ?min_confidence (default 0.5).
    """
    try:
        limit = max(1, min(100, int(request.query_params.get("limit", 20))))
    except ValueError:
        limit = 20
    try:
        min_conf = float(request.query_params.get("min_confidence", 0.5))
    except ValueError:
        min_conf = 0.5
    since = request.query_params.get("since", "7d")
    interval = {"24h": "1 day", "7d": "7 days", "30d": "30 days"}.get(since, "7 days")

    from nobrainr.db.pool import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT id, content, summary, tags, category, confidence, importance,
                   created_at, metadata, quality_score
            FROM memories
            WHERE source_type = 'synthesis'
              AND category != '_archived'
              AND superseded_by IS NULL
              AND created_at > NOW() - INTERVAL '{interval}'
              AND COALESCE(confidence, 0.7) >= $1
            ORDER BY COALESCE(quality_score, 0.5) DESC NULLS LAST,
                     confidence DESC NULLS LAST,
                     created_at DESC
            LIMIT $2
            """,
            min_conf, limit,
        )
    out = []
    for r in rows:
        d = dict(r)
        d["id"] = str(d["id"])
        if d.get("created_at") and hasattr(d["created_at"], "isoformat"):
            d["created_at"] = d["created_at"].isoformat()
        if isinstance(d.get("metadata"), str):
            try:
                d["metadata"] = json.loads(d["metadata"])
            except Exception:
                pass
        out.append(d)
    return JSONResponse(out)


async def api_insight_of_the_day(request: Request) -> JSONResponse:
    """GET /api/insights/today — single insight card for dashboard home.

    Strong recency bias: 24h candidates score 3x, 7d 1.5x, 30d 1.0x, older
    excluded. Filters out trivial synthesis summaries (length<=24 chars).
    Daily-stable shuffle: same insight stays for the calendar day so the
    card doesn't refresh-flicker, but changes naturally the next day.
    """
    from nobrainr.db.pool import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, content, summary, tags, category, confidence,
                   created_at, metadata, quality_score
            FROM memories
            WHERE source_type = 'synthesis'
              AND category != '_archived'
              AND superseded_by IS NULL
              AND created_at > NOW() - INTERVAL '30 days'
              AND length(COALESCE(content, '')) > 80
            ORDER BY
              -- recency multiplier
              CASE
                WHEN created_at > NOW() - INTERVAL '24 hours' THEN 3.0
                WHEN created_at > NOW() - INTERVAL '7 days'   THEN 1.5
                ELSE 1.0
              END
              -- score blend: confidence (0.7-1.0 for synth) + quality if scored
              * (COALESCE(confidence, 0.7) + COALESCE(quality_score, 0.3))
              -- daily-stable hash → same pick across the calendar day, changes next day
              * (1.0 + 0.4 * (
                  abs(hashtext(id::text || to_char(now() at time zone 'utc', 'YYYY-MM-DD')))
                  % 1000
                ) / 1000.0)
              DESC
            LIMIT 1
            """
        )
    if not row:
        return JSONResponse({"insight": None})
    d = dict(row)
    d["id"] = str(d["id"])
    if d.get("created_at") and hasattr(d["created_at"], "isoformat"):
        d["created_at"] = d["created_at"].isoformat()
    if isinstance(d.get("metadata"), str):
        try:
            d["metadata"] = json.loads(d["metadata"])
        except Exception:
            pass
    return JSONResponse({"insight": d})


async def api_conversations_search(request: Request) -> JSONResponse:
    """GET /api/conversations — two-layer commonplace: search raw threads.

    Hybrid vector + FTS search over conversations_raw. Returns thread
    metadata (id, source_type, title, message_count). Use
    /api/conversations/{id} to open the full thread.
    """
    q = request.query_params.get("q", "").strip()
    source_type = request.query_params.get("source_type") or None
    try:
        limit = max(1, min(100, int(request.query_params.get("limit", 20))))
    except ValueError:
        limit = 20

    if not q:
        # No query = recent threads
        from nobrainr.db.pool import get_pool
        pool = await get_pool()
        async with pool.acquire() as conn:
            args: list = [limit]
            where = ""
            if source_type:
                args.insert(0, source_type)
                where = "WHERE source_type = $1"
                args[0], args[1] = args[0], args[1]  # already correct
                rows = await conn.fetch(
                    f"SELECT id, source_type, title, message_count, imported_at "
                    f"FROM conversations_raw {where} "
                    f"ORDER BY imported_at DESC LIMIT $2",
                    source_type, limit,
                )
            else:
                rows = await conn.fetch(
                    "SELECT id, source_type, title, message_count, imported_at "
                    "FROM conversations_raw "
                    "ORDER BY imported_at DESC LIMIT $1",
                    limit,
                )
        return JSONResponse([{
            "id": str(r["id"]),
            "source_type": r["source_type"],
            "title": r["title"],
            "message_count": r["message_count"],
            "imported_at": r["imported_at"].isoformat() if r["imported_at"] else None,
        } for r in rows])

    embedding: list[float] | None = None
    try:
        embedding = await embed_text_with_timeout(q, timeout_s=15.0)
    except (EmbedTimeout, Exception):
        embedding = None

    results = await queries.search_conversations_raw(
        embedding=embedding, text_query=q, limit=limit, source_type=source_type,
    )
    # JSON-serialize datetimes
    for r in results:
        if r.get("imported_at") and hasattr(r["imported_at"], "isoformat"):
            r["imported_at"] = r["imported_at"].isoformat()
    return JSONResponse(results)


async def api_conversation_detail(request: Request) -> JSONResponse:
    """GET /api/conversations/{conv_id} — full thread for the Threads UI."""
    conv_id = request.path_params["conv_id"]
    if not _valid_uuid(conv_id):
        return JSONResponse({"error": "Invalid conversation_id"}, status_code=400)
    conv = await queries.get_conversation_raw(conv_id)
    if not conv:
        return JSONResponse({"error": "Not found"}, status_code=404)
    if conv.get("imported_at") and hasattr(conv["imported_at"], "isoformat"):
        conv["imported_at"] = conv["imported_at"].isoformat()
    if isinstance(conv.get("messages"), str):
        try:
            conv["messages"] = json.loads(conv["messages"])
        except Exception:
            pass
    if isinstance(conv.get("metadata"), str):
        try:
            conv["metadata"] = json.loads(conv["metadata"])
        except Exception:
            pass
    return JSONResponse(conv)


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
_GALAXY_CACHE_GZ_PATH = "/tmp/nobrainr_galaxy_cache.json.gz"
_galaxy_recomputing = False


def _galaxy_cached_response(request: Request) -> Response:
    """Serve the galaxy cache as raw bytes — no json parse/re-serialize.

    Serves the pre-compressed .gz variant when the client accepts gzip
    (browsers always do); falls back to plain JSON bytes otherwise.
    """
    import os

    accept = request.headers.get("accept-encoding", "")
    if "gzip" in accept and os.path.exists(_GALAXY_CACHE_GZ_PATH):
        with open(_GALAXY_CACHE_GZ_PATH, "rb") as f:
            return Response(
                f.read(),
                media_type="application/json",
                headers={"Content-Encoding": "gzip", "Vary": "Accept-Encoding"},
            )
    with open(_GALAXY_CACHE_PATH, "rb") as f:
        return Response(f.read(), media_type="application/json")


async def api_galaxy(request: Request) -> JSONResponse:
    """3D galaxy visualization data — PCA-reduced embeddings for all memories.

    Returns flat arrays for efficient Three.js InstancedBufferGeometry consumption.

    Stale-while-revalidate: ANY existing cache is served immediately; if it
    is older than the TTL, a background recompute refreshes it for the next
    request. A user only ever blocks on the full UMAP when no cache exists
    at all (first request after a redeploy, before the +10min pre-warm).
    """
    import os
    import time as _time

    global _galaxy_recomputing

    cache_ttl = 1800  # 30 minutes
    force = request.query_params.get("refresh", "").lower() == "true"
    limit = _int_param(request, "limit", 10000, hi=50000)

    if not force and os.path.exists(_GALAXY_CACHE_PATH):
        age = _time.time() - os.path.getmtime(_GALAXY_CACHE_PATH)
        if age >= cache_ttl and not _galaxy_recomputing:
            # Serve stale now, refresh in the background.
            _galaxy_recomputing = True

            async def _refresh() -> None:
                global _galaxy_recomputing
                try:
                    await _compute_galaxy(limit)
                    log.info("Galaxy: background SWR refresh complete")
                except Exception:
                    log.exception("Galaxy: background SWR refresh failed")
                finally:
                    _galaxy_recomputing = False

            asyncio.create_task(_refresh())
        return _galaxy_cached_response(request)

    result = await _compute_galaxy(limit)
    if not os.path.exists(_GALAXY_CACHE_PATH):
        # Empty DB — compute returned without writing a cache file.
        return JSONResponse(result)
    return _galaxy_cached_response(request)


async def _compute_galaxy(limit: int) -> dict:
    """Run the embeddings → PCA → UMAP pipeline and write the cache file."""
    import json

    from nobrainr.db.pool import get_pool

    pool = await get_pool()

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
        return {"count": 0, "positions": [], "categories": [], "ids": []}

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

    # Flatten for JSON transfer. Coordinates are normalized to [-1, 1];
    # 4 decimals is sub-pixel at any zoom and roughly halves the JSON size
    # vs full float repr.
    positions = [round(float(v), 4) for v in coords.flatten()]

    result = {
        "count": count,
        "positions": positions,
        "categories": categories,
        "tiers": tiers,
        "ids": ids,
        "summaries": summaries,
        "importances": [round(v, 3) for v in importances],
    }

    # Cache — plain JSON plus a pre-compressed gz twin so cached requests
    # are a raw byte read (no parse, no re-serialize, no per-request gzip).
    try:
        import gzip as _gzip

        payload = json.dumps(result, separators=(",", ":")).encode()
        with open(_GALAXY_CACHE_PATH, "wb") as f:
            f.write(payload)
        with open(_GALAXY_CACHE_GZ_PATH, "wb") as f:
            f.write(_gzip.compress(payload, 6))
    except Exception:
        pass

    return result


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
        # Shared CTE prefix. The old version computed top_names as a
        # correlated subquery per community — 4.2s of a 4.4s endpoint.
        # The window-function rewrite does one pass over entities (31ms).
        # entity/memory counts are split into separate CTEs so the planner
        # avoids a double COUNT(DISTINCT) sort over the em join.
        _CTE_PREFIX = """
                WITH name_max AS (
                    SELECT community_id, canonical_name, MAX(mention_count) AS mc
                    FROM entities
                    WHERE community_id IS NOT NULL AND canonical_name IS NOT NULL
                    GROUP BY community_id, canonical_name
                ),
                ranked AS (
                    SELECT community_id, canonical_name,
                           ROW_NUMBER() OVER (
                               PARTITION BY community_id ORDER BY mc DESC
                           ) AS rn
                    FROM name_max
                ),
                top_names AS (
                    SELECT community_id,
                           string_agg(canonical_name, ' · ' ORDER BY rn) AS names
                    FROM ranked WHERE rn <= 3
                    GROUP BY community_id
                ),
                ent_counts AS (
                    SELECT community_id, COUNT(*) AS entity_count
                    FROM entities
                    WHERE community_id IS NOT NULL
                    GROUP BY community_id
                ),
                mem_counts AS (
                    SELECT e.community_id,
                           COUNT(DISTINCT em.memory_id) AS memory_count
                    FROM entities e
                    JOIN entity_memories em ON em.entity_id = e.id
                    WHERE e.community_id IS NOT NULL
                    GROUP BY e.community_id
                    HAVING COUNT(DISTINCT em.memory_id) > 0
                )
        """
        if q:
            embedding = await embed_text(q)
            # Semantic search on community_summaries where embedding exists,
            # fall back to all entity-communities ordered by memory_count
            rows = await conn.fetch(
                _CTE_PREFIX + """
                SELECT mc.community_id,
                       COALESCE(cs.title, tn.names) AS title,
                       COALESCE(cs.summary, '') AS summary,
                       COALESCE(cs.key_topics, ARRAY[]::text[]) AS key_topics,
                       ec.entity_count AS member_count,
                       cs.updated_at,
                       mc.memory_count,
                       CASE WHEN cs.embedding IS NOT NULL
                           THEN 1 - (cs.embedding <=> $1::vector)
                           ELSE NULL END AS score
                FROM mem_counts mc
                JOIN ent_counts ec ON ec.community_id = mc.community_id
                LEFT JOIN top_names tn ON tn.community_id = mc.community_id
                LEFT JOIN community_summaries cs ON cs.community_id = mc.community_id
                ORDER BY
                    CASE WHEN cs.embedding IS NOT NULL
                         THEN cs.embedding <=> $1::vector ELSE 1.0 END ASC,
                    mc.memory_count DESC
                LIMIT $2
                """,
                str(embedding),
                limit,
            )
        else:
            rows = await conn.fetch(
                _CTE_PREFIX + """
                SELECT mc.community_id,
                       COALESCE(cs.title, tn.names) AS title,
                       COALESCE(cs.summary, '') AS summary,
                       COALESCE(cs.key_topics, ARRAY[]::text[]) AS key_topics,
                       ec.entity_count AS member_count,
                       cs.updated_at,
                       mc.memory_count,
                       NULL::float AS score
                FROM mem_counts mc
                JOIN ent_counts ec ON ec.community_id = mc.community_id
                LEFT JOIN top_names tn ON tn.community_id = mc.community_id
                LEFT JOIN community_summaries cs ON cs.community_id = mc.community_id
                ORDER BY mc.memory_count DESC
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


async def api_redistill_progress(request: Request) -> JSONResponse:
    """Progress of the qwen3.6-27b full re-distill campaign (2026-08-11):
    counts, learnings, throughput from distilled_at stamps, and an ETA.
    Rate window is the last 6h of completions so pauses (GPU yielded to
    live traffic) age out of the estimate instead of skewing it forever."""
    from nobrainr.db.pool import get_pool

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
              count(*) AS total,
              count(*) FILTER (WHERE (metadata->>'redistill')::bool) AS pending,
              count(*) FILTER (WHERE metadata->>'distilled' = 'true'
                               AND metadata->>'redistill' IS NULL) AS done,
              count(*) FILTER (WHERE metadata ? 'distill_progress') AS in_flight,
              COALESCE(sum((metadata->>'learning_count')::int)
                       FILTER (WHERE metadata->>'distilled' = 'true'), 0) AS learnings,
              min((metadata->>'distilled_at')::timestamptz) AS first_done_at,
              max((metadata->>'distilled_at')::timestamptz) AS last_done_at,
              count(*) FILTER (
                  WHERE (metadata->>'distilled_at')::timestamptz
                        > now() - interval '6 hours') AS done_6h
            FROM conversations_raw
            """,
        )

    total, pending, done = row["total"], row["pending"], row["done"]
    rate_per_hour = round(row["done_6h"] / 6.0, 2)
    eta_hours = round(pending / rate_per_hour, 1) if rate_per_hour > 0 else None
    elapsed_hours = None
    if row["first_done_at"]:
        elapsed_hours = round(
            (datetime.now(UTC) - row["first_done_at"]).total_seconds() / 3600, 1
        )
    return JSONResponse({
        "total": total,
        "done": done,
        "pending": pending,
        "in_flight": row["in_flight"],
        "pct": round(100.0 * done / total, 1) if total else 0.0,
        "learnings": row["learnings"],
        "rate_per_hour": rate_per_hour,
        "eta_hours": eta_hours,
        "elapsed_hours": elapsed_hours,
        "first_done_at": row["first_done_at"].isoformat() if row["first_done_at"] else None,
        "last_done_at": row["last_done_at"].isoformat() if row["last_done_at"] else None,
    })


async def api_presence(request: Request) -> JSONResponse:
    """Fleet presence roster (agent-comms layer, 2026-08-15). GET lists
    active agents; POST /api/presence/beat lets session-start hooks
    announce without an MCP round-trip."""
    from nobrainr.db.queries import list_agent_presence

    try:
        window = min(int(request.query_params.get("window_s", "300")), 86400)
    except ValueError:
        window = 300
    roster = await list_agent_presence(active_within_s=window)
    for r in roster:
        r["last_seen"] = r["last_seen"].isoformat()
    return JSONResponse({"active": roster, "count": len(roster)})


async def api_presence_beat(request: Request) -> JSONResponse:
    from nobrainr.db.queries import upsert_agent_presence

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "json body required"}, status_code=400)
    agent = (body.get("agent") or "").strip()
    machine = (body.get("machine") or "").strip()
    if not agent or not machine:
        return JSONResponse({"error": "agent and machine required"}, status_code=400)
    await upsert_agent_presence(
        agent, machine,
        status=(body.get("status") or "active")[:40],
        task=body.get("task"),
        session_ref=body.get("session_ref"),
    )
    return JSONResponse({"ok": True})


api_routes = [
    Route("/api/transcribe", api_transcribe, methods=["POST"]),
    Route("/api/tts", api_tts, methods=["POST"]),
    Route("/api/chat", api_chat, methods=["POST"]),
    Route("/api/galaxy", api_galaxy),
    Route("/api/graph", api_graph),
    Route("/api/graph/live", api_graph_live),
    Route("/api/graph/communities", api_graph_communities),
    Route("/api/graph/search", api_graph_search),
    Route("/api/memories", api_memories),
    Route("/api/memories/{memory_id}", api_memory_detail, methods=["GET"]),
    Route("/api/memories/{memory_id}", api_memory_update, methods=["POST"]),
    Route("/api/memories/{memory_id}", api_memory_delete, methods=["DELETE"]),
    Route("/api/memories/{memory_id}/feedback", api_memory_feedback, methods=["POST"]),
    Route("/api/memories/{memory_id}/click", api_memory_click, methods=["POST"]),
    Route("/api/memories/{memory_id}/source", api_memory_source, methods=["GET"]),
    Route("/api/files/docx/{filename}", api_files_docx, methods=["GET"]),
    Route("/api/files/affine/{filename}", api_files_affine, methods=["GET"]),
    Route("/api/memories/{memory_id}/history", api_memory_history, methods=["GET"]),
    Route("/api/memories/{memory_id}/facts", api_memory_facts, methods=["GET"]),
    Route("/api/memories/{memory_id}/restore", api_memory_restore, methods=["POST"]),
    Route("/api/memories/{memory_id}/origin", api_memory_origin, methods=["GET"]),
    Route("/api/insights", api_insights),
    Route("/api/insights/today", api_insight_of_the_day),
    Route("/api/conversations", api_conversations_search),
    Route("/api/conversations/{conv_id}", api_conversation_detail),
    Route("/api/facts", api_facts_search),
    Route("/api/facts/stats", api_facts_stats),
    Route("/api/queue/{queue_id}/retry", api_queue_retry, methods=["POST"]),
    Route("/api/timeline", api_timeline),
    Route("/api/node/{entity_id}", api_node_detail),
    Route("/api/stats", api_stats),
    Route("/api/llm-queue", api_llm_queue),
    Route("/api/scheduler", api_scheduler),
    Route("/api/redistill-progress", api_redistill_progress),
    Route("/api/presence", api_presence),
    Route("/api/presence/beat", api_presence_beat, methods=["POST"]),
    Route("/api/scheduler/pause", api_scheduler_pause, methods=["POST"]),
    Route("/api/scheduler/resume", api_scheduler_resume, methods=["POST"]),
    Route("/api/scheduler/task/{task_name}", api_scheduler_task_runs, methods=["GET"]),
    Route("/api/recall", api_recall),
    Route("/api/search/preview", api_search_preview),
    Route("/api/smart-recall", api_smart_recall),
    Route("/api/brief", api_brief),
    Route("/api/library", api_library),
    Route("/api/library/doc", api_library_doc),
    Route("/api/library/search", api_library_search),
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


# --- API basics: health, index, uniform errors --------------------------------


async def api_health(request: Request) -> JSONResponse:
    """Liveness: is the process up and does the database answer?

    One ``SELECT 1`` with a 2s limit — no counts, no locks. Made for polling
    (dashboard connection check, container healthcheck, monitors); the
    detailed signals are at /api/health/detailed. 503 when the database
    does not answer.
    """
    from nobrainr.db.pool import get_pool

    db = False
    try:
        pool = await asyncio.wait_for(get_pool(), timeout=2)
        async with pool.acquire(timeout=2) as conn:
            db = await conn.fetchval("SELECT 1", timeout=2) == 1
    except Exception:
        log.warning("health: database did not answer", exc_info=True)
    return JSONResponse({"status": "ok" if db else "degraded", "db": db}, status_code=200 if db else 503)


async def api_index(request: Request) -> JSONResponse:
    """Every route of this API: path, methods, one-line description."""
    eintraege = []
    for route in api_routes:
        if route.path in (_UNBEKANNT_PFAD, "/api/"):
            continue
        doc = (getattr(route.endpoint, "__doc__", None) or "").strip()
        eintraege.append({
            "path": route.path,
            "methods": sorted((route.methods or set()) - {"HEAD"}),
            "summary": doc.split("\n", 1)[0].strip(),
        })
    eintraege.sort(key=lambda e: (e["path"], e["methods"]))
    return JSONResponse({"count": len(eintraege), "routes": eintraege})


async def api_not_found(request: Request) -> JSONResponse:
    """Unknown /api path: a JSON 404 like every other error here (it used to fall through to the MCP app's plain-text 404).

    This route catches everything nothing else took, so a known path asked
    with the wrong method lands here too; that stays a 405 with the methods
    that do exist.
    """
    from starlette.routing import Match

    erlaubt: set[str] = set()
    for route in api_routes:
        if route.path != _UNBEKANNT_PFAD and route.matches(request.scope)[0] == Match.PARTIAL:
            erlaubt |= (route.methods or set()) - {"HEAD"}
    if erlaubt:
        methoden = sorted(erlaubt)
        return JSONResponse({"error": f"Method {request.method} not allowed", "allowed": methoden},
                            status_code=405, headers={"Allow": ", ".join(methoden)})
    return JSONResponse({"error": f"No such route: {request.url.path}", "index": "/api"}, status_code=404)


def _mit_json_fehlern(route: Route) -> Route:
    """Same route, but its failures are JSON.

    A bad parameter (BadParam) is a 400 with the reason. Anything unexpected
    is logged with its traceback and answered as 500 {"error": ...}; it used
    to be Starlette's plain-text "Internal Server Error", on which the
    dashboard's ``response.json()`` threw a second, unrelated error.
    Streaming answers are untouched: only what is raised before the response
    object exists passes through here.
    """
    endpoint = route.endpoint

    async def sicher(request: Request):
        try:
            return await endpoint(request)
        except BadParam as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Unhandled error in %s %s", request.method, request.url.path)
            return JSONResponse({"error": "Internal server error"}, status_code=500)

    sicher.__name__ = getattr(endpoint, "__name__", "endpoint")
    sicher.__doc__ = endpoint.__doc__
    return Route(route.path, sicher, methods=sorted(route.methods) if route.methods else None, name=route.name)


_UNBEKANNT_PFAD = "/api/{rest:path}"
api_routes += [
    Route("/api/health", api_health),
    Route("/api", api_index),
    Route("/api/", api_index),  # the dashboard's nginx only proxies paths under /api/
]
api_routes = [_mit_json_fehlern(r) for r in api_routes]
# Last, so it only sees what nothing above matched.
api_routes.append(Route(_UNBEKANNT_PFAD, api_not_found, methods=["GET", "POST", "PUT", "PATCH", "DELETE"]))
