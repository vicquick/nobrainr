"""RAG chat pipeline — embed, search, build context, stream from Ollama."""

import asyncio
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


def _build_context(memories: list[dict], entities: list[dict], facts: list[dict] | None = None) -> str:
    parts: list[str] = []
    # Facts first — most precise, token-efficient context
    if facts:
        parts.append("FACTS (atomic knowledge):")
        for i, f in enumerate(facts, 1):
            parts.append(f"  [{i}] {sanitize_context(f['content'], 200)}")
    if memories:
        parts.append("\nMEMORIES:")
        for i, m in enumerate(memories, 1):
            summary = m.get("summary") or m["content"][:300]
            cat = m.get("category") or "uncategorized"
            parts.append(f"  [{i}] ({cat}) {sanitize_context(summary, 400)}")
    if entities:
        parts.append("\nENTITIES:")
        for e in entities:
            desc = f" — {sanitize_context(e.get('description') or '', 200)}" if e.get("description") else ""
            name = e.get("canonical_name") or e.get("name") or "unknown"
            parts.append(f"  - {name} ({e['entity_type']}){desc}")
    return "\n".join(parts) if parts else "(No relevant context found.)"


# Dynamic topic cache — refreshed every 10 minutes from entity names
_topic_cache: set[str] = set()
_topic_cache_time: float = 0


async def _get_known_topics() -> set[str]:
    """Load entity names as known topics, cached for 10 minutes."""
    global _topic_cache, _topic_cache_time
    import time
    now = time.monotonic()
    if _topic_cache and (now - _topic_cache_time) < 600:
        return _topic_cache
    try:
        from nobrainr.db.pool import get_pool
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT DISTINCT LOWER(name) as name FROM entities "
                "WHERE mention_count >= 3 AND LENGTH(name) >= 3 "
                "ORDER BY name LIMIT 5000"
            )
        _topic_cache = {r["name"] for r in rows}
        _topic_cache_time = now
        logger.info("Topic cache refreshed: %d known topics", len(_topic_cache))
    except Exception:
        pass
    return _topic_cache


async def _try_fast_answer(question: str) -> str | None:
    """Smart router: answer questions via the fastest possible path.

    4 tiers (checked in order):
      1. FAST_SQL  — meta-questions answered by direct SQL (<1s)
      2. SEARCH    — topic lookups answered by embedding search, no LLM (~2s)
      3. GREETING  — social/conversational, canned response (<0.1s)
      4. None      — falls through to full RAG+LLM pipeline (~15-20s)
    """
    q = question.lower().strip()
    words = q.split()

    # ── TIER 1: FAST SQL — meta-questions about the KB itself ──
    try:
        from nobrainr.db.pool import get_pool
        pool = await get_pool()

        # Recent/latest/newest memory
        if any(kw in q for kw in ["most recent", "latest memory", "newest memory", "last memory",
                                   "latest entry", "last entry", "newest entry"]):
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT summary, content, category, source_type, created_at "
                    "FROM memories WHERE tier < 3 ORDER BY created_at DESC LIMIT 1"
                )
            if row:
                summary = row["summary"] or row["content"][:200]
                return (f"Most recent memory ({row['category']}, {row['source_type']}) "
                        f"from {str(row['created_at'])[:16]}:\n\n{summary}")

        # Oldest memory
        if any(kw in q for kw in ["oldest memory", "first memory", "earliest memory"]):
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT summary, content, category, source_type, created_at "
                    "FROM memories ORDER BY created_at ASC LIMIT 1"
                )
            if row:
                summary = row["summary"] or row["content"][:200]
                return (f"Oldest memory ({row['category']}, {row['source_type']}) "
                        f"from {str(row['created_at'])[:16]}:\n\n{summary}")

        # Count memories
        if any(kw in q for kw in ["how many memories", "total memories", "memory count",
                                   "number of memories"]):
            async with pool.acquire() as conn:
                count = await conn.fetchval("SELECT COUNT(*) FROM memories WHERE tier < 3")
                total = await conn.fetchval("SELECT COUNT(*) FROM memories")
            return f"You have {total:,} memories ({count:,} searchable, {total - count:,} archived)."

        # Categories
        if any(kw in q for kw in ["largest category", "biggest category", "top category",
                                   "category breakdown", "what categories", "list categories",
                                   "all categories", "show me categories", "show categories",
                                   "my categories"]):
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT category, COUNT(*) as cnt FROM memories "
                    "GROUP BY category ORDER BY cnt DESC LIMIT 10"
                )
            if rows:
                lines = [f"{r['category']}: {r['cnt']:,}" for r in rows]
                return "Categories:\n" + "\n".join(f"  {i+1}. {line}" for i, line in enumerate(lines))

        # Entity/relation count
        if any(kw in q for kw in ["how many entities", "total entities", "entity count",
                                   "how many relations", "total relations", "relation count"]):
            async with pool.acquire() as conn:
                count = await conn.fetchval("SELECT COUNT(*) FROM entities")
                rels = await conn.fetchval("SELECT COUNT(*) FROM entity_relations")
            return f"Knowledge graph: {count:,} entities connected by {rels:,} relations."

        # Tags
        if any(kw in q for kw in ["top tags", "what tags", "list tags", "popular tags"]):
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT unnest(tags) as tag, COUNT(*) as cnt FROM memories "
                    "GROUP BY tag ORDER BY cnt DESC LIMIT 15"
                )
            if rows:
                lines = [f"{r['tag']}: {r['cnt']:,}" for r in rows]
                return "Top tags:\n" + "\n".join(f"  {line}" for line in lines)

        # Sources
        if any(kw in q for kw in ["what sources", "data sources", "where does", "source breakdown"]):
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT source_type, COUNT(*) as cnt FROM memories "
                    "GROUP BY source_type ORDER BY cnt DESC"
                )
            if rows:
                lines = [f"{r['source_type']}: {r['cnt']:,}" for r in rows]
                return "Data sources:\n" + "\n".join(f"  {line}" for line in lines)

        # Machines
        if any(kw in q for kw in ["what machines", "which machines", "connected machines",
                                   "source machines"]):
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT source_machine, COUNT(*) as cnt FROM memories "
                    "WHERE source_machine IS NOT NULL "
                    "GROUP BY source_machine ORDER BY cnt DESC"
                )
            if rows:
                lines = [f"{r['source_machine']}: {r['cnt']:,}" for r in rows]
                return "Machines:\n" + "\n".join(f"  {line}" for line in lines)

        # Full stats summary
        if any(kw in q for kw in ["full stats", "knowledge base stats", "kb stats", "overview",
                                   "system stats", "dashboard"]):
            stats = await queries.get_stats()
            cats = stats.get("by_category", [])[:5]
            return (
                f"Knowledge Base Overview:\n"
                f"  Memories: {stats.get('total_memories', '?'):,}\n"
                f"  Entities: {stats.get('total_entities', '?'):,}\n"
                f"  Relations: {stats.get('total_relations', '?'):,}\n"
                f"  Top categories: {', '.join(str(c['category']) + '(' + str(c['cnt']) + ')' for c in cats)}\n"
                f"  Extraction: {stats.get('extraction_done', '?'):,} done, "
                f"{stats.get('extraction_pending', '?')} pending"
            )

    except Exception:
        logger.debug("Fast SQL path failed, falling through to RAG", exc_info=True)

    # ── TIER 2: SEARCH — topic lookups, return formatted results directly ──
    # Detect "find/show/search/what do I know about X" patterns
    # Skip if the topic itself is a meta-keyword (handled by SQL tier above)
    _meta_topics = {"categories", "category", "tags", "sources", "machines", "entities",
                    "relations", "memories", "stats", "overview", "dashboard"}
    search_prefixes = ["find ", "search ", "show me ", "list ", "what do i know about ",
                       "what do you know about ", "tell me about ", "memories about ",
                       "anything about ", "look up "]
    for prefix in search_prefixes:
        if q.startswith(prefix):
            topic = question[len(prefix):].strip().rstrip("?.")
            # Skip search if topic is a meta-keyword (already handled by SQL tier)
            if topic.lower().strip() in _meta_topics:
                break
            if topic and len(topic) > 2:
                try:
                    embedding = await embed_text(topic)
                    results = await queries.search_memories(
                        embedding=embedding, limit=5, threshold=0.3, text_query=topic,
                    )
                    if results:
                        parts = [f"Found {len(results)} memories about \"{topic}\":\n"]
                        for i, m in enumerate(results, 1):
                            summary = m.get("summary") or m.get("content", "")[:200]
                            cat = m.get("category", "")
                            parts.append(f"  {i}. [{cat}] {summary}")
                        return "\n".join(parts)
                    else:
                        return f"No memories found about \"{topic}\"."
                except Exception:
                    pass
            break  # matched prefix but topic too short, fall through

    # ── TIER 3: GREETINGS — social/conversational, no LLM needed ──
    greetings = {"hi", "hello", "hey", "hola", "hallo", "yo", "sup", "greetings"}
    thanks = {"thanks", "thank you", "thx", "cheers", "danke", "merci"}
    farewells = {"bye", "goodbye", "ciao", "tschüss", "see you", "good night", "gn"}
    if q.rstrip("!.? ") in greetings or (len(words) <= 5 and words[0] in greetings):
        return "Hello! Ask me anything about your knowledge base. Try: \"what is my largest category?\" or \"find memories about Docker\"."
    if q.rstrip("!.? ") in thanks or any(w in thanks for w in words[:2]):
        return "You're welcome! Let me know if you need anything else."
    if q.rstrip("!.? ") in farewells or any(fw in q for fw in farewells):
        return "See you! The knowledge base keeps growing while you're away."
    if q.rstrip("!.? ") in {"ok", "okay", "got it", "understood", "alright", "cool", "nice",
                             "great", "perfect", "awesome", "sweet", "good", "yes", "no", "nah"}:
        return "Got it! What else would you like to know?"
    # Out-of-scope questions — things the KB can't answer
    _out_of_scope = ["weather", "time is it", "date today", "play music", "set alarm",
                     "send email", "call ", "open ", "download ", "install "]
    if any(oos in q for oos in _out_of_scope):
        return "I can only answer questions about your knowledge base. Try: \"find memories about Docker\" or \"top tags\"."

    # Very short messages (1-2 words) that aren't likely search topics
    # Dynamic topic detection — check against actual entities in the knowledge graph
    _likely_topics = await _get_known_topics()
    if len(words) <= 2 and len(q) < 20 and not any(c.isdigit() for c in q):
        # Check if it's a known topic — route to search instead
        if any(w in _likely_topics for w in words):
            topic = question.strip().rstrip("?.!")
            try:
                embedding = await embed_text(topic)
                results = await queries.search_memories(
                    embedding=embedding, limit=5, threshold=0.3, text_query=topic,
                )
                if results:
                    parts = [f"Here's what I know about \"{topic}\":\n"]
                    for i, m in enumerate(results, 1):
                        summary = m.get("summary") or m.get("content", "")[:200]
                        cat = m.get("category", "")
                        parts.append(f"  {i}. [{cat}] {summary}")
                    return "\n".join(parts)
            except Exception:
                pass
        else:
            return "I'm here to help! Try asking about your knowledge base, like \"find memories about Python\" or \"top tags\"."

    # ── TIER 4: Falls through to full RAG+LLM pipeline ──
    return None


async def stream_chat_response(
    message: str,
    history: list[dict],
    images: list[str] | None = None,
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

    # 3. Fast path — answer simple meta-questions directly without LLM
    fast_answer = await _try_fast_answer(clean)
    if fast_answer:
        yield _sse("token", {"content": fast_answer})
        yield _sse("sources", {"memories": [], "entities": []})
        yield _sse("done", {})
        return

    # 4. Stream progress — show each pipeline step so user sees what's happening
    yield _sse("thinking", {"status": "Embedding query..."})

    import time as _time
    _t0 = _time.monotonic()

    # 4. Embed query
    try:
        embedding = await embed_text(clean)
    except Exception:
        logger.exception("Chat embedding failed")
        yield _sse("error", {"message": "Search temporarily unavailable. Please try again."})
        return

    yield _sse("thinking", {"status": "Searching knowledge base..."})

    # 5. Hybrid memory search — fetch more for sources, top-N for LLM context
    all_memories = await queries.search_memories(
        embedding=embedding,
        limit=settings.chat_max_source_memories,
        threshold=0.25,
        text_query=clean,
    )
    context_memories = all_memories[: settings.chat_max_context_memories]

    # 5b. Search facts for more precise context (Mem0-style)
    matched_facts = []
    try:
        matched_facts = await queries.search_facts(
            embedding=embedding, limit=10, threshold=0.35, text_query=clean,
        )
    except Exception:
        pass

    fact_count = len(matched_facts)
    yield _sse("thinking", {"status": f"Found {len(all_memories)} memories + {fact_count} facts, building context..."})

    # 6. Collect linked entities from context memories (parallel, not serial)
    async def _fetch_entities(mem_id: str) -> list[dict]:
        try:
            return await queries.get_memory_entities(mem_id)
        except Exception:
            return []

    entity_results = await asyncio.gather(
        *[_fetch_entities(m["id"]) for m in context_memories]
    )
    entity_map: dict[str, dict] = {}
    for ents in entity_results:
        for e in ents:
            entity_map[e["id"]] = e

    # 7. Build context (only top-N memories fed to LLM)
    # Inject knowledge base stats only for meta-questions (saves ~2s context processing)
    _meta_keywords = {"category", "categories", "how many", "total", "largest", "smallest", "stats", "count", "tag", "tags", "memories", "entities", "relations"}
    _is_meta = any(kw in clean.lower() for kw in _meta_keywords)
    stats_summary = ""
    if _is_meta:
        try:
            from nobrainr.db import queries as _q
            _stats = await _q.get_stats()
            cats = _stats.get("by_category") or []
            tags = _stats.get("top_tags") or []
            cat_str = ", ".join(f"{c['category']}({c['cnt']})" for c in cats[:5])
            tag_str = ", ".join(f"{t['tag']}({t['cnt']})" for t in tags[:10])
            stats_summary = (
                f"\nKNOWLEDGE BASE STATS:\n"
                f"  Total memories: {_stats.get('total_memories', '?')}\n"
                f"  Total entities: {_stats.get('total_entities', '?')}\n"
                f"  Total relations: {_stats.get('total_relations', '?')}\n"
                f"  Top categories: {cat_str}\n"
                f"  Top tags: {tag_str}\n"
            )
        except Exception:
            pass
    context = _build_context(context_memories, list(entity_map.values()), matched_facts) + stats_summary
    llm_messages = [
        {"role": "system", "content": SYSTEM_PROMPT.format(context=context)},
    ]
    # Sanitized history — NOTE: images are intentionally excluded from history messages.
    # Only the current user message carries images. Prior turns' images are not forwarded
    # because: (1) Ollama re-processes all images in every request, making multi-image
    # history very expensive in VRAM and latency; (2) the frontend only stores display
    # data URLs on messages, not the raw base64 needed by the API; (3) for knowledge-base
    # Q&A, the textual conversation context is sufficient for multi-turn coherence.
    for h in history[-settings.chat_max_history_length:]:
        role = "user" if h.get("role") == "user" else "assistant"
        content = sanitize_user_input(h.get("content", ""), settings.chat_max_message_length)
        if content:
            llm_messages.append({"role": role, "content": content})
    user_msg: dict = {"role": "user", "content": clean}
    if images:
        user_msg["images"] = images
    llm_messages.append(user_msg)

    # 8. Stream from Ollama
    _t_pre_llm = _time.monotonic()
    logger.info("Chat RAG pipeline: embed+search+context took %.1fs", _t_pre_llm - _t0)
    yield _sse("thinking", {"status": f"Thinking... ({len(context_memories)} memories in context)"})
    model = settings.chat_model or settings.extraction_model
    client = _get_client()
    # OpenAI-compatible shape — llama-server has no /api/chat route (Ollama-
    # native) and 404s on it; confirmed 2026-09-04 after NOBRAINR_CHAT_ENABLED
    # was re-enabled following the Ollama decommission and every request
    # failed with "Ollama chat error 404". num_ctx/keep_alive/think are
    # Ollama-only options with no OpenAI equivalent — thinking is already
    # disabled server-side via --chat-template-kwargs on the llama-swap
    # command line (see /data/coolify/llama-swap/config.yaml), so dropping
    # `think` here changes nothing about actual generation behaviour.
    payload = {
        "model": model,
        "messages": llm_messages,
        "stream": True,
        "temperature": 0.3,
    }

    try:
        async with client.stream("POST", "/v1/chat/completions", json=payload, timeout=120.0) as resp:
            if resp.status_code != 200:
                body = await resp.aread()
                logger.error("Chat backend error %d: %s", resp.status_code, body[:500])
                yield _sse("error", {"message": "Generation temporarily unavailable."})
                return
            # Use aiter_bytes for minimal buffering (aiter_lines batches)
            buf = b""
            async for chunk in resp.aiter_bytes():
                buf += chunk
                while b"\n" in buf:
                    raw_line, buf = buf.split(b"\n", 1)
                    line = raw_line.strip()
                    if not line or not line.startswith(b"data:"):
                        continue
                    payload_bytes = line[len(b"data:"):].strip()
                    if payload_bytes == b"[DONE]":
                        break
                    try:
                        data = json.loads(payload_bytes)
                    except json.JSONDecodeError:
                        continue
                    choices = data.get("choices") or []
                    token = choices[0].get("delta", {}).get("content", "") if choices else ""
                    if token:
                        yield _sse("token", {"content": token})
                    if choices and choices[0].get("finish_reason"):
                        break
    except httpx.ReadTimeout:
        yield _sse("error", {"message": "Response timed out. Please try a shorter question."})
        return
    except Exception:
        logger.exception("Chat stream error")
        yield _sse("error", {"message": "Generation error. Please try again."})
        return

    # 9. Fetch entities from remaining source memories (non-context) for richer sources
    remaining = [m for m in all_memories[settings.chat_max_context_memories:]]
    if remaining:
        extra_results = await asyncio.gather(
            *[_fetch_entities(m["id"]) for m in remaining]
        )
        for ents in extra_results:
            for e in ents:
                entity_map[e["id"]] = e

    # 10. Emit sources (all retrieved, not just LLM context)
    source_memories = [
        {"id": m["id"], "summary": m.get("summary"), "content": m["content"][:200]}
        for m in all_memories
    ]
    source_entities = [
        {"id": e["id"], "name": e.get("canonical_name") or e.get("name"), "entity_type": e["entity_type"]}
        for e in entity_map.values()
    ]
    yield _sse("sources", {"memories": source_memories, "entities": source_entities})
    yield _sse("done", {})
