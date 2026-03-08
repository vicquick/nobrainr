"""Write-path decision logic — Mem0-style ADD/UPDATE/SUPERSEDE/NOOP.

On every memory_store (with infer=True), we:
1. Embed the new content
2. Find top-N similar existing memories
3. If any exceed a similarity threshold, ask the LLM to decide the action
4. Execute: ADD (new), UPDATE (merge into existing), SUPERSEDE (replace old), NOOP (skip)
"""

import logging

from nobrainr.db.queries import find_similar_memories
from nobrainr.extraction.llm import ollama_chat

logger = logging.getLogger("nobrainr")

# Legacy schema used by consolidation scheduler job
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

WRITE_PATH_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["ADD", "UPDATE", "SUPERSEDE", "NOOP"],
            "description": "ADD=genuinely new info. UPDATE=merge new details into an existing memory. SUPERSEDE=new version replaces outdated existing memory. NOOP=duplicate, skip.",
        },
        "target_id": {
            "type": "string",
            "description": "ID of the existing memory to UPDATE or SUPERSEDE. Empty string for ADD/NOOP.",
        },
        "content": {
            "type": "string",
            "description": "For UPDATE: merged content combining both memories. For SUPERSEDE: the new replacement content. For ADD/NOOP: empty string.",
        },
        "reason": {
            "type": "string",
            "description": "Brief explanation (1 sentence) for the decision.",
        },
    },
    "required": ["action", "target_id", "content", "reason"],
}

SYSTEM_PROMPT = """\
You are a knowledge base curator. Given a NEW memory and a list of EXISTING similar memories, decide the best action:

- **ADD**: The new memory contains genuinely novel information not covered by any existing memory. Store it.
- **UPDATE**: An existing memory covers the same topic but the new memory adds useful details. Produce merged content that combines ALL unique information from both (keep everything, lose nothing).
- **SUPERSEDE**: An existing memory is outdated or incorrect, and the new memory is the updated version. The new content replaces the old.
- **NOOP**: The new memory is essentially a duplicate of an existing one — same information, no new details. Skip it.

Rules:
- Prefer UPDATE over ADD when topics overlap significantly — avoid near-duplicates.
- Prefer SUPERSEDE when the new memory explicitly contradicts or updates facts in an existing one.
- Only choose NOOP when the new memory adds zero new information.
- When choosing UPDATE, the merged content must include ALL details from both memories.
- Set target_id to the ID of the most relevant existing memory for UPDATE/SUPERSEDE. Use empty string for ADD/NOOP."""


async def decide_write_action(
    content: str,
    embedding: list[float],
    *,
    similarity_threshold: float = 0.7,
    candidate_limit: int = 5,
) -> dict:
    """Decide what to do with a new memory based on similar existing ones.

    Returns a dict with keys: action, target_id, content, reason.
    On failure, returns {"action": "ADD"} to fall through to normal storage.
    """
    candidates = await find_similar_memories(
        embedding, limit=candidate_limit, threshold=similarity_threshold,
    )

    if not candidates:
        return {"action": "ADD", "target_id": "", "content": "", "reason": "No similar memories found"}

    # Build context for LLM
    existing_text = ""
    for i, mem in enumerate(candidates, 1):
        sim = mem.get("similarity", 0)
        summary = mem.get("summary") or ""
        summary_line = f" (summary: {summary})" if summary else ""
        existing_text += (
            f"[{i}] ID: {mem['id']} | similarity: {sim:.2%}{summary_line}\n"
            f"    {mem['content'][:500]}\n\n"
        )

    user_prompt = (
        f"NEW MEMORY:\n{content}\n\n"
        f"EXISTING SIMILAR MEMORIES:\n{existing_text}"
    )

    try:
        result = await ollama_chat(
            system=SYSTEM_PROMPT,
            user=user_prompt,
            schema=WRITE_PATH_SCHEMA,
            think=False,  # gemma3 doesn't support thinking
            num_ctx=4096,
        )

        action = result.get("action", "ADD").upper()
        if action not in ("ADD", "UPDATE", "SUPERSEDE", "NOOP"):
            action = "ADD"

        # Validate target_id exists in candidates for UPDATE/SUPERSEDE
        if action in ("UPDATE", "SUPERSEDE"):
            target_id = result.get("target_id", "")
            valid_ids = {m["id"] for m in candidates}
            if target_id not in valid_ids:
                # LLM hallucinated an ID — fall back to the most similar
                target_id = candidates[0]["id"]
                logger.warning("Write path: LLM returned invalid target_id, using most similar: %s", target_id)
            result["target_id"] = target_id

            # For UPDATE, ensure merged content is non-empty
            if action == "UPDATE" and not result.get("content", "").strip():
                result["content"] = content  # fall back to new content

            # For SUPERSEDE, ensure replacement content is non-empty
            if action == "SUPERSEDE" and not result.get("content", "").strip():
                result["content"] = content

        result["action"] = action
        return result

    except Exception:
        logger.exception("Write path LLM decision failed, defaulting to ADD")
        return {"action": "ADD", "target_id": "", "content": "", "reason": "LLM decision failed"}


# Backwards compatibility alias
async def check_memory_dedup(content: str, embedding: list[float]) -> dict | None:
    """Legacy dedup check — wraps decide_write_action for old callers."""
    result = await decide_write_action(content, embedding)
    if result["action"] == "UPDATE":
        return {
            "should_merge": True,
            "target_id": result["target_id"],
            "merged_content": result["content"],
            "reason": result["reason"],
        }
    return None
