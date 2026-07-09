"""Shared memory storage service — used by MCP tools, scheduler, and crawler.

Provides store_memory_with_extraction() which handles:
  1. Context-enriched embedding (with optional contextual prefix)
  2. Mem0-style write path (ADD/UPDATE/SUPERSEDE/NOOP)
  3. Storage via queries.store_memory()
  4. Fire-and-forget entity extraction
"""

import asyncio
import logging
import re
from datetime import datetime  # noqa: F401 — type-hint annotation only

from nobrainr.config import settings
from nobrainr.db import queries
from nobrainr.embeddings.ollama import embed_text

logger = logging.getLogger("nobrainr")

CONTEXTUAL_PREFIX_SCHEMA = {
    "type": "object",
    "properties": {
        "context": {
            "type": "string",
            "description": "A short 1-2 sentence context that situates the chunk within the document, max 30 words",
        },
    },
    "required": ["context"],
}


async def _generate_chunk_context(
    document_summary: str,
    chunk_text: str,
) -> str:
    """Generate a contextual prefix for a chunk using the LLM.

    Returns a short 1-2 sentence context that situates the chunk within
    its source document, improving embedding quality by 35-49% (Anthropic research).
    """
    try:
        from nobrainr.extraction.llm import ollama_chat

        result = await ollama_chat(
            system=(
                "You are a retrieval optimization assistant. Given a document summary "
                "and a chunk from that document, write a very short context (1-2 sentences, "
                "max 30 words) that situates this chunk within the document. Focus on WHO, "
                "WHAT, and WHERE this information belongs. Be factual and concise."
            ),
            user=(
                f"Document: {document_summary[:500]}\n\n"
                f"Chunk: {chunk_text[:1000]}\n\n"
                "Write a short context to situate this chunk."
            ),
            schema=CONTEXTUAL_PREFIX_SCHEMA,
            model=settings.scheduler_llm_model,
            # 600s matches the April 2026 industry-standard client timeout
            # (OpenAI SDK default, llama.cpp server LLAMA_ARG_TIMEOUT default).
            # Prefix generation runs during the `contextual_prefix_backfill`
            # scheduler job, which previously timed out ~75% of its runs on
            # 2026-04-10/11 because the 30s client timeout kept firing before
            # the single-slot server could queue + process the request.
            timeout=600.0,
            think=False,
        )
        ctx = result.get("context", "").strip()
        if ctx and len(ctx) > 5:
            return ctx
    except Exception:
        logger.debug("Contextual prefix generation failed, using empty prefix")
    return ""

# Rate-limit extraction: 1 at a time with 30s cooldown
_extraction_semaphore = asyncio.Semaphore(1)


# ──────────────────────────────────────────────
# `lesson` tag auto-detection
# ──────────────────────────────────────────────
# A memory is a "lesson" when it documents a mistake surfaced, a fix
# applied, or a correction to a previous understanding. `lesson` is
# orthogonal to `confidence` — a memory can be a lesson with high
# confidence (we're sure about what went wrong) or low confidence
# (we're still investigating). This auto-tagger lets writers stay
# oblivious of the convention: tag once, here, at the canonical
# write path and at the github importer, instead of asking every
# caller to remember the tag.

_LESSON_CATEGORIES = {"debugging", "incident", "postmortem"}

_LESSON_TAG_MARKERS = {
    "bug", "bugfix", "bug-fix", "regression", "hotfix", "rollback",
    "revert", "incident", "postmortem", "broken", "security-fix",
    "fix", "fixes",
}

# Commit message title prefixes that indicate a fix-type commit.
_LESSON_COMMIT_PREFIX_RE = re.compile(
    r"^(fix|hotfix|perf|security|revert)[\(\:]",
    re.IGNORECASE,
)


def _augment_tags_with_lesson(
    tags: list[str] | None,
    category: str | None,
    content: str | None,
    source_type: str,
) -> list[str]:
    """Return ``tags`` with ``lesson`` appended when markers indicate
    the memory documents a mistake-surfaced / fix-applied narrative.

    See the comment block above for rationale. Safe to call on every
    write — idempotent (won't double-tag) and conservative (Tier-1
    markers only).
    """
    tag_list = list(tags or [])
    if "lesson" in tag_list:
        return tag_list

    # Marker 1: category signals a debugging/incident/postmortem memory
    if category in _LESSON_CATEGORIES:
        tag_list.append("lesson")
        return tag_list

    # Marker 2: explicit fix-type tag already set by the caller
    if any(t in _LESSON_TAG_MARKERS for t in tag_list):
        tag_list.append("lesson")
        return tag_list

    # Marker 3: github commit whose title starts with a fix-type prefix.
    # The commit importer formats content as:
    #   "## Commit: repo `sha`\n\n**Date:**...**Author:**...\n\n### {title}\n..."
    # We detect the title line and match against _LESSON_COMMIT_PREFIX_RE.
    if source_type == "github" and content and "commit" in (tag_list or []):
        for line in (content.splitlines() or []):
            if line.startswith("### "):
                if _LESSON_COMMIT_PREFIX_RE.match(line[4:].strip()):
                    tag_list.append("lesson")
                return tag_list

    return tag_list


def _default_situating_prefix(
    *,
    event_ts: "datetime | None",
    source_type: str,
    source_machine: str | None,
    source_ref: str | None,
    category: str | None,
    summary: str | None = None,
    tags: list[str] | None = None,
) -> str:
    """Build a cheap, LLM-free situating prefix from write-time metadata.

    Anthropic's Contextual Retrieval recipe uses a 1-2 sentence situating
    context to anchor embeddings (and FTS, since 2026-04-19) of short/vague
    content. Their own example uses an LLM to generate it, but for single-
    memory writes the same benefit comes from a deterministic template over
    the metadata we already have (timestamp, host, source, category, summary,
    tags). No extra LLM call per write.

    2026-05-03 — extended to include ``summary`` and ``tags`` after a one-shot
    backfill of 52,292 standalone memories proved this composition is the
    high-leverage shape: ``category | summary | tags: a, b, c``. Standalone
    fts_context coverage went from 0.6% → 100% via SQL-only UPDATE; we want
    new writes to land in the same shape, not the older sparse one.

    Empty return means the callers' existing contextual_prefix logic kicks
    back in (e.g. the LLM-generated per-chunk prefix for multi-chunk docs).
    """
    parts: list[str] = []
    if event_ts:
        parts.append(event_ts.strftime("%Y-%m-%d"))
    if source_type and source_type != "manual":
        parts.append(f"via {source_type}")
    if source_machine:
        parts.append(f"on {source_machine}")
    if category:
        parts.append(f"[{category}]")
    if summary:
        # Cap to 200 chars so the prefix doesn't dominate the embedded input.
        parts.append(summary[:200])
    if tags:
        parts.append("tags: " + ", ".join(tags[:8]))
    if source_ref and len(source_ref) <= 80:
        parts.append(f"ref={source_ref}")
    if not parts:
        return ""
    return " ".join(parts)


async def store_memory_with_extraction(
    content: str,
    *,
    summary: str | None = None,
    tags: list[str] | None = None,
    category: str | None = None,
    source_type: str = "manual",
    source_machine: str | None = None,
    source_ref: str | None = None,
    confidence: float = 1.0,
    metadata: dict | None = None,
    skip_dedup: bool = False,
    contextual_prefix: str | None = None,
    event_ts: "datetime | None" = None,
) -> dict:
    """Store a memory with embedding, write-path decision, and async entity extraction.

    This is the canonical way to store memories — both MCP tools and internal
    code (scheduler, crawler) should use this instead of queries.store_memory().

    Write-path actions (Mem0-style):
      - ADD: store as new memory
      - UPDATE: merge into existing memory
      - SUPERSEDE: archive old, store new
      - NOOP: exact duplicate, skip

    Args:
        contextual_prefix: Optional LLM-generated context that situates this
            memory within its source document. Prepended to the embedding input
            (NOT stored as content) for better retrieval. See Anthropic's
            contextual retrieval research (35-49% improvement).

    Returns:
        {"status": "stored"|"updated"|"superseded"|"skipped", ...}
    """
    confidence = max(0.0, min(confidence, 1.0))

    # Auto-tag 'lesson' when the memory documents a mistake-surfaced /
    # fix-applied narrative. See `_augment_tags_with_lesson` comment
    # block for the rationale — `lesson` is the orthogonal axis to
    # `confidence`, and we want every fix-type memory to be searchable
    # by "coding journey" queries regardless of who wrote it.
    tags = _augment_tags_with_lesson(tags, category, content, source_type)

    # Context-enriched embedding (with optional contextual prefix).
    # When the caller didn't supply an LLM-generated prefix (the common
    # single-memory case), fall back to a metadata-derived template prefix
    # so short memories still get a situating anchor. See
    # _default_situating_prefix for rationale.
    effective_prefix = contextual_prefix
    if not effective_prefix and settings.contextual_embeddings_enabled:
        effective_prefix = _default_situating_prefix(
            event_ts=event_ts,
            source_type=source_type,
            source_machine=source_machine,
            source_ref=source_ref,
            category=category,
            summary=summary,
            tags=tags,
        )

    embed_parts = []
    if effective_prefix:
        embed_parts.append(effective_prefix)
    if category:
        embed_parts.append(category)
    if tags:
        embed_parts.append(", ".join(tags))
    embed_input = ". ".join(embed_parts) + ". " + content if embed_parts else content
    embedding = await embed_text(embed_input)

    # Fast-path for low-value auto-captured categories: skip the LLM
    # dedup classifier entirely. session-log auto-captures from claude-code
    # Stop hooks land ~3× per session (even with client cooldown some slip
    # through), and dedup LLM takes 30-90s under n_parallel=1 to rule each
    # one NOOP — a full minute of GPU time spent to say "we already have
    # this". Short-circuiting here turns writes from 60-90s into 1-2s and
    # makes the queue drain fast. True semantic similarity is still caught
    # by the SHA256 pre-check in enqueue_memory_write (below).
    _FAST_PATH_CATEGORIES = {"session-log"}
    if category in _FAST_PATH_CATEGORIES:
        skip_dedup = True

    # Write-path decision: ADD / UPDATE / SUPERSEDE / NOOP
    if settings.extraction_enabled and not skip_dedup:
        try:
            from nobrainr.extraction.dedup import decide_write_action

            decision = await decide_write_action(content, embedding)
            action = decision.get("action", "ADD")

            if action == "NOOP":
                logger.info("Write path NOOP: %s", decision.get("reason"))
                return {
                    "status": "skipped",
                    "reason": decision.get("reason", "Duplicate"),
                }

            if action == "UPDATE":
                target_id = decision["target_id"]
                merged_content = decision["content"]
                new_embedding = await embed_text(merged_content)
                # Trigger snapshots old state automatically
                await queries.update_memory(
                    target_id,
                    content=merged_content,
                    embedding=new_embedding,
                    tags=tags,
                    category=category,
                    metadata=metadata,
                    _changed_by="mcp",
                    _change_type="dedup_update",
                    _change_reason=decision.get("reason", ""),
                )
                logger.info("Write path UPDATE %s: %s", target_id, decision.get("reason"))
                return {
                    "status": "updated",
                    "updated_id": target_id,
                    "reason": decision.get("reason", ""),
                }

            if action == "SUPERSEDE":
                target_id = decision["target_id"]
                # Trigger snapshots old state automatically
                await queries.update_memory(
                    target_id,
                    category="_archived",
                    metadata={"archived_reason": "superseded", "superseded_by": "pending"},
                    _changed_by="mcp",
                    _change_type="dedup_supersede",
                    _change_reason=decision.get("reason", ""),
                )
                if metadata is None:
                    metadata = {}
                metadata["supersedes"] = target_id
                logger.info("Write path SUPERSEDE %s: %s", target_id, decision.get("reason"))
                # Fall through to store new memory below

        except Exception:
            logger.exception("Write path decision failed, storing as new")

    # Store new memory (ADD or SUPERSEDE fall-through). Contextual BM25
    # (2026-04-19): the same prefix we prepend to the embedding input is
    # also written to the fts_context column so the FTS GIN index can see
    # it — Anthropic's contextual-retrieval paper documents 35%→49%
    # failure reduction when BOTH branches are contextualized.
    result = await queries.store_memory(
        content=content,
        embedding=embedding,
        summary=summary,
        source_type=source_type,
        source_machine=source_machine,
        source_ref=source_ref,
        tags=tags,
        category=category,
        confidence=confidence,
        metadata=metadata,
        fts_context=effective_prefix,
        event_ts=event_ts,
    )

    # For SUPERSEDE, backlink the archived memory. (2026-07-09) Two bugs
    # fixed: metadata arrives as a JSON STRING on the queued-write path
    # (asyncpg jsonb), so .get() never fired; and the backlink wrote
    # metadata jsonb instead of the superseded_by COLUMN that search
    # filters and the trust formula actually read. 387 claimed chains vs
    # 4 real columns at discovery.
    if isinstance(metadata, str):
        try:
            import json as _json
            metadata = _json.loads(metadata)
        except Exception:
            metadata = None
    if metadata and metadata.get("supersedes"):
        old_id = metadata["supersedes"]
        try:
            await queries.supersede_memory(
                old_id, result["id"], reason="explicit supersedes at store",
            )
        except Exception:
            logger.warning("Failed to backlink superseded memory %s", old_id)
        _schedule_extraction(result["id"], content, tags)
        return {"status": "superseded", "new_id": result["id"], "archived_id": old_id}

    # Fire-and-forget entity extraction
    if settings.extraction_enabled:
        _schedule_extraction(result["id"], content, tags)

    return {"status": "stored", **result}


async def store_document_chunked(
    content: str,
    *,
    title: str | None = None,
    summary: str | None = None,
    tags: list[str] | None = None,
    category: str | None = None,
    source_type: str = "document",
    source_machine: str | None = None,
    source_ref: str | None = None,
    confidence: float = 0.8,
    metadata: dict | None = None,
    max_chars: int | None = None,
    overlap: int | None = None,
) -> dict:
    """Store a long document as chunked memories with linking metadata.

    If the content is shorter than the chunk threshold, stores as a single
    memory via the normal path.  Otherwise splits into overlapping chunks,
    stores each one, and links them via metadata.

    Returns:
        {"status": "stored", "chunks": N, "memory_ids": [...], "document_id": "..."}
    """
    from nobrainr.services.chunking import chunk_text

    content = content.strip()
    if not content:
        return {"error": "Empty content"}

    # Short content — store as single memory
    if len(content) <= settings.chunk_threshold:
        result = await store_memory_with_extraction(
            content=content,
            summary=summary or (f"Document: {title}" if title else None),
            tags=tags,
            category=category,
            source_type=source_type,
            source_machine=source_machine,
            source_ref=source_ref,
            confidence=confidence,
            metadata=metadata,
        )
        return {**result, "chunks": 1, "memory_ids": [result.get("id") or result.get("updated_id", "")]}

    # Chunk the content
    chunks = chunk_text(content, max_chars=max_chars, overlap=overlap)
    if not chunks:
        return {"error": "Chunking produced no output"}

    # Generate a document ID to link all chunks
    import uuid
    document_id = str(uuid.uuid4())

    # Generate contextual prefixes for multi-chunk documents (Anthropic contextual retrieval)
    # This improves embedding quality by 35-49% for chunked content
    contextual_prefixes: dict[int, str] = {}
    if settings.contextual_embeddings_enabled and len(chunks) > 1:
        doc_summary = f"{title or source_ref or 'Document'}. {content[:500]}"
        for chunk in chunks:
            try:
                prefix = await _generate_chunk_context(doc_summary, chunk.text)
                if prefix:
                    contextual_prefixes[chunk.index] = prefix
            except Exception:
                pass  # Fall back to no prefix for this chunk

    memory_ids: list[str] = []
    stored = 0
    skipped = 0

    for chunk in chunks:
        chunk_meta = dict(metadata or {})
        chunk_meta.update({
            "document_id": document_id,
            "chunk_index": chunk.index,
            "chunk_total": chunk.total,
            "chunk_offset": chunk.char_offset,
        })
        if title:
            chunk_meta["document_title"] = title
        ctx_prefix = contextual_prefixes.get(chunk.index, "")
        if ctx_prefix:
            chunk_meta["contextual_prefix"] = ctx_prefix

        chunk_summary = title or summary or source_ref or "Document chunk"
        if chunk.total > 1:
            chunk_summary = f"{chunk_summary} [{chunk.index + 1}/{chunk.total}]"

        result = await store_memory_with_extraction(
            content=chunk.text,
            summary=chunk_summary[:200],
            tags=tags,
            category=category,
            source_type=source_type,
            source_machine=source_machine,
            source_ref=source_ref,
            confidence=confidence,
            metadata=chunk_meta,
            skip_dedup=True,  # Don't dedup individual chunks
            contextual_prefix=ctx_prefix,
        )

        mid = result.get("id") or result.get("updated_id", "")
        if mid:
            memory_ids.append(mid)
            stored += 1
        else:
            skipped += 1

    return {
        "status": "stored",
        "document_id": document_id,
        "chunks": stored,
        "skipped": skipped,
        "memory_ids": memory_ids,
    }


def _schedule_extraction(memory_id: str, content: str, tags: list[str] | None) -> None:
    """Schedule entity extraction as a background task."""

    async def _run():
        async with _extraction_semaphore:
            try:
                from nobrainr.extraction.pipeline import process_memory

                await process_memory(memory_id, content, tags)
            except Exception:
                logger.exception("Extraction failed for %s", memory_id)
            await asyncio.sleep(1)  # Brief cooldown between extractions

    try:
        asyncio.create_task(_run())
    except Exception:
        logger.exception("Failed to start extraction task for %s", memory_id)
