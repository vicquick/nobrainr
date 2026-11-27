"""Graph clustering kernels for community detection, run in a child process.

vpsfixer 27.11.2026: ``leidenalg.find_partition`` holds the GIL for its whole
run. communities.py used to call it through ``run_in_executor(None, ...)`` — a
*thread* — which froze the asyncio event loop anyway: measured 11.9 s of Leiden
starved a neighbour thread for 8.43 s; on the live graph (221k nodes) nobrainr
was unhealthy for ~2.5 min (health timeouts, MCP connections closed), 6 min
after every start and weekly. These functions now run in a spawned process.

This module imports nothing from nobrainr on purpose: a spawned child imports
it fresh, and it must stay cheap (no DB pool, no settings, no LLM client).
"""

import networkx as nx


def run_leiden(
    nodes: list[str],
    edges: list[tuple[str, str, float]],
    *,
    resolution: float,
    initial_membership: list[int] | None = None,
) -> list[set[str]]:
    """Run Leiden via ``leidenalg`` + ``igraph``. Returns communities as sets of node IDs."""
    import igraph as ig
    import leidenalg

    # Build igraph graph with stable node-id → vertex-index map
    idx_of = {nid: i for i, nid in enumerate(nodes)}
    g = ig.Graph(n=len(nodes), directed=False)
    ig_edges = []
    weights = []
    for src, tgt, w in edges:
        i = idx_of.get(src)
        j = idx_of.get(tgt)
        if i is None or j is None or i == j:
            continue
        ig_edges.append((i, j))
        weights.append(float(w))
    g.add_edges(ig_edges)
    g.es["weight"] = weights

    partition = leidenalg.find_partition(
        g,
        leidenalg.RBConfigurationVertexPartition,
        initial_membership=initial_membership,
        weights="weight",
        resolution_parameter=resolution,
        seed=42,
        n_iterations=-1,  # iterate until convergence (Leiden's key property)
    )
    return [{nodes[i] for i in members} for members in partition]


def run_louvain(
    nodes: list[str],
    edges: list[tuple[str, str, float]],
    *,
    resolution: float,
) -> list[set[str]]:
    """Fallback: networkx Louvain when leidenalg isn't installed."""
    g = nx.Graph()
    for nid in nodes:
        g.add_node(nid)
    for src, tgt, w in edges:
        if g.has_edge(src, tgt):
            g[src][tgt]["weight"] += float(w)
        else:
            g.add_edge(src, tgt, weight=float(w))
    return [
        set(c)
        for c in nx.community.louvain_communities(
            g, weight="weight", resolution=resolution, seed=42,
        )
    ]
