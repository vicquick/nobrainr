"""Multi-query expansion — generate variant queries for better recall.

Uses the local LLM to rephrase a search query into 2-3 diverse variants,
then all variants are searched and results fused via RRF.
"""

import logging

from nobrainr.extraction.llm import ollama_chat

logger = logging.getLogger("nobrainr")

EXPANSION_SCHEMA = {
    "type": "object",
    "properties": {
        "queries": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 2,
            "maxItems": 3,
        }
    },
    "required": ["queries"],
}

SYSTEM_PROMPT = (
    "You are a search query expansion expert. Given a search query, generate 2-3 "
    "diverse alternative phrasings that would help find relevant results. "
    "Each variant should approach the topic from a different angle — use synonyms, "
    "rephrase as a question, extract key concepts, or broaden/narrow scope. "
    "Return ONLY the variant queries (do NOT include the original)."
)


async def expand_query(query: str) -> list[str]:
    """Generate 2-3 variant queries for the given search query.

    Returns:
        List of variant query strings (does NOT include the original).
        Returns empty list on failure (graceful degradation).
    """
    try:
        result = await ollama_chat(
            system=SYSTEM_PROMPT,
            user=query,
            schema=EXPANSION_SCHEMA,
            temperature=0.3,
            num_ctx=512,
            timeout=15.0,
            keep_alive="5m",
            think=False,
        )
        variants = result.get("queries", [])
        # Deduplicate and filter empties
        seen = {query.lower().strip()}
        unique = []
        for v in variants:
            v = v.strip()
            if v and v.lower() not in seen:
                seen.add(v.lower())
                unique.append(v)
        return unique[:3]
    except Exception:
        logger.debug("Query expansion failed for %r, using original only", query)
        return []
