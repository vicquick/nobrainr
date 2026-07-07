"""Orchestrator — runs extraction pipeline on memories."""

import asyncio
import logging
import os
import re
from typing import Callable

from nobrainr.db.queries import (
    find_or_create_entity,
    get_nearby_entities_for_memory,
    get_unextracted_memories,
    link_entity_to_memory,
    set_extraction_status,
    store_entity_relation,
)
from nobrainr.embeddings.ollama import embed_batch, embed_text
from nobrainr.extraction.extractor import extract_entities
from nobrainr.extraction.models import normalize_relationship

logger = logging.getLogger("nobrainr")

# Post-extraction noise filter: reject entities that are too short, numeric, or generic
_NOISE_RE = re.compile(r"^[0-9./\-]+$")  # pure numbers/versions/dates
_BRANCH_RE = re.compile(r"^(feature|agent|bugfix|hotfix|release|develop)/", re.IGNORECASE)
_FUNC_RE = re.compile(r"^[\w.]+\(\)$")  # function calls: strpos(), format(), etc.
_CLI_FLAG_RE = re.compile(r"^--?\w")  # CLI flags: --content, -v, etc.
_CSS_SELECTOR_RE = re.compile(r"^[\w-]+\.[\w-]+$")  # CSS selectors: html.dark-mode
_TRIVIAL_FILE_RE = re.compile(r"^\w+\.(txt|log|tmp|bak|csv|json|xml|yaml|yml|ini|cfg|conf)$", re.IGNORECASE)
_RESOLUTION_RE = re.compile(r"^\d+\s*(PPI|DPI|px|pt|em|rem|%)$", re.IGNORECASE)  # 300 PPI, 72 DPI
_CAMELCASE_VAR_RE = re.compile(
    r"^(get|set|on|handle|update|create|load|init|dispatch|enforce|dispose|is|has|should|can|will|"
    r"fetch|render|toggle|parse|validate|process|compute|calculate|check|find|show|hide|enable|disable)"
    r"[A-Z][a-zA-Z0-9]*$"
)  # camelCase function/variable names: initializeMap, enforceBackgroundColor (NOT tech names like pgRouting)
_VAGUE_PHRASE_RE = re.compile(r"^(the |a |an )?\w+\s+(color|mode|size|type|name|value|data|list|set|map|view|state|style|config|option)s?$", re.IGNORECASE)
_GENERIC_NAMES = frozenset({
    "main", "fix", "update", "test", "bug", "feature", "release", "merge",
    "commit", "branch", "tag", "none", "null", "true", "false", "yes", "no",
    "unknown", "default", "master", "develop", "head", "base", "origin",
    "main street",  # common misextraction from "main" branch
    # Generic terms the LLM sometimes extracts as entities
    "project", "agent", "server", "client", "frontend", "backend", "api",
    "database", "file", "code", "script", "function", "model", "config",
    "configuration", "configuration files", "server logic", "cli scripts",
    "comment", "request_changes", "review", "pull request", "issue",
    "error", "warning", "info", "debug", "log", "output", "input",
    "data", "result", "response", "request", "query", "search",
    # Python/JS builtins — too fundamental to be knowledge entities
    "int", "str", "float", "bool", "list", "dict", "set", "tuple",
    "len", "ord", "pow", "sum", "min", "max", "abs", "map", "zip",
    "any", "all", "type", "print", "range", "open", "super", "iter",
    "next", "hash", "repr", "eval", "exec", "chr", "hex", "oct", "bin",
    # Python/JS runtime objects
    "session", "venv", "__pycache__", "self", "cls", "args", "kwargs",
    "module", "package", "class", "method", "variable", "parameter",
    "object", "instance", "constructor", "prototype", "callback",
    "promise", "async", "await", "import", "export", "return",
    # HTML element IDs — too granular for knowledge graph
    "div", "span", "button", "form", "table", "body", "header", "footer",
    "container", "wrapper", "content", "sidebar", "modal", "dialog",
    # German common words (from German-language conversations)
    "länge", "wand", "weg", "bau", "alt", "neu", "zahl", "fläche",
    "breite", "höhe", "name", "text", "datum", "art", "typ", "form",
    "ort", "raum", "haus", "stadt", "land", "user", "tab",
    # Common abbreviations that aren't real entities
    "num", "val", "var", "obj", "arr", "idx", "tmp", "cfg",
    "src", "dst", "msg", "req", "res", "ctx", "env", "cmd",
    "arg", "fmt", "buf", "ptr", "ref", "err", "col", "row",
    "key", "sql", "url", "uri", "csv", "xml", "svg", "pdf",
    "img", "btn", "nav", "app", "lib", "doc", "sys", "pid",
    "uid", "gid", "tls", "ssh", "dns", "tcp", "udp", "ftp",
})


def _is_noise_entity(name: str) -> bool:
    """Return True if this entity name is noise and should be skipped."""
    if len(name) <= 3:
        return True
    if _NOISE_RE.match(name):
        return True
    if name.lower() in _GENERIC_NAMES:
        return True
    # Git branch names are not meaningful entities
    if _BRANCH_RE.match(name):
        return True
    # Function calls (strpos(), substr(), format()) — too granular
    if _FUNC_RE.match(name):
        return True
    # CLI flags (--content, --verbose) — not knowledge entities
    if _CLI_FLAG_RE.match(name):
        return True
    # CSS selectors (html.dark-mode) — too granular for knowledge graph
    if _CSS_SELECTOR_RE.match(name):
        return True
    # Trivial file names (urls.txt, data.csv) — not meaningful unless specific
    if _TRIVIAL_FILE_RE.match(name):
        return True
    # Resolution/unit strings (300 PPI, 72 DPI)
    if _RESOLUTION_RE.match(name):
        return True
    # camelCase function/variable names starting with verb prefixes (initializeMap, enforceBackgroundColor)
    if _CAMELCASE_VAR_RE.match(name):
        return True
    # Vague two-word phrases (water color, background color, map configuration)
    if _VAGUE_PHRASE_RE.match(name):
        return True
    return False


_NOISE_CONTENT_RE = re.compile(
    r"^(ok|okay|sure|thanks|thank you|yes|no|yep|nope|got it|understood|"
    r"right|alright|cool|nice|great|good|fine|hmm|hm|ah|oh|lol|haha|"
    r"👍|👌|✅|❌|🙏|😊|🤔)\s*[.!?]*$",
    re.IGNORECASE,
)
_SYSTEM_LINE_RE = re.compile(
    r"^(\[system\]|\[routing\]|HEARTBEAT|ping|pong|timestamp:|session:|"
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})",
    re.IGNORECASE,
)


def _prefilter_content(text: str) -> str | None:
    """Pre-filter content before LLM extraction. Returns None if content is noise."""
    if not text or len(text.strip()) < 30:
        return None

    # Strip system/routing lines
    lines = text.split("\n")
    clean_lines = [ln for ln in lines if not _SYSTEM_LINE_RE.match(ln.strip())]
    cleaned = "\n".join(clean_lines).strip()

    if not cleaned or len(cleaned) < 30:
        return None

    # Single-line noise check (acknowledgments, emojis)
    if _NOISE_CONTENT_RE.match(cleaned):
        return None

    return cleaned


async def process_memory(
    memory_id: str,
    content: str,
    tags: list[str] | None = None,
) -> None:
    """Run the full extraction pipeline on a single memory.

    1. Mark extraction as pending
    2. Extract entities and relationships via LLM
    3. Create/find entities with embeddings, link to memory
    4. Store relationships between entities
    5. Mark extraction as done (or failed on error)
    """
    try:
        # Pre-filter: skip noise content before wasting GPU on extraction
        filtered_content = _prefilter_content(content)
        if filtered_content is None:
            await set_extraction_status(memory_id, "done")
            logger.debug("Skipped extraction for %s — content is noise/too short", memory_id)
            return
        content = filtered_content

        await set_extraction_status(memory_id, "pending")

        # Fetch neighborhood context: embed the memory, find nearby entities
        # Skip when NOBRAINR_SKIP_EXTRACTION_EMBED=true (avoids model swap during bulk backfill)
        known_entities = None
        skip_embed = os.environ.get("NOBRAINR_SKIP_EXTRACTION_EMBED", "").lower() in ("true", "1", "yes")
        if not skip_embed:
            try:
                mem_embedding = await embed_text(content[:1500])  # quick embed for lookup
                nearby = await get_nearby_entities_for_memory(
                    mem_embedding, limit=15, min_mentions=2,
                )
                if nearby:
                    known_entities = [
                        {"name": e["name"], "entity_type": e["entity_type"],
                         "description": e.get("description", "")}
                        for e in nearby
                        if e.get("similarity", 0) > 0.3  # only reasonably relevant
                    ]
            except Exception:
                logger.debug("Neighborhood lookup failed for %s, proceeding without", memory_id)

        result = await extract_entities(content, known_entities=known_entities)

        # Map entity names to their resolved IDs for relationship linking
        entity_id_map: dict[str, str] = {}

        # Filter noise entities before processing
        clean_entities = [e for e in result.entities if not _is_noise_entity(e.name)]
        if len(clean_entities) < len(result.entities):
            skipped = len(result.entities) - len(clean_entities)
            logger.debug("Filtered %d noise entities from memory %s", skipped, memory_id)
        clean_names = {e.name for e in clean_entities}

        # Batch-embed all entities at once (context-enriched: "type: name - description")
        # Skip when NOBRAINR_SKIP_EXTRACTION_EMBED=true (bulk backfill mode)
        if clean_entities:
            embed_texts = []
            for entity in clean_entities:
                desc = entity.description or entity.name
                embed_texts.append(f"{entity.entity_type}: {entity.name} - {desc}")
            if skip_embed:
                embeddings = [None] * len(embed_texts)
            else:
                embeddings = await embed_batch(embed_texts)

            for entity, embedding in zip(clean_entities, embeddings):
                entity_id = await find_or_create_entity(
                    entity.name,
                    entity.entity_type,
                    description=entity.description,
                    embedding=embedding,
                )
                entity_id_map[entity.name] = entity_id

                await link_entity_to_memory(
                    memory_id, entity_id, role="mention", confidence=1.0,
                )

        for rel in result.relationships:
            # Closed predicate vocabulary (P2a, 2026-07-07): normalize the
            # type onto the canonical list, swapping direction for inverse
            # predicates; drop anything unmappable. Grammar-constrained
            # decoding already enforces the enum on the main path — this
            # covers the salvage-parser path and legacy callers.
            norm = normalize_relationship(rel.relationship_type, rel.source, rel.target)
            if norm is None:
                continue
            rel_type, src_name, tgt_name = norm

            # Skip relationships involving filtered noise entities
            if src_name not in clean_names or tgt_name not in clean_names:
                continue

            source_id = entity_id_map.get(src_name)
            target_id = entity_id_map.get(tgt_name)

            if not source_id or not target_id:
                continue

            await store_entity_relation(
                source_id,
                target_id,
                rel_type,
                confidence=rel.confidence,
                source_memory=memory_id,
            )

        await set_extraction_status(memory_id, "done")

        stored_rels = sum(
            1 for r in result.relationships
            if r.source in entity_id_map and r.target in entity_id_map
            and r.source in clean_names and r.target in clean_names
        )
        logger.info(
            "Extracted %d entities (%d raw), %d relationships from memory %s",
            len(clean_entities), len(result.entities), stored_rels, memory_id,
        )

        # Notify the dashboard only when the entity graph actually changed —
        # memory_created fires on every write (session logs, scheduler) and
        # made the graph's "update available" banner cry wolf.
        if entity_id_map or stored_rels:
            try:
                from nobrainr.events import publish
                publish("entities_changed", {
                    "memory_id": memory_id,
                    "entities": len(entity_id_map),
                    "relations": stored_rels,
                })
            except Exception:
                pass

    except Exception:
        logger.exception("Extraction pipeline failed for memory %s", memory_id)
        await set_extraction_status(memory_id, "failed")


async def backfill(
    batch_size: int = 5,
    concurrency: int = 4,
    on_progress: Callable[[int, dict], None] | None = None,
) -> int:
    """Process all unextracted memories in batches with concurrency.

    Args:
        batch_size: Number of memories to fetch per batch.
        concurrency: Max concurrent extraction tasks (match Ollama NUM_PARALLEL).
        on_progress: Optional callback(processed_count, memory_dict) for CLI progress.

    Returns:
        Total number of memories processed.
    """
    total = 0
    sem = asyncio.Semaphore(concurrency)

    async def _process_one(memory: dict) -> bool:
        async with sem:
            try:
                await process_memory(
                    memory_id=memory["id"],
                    content=memory["content"],
                    tags=memory.get("tags"),
                )
                return True
            except Exception:
                return False

    while True:
        batch = await get_unextracted_memories(batch_size)
        if not batch:
            break

        tasks = [asyncio.create_task(_process_one(m)) for m in batch]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for memory, result in zip(batch, results):
            if result is True:
                total += 1
            if on_progress:
                on_progress(total, memory)

    logger.info("Backfill complete: %d memories processed", total)
    return total
