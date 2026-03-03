"""ChatGPT conversation import pipeline."""

import json
import logging
from pathlib import Path

from nobrainr.db import queries
from nobrainr.embeddings.ollama import embed_text, embed_batch

logger = logging.getLogger("nobrainr.import.chatgpt")


async def import_chatgpt_export(file_path: str, *, distill: bool = False) -> dict:
    """Import conversations from ChatGPT export JSON.

    The OpenAI export contains a conversations.json with structure:
    [
        {
            "title": "...",
            "create_time": 1234567890.0,
            "update_time": 1234567890.0,
            "mapping": {
                "node-id": {
                    "message": {
                        "author": {"role": "user"|"assistant"|"system"},
                        "content": {"parts": ["..."]},
                        "create_time": 1234567890.0
                    },
                    "parent": "parent-node-id",
                    "children": ["child-node-id"]
                }
            }
        }
    ]
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
    distilled = 0

    for convo in conversations:
        title = convo.get("title", "Untitled")
        messages = _extract_messages(convo)

        if not messages:
            skipped += 1
            continue

        # Store raw conversation
        create_time = convo.get("create_time")
        metadata = {}
        if create_time:
            from datetime import datetime, timezone
            metadata["original_date"] = datetime.fromtimestamp(
                create_time, tz=timezone.utc
            ).isoformat()
        metadata["model"] = _extract_model(convo)

        await queries.store_raw_conversation(
            source_type="chatgpt",
            title=title,
            messages=messages,
            source_file=str(path.name),
            metadata=metadata,
        )
        imported += 1

        # Optionally distill into memory chunks
        if distill:
            chunks = _chunk_conversation(title, messages)
            for chunk in chunks:
                try:
                    embedding = await embed_text(chunk["content"])
                    await queries.store_memory(
                        content=chunk["content"],
                        embedding=embedding,
                        summary=f"ChatGPT: {title}",
                        source_type="chatgpt",
                        source_ref=title,
                        tags=["imported", "chatgpt"],
                        category="conversation",
                        confidence=0.7,
                        metadata=metadata,
                    )
                    distilled += 1
                except Exception as e:
                    logger.warning(f"Failed to distill chunk from '{title}': {e}")

    return {
        "status": "complete",
        "conversations_imported": imported,
        "conversations_skipped": skipped,
        "memories_distilled": distilled,
        "total_conversations": len(conversations),
    }


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

    # Sort by timestamp
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


def _chunk_conversation(title: str, messages: list[dict], max_chars: int = 2000) -> list[dict]:
    """Chunk a conversation into digestible pieces for embedding."""
    chunks = []
    current = f"Conversation: {title}\n\n"

    for msg in messages:
        line = f"{msg['role'].upper()}: {msg['content']}\n\n"
        if len(current) + len(line) > max_chars and len(current) > 100:
            chunks.append({"content": current.strip()})
            current = f"Conversation: {title} (continued)\n\n"
        current += line

    if current.strip() and len(current) > 50:
        chunks.append({"content": current.strip()})

    return chunks
