"""Graph layout computation using NetworkX.

Computes community-grouped positions for the knowledge graph visualization.
Uses Louvain community detection + two-level spring layout:
1. Communities detected via Louvain
2. Community centroids positioned via spring layout on a meta-graph
3. Nodes positioned within communities via local spring layout
"""

import logging
import math

import networkx as nx
from networkx.algorithms.community import louvain_communities

logger = logging.getLogger("nobrainr")


def compute_graph_layout(nodes: list[dict], edges: list[dict]) -> dict:
    """Compute community-grouped layout for graph visualization.

    Returns dict mapping node_id -> {"x": float, "y": float, "community": int}
    """
    G = nx.Graph()

    node_ids = set()
    node_weight: dict[str, int] = {}
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

    # --- Community detection ---
    try:
        communities_raw = list(louvain_communities(G, seed=42, resolution=1.0))
    except Exception:
        communities_raw = [set(G.nodes())]

    # Separate connected communities from isolates
    real_communities: list[set[str]] = []
    isolate_nodes: list[str] = []
    for comm in communities_raw:
        connected = {n for n in comm if G.degree(n) > 0}
        isolated = [n for n in comm if G.degree(n) == 0]
        if connected:
            real_communities.append(connected)
        isolate_nodes.extend(isolated)

    # Sort by total weight (most important first)
    real_communities.sort(
        key=lambda c: sum(node_weight.get(n, 1) for n in c), reverse=True
    )

    # --- Build meta-graph for community positioning ---
    node_to_comm: dict[str, int] = {}
    for i, comm in enumerate(real_communities):
        for n in comm:
            node_to_comm[n] = i

    meta_G = nx.Graph()
    for i in range(len(real_communities)):
        meta_G.add_node(i, size=len(real_communities[i]))

    for src, tgt in G.edges():
        c_src = node_to_comm.get(src)
        c_tgt = node_to_comm.get(tgt)
        if c_src is not None and c_tgt is not None and c_src != c_tgt:
            if meta_G.has_edge(c_src, c_tgt):
                meta_G[c_src][c_tgt]["weight"] += 1
            else:
                meta_G.add_edge(c_src, c_tgt, weight=1)

    # Position community centroids using spring layout on meta-graph
    num_comm = len(real_communities)
    base_scale = max(5000, 2000 * math.sqrt(num_comm))

    if num_comm == 0:
        meta_pos: dict = {}
    elif num_comm == 1:
        meta_pos = {0: (0.0, 0.0)}
    else:
        meta_pos = nx.spring_layout(
            meta_G,
            k=4.0 / math.sqrt(num_comm),
            iterations=200,
            seed=42,
            scale=base_scale,
        )
        # Convert numpy arrays to tuples of floats
        meta_pos = {k: (float(v[0]), float(v[1])) for k, v in meta_pos.items()}

    # --- Position nodes within each community ---
    result: dict[str, dict] = {}

    for i, comm in enumerate(real_communities):
        cx, cy = meta_pos.get(i, (0.0, 0.0))

        if len(comm) == 1:
            node_id = list(comm)[0]
            result[node_id] = {"x": cx, "y": cy, "community": i}
            continue

        subgraph = G.subgraph(comm)
        # Scale inner layout proportional to sqrt of community size
        inner_scale = max(300, 150 * math.sqrt(len(comm)))

        try:
            pos = nx.spring_layout(
                subgraph,
                k=3.0 / math.sqrt(max(len(comm), 1)),
                iterations=80,
                seed=42 + i,
                scale=inner_scale,
                center=(cx, cy),
            )
        except Exception:
            pos = nx.circular_layout(subgraph, scale=inner_scale, center=(cx, cy))

        for node_id, coords in pos.items():
            result[node_id] = {
                "x": float(coords[0]),
                "y": float(coords[1]),
                "community": i,
            }

    # --- Scatter isolates around the periphery ---
    if isolate_nodes:
        iso_community = num_comm
        for j, node_id in enumerate(isolate_nodes):
            angle = 2 * math.pi * j / max(len(isolate_nodes), 1)
            # Vary radius for organic look
            r = base_scale * 1.4 + 300 * math.sin(j * 5.7)
            result[node_id] = {
                "x": r * math.cos(angle),
                "y": r * math.sin(angle),
                "community": iso_community,
            }

    total_communities = num_comm + (1 if isolate_nodes else 0)
    logger.info(
        "Layout computed: %d nodes, %d communities (%d isolates)",
        len(result),
        total_communities,
        len(isolate_nodes),
    )

    return result
