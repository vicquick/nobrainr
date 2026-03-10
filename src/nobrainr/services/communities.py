"""GraphRAG community detection — find clusters of related entities.

Uses the Louvain algorithm on the entity_relations graph to identify
densely connected communities. Each community gets an LLM-generated
summary for hierarchical retrieval.
"""

import logging

import networkx as nx

from nobrainr.db.pool import get_pool
from nobrainr.extraction.llm import ollama_chat

logger = logging.getLogger("nobrainr")

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

    # Build networkx graph
    G = nx.Graph()
    entity_map = {e["id"]: e for e in entities}
    for e in entities:
        G.add_node(str(e["id"]))

    for edge in edges:
        src = str(edge["source_entity_id"])
        tgt = str(edge["target_entity_id"])
        weight = float(edge["confidence"] or 1.0)
        if G.has_edge(src, tgt):
            G[src][tgt]["weight"] += weight
        else:
            G.add_edge(src, tgt, weight=weight)

    # Run Louvain community detection
    communities = nx.community.louvain_communities(
        G, weight="weight", resolution=resolution, seed=42,
    )

    # Filter out singleton/tiny communities
    valid_communities = [c for c in communities if len(c) >= min_community_size]

    # Assign community IDs
    community_assignments: dict[str, int] = {}
    for idx, members in enumerate(valid_communities):
        for node_id in members:
            community_assignments[node_id] = idx

    # Store community assignments in entities table
    async with pool.acquire() as conn:
        # Ensure community_id column exists
        await conn.execute("""
            ALTER TABLE entities ADD COLUMN IF NOT EXISTS community_id integer
        """)
        # Clear old assignments
        await conn.execute("UPDATE entities SET community_id = NULL")

        # Batch update
        for node_id_str, comm_id in community_assignments.items():
            try:
                from uuid import UUID
                await conn.execute(
                    "UPDATE entities SET community_id = $1 WHERE id = $2",
                    comm_id, UUID(node_id_str),
                )
            except Exception:
                continue

    # Count singletons (entities in communities smaller than min_size)
    assigned = len(community_assignments)
    total = len(entities)
    singleton = total - assigned

    largest = max(len(c) for c in valid_communities) if valid_communities else 0

    logger.info(
        "Community detection: %d communities, %d entities assigned, %d singletons, largest=%d",
        len(valid_communities), assigned, singleton, largest,
    )

    return {
        "communities": len(valid_communities),
        "entities_assigned": assigned,
        "singleton_entities": singleton,
        "largest_community_size": largest,
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
        descriptions = [d for d in row["descriptions"] if d]

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
                temperature=0.1,
                num_ctx=2048,
                timeout=30.0,
                think=False,
            )
            summaries[comm_id] = result
            summarized += 1
        except Exception:
            logger.debug("Failed to summarize community %d", comm_id)
            failed += 1

    # Store summaries in a metadata table or in entity metadata
    if summaries:
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
            for comm_id, s in summaries.items():
                member_count = sum(1 for r in rows if r["community_id"] == comm_id)
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
