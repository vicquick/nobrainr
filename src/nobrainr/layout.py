"""Graph layout computation using NetworkX.

Computes community-grouped positions for the knowledge graph visualization.
Uses Louvain community detection + two-level spring layout so clusters
are clearly separated and internally structured.
"""

import logging
import math

import networkx as nx
from networkx.algorithms.community import louvain_communities

logger = logging.getLogger("nobrainr")


def compute_graph_layout(nodes: list[dict], edges: list[dict]) -> dict:
    """Compute community-grouped layout for graph visualization.

    Two-level approach:
    1. Louvain community detection groups related nodes
    2. Communities placed on a large circle (well-separated)
    3. Within each community, spring_layout positions nodes locally

    Returns dict mapping node_id -> {"x": float, "y": float, "community": int}
    """
    G = nx.Graph()

    node_ids = set()
    node_weight = {}
    for node in nodes:
        nid = node["data"]["id"]
        G.add_node(nid)
        node_ids.add(nid)
        node_weight[nid] = node["data"].get("mention_count", 1)

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

    # Community detection (Louvain)
    try:
        communities_raw = list(louvain_communities(G, seed=42, resolution=1.0))
    except Exception:
        communities_raw = [set(G.nodes())]

    # Separate connected nodes from isolates
    real_communities: list[set] = []
    isolate_nodes: list[str] = []
    for comm in communities_raw:
        connected = {n for n in comm if G.degree(n) > 0}
        isolated = [n for n in comm if G.degree(n) == 0]
        if connected:
            real_communities.append(connected)
        isolate_nodes.extend(isolated)

    # Sort communities by total weight (largest/most important first)
    real_communities.sort(
        key=lambda c: sum(node_weight.get(n, 1) for n in c), reverse=True
    )

    num_communities = len(real_communities)
    # Scale radius so communities don't overlap
    community_radius = max(3000, 1200 * math.sqrt(max(num_communities, 1)))

    result: dict[str, dict] = {}

    for i, comm in enumerate(real_communities):
        # Community centroid on circle
        angle = 2 * math.pi * i / max(num_communities, 1)
        cx = community_radius * math.cos(angle)
        cy = community_radius * math.sin(angle)

        if len(comm) == 1:
            node_id = list(comm)[0]
            result[node_id] = {"x": cx, "y": cy, "community": i}
            continue

        # Spring layout within community
        subgraph = G.subgraph(comm)
        inner_scale = max(300, 120 * math.sqrt(len(comm)))

        try:
            k = 2.5 / math.sqrt(max(len(comm), 1))
            pos = nx.spring_layout(
                subgraph,
                k=k,
                iterations=100,
                seed=42 + i,
                scale=inner_scale,
                center=(cx, cy),
            )
        except Exception:
            # Fallback: circular layout
            pos = nx.circular_layout(subgraph, scale=inner_scale, center=(cx, cy))

        for node_id, coords in pos.items():
            result[node_id] = {
                "x": float(coords[0]),
                "y": float(coords[1]),
                "community": i,
            }

    # Place isolates in a grid below the main layout
    if isolate_nodes:
        iso_community = num_communities
        cols = max(1, int(math.sqrt(len(isolate_nodes))))
        spacing = 200
        start_x = -(cols * spacing) / 2
        start_y = community_radius + 1200

        for j, node_id in enumerate(isolate_nodes):
            col = j % cols
            row = j // cols
            result[node_id] = {
                "x": start_x + col * spacing,
                "y": start_y + row * spacing,
                "community": iso_community,
            }

    total_communities = num_communities + (1 if isolate_nodes else 0)
    logger.info(
        "Layout computed: %d nodes, %d communities (%d isolates)",
        len(result),
        total_communities,
        len(isolate_nodes),
    )

    return result
