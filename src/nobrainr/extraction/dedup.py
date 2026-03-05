"""Deduplication logic — checks if a new memory should be merged with an existing one."""

import logging

from nobrainr.db.queries import find_similar_memories
from nobrainr.extraction.llm import ollama_chat

logger = logging.getLogger("nobrainr")

DEDUP_SCHEMA = {
    "type": "object",
    "properties": {
        "should_merge": {
            "type": "boolean",
            "description": "Whether the two memories should be merged",
        },
        "merged_content": {
            "type": "string",
            "description": "The merged content combining both memories (only if should_merge is true)",
        },
        "reason": {
            "type": "string",
            "description": "Brief reason for the decision",
        },
    },
    "required": ["should_merge", "merged_content", "reason"],
}


async def check_memory_dedup(
    content: str,
    embedding: list[float],
) -> dict | None:
    """Check if content is a duplicate of an existing memory.

    Returns merge info dict if a merge is recommended, None otherwise.
    """
    candidates = await find_similar_memories(
        embedding, limit=3, threshold=0.85,
    )

    if not candidates:
        return None

    # Check the most similar candidate via LLM
    best = candidates[0]

    try:
        result = await ollama_chat(
            system=(
                "You are a deduplication assistant. Compare two memories and decide "
                "if they should be merged. If yes, produce a merged version that "
                "combines all unique information from both."
            ),
            user=(
                "Should these two memories be merged?\n\n"
                f"Memory A (new):\n{content}\n\n"
                f"Memory B (existing, similarity {best.get('similarity', 0):.4f}):\n{best['content']}"
            ),
            schema=DEDUP_SCHEMA,
        )

        if result.get("should_merge"):
            return {
                "should_merge": True,
                "target_id": best["id"],
                "merged_content": result["merged_content"],
                "reason": result["reason"],
            }

    except Exception:
        logger.exception("Dedup LLM check failed")

    return None
