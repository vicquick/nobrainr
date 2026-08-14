"""nobrainr MCP server — collective agent memory with knowledge graph."""

import logging
import re as _re
from uuid import UUID, uuid4

from mcp.server.fastmcp import FastMCP

from nobrainr.config import settings
from nobrainr.db import queries
from nobrainr.embeddings.ollama import embed_text
from nobrainr.services.memory import store_memory_with_extraction
from nobrainr.utils.categories import normalize_category


def _validate_uuid(value: str) -> str:
    """Validate and return a UUID string. Raises ValueError on invalid input."""
    UUID(value)
    return value

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("nobrainr")

# ──────────────────────────────────────────────
# FastMCP instance (no lifespan — parent app handles it)
# ──────────────────────────────────────────────

mcp = FastMCP(
    "nobrainr",
    host=settings.host,
    port=settings.port,
    stateless_http=True,
    instructions=(
        "nobrainr is a self-improving collective memory service for AI agents with a knowledge graph.\n\n"
        "## Core workflow\n"
        "1. ALWAYS call `memory_search` before starting any task — check what's already known. "
        "This is CRITICAL: past sessions may have solved the same problem, established conventions, "
        "or documented gotchas. Searching first prevents duplicate work and repeated mistakes.\n"
        "2. If the task involves an ARCHITECTURAL or TECHNICAL CHOICE (stack selection, config "
        "policy, trade-off, 'should we do X or Y') — ALSO call `decision_search(query, scope=...)` "
        "to see if this is already settled policy. Decisions are separate from free-form memories: "
        "they're prescriptive choices with rationale + rejected alternatives. Re-arguing a "
        "decided choice wastes cycles.\n"
        "3. Use `memory_store` for observations/learnings — things you DISCOVERED. Use "
        "`decision_store(scope, decision, rationale, constraints=, alternatives_rejected=)` for "
        "explicit CHOICES you make. The distinction matters: learnings are descriptive ('BGE "
        "CPU is 1s/doc'), decisions are prescriptive ('cap BGE at 8'). Writes are queued — the "
        "tool returns in <50ms with a `queue_id`, and a background worker processes the full "
        "pipeline (embedding, dedup, entity extraction). You usually don't need to wait.\n"
        "4. Call `memory_feedback` after using search results — report if they were helpful. Pass "
        "through `search_trace_id`, `search_rank`, and `search_query` from the original result so "
        "we can compute rank-aware metrics (MRR/NDCG).\n"
        "5. Call `memory_reflect` at session end with a batch of learnings from the session.\n"
        "6. Use `log_event` to record significant agent activity (session starts, decisions, completions).\n\n"
        "## Write path — queued by default\n"
        "- `memory_store` returns `{status: 'queued', queue_id, enqueued_at}` in <50ms. The worker\n"
        "  processes writes FIFO through the same embedding+dedup+extraction pipeline as before.\n"
        "  Pass `wait=True` only if you genuinely need to block for the memory_id. Most of the time\n"
        "  you don't — the write is durable the moment the queue row exists.\n"
        "- `memory_store_status(queue_id)` — poll a queued write to see {status, memory_id,\n"
        "  result_status, error_message}. Use when you need to close the loop on a write.\n"
        "- `memory_store_document` — long-document path, same queue. Returns one `queue_id` per\n"
        "  chunk with a shared `document_id` in metadata. Contextual prefixes are filled in later\n"
        "  by a scheduler job, not on the hot path.\n"
        "- `crawl_and_store` — crawl via Crawl4AI (synchronous) + enqueue the result via the\n"
        "  document queue path.\n\n"
        "## Search & retrieval\n"
        "- `memory_search` — hybrid semantic + text search, reranked. Every result row carries\n"
        "  `search_trace_id`, `search_rank` (1-indexed), and `search_query` so you can close the\n"
        "  feedback loop via memory_feedback and populate MRR/NDCG metrics. Prefer `hybrid=True`.\n"
        "  Results include learnings, decisions, and all other memory types mixed together.\n"
        "- `decision_search` — DECISIONS ONLY (category='decision', status='active' by default).\n"
        "  Use BEFORE making an architectural or technical choice to see prior policy. Takes\n"
        "  optional `scope` prefix ('nobrainr', 'bimavo/gaeb-parser') and `status` filter.\n"
        "  Results come back without the noise of free-form memories.\n"
        "- `memory_query` — structured filtering by tags, category, source.\n"
        "- `entity_search` / `entity_graph` — knowledge graph exploration.\n"
        "- `graph_search` / `fact_search` — entity-graph and fact-layer retrieval.\n\n"
        "## Closing the feedback loop\n"
        "After you actually USE a search result (acted on it, learned from it, cited it), call\n"
        "`memory_feedback(memory_id, was_useful=True, query_trace_id=<from result>, "
        "result_rank=<from result>, query_text=<from result>)`. Negative feedback is equally "
        "valuable — if a result was irrelevant, set `was_useful=False` with the same trace fields. "
        "integrate_feedback_scores only adjusts importance when it sees ≥5 events with ≥1 negative, "
        "so silent positive-only loops no longer inflate scores.\n\n"
        "## Best practices\n"
        "- Always tag memories well so they can be found later.\n"
        "- CRITICAL: Set `source_machine` to YOUR machine (worklaptop/workserver/bimavo/personalpc/etc.), NOT the server hostname. "
        "The MCP server always runs on bimavo but memories are created by agents on many machines. "
        "Wrong source_machine = broken recall by machine. When in doubt, check hostname or read CLAUDE.md.\n"
        "- Use canonical categories: architecture, debugging, deployment, infrastructure, patterns, "
        "tooling, security, frontend, backend, data, business, documentation, session-log, insight, decision.\n"
        "- Decision vs learning: a LEARNING is a discovery ('found that X behaves Y'); a DECISION\n"
        "  is a deliberate choice ('we picked A over B because C'). If you write 'we use X instead\n"
        "  of Y because Z' in a memory, that belongs in `decision_store`, not `memory_store`.\n"
        "- Feedback improves future search ranking — always report usefulness with the trace fields.\n"
        "- Maintenance runs automatically; `memory_maintenance` is available for manual runs."
    ),
)


# ──────────────────────────────────────────────
# Resources: read-only data for agent context
# ──────────────────────────────────────────────

@mcp.resource("nobrainr://briefing")
async def briefing() -> dict:
    """System briefing — stats, recent activity, top communities. Read this on session start."""
    from nobrainr.db.pool import get_pool

    pool = await get_pool()
    async with pool.acquire() as conn:
        stats = {}
        stats["total_memories"] = await conn.fetchval("SELECT count(*) FROM memories")
        stats["total_entities"] = await conn.fetchval("SELECT count(*) FROM entities")
        stats["total_relations"] = await conn.fetchval(
            "SELECT count(*) FROM entity_relations WHERE valid = true"
        )
        stats["new_24h"] = await conn.fetchval(
            "SELECT count(*) FROM memories WHERE created_at > now() - interval '24 hours'"
        )
        stats["embedding_model"] = settings.embedding_model

        # Top categories
        cats = await conn.fetch(
            "SELECT category, count(*) AS n FROM memories WHERE category IS NOT NULL "
            "GROUP BY category ORDER BY n DESC LIMIT 5"
        )
        stats["top_categories"] = {r["category"]: r["n"] for r in cats}

        # Top communities (if available)
        has_comm = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'community_summaries')"
        )
        if has_comm:
            comms = await conn.fetch(
                "SELECT title, summary, member_count FROM community_summaries ORDER BY member_count DESC LIMIT 5"
            )
            stats["top_communities"] = [
                {"title": c["title"], "summary": c["summary"], "members": c["member_count"]}
                for c in comms
            ]

        # Recent agent events
        events = await conn.fetch(
            "SELECT event_type, description, created_at FROM agent_events "
            "ORDER BY created_at DESC LIMIT 5"
        )
        stats["recent_events"] = [
            {"type": e["event_type"], "description": e["description"][:100] if e["description"] else "", "at": str(e["created_at"])}
            for e in events
        ]

    return stats


@mcp.resource("nobrainr://categories")
async def categories_resource() -> list[dict]:
    """Available memory categories with counts."""
    from nobrainr.db.pool import get_pool

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT category, count(*) AS count FROM memories "
            "WHERE category IS NOT NULL GROUP BY category ORDER BY count DESC"
        )
        return [{"category": r["category"], "count": r["count"]} for r in rows]


@mcp.resource("nobrainr://machines")
async def machines_resource() -> list[dict]:
    """Connected machines/agents with memory counts."""
    from nobrainr.db.pool import get_pool

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT source_machine, count(*) AS count FROM memories "
            "WHERE source_machine IS NOT NULL GROUP BY source_machine ORDER BY count DESC"
        )
        return [{"machine": r["source_machine"], "count": r["count"]} for r in rows]


@mcp.resource("nobrainr://entity-types")
async def entity_types_resource() -> list[dict]:
    """Entity types in the knowledge graph with counts."""
    from nobrainr.db.pool import get_pool

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT entity_type, count(*) AS count FROM entities GROUP BY entity_type ORDER BY count DESC"
        )
        return [{"type": r["entity_type"], "count": r["count"]} for r in rows]


@mcp.resource("nobrainr://card/{subject}")
async def context_card_resource(subject: str) -> dict:
    """Learned-context card for a subject (entity name / project / community).

    A pre-thought, trust-filtered brief built by the card_builder job from
    the subject's highest-trust memories — read this instead of running
    several searches. Returns {found: false} when no card exists yet.
    """
    from nobrainr.db.pool import get_pool

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT title, body, trust_score, built_at,
                   array_length(source_ids, 1) AS n_sources
            FROM context_cards
            WHERE subject_key ILIKE $1
            ORDER BY built_at DESC LIMIT 1
            """,
            subject,
        )
    if not row:
        return {"found": False, "subject": subject}
    return {
        "found": True, "subject": subject, "title": row["title"],
        "brief": row["body"], "trust_score": row["trust_score"],
        "sources": row["n_sources"], "built_at": str(row["built_at"]),
    }


# ──────────────────────────────────────────────
# Prompts: structured agent workflows
# ──────────────────────────────────────────────

@mcp.prompt()
def session_start(machine: str = "unknown", task: str = "") -> str:
    """Prompt for starting a new agent session — searches for relevant context."""
    return (
        f"Starting session on machine '{machine}'.\n"
        f"Task: {task}\n\n"
        "Before beginning work:\n"
        "1. Call memory_search with keywords from the task to find relevant prior knowledge\n"
        "2. Call log_event with event_type='session_start' to record this session\n"
        "3. Review any returned memories for established conventions, known issues, or past solutions\n"
        "4. If the task involves a specific entity, call entity_graph to explore its connections"
    )


@mcp.prompt()
def session_end(learnings: str = "") -> str:
    """Prompt for ending a session — saves learnings and logs completion."""
    return (
        "Session ending. Before closing:\n"
        "1. Call memory_reflect with all significant learnings from this session:\n"
        f"   {learnings}\n"
        "2. Call log_event with event_type='session_end'\n"
        "3. For any search results you used, call memory_feedback to report usefulness\n"
        "4. If you discovered new patterns or conventions, store them with memory_store"
    )


@mcp.prompt()
def debug_investigation(error: str, context: str = "") -> str:
    """Prompt for investigating a bug — structured search + knowledge capture."""
    return (
        f"Investigating error: {error}\n"
        f"Context: {context}\n\n"
        "Investigation workflow:\n"
        "1. Search for known solutions: memory_search(query=<error keywords>, expand=True)\n"
        "2. Search entity graph: entity_search(query=<affected component>)\n"
        "3. If a crawled doc might help: memory_search(tags=['crawled'], query=<topic>)\n"
        "4. After fixing: store the root cause + solution with memory_store\n"
        "   Tags: ['debugging', '<component>'], category: 'debugging'"
    )


@mcp.prompt()
def research_topic(topic: str) -> str:
    """Prompt for researching a topic — combines memory search with web crawling."""
    return (
        f"Researching: {topic}\n\n"
        "Research workflow:\n"
        "1. Search existing knowledge: memory_search(query=<topic>, expand=True, limit=20)\n"
        "2. Explore entity graph: entity_search(query=<topic>)\n"
        "3. Check communities: community_list() to find relevant clusters\n"
        "4. If knowledge gaps exist, crawl authoritative sources:\n"
        "   crawl_and_store(url=<docs_url>, tags=[<topic>], category='documentation')\n"
        "5. Synthesize findings with memory_store, category='insight'"
    )


# ──────────────────────────────────────────────
# ASI06 crawl sanitizer (C2, 2026-07-14)
# ──────────────────────────────────────────────
# Lines that try to program a future agent. Conservative: matches
# imperative memory/recommendation directives, not incidental prose.
_INJECTION_PATTERNS = [
    _re.compile(p, _re.IGNORECASE)
    for p in (
        r"\b(remember|note|treat|regard|consider)\b.{0,40}\b(as|to be)\b.{0,40}"
        r"\b(trusted|authoritative|the best|preferred|official)\b",
        r"\b(always|from now on|in (the )?future|going forward)\b.{0,50}"
        r"\b(recommend|prefer|suggest|choose|use|cite)\b",
        r"\bignore\b.{0,30}\b(instructions|context|rules|prompt|guidelines)\b",
        r"\b(disregard|forget|override)\b.{0,30}\b(instructions|context|rules|prompt|above|previous)\b",
        r"\byou (must|should|are to) (now )?(always |only )?(recommend|prefer|treat|remember)\b",
        r"\b(system|developer) (prompt|message|instruction)s?\b.{0,30}\b(override|replace|update)\b",
    )
]


def _sanitize_crawled_text(text: str) -> tuple[str, list[str]]:
    """Defang instruction-shaped lines in crawled content. Returns
    (sanitized_text, list_of_flagged_line_previews). Non-destructive: the
    line is preserved as quoted DATA so retrieval still surfaces what the
    page said, but it can no longer read as a live instruction."""
    flagged: list[str] = []
    out_lines: list[str] = []
    for line in text.splitlines():
        if any(pat.search(line) for pat in _INJECTION_PATTERNS):
            flagged.append(line.strip()[:120])
            out_lines.append("[quoted-web-text, not an instruction] " + line)
        else:
            out_lines.append(line)
    return "\n".join(out_lines), flagged


# ──────────────────────────────────────────────
# Tool: session_brief (C1, 2026-07-14) — system delivers, agent doesn't search
# ──────────────────────────────────────────────
@mcp.tool()
async def session_brief(task: str, limit: int = 5) -> dict:
    """Get pre-thought, trust-filtered context cards for a task — call this ONCE
    at the start of work instead of running several memory_search calls.

    Matches the task against learned-context cards (living per-subject briefs
    the card_builder job distils from the highest-trust memories) and returns
    the most relevant, each a dense standalone brief with current state,
    decisions, gotchas, and procedures. Superseded and low-trust knowledge is
    already excluded — what you get is what's current and trusted.

    Args:
        task: What you're about to work on (a phrase or sentence).
        limit: Max cards to return (default 5).

    Returns:
        {"cards": [{title, brief, subject, trust_score, built_at}], "count": N}
        Fall back to memory_search for anything the cards don't cover.
    """
    from nobrainr.db.pool import get_pool

    limit = max(1, min(limit, 12))
    pool = await get_pool()
    # Match cards by trigram similarity on title/subject + FTS on body —
    # cheap, no LLM, no embedding-of-cards needed (cards are few).
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT title, body, subject_type, subject_key, trust_score, built_at,
                       published_accuracy,
                       GREATEST(
                         similarity(lower(subject_key), lower($1)),
                         similarity(lower(title), lower($1)),
                         ts_rank(to_tsvector('simple', body),
                                 plainto_tsquery('simple', $1)) * 4
                       ) AS score
                FROM context_cards
                ORDER BY score DESC
                LIMIT $2
                """,
                task, limit,
            )
    except Exception:
        import logging
        logging.getLogger("nobrainr").exception("session_brief query failed")
        return {"cards": [], "count": 0, "error": "brief lookup failed"}
    cards = [
        {"title": r["title"], "brief": r["body"], "subject": r["subject_key"],
         "trust_score": r["trust_score"], "built_at": str(r["built_at"]),
         # M1: fact-checked accuracy (supported/(supported+contradicted)).
         # None = not yet checked. Treat < 0.7 as "verify before relying".
         "published_accuracy": r["published_accuracy"]}
        for r in rows if (r["score"] or 0) > 0.05
    ]
    return {"cards": cards, "count": len(cards)}


# ──────────────────────────────────────────────
# Tool: memory_store (queued write — returns in <50ms)
# ──────────────────────────────────────────────
@mcp.tool()
async def memory_store(
    content: str,
    summary: str | None = None,
    tags: list[str] | None = None,
    category: str | None = None,
    source_type: str = "manual",
    source_machine: str | None = None,
    source_ref: str | None = None,
    confidence: float = 1.0,
    metadata: dict | None = None,
    wait: bool = False,
) -> dict:
    """Store a new memory. Queued by default — returns in <50ms with a queue_id.

    The actual write path (embedding + dedup LLM classification + storage +
    entity extraction) runs on a background worker. This keeps the MCP call
    fast regardless of GPU contention — before this change, a busy
    llama-server could make memory_store hang for 10+ minutes and silently
    time out the caller.

    Args:
        content: The knowledge/learning/decision to remember.
        summary: One-line summary for quick scanning.
        tags: List of tags for categorization (e.g. ["python", "debugging", "asyncio"]).
        category: High-level category (e.g. "architecture", "debugging", "ops", "pattern").
        source_type: Where this came from ("manual", "chatgpt", "claude", "agent").
        source_machine: Which host generated this (e.g. "my-server", "laptop").
        source_ref: Reference to original source (conversation ID, file path, etc.).
        confidence: How reliable is this knowledge (0.0-1.0, default 1.0).
        metadata: Any additional structured data.
        wait: If True, poll the queue and return only when the write is
            fully processed (or 60s has elapsed). Use sparingly — the
            whole point of the queue is that you don't have to wait.
            Most callers should leave this False and, if they need the
            memory_id, follow up with memory_store_status(queue_id).

    Returns:
        - Default: {"status": "queued", "queue_id": "...", "enqueued_at": "..."}
        - With wait=True and completion within 60s: full status from the worker
          including memory_id and result_status
        - With wait=True and 60s timeout: {"status": "queued_waiting_timed_out", ...}
          — the write is STILL durable, just not yet complete
    """
    if len(content) > settings.max_content_length:
        return {
            "error": f"Content too large ({len(content)} chars, max {settings.max_content_length})"
        }

    category = normalize_category(category)

    from nobrainr.db import write_queue

    enq = await write_queue.enqueue_memory_write(
        content=content,
        summary=summary,
        tags=tags,
        category=category,
        source_type=source_type,
        source_machine=source_machine,
        source_ref=source_ref,
        confidence=confidence,
        metadata=metadata,
    )

    if not wait:
        return {
            "status": "queued",
            "queue_id": enq["queue_id"],
            "enqueued_at": enq["enqueued_at"],
            "message": (
                "Write accepted and durably queued. The background worker "
                "will process it serially. Poll memory_store_status(queue_id) "
                "if you need the memory_id."
            ),
        }

    # wait=True: poll the worker's status for up to 60s.
    import asyncio
    loop = asyncio.get_event_loop()
    deadline = loop.time() + 60.0
    while loop.time() < deadline:
        status = await write_queue.get_queue_status(enq["queue_id"])
        if status and status["status"] in ("done", "failed"):
            return status
        await asyncio.sleep(0.5)

    # Still in-flight after 60s — the write is durable, the caller just
    # has to come back later. Return the current status so they can see
    # whether it's still pending or actively processing.
    status = await write_queue.get_queue_status(enq["queue_id"])
    return {
        "status": "queued_waiting_timed_out",
        "queue_id": enq["queue_id"],
        "last_observed": status,
        "message": (
            "Write is durably queued but did not complete within 60s. "
            "It will still be processed — poll memory_store_status later."
        ),
    }


# ──────────────────────────────────────────────
# Tool: memory_store_status (poll queued writes)
# ──────────────────────────────────────────────
@mcp.tool()
async def memory_store_status(queue_id: str) -> dict:
    """Check the status of a queued memory write.

    Use this to close the loop on a previous ``memory_store`` call when you
    need the final ``memory_id`` or want to confirm success.

    Args:
        queue_id: The UUID returned by memory_store in its queue_id field.

    Returns:
        {
            "queue_id": "...",
            "status": "pending" | "processing" | "done" | "failed",
            "attempts": int,
            "max_attempts": int,
            "memory_id": "..." | null,        # populated when status=done
            "result_status": "stored|updated|superseded|skipped" | null,
            "error_message": "..." | null,    # populated when status=failed
            "enqueued_at": "...",
            "started_at": "..." | null,
            "completed_at": "..." | null,
            "next_attempt_at": "...",         # only meaningful for pending retries
        }
    """
    try:
        _validate_uuid(queue_id)
    except ValueError:
        return {"error": "Invalid queue_id format"}

    from nobrainr.db import write_queue

    status = await write_queue.get_queue_status(queue_id)
    if status is None:
        return {"error": "queue_id not found", "queue_id": queue_id}
    return status


# ──────────────────────────────────────────────
# Auto-routing query planner (Phase B G2, v6.7)
# ──────────────────────────────────────────────


def _auto_route_query(query: str) -> dict[str, bool]:
    """Heuristic query router for memory_search auto_route mode.

    Picks the best retrieval strategy for this query shape alone — no LLM
    call, no embedding, no async. Runs in <1ms. Zero added latency.

    Inspired by Cognee's auto-routing query planner (which uses an LLM for
    the same decision). We pay the LLM cost later if quality demands it;
    for now the heuristic covers the four shapes that actually benefit
    from different strategies.

    Rules (first match wins):
      1. Long or multi-clause query (>= 12 words OR 2+ commas OR 2+ " and "):
         hybrid RRF + decompose (break into sub-queries for thorough recall)
      2. Why/how/when question with >= 5 words:
         hybrid RRF + HyDE (hypothetical answer embedding helps semantic
         match for conceptual questions that don't share vocabulary with
         the stored memory)
      3. Short query (<= 3 words):
         pure vector + expand (short queries lose precision in FTS;
         expand generates variants to compensate)
      4. Default: hybrid RRF (the existing default)

    Returns a dict of flag overrides {hybrid, expand, hyde, decompose}.
    The caller merges these into the memory_search param set BEFORE the
    expansion/decompose/hyde code blocks run, so the selected flags take
    effect.
    """
    q = (query or "").strip().lower()
    if not q:
        return {"hybrid": True}

    words = q.split()
    word_count = len(words)

    # Rule 1 — long or multi-clause → decompose into sub-queries.
    # Thresholds retuned 2026-07-22: at >=12 words, 80% of live traffic
    # routed LLM-enhanced (agents write sentence-length queries) and live
    # p95 hit 7.1s vs 2.5s on the no-llm path. Decompose is for genuinely
    # multi-clause research queries, not every full-sentence search.
    if word_count >= 24 or q.count(",") >= 3 or q.count(" and ") >= 3:
        return {"hybrid": True, "decompose": True}

    # Rule 2 — why/how/when questions, now >= 8 words (same retune: short
    # conceptual questions do fine on hybrid+rerank without the HyDE tax)
    question_prefixes = (
        "why ", "how ",
        "what if ", "when did ", "when do ", "when was ",
    )
    if any(q.startswith(p) for p in question_prefixes) and word_count >= 8:
        return {"hybrid": True, "hyde": True}

    # Rule 3 — short query → pure vector + expand (fuzzy variants)
    if word_count <= 3:
        return {"hybrid": False, "expand": True}

    # Rule 4 — default: hybrid RRF
    return {"hybrid": True}


def _extract_temporal_bounds(query: str) -> tuple[str | None, str | None]:
    """Extract date_from / date_to from natural-language temporal phrases.

    Runs in <1ms (regex only, no LLM). Called before search so agents
    don't need to do date arithmetic manually. Returns ISO-8601 date
    strings or None if no temporal phrase detected.

    Patterns handled: "last N days/weeks/months", "yesterday", "today",
    "this week/month/year", "past N days", "in [Month] [Year]", "since [date]".
    """
    import re
    from datetime import date, timedelta

    q = (query or "").lower()
    today = date.today()

    # "last N days / weeks / months / years"
    m = re.search(r"\b(?:last|past)\s+(\d+)\s+(day|week|month|year)s?\b", q)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = {
            "day": timedelta(days=n),
            "week": timedelta(weeks=n),
            "month": timedelta(days=n * 30),
            "year": timedelta(days=n * 365),
        }[unit]
        return (today - delta).isoformat(), None

    # "yesterday"
    if "yesterday" in q:
        y = today - timedelta(days=1)
        return y.isoformat(), y.isoformat()

    # "today"
    if re.search(r"\btoday\b", q):
        return today.isoformat(), today.isoformat()

    # "this week"
    if re.search(r"\bthis\s+week\b", q):
        monday = today - timedelta(days=today.weekday())
        return monday.isoformat(), None

    # "this month"
    if re.search(r"\bthis\s+month\b", q):
        return today.replace(day=1).isoformat(), None

    # "this year"
    if re.search(r"\bthis\s+year\b", q):
        return today.replace(month=1, day=1).isoformat(), None

    # "in [Month] [Year]" e.g. "in March 2026" or "in march"
    months = {
        "january": 1, "february": 2, "march": 3, "april": 4,
        "may": 5, "june": 6, "july": 7, "august": 8,
        "september": 9, "october": 10, "november": 11, "december": 12,
    }
    m = re.search(r"\bin\s+(" + "|".join(months) + r")(?:\s+(\d{4}))?\b", q)
    if m:
        mon = months[m.group(1)]
        year = int(m.group(2)) if m.group(2) else today.year
        from calendar import monthrange
        last_day = monthrange(year, mon)[1]
        return date(year, mon, 1).isoformat(), date(year, mon, last_day).isoformat()

    # "since [date]" e.g. "since 2026-03-01" or "since april 2026"
    m = re.search(r"\bsince\s+(\d{4}-\d{2}-\d{2})\b", q)
    if m:
        return m.group(1), None

    return None, None


async def _log_auto_negative_outcomes(
    results: list[dict],
    trace_id: str,
    query: str,
) -> None:
    """Write synthetic was_useful=false rows when retrieval looks weak.

    Two triggers, independent — each result can pick up both signals:
      1. low_recall: fewer than auto_negative_low_recall_threshold results
         came back. Every surfaced hit gets one negative since the query
         came up thin overall.
      2. low_rerank: the top-1 rerank score is below
         auto_negative_low_rerank_threshold. Targeted negative against the
         top hit so the feedback loop can down-rank it for this query
         shape. Only fires when the reranker actually ran (rerank_score
         present).

    Fire-and-forget. Never raises — failure here should never block a
    search response. Context is prefixed so the scheduler feedback job
    can tell synthetic rows from human feedback.
    """
    try:
        negatives: list[tuple[str, int, str]] = []  # (memory_id, rank, context)
        low_recall = len(results) < settings.auto_negative_low_recall_threshold
        if low_recall:
            for r in results:
                negatives.append(
                    (r["id"], r.get("search_rank") or 1, "low_recall")
                )

        top = results[0]
        top_rerank = top.get("rerank_score")
        if (
            top_rerank is not None
            and top_rerank < settings.auto_negative_low_rerank_threshold
        ):
            negatives.append((top["id"], 1, "low_rerank"))

        if not negatives:
            return

        # Dedup: if a single memory triggers both reasons, merge reasons
        # into one row so we don't double-count in ratio aggregates.
        merged: dict[tuple[str, int], list[str]] = {}
        for mid, rank, ctx in negatives:
            merged.setdefault((mid, rank), []).append(ctx)

        for (mid, rank), reasons in merged.items():
            context_str = (
                settings.auto_negative_context_prefix + ",".join(reasons)
            )
            await queries.store_memory_outcome(
                mid,
                False,
                context=context_str,
                agent_id="auto-negative",
                query_trace_id=trace_id,
                query_text=query,
                result_rank=rank,
            )
    except Exception:
        logger.exception("auto-negative outcome logging failed")


# ──────────────────────────────────────────────
# Tool: memory_search
# ──────────────────────────────────────────────
@mcp.tool()
async def memory_search(
    query: str,
    limit: int = settings.default_search_limit,
    threshold: float = settings.default_similarity_threshold,
    tags: list[str] | None = None,
    category: str | None = None,
    source_type: str | None = None,
    source_machine: str | None = None,
    hybrid: bool = True,
    expand: bool = False,
    include_cold: bool = False,
    hyde: bool = False,
    decompose: bool = False,
    date_from: str | None = None,
    date_to: str | None = None,
    auto_route: bool = True,
    include_related: bool = False,
    trust_floor: float | None = None,
) -> list[dict]:
    """Semantic search across all memories, ranked by relevance (similarity + recency + importance).

    Uses hybrid search by default: combines vector similarity with full-text
    keyword matching via Reciprocal Rank Fusion (RRF) for best recall.
    Set hybrid=False for pure vector search only.

    Args:
        query: Natural language search query (e.g. "How did we fix the Docker networking issue?").
        limit: Max results to return (default 10).
        threshold: Minimum similarity score 0.0-1.0 (default 0.3).
        tags: Filter to memories with any of these tags.
        category: Filter to specific category.
        source_type: Filter by source ("chatgpt", "claude", "manual", "agent").
        source_machine: Filter to specific host.
        hybrid: Combine vector + full-text search via RRF (default True).
        expand: Generate variant queries via LLM for broader recall (default False). Adds ~500ms latency.
        include_cold: Include tier-3 (cold/archived) memories in search (default False).
        hyde: Use HyDE (Hypothetical Document Embedding) — generates a hypothetical answer
              and searches with its embedding for better semantic matching. Adds ~1s latency.
        decompose: Break complex queries into sub-queries for more thorough recall. Adds ~1s latency.
        date_from: ISO 8601 lower bound on created_at (inclusive). Accepts full
            timestamps ("2026-03-01T00:00:00Z") or just dates ("2026-03-01").
            Use this for temporal queries like "what did we discuss last week" —
            calculate the absolute date client-side and pass it here.
        date_to: ISO 8601 upper bound on created_at (inclusive). Same format as
            date_from. Combine with date_from for a date range.
        auto_route: When True (default since 2026-07-05), analyze the query
            shape and automatically pick the best retrieval strategy
            (hybrid / hyde / decompose / expand) — agents don't have to
            choose. Uses a lightweight heuristic based on query length,
            comma/and count, and question prefix. Zero added latency.
            Routing only applies when the caller left all strategy flags
            at their defaults — an explicitly-set expand/hyde/decompose
            (or hybrid=False) always wins over the router.
        trust_floor: When set (0-1), drop results whose trust_score is below
            it. Use for high-stakes work that must not act on unverified or
            low-trust (potentially poisoned) memory — returns fewer results
            by design. Unscored memories pass (NULL != low).
    """
    import asyncio
    from datetime import datetime
    from time import monotonic

    limit = max(1, min(limit, 100))
    threshold = max(0.0, min(threshold, 1.0))

    # Budget tracking (2026-04-19): each expensive stage checks elapsed
    # against search_hard_timeout_s before running. Stages that don't
    # fit drop out of the pipeline and the result is tagged with
    # `quality_tier` so the caller knows which stages ran.
    _t0 = monotonic()
    _budget_s = settings.search_hard_timeout_s
    quality_tier = "A"  # A=full, B=no-related, C=no-rerank, D=vec-only, E=timeout

    def _elapsed() -> float:
        return monotonic() - _t0

    def _over(frac: float) -> bool:
        return _elapsed() > _budget_s * frac

    # Auto-routing query planner — Phase B G2 (v6.7), default-on since
    # 2026-07-05. Pick the best retrieval strategy for this query shape,
    # but only when the caller left every strategy flag at its default —
    # an explicit expand/hyde/decompose or hybrid=False is a deliberate
    # choice and always wins over the router.
    _caller_chose_strategy = expand or hyde or decompose or not hybrid
    if auto_route and not _caller_chose_strategy:
        routing = _auto_route_query(query)
        hybrid = routing.get("hybrid", True)
        expand = routing.get("expand", False)
        hyde = routing.get("hyde", False)
        decompose = routing.get("decompose", False)

    # Parse temporal filters. Accept ISO date or datetime; reject garbage so
    # the SQL layer never sees a bogus string. Invalid input falls through to
    # "no filter" rather than erroring out the whole search.
    def _parse_iso(raw: str | None) -> "datetime | None":
        if not raw:
            return None
        try:
            # Handle both "2026-03-01" and "2026-03-01T12:34:56Z" / "+00:00"
            s = raw.strip()
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            if len(s) == 10:  # plain date
                return datetime.fromisoformat(s)
            return datetime.fromisoformat(s)
        except (ValueError, AttributeError):
            return None

    # Auto-detect temporal phrases if agent didn't supply explicit bounds.
    # e.g. "what did we do to nobrainr last week" → date_from = 7 days ago
    if not date_from and not date_to:
        auto_from, auto_to = _extract_temporal_bounds(query)
        if auto_from:
            date_from = auto_from
        if auto_to:
            date_to = auto_to

    parsed_from = _parse_iso(date_from)
    parsed_to = _parse_iso(date_to)

    # Multi-query expansion: generate variants and search each, then RRF-fuse
    all_queries = [query]
    if expand:
        from nobrainr.services.query_expansion import expand_query
        variants = await expand_query(query)
        all_queries.extend(variants)

    # Query decomposition: break complex queries into sub-queries
    if decompose:
        from nobrainr.services.search_enhancements import decompose_query
        try:
            sub_queries = await decompose_query(query)
            all_queries.extend(sub_queries)
        except Exception:
            pass  # Fall back to original query

    # Embed all queries (batch for efficiency)
    from nobrainr.embeddings.ollama import embed_batch
    embeddings = await embed_batch(all_queries)

    # HyDE: generate a hypothetical answer and add its embedding
    if hyde:
        from nobrainr.services.search_enhancements import generate_hyde_document
        try:
            hyde_doc = await generate_hyde_document(query)
            if hyde_doc:
                hyde_embeddings = await embed_batch([hyde_doc])
                embeddings.extend(hyde_embeddings)
                all_queries.append(hyde_doc)
        except Exception:
            pass  # Fall back to original embeddings

    # Anthropic Contextual Retrieval recipe: retrieve top-150 → rerank to top-20
    # for the best recall/precision trade-off (≈67% failure-rate reduction).
    # The cross-encoder is the only thing that can recover relevant items
    # buried at rank 50-150 in the vector pass, so we feed it everything.
    # Cap at 200 so a single oversized search can't OOM the executor.
    if settings.reranker_enabled or len(all_queries) > 1:
        fetch_limit = min(max(limit * 15, 60), 200)
    else:
        fetch_limit = max(limit * 5, 30)

    # Search with each query embedding
    search_coros = [
        queries.search_memories(
            embedding=emb,
            limit=fetch_limit,
            threshold=threshold,
            tags=tags,
            category=category,
            source_type=source_type,
            source_machine=source_machine,
            text_query=q if hybrid else None,
            include_cold=include_cold,
            date_from=parsed_from,
            date_to=parsed_to,
        )
        for q, emb in zip(all_queries, embeddings)
    ]
    all_results = await asyncio.gather(*search_coros)

    # Fuse results from all queries via RRF
    if len(all_results) > 1:
        rrf_k = 60
        rrf_scores: dict[str, float] = {}
        rows_by_id: dict[str, dict] = {}

        for result_set in all_results:
            for rank, row in enumerate(result_set, start=1):
                rid = row["id"]
                rrf_scores[rid] = rrf_scores.get(rid, 0.0) + 1.0 / (rrf_k + rank)
                if rid not in rows_by_id:
                    rows_by_id[rid] = row

        sorted_ids = sorted(rrf_scores, key=rrf_scores.get, reverse=True)
        results = []
        for rid in sorted_ids:
            row = rows_by_id[rid]
            row["rrf_score"] = rrf_scores[rid]
            results.append(row)
    else:
        results = all_results[0]

    # Skip-when-dominant (2026-04-19): if the top-1 RRF score is
    # strongly dominant over top-2 (ratio > threshold), the reranker
    # can't meaningfully reorder — top-1 is going to stay top-1. Skip
    # it and save the CPU for harder queries. Quality tier stays "A"
    # because the ranking IS the confident one.
    _rerank_skip_dominant = False
    if (
        settings.rerank_skip_when_dominant
        and len(results) >= 2
        and results[0].get("rrf_score") is not None
        and results[1].get("rrf_score") is not None
    ):
        r0 = float(results[0]["rrf_score"])
        r1 = float(results[1]["rrf_score"]) or 1e-9
        if r0 / r1 >= settings.rerank_skip_dominance_ratio:
            _rerank_skip_dominant = True

    # Rerank with cross-encoder if enabled. Skip if we've already burned
    # most of the budget on earlier stages — returning an RRF-sorted
    # result at tier C is strictly better than a timeout at tier E.
    if (
        settings.reranker_enabled
        and len(results) > 1
        and not _over(settings.search_rerank_budget_frac)
        and not _rerank_skip_dominant
    ):
        # Reranker gets the remaining budget as its hard cap. If torch /
        # the thread pool is contended (UMAP pre-warm, parallel inference)
        # we fall through to RRF order rather than block the caller.
        remaining = max(0.5, _budget_s - _elapsed())
        try:
            from nobrainr.services.reranker import rerank
            results = await asyncio.wait_for(
                rerank(query, results, limit=limit),
                timeout=remaining,
            )
        except asyncio.TimeoutError:
            import logging
            logging.getLogger("nobrainr").warning(
                "Reranker exceeded remaining budget %.1fs — tier C fallback",
                remaining,
            )
            results = results[:limit]
            quality_tier = "C"
        except Exception:
            import logging
            logging.getLogger("nobrainr").exception("Reranker failed, using original ranking")
            results = results[:limit]
            quality_tier = "C"
    else:
        if settings.reranker_enabled and len(results) > 1:
            quality_tier = "C"  # budget exhausted before rerank
        results = results[:limit]

    # Expand chunk context: fetch adjacent chunks for continuity
    if settings.chunk_context_window > 0 and not _over(0.9):
        results = await queries.expand_chunk_context(results, window=settings.chunk_context_window)

    # Record interest signal for the search query
    if settings.interest_tracking_enabled and query and len(query) > 5:
        try:
            await queries.record_interest_signal(
                topic=query[:200],
                signal_type="search",
                strength=1.0,
                source_machine=source_machine,
            )
        except Exception:
            pass

    # Feedback trace (v6, 2026-04-11): tag every result with a shared
    # trace_id + 1-indexed rank so the caller can close the loop via
    # memory_feedback(query_trace_id=..., result_rank=...). Without this,
    # feedback carries no signal about WHERE the memory was surfaced —
    # a "useful" hit at rank 1 and rank 50 look identical in the DB.
    trace_id = str(uuid4())
    for rank, row in enumerate(results, start=1):
        row["search_trace_id"] = trace_id
        row["search_rank"] = rank
        row["search_query"] = query

    # Auto-negative outcome signal (2026-04-18). Before this, memory_outcomes
    # had 94K rows all was_useful=true — zero variance, so the scheduler
    # feedback loop (integrate_feedback_scores) never adjusted anything.
    # Log negatives when retrieval is thin so the ranker has something to
    # learn from. Fire-and-forget — never block the search response.
    if settings.auto_negative_outcomes_enabled and results:
        asyncio.create_task(
            _log_auto_negative_outcomes(results, trace_id, query)
        )

    # Related-memories expansion (Phase Q, v6.16) — Graphiti-style graph
    # expansion on retrieval. Attach a ``related_memories`` field to each
    # result listing top-3 memories that share entities with it. One
    # batched SQL query with a window function, so this is O(1) extra
    # round-trips regardless of limit. Opt-in because not every caller
    # wants the extra payload. Budget-gated: dropped at tier B if we
    # ran tight on time.
    if include_related and results and _over(0.9):
        quality_tier = "B"
        include_related = False
    if include_related and results:
        try:
            related_map = await queries.get_related_memories_batch(
                [r["id"] for r in results],
                limit_per_memory=3,
            )
            for row in results:
                row["related_memories"] = related_map.get(row["id"], [])
        except Exception:
            import logging
            logging.getLogger("nobrainr").exception(
                "Related-memories expansion failed, returning results without it"
            )
            for row in results:
                row["related_memories"] = []

    # Trust floor (C2 ASI06, 2026-07-14). When the caller sets trust_floor,
    # drop results below it — a post-filter, so a high-stakes consumer that
    # asks for trust_floor=0.6 can never be handed a low-trust or unverified
    # (poisoned-candidate) memory. Applied here rather than in SQL because
    # trust_score is computed/joined per row and this keeps the 3 search
    # paths (vec / hybrid / graph) untouched. Returns fewer than limit by
    # design — "trusted only" is the contract.
    if trust_floor is not None:
        results = [r for r in results
                   if (r.get("trust_score") is None
                       or r["trust_score"] >= trust_floor)]

    # Stamp quality tier + elapsed ms on every row so callers can reason
    # about degradation. Cheap — tiny string + int per result.
    elapsed_ms = int(_elapsed() * 1000)
    for row in results:
        row["quality_tier"] = quality_tier
        row["search_elapsed_ms"] = elapsed_ms

    # Record for the /api/health/detailed endpoint so operators can see
    # p95/p99 search latency over the last minute.
    try:
        from nobrainr.services.metrics import record_search_latency
        record_search_latency(elapsed_ms)
    except Exception:
        pass

    # Persist the trace (2026-07-05). Real queries are the golden-set
    # mining source (memory_outcomes captured query text on only 2 of
    # 101k rows) and the empty-query observability signal. Fire-and-forget.
    asyncio.create_task(
        _persist_search_trace(
            trace_id=trace_id,
            query=query,
            results=results,
            quality_tier=quality_tier,
            elapsed_ms=elapsed_ms,
            strategy={
                "hybrid": hybrid, "expand": expand, "hyde": hyde,
                "decompose": decompose, "auto_route": auto_route,
                "limit": limit,
            },
        )
    )

    return results


async def _persist_search_trace(
    *, trace_id: str, query: str, results: list[dict],
    quality_tier: str, elapsed_ms: int, strategy: dict,
) -> None:
    """Best-effort INSERT of one search trace row. Never raises."""
    try:
        import json as _json
        from uuid import UUID as _UUID
        from nobrainr.db.pool import get_pool
        pool = await get_pool()
        top = [r["id"] for r in results[:10]]
        top_score = float(results[0].get("relevance") or results[0].get("similarity") or 0.0) if results else None
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO search_traces
                    (trace_id, query, result_count, top_ids, top_score,
                     quality_tier, elapsed_ms, strategy)
                VALUES ($1, $2, $3, $4::uuid[], $5, $6, $7, $8::jsonb)
                ON CONFLICT (trace_id) DO NOTHING
                """,
                _UUID(trace_id), query[:1000], len(results),
                [_UUID(str(t)) for t in top], top_score,
                quality_tier, elapsed_ms, _json.dumps(strategy),
            )
    except Exception:
        import logging
        logging.getLogger("nobrainr").debug("search trace persist failed", exc_info=True)


# ──────────────────────────────────────────────
# Tool: deep_recall (2026-07-06)
# ──────────────────────────────────────────────
# Bounded multi-hop recall loop: search → LLM reads the hits and emits
# ONE follow-up query naming the missing bridge → search again → rerank
# the union against the original question. Built after the
# include_related A/B came back NEGATIVE (entity-shared graph neighbors
# are too noisy a join at relation F1 0.03): this loop reads memory
# CONTENT to find the bridge instead of trusting graph edges — the
# Letta finding that tool-surface beats retrieval internals, applied.
# Deliberate tool for multi-hop questions; expected latency 10-30s.

_DEEP_RECALL_SCHEMA = {
    "type": "object",
    "properties": {
        "complete": {
            "type": "boolean",
            "description": "True if the retrieved notes fully answer the question.",
        },
        "followup_query": {
            "type": "string",
            "description": (
                "If not complete: ONE different search query targeting the "
                "missing piece. Name the concrete entity/system/aspect the "
                "notes point to but don't cover. Empty string if complete."
            ),
        },
        "reason": {"type": "string"},
    },
    "required": ["complete", "followup_query"],
}


@mcp.tool()
async def deep_recall(
    query: str,
    limit: int = 10,
    max_hops: int | None = None,
    min_hops: int = 1,
) -> dict:
    """Multi-hop memory recall for questions whose answer spans SEVERAL memories.

    Runs a bounded search→read→refine→search loop: after the first search
    an LLM reads the hits and, if the question isn't fully covered, emits
    one follow-up query naming the missing bridge (entity, system, aspect);
    the union of all hops is cross-encoder-reranked against the original
    question. Use memory_search for direct lookups (fast); use this when
    the question connects two things ("how does X relate to Y", "what did
    the fix for X mean for Y") — expected latency 10-30s.

    Args:
        query: The multi-hop question.
        limit: Max memories returned after final rerank (default 10).
        max_hops: Search rounds cap (default from config, 2).
        min_hops: Force at least this many search rounds — the
            completeness judge is lenient (fired hop 2 on only 26/80
            multihop goldens), so pass min_hops=2 when you KNOW the
            question spans multiple memories.

    Returns:
        {"memories": [...], "hop_queries": [...], "complete": bool}
        — memories in reranked order, each row shaped like memory_search
        output plus "recall_hop" (which round found it).
    """
    import asyncio as _aio

    from nobrainr.extraction.llm import ollama_chat
    from nobrainr.services.reranker import rerank as _rerank

    hops = max_hops or settings.deep_recall_max_hops
    per_hop = settings.deep_recall_per_hop_limit
    fn = memory_search.fn if hasattr(memory_search, "fn") else memory_search

    seen: dict[str, dict] = {}
    hop_queries: list[str] = [query]
    complete = False

    for hop in range(max(1, hops)):
        try:
            # auto_route=False: hop searches must be plain hybrid+rerank —
            # HyDE/decompose inside the loop would add a contended-GPU LLM
            # call per hop on top of the follow-up call (145 HyDE timeouts
            # in the 2026-07-06 A/B run under load).
            res = await fn(query=hop_queries[-1], limit=per_hop, auto_route=False)
        except Exception:
            logging.getLogger("nobrainr").exception("deep_recall hop %d search failed", hop)
            break
        for r in res:
            rid = str(r["id"])
            if rid not in seen:
                r["recall_hop"] = hop
                seen[rid] = r

        if hop >= hops - 1 or not seen:
            break

        # Read the accumulated notes; decide whether a bridge is missing.
        notes = "\n".join(
            f"[{i}] {(r.get('summary') or r.get('content') or '')[:280]}"
            for i, r in enumerate(list(seen.values())[:12])
        )
        _force = hop + 1 < min_hops
        try:
            verdict = await ollama_chat(
                system=(
                    "You check whether retrieved memory notes fully answer a "
                    "question. If something is missing, emit ONE follow-up "
                    "search query for exactly the missing piece: for "
                    "multi-part questions, the uncovered part (its own "
                    "keywords, not the whole question); for bridge "
                    "questions, the entity/system the notes point to but "
                    "don't explain — use concrete names from the notes. "
                    "Never repeat the original query."
                    + (
                        " The caller requires another search round: treat the "
                        "notes as incomplete and always emit a follow-up query."
                        if _force else ""
                    )
                ),
                user=f"Question: {query}\n\nRetrieved notes:\n{notes}",
                schema=_DEEP_RECALL_SCHEMA,
                temperature=0.2,
                model=settings.deep_recall_followup_model,
                timeout=settings.deep_recall_followup_timeout_s,
                caller_kind="live",
                think=False,
            )
        except Exception:
            # GPU contended — return what hop 0 found rather than block.
            break
        if verdict.get("complete") and not _force:
            complete = True
            break
        follow = (verdict.get("followup_query") or "").strip()
        if not follow or follow.lower() == query.lower():
            break
        hop_queries.append(follow)

    union = list(seen.values())
    if len(union) > limit:
        # Global cross-encoder rerank against the ORIGINAL question.
        # A/B'd against per-hop slot allocation (2026-07-06): slots
        # LOST (0.356 vs 0.406) — hop-1 noise displaced good hop-0
        # hits. The reranker keeps the union honest.
        try:
            union = await _aio.wait_for(
                _rerank(query, union, limit=limit),
                timeout=settings.search_hard_timeout_s,
            )
        except Exception:
            union = union[:limit]

    return {
        "memories": union[:limit],
        "hop_queries": hop_queries,
        "complete": complete,
    }


# ──────────────────────────────────────────────
# Tool: library_search (document layer, 2026-07-27)
# ──────────────────────────────────────────────
LIBRARY_ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "citations": {"type": "array", "items": {"type": "integer"}},
    },
    "required": ["answer", "citations"],
}

_LIBRARY_ANSWER_SYSTEM = (
    "Answer the question ONLY from the numbered document excerpts. Cite "
    "every claim with the excerpt numbers you used (citations array). If "
    "the excerpts don't contain the answer, say so plainly — never fill "
    "gaps from general knowledge; these are the user's own documents and "
    "fidelity beats fluency."
)


@mcp.tool()
async def library_search(
    query: str,
    document: str | None = None,
    limit: int = 8,
    synthesize: bool = False,
) -> dict:
    """Search the personal document library (study documents, Affine notes,
    markdown) — chunked, hybrid-searched, cross-encoder reranked.

    Use for questions about the user's OWN documents ("what do my geodesy
    notes say about...", "find the section on X in <file>"). Pass
    document=<file ref from results> to scope to ONE document.
    synthesize=True adds a cited answer composed strictly from the
    excerpts (one LLM call, ~5-15s; leave False for fast raw hits).

    Returns: {"hits": [...], "documents": [refs], "answer"?, "citations"?}
    """
    import json as _json

    from nobrainr.extraction.llm import ollama_chat
    from nobrainr.services.reranker import rerank as _rerank

    limit = max(1, min(limit, 20))
    emb = await embed_text(query)
    hits = await queries.search_memories(
        embedding=emb, limit=limit * 4, threshold=0.2, text_query=query,
        source_type=list(settings.library_source_types),
    )

    def _fp(h: dict):
        m = h.get("metadata")
        if isinstance(m, str):
            try:
                m = _json.loads(m)
            except Exception:
                m = {}
        return (m or {}).get("file_path") or h.get("source_ref")

    scoped = [h for h in hits
              if h.get("source_type") in settings.library_source_types
              and (not document or _fp(h) == document)]
    try:
        scoped = await _rerank(query, scoped, limit=limit)
    except Exception:
        pass
    scoped = scoped[:limit]

    out: dict = {
        "hits": [
            {"id": str(h["id"]), "document": _fp(h),
             "content": (h.get("content") or "")[:1200],
             "summary": h.get("summary")}
            for h in scoped
        ],
        "documents": sorted({_fp(h) for h in scoped if _fp(h)}),
    }

    if synthesize and scoped:
        excerpts = "\n\n".join(
            f"[{i}] ({_fp(h)}):\n{(h.get('content') or '')[:900]}"
            for i, h in enumerate(scoped)
        )
        try:
            resp = await ollama_chat(
                system=_LIBRARY_ANSWER_SYSTEM,
                user=f"Question: {query}\n\nExcerpts:\n{excerpts}",
                schema=LIBRARY_ANSWER_SCHEMA,
                temperature=0.2,
                caller_kind="live",
                think=False,
            )
            out["answer"] = (resp or {}).get("answer", "")
            out["citations"] = [
                i for i in (resp or {}).get("citations", [])
                if isinstance(i, int) and 0 <= i < len(scoped)
            ]
        except Exception:
            out["answer_error"] = "synthesis unavailable (GPU busy) — use hits"
    return out


# ──────────────────────────────────────────────
# Tool: evidence_gather (LME-V2 AgentRunbook-C, 2026-07-22)
# ──────────────────────────────────────────────
# LongMemEval-V2 finding: agentic evidence-gathering (72.5%) beats the
# best pure-RAG memory (48.5%) by 24 points. This is our bounded version:
# a small LLM drives search / read-by-id / READ-ONLY SQL steps over the
# memory substrate and returns a compact evidence set. Unlike deep_recall
# (which can only re-query), the gatherer can COUNT, ORDER BY date, join
# entities, and inspect full memory bodies — the operations multihop
# questions actually need.

_ALLOWED_SQL = _re.compile(r"^\s*select\b", _re.IGNORECASE)
_FORBIDDEN_SQL = _re.compile(
    r"\b(insert|update|delete|drop|alter|create|grant|truncate|copy|vacuum|call|do)\b|;",
    _re.IGNORECASE,
)


def _guard_sql(sql: str, row_cap: int) -> str | None:
    """Read-only SQL guard: single SELECT, no DML/DDL keywords, no
    statement chaining; wrapped with a hard row cap. Defense in depth —
    the executing transaction is ALSO read-only with a statement timeout."""
    s = (sql or "").strip()
    if not s or not _ALLOWED_SQL.match(s) or _FORBIDDEN_SQL.search(s):
        return None
    return f"SELECT * FROM ({s}) _eg LIMIT {int(row_cap)}"


EVIDENCE_STEP_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["search", "sql", "read", "done"]},
        "query": {"type": "string"},
        "sql": {"type": "string"},
        "ids": {"type": "array", "items": {"type": "string"}},
        "notes": {"type": "string"},
    },
    "required": ["action"],
}

_EVIDENCE_SYSTEM = (
    "You gather evidence from a PostgreSQL memory database to answer a "
    "question. Each turn pick ONE action:\n"
    "- search: semantic+keyword search (query = short phrase)\n"
    "- sql: ONE read-only SELECT. Tables: memories(id, summary, content, "
    "created_at, updated_at, claim_kind, trust_score, source_type, tags), "
    "context_cards(subject_key, title, body, published_accuracy), "
    "entities(id, canonical_name), entity_memories(entity_id, memory_id). "
    "Use SQL for counting, date ordering, joins — things search can't do.\n"
    "- read: fetch full content of memory ids you already saw\n"
    "- done: finish; put the memory ids that ANSWER the question in ids "
    "and a one-line synthesis in notes.\n"
    "Be economical: done as soon as the evidence suffices."
)


@mcp.tool()
async def evidence_gather(
    question: str,
    max_steps: int | None = None,
    limit: int = 10,
) -> dict:
    """Agentic evidence gathering for questions plain search can't answer.

    A bounded loop where a small LLM drives search, read-only SQL
    (counts, date ordering, entity joins), and full-memory reads over the
    knowledge base, then returns the evidence set. Use for multi-hop or
    aggregate questions ("how many X since May", "which came first",
    "what connects X and Y"); use memory_search for direct lookups and
    deep_recall for pure bridge questions. Expected latency 20-60s.

    Args:
        question: The question to gather evidence for.
        max_steps: Loop cap (default from config, 5).
        limit: Max evidence memories returned (default 10).

    Returns:
        {"evidence": [...], "steps": [{action, detail}], "notes": str}
    """
    from nobrainr.db.pool import get_pool
    from nobrainr.extraction.llm import ollama_chat
    from nobrainr.services.reranker import rerank as _rerank

    steps_cap = min(max_steps or settings.evidence_gather_max_steps, 8)
    fn = memory_search.fn if hasattr(memory_search, "fn") else memory_search
    pool = await get_pool()

    seen: dict[str, dict] = {}
    step_log: list[dict] = []
    observation = "no observations yet"
    notes = ""

    for _step in range(steps_cap):
        try:
            decision = await ollama_chat(
                system=_EVIDENCE_SYSTEM,
                user=(
                    f"Question: {question}\n\nEvidence so far "
                    f"({len(seen)} memories):\n"
                    + "\n".join(
                        f"- {rid[:8]}: {(r.get('summary') or '')[:90]}"
                        for rid, r in list(seen.items())[:10]
                    )
                    + f"\n\nLast result:\n{observation[:700]}"
                ),
                schema=EVIDENCE_STEP_SCHEMA,
                temperature=0.2,
                model=settings.evidence_gather_model,
                timeout=45,
                caller_kind="live",
                think=False,
            )
        except Exception:
            logger.exception("evidence_gather step LLM failed")
            break

        action = (decision or {}).get("action", "done")
        if action == "done":
            notes = (decision.get("notes") or "")[:400]
            picked = [str(i) for i in (decision.get("ids") or [])]
            if picked:
                # normalize short prefixes back to full ids
                full = {rid[:8]: rid for rid in seen}
                ordered = [full.get(p[:8], p) for p in picked]
                seen = {rid: seen[rid] for rid in ordered if rid in seen} or seen
            step_log.append({"action": "done", "detail": notes[:120]})
            break

        if action == "search":
            q = (decision.get("query") or question)[:200]
            step_log.append({"action": "search", "detail": q[:120]})
            try:
                res = await fn(query=q, limit=8, auto_route=False)
            except Exception:
                observation = "search failed"
                continue
            for r in res:
                seen.setdefault(str(r["id"]), r)
            observation = "search hits:\n" + "\n".join(
                f"- {str(r['id'])[:8]}: {(r.get('summary') or '')[:90]}" for r in res[:8]
            )

        elif action == "sql":
            guarded = _guard_sql(decision.get("sql") or "",
                                 settings.evidence_gather_sql_row_cap)
            step_log.append({"action": "sql",
                             "detail": (decision.get("sql") or "")[:120]})
            if not guarded:
                observation = "SQL rejected: only a single read-only SELECT is allowed"
                continue
            try:
                async with pool.acquire() as conn:
                    async with conn.transaction(readonly=True):
                        await conn.execute(
                            f"SET LOCAL statement_timeout = "
                            f"{settings.evidence_gather_sql_timeout_ms}")
                        rows = await conn.fetch(guarded)
                observation = "sql rows:\n" + "\n".join(
                    str(dict(r))[:200] for r in rows[:15]
                ) if rows else "sql returned 0 rows"
            except Exception as e:
                observation = f"sql error: {str(e)[:150]}"

        elif action == "read":
            ids = [str(i) for i in (decision.get("ids") or [])][:5]
            full = {rid[:8]: rid for rid in seen}
            ids = [full.get(i[:8], i) for i in ids]
            step_log.append({"action": "read", "detail": ",".join(i[:8] for i in ids)})
            bodies = []
            for mid in ids:
                try:
                    m = await queries.get_memory(mid)
                except Exception:
                    m = None
                if m:
                    seen[str(m["id"])] = {**seen.get(str(m["id"]), {}), **dict(m)}
                    bodies.append(f"[{mid[:8]}] {(m.get('content') or '')[:400]}")
            observation = "full contents:\n" + "\n".join(bodies) if bodies else "no memories found for ids"

        else:
            observation = f"unknown action {action!r}"

    evidence = list(seen.values())
    if len(evidence) > limit:
        import asyncio as _aio

        try:
            evidence = await _aio.wait_for(
                _rerank(question, evidence, limit=limit),
                timeout=settings.search_hard_timeout_s,
            )
        except Exception:
            evidence = evidence[:limit]

    return {"evidence": evidence[:limit], "steps": step_log, "notes": notes}


# ──────────────────────────────────────────────
# Tool: memory_aggregate (Phase L, v6.13, 2026-04-12)
# ──────────────────────────────────────────────
# Supermemory-inspired Aggregation pattern. Instead of re-ranking N
# candidates and returning the top K verbatim, call the LLM once to
# SYNTHESIZE the N candidates into K self-contained answer slots.
# Different from the v6 reranker (which reorders) and the cross-encoder
# (which picks top-K verbatim). Aggregation actively COMBINES evidence
# across memories into new compact answers — particularly useful for
# multi-hop questions where the answer is scattered across several
# memories.
#
# Cost: one LLM call per query (~2s on llama-server with N_PARALLEL=3
# from Phase A). Agents should call this when they need a synthesized
# answer, not when they need raw memory retrieval.

_AGGREGATE_SCHEMA = {
    "type": "object",
    "properties": {
        "slots": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "answer": {
                        "type": "string",
                        "description": "Self-contained 2-5 sentence answer synthesized from the candidate memories. Must stand alone without requiring the original query.",
                    },
                    "source_memory_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "UUIDs (pulled from the [n] labels) of the candidate memories used to synthesize this answer.",
                    },
                    "confidence": {
                        "type": "number",
                        "description": "0.0-1.0 — how well-supported by the source memories. 0.3 = weak inference, 0.7 = solid, 0.9 = directly stated.",
                    },
                },
                "required": ["answer", "source_memory_ids", "confidence"],
            },
            "description": "Up to K synthesized answer slots, ordered best-first.",
        },
    },
    "required": ["slots"],
}

_AGGREGATE_SYSTEM_PROMPT = (
    "You are an evidence synthesizer. Given a query and a list of candidate "
    "memories (each labeled [n] with a UUID), produce up to K self-contained "
    "answer slots. Each slot should:\n"
    "- Combine evidence from ONE or MORE memories into a clear answer\n"
    "- Cite the UUIDs of the memories you drew from via source_memory_ids\n"
    "- State the confidence (0.0-1.0) based on how directly the memories "
    "support it\n"
    "- Be written as a standalone fact — someone reading only the slot should "
    "understand it without the query\n"
    "- Prefer fewer, higher-confidence slots over many weak ones\n\n"
    "If the memories don't answer the query at all, return an empty slots "
    "array rather than fabricating content."
)


@mcp.tool()
async def memory_aggregate(
    query: str,
    k: int = 3,
    fetch_limit: int = 15,
    threshold: float = 0.3,
    tags: list[str] | None = None,
    category: str | None = None,
    source_type: str | None = None,
    source_machine: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict:
    """Retrieve-then-synthesize: fetch top-N memories, synthesize K answer slots.

    Supermemory Aggregation pattern — different from the v6 reranker
    (which reorders) and the cross-encoder (which picks top-K verbatim):
    aggregation COMBINES evidence across multiple memories into new
    compact answers. Particularly useful for multi-hop questions where
    the answer is scattered across several memories.

    Costs one LLM call per query (~2s on llama-server with Phase A's
    concurrency=3). Agents should call this when they need a SYNTHESIZED
    answer, not raw retrieval — use ``memory_search`` for that.

    Args:
        query: Natural language question.
        k: Max answer slots to return (default 3, clamped to [1, 10]).
        fetch_limit: Candidate pool size before synthesis (default 15,
            clamped to [k, 50]).
        threshold: Minimum similarity for candidates (default 0.3).
        tags, category, source_type, source_machine: Filter candidates.
        date_from, date_to: ISO 8601 date bounds on candidate creation.

    Returns:
        ``{"query": str, "slots": list of {answer, source_memory_ids,
        confidence}, "candidate_count": int}``. On embed/LLM failure
        the ``slots`` list is empty and an ``error`` key is populated.
    """
    from datetime import datetime

    from nobrainr.embeddings.ollama import embed_text
    from nobrainr.extraction.llm import ollama_chat

    k = max(1, min(k, 10))
    fetch_limit = max(k, min(fetch_limit, 50))
    threshold = max(0.0, min(threshold, 1.0))

    def _parse_iso(raw: str | None):
        if not raw:
            return None
        try:
            s = raw.strip()
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            return datetime.fromisoformat(s)
        except (ValueError, AttributeError):
            return None

    try:
        embedding = await embed_text(query)
    except Exception as exc:
        return {
            "query": query,
            "slots": [],
            "candidate_count": 0,
            "error": f"embed failed: {exc}",
        }

    candidates = await queries.search_memories(
        embedding=embedding,
        limit=fetch_limit,
        threshold=threshold,
        tags=tags,
        category=category,
        source_type=source_type,
        source_machine=source_machine,
        text_query=query,  # hybrid RRF
        date_from=_parse_iso(date_from),
        date_to=_parse_iso(date_to),
    )

    if not candidates:
        return {"query": query, "slots": [], "candidate_count": 0}

    # Format context for the LLM — numbered refs [1], [2], ... with
    # content preview. Per-memory chars bounded so a single giant memory
    # can't blow the context window.
    context_lines = []
    for i, mem in enumerate(candidates, 1):
        content = (mem.get("content") or "")[:700].replace("\n", " ")
        context_lines.append(f"[{i}] ID: {mem['id']}\n    {content}")
    context = "\n\n".join(context_lines)

    user_prompt = (
        f"QUERY: {query}\n\n"
        f"CANDIDATE MEMORIES ({len(candidates)}):\n{context}\n\n"
        f"Produce up to {k} synthesized answer slots."
    )

    try:
        result = await ollama_chat(
            system=_AGGREGATE_SYSTEM_PROMPT,
            user=user_prompt,
            schema=_AGGREGATE_SCHEMA,
            num_ctx=8192,
            think=False,
            caller_kind="live",
        )
    except Exception as exc:
        return {
            "query": query,
            "slots": [],
            "candidate_count": len(candidates),
            "error": f"synthesis failed: {exc}",
        }

    slots = (result or {}).get("slots", []) or []
    return {
        "query": query,
        "slots": slots[:k],
        "candidate_count": len(candidates),
    }


# ──────────────────────────────────────────────
# Tool: memory_query
# ──────────────────────────────────────────────
@mcp.tool()
async def memory_query(
    tags: list[str] | None = None,
    category: str | None = None,
    source_type: str | None = None,
    source_machine: str | None = None,
    text_query: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    """Structured query for memories with filters. No semantic search, just filtering.

    Args:
        tags: Filter to memories with any of these tags.
        category: Filter to specific category.
        source_type: Filter by source ("chatgpt", "claude", "manual", "agent").
        source_machine: Filter to specific host.
        text_query: Full-text search on content.
        limit: Max results (default 50).
        offset: Pagination offset.
    """
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    return await queries.query_memories(
        tags=tags,
        category=category,
        source_type=source_type,
        source_machine=source_machine,
        text_query=text_query,
        limit=limit,
        offset=offset,
    )


# ──────────────────────────────────────────────
# Tool: memory_get
# ──────────────────────────────────────────────
@mcp.tool()
async def memory_get(memory_id: str) -> dict | None:
    """Get a specific memory by its ID.

    Args:
        memory_id: The UUID of the memory to retrieve.
    """
    try:
        _validate_uuid(memory_id)
    except ValueError:
        return {"error": "Invalid memory_id format"}
    return await queries.get_memory(memory_id)


# ──────────────────────────────────────────────
# Tool: memory_update
# ──────────────────────────────────────────────
@mcp.tool()
async def memory_update(
    memory_id: str,
    content: str | None = None,
    summary: str | None = None,
    tags: list[str] | None = None,
    category: str | None = None,
    confidence: float | None = None,
    metadata: dict | None = None,
) -> dict | None:
    """Update an existing memory. Re-embeds if content changes.

    Args:
        memory_id: The UUID of the memory to update.
        content: New content (triggers re-embedding).
        summary: New summary.
        tags: New tags (replaces existing).
        category: New category.
        confidence: New confidence score.
        metadata: Additional metadata to merge.
    """
    try:
        _validate_uuid(memory_id)
    except ValueError:
        return {"error": "Invalid memory_id format"}
    category = normalize_category(category)
    embedding = None
    if content is not None:
        embed_parts = []
        if category:
            embed_parts.append(category)
        if tags:
            embed_parts.append(", ".join(tags))
        if embed_parts:
            embed_input = ". ".join(embed_parts) + ". " + content
        else:
            embed_input = content
        embedding = await embed_text(embed_input)

    # Trigger snapshots old state automatically
    return await queries.update_memory(
        memory_id,
        content=content,
        summary=summary,
        embedding=embedding,
        tags=tags,
        category=category,
        confidence=confidence,
        metadata=metadata,
        _changed_by="mcp",
        _change_type="manual_update",
    )


# ──────────────────────────────────────────────
# Tool: memory_delete
# ──────────────────────────────────────────────
@mcp.tool()
async def memory_delete(memory_id: str) -> dict:
    """Delete a memory by its ID.

    Args:
        memory_id: The UUID of the memory to delete.
    """
    try:
        _validate_uuid(memory_id)
    except ValueError:
        return {"error": "Invalid memory_id format"}
    # Trigger snapshots old state automatically before deletion
    deleted = await queries.delete_memory(
        memory_id,
        _changed_by="mcp",
        _change_type="manual_delete",
    )
    if deleted:
        return {"status": "deleted", "id": memory_id}
    return {"status": "not_found", "id": memory_id}


# ──────────────────────────────────────────────
# Decision memory tools (2026-04-20, ADR-style)
# ──────────────────────────────────────────────
# Distinct from free-form memories. A decision is a CHOICE with rationale
# and rejected alternatives — prescriptive, not descriptive. Pattern from
# DecisionNode, adapted for our shared Postgres KB. See
# nobrainr.models.decisions.DecisionMetadata for the full "why" and schema.


@mcp.tool()
async def decision_store(
    scope: str,
    decision: str,
    rationale: str,
    constraints: list[str] | None = None,
    alternatives_rejected: list[str] | None = None,
    supersedes: list[str] | None = None,
    tags: list[str] | None = None,
    source_machine: str | None = None,
    confidence: float = 1.0,
) -> dict:
    """Store an ADR-style decision. Different from a learning — a decision is a
    forward-looking CHOICE with rationale + rejected alternatives, meant to
    prevent future-you re-arguing it.

    Use when you explicitly pick path A over path B. Use memory_store with
    category='learning' for observations/discoveries.

    Args:
        scope: Dotted subsystem path — 'nobrainr/retrieval', 'bimavo/gaeb-parser'.
        decision: One-sentence imperative. 'Cap BGE reranker at 8 candidates'.
        rationale: Why this path — concrete reasons, not vibes.
        constraints: Hard limits that rule out alternatives. e.g. ['20GB VRAM'].
        alternatives_rejected: Paths considered + short reason each was dropped.
        supersedes: Memory-IDs of prior decisions this replaces. Set their
            status='superseded' separately if you want to retire them from search.
        tags: Extra tags. Always prepends ['decision', <scope-first-segment>].
        source_machine: Defaults to hostname.
        confidence: 0-1 reliability. Default 1.0.

    Returns {status, queue_id, enqueued_at} — same shape as memory_store.
    """
    from nobrainr.models.decisions import DecisionMetadata

    try:
        meta = DecisionMetadata(
            scope=scope,
            decision=decision,
            rationale=rationale,
            constraints=constraints or [],
            alternatives_rejected=alternatives_rejected or [],
            supersedes=supersedes or [],
        )
    except Exception as exc:
        return {"error": f"Invalid decision metadata: {exc}"}

    # Content = human-readable rendering so the full decision is searchable
    # via both embedding + FTS without requiring metadata lookup.
    parts = [
        f"DECISION ({scope}): {decision}",
        f"Rationale: {rationale}",
    ]
    if meta.constraints:
        parts.append("Constraints:\n" + "\n".join(f"- {c}" for c in meta.constraints))
    if meta.alternatives_rejected:
        parts.append("Alternatives rejected:\n" + "\n".join(
            f"- {a}" for a in meta.alternatives_rejected))
    if meta.supersedes:
        parts.append("Supersedes: " + ", ".join(meta.supersedes))
    content = "\n\n".join(parts)

    scope_root = scope.split("/", 1)[0]
    all_tags = sorted(set((tags or []) + ["decision", scope_root]))

    from nobrainr.db import write_queue
    # skip_dedup=True for decisions: they're forward-looking singleton writes
    # meant to be distinct records. Running the LLM dedup/merge classifier
    # just slows the queue without value — and if a new decision happens to
    # look like an old one, SUPERSEDING is the right action (see `supersedes`
    # parameter), not automatic merge.
    enq = await write_queue.enqueue_memory_write(
        content=content,
        summary=f"Decision: {decision}",
        tags=all_tags,
        category="decision",
        source_type="agent",
        source_machine=source_machine,
        source_ref=None,
        confidence=confidence,
        metadata=meta.to_dict(),
        skip_dedup=True,
    )
    return {
        "status": "queued",
        "queue_id": enq["queue_id"],
        "enqueued_at": enq["enqueued_at"],
        "scope": scope,
        "message": (
            "Decision queued. Poll memory_store_status(queue_id) for the "
            "memory_id. Retrieve later with decision_search(scope=...)."
        ),
    }


@mcp.tool()
async def decision_search(
    query: str,
    *,
    scope: str | None = None,
    status: str = "active",
    limit: int = 10,
) -> dict:
    """Search decisions only (category='decision') — filters out observational
    learnings and other free-form memories so agents get only prescriptive
    prior choices. Use this before making an architectural choice, not
    memory_search, to avoid re-debating settled policy.

    Args:
        query: Natural language search. Matches against scope + decision +
            rationale + constraints (all embedded together).
        scope: Optional exact-prefix scope filter. 'nobrainr' matches
            'nobrainr/retrieval' and 'nobrainr/extraction'.
        status: 'active' (default), 'deprecated', 'superseded', or '*' for all.
        limit: Max results. Default 10.
    """
    # Delegate to memory_search with category='decision' then post-filter.
    from nobrainr.embeddings.ollama import embed_text
    embedding = await embed_text(query)
    raw = await queries.search_memories(
        embedding=embedding,
        limit=max(limit * 3, 30),  # overfetch so post-filter on scope/status has room
        text_query=query,
        category="decision",
    )
    out: list[dict] = []
    for m in raw:
        md = m.get("metadata") or {}
        if isinstance(md, str):
            import json as _json
            try:
                md = _json.loads(md)
            except Exception:
                md = {}
        if scope and not str(md.get("scope", "")).startswith(scope):
            continue
        if status != "*" and md.get("status", "active") != status:
            continue
        out.append(m)
        if len(out) >= limit:
            break
    return {"decisions": out, "count": len(out), "scope": scope, "status": status}


# ──────────────────────────────────────────────
# Procedural memory tools (Phase C G4, 2026-04-12, v6.8)
# ──────────────────────────────────────────────
# Letta + LangGraph-inspired: agent-writable rules and instructions that
# affect future behavior. Retrieved by scope (not similarity) and applied
# at session start. Separate from regular memories so rules never compete
# with facts in search results.


def _parse_iso_datetime(raw):
    """Accept '2026-03-01', '2026-03-01T12:00:00Z', or ISO with offset.

    Returns a datetime or None. None on garbage input rather than
    erroring, so MCP callers can't accidentally break procedural
    storage with a malformed date. Type annotation is omitted on the
    return so we don't need to import datetime at module scope.
    """
    from datetime import datetime
    if not raw:
        return None
    try:
        s = raw.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except (ValueError, AttributeError):
        return None


@mcp.tool()
async def memory_store_procedural(
    content: str,
    title: str | None = None,
    scope: str = "global",
    agent_id: str | None = None,
    project_id: str | None = None,
    session_id: str | None = None,
    priority: int = 50,
    tags: list[str] | None = None,
    metadata: dict | None = None,
    expires_at: str | None = None,
) -> dict:
    """Store a procedural memory — an instruction or rule that affects
    future agent behavior.

    Unlike regular memories (which are retrieved by similarity),
    procedural memories are retrieved by SCOPE and applied at session
    start or on demand. Typical use cases:

      - ``scope="global"`` — rule applies to every agent everywhere
        ("always run tests before committing")
      - ``scope="agent"`` — rule applies to one specific agent
        ("this agent prefers terse responses")
      - ``scope="project"`` — rule applies to one project
        ("in project foo, tests live in tests/")
      - ``scope="session"`` — temporary rule that auto-expires with
        the session

    Agents should call ``memory_get_procedural`` at session start to
    discover rules that should govern their behavior, then apply them.

    Args:
        content: The rule/instruction itself.
        title: Short label for quick reference (optional).
        scope: 'global' | 'agent' | 'project' | 'session' (default 'global').
        agent_id: Required when scope='agent'.
        project_id: Required when scope='project'.
        session_id: Required when scope='session'.
        priority: 0-100, higher applies first when multiple rules match.
            Default 50.
        tags: Free-form tags for organization.
        metadata: Structured context (source, reason, etc.).
        expires_at: ISO 8601 timestamp when this rule should auto-deactivate.
            Use for session-scoped or temporary rules. Accepts
            "2026-04-12", "2026-04-12T09:55:00Z", or "2026-04-12T09:55:00+00:00".
    """
    parsed_expires = _parse_iso_datetime(expires_at)
    try:
        return await queries.store_procedural_memory(
            content=content,
            title=title,
            scope=scope,
            agent_id=agent_id,
            project_id=project_id,
            session_id=session_id,
            priority=priority,
            tags=tags,
            metadata=metadata,
            expires_at=parsed_expires,
        )
    except ValueError as exc:
        return {"error": str(exc)}


@mcp.tool()
async def memory_get_procedural(
    scope: str | None = None,
    agent_id: str | None = None,
    project_id: str | None = None,
    session_id: str | None = None,
    include_expired: bool = False,
    include_inactive: bool = False,
    limit: int = 100,
) -> list[dict]:
    """Retrieve active procedural memories (rules/instructions).

    Returns rules in priority order (highest first). Expired and
    inactive rules are filtered out by default.

    Scope merging (when no explicit scope is passed):
      - If ``agent_id`` is passed, returns global + agent-specific rules
        for that agent (the standard "rules that apply to me" query).
      - If ``project_id`` is passed, returns global + project rules.
      - If ``session_id`` is passed, returns global + session rules.
      - Any combination of the above also works (ids are independent).
      - If nothing is passed, returns all active rules across all scopes.

    An explicit ``scope`` parameter overrides this merge — passing
    ``scope="global"`` returns only global rules regardless of which
    ids are passed.

    Call this at session start to discover rules that should govern
    agent behavior.

    Args:
        scope: Optional exact-scope filter. Overrides the id-based merge.
        agent_id: The current agent's id — returns global + agent rules.
        project_id: The current project id — returns global + project rules.
        session_id: The current session id — returns global + session rules.
        include_expired: Include rules past their expires_at (default False).
        include_inactive: Include soft-deleted rules (default False).
        limit: Max rows to return (default 100).
    """
    try:
        return await queries.get_procedural_memories(
            scope=scope,
            agent_id=agent_id,
            project_id=project_id,
            session_id=session_id,
            include_expired=include_expired,
            include_inactive=include_inactive,
            limit=limit,
        )
    except ValueError as exc:
        return [{"error": str(exc)}]


@mcp.tool()
async def memory_delete_procedural(memory_id: str, hard: bool = False) -> dict:
    """Deactivate (soft delete) a procedural memory.

    Default is soft — sets ``active = false`` so the rule leaves an
    audit trail of what was tried. Pass ``hard=True`` to actually remove
    the row (dashboard cleanup).

    Args:
        memory_id: The UUID of the procedural memory to deactivate.
        hard: If True, DELETE the row instead of marking it inactive.
            Default False (soft delete).
    """
    ok = await queries.delete_procedural_memory(memory_id, hard=hard)
    if ok:
        return {"status": "deleted" if hard else "deactivated", "id": memory_id}
    return {"status": "not_found", "id": memory_id}


# ──────────────────────────────────────────────
# Tool: memory_get_user_profile (Phase M, v6.14, 2026-04-12)
# ──────────────────────────────────────────────
# Supermemory-inspired dual-layer user profile — one-shot "everything an
# agent needs to start a session" call. Combines three layers:
#   1. static facts    — high-importance stable memories about the user
#   2. recent activity — memories touched in the last N days
#   3. procedural rules — global + agent-specific rules from Phase C G4
#
# Agents call this once at session start and prepend the result to their
# system prompt, avoiding ad-hoc memory_search calls to reconstruct user
# context on every session. ~50ms end-to-end (two indexed SQL queries +
# one procedural_memories read), no LLM call.
@mcp.tool()
async def memory_get_user_profile(
    source_machine: str | None = None,
    agent_id: str | None = None,
    static_limit: int = 20,
    recent_limit: int = 15,
    rule_limit: int = 30,
    recent_window_days: int = 7,
    static_importance_floor: float = 0.75,
) -> dict:
    """Return the user profile — the one-shot "everything for session start" call.

    Three layers, returned in one dict:
      - ``static_facts``: top-N memories with importance >= floor (default
        0.75), ordered by importance/stability DESC. These are the durable
        "who the user is / what they prefer / what they're working on"
        memories.
      - ``recent_activity``: top-N memories touched (accessed or created)
        in the last ``recent_window_days`` days, ordered newest-first —
        "what they've been doing this week".
      - ``procedural_rules``: global rules + (agent-specific rules when
        ``agent_id`` is passed), ordered by priority DESC. Phase C G4
        agent-writable instructions.

    Scope: all three layers are optionally filtered by ``source_machine``
    so you get "user profile on this machine" rather than cross-host noise.

    Cost: 2 SQL queries for static+recent + 1 SQL query for procedural.
    No LLM call, no embedding. ~50ms end-to-end. Safe to call at every
    session start.

    Args:
        source_machine: Filter memories to this host (default None = all).
        agent_id: Specific agent — gets global + agent-scoped rules.
        static_limit: Max static facts (default 20, clamped 0-100).
        recent_limit: Max recent items (default 15, clamped 0-100).
        rule_limit: Max procedural rules (default 30, clamped 0-200).
        recent_window_days: Window for "recent" (default 7, clamped 1-90).
        static_importance_floor: Min importance for static facts (default
            0.75, clamped 0.0-1.0).

    Returns:
        ``{source_machine, agent_id, static_facts, recent_activity,
        procedural_rules, generated_at, counts}``.
    """
    from datetime import datetime, timezone

    layers = await queries.get_user_profile_layers(
        source_machine=source_machine,
        static_limit=max(0, min(static_limit, 100)),
        recent_limit=max(0, min(recent_limit, 100)),
        recent_window_days=max(1, min(recent_window_days, 90)),
        static_importance_floor=max(0.0, min(static_importance_floor, 1.0)),
    )

    # Procedural layer — fetch failure must NOT block the profile, because
    # a broken procedural table shouldn't poison the whole session-start
    # flow. Fall back to an empty list and log the exception.
    try:
        procedural = await queries.get_procedural_memories(
            agent_id=agent_id,
            limit=max(0, min(rule_limit, 200)),
        )
    except Exception:
        import logging
        logging.getLogger("nobrainr").exception(
            "Procedural fetch failed in memory_get_user_profile (returning empty list)"
        )
        procedural = []

    return {
        "source_machine": source_machine,
        "agent_id": agent_id,
        "static_facts": layers["static"],
        "recent_activity": layers["recent"],
        "procedural_rules": procedural,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "counts": {
            "static_facts": len(layers["static"]),
            "recent_activity": len(layers["recent"]),
            "procedural_rules": len(procedural),
        },
    }


# ──────────────────────────────────────────────
# Tool: memory_history
# ──────────────────────────────────────────────
@mcp.tool()
async def memory_history(memory_id: str) -> list[dict]:
    """Get the full version history of a memory (audit trail / time machine).

    Returns all recorded versions ordered newest-first.
    Each version includes: version number, change_type, content snapshot,
    tags, category, change_reason, and who made the change.

    Args:
        memory_id: The UUID of the memory.
    """
    try:
        _validate_uuid(memory_id)
    except ValueError:
        return [{"error": "Invalid memory_id format"}]
    return await queries.get_memory_history(memory_id)


# ──────────────────────────────────────────────
# Tool: memory_restore
# ──────────────────────────────────────────────
@mcp.tool()
async def memory_restore(memory_id: str, version: int) -> dict:
    """Restore a memory to a previous version from its history.

    This reverts the memory's content, tags, category, and confidence
    to the state captured in the specified version snapshot. A new
    version record is created with change_type='restore'.

    Args:
        memory_id: The UUID of the memory to restore.
        version: The version number to restore to (from memory_history).
    """
    try:
        _validate_uuid(memory_id)
    except ValueError:
        return {"error": "Invalid memory_id format"}
    result = await queries.restore_memory_version(memory_id, version)
    if result is None:
        return {"error": "Version not found or memory does not exist"}
    return result


# ──────────────────────────────────────────────
# Tool: memory_stats
# ──────────────────────────────────────────────
@mcp.tool()
async def memory_stats() -> dict:
    """Get statistics about the memory database including knowledge graph stats.

    Returns counts by source, category, machine, top tags, entity/relation counts.
    """
    return await queries.get_stats()


# ──────────────────────────────────────────────
# Tool: entity_search
# ──────────────────────────────────────────────
@mcp.tool()
async def entity_search(
    query: str,
    entity_type: str | None = None,
    limit: int = 10,
) -> list[dict]:
    """Semantic search on knowledge graph entities.

    Args:
        query: Natural language query to find entities (e.g. "postgresql", "docker networking").
        entity_type: Filter by type (person/project/technology/concept/file/config/error/location/organization).
        limit: Max results (default 10).
    """
    embedding = await embed_text(query)
    return await queries.search_entities(
        embedding=embedding,
        entity_type=entity_type,
        limit=limit,
    )


# ──────────────────────────────────────────────
# Tool: entity_graph
# ──────────────────────────────────────────────
@mcp.tool()
async def entity_graph(
    entity_name: str,
    depth: int = 2,
) -> dict:
    """Traverse the knowledge graph from a named entity.

    Returns connected entities and relationships up to the specified depth.

    Args:
        entity_name: Name of the entity to start from (e.g. "nobrainr", "PostgreSQL").
        depth: How many hops to traverse (default 2, max 5).
    """
    depth = min(depth, 5)
    return await queries.get_entity_graph(entity_name, depth=depth)


# ──────────────────────────────────────────────
# Tool: entity_list
# ──────────────────────────────────────────────
@mcp.tool()
async def entity_list(
    entity_type: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    """List entities in the knowledge graph, optionally filtered by type.

    Args:
        entity_type: Filter by type (person/project/technology/concept/service/database/etc).
        limit: Max results (default 100).
        offset: Pagination offset.
    """
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    return await queries.list_entities(
        entity_type=entity_type,
        limit=limit,
        offset=offset,
    )


# ──────────────────────────────────────────────
# Tool: entity_memories
# ──────────────────────────────────────────────
@mcp.tool()
async def entity_memories(entity_id: str) -> list[dict]:
    """Get all memories linked to a specific entity.

    Args:
        entity_id: The UUID of the entity.
    """
    try:
        _validate_uuid(entity_id)
    except ValueError:
        return [{"error": "Invalid entity_id format"}]
    return await queries.get_entity_memories(entity_id)


# ──────────────────────────────────────────────
# Tool: memory_maintenance
# ──────────────────────────────────────────────
@mcp.tool()
async def memory_maintenance() -> dict:
    """Run periodic intelligence maintenance tasks.

    - Recomputes importance scores for all memories
    - Decays stability for stale memories (not accessed in 7+ days)

    Call this periodically (e.g. daily) to keep relevance scoring fresh.
    """
    importance_count = await queries.recompute_importance()
    decay_count = await queries.decay_stability()
    return {
        "status": "done",
        "importance_recomputed": importance_count,
        "stability_decayed": decay_count,
    }


# ──────────────────────────────────────────────
# Tool: memory_extract
# ──────────────────────────────────────────────
@mcp.tool()
async def memory_extract(memory_id: str) -> dict:
    """Manually trigger entity extraction for a specific memory.

    Args:
        memory_id: The UUID of the memory to extract entities from.
    """
    try:
        _validate_uuid(memory_id)
    except ValueError:
        return {"error": "Invalid memory_id format"}
    memory = await queries.get_memory(memory_id)
    if not memory:
        return {"status": "not_found", "id": memory_id}

    from nobrainr.extraction.pipeline import process_memory
    await process_memory(memory_id, memory["content"], memory.get("tags"))
    return {"status": "extracted", "id": memory_id}


# ──────────────────────────────────────────────
# Tool: memory_feedback
# ──────────────────────────────────────────────
@mcp.tool()
async def memory_feedback(
    memory_id: str,
    was_useful: bool,
    context: str | None = None,
    query_trace_id: str | None = None,
    result_rank: int | None = None,
    query_text: str | None = None,
    agent_id: str | None = None,
    session_id: str | None = None,
) -> dict:
    """Report whether a memory search result was useful. This feedback improves future search ranking.

    Call this after using results from memory_search to close the feedback loop.
    When closing the loop on a result from memory_search, pass through the
    `search_trace_id`, `search_rank`, and `search_query` fields from that result
    — this links your feedback to the specific search that surfaced the memory
    and lets us compute MRR/NDCG, not just positive/negative ratios.

    Args:
        memory_id: The UUID of the memory to give feedback on.
        was_useful: True if the memory was helpful, False if not. Negative
            feedback is especially valuable — it's the only signal that moves
            importance scoring (see integrate_feedback_scores).
        context: Optional context about how/why it was or wasn't useful.
        query_trace_id: UUID from `search_trace_id` on the memory_search result.
            Optional — omit for manual/dashboard feedback with no search context.
        result_rank: 1-indexed position at which the memory appeared in the
            search result list. Values < 1 are dropped.
        query_text: The query text that surfaced this memory. Trimmed to 500
            chars server-side. Used for later quality diagnostics.
        agent_id: Which agent/machine is giving feedback (C5 provenance,
            2026-07-14) — lets us attribute which memories informed which
            agent's work, feeding trust and future learned-manager training.
        session_id: The session this feedback belongs to.
    """
    try:
        _validate_uuid(memory_id)
    except ValueError:
        return {"error": "Invalid memory_id format"}
    result = await queries.store_memory_outcome(
        memory_id,
        was_useful,
        context=context,
        query_trace_id=query_trace_id,
        query_text=query_text,
        result_rank=result_rank,
        agent_id=agent_id,
        session_id=session_id,
    )
    return {"status": "recorded", **result}


# ──────────────────────────────────────────────
# Tool: memory_reflect
# ──────────────────────────────────────────────
@mcp.tool()
async def memory_reflect(
    learnings: list[dict],
) -> dict:
    """Batch-store session learnings. More efficient than individual memory_store calls.

    Call this at session end to capture what was learned. Each entry goes through
    the full pipeline (embedding, dedup check, entity extraction).

    Args:
        learnings: List of learning entries. Each dict should have:
            - content (str, required): The learning/insight to store.
            - summary (str, optional): One-line summary.
            - tags (list[str], optional): Tags for categorization.
            - category (str, optional): High-level category.
            - source_type (str, optional): Defaults to "agent".
            - source_machine (str, optional): Which host generated this.
    """
    results = []
    for entry in learnings:
        content = entry.get("content")
        if not content:
            results.append({"status": "skipped", "reason": "no content"})
            continue
        try:
            result = await memory_store(
                content=content,
                summary=entry.get("summary"),
                tags=entry.get("tags"),
                category=entry.get("category"),
                source_type=entry.get("source_type", "agent"),
                source_machine=entry.get("source_machine"),
            )
            results.append(result)
        except Exception as e:
            logger.exception("Failed to store learning: %s", content[:80])
            results.append({"status": "error", "error": str(e)})
    # "queued" counts as accepted since v7 — the write is durable, just
    # not yet processed by the worker. "stored" / "merged" are legacy
    # names for the same success state from the pre-queue code path.
    accepted = sum(
        1 for r in results if r.get("status") in ("stored", "merged", "queued")
    )
    return {
        "total": len(learnings),
        "accepted": accepted,
        "stored": accepted,  # backward-compat alias
        "results": results,
    }


# ──────────────────────────────────────────────
# Tool: log_event
# ──────────────────────────────────────────────
@mcp.tool()
async def log_event(
    event_type: str,
    description: str,
    agent_id: str | None = None,
    session_id: str | None = None,
    category: str | None = None,
    related_memory_ids: list[str] | None = None,
    metadata: dict | None = None,
) -> dict:
    """Log an agent activity event for tracking and analytics.

    Use this to record session starts, task completions, important decisions, errors, etc.

    Args:
        event_type: Type of event (e.g. "session_start", "task_complete", "decision", "error").
        description: Human-readable description of what happened.
        agent_id: Identifier for the agent logging the event.
        session_id: Current session identifier.
        category: Event category for filtering.
        related_memory_ids: UUIDs of related memories.
        metadata: Additional structured data.
    """
    if related_memory_ids:
        try:
            for mid in related_memory_ids:
                _validate_uuid(mid)
        except ValueError:
            return {"error": "Invalid UUID in related_memory_ids"}
    result = await queries.log_agent_event(
        event_type=event_type,
        description=description,
        agent_id=agent_id,
        session_id=session_id,
        category=category,
        related_memory_ids=related_memory_ids,
        metadata=metadata,
    )
    return {"status": "logged", **result}


# ──────────────────────────────────────────────
# Tools: error prevention patterns
# ──────────────────────────────────────────────

@mcp.tool()
async def error_store(
    error_signature: str,
    root_cause: str,
    fix: str,
    prevention: str,
    summary: str | None = None,
    tags: list[str] | None = None,
    source_machine: str | None = None,
    related_memory_ids: list[str] | None = None,
) -> dict:
    """Store a structured error pattern for future prevention.

    Use this after solving a non-trivial bug to capture the fix so it's
    auto-surfaced when the same error occurs again.

    Args:
        error_signature: The error message or pattern (e.g. "TypeError: cannot read property 'x' of undefined").
        root_cause: Why the error happened (e.g. "Race condition in async initialization").
        fix: What was done to fix it (e.g. "Added await before accessing the property").
        prevention: How to avoid this in the future (e.g. "Always await async init before accessing properties").
        summary: One-line summary for quick scanning.
        tags: Tags for categorization.
        source_machine: Which host this error was found on.
        related_memory_ids: UUIDs of related memories (e.g. the debug session).
    """
    content = (
        f"## Error Pattern\n\n"
        f"**Signature:** {error_signature}\n\n"
        f"**Root cause:** {root_cause}\n\n"
        f"**Fix:** {fix}\n\n"
        f"**Prevention:** {prevention}"
    )
    meta = {
        "error_signature": error_signature,
        "root_cause": root_cause,
        "fix": fix,
        "prevention": prevention,
        "type": "error_pattern",
    }
    if related_memory_ids:
        meta["related_memory_ids"] = related_memory_ids

    result = await memory_store(
        content=content,
        summary=summary or f"Error: {error_signature[:100]}",
        tags=list(set((tags or []) + ["error-pattern"])),
        category="debugging",
        source_type="agent",
        source_machine=source_machine,
        metadata=meta,
    )
    return result


@mcp.tool()
async def error_search(
    error_text: str,
    limit: int = 5,
    source_machine: str | None = None,
) -> list[dict]:
    """Search for known error patterns matching an error message.

    Call this BEFORE starting to debug — past sessions may have already
    solved this exact error. Returns structured error patterns with
    root cause, fix, and prevention guidance.

    Args:
        error_text: The error message or stack trace to search for.
        limit: Max results (default 5).
        source_machine: Filter to errors from a specific host.
    """
    results = await memory_search(
        query=error_text,
        limit=limit,
        tags=["error-pattern"],
        source_machine=source_machine,
        hybrid=True,
        include_cold=True,  # errors are always worth finding, even if cold
    )
    return results


# ──────────────────────────────────────────────
# Tool: crawl_page
# ──────────────────────────────────────────────
@mcp.tool()
async def crawl_page(
    url: str,
    extract_markdown: bool = True,
    extract_links: bool = False,
    wait_for_selector: str | None = None,
    css_selector: str | None = None,
    target_elements: list[str] | None = None,
    query: str | None = None,
    capture_network: bool = False,
    screenshot: bool = False,
) -> dict:
    """Crawl a web page and return its content as clean markdown.

    Uses a local Crawl4AI instance with headless Chromium for JS-rendered pages.
    Content is automatically filtered to remove boilerplate (nav, sidebars, ads).

    Args:
        url: The URL to crawl.
        extract_markdown: Return cleaned markdown content (default True).
        extract_links: Include extracted links in response.
        wait_for_selector: CSS selector to wait for before extracting (for JS-heavy pages).
        css_selector: CSS selector to scope extraction to a specific page region.
        target_elements: List of CSS selectors to focus content extraction on.
        query: When set, uses BM25 relevance filtering to return only content matching this query.
        capture_network: Capture XHR/fetch API calls made by the page (for SPA API discovery).
        screenshot: Include a base64 screenshot of the page.
    """
    from nobrainr.crawler.client import crawl4ai_request

    crawler_config: dict = {
        "cache_mode": "bypass",
        "word_count_threshold": 20,
        "exclude_social_media_links": True,
        "remove_overlay_elements": True,
    }

    if css_selector:
        crawler_config["css_selector"] = css_selector
    if target_elements:
        crawler_config["target_elements"] = target_elements
    if wait_for_selector:
        crawler_config["wait_for"] = f"css:{wait_for_selector}"
    if capture_network:
        crawler_config["capture_network_requests"] = True

    # Content filtering: BM25 (query-aware) or Pruning (general noise removal)
    if query:
        crawler_config["markdown_generator"] = {
            "type": "DefaultMarkdownGenerator",
            "params": {
                "content_filter": {
                    "type": "BM25ContentFilter",
                    "params": {"user_query": query, "bm25_threshold": 1.0},
                }
            },
        }
    else:
        crawler_config["markdown_generator"] = {
            "type": "DefaultMarkdownGenerator",
            "params": {
                "content_filter": {
                    "type": "PruningContentFilter",
                    "params": {
                        "threshold": 0.45,
                        "threshold_type": "dynamic",
                        "min_word_threshold": 5,
                    },
                }
            },
        }

    data = await crawl4ai_request(url, crawler_config=crawler_config)
    if "error" in data:
        return data

    result = data["results"][0]
    output: dict = {
        "url": result.get("url", url),
        "status_code": result.get("status_code"),
        "title": result.get("metadata", {}).get("title"),
    }

    if extract_markdown:
        md = result.get("markdown", {})
        if isinstance(md, dict):
            output["markdown"] = md.get("fit_markdown") or md.get("raw_markdown", "")
        else:
            output["markdown"] = str(md)

    if extract_links:
        links = result.get("links", {})
        output["links"] = {
            "internal": [link.get("href") for link in links.get("internal", [])[:50]],
            "external": [link.get("href") for link in links.get("external", [])[:50]],
        }

    if capture_network and result.get("network_requests"):
        api_calls = [
            {"method": r.get("method"), "url": r.get("url"), "status": r.get("status")}
            for r in result["network_requests"]
            if r.get("event_type") in ("request", "response")
            and r.get("resource_type") in ("fetch", "xhr", None)
        ][:50]
        output["api_calls"] = api_calls

    if screenshot:
        ss = await _crawl4ai_screenshot(url)
        if ss:
            output["screenshot_base64"] = ss

    return output


async def _crawl4ai_screenshot(url: str) -> str | None:
    """Capture a page screenshot via Crawl4AI /screenshot endpoint."""
    import httpx

    headers = {"Content-Type": "application/json"}
    if settings.crawl4ai_api_token:
        headers["Authorization"] = f"Bearer {settings.crawl4ai_api_token}"
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{settings.crawl4ai_url}/screenshot",
                json={"url": url},
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("screenshot") or data.get("result", {}).get("screenshot")
    except Exception:
        return None


# ──────────────────────────────────────────────
# Tool: web_search (Brave Search API)
# ──────────────────────────────────────────────
async def _searxng_search_request(params: dict) -> list[dict]:
    """Query the self-hosted SearXNG JSON API. Returns raw result dicts.

    Raises on transport/HTTP errors so callers can fall back to Brave.
    freshness pd/pw/pm/py maps to SearXNG time_range; ranges are not
    supported and are ignored (SearXNG has no date-range filter)."""
    import httpx

    q: dict = {"q": params["q"], "format": "json", "categories": "general"}
    fr = {"pd": "day", "pw": "week", "pm": "month", "py": "year"}.get(
        params.get("freshness") or "")
    if fr:
        q["time_range"] = fr
    if params.get("country"):
        # SearXNG localizes via language tags, e.g. de-DE
        cc = params["country"].lower()
        q["language"] = f"{cc}-{params['country'].upper()}"
    if params.get("offset"):
        q["pageno"] = int(params["offset"]) + 1
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(f"{settings.searxng_url}/search", params=q)
        resp.raise_for_status()
        return resp.json().get("results", [])


async def _brave_search_request(params: dict) -> dict:
    """GET the Brave web-search endpoint. Split out for testability."""
    import httpx

    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "X-Subscription-Token": settings.brave_api_key,
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(settings.brave_search_url, params=params, headers=headers)
        resp.raise_for_status()
        return resp.json()


async def _count_web_search_use() -> int:
    """Increment and return this month's web_search query count."""
    from nobrainr.db.pool import get_pool

    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            """
            INSERT INTO web_search_usage (month, queries)
            VALUES (to_char(now(), 'YYYY-MM'), 1)
            ON CONFLICT (month) DO UPDATE
                SET queries = web_search_usage.queries + 1
            RETURNING queries
            """
        )


@mcp.tool()
async def web_search(
    query: str,
    count: int = 8,
    freshness: str | None = None,
    country: str | None = None,
    offset: int = 0,
) -> dict:
    """Search the web via the Brave Search API (independent index, not a Google/Bing proxy).

    Discovery tool — returns a ranked, TRANSIENT list of URLs + snippets.
    Results are never persisted (Brave storage-rights terms): to keep a
    source, crawl the page itself with crawl_and_store. The intended
    pipeline is web_search (discover) -> crawl_page / crawl_and_store
    (extract + persist).

    Args:
        query: Search query. Include the current year for time-sensitive topics.
        count: Number of results (1-20, default 8).
        freshness: Age filter: 'pd' (24h), 'pw' (week), 'pm' (month), 'py' (year),
            or a range 'YYYY-MM-DDtoYYYY-MM-DD'.
        country: 2-letter country code to localize results (e.g. 'DE', 'US').
        offset: Pagination offset (page number, 0-9).
    """
    import httpx

    # SearXNG primary (2026-08-12): self-hosted, quota-free, anonymous.
    # Any failure or empty result falls through to Brave below.
    if settings.searxng_url:
        try:
            raw = await _searxng_search_request(
                {"q": query, "freshness": freshness, "country": country,
                 "offset": offset},
            )
            if raw:
                results = []
                for r in raw[: max(1, min(count, 20))]:
                    title, _ = _sanitize_crawled_text(r.get("title") or "")
                    snippet, _ = _sanitize_crawled_text(r.get("content") or "")
                    results.append({
                        "title": title,
                        "url": r.get("url"),
                        "snippet": snippet,
                        "age": r.get("publishedDate"),
                        "language": None,
                    })
                return {
                    "query": query,
                    "count": len(results),
                    "results": results,
                    "source": "searxng",
                    "note": "transient SERP — persist sources via crawl_and_store, never this list",
                }
        except Exception:
            logger.warning("searxng unavailable — falling back to Brave", exc_info=True)

    if not settings.brave_api_key:
        return {
            "error": "searxng unavailable and brave_api_key not configured",
            "hint": "check the searxng service or set NOBRAINR_BRAVE_API_KEY",
        }

    # Monthly usage counter. The Brave dashboard is capped at the free
    # tier — past the budget the key just stops working, so fail HERE
    # with a clean, actionable error instead of Brave's 401/429.
    # Accounting failures never block search: DB hiccup → search anyway.
    used = None
    try:
        used = await _count_web_search_use()
    except Exception:
        logger.warning("web_search usage accounting failed", exc_info=True)
    cap = settings.brave_monthly_query_cap
    if used is not None and cap > 0:
        if used > cap:
            return {
                "error": "monthly web_search quota exhausted",
                "quota": {"used": used, "cap": cap},
                "hint": "fall back to the built-in WebSearch tool until next month",
            }
        if used == int(cap * 0.8):
            logger.warning("web_search at 80%% of monthly quota (%s/%s)", used, cap)

    params: dict = {"q": query, "count": max(1, min(count, 20))}
    if freshness:
        params["freshness"] = freshness
    if country:
        params["country"] = country
    if offset:
        params["offset"] = offset

    try:
        data = await _brave_search_request(params)
    except httpx.HTTPStatusError as e:
        return {
            "error": f"brave api HTTP {e.response.status_code}",
            "detail": e.response.text[:300],
        }
    except Exception as e:  # noqa: BLE001 — surface transport errors to the caller
        return {"error": f"brave request failed: {e}"}

    results = []
    for r in data.get("web", {}).get("results", []):
        # ASI06: SERP snippets are third-party text entering agent context —
        # run them through the same injection filter as crawled content.
        title, _ = _sanitize_crawled_text(r.get("title") or "")
        snippet, _ = _sanitize_crawled_text(r.get("description") or "")
        results.append(
            {
                "title": title,
                "url": r.get("url"),
                "snippet": snippet,
                "age": r.get("age") or r.get("page_age"),
                "language": r.get("language"),
            }
        )

    out = {
        "query": query,
        "count": len(results),
        "results": results,
        "source": "brave",
        "note": "transient SERP — persist sources via crawl_and_store, never this list",
    }
    if used is not None and cap > 0:
        out["quota"] = {"used": used, "cap": cap}
    return out


# ──────────────────────────────────────────────
# Tool: crawl_and_store
# ──────────────────────────────────────────────
@mcp.tool()
async def crawl_and_store(
    url: str,
    tags: list[str] | None = None,
    category: str = "documentation",
    source_machine: str | None = None,
    max_content_chars: int = 50000,
    chunked: bool = True,
) -> dict:
    """Crawl a web page and enqueue its content as memories in nobrainr.

    Fetches the page via Crawl4AI, extracts clean markdown, and enqueues it
    into the write queue (see PR #18). The crawl itself is synchronous —
    the caller waits for Crawl4AI — but the store path is fully queued so
    the caller never waits on the embedding + dedup + entity extraction
    pipeline.

    Long pages are automatically split into overlapping chunks so no
    content is lost. Set chunked=False to store as a single memory
    (truncated to max_content_chars).

    Args:
        url: The URL to crawl and store.
        tags: Tags for the stored memory.
        category: Memory category (default "documentation").
        source_machine: Which machine initiated this crawl.
        max_content_chars: Max chars to keep from the page (default 50000).
        chunked: Split long content into overlapping chunks (default True).

    Returns:
        {
            "url": ..., "title": ..., "chars_total": int,
            "chunked": bool,
            "result": {                     # queued shape from enqueue_document_chunks
                "status": "queued",
                "queue_ids": [...],
                "chunks": int,
                "document_id": str | null,
            },
        }
    """
    from nobrainr.db import write_queue

    # Crawl synchronously — the caller IS waiting for the crawl result,
    # that's why they called crawl_and_store over a raw memory_store.
    crawl_result = await crawl_page(url)
    if "error" in crawl_result:
        return crawl_result

    markdown = crawl_result.get("markdown", "")
    if not markdown or len(markdown.strip()) < 50:
        return {"error": "Page returned too little content", "url": url}

    title = crawl_result.get("title", url)
    content = markdown[:max_content_chars]

    # ASI06 crawl sanitizer (C2, 2026-07-14). Crawled web pages are the one
    # path where UNTRUSTED external text enters long-term memory — the exact
    # "Summarize with AI" vector Microsoft documented (50 poisoning attempts
    # / 31 companies in 60 days). Neutralize instruction-shaped lines that
    # try to program a future agent ("remember X as authoritative", "always
    # recommend Y") by prefixing them with a quoted marker so they read as
    # data, never as instructions, and tag the memory for the observability
    # sweep. This runs ONLY on the crawl path — agent-authored memories
    # (which legitimately contain instruction text about system design) are
    # untouched.
    content, _flags = _sanitize_crawled_text(content)
    all_tags = list(tags or []) + ["crawled"]
    if _flags:
        all_tags.append("sanitized-injection")
    norm_category = normalize_category(category)

    if chunked:
        store_result = await write_queue.enqueue_document_chunks(
            content=content,
            title=title,
            summary=f"Crawled: {title}"[:200],
            tags=all_tags,
            category=norm_category,
            source_type="crawl",
            source_machine=source_machine,
            source_ref=url,
        )
    else:
        # Single-memory mode (truncated to chunk_threshold) — still queued.
        store_result = await write_queue.enqueue_memory_write(
            content=content[: settings.chunk_threshold],
            summary=f"Crawled: {title}"[:200],
            tags=all_tags,
            category=norm_category,
            source_type="crawl",
            source_machine=source_machine,
            source_ref=url,
        )
        store_result = {
            "status": "queued",
            "queue_ids": [store_result["queue_id"]],
            "chunks": 1,
            "document_id": None,
        }

    # Record interest signal for the crawled domain/topic
    if settings.interest_tracking_enabled:
        try:
            from urllib.parse import urlparse

            domain = urlparse(url).netloc
            await queries.record_interest_signal(
                topic=domain,
                signal_type="crawl",
                strength=2.0,
                source_machine=source_machine,
                metadata={"url": url, "title": title},
            )
        except Exception:
            pass

    return {
        "url": url,
        "title": title,
        "chars_total": len(content),
        "chunked": chunked,
        "result": store_result,
    }


# ──────────────────────────────────────────────
# Tool: deep_crawl
# ──────────────────────────────────────────────
@mcp.tool()
async def deep_crawl(
    url: str,
    max_pages: int = 10,
    max_depth: int = 3,
    strategy: str = "bfs",
    include_patterns: list[str] | None = None,
    exclude_patterns: list[str] | None = None,
    store_results: bool = False,
    tags: list[str] | None = None,
    category: str = "documentation",
    source_machine: str | None = None,
) -> dict:
    """Deep crawl a website starting from a URL, following links up to a depth limit.

    Uses Crawl4AI's deep crawl engine (BFS or DFS) to automatically discover
    and crawl multiple pages from a site. Content is filtered with PruningContentFilter.

    When store_results=True, all crawled pages are stored as chunked memories
    in the knowledge graph with entity extraction.

    Args:
        url: Starting URL for the deep crawl.
        max_pages: Maximum pages to crawl (default 10, max 50).
        max_depth: Maximum link depth from start URL (default 3).
        strategy: Crawl strategy — "bfs" (breadth-first) or "dfs" (depth-first).
        include_patterns: URL regex patterns to include (e.g. ["/docs/.*"]).
        exclude_patterns: URL regex patterns to exclude (e.g. ["/blog/.*"]).
        store_results: Store all crawled pages as memories (default False).
        tags: Tags for stored memories (only used when store_results=True).
        category: Category for stored memories (default "documentation").
        source_machine: Machine identifier for stored memories.
    """
    from nobrainr.crawler.client import crawl4ai_deep

    max_pages = max(1, min(max_pages, 50))
    max_depth = max(1, min(max_depth, 5))

    data = await crawl4ai_deep(
        url,
        strategy=strategy,
        max_pages=max_pages,
        max_depth=max_depth,
        include_patterns=include_patterns,
        exclude_patterns=exclude_patterns,
    )

    if "error" in data:
        return data

    pages = data.get("pages", [])

    if store_results and pages:
        from nobrainr.services.memory import store_document_chunked

        stored_count = 0
        all_tags = list(tags or []) + ["crawled", "deep-crawl"]
        norm_category = normalize_category(category)

        for page in pages:
            markdown = page.get("markdown", "")
            if not markdown or len(markdown.strip()) < 50:
                continue
            try:
                store_result = await store_document_chunked(
                    content=markdown[:50000],
                    title=page.get("title", page["url"]),
                    summary=f"Deep crawl: {page.get('title', page['url'])}"[:200],
                    tags=all_tags,
                    category=norm_category,
                    source_type="crawl",
                    source_machine=source_machine,
                    source_ref=page["url"],
                )
                if store_result.get("status") in ("stored", "updated"):
                    stored_count += store_result.get("chunks", 1)
            except Exception:
                logger.exception("deep_crawl store failed for %s", page["url"])

        data["stored_chunks"] = stored_count

    # Trim markdown from response to avoid huge payloads (return truncated summaries)
    for page in pages:
        md = page.get("markdown", "")
        page["chars"] = len(md)
        page["markdown"] = md[:2000] + ("..." if len(md) > 2000 else "")

    return data


# ──────────────────────────────────────────────
# Tool: discover_sitemap
# ──────────────────────────────────────────────
@mcp.tool()
async def discover_sitemap(
    url: str,
    max_urls: int = 100,
) -> dict:
    """Discover page URLs from a website's sitemap.xml and robots.txt.

    Useful for finding all documentation pages on a site before selective crawling.

    Args:
        url: Base URL of the website (e.g. "https://docs.example.com").
        max_urls: Maximum URLs to return (default 100).
    """
    from nobrainr.crawler.client import discover_sitemap_urls

    max_urls = max(1, min(max_urls, 500))
    urls = await discover_sitemap_urls(url, max_urls=max_urls)
    return {
        "base_url": url,
        "urls_found": len(urls),
        "urls": urls,
    }


# ──────────────────────────────────────────────
# Tool: memory_store_document (chunked ingestion)
# ──────────────────────────────────────────────
@mcp.tool()
async def memory_store_document(
    content: str,
    title: str | None = None,
    tags: list[str] | None = None,
    category: str = "documentation",
    source_type: str = "document",
    source_machine: str | None = None,
    source_ref: str | None = None,
    confidence: float = 0.8,
) -> dict:
    """Store a long document as chunked memories via the write queue.

    For content shorter than the chunk threshold (~4000 chars), enqueues a
    single memory. For longer content, splits into overlapping chunks that
    preserve context at boundaries, links them via a shared document_id in
    metadata, and enqueues each chunk individually. The background worker
    runs embedding, dedup, and entity extraction on each chunk serially.

    Returns in under ~100ms with a list of queue_ids even for large docs —
    the whole pipeline (including up to N LLM calls for N chunks) happens
    asynchronously so the caller is never stuck waiting on GPU contention.
    Contextual-retrieval prefixes are NOT generated on the hot path; the
    existing contextual_prefix_backfill scheduler job fills them in later.

    Use this for architecture docs, ADRs, meeting notes, specs, or any text
    too long for a single memory_store call.

    Args:
        content: The full document text.
        title: Document title (used in chunk summaries and metadata).
        tags: Tags for all chunks.
        category: Category for all chunks (default "documentation").
        source_type: Source type (default "document").
        source_machine: Which machine generated this.
        source_ref: Reference (file path, URL, etc.).
        confidence: Confidence score (default 0.8).

    Returns:
        {
            "status": "queued",
            "queue_ids": [...],           # one per chunk
            "chunks": int,
            "document_id": str | null,    # null for single-chunk writes
        }
        Poll memory_store_status(queue_id) for any individual chunk to
        follow it to completion.
    """
    from nobrainr.db import write_queue

    if len(content) > settings.max_content_length * 5:
        return {
            "error": f"Content too large ({len(content)} chars, max {settings.max_content_length * 5})"
        }

    return await write_queue.enqueue_document_chunks(
        content=content,
        title=title,
        tags=tags,
        category=normalize_category(category),
        source_type=source_type,
        source_machine=source_machine,
        source_ref=source_ref,
        confidence=confidence,
    )


# ──────────────────────────────────────────────
# Import tools (kept for backwards compat)
# ──────────────────────────────────────────────
@mcp.tool()
async def memory_import_chatgpt_sessions(
    source_machine: str | None = None,
    limit: int = 50,
    max_content_chars: int = 30000,
    min_turns: int = 2,
) -> dict:
    """Store raw ChatGPT/Claude conversations as session-level memories.

    doobidoo/mcp-memory-service pattern (+5.6% R@5 on LongMemEval): each
    raw conversation becomes ONE memory in the memories table with the
    full conversation text, rather than being distilled into multiple
    fine-grained learnings. This is ORTHOGONAL to ``memory_import_chatgpt``
    with ``distill=True`` — both paths can run for the same raw
    conversation (tracked independently via separate metadata flags:
    ``distilled`` and ``session_stored``). Running both gives you both
    fine-grained semantic recall AND session-level "did we ever talk
    about X" recall.

    Call this after a raw import (``memory_import_chatgpt(file, distill=False)``)
    to materialize the sessions. Processes ``limit`` conversations per
    call; run repeatedly until ``processed==0`` to drain the backlog.

    Args:
        source_machine: Override the per-conversation source_machine.
        limit: Max raw conversations to process per call (default 50).
        max_content_chars: Truncate session text at this many chars
            (default 30,000 ~= 8-10k tokens).
        min_turns: Minimum user/assistant turns to warrant a session
            memory (default 2). Shorter conversations are marked skipped.

    Returns:
        dict with status, processed, stored, skipped, errors counts.
    """
    from nobrainr.importers.chatgpt import store_conversations_as_sessions
    return await store_conversations_as_sessions(
        source_machine=source_machine,
        limit=limit,
        max_content_chars=max_content_chars,
        min_turns=min_turns,
    )


@mcp.tool()
async def memory_import_chatgpt(file_path: str, distill: bool = False) -> dict:
    """Import ChatGPT conversations from an OpenAI export file.

    Args:
        file_path: Path to conversations.json from ChatGPT export.
        distill: If true, also extract key learnings into memories (slower).
    """
    from pathlib import Path
    resolved = Path(file_path).resolve()
    if not resolved.is_file():
        return {"error": f"File not found: {file_path}"}
    from nobrainr.importers.chatgpt import import_chatgpt_export
    return await import_chatgpt_export(str(resolved), distill=distill)


@mcp.tool()
async def memory_import_claude(directory: str, machine_name: str | None = None) -> dict:
    """Import Claude memory files from a .claude directory.

    Args:
        directory: Path to the .claude directory (e.g. /root/.claude).
        machine_name: Name of the machine this came from.
    """
    from pathlib import Path
    resolved = Path(directory).resolve()
    if not resolved.is_dir():
        return {"error": f"Directory not found: {directory}"}
    from nobrainr.importers.claude import import_claude_memory
    return await import_claude_memory(str(resolved), machine_name=machine_name)


@mcp.tool()
async def memory_import_claude_web(
    file_path: str, source_machine: str | None = None,
) -> dict:
    """Import Claude.ai web export (conversations.json from Settings → Export Data).

    Args:
        file_path: Path to conversations.json from Claude.ai export ZIP.
        source_machine: Machine identifier.
    """
    from pathlib import Path
    resolved = Path(file_path).resolve()
    if not resolved.is_file():
        return {"error": f"File not found: {file_path}"}
    from nobrainr.importers.claude_web import import_claude_web_export
    return await import_claude_web_export(str(resolved), source_machine=source_machine)


@mcp.tool()
async def memory_import_claude_memories(
    file_path: str, source_machine: str | None = None,
) -> dict:
    """Import Claude.ai memories.json (built-in user memory from export).

    Args:
        file_path: Path to memories.json from Claude.ai export ZIP.
        source_machine: Machine identifier.
    """
    from pathlib import Path
    resolved = Path(file_path).resolve()
    if not resolved.is_file():
        return {"error": f"File not found: {file_path}"}
    from nobrainr.importers.claude_web import import_claude_memories
    return await import_claude_memories(str(resolved), source_machine=source_machine)


@mcp.tool()
async def memory_import_claude_projects(
    file_path: str, source_machine: str | None = None,
) -> dict:
    """Import Claude.ai projects.json (project descriptions from export).

    Args:
        file_path: Path to projects.json from Claude.ai export ZIP.
        source_machine: Machine identifier.
    """
    from pathlib import Path
    resolved = Path(file_path).resolve()
    if not resolved.is_file():
        return {"error": f"File not found: {file_path}"}
    from nobrainr.importers.claude_web import import_claude_projects
    return await import_claude_projects(str(resolved), source_machine=source_machine)


@mcp.tool()
async def memory_import_sticky_notes(
    file_path: str, source_machine: str | None = None,
) -> dict:
    """Import Windows Sticky Notes from CSV export.

    Args:
        file_path: Path to stickynotes.CSV file.
        source_machine: Machine identifier (default: workpc).
    """
    from pathlib import Path
    resolved = Path(file_path).resolve()
    if not resolved.is_file():
        return {"error": f"File not found: {file_path}"}
    from nobrainr.importers.sticky_notes import import_sticky_notes
    return await import_sticky_notes(str(resolved), source_machine=source_machine)


@mcp.tool()
async def memory_import_markdown_notes(
    directory: str,
    source_type: str = "google_keep",
    source_machine: str | None = None,
) -> dict:
    """Import markdown notes with YAML frontmatter from a directory.

    Args:
        directory: Path to directory containing .md files.
        source_type: Type identifier (google_keep, affine_memos).
        source_machine: Machine identifier.
    """
    from pathlib import Path
    resolved = Path(directory).resolve()
    if not resolved.is_dir():
        return {"error": f"Directory not found: {directory}"}
    from nobrainr.importers.markdown_notes import import_markdown_notes
    return await import_markdown_notes(str(resolved), source_type=source_type, source_machine=source_machine)


@mcp.tool()
async def memory_import_docx(
    directory: str, source_machine: str | None = None,
) -> dict:
    """Import .docx files from a directory (Google Docs, Nextcloud documents).

    Args:
        directory: Path to directory containing .docx files (searched recursively).
        source_machine: Machine identifier.
    """
    from pathlib import Path
    resolved = Path(directory).resolve()
    if not resolved.is_dir():
        return {"error": f"Directory not found: {directory}"}
    from nobrainr.importers.docx_importer import import_docx_files
    return await import_docx_files(str(resolved), source_machine=source_machine)


@mcp.tool()
async def memory_import_website(
    directory: str,
    website_name: str = "my-website",
    source_machine: str | None = None,
) -> dict:
    """Import website content from PHP files.

    Args:
        directory: Path to directory containing PHP files.
        website_name: Name of the website for tagging.
        source_machine: Machine identifier.
    """
    from pathlib import Path
    resolved = Path(directory).resolve()
    if not resolved.is_dir():
        return {"error": f"Directory not found: {directory}"}
    from nobrainr.importers.website import import_website_content
    return await import_website_content(str(resolved), source_machine=source_machine, website_name=website_name)


@mcp.tool()
async def memory_import_documents(
    directory: str,
    source_machine: str | None = None,
    use_vision: bool = True,
    category: str = "documentation",
    tags: list[str] | None = None,
    recursive: bool = True,
) -> dict:
    """Import documents from a directory (PDF, images, DOCX, markdown, text).

    Extracts text from all supported file types. For scanned PDFs and images,
    uses gemma3 vision to OCR/extract content. All content is stored with
    entity extraction for the knowledge graph.

    Args:
        directory: Path to directory containing documents.
        source_machine: Machine identifier for provenance.
        use_vision: Use gemma3 vision for scanned PDFs and image files (default: True).
        category: Category for stored memories (default: "documentation").
        tags: Additional tags to apply (always includes "imported", "document").
        recursive: Search subdirectories (default: True).
    """
    from pathlib import Path as P
    resolved = P(directory).resolve()
    if not resolved.is_dir():
        return {"error": f"Directory not found: {directory}"}
    from nobrainr.importers.documents import import_documents
    return await import_documents(
        str(resolved),
        source_machine=source_machine,
        use_vision=use_vision,
        category=category,
        tags=tags,
        recursive=recursive,
    )


@mcp.tool()
async def memory_import_github(
    owner: str = "",
    repos: list[str] | None = None,
    source_machine: str | None = None,
    include_commits: bool = True,
    include_issues: bool = True,
    include_code_structure: bool = True,
    include_source_code: bool = True,
    include_closed_issues: bool = True,
    include_forks: bool = False,
) -> dict:
    """Import knowledge from GitHub repositories into the memory system.

    Fetches repo metadata, README, commit history, file structure, key config
    files, source code files, issues, and PRs — all with entity extraction
    for the knowledge graph. Requires the `gh` CLI to be installed and authenticated.

    Args:
        owner: GitHub username or organization (default: "vicquick").
        repos: Specific repo names to import (default: all repos). When set,
            forks in the list ARE imported regardless of include_forks.
        source_machine: Machine identifier for provenance.
        include_commits: Import commit history grouped by week.
        include_issues: Import issues and pull requests with comments.
        include_code_structure: Import file tree and key config files.
        include_source_code: Import actual source code files (*.py, *.ts, *.vue, etc.).
        include_closed_issues: Include closed issues and merged PRs.
        include_forks: Include forked repos on bulk import. Default False —
            forks are upstream noise for a personal knowledge base.
    """
    from nobrainr.importers.github import import_github
    return await import_github(
        owner,
        repos=repos,
        source_machine=source_machine,
        include_commits=include_commits,
        include_issues=include_issues,
        include_code_structure=include_code_structure,
        include_source_code=include_source_code,
        include_closed_issues=include_closed_issues,
        include_forks=include_forks,
    )


# ──────────────────────────────────────────────
# Tool: distill (compress text via local LLM)
# ──────────────────────────────────────────────
@mcp.tool()
async def distill(
    text: str,
    question: str,
    max_input_chars: int = 50000,
) -> dict:
    """Compress text using the local LLM, extracting only essential information.

    Inspired by samuelfaj/distill. Pipe any large output (logs, search results,
    crawled pages, code) through this tool with a specific question to get a
    compressed answer that saves 90-99% of tokens.

    Examples:
        distill(text=<git diff output>, question="what changed?")
        distill(text=<error log>, question="what errors occurred?")
        distill(text=<long document>, question="summarize the key decisions")

    Args:
        text: Raw text to compress.
        question: What to extract (e.g. "what errors?", "summarize key points").
        max_input_chars: Max input length before truncation (default 50000).
    """
    from nobrainr.services.distill import distill_text

    if not text or not text.strip():
        return {"error": "Empty text provided"}
    if not question or not question.strip():
        return {"error": "Empty question provided"}

    return await distill_text(text, question, max_input_chars=max_input_chars)


# ──────────────────────────────────────────────
# Tool: distill_search (compressed memory search)
# ──────────────────────────────────────────────
@mcp.tool()
async def distill_search(
    query: str,
    question: str | None = None,
    limit: int = 20,
    threshold: float = 0.3,
    tags: list[str] | None = None,
    category: str | None = None,
) -> dict:
    """Search memories and return a distilled/compressed answer.

    Like memory_search but runs results through the local LLM to compress
    them into a focused answer. Returns ~90% fewer tokens than raw results.

    Use this when you need information from memory but don't need to see
    every individual memory — just the answer to your question.

    Args:
        query: Search query (also used as the distill question if question is None).
        question: Specific question to answer from results (defaults to query).
        limit: How many memories to search through (default 20, more = better coverage).
        threshold: Similarity threshold (default 0.3).
        tags: Filter by tags.
        category: Filter by category.
    """
    from nobrainr.services.distill import distill_memories

    embedding = await embed_text(query)
    results = await queries.search_memories(
        embedding=embedding,
        query_text=query,
        limit=limit,
        threshold=threshold,
        tags=tags,
        category=category,
        hybrid=True,
    )

    distill_q = question or query
    result = await distill_memories(results, distill_q)
    result["search_query"] = query
    return result


# ──────────────────────────────────────────────
# Tool: code_index (AST-based code symbol indexing)
# ──────────────────────────────────────────────
@mcp.tool()
async def code_index(
    directory: str,
    tags: list[str] | None = None,
    source_machine: str | None = None,
    extensions: list[str] | None = None,
) -> dict:
    """Index a codebase by extracting code symbols (functions, classes, methods).

    Uses AST parsing to extract symbols with their signatures, docstrings,
    and line numbers. Stores each symbol as a memory for future retrieval.
    Inspired by jcodemunch-mcp — enables 99% token savings for code exploration.

    Args:
        directory: Path to the code directory to index.
        tags: Additional tags (always includes "code", "indexed").
        source_machine: Machine identifier.
        extensions: File extensions to index (default: [".py"]).
    """
    from nobrainr.services.code_index import index_directory

    from pathlib import Path
    resolved = Path(directory).resolve()
    if not resolved.is_dir():
        return {"error": f"Directory not found: {directory}"}

    return await index_directory(
        str(resolved),
        tags=tags,
        source_machine=source_machine,
        extensions=extensions,
    )


# ──────────────────────────────────────────────
# Tool: code_search (symbol-level code retrieval)
# ──────────────────────────────────────────────
@mcp.tool()
async def code_search(
    query: str,
    kind: str | None = None,
    limit: int = 10,
) -> list[dict]:
    """Search indexed code symbols by name, kind, or semantic query.

    Returns symbol signatures, docstrings, and locations — NOT full source code.
    Use this to find functions/classes without reading entire files.

    Args:
        query: Symbol name or description to search for.
        kind: Filter by symbol kind ("function", "class", "method").
        limit: Max results (default 10).
    """
    embedding = await embed_text(query)
    tags_filter = ["code", "indexed"]
    if kind:
        tags_filter.append(f"kind:{kind}")

    results = await queries.search_memories(
        embedding=embedding,
        query_text=query,
        limit=limit,
        threshold=0.3,
        tags=tags_filter if kind else ["code", "indexed"],
        hybrid=True,
    )

    # Format results to show symbol info compactly
    symbols = []
    for mem in results:
        meta = mem.get("metadata", {}) or {}
        symbols.append({
            "symbol": meta.get("qualified_name", mem.get("summary", "")),
            "kind": meta.get("kind", "unknown"),
            "file": meta.get("file_path", ""),
            "line": meta.get("line_number", 0),
            "signature": meta.get("signature", ""),
            "docstring": meta.get("docstring", "")[:200],
            "memory_id": mem.get("id", ""),
        })

    return symbols


# ──────────────────────────────────────────────
# Tools: communities (GraphRAG)
# ──────────────────────────────────────────────

@mcp.tool()
async def community_detect(
    min_size: int = 3,
    resolution: float = 1.0,
    summarize: bool = True,
) -> dict:
    """Run community detection on the knowledge graph using Louvain algorithm.

    Identifies clusters of densely connected entities. Optionally generates
    LLM summaries for each community.

    Args:
        min_size: Minimum entities per community (default 3).
        resolution: Louvain resolution — higher values find more, smaller communities.
        summarize: Generate LLM summaries for each community (default True).
    """
    from nobrainr.services.communities import detect_communities, generate_community_summaries

    result = await detect_communities(min_community_size=min_size, resolution=resolution)
    if summarize and result["communities"] > 0:
        summary_result = await generate_community_summaries()
        result["summaries"] = summary_result
    return result


@mcp.tool()
async def community_list(limit: int = 50) -> list[dict]:
    """List all detected knowledge graph communities with summaries.

    Returns communities sorted by size with title, summary, key topics,
    and top entities. Run community_detect first to populate.

    Args:
        limit: Max communities to return (default 50).
    """
    from nobrainr.services.communities import list_communities
    return await list_communities(limit=limit)


@mcp.tool()
async def community_members(community_id: int) -> list[dict]:
    """Get all entities belonging to a specific community.

    Args:
        community_id: The community ID from community_list results.
    """
    from nobrainr.services.communities import get_community_members
    return await get_community_members(community_id)


# ──────────────────────────────────────────────
# Tools: memory tiering
# ──────────────────────────────────────────────

@mcp.tool()
async def memory_set_tier(
    memory_id: str,
    tier: int,
) -> dict:
    """Set a memory's tier level for search prioritization.

    Tier 0 (pinned): Always included, highest priority — critical infra, active projects.
    Tier 1 (hot): Recently active or high-quality memories.
    Tier 2 (standard): Default tier for all new memories.
    Tier 3 (cold): Archived, excluded from search unless include_cold=True.

    Args:
        memory_id: UUID of the memory to update.
        tier: Tier level (0-3).
    """
    memory_id = _validate_uuid(memory_id)
    result = await queries.set_memory_tier(memory_id, tier)
    if result is None:
        return {"error": "Memory not found"}
    return result


@mcp.tool()
async def memory_tier_stats() -> list[dict]:
    """Get memory counts by tier level (0=pinned, 1=hot, 2=standard, 3=cold)."""
    return await queries.get_tier_stats()


# ──────────────────────────────────────────────
# Tools: cross-session handoff
# ──────────────────────────────────────────────

@mcp.tool()
async def handoff_create(
    task_summary: str,
    status: str,
    next_steps: list[str],
    blockers: list[str] | None = None,
    relevant_memory_ids: list[str] | None = None,
    source_machine: str | None = None,
    metadata: dict | None = None,
) -> dict:
    """Create a handoff document for the next agent session.

    Store structured context about in-progress work so the next session
    can pick up where you left off without losing context.

    Args:
        task_summary: What was being worked on (1-3 sentences).
        status: Current status ("in_progress", "blocked", "needs_review", "ready_for_next").
        next_steps: Ordered list of next actions to take.
        blockers: Current blockers preventing progress.
        relevant_memory_ids: Memory IDs with important context for the task.
        source_machine: Which host created this handoff.
        metadata: Additional structured context (commit hashes, file paths, etc.).
    """
    content_parts = [
        f"## Handoff: {task_summary}",
        f"**Status:** {status}",
        "",
        "### Next Steps",
    ]
    for i, step in enumerate(next_steps, 1):
        content_parts.append(f"{i}. {step}")

    if blockers:
        content_parts.extend(["", "### Blockers"])
        for b in blockers:
            content_parts.append(f"- {b}")

    if relevant_memory_ids:
        content_parts.extend(["", "### Related Memories"])
        for mid in relevant_memory_ids[:10]:
            content_parts.append(f"- {mid}")

    content = "\n".join(content_parts)

    meta = dict(metadata or {})
    meta["handoff_status"] = status
    meta["next_steps"] = next_steps
    if blockers:
        meta["blockers"] = blockers
    if relevant_memory_ids:
        meta["relevant_memory_ids"] = relevant_memory_ids[:10]

    result = await store_memory_with_extraction(
        content=content,
        summary=f"Handoff: {task_summary} [{status}]",
        tags=["handoff", f"handoff:{status}"],
        category="session-log",
        source_type="agent",
        source_machine=source_machine,
        confidence=1.0,
        metadata=meta,
    )
    return result


@mcp.tool()
async def handoff_pickup(
    source_machine: str | None = None,
    limit: int = 3,
) -> list[dict]:
    """Pick up pending handoffs from previous sessions.

    Searches for recent handoff documents that haven't been resolved yet.
    Call this at session start to continue unfinished work.

    Args:
        source_machine: Filter handoffs from a specific machine.
        limit: Max handoffs to return (default 3).
    """
    embedding = await embed_text("session handoff in-progress next steps blockers")
    results = await queries.search_memories(
        embedding=embedding,
        limit=limit * 3,
        threshold=0.2,
        tags=["handoff"],
        source_machine=source_machine,
        text_query="handoff",
    )
    # Filter to non-completed handoffs and sort by recency
    handoffs = []
    for r in results:
        meta = r.get("metadata", {}) or {}
        status = meta.get("handoff_status", "")
        if status != "completed":
            handoffs.append(r)
    return handoffs[:limit]


@mcp.tool()
async def handoff_resolve(
    memory_id: str,
    resolution: str = "completed",
) -> dict:
    """Mark a handoff as resolved/completed.

    Args:
        memory_id: The memory ID of the handoff to resolve.
        resolution: Resolution status ("completed", "superseded", "abandoned").
    """
    memory_id = _validate_uuid(memory_id)
    from nobrainr.db.pool import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE memories SET
                tags = array_remove(tags, 'handoff:in_progress') || $2::text[],
                metadata = metadata || $3::jsonb
            WHERE id = $1
            """,
            UUID(memory_id),
            [f"handoff:{resolution}"],
            f'{{"handoff_status": "{resolution}"}}',
        )
    return {"status": "resolved", "memory_id": memory_id, "resolution": resolution}


# ──────────────────────────────────────────────
# Tools: advanced search (GraphRAG)
# ──────────────────────────────────────────────

@mcp.tool()
async def global_search(
    query: str,
    max_communities: int = 30,
) -> dict:
    """Answer broad questions by scanning all knowledge graph communities (GraphRAG global search).

    Uses map-reduce: scores each community's relevance to the query, then synthesizes
    an answer from the most relevant communities. Best for high-level questions that
    span multiple topics (e.g. "What are all the infrastructure components?",
    "Summarize everything about security").

    For specific factual lookups, use memory_search instead.

    Args:
        query: Broad question to answer across all knowledge domains.
        max_communities: Maximum communities to scan (default 30).
    """
    from nobrainr.services.search_enhancements import global_search as _global_search
    return await _global_search(query, max_communities=max_communities)


@mcp.tool()
async def graph_search(
    query: str,
    limit: int = 10,
    depth: int = 1,
    include_cold: bool = False,
) -> dict:
    """Entity-centric search using the knowledge graph for relationship-aware retrieval.

    Finds relevant entities via semantic search, traverses the graph to discover
    related entities (1-2 hops), then collects all linked memories. Returns both
    memories and the entity neighborhood for context.

    Best for queries about specific entities and their connections
    (e.g. "What does nobrainr depend on?", "How are Docker and Traefik related?").

    Args:
        query: Search query (entity-focused works best).
        limit: Max memories to return (default 10).
        depth: Graph traversal depth — 1 for direct neighbors, 2 for extended network.
        include_cold: Include archived (cold) memories.
    """
    from nobrainr.services.search_enhancements import graph_search as _graph_search
    return await _graph_search(query, limit=limit, depth=depth, include_cold=include_cold)


@mcp.tool()
async def fact_search(
    query: str,
    limit: int = 10,
    threshold: float = 0.3,
    date_asof: str | None = None,
) -> dict:
    """Search atomic facts extracted from memories (Mem0-style).

    Facts are short, self-contained statements distilled from longer memories.
    They are independently embedded and searchable, making them ideal for
    precise factual lookups.

    Phase K (v6.15, 2026-04-12): adds bi-temporal validity filtering.
    By default returns only CURRENTLY valid facts (the ones with
    ``valid_to IS NULL``) — superseded facts stay in the table for
    audit but don't pollute normal search results. Pass ``date_asof``
    to do a point-in-time query ("what did we believe on date X") —
    returns facts that were valid at that timestamp.

    Args:
        query: Search query for matching facts.
        limit: Maximum number of facts to return (default 10).
        threshold: Minimum similarity threshold (default 0.3).
        date_asof: ISO 8601 timestamp for a point-in-time query.
            Accepts "2026-03-01", "2026-03-01T12:00:00Z", or
            "2026-03-01T12:00:00+00:00". If None (default), returns
            only currently-valid facts.
    """
    from datetime import datetime

    parsed_asof = None
    if date_asof:
        try:
            s = date_asof.strip()
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            parsed_asof = datetime.fromisoformat(s)
        except (ValueError, AttributeError):
            parsed_asof = None  # Degrade to current-valid on garbage input

    emb = await embed_text(query)
    facts = await queries.search_facts(
        emb,
        limit=limit,
        threshold=threshold,
        text_query=query,
        date_asof=parsed_asof,
    )
    return {
        "facts": facts,
        "total": len(facts),
        "query": query,
        "date_asof": parsed_asof.isoformat() if parsed_asof else None,
    }


@mcp.tool()
async def fact_search_prioritized(
    query: str,
    limit: int = 10,
    threshold: float = 0.3,
    date_asof: str | None = None,
) -> dict:
    """Priority cascade search: check canonical facts first, then vector.

    Phase K implementation of the 3-tiered Graph RAG pattern from
    MachineLearningMastery. Canonical (tier=1) facts are checked first
    and returned immediately if found — preventing vector search from
    overriding verified ground truth.

    Use this when you need authoritative answers that must not be
    polluted by semantically-similar but potentially outdated or
    incorrect vector matches.

    Args:
        query: Search query for matching facts.
        limit: Maximum number of facts to return (default 10).
        threshold: Minimum similarity threshold (default 0.3).
        date_asof: ISO 8601 timestamp for point-in-time query.
    """
    from datetime import datetime

    parsed_asof = None
    if date_asof:
        try:
            s = date_asof.strip()
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            parsed_asof = datetime.fromisoformat(s)
        except (ValueError, AttributeError):
            parsed_asof = None

    emb = await embed_text(query)
    facts = await queries.search_facts_prioritized(
        emb,
        limit=limit,
        threshold=threshold,
        text_query=query,
        date_asof=parsed_asof,
    )

    # Identify if results came from canonical tier
    has_canonical = any(f.get("priority") == "canonical" for f in facts)

    return {
        "facts": facts,
        "total": len(facts),
        "query": query,
        "source": "canonical" if has_canonical else "vector",
        "date_asof": parsed_asof.isoformat() if parsed_asof else None,
    }


@mcp.tool()
async def fact_promote(
    fact_id: str,
    verified_by: str = "user",
) -> dict:
    """Promote a fact to canonical (tier=1) status.

    Phase K: marks a fact as verified/canonical so it takes priority
    in fact_search_prioritized queries. Once promoted, this fact will
    be returned before any vector-based results for matching queries.

    Use this when:
    - A user confirms a fact is correct
    - An authoritative source validates the information
    - You want a fact to always override similar vector matches

    Args:
        fact_id: UUID of the fact to promote.
        verified_by: Who verified this fact (user ID, agent name, etc.).

    Returns:
        Updated fact with tier=1, verified_at, verified_by set.
    """
    result = await queries.promote_fact(fact_id, verified_by=verified_by, tier=1)
    if result is None:
        return {"error": "Fact not found", "fact_id": fact_id}
    return {
        "status": "promoted",
        "fact": result,
        "message": f"Fact promoted to canonical tier by {verified_by}",
    }


@mcp.tool()
async def fact_demote(
    fact_id: str,
) -> dict:
    """Demote a fact from canonical back to derived (tier=3) status.

    Reverses a fact_promote call — the fact returns to normal vector
    search behavior and no longer overrides other results.

    Args:
        fact_id: UUID of the fact to demote.

    Returns:
        Updated fact with tier=3, verified_at/verified_by cleared.
    """
    result = await queries.demote_fact(fact_id)
    if result is None:
        return {"error": "Fact not found", "fact_id": fact_id}
    return {
        "status": "demoted",
        "fact": result,
        "message": "Fact demoted back to derived tier",
    }


# ──────────────────────────────────────────────
# Observational Memory MCP tools (Mastra-style, 2026-05-06)
# ──────────────────────────────────────────────

@mcp.tool()
async def record_observation(
    thread_id: str,
    observation: str,
    metadata: dict | None = None,
) -> dict:
    """Append a dense observation to a thread's observation log.

    Use after each chat turn to record what the user said + what was
    answered, in <=120 tokens of paraphrase. The observation should
    capture facts the user might ask about later. The Reflector job
    consolidates near-duplicates every 30min.

    Args:
        thread_id: Stable thread identifier (the chat session id).
        observation: <=120 tokens dense paraphrase. Drop pleasantries.
        metadata: Optional context (timestamps, agent_id, source url).

    Returns:
        {observation_id, thread_id}
    """
    from nobrainr.embeddings.ollama import embed_text
    body = (observation or "").strip()
    if not body:
        return {"error": "observation is empty"}
    if not thread_id:
        return {"error": "thread_id required"}
    try:
        emb = await embed_text(body[:4000])
    except Exception:
        emb = None
    obs_id = await queries.store_observation(
        thread_id=thread_id, body=body[:8000],
        embedding=emb, metadata=metadata or {},
    )
    return {"observation_id": obs_id, "thread_id": thread_id}


@mcp.tool()
async def chat_recall(
    thread_id: str,
    query: str | None = None,
    limit: int = 10,
) -> dict:
    """Recall a thread's observation log — the cache-stable chat memory.

    Returns the active (non-superseded) observations for the thread. If
    a query is given, also returns top-K observations across ALL threads
    that semantically match — useful when the user references something
    from a different conversation.

    Args:
        thread_id: The thread whose log to load.
        query: Optional semantic search across all observation logs.
        limit: Max hits per scope (default 10).

    Returns:
        {thread_log: [...], cross_thread_hits: [...]}
    """
    from nobrainr.embeddings.ollama import embed_text
    thread_log = await queries.fetch_observation_log(thread_id, limit=50)
    cross_thread_hits: list[dict] = []
    if query and len(query.strip()) > 2:
        try:
            emb = await embed_text(query.strip()[:4000])
            cross_thread_hits = await queries.search_observations(
                emb, thread_id=None, limit=limit,
            )
            cross_thread_hits = [h for h in cross_thread_hits if h["thread_id"] != thread_id]
        except Exception:
            cross_thread_hits = []
    return {
        "thread_id": thread_id,
        "thread_log": thread_log,
        "cross_thread_hits": cross_thread_hits,
    }


# ──────────────────────────────────────────────
# Entry points
# ──────────────────────────────────────────────

def main():
    """Entry point — run as parent ASGI app with dashboard + MCP."""
    import uvicorn
    from nobrainr.dashboard.app import create_app

    app = create_app()
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()


# ──────────────────────────────────────────────
# Agent comms layer (2026-08-15): presence + instant messaging.
# Postgres is the bus: fast-lane stores land in <1s (extraction deferred
# to the backfill), pg_notify pushes, msg_wait long-polls a LISTEN
# connection. Messages remain ordinary memories — searchable, trust-
# scored, digest-visible; this layer is delivery, not a second store.
# ──────────────────────────────────────────────

_AGENT_MSG_CHANNEL = "agent_msg"


@mcp.tool()
async def agent_presence(
    agent: str,
    machine: str,
    status: str = "active",
    task: str | None = None,
) -> dict:
    """Register/refresh this agent's presence and get the active roster.

    Call at session start and on long-task transitions. status: active |
    busy | idle | done. Returns every agent seen in the last 5 minutes so
    one call both announces you and tells you who else is around.

    Args:
        agent: Agent name (e.g. 'dev', 'infra', 'gis-field', or a session name).
        machine: Host the agent runs on (e.g. 'workserver', 'bimavo').
        status: active | busy | idle | done.
        task: Optional one-line description of what you're working on.
    """
    from nobrainr.db.queries import list_agent_presence, upsert_agent_presence

    await upsert_agent_presence(agent, machine, status=status, task=task)
    roster = await list_agent_presence()
    for r in roster:
        r["last_seen"] = r["last_seen"].isoformat()
    return {"registered": f"{agent}@{machine}", "active_agents": roster}


@mcp.tool()
async def agents_active(window_s: int = 300) -> list[dict]:
    """List agents active across the fleet (seen within window_s seconds).

    Args:
        window_s: Freshness window in seconds (default 300).
    """
    from nobrainr.db.queries import list_agent_presence

    roster = await list_agent_presence(active_within_s=min(window_s, 86400))
    for r in roster:
        r["last_seen"] = r["last_seen"].isoformat()
    return roster


@mcp.tool()
async def msg_send(
    to: str,
    subject: str,
    body: str,
    from_agent: str,
    machine: str | None = None,
) -> dict:
    """Send a near-instant message to another agent (or 'all').

    The message is stored as a durable agent-comm memory on the FAST LANE
    (visible in <1s; entity extraction deferred) and pushed via
    pg_notify — an agent blocked in msg_wait receives it immediately.

    Args:
        to: Target agent name, or 'all' for a fleet broadcast.
        subject: One-line subject.
        body: Message body (markdown fine).
        from_agent: Sender agent name.
        machine: Sender machine (defaults to unspecified).
    """
    import json as _json

    from nobrainr.db.pool import get_pool
    from nobrainr.services.memory import store_memory_with_extraction

    result = await store_memory_with_extraction(
        content=f"**To:** {to}\n**From:** {from_agent}\n**Subject:** {subject}\n\n{body}",
        summary=f"[{from_agent}→{to}] {subject}"[:200],
        tags=["agent-msg", f"to:{to}", f"from:{from_agent}"],
        category="agent-comm",
        source_type="agent",
        source_machine=machine or from_agent,
        confidence=1.0,
        skip_dedup=True,
        defer_extraction=True,
    )
    memory_id = result.get("memory_id") or result.get("id")
    payload = _json.dumps({
        "to": to, "from": from_agent, "subject": subject[:200],
        "body": body[:2000], "memory_id": str(memory_id),
    })
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("SELECT pg_notify($1, $2)", _AGENT_MSG_CHANNEL, payload)
    return {"sent": True, "to": to, "memory_id": str(memory_id)}


@mcp.tool()
async def msg_wait(agent: str, timeout_s: int = 45) -> dict:
    """Block until a message addressed to this agent (or 'all') arrives.

    Long-poll over a dedicated Postgres LISTEN connection — returns the
    message the moment msg_send fires, or {"timed_out": true} after
    timeout_s. LOOP this tool to wait longer: the ceiling is 50s because
    MCP clients and the MetaMCP proxy kill calls around 60s — a 120s wait
    died as a raw client timeout instead of returning cleanly
    (live-tested 2026-08-15). Fifty-second turns, called in a loop, wait
    forever without ever tripping a proxy.

    Args:
        agent: This agent's name (matches msg_send's `to`, plus 'all').
        timeout_s: Max seconds to wait (1-50, default 45).
    """
    import asyncio
    import json as _json

    import asyncpg

    timeout_s = max(1, min(timeout_s, 50))
    queue: asyncio.Queue = asyncio.Queue()

    def _on_notify(_conn, _pid, _channel, payload: str) -> None:
        try:
            msg = _json.loads(payload)
        except Exception:
            return
        if msg.get("to") in (agent, "all"):
            queue.put_nowait(msg)

    # Dedicated connection (NOT from the pool): LISTEN holds it for the
    # whole wait and must never starve the app pool.
    conn = await asyncpg.connect(settings.database_url)
    try:
        await conn.add_listener(_AGENT_MSG_CHANNEL, _on_notify)
        try:
            msg = await asyncio.wait_for(queue.get(), timeout=timeout_s)
            return {"message": msg, "timed_out": False}
        except asyncio.TimeoutError:
            return {"timed_out": True, "waited_s": timeout_s}
    finally:
        try:
            await conn.remove_listener(_AGENT_MSG_CHANNEL, _on_notify)
        except Exception:
            pass
        await conn.close()
