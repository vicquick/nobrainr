"""ChatGPT conversation import pipeline.

Phase 1: import_chatgpt_export() — store raw conversations (fast, no embedding)
Phase 2: distill_conversations() — LLM extracts learnings from raw conversations
"""

import json
import logging
from pathlib import Path

from nobrainr.db import queries
from nobrainr.db.pool import get_pool
from nobrainr.embeddings.ollama import embed_text
from nobrainr.extraction.llm import ollama_chat

logger = logging.getLogger("nobrainr.import.chatgpt")

# Max chars to send to embedding model (nomic-embed-text context ~8192 tokens)
MAX_EMBED_CHARS = 6000

DISTILL_SCHEMA = {
    "type": "object",
    "properties": {
        "has_learnings": {
            "type": "boolean",
            "description": "Whether this conversation contains reusable knowledge",
        },
        "learnings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "The learning/knowledge distilled (2-5 sentences)",
                    },
                    "summary": {
                        "type": "string",
                        "description": "One-line summary (max 12 words)",
                    },
                    "category": {
                        "type": "string",
                        "description": "One of: architecture, debugging, patterns, infrastructure, tooling, deployment, security, business, ops",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "3-5 relevant tags",
                    },
                },
                "required": ["content", "summary", "category", "tags"],
            },
            "description": "List of distilled learnings (0-3)",
        },
    },
    "required": ["has_learnings", "learnings"],
}


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
# Phase 2: LLM distillation
# ──────────────────────────────────────────────

async def distill_conversations(
    *,
    batch_size: int = 20,
    source_machine: str | None = None,
    llm_model: str | None = None,
) -> dict:
    """Distill raw conversations into memory learnings using LLM.

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
            WHERE source_type = 'chatgpt'
              AND (metadata->>'distilled') IS NULL
            ORDER BY imported_at ASC
            LIMIT $1
            """,
            batch_size,
        )

    if not rows:
        return {"processed": 0, "distilled": 0, "skipped": 0}

    processed = 0
    distilled = 0
    skipped = 0

    for row in rows:
        convo_id = str(row["id"])
        title = row["title"] or "Untitled"
        messages = row["messages"] if isinstance(row["messages"], list) else json.loads(row["messages"])
        metadata = row["metadata"] if isinstance(row["metadata"], dict) else json.loads(row["metadata"] or "{}")
        machine = source_machine or metadata.get("source_machine")

        try:
            # Pre-filter: skip trivial conversations without LLM call
            relevant_msgs = [m for m in messages if m.get("role") in ("user", "assistant")]
            total_content = sum(len(m.get("content", "")) for m in relevant_msgs)

            if len(relevant_msgs) < 3 or total_content < 200:
                await _mark_distilled(convo_id, 0)
                skipped += 1
                processed += 1
                continue

            # Compress conversation to fit LLM context
            convo_text = _compress_for_llm(title, messages, max_chars=2000)

            if len(convo_text) < 100:
                # Too short to be useful
                await _mark_distilled(convo_id, 0)
                skipped += 1
                processed += 1
                continue

            result = await ollama_chat(
                system=(
                    "Extract reusable technical learnings from this conversation. "
                    "Focus on solutions, commands, configs, patterns. "
                    "Return has_learnings=false if trivial or generic."
                ),
                user=convo_text,
                schema=DISTILL_SCHEMA,
                model=llm_model,
                timeout=300.0,
                num_ctx=3072,
                think=False,
            )

            learnings = result.get("learnings", []) if result.get("has_learnings") else []

            for learning in learnings[:3]:
                content = learning.get("content", "").strip()
                if not content or len(content) < 20:
                    continue

                # Truncate for embedding safety
                embed_content = content[:MAX_EMBED_CHARS]
                try:
                    embedding = await embed_text(embed_content)
                except Exception:
                    logger.warning("Embedding failed for learning from '%s', skipping", title)
                    continue

                await queries.store_memory(
                    content=content,
                    embedding=embedding,
                    summary=learning.get("summary", f"ChatGPT: {title}")[:200],
                    source_type="chatgpt",
                    source_machine=machine,
                    source_ref=title,
                    tags=learning.get("tags", []) + ["imported", "chatgpt-distilled"],
                    category=learning.get("category", "learned-pattern"),
                    confidence=0.7,
                    metadata={"conversation_id": convo_id},
                )
                distilled += 1

            await _mark_distilled(convo_id, len(learnings))
            processed += 1

        except Exception as e:
            logger.warning("Distillation failed for '%s': %s", title, e)
            await _mark_distilled(convo_id, 0, error=str(e))
            processed += 1

    return {"processed": processed, "distilled": distilled, "skipped": skipped}


async def _mark_distilled(convo_id: str, learning_count: int, *, error: str | None = None) -> None:
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


def _compress_for_llm(title: str, messages: list[dict], max_chars: int = 4000) -> str:
    """Compress a conversation for LLM distillation, keeping key exchanges."""
    # Filter to user and assistant messages only
    relevant = [m for m in messages if m.get("role") in ("user", "assistant")]
    if not relevant:
        return ""

    header = f"Conversation: {title}\n\n"
    budget = max_chars - len(header)

    # If conversation fits, include it all
    full_text = ""
    for m in relevant:
        role = m["role"].upper()
        content = m["content"].strip()
        full_text += f"{role}: {content}\n\n"

    if len(full_text) <= budget:
        return header + full_text

    # Otherwise, take first exchange + last few exchanges (where conclusions usually are)
    parts = []
    # First 2 messages (problem statement)
    for m in relevant[:2]:
        parts.append(f"{m['role'].upper()}: {m['content'][:500]}")

    # Last 4 messages (solution/conclusion)
    for m in relevant[-4:]:
        parts.append(f"{m['role'].upper()}: {m['content'][:500]}")

    compressed = "\n\n".join(parts)
    return (header + compressed)[:max_chars]
