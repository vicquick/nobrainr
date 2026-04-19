"""nobrainr MCP server — collective agent memory with knowledge graph."""

import logging
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
        "2. Use `memory_store` to save learnings, decisions, patterns, and context. Writes are "
        "queued — the tool returns in <50ms with a `queue_id`, and a background worker processes "
        "the full pipeline (embedding, dedup, entity extraction). You usually don't need to wait.\n"
        "3. Call `memory_feedback` after using search results — report if they were helpful. Pass "
        "through `search_trace_id`, `search_rank`, and `search_query` from the original result so "
        "we can compute rank-aware metrics (MRR/NDCG).\n"
        "4. Call `memory_reflect` at session end with a batch of learnings from the session.\n"
        "5. Use `log_event` to record significant agent activity (session starts, decisions, completions).\n\n"
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
        "tooling, security, frontend, backend, data, business, documentation, session-log, insight.\n"
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

    # Rule 1 — long or multi-clause → decompose into sub-queries
    if word_count >= 12 or q.count(",") >= 2 or q.count(" and ") >= 2:
        return {"hybrid": True, "decompose": True}

    # Rule 2 — why/how/when questions with 5+ words → HyDE
    question_prefixes = (
        "why ", "how ",
        "what if ", "when did ", "when do ", "when was ",
    )
    if any(q.startswith(p) for p in question_prefixes) and word_count >= 5:
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
    auto_route: bool = False,
    include_related: bool = False,
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
        auto_route: When True, analyze the query shape and automatically pick
            the best retrieval strategy (hybrid / hyde / decompose / expand) —
            agents don't have to choose. Uses a lightweight heuristic based
            on query length, comma/and count, and question prefix. Zero
            added latency. When True, the selected flags OVERRIDE whatever
            was passed explicitly for hybrid/expand/hyde/decompose (Phase
            B G2, v6.7).
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

    # Auto-routing query planner — Phase B G2 (v6.7). When enabled, pick the
    # best retrieval strategy for this query shape. Overrides any explicit
    # hybrid/expand/hyde/decompose flags. See _auto_route_query for rules.
    if auto_route:
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

    # Rerank with cross-encoder if enabled. Skip if we've already burned
    # most of the budget on earlier stages — returning an RRF-sorted
    # result at tier C is strictly better than a timeout at tier E.
    if (
        settings.reranker_enabled
        and len(results) > 1
        and not _over(settings.search_rerank_budget_frac)
    ):
        try:
            from nobrainr.services.reranker import rerank
            results = await rerank(query, results, limit=limit)
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

    return results


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

    all_tags = list(tags or []) + ["crawled"]
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
