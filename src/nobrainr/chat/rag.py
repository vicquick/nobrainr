"""RAG chat pipeline — embed, search, build context, stream from Ollama."""

import json
import logging
from collections.abc import AsyncIterator

import httpx

from nobrainr.config import settings
from nobrainr.db import queries
from nobrainr.embeddings.ollama import embed_text

from .prompts import SYSTEM_PROMPT
from .sanitize import is_injection_attempt, sanitize_context, sanitize_user_input

logger = logging.getLogger("nobrainr")

_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(base_url=settings.ollama_url, timeout=180.0)
    return _client


def _sse(event_type: str, data: dict) -> str:
    return f"data: {json.dumps({'type': event_type, **data})}\n\n"


def _build_context(memories: list[dict], entities: list[dict]) -> str:
    parts: list[str] = []
    if memories:
        parts.append("MEMORIES:")
        for i, m in enumerate(memories, 1):
            summary = m.get("summary") or m["content"][:300]
            cat = m.get("category") or "uncategorized"
            parts.append(f"  [{i}] ({cat}) {sanitize_context(summary, 400)}")
    if entities:
        parts.append("\nENTITIES:")
        for e in entities:
            desc = f" — {sanitize_context(e.get('description') or '', 200)}" if e.get("description") else ""
            parts.append(f"  - {e['canonical_name']} ({e['entity_type']}){desc}")
    return "\n".join(parts) if parts else "(No relevant context found.)"


async def stream_chat_response(
    message: str,
    history: list[dict],
) -> AsyncIterator[str]:
    """Full RAG pipeline: sanitize → embed → search → context → stream."""
    # 1. Sanitize input
    clean = sanitize_user_input(message, settings.chat_max_message_length)
    if not clean:
        yield _sse("error", {"message": "Empty message"})
        return

    # 2. Check for injection
    if is_injection_attempt(clean):
        yield _sse("token", {"content": "I can only answer questions about the knowledge stored in the memory system. Could you rephrase your question?"})
        yield _sse("sources", {"memories": [], "entities": []})
        yield _sse("done", {})
        return

    # 3. Embed query
    try:
        embedding = await embed_text(clean)
    except Exception:
        logger.exception("Chat embedding failed")
        yield _sse("error", {"message": "Search temporarily unavailable. Please try again."})
        return

    # 4. Hybrid memory search
    memories = await queries.search_memories(
        embedding=embedding,
        limit=settings.chat_max_context_memories,
        threshold=0.25,
        text_query=clean,
    )

    # 5. Collect linked entities
    entity_map: dict[str, dict] = {}
    for mem in memories:
        try:
            ents = await queries.get_memory_entities(mem["id"])
            for e in ents:
                entity_map[e["id"]] = e
        except Exception:
            pass  # non-critical

    # 6. Build context and messages
    context = _build_context(memories, list(entity_map.values()))
    llm_messages = [
        {"role": "system", "content": SYSTEM_PROMPT.format(context=context)},
    ]
    # Sanitized history
    for h in history[-settings.chat_max_history_length:]:
        role = "user" if h.get("role") == "user" else "assistant"
        content = sanitize_user_input(h.get("content", ""), settings.chat_max_message_length)
        if content:
            llm_messages.append({"role": role, "content": content})
    llm_messages.append({"role": "user", "content": clean})

    # 7. Stream from Ollama
    model = settings.chat_model or settings.extraction_model
    client = _get_client()
    payload = {
        "model": model,
        "messages": llm_messages,
        "stream": True,
        "think": False,
        "options": {"temperature": 0.3, "num_ctx": 8192},
        "keep_alive": "24h",
    }

    try:
        async with client.stream("POST", "/api/chat", json=payload, timeout=120.0) as resp:
            if resp.status_code != 200:
                body = await resp.aread()
                logger.error("Ollama chat error %d: %s", resp.status_code, body[:500])
                yield _sse("error", {"message": "Generation temporarily unavailable."})
                return
            async for line in resp.aiter_lines():
                line = line.strip()
                if not line:
                    continue
                try:
                    chunk = json.loads(line)
                except json.JSONDecodeError:
                    continue
                token = chunk.get("message", {}).get("content", "")
                if token:
                    yield _sse("token", {"content": token})
                if chunk.get("done"):
                    break
    except httpx.ReadTimeout:
        yield _sse("error", {"message": "Response timed out. Please try a shorter question."})
        return
    except Exception:
        logger.exception("Chat stream error")
        yield _sse("error", {"message": "Generation error. Please try again."})
        return

    # 8. Emit sources
    source_memories = [
        {"id": m["id"], "summary": m.get("summary"), "content": m["content"][:200]}
        for m in memories
    ]
    source_entities = [
        {"id": e["id"], "name": e["canonical_name"], "entity_type": e["entity_type"]}
        for e in entity_map.values()
    ]
    yield _sse("sources", {"memories": source_memories, "entities": source_entities})
    yield _sse("done", {})
