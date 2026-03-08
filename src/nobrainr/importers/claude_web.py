"""Import Claude.ai web export (conversations.json, memories.json, projects.json)."""

import json
import logging

from nobrainr.db import queries
from nobrainr.embeddings.ollama import embed_text

logger = logging.getLogger("nobrainr")

MAX_EMBED_CHARS = 6000


async def import_claude_web_export(
    file_path: str,
    source_machine: str | None = None,
) -> dict:
    """Import Claude.ai web export conversations into conversations_raw.

    The web export has format:
    [{
        "uuid": "...",
        "name": "conversation title",
        "summary": "...",
        "created_at": "2025-12-01T22:20:03.048838Z",
        "chat_messages": [
            {"sender": "human", "text": "...", "created_at": "..."},
            {"sender": "assistant", "text": "...", "created_at": "..."},
        ]
    }]
    """
    with open(file_path, encoding="utf-8") as f:
        conversations = json.load(f)

    imported = 0
    skipped = 0

    for conv in conversations:
        messages = conv.get("chat_messages", [])
        if len(messages) < 2:
            skipped += 1
            continue

        title = conv.get("name") or conv.get("summary") or "Untitled"

        # Normalize messages to ChatGPT-like format for the distill pipeline
        normalized_messages = []
        for msg in messages:
            sender = msg.get("sender", "human")
            text = msg.get("text", "")
            # Claude export uses "content" array sometimes
            if not text and msg.get("content"):
                if isinstance(msg["content"], list):
                    text = " ".join(
                        c.get("text", "") for c in msg["content"]
                        if isinstance(c, dict) and c.get("type") == "text"
                    )
                elif isinstance(msg["content"], str):
                    text = msg["content"]

            if not text.strip():
                continue

            role = "user" if sender == "human" else "assistant"
            normalized_messages.append({
                "role": role,
                "content": text,
            })

        if len(normalized_messages) < 2:
            skipped += 1
            continue

        stored = await queries.store_raw_conversation(
            source_type="claude_web",
            title=title,
            messages=normalized_messages,
            metadata={
                "claude_uuid": conv.get("uuid"),
                "created_at": conv.get("created_at"),
                "source_machine": source_machine,
            },
        )

        if stored:
            imported += 1
        else:
            skipped += 1

    logger.info(
        "Claude web import: %d imported, %d skipped from %s",
        imported, skipped, file_path,
    )
    return {"imported": imported, "skipped": skipped, "source": file_path}


async def import_claude_memories(
    file_path: str,
    source_machine: str | None = None,
) -> dict:
    """Import Claude.ai memories.json — Claude's built-in user memory.

    Each entry is a text block containing Claude's understanding of the user.
    Store each as a memory with source_type="claude_memory".
    """
    with open(file_path, encoding="utf-8") as f:
        memories = json.load(f)

    if not isinstance(memories, list):
        return {"error": "Expected a JSON array"}

    imported = 0
    skipped = 0

    for mem in memories:
        # Claude memories.json format: [{"conversations_memory": "text..."}]
        content = mem.get("conversations_memory", "")
        if not content or len(content.strip()) < 20:
            skipped += 1
            continue

        try:
            embedding = await embed_text(content[:MAX_EMBED_CHARS])
        except Exception:
            logger.warning("Embedding failed for Claude memory, skipping")
            skipped += 1
            continue

        await queries.store_memory(
            content=content,
            embedding=embedding,
            summary="Claude.ai built-in memory snapshot",
            source_type="claude_memory",
            source_machine=source_machine,
            tags=["imported", "claude-ai", "user-profile"],
            category="documentation",
            confidence=0.8,
            metadata={"source_file": file_path},
        )
        imported += 1

    logger.info("Claude memories import: %d imported, %d skipped", imported, skipped)
    return {"imported": imported, "skipped": skipped}


async def import_claude_projects(
    file_path: str,
    source_machine: str | None = None,
) -> dict:
    """Import Claude.ai projects.json — project descriptions and templates.

    Store each project as a memory capturing the project context.
    """
    with open(file_path, encoding="utf-8") as f:
        projects = json.load(f)

    if not isinstance(projects, list):
        return {"error": "Expected a JSON array"}

    imported = 0
    skipped = 0

    for proj in projects:
        name = proj.get("name", "")
        desc = proj.get("description", "")
        prompt = proj.get("prompt_template", "")

        parts = []
        if name:
            parts.append(f"Project: {name}")
        if desc:
            parts.append(f"Description: {desc}")
        if prompt:
            parts.append(f"Prompt template: {prompt}")

        content = "\n".join(parts)
        if len(content.strip()) < 20:
            skipped += 1
            continue

        try:
            embedding = await embed_text(content[:MAX_EMBED_CHARS])
        except Exception:
            logger.warning("Embedding failed for project '%s', skipping", name)
            skipped += 1
            continue

        await queries.store_memory(
            content=content,
            embedding=embedding,
            summary=f"Claude.ai project: {name}"[:200],
            source_type="claude_project",
            source_machine=source_machine,
            tags=["imported", "claude-ai", "project"],
            category="architecture",
            confidence=0.7,
            metadata={
                "claude_uuid": proj.get("uuid"),
                "created_at": proj.get("created_at"),
            },
        )
        imported += 1

    logger.info("Claude projects import: %d imported, %d skipped", imported, skipped)
    return {"imported": imported, "skipped": skipped}
