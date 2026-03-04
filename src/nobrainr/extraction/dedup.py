"""Deduplication logic — checks if a new memory should be merged with an existing one."""

import json
import logging

import httpx

from nobrainr.config import settings
from nobrainr.db.queries import find_similar_memories

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
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{settings.ollama_url}/api/chat",
                json={
                    "model": settings.extraction_model,
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "You are a deduplication assistant. Compare two memories and decide "
                                "if they should be merged. If yes, produce a merged version that "
                                "combines all unique information from both."
                            ),
                        },
                        {
                            "role": "user",
                            "content": (
                                "Should these two memories be merged?\n\n"
                                f"Memory A (new):\n{content}\n\n"
                                f"Memory B (existing, similarity {best.get('similarity', 0):.4f}):\n{best['content']}"
                            ),
                        },
                    ],
                    "format": DEDUP_SCHEMA,
                    "stream": False,
                    "options": {
                        "temperature": 0.1,
                        "num_ctx": 4096,
                    },
                },
            )
            resp.raise_for_status()
            data = resp.json()
            result = json.loads(data["message"]["content"])

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
