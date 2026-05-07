"""Graph layout computation using NetworkX — optimized for performance.

Phase K optimizations (2026-04-12):
- Reduced iteration counts with adaptive scaling
- Size-based algorithm selection
- Early termination thresholds
- Cython-compatible NumPy operations where possible
- Batched community processing

Two-level approach:
1. Louvain community detection → many small communities
2. Merge small communities into nearest large one (target: 30-80 clusters)
3. Spring layout on the community meta-graph for centroids
4. Spring layout within each community for local structure
5. Isolates scattered around periphery
"""

import logging
import math
from collections import Counter

import networkx as nx
from networkx.algorithms.community import louvain_communities

logger = logging.getLogger("nobrainr")

MERGE_THRESHOLD = 15  # communities smaller than this get merged

# Performance tuning
META_ITERATIONS_BASE = 200
COMMUNITY_ITERATIONS_BASE = 60
LARGE_COMMUNITY_THRESHOLD = 5000  # only use circular layout for truly enormous communities


def compute_graph_layout(
    nodes: list[dict],
    edges: list[dict],
    *,
    fast_mode: bool = False,
) -> dict:
    """Compute community-grouped layout for graph visualization.

    Args:
        nodes: List of node dicts with data.id.
        edges: List of edge dicts with data.source/target.
        fast_mode: If True, use even fewer iterations (for previews).

    Returns:
        Dict mapping node_id -> {x, y, community}.
    """
    G = nx.Graph()

    node_ids = set()
    for node in nodes:
        nid = node["data"]["id"]
        G.add_node(nid)
        node_ids.add(nid)

    for edge in edges:
        src = edge["data"]["source"]
        tgt = edge["data"]["target"]
        if src in node_ids and tgt in node_ids:
            try:
                G.add_edge(src, tgt)
            except Exception:
                pass

    if len(G) == 0:
        return {}

    # --- Step 1: Community detection ---
    try:
        communities_raw = list(louvain_communities(G, seed=42, resolution=1.0))
    except Exception:
        communities_raw = [set(G.nodes())]

    # Separate isolates from connected nodes
    connected_communities: list[set[str]] = []
    isolate_nodes: list[str] = []
    for comm in communities_raw:
        connected = {n for n in comm if G.degree(n) > 0}
        isolated = [n for n in comm if G.degree(n) == 0]
        if connected:
            connected_communities.append(connected)
        isolate_nodes.extend(isolated)

    if not connected_communities:
        # All isolates — scatter in grid
        return _scatter_grid(list(G.nodes()), community=0)

    # --- Step 2: Merge small communities into nearest large one ---
    merged = _merge_communities(connected_communities, G, MERGE_THRESHOLD)

    # --- Step 3: Build meta-graph and position centroids ---
    node_to_comm: dict[str, int] = {}
    for i, comm in enumerate(merged):
        for n in comm:
            node_to_comm[n] = i

    meta_G = nx.Graph()
    for i in range(len(merged)):
        meta_G.add_node(i, size=len(merged[i]))

    for src, tgt in G.edges():
        c_src = node_to_comm.get(src)
        c_tgt = node_to_comm.get(tgt)
        if c_src is not None and c_tgt is not None and c_src != c_tgt:
            if meta_G.has_edge(c_src, c_tgt):
                meta_G[c_src][c_tgt]["weight"] += 1
            else:
                meta_G.add_edge(c_src, c_tgt, weight=1)

    num_comm = len(merged)
    base_scale = max(8000, 4000 * math.sqrt(num_comm))

    # Adaptive iteration count based on graph size
    meta_iters = META_ITERATIONS_BASE // 2 if fast_mode else META_ITERATIONS_BASE
    if num_comm > 100:
        meta_iters = max(50, meta_iters // 2)

    if num_comm == 1:
        meta_pos: dict[int, tuple[float, float]] = {0: (0.0, 0.0)}
    elif num_comm <= 50:
        # Kamada-Kawai only for small graphs (O(n³) complexity)
        try:
            raw_pos = nx.kamada_kawai_layout(meta_G, scale=base_scale)
            meta_pos = {k: (float(v[0]), float(v[1])) for k, v in raw_pos.items()}
        except Exception:
            raw_pos = nx.spring_layout(
                meta_G,
                k=3.0 / math.sqrt(num_comm),
                iterations=meta_iters,
                seed=42,
                scale=base_scale,
            )
            meta_pos = {k: (float(v[0]), float(v[1])) for k, v in raw_pos.items()}
    else:
        # Spring layout with reduced iterations for larger graphs
        raw_pos = nx.spring_layout(
            meta_G,
            k=3.0 / math.sqrt(num_comm),
            iterations=meta_iters,
            seed=42,
            scale=base_scale,
            threshold=1e-3,  # Early termination
        )
        meta_pos = {k: (float(v[0]), float(v[1])) for k, v in raw_pos.items()}

    # --- Step 4: Position nodes within each community ---
    result: dict[str, dict] = {}

    # Adaptive community iteration count
    comm_iters = COMMUNITY_ITERATIONS_BASE // 2 if fast_mode else COMMUNITY_ITERATIONS_BASE

    for i, comm in enumerate(merged):
        cx, cy = meta_pos.get(i, (0.0, 0.0))

        if len(comm) == 1:
            result[list(comm)[0]] = {"x": cx, "y": cy, "community": i}
            continue

        subgraph = G.subgraph(comm)
        # Scale inner layout: larger communities get more space
        inner_scale = max(600, 260 * math.sqrt(len(comm)))

        # Very large communities: use circular (O(n) vs O(n*iters))
        if len(comm) > LARGE_COMMUNITY_THRESHOLD:
            pos = nx.circular_layout(subgraph, scale=inner_scale, center=(cx, cy))
        else:
            # Adaptive iterations — larger communities need at least 50 to avoid ring artifacts
            local_iters = max(50, min(comm_iters * 3, comm_iters * 150 // max(len(comm), 1)))

            try:
                # Use spectral layout as seed — far better initial positions than random,
                # avoids the ring formation that spring_layout gets stuck in on dense graphs
                try:
                    init_pos = nx.spectral_layout(subgraph, scale=inner_scale, center=(cx, cy))
                except Exception:
                    init_pos = None
                pos = nx.spring_layout(
                    subgraph,
                    pos=init_pos,
                    fixed=None,
                    k=4.5 / math.sqrt(max(len(comm), 1)),
                    iterations=local_iters,
                    seed=42 + i,
                    scale=inner_scale,
                    center=(cx, cy),
                    threshold=5e-4,
                )
            except Exception:
                pos = nx.circular_layout(subgraph, scale=inner_scale, center=(cx, cy))

        for node_id, coords in pos.items():
            result[node_id] = {
                "x": float(coords[0]),
                "y": float(coords[1]),
                "community": i,
            }

    # --- Step 5: Scatter isolates around periphery ---
    if isolate_nodes:
        iso_community = num_comm
        for j, node_id in enumerate(isolate_nodes):
            angle = 2 * math.pi * j / max(len(isolate_nodes), 1)
            r = base_scale * 1.4 + 200 * math.sin(j * 5.7)
            result[node_id] = {
                "x": r * math.cos(angle),
                "y": r * math.sin(angle),
                "community": iso_community,
            }

    total_comm = num_comm + (1 if isolate_nodes else 0)
    logger.info(
        "Layout: %d nodes, %d communities (%d merged from %d, %d isolates), "
        "meta_iters=%d, comm_iters=%d",
        len(result),
        total_comm,
        num_comm,
        len(connected_communities),
        len(isolate_nodes),
        meta_iters,
        comm_iters,
    )
    return result


def compute_incremental_layout(
    existing: dict[str, dict],
    new_nodes: list[dict],
    new_edges: list[dict],
    all_edges: list[dict],
) -> dict[str, dict]:
    """Incrementally update layout with new nodes.

    Instead of recomputing the full layout, positions new nodes
    relative to their neighbors' existing positions. Much faster
    for small additions to a large cached graph.

    Args:
        existing: Current layout {node_id: {x, y, community}}.
        new_nodes: Newly added nodes.
        new_edges: Edges involving new nodes.
        all_edges: Full edge list (for neighbor lookup).

    Returns:
        Updated layout including new node positions.
    """
    if not new_nodes:
        return existing

    result = dict(existing)

    # Build neighbor lookup from all edges
    neighbors: dict[str, list[str]] = {}
    for edge in all_edges:
        src = edge["data"]["source"]
        tgt = edge["data"]["target"]
        neighbors.setdefault(src, []).append(tgt)
        neighbors.setdefault(tgt, []).append(src)

    for node in new_nodes:
        nid = node["data"]["id"]
        if nid in result:
            continue

        # Position relative to neighbors
        neighbor_ids = neighbors.get(nid, [])
        positioned_neighbors = [n for n in neighbor_ids if n in result]

        if positioned_neighbors:
            # Average position of neighbors + small offset
            avg_x = sum(result[n]["x"] for n in positioned_neighbors) / len(
                positioned_neighbors
            )
            avg_y = sum(result[n]["y"] for n in positioned_neighbors) / len(
                positioned_neighbors
            )
            # Slight random offset to avoid overlap
            import random

            offset = 100 + random.random() * 50
            angle = random.random() * 2 * math.pi
            x = avg_x + offset * math.cos(angle)
            y = avg_y + offset * math.sin(angle)
            # Inherit community from most common neighbor
            comm_counts: Counter[int] = Counter()
            for n in positioned_neighbors:
                comm_counts[result[n]["community"]] += 1
            community = comm_counts.most_common(1)[0][0] if comm_counts else 0
        else:
            # No positioned neighbors — place at origin with unique community
            x, y = 0.0, 0.0
            community = max((r["community"] for r in result.values()), default=-1) + 1

        result[nid] = {"x": x, "y": y, "community": community}

    return result


def _merge_communities(
    communities: list[set[str]], G: nx.Graph, threshold: int
) -> list[set[str]]:
    """Merge small communities into their most-connected larger neighbor."""
    # Sort by size descending
    communities = sorted(communities, key=len, reverse=True)

    major: list[set[str]] = []
    minor: list[set[str]] = []
    for comm in communities:
        if len(comm) >= threshold:
            major.append(comm)
        else:
            minor.append(comm)

    if not major:
        # All communities are small — just return as-is
        return communities

    # Build lookup: node -> major community index
    node_to_major: dict[str, int] = {}
    for i, comm in enumerate(major):
        for n in comm:
            node_to_major[n] = i

    # Merge each minor into the major it connects to most
    orphans: list[set[str]] = []
    for mc in minor:
        edge_counts: Counter[int] = Counter()
        for n in mc:
            for neighbor in G.neighbors(n):
                if neighbor in node_to_major:
                    edge_counts[node_to_major[neighbor]] += 1
        if edge_counts:
            best_major = edge_counts.most_common(1)[0][0]
            major[best_major] = major[best_major] | mc
            for n in mc:
                node_to_major[n] = best_major
        else:
            orphans.append(mc)

    # Group orphans together into chunks of ~threshold size
    if orphans:
        current: set[str] = set()
        for oc in orphans:
            current |= oc
            if len(current) >= threshold:
                major.append(current)
                current = set()
        if current:
            major.append(current)

    return major


def _scatter_grid(node_ids: list[str], community: int) -> dict:
    """Place nodes in a grid pattern."""
    cols = max(1, int(math.sqrt(len(node_ids))))
    spacing = 200
    result = {}
    for j, nid in enumerate(node_ids):
        result[nid] = {
            "x": float((j % cols) * spacing),
            "y": float((j // cols) * spacing),
            "community": community,
        }
    return result
