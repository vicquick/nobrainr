"""Advanced search enhancements — HyDE, query decomposition, global search, graph search.

HyDE (Hypothetical Document Embedding): Generate a hypothetical answer to the query,
embed it, and search with that embedding. This bridges the query-document semantic gap
because the hypothetical answer is closer to actual stored documents in embedding space.

Query decomposition: Break complex multi-part questions into simpler sub-queries,
search each independently, and fuse results via RRF.

Global search: Map-reduce over community summaries for broad questions that span
multiple topics (inspired by Microsoft GraphRAG).

Graph search: Entity-centric search — find relevant entities, traverse the knowledge
graph, and collect connected memories for relationship-aware retrieval.
"""

import logging

from nobrainr.config import settings
from nobrainr.extraction.llm import ollama_chat, ollama_generate

logger = logging.getLogger("nobrainr")


# ──────────────────────────────────────────────
# HyDE — Hypothetical Document Embedding
# ──────────────────────────────────────────────

async def generate_hyde_document(query: str) -> str | None:
    """Generate a hypothetical document that would answer the query.

    Returns the hypothetical text (to be embedded for search), or None on failure.
    """
    try:
        result = await ollama_generate(
            prompt=f"Write a brief, factual passage (3-5 sentences) that would answer this question:\n\n{query}",
            system=(
                "You are a technical knowledge base. Write a concise, specific passage that "
                "directly answers the question. Include concrete details, names, versions, "
                "and technical specifics. Do NOT say 'I don't know' — always produce a "
                "plausible answer even if you must hypothesize."
            ),
            temperature=0.3,
            num_ctx=1024,
            timeout=30.0,
            max_tokens=256,
        )
        if result and len(result) > 20:
            return result
        return None
    except Exception:
        logger.debug("HyDE generation failed for %r", query)
        return None


# ──────────────────────────────────────────────
# Query Decomposition
# ──────────────────────────────────────────────

DECOMPOSE_SCHEMA = {
    "type": "object",
    "properties": {
        "sub_queries": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 2,
            "maxItems": 4,
        }
    },
    "required": ["sub_queries"],
}


async def decompose_query(query: str) -> list[str]:
    """Break a complex query into 2-4 simpler sub-queries.

    Returns sub-queries (not including the original). Empty list on failure.
    """
    try:
        result = await ollama_chat(
            system=(
                "You decompose complex questions into simpler sub-questions. "
                "Each sub-question should target a specific aspect of the original query. "
                "Return 2-4 focused sub-questions that together cover the full scope."
            ),
            user=f"Decompose this query into sub-questions:\n\n{query}",
            schema=DECOMPOSE_SCHEMA,
            temperature=0.2,
            num_ctx=512,
            timeout=15.0,
            think=False,
        )
        subs = result.get("sub_queries", [])
        seen = {query.lower().strip()}
        unique = []
        for s in subs:
            s = s.strip()
            if s and s.lower() not in seen:
                seen.add(s.lower())
                unique.append(s)
        return unique[:4]
    except Exception:
        logger.debug("Query decomposition failed for %r", query)
        return []


# ──────────────────────────────────────────────
# Global Search (GraphRAG-style map-reduce over communities)
# ──────────────────────────────────────────────

RELEVANCE_SCHEMA = {
    "type": "object",
    "properties": {
        "relevant": {"type": "boolean"},
        "score": {"type": "number"},
        "key_points": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["relevant", "score", "key_points"],
}


async def global_search(query: str, *, max_communities: int = 30) -> dict:
    """Map-reduce over community summaries to answer broad questions.

    Phase 1 (Map): Score each community's relevance to the query.
    Phase 2 (Reduce): Synthesize answer from relevant communities.

    Returns:
        {"answer": str, "communities_used": list[dict], "total_scanned": int}
    """
    from nobrainr.services.communities import list_communities

    communities = await list_communities(limit=max_communities)
    if not communities:
        return {"answer": "No communities detected yet. Run community_detect first.", "communities_used": [], "total_scanned": 0}

    # Phase 1: Map — score each community's relevance
    relevant = []
    for comm in communities:
        summary = comm.get("summary", "")
        title = comm.get("title", "")
        topics = ", ".join(comm.get("key_topics", []))
        top_entities = ", ".join(comm.get("top_entities", [])[:5])

        if not summary:
            continue

        try:
            result = await ollama_chat(
                system="Rate how relevant this knowledge graph community is to the query. Score 0.0-1.0.",
                user=f"Query: {query}\n\nCommunity: {title}\nSummary: {summary}\nTopics: {topics}\nEntities: {top_entities}",
                schema=RELEVANCE_SCHEMA,
                temperature=0.1,
                num_ctx=1024,
                timeout=15.0,
                think=False,
            )
            if result.get("relevant") and result.get("score", 0) >= 0.3:
                comm["relevance_score"] = result["score"]
                comm["key_points"] = result.get("key_points", [])
                relevant.append(comm)
        except Exception:
            continue

    if not relevant:
        return {"answer": "No relevant communities found for this query.", "communities_used": [], "total_scanned": len(communities)}

    # Sort by relevance score
    relevant.sort(key=lambda c: c.get("relevance_score", 0), reverse=True)
    top = relevant[:10]

    # Phase 2: Reduce — synthesize answer from relevant communities
    context_parts = []
    for c in top:
        points = "\n".join(f"  - {p}" for p in c.get("key_points", []))
        context_parts.append(
            f"**{c['title']}** (relevance: {c.get('relevance_score', 0):.1f}, "
            f"{c['member_count']} entities)\n"
            f"  Summary: {c['summary']}\n"
            f"  Topics: {', '.join(c.get('key_topics', []))}\n"
            f"  Key entities: {', '.join(c.get('top_entities', [])[:5])}\n"
            f"{points}"
        )
    context = "\n\n".join(context_parts)

    try:
        answer = await ollama_generate(
            prompt=f"Question: {query}\n\nRelevant knowledge graph communities:\n\n{context}\n\nSynthesize a comprehensive answer based on the communities above.",
            system=(
                "You are a knowledge synthesis engine. Based on the knowledge graph communities "
                "provided, synthesize a clear, comprehensive answer to the question. Reference "
                "specific entities and topics. Be concrete and actionable."
            ),
            temperature=0.3,
            num_ctx=8192,
            timeout=60.0,
            max_tokens=1024,
        )
    except Exception:
        answer = "Failed to synthesize answer. Relevant communities found but synthesis failed."

    return {
        "answer": answer,
        "communities_used": [
            {
                "community_id": c["community_id"],
                "title": c["title"],
                "relevance_score": c.get("relevance_score", 0),
                "key_points": c.get("key_points", []),
            }
            for c in top
        ],
        "total_scanned": len(communities),
    }


# ──────────────────────────────────────────────
# Graph-Aware Local Search
# ──────────────────────────────────────────────

async def graph_search(
    query: str,
    *,
    limit: int = 10,
    depth: int = 1,
    include_cold: bool = False,
) -> dict:
    """Entity-centric search: find entities → traverse graph → collect memories.

    1. Semantic search on entities to find the most relevant ones
    2. Traverse the knowledge graph (1-2 hops) to find related entities
    3. Collect all memories linked to the entity neighborhood
    4. Rerank memories by relevance to the original query

    Returns:
        {
            "memories": list[dict],
            "entities": list[dict],
            "relationships": list[dict],
        }
    """
    from nobrainr.db import queries as db
    from nobrainr.embeddings.ollama import embed_text

    embedding = await embed_text(query)

    # Step 1: Find relevant entities via semantic search
    entity_results = await db.search_entities(embedding, limit=5)
    # Filter by similarity threshold
    entity_results = [e for e in entity_results if e.get("similarity", 0) >= 0.3]
    if not entity_results:
        return {"memories": [], "entities": [], "relationships": []}

    # Step 2: Traverse graph to get neighborhood
    all_entity_ids = set()
    all_relationships = []
    entity_details = {}

    for entity in entity_results:
        eid = entity["id"]
        ename = entity.get("name", "")
        all_entity_ids.add(eid)
        entity_details[eid] = entity

        try:
            graph = await db.get_entity_graph(ename, depth=depth, max_nodes=50)
            for node in graph.get("nodes", []):
                nid = node.get("id")
                if nid:
                    all_entity_ids.add(nid)
                    if nid not in entity_details:
                        entity_details[nid] = node
            for edge in graph.get("edges", []):
                all_relationships.append(edge)
        except Exception:
            continue

    # Step 3: Collect memories linked to all discovered entities
    memory_ids_seen = set()
    memories = []

    for eid in list(all_entity_ids)[:20]:  # Cap to avoid huge queries
        try:
            entity_mems = await db.get_entity_memories(eid)
            for mem in entity_mems:
                mid = mem.get("id")
                if mid and mid not in memory_ids_seen:
                    # Respect cold tier exclusion
                    if not include_cold and mem.get("tier", 2) >= 3:
                        continue
                    memory_ids_seen.add(mid)
                    memories.append(mem)
        except Exception:
            continue

    # Step 4: Rerank memories by relevance to query
    if settings.reranker_enabled and len(memories) > 1:
        try:
            from nobrainr.services.reranker import rerank
            memories = await rerank(query, memories, limit=limit)
        except Exception:
            # Fallback: sort by similarity if available, else just truncate
            memories.sort(key=lambda m: m.get("similarity", 0), reverse=True)
            memories = memories[:limit]
    else:
        memories = memories[:limit]

    return {
        "memories": memories,
        "entities": [
            entity_details[eid]
            for eid in list(all_entity_ids)[:20]
            if eid in entity_details
        ],
        "relationships": all_relationships[:50],
    }
