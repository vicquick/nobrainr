"""GraphRAG community detection — find clusters of related entities.

Uses the **Leiden** algorithm (Traag et al. 2019, CWTS) on the entity_relations
graph to identify densely connected communities. Leiden guarantees that each
cluster is internally connected — unlike Louvain, which can produce clusters
that fall apart as soon as one bridging node is moved out. On our graph
(a handful of hubs bridging otherwise-isolated communities) this difference
matters in practice.

Falls back to Louvain if ``leidenalg``/``igraph`` are not importable, so the
service keeps working in environments that haven't rebuilt the container yet.

Each community gets an LLM-generated summary for hierarchical retrieval.
"""

import logging

import networkx as nx

from nobrainr.db.pool import get_pool
from nobrainr.extraction.llm import ollama_chat

logger = logging.getLogger("nobrainr")


def _run_leiden(
    nodes: list[str],
    edges: list[tuple[str, str, float]],
    *,
    resolution: float,
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
        weights="weight",
        resolution_parameter=resolution,
        seed=42,
        n_iterations=-1,  # iterate until convergence (Leiden's key property)
    )
    return [{nodes[i] for i in members} for members in partition]


def _run_louvain(
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

SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "key_topics": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["title", "summary", "key_topics"],
}


async def detect_communities(
    *,
    min_community_size: int = 3,
    resolution: float = 1.0,
) -> dict:
    """Run Louvain community detection on the entity graph.

    Returns:
        {
            "communities": int,
            "entities_assigned": int,
            "singleton_entities": int,
            "largest_community_size": int,
        }
    """
    pool = await get_pool()

    async with pool.acquire() as conn:
        # Load edges
        edges = await conn.fetch("""
            SELECT source_entity_id, target_entity_id, confidence
            FROM entity_relations
            WHERE valid = true
        """)

        # Load entity names for labeling
        entities = await conn.fetch("""
            SELECT id, name, entity_type, canonical_name
            FROM entities
        """)

    if not edges:
        return {"communities": 0, "entities_assigned": 0, "singleton_entities": len(entities), "largest_community_size": 0}

    # Flatten raw inputs once, then hand off to the chosen backend.
    node_ids = [str(e["id"]) for e in entities]
    edge_tuples: list[tuple[str, str, float]] = [
        (str(e["source_entity_id"]), str(e["target_entity_id"]), float(e["confidence"] or 1.0))
        for e in edges
    ]

    algo = "leiden"
    try:
        communities = _run_leiden(node_ids, edge_tuples, resolution=resolution)
    except ImportError:
        logger.warning(
            "leidenalg/igraph not installed — falling back to Louvain. "
            "Install `leidenalg` + `python-igraph` for guaranteed connected clusters."
        )
        communities = _run_louvain(node_ids, edge_tuples, resolution=resolution)
        algo = "louvain_fallback"
    except Exception:
        logger.exception("Leiden failed unexpectedly, falling back to Louvain")
        communities = _run_louvain(node_ids, edge_tuples, resolution=resolution)
        algo = "louvain_fallback"

    # Filter out singleton/tiny communities
    valid_communities = [c for c in communities if len(c) >= min_community_size]

    # Assign community IDs using a STABLE hash of the cluster's top-K
    # **core** members (sorted UUIDs, take the first 10). Rationale:
    #
    # v1 of this hash used all sorted members, which was perfectly stable
    # when clusters were identical across runs — but flipped ENTIRELY
    # when even one member was added/removed, because every member of
    # that cluster then had a different community_id. A single merge into
    # the 6,157-node giant cluster cascaded 6K+ entity UPDATEs per run.
    #
    # The top-10 sorted-UUID core is stable against any fringe membership
    # change: a cluster's label only shifts when one of its 10 lowest-UUID
    # members actually leaves/joins. UUID v7 is time-ordered so the
    # first-10 corresponds to the longest-resident members of the cluster.
    # Across hundreds of clusters the collision probability is negligible.
    import hashlib as _hashlib
    SIGNATURE_K = 10
    community_assignments: dict[str, int] = {}
    for members in valid_communities:
        sorted_members = sorted(members)
        core = sorted_members[:SIGNATURE_K]
        sig = _hashlib.md5(
            "|".join(core).encode("utf-8"),
        ).hexdigest()
        comm_id = int(sig[:8], 16) & 0x7FFFFFFF  # 31-bit, fits in PG int
        for node_id in members:
            community_assignments[node_id] = comm_id

    # Store community assignments in entities table using a SINGLE delta-only
    # bulk update. The previous pattern — "UPDATE entities SET community_id =
    # NULL" followed by 34K individual UPDATEs — was writing ~72K audit_log
    # rows per run (~250 MB/day) even when the partition barely changed, and
    # the audit trigger fired on every one because the triggers treat any
    # UPDATE as a row event. Delta-only + bulk UNNEST means only genuinely
    # moved entities generate audit events. Typical steady-state run: <500
    # audit rows instead of 72,000.
    from uuid import UUID as _UUID
    async with pool.acquire() as conn:
        await conn.execute(
            "ALTER TABLE entities ADD COLUMN IF NOT EXISTS community_id integer"
        )

        if community_assignments:
            ids = []
            cids = []
            for node_id_str, comm_id in community_assignments.items():
                try:
                    ids.append(_UUID(node_id_str))
                    cids.append(int(comm_id))
                except (ValueError, TypeError):
                    continue

            # 1) NULL out entities that are currently in a community but no
            # longer in the assignment map — only rows where the value
            # actually changes get audited because the trigger short-circuits
            # on OLD IS NOT DISTINCT FROM NEW.
            await conn.execute(
                """
                UPDATE entities
                SET community_id = NULL
                WHERE community_id IS NOT NULL
                  AND id <> ALL($1::uuid[])
                """,
                ids,
            )
            # 2) Apply new assignments, but only where the community_id is
            # actually changing (IS DISTINCT FROM catches NULL→N and N→M).
            await conn.execute(
                """
                UPDATE entities AS e
                SET community_id = v.cid
                FROM unnest($1::uuid[], $2::int[]) AS v(eid, cid)
                WHERE e.id = v.eid
                  AND e.community_id IS DISTINCT FROM v.cid
                """,
                ids, cids,
            )
        else:
            # No communities at all — NULL out everyone with a stale assignment
            await conn.execute(
                "UPDATE entities SET community_id = NULL WHERE community_id IS NOT NULL"
            )

    # Count singletons (entities in communities smaller than min_size)
    assigned = len(community_assignments)
    total = len(entities)
    singleton = total - assigned

    largest = max(len(c) for c in valid_communities) if valid_communities else 0

    logger.info(
        "Community detection (%s): %d communities, %d entities assigned, %d singletons, largest=%d",
        algo, len(valid_communities), assigned, singleton, largest,
    )

    return {
        "communities": len(valid_communities),
        "entities_assigned": assigned,
        "singleton_entities": singleton,
        "largest_community_size": largest,
        "algorithm": algo,
    }


async def generate_community_summaries(*, max_communities: int = 50) -> dict:
    """Generate LLM summaries for each detected community.

    Returns:
        {"summarized": int, "failed": int}
    """
    pool = await get_pool()

    async with pool.acquire() as conn:
        # Get distinct communities with their entities
        rows = await conn.fetch("""
            SELECT community_id, array_agg(name) AS names, array_agg(entity_type) AS types,
                   array_agg(COALESCE(description, '')) AS descriptions
            FROM entities
            WHERE community_id IS NOT NULL
            GROUP BY community_id
            ORDER BY count(*) DESC
            LIMIT $1
        """, max_communities)

    if not rows:
        return {"summarized": 0, "failed": 0}

    summarized = 0
    failed = 0
    summaries = {}

    for row in rows:
        comm_id = row["community_id"]
        names = list(row["names"])
        types = list(row["types"])

        # Build context for LLM
        members = []
        for n, t, d in zip(names, types, row["descriptions"]):
            entry = f"- {n} ({t})"
            if d:
                entry += f": {d[:100]}"
            members.append(entry)

        context = "\n".join(members[:30])  # Limit to 30 members for context window

        try:
            result = await ollama_chat(
                system=(
                    "You are analyzing a cluster of related entities from a knowledge graph. "
                    "Generate a concise title (3-5 words), a 1-2 sentence summary of what "
                    "this cluster represents, and 3-5 key topics it covers."
                ),
                user=f"Community members:\n{context}",
                schema=SUMMARY_SCHEMA,
                num_ctx=2048,
                timeout=120.0,
                think=False,
            )
            summaries[comm_id] = result
            summarized += 1
        except Exception:
            logger.debug("Failed to summarize community %d", comm_id)
            failed += 1

    # Store summaries in a metadata table or in entity metadata
    if summaries:
        # Build member count lookup from the original query
        member_counts = {r["community_id"]: len(list(r["names"])) for r in rows}

        async with pool.acquire() as conn:
            # Create community_summaries table if needed
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS community_summaries (
                    community_id integer PRIMARY KEY,
                    title text,
                    summary text,
                    key_topics text[],
                    member_count integer,
                    updated_at timestamptz DEFAULT now()
                )
            """)
            # NOTE: Don't DELETE — relies on UPSERT below. The previous DELETE
            # wiped every community whose id wasn't in the current top-N batch,
            # which capped total summaries at max_communities (~50). Removing
            # the DELETE lets us accumulate summaries across batches and keeps
            # historical summaries even if community_ids drift. Stale entries
            # are pruned by maintenance scripts, not by this function.
            for comm_id, s in summaries.items():
                member_count = member_counts.get(comm_id, 0)
                await conn.execute("""
                    INSERT INTO community_summaries (community_id, title, summary, key_topics, member_count)
                    VALUES ($1, $2, $3, $4, $5)
                    ON CONFLICT (community_id) DO UPDATE
                    SET title = $2, summary = $3, key_topics = $4, member_count = $5, updated_at = now()
                """, comm_id, s.get("title", ""), s.get("summary", ""),
                     s.get("key_topics", []), member_count)

    return {"summarized": summarized, "failed": failed}


async def list_communities(*, limit: int = 50) -> list[dict]:
    """List all detected communities with their summaries."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Check if tables exist
        has_table = await conn.fetchval("""
            SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'community_summaries')
        """)
        if not has_table:
            return []

        rows = await conn.fetch("""
            SELECT cs.community_id, cs.title, cs.summary, cs.key_topics, cs.member_count, cs.updated_at,
                   array_agg(e.name ORDER BY e.mention_count DESC) AS top_entities
            FROM community_summaries cs
            LEFT JOIN entities e ON e.community_id = cs.community_id
            GROUP BY cs.community_id, cs.title, cs.summary, cs.key_topics, cs.member_count, cs.updated_at
            ORDER BY cs.member_count DESC
            LIMIT $1
        """, limit)
        return [
            {
                "community_id": r["community_id"],
                "title": r["title"],
                "summary": r["summary"],
                "key_topics": list(r["key_topics"] or []),
                "member_count": r["member_count"],
                "top_entities": list(r["top_entities"] or [])[:10],
                "updated_at": str(r["updated_at"]),
            }
            for r in rows
        ]


async def get_community_members(community_id: int) -> list[dict]:
    """Get all entities in a specific community."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, name, entity_type, canonical_name, description, mention_count
            FROM entities
            WHERE community_id = $1
            ORDER BY mention_count DESC
        """, community_id)
        return [
            {
                "id": str(r["id"]),
                "name": r["name"],
                "entity_type": r["entity_type"],
                "description": r["description"],
                "mention_count": r["mention_count"],
            }
            for r in rows
        ]
