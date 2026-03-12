"""ChatGPT conversation import pipeline.

Phase 1: import_chatgpt_export() — store raw conversations (fast, no embedding)
Phase 2: distill_conversations() — LLM extracts learnings from raw conversations
         Uses sliding-window multi-pass for long conversations.
"""

import asyncio
import json
import logging
from pathlib import Path

from nobrainr.db import queries
from nobrainr.db.pool import get_pool
from nobrainr.embeddings.ollama import embed_text
from nobrainr.extraction.llm import ollama_chat

logger = logging.getLogger("nobrainr.import.chatgpt")

# Max chars to send to embedding model
MAX_EMBED_CHARS = 6000

DISTILL_SCHEMA = {
    "type": "object",
    "properties": {
        "has_learnings": {
            "type": "boolean",
            "description": "Whether this conversation segment contains reusable knowledge",
        },
        "learnings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "The knowledge distilled — 2-5 detailed sentences capturing the insight, solution, decision, or pattern",
                    },
                    "summary": {
                        "type": "string",
                        "description": "One-line summary (max 12 words)",
                    },
                    "category": {
                        "type": "string",
                        "description": "One of: architecture, debugging, patterns, infrastructure, tooling, deployment, security, business, data, frontend, backend, insight, documentation",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "3-5 relevant tags",
                    },
                },
                "required": ["content", "summary", "category", "tags"],
            },
            "description": "List of distilled learnings (0-5 per segment)",
        },
    },
    "required": ["has_learnings", "learnings"],
}

DISTILL_SYSTEM_PROMPT = (
    "You are a knowledge extractor. Extract ALL reusable knowledge from this conversation segment. "
    "This includes:\n"
    "- Technical solutions, commands, configurations, code patterns\n"
    "- Architectural decisions and their rationale\n"
    "- Debugging insights — what went wrong and why\n"
    "- Workflow patterns and best practices\n"
    "- Tool configurations and integration details\n"
    "- Data processing techniques\n"
    "- Personal preferences, creative insights, and domain knowledge\n"
    "- Business decisions, project plans, and strategies\n\n"
    "Each learning should be self-contained — someone reading it without the conversation "
    "should understand the knowledge. Include specific names, versions, and details.\n"
    "Return has_learnings=false ONLY if the segment is truly trivial (greetings, "
    "small talk with no substance, or pure noise)."
)


# ──────────────────────────────────────────────
# Phase 1: Raw import (fast)
# ──────────────────────────────────────────────

async def import_chatgpt_export(
    file_path: str, *, distill: bool = False, source_machine: str | None = None,
) -> dict:
    """Import conversations from ChatGPT export JSON into conversations_raw table.

    If distill=True, also runs LLM distillation after import.
    """
    path = Path(file_path)
    if not path.exists():
        return {"error": f"File not found: {file_path}"}

    with open(path) as f:
        conversations = json.load(f)

    if not isinstance(conversations, list):
        return {"error": "Expected a JSON array of conversations"}

    imported = 0
    skipped = 0

    for convo in conversations:
        title = convo.get("title", "Untitled")
        messages = _extract_messages(convo)

        if not messages or len(messages) < 2:
            skipped += 1
            continue

        create_time = convo.get("create_time")
        metadata = {"source_machine": source_machine}
        if create_time:
            from datetime import datetime, timezone
            metadata["original_date"] = datetime.fromtimestamp(
                create_time, tz=timezone.utc
            ).isoformat()
        metadata["model"] = _extract_model(convo)
        metadata["message_count"] = len(messages)
        # Store ChatGPT conversation ID for future dedup
        chatgpt_id = convo.get("conversation_id") or convo.get("id")
        if chatgpt_id:
            metadata["chatgpt_conversation_id"] = chatgpt_id

        result_id = await queries.store_raw_conversation(
            source_type="chatgpt",
            title=title,
            messages=messages,
            source_file=str(path.name),
            metadata=metadata,
        )
        if result_id is None:
            skipped += 1  # duplicate
        else:
            imported += 1

    result = {
        "status": "complete",
        "conversations_imported": imported,
        "conversations_skipped": skipped,
        "total_conversations": len(conversations),
    }

    if distill and imported > 0:
        distill_result = await distill_conversations(source_machine=source_machine)
        result["distill"] = distill_result

    return result


# ──────────────────────────────────────────────
# Phase 2: LLM distillation (multi-pass)
# ──────────────────────────────────────────────

def _sliding_windows(
    messages: list[dict],
    window_size: int = 8,
    overlap: int = 2,
    max_chars_per_window: int = 3500,
) -> list[str]:
    """Split conversation messages into overlapping text windows for LLM processing.

    Args:
        messages: List of message dicts with 'role' and 'content'.
        window_size: Number of messages per window.
        overlap: Number of messages overlapping between windows.
        max_chars_per_window: Max chars per window text.

    Returns:
        List of formatted text strings, one per window.
    """
    relevant = [m for m in messages if m.get("role") in ("user", "assistant")]
    if not relevant:
        return []

    # Short conversations: single window
    full_text = ""
    for m in relevant:
        full_text += f"{m['role'].upper()}: {m['content'].strip()}\n\n"
    if len(full_text) <= max_chars_per_window:
        return [full_text]

    # Build sliding windows
    windows = []
    step = max(1, window_size - overlap)
    i = 0
    while i < len(relevant):
        chunk = relevant[i : i + window_size]
        text = ""
        for m in chunk:
            content = m["content"].strip()
            # Truncate individual messages that are excessively long (code dumps etc)
            if len(content) > 2000:
                content = content[:1800] + "\n[...truncated...]"
            text += f"{m['role'].upper()}: {content}\n\n"

        # Trim to max chars
        if len(text) > max_chars_per_window:
            text = text[:max_chars_per_window]

        windows.append(text)
        i += step

        # Safety: cap at 30 windows per conversation to bound LLM cost
        if len(windows) >= 30:
            break

    return windows


async def distill_conversations(
    *,
    batch_size: int = 20,
    source_machine: str | None = None,
    llm_model: str | None = None,
    concurrency: int = 1,
) -> dict:
    """Distill raw conversations into memory learnings using LLM.

    Uses sliding-window multi-pass for long conversations so no content is lost.
    Processes conversations that haven't been distilled yet (no 'distilled' in metadata).
    """
    if llm_model is None:
        from nobrainr.config import settings
        llm_model = settings.chatgpt_distill_model
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, title, messages, metadata
            FROM conversations_raw
            WHERE source_type IN ('chatgpt', 'claude_web')
              AND (metadata->>'distilled') IS NULL
            ORDER BY imported_at ASC
            LIMIT $1
            """,
            batch_size,
        )

    if not rows:
        return {"processed": 0, "distilled": 0, "skipped": 0}

    sem = asyncio.Semaphore(concurrency)
    results = {"processed": 0, "distilled": 0, "skipped": 0, "windows_processed": 0}
    lock = asyncio.Lock()

    async def _distill_one(row):
        convo_id = str(row["id"])
        title = row["title"] or "Untitled"
        messages = row["messages"] if isinstance(row["messages"], list) else json.loads(row["messages"])
        metadata = row["metadata"] if isinstance(row["metadata"], dict) else json.loads(row["metadata"] or "{}")
        machine = source_machine or metadata.get("source_machine")

        local_distilled = 0
        local_windows = 0

        try:
            # Pre-filter: skip trivial conversations without LLM call
            relevant_msgs = [m for m in messages if m.get("role") in ("user", "assistant")]
            total_content = sum(len(m.get("content", "")) for m in relevant_msgs)

            if len(relevant_msgs) < 3 or total_content < 200:
                await _mark_distilled(convo_id, 0)
                async with lock:
                    results["skipped"] += 1
                    results["processed"] += 1
                return

            # Split into sliding windows
            windows = _sliding_windows(messages)
            if not windows:
                await _mark_distilled(convo_id, 0)
                async with lock:
                    results["skipped"] += 1
                    results["processed"] += 1
                return

            # Process each window
            for window_idx, window_text in enumerate(windows):
                if len(window_text.strip()) < 100:
                    continue

                user_prompt = f"Conversation: {title}"
                if len(windows) > 1:
                    user_prompt += f" (segment {window_idx + 1}/{len(windows)})"
                user_prompt += f"\n\n{window_text}"

                async with sem:
                    try:
                        result = await ollama_chat(
                            system=DISTILL_SYSTEM_PROMPT,
                            user=user_prompt,
                            schema=DISTILL_SCHEMA,
                            model=llm_model,
                            timeout=900.0,
                            num_ctx=4096,
                            think=False,
                        )
                    except Exception:
                        logger.warning(
                            "Window %d/%d failed for '%s'",
                            window_idx + 1, len(windows), title,
                        )
                        continue

                local_windows += 1
                learnings = result.get("learnings", []) if result.get("has_learnings") else []

                for learning in learnings[:5]:  # up to 5 per window
                    content = learning.get("content", "").strip()
                    if not content or len(content) < 20:
                        continue

                    # Prefix with conversation title for context
                    full_content = f"[{title}]\n{content}" if title != "Untitled" else content

                    try:
                        embedding = await embed_text(full_content[:MAX_EMBED_CHARS])
                    except Exception:
                        logger.warning("Embedding failed for learning from '%s', skipping", title)
                        continue

                    await queries.store_memory(
                        content=full_content,
                        embedding=embedding,
                        summary=learning.get("summary", f"ChatGPT: {title}")[:200],
                        source_type=metadata.get("source_type_original", "chatgpt"),
                        source_machine=machine,
                        source_ref=title,
                        tags=learning.get("tags", []) + ["imported", "chatgpt-distilled"],
                        category=learning.get("category", "insight"),
                        confidence=0.7,
                        metadata={
                            "conversation_id": convo_id,
                            "window_index": window_idx,
                            "total_windows": len(windows),
                        },
                    )
                    local_distilled += 1

                # Yield between windows to let live requests through
                from nobrainr.config import settings as _s
                await asyncio.sleep(_s.scheduler_inter_request_delay)

            await _mark_distilled(convo_id, local_distilled, windows=len(windows))
            async with lock:
                results["distilled"] += local_distilled
                results["windows_processed"] += local_windows
                results["processed"] += 1

        except Exception as e:
            err_msg = f"{type(e).__name__}: {e}" or "unknown error"
            logger.warning("Distillation failed for '%s': %s", title, err_msg, exc_info=True)
            await _mark_distilled(convo_id, local_distilled, error=err_msg, windows=local_windows)
            async with lock:
                results["distilled"] += local_distilled
                results["windows_processed"] += local_windows
                results["processed"] += 1

        from nobrainr.config import settings as _s
        await asyncio.sleep(_s.scheduler_inter_request_delay)

    tasks = [asyncio.create_task(_distill_one(row)) for row in rows]
    await asyncio.gather(*tasks, return_exceptions=True)

    return results


async def _mark_distilled(
    convo_id: str,
    learning_count: int,
    *,
    error: str | None = None,
    windows: int = 0,
) -> None:
    """Mark a raw conversation as distilled."""
    pool = await get_pool()
    from uuid import UUID
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE conversations_raw
            SET metadata = COALESCE(metadata, '{}'::jsonb) || $1::jsonb
            WHERE id = $2
            """,
            json.dumps({
                "distilled": True,
                "learning_count": learning_count,
                "distill_windows": windows,
                "distill_version": 2,
                **({"distill_error": error} if error else {}),
            }),
            UUID(convo_id),
        )


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def _extract_messages(convo: dict) -> list[dict]:
    """Extract ordered messages from ChatGPT conversation mapping."""
    mapping = convo.get("mapping", {})
    messages = []

    for node_id, node in mapping.items():
        msg = node.get("message")
        if msg is None:
            continue

        author = msg.get("author", {}).get("role", "unknown")
        content_parts = msg.get("content", {}).get("parts", [])
        content = "\n".join(str(p) for p in content_parts if isinstance(p, str))

        if not content.strip():
            continue

        messages.append({
            "role": author,
            "content": content,
            "timestamp": msg.get("create_time"),
        })

    messages.sort(key=lambda m: m.get("timestamp") or 0)
    return messages


def _extract_model(convo: dict) -> str | None:
    """Try to extract the model used in a conversation."""
    mapping = convo.get("mapping", {})
    for node in mapping.values():
        msg = node.get("message")
        if msg and msg.get("author", {}).get("role") == "assistant":
            meta = msg.get("metadata", {})
            return meta.get("model_slug") or meta.get("model")
    return None


# Keep old function for backwards compatibility with any callers
def _compress_for_llm(title: str, messages: list[dict], max_chars: int = 4000) -> str:
    """Legacy compression — use _sliding_windows for new code."""
    relevant = [m for m in messages if m.get("role") in ("user", "assistant")]
    if not relevant:
        return ""

    header = f"Conversation: {title}\n\n"
    budget = max_chars - len(header)

    full_text = ""
    for m in relevant:
        role = m["role"].upper()
        content = m["content"].strip()
        full_text += f"{role}: {content}\n\n"

    if len(full_text) <= budget:
        return header + full_text

    parts = []
    for m in relevant[:2]:
        parts.append(f"{m['role'].upper()}: {m['content'][:500]}")
    for m in relevant[-4:]:
        parts.append(f"{m['role'].upper()}: {m['content'][:500]}")

    compressed = "\n\n".join(parts)
    return (header + compressed)[:max_chars]
