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
You are a strict knowledge base curator. Given a NEW memory and EXISTING similar memories, decide the best action:

- **ADD**: The new memory is about a different specific topic. Store it separately.
- **UPDATE**: An existing memory is about the EXACT SAME specific topic/issue AND the new memory adds concrete details. Merge them.
- **SUPERSEDE**: An existing memory is about the exact same topic but is now outdated/incorrect. Replace it.
- **NOOP**: The new memory is a near-exact duplicate — same topic, same details. Skip it.

STRICT RULES — follow these precisely:
- Two memories are about the "same topic" ONLY if they describe the same specific thing (e.g. the same bug, the same tool, the same config). Sharing a general domain like "development" or "tooling" is NOT enough.
- NEVER merge memories from clearly different projects, tools, or problem domains. A CI lint fix is NOT the same topic as a QGIS scripting question, even if both involve "development."
- When in doubt, choose ADD. A few extra memories is far better than corrupting existing ones by merging unrelated content.
- Prefer ADD over UPDATE unless you are very confident the memories describe the exact same thing.
- When choosing UPDATE, the merged content must include ALL details from both memories.
- Set target_id to the ID of the most relevant existing memory for UPDATE/SUPERSEDE. Use empty string for ADD/NOOP."""


async def decide_write_action(
    content: str,
    embedding: list[float],
    *,
    similarity_threshold: float = 0.85,
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
        f"NEW MEMORY:\n{content[:3000]}\n\n"
        f"EXISTING SIMILAR MEMORIES:\n{existing_text}"
    )

    try:
        result = await ollama_chat(
            system=SYSTEM_PROMPT,
            user=user_prompt,
            schema=WRITE_PATH_SCHEMA,
            think=False,  # Structured labeling, no reasoning needed
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
