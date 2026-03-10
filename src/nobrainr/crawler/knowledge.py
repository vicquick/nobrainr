"""Scheduled knowledge crawler — safe, rate-limited documentation fetcher.

Crawls curated documentation URLs, stores as memories with entity extraction.
Designed to be polite: long delays, dedup, one domain at a time.

Phases:
  - Crawl seed URLs + queued URLs from crawl_queue table
  - Discover links from crawled pages and queue interesting ones (link discovery)
  - Re-crawl stale pages that have changed (freshness)
  - Saturation detection: skip domains where recent crawls yield mostly duplicates
"""

import asyncio
import logging
from datetime import datetime
from urllib.parse import urlparse

from nobrainr.config import settings
from nobrainr.crawler.client import crawl4ai_job, crawl4ai_request
from nobrainr.db import queries
from nobrainr.db.pool import get_pool
from nobrainr.services.memory import store_document_chunked

logger = logging.getLogger("nobrainr")

# Saturation detection thresholds
SATURATION_WINDOW_DAYS = 7       # Look at crawls from the last 7 days
SATURATION_MIN_CRAWLS = 3        # Need at least 3 crawls to judge saturation
SATURATION_NOVELTY_THRESHOLD = 0.2  # If <20% of recent crawls yielded new content, domain is saturated
SATURATION_COOLDOWN_HOURS = 48   # Skip saturated domains for 48 hours

# Curated seed URLs — safe public documentation.
# Each entry: (url, tags, category)
SEED_URLS = [
    # Python ecosystem
    ("https://docs.python.org/3/whatsnew/3.13.html", ["python", "changelog"], "documentation"),
    ("https://docs.python.org/3/library/asyncio.html", ["python", "asyncio"], "documentation"),
    ("https://docs.python.org/3/library/typing.html", ["python", "typing"], "documentation"),
    ("https://docs.astral.sh/uv/", ["uv", "python", "package-manager"], "tooling"),
    ("https://docs.astral.sh/ruff/", ["ruff", "python", "linter"], "tooling"),
    ("https://docs.pydantic.dev/latest/", ["pydantic", "python", "validation"], "documentation"),
    ("https://fastapi.tiangolo.com/", ["fastapi", "python", "api"], "documentation"),
    # Database
    ("https://www.postgresql.org/docs/current/release-18.html", ["postgresql", "changelog"], "documentation"),
    ("https://github.com/pgvector/pgvector", ["pgvector", "postgresql", "embeddings"], "documentation"),
    # Frontend
    ("https://vuejs.org/guide/introduction.html", ["vue", "frontend", "javascript"], "documentation"),
    ("https://vuetifyjs.com/en/getting-started/installation/", ["vuetify", "vue", "ui"], "documentation"),
    ("https://vite.dev/guide/", ["vite", "frontend", "build-tool"], "documentation"),
    # Infrastructure
    ("https://docs.docker.com/engine/release-notes/", ["docker", "changelog"], "infrastructure"),
    ("https://docs.docker.com/compose/", ["docker", "compose"], "infrastructure"),
    ("https://doc.traefik.io/traefik/", ["traefik", "reverse-proxy"], "infrastructure"),
    ("https://coolify.io/docs", ["coolify", "paas", "deployment"], "infrastructure"),
    # AI/ML
    ("https://github.com/ollama/ollama/blob/main/docs/api.md", ["ollama", "api", "llm"], "documentation"),
    ("https://docs.anthropic.com/en/docs/overview", ["claude", "api", "anthropic"], "documentation"),
    ("https://modelcontextprotocol.io/introduction", ["mcp", "protocol", "ai"], "documentation"),
    # BIM/Construction (domain-specific)
    ("https://technical.buildingsmart.org/standards/ifc/", ["ifc", "bim", "buildingsmart"], "documentation"),
    ("https://www.buildingsmart.org/standards/bsi-standards/industry-foundation-classes/", ["ifc", "bim", "standards"], "business"),
]

MAX_CONTENT_CHARS = 50000  # Cap at 50k chars, chunking handles the rest

# Domains we trust for automatic link discovery (avoid crawling the entire web)
TRUSTED_DOMAINS = {
    "docs.python.org", "docs.astral.sh", "docs.pydantic.dev", "fastapi.tiangolo.com",
    "www.postgresql.org", "vuejs.org", "vuetifyjs.com", "vite.dev",
    "docs.docker.com", "doc.traefik.io", "coolify.io",
    "docs.anthropic.com", "modelcontextprotocol.io",
    "technical.buildingsmart.org", "www.buildingsmart.org",
    "github.com", "developer.mozilla.org",
}

# Path patterns that indicate documentation (for link scoring)
DOC_PATH_PATTERNS = ("/docs/", "/guide/", "/tutorial/", "/api/", "/reference/", "/manual/", "/learn/")


async def _crawl_url(
    url: str,
    *,
    extract_links: bool = False,
    use_async_job: bool = False,
    query: str | None = None,
) -> dict | None:
    """Fetch a URL via Crawl4AI and return markdown content + optional links.

    Args:
        url: URL to crawl.
        extract_links: Include discovered links in the output.
        use_async_job: Use the async /crawl/job API (better for slow pages).
        query: If set, use BM25 query-aware content filtering instead of pruning.
    """
    from nobrainr.crawler.client import bm25_markdown_generator

    crawler_config: dict = {"word_count_threshold": 50}

    # Use BM25 for query-aware filtering, otherwise PruningContentFilter is applied
    # automatically by the shared client
    if query:
        crawler_config["markdown_generator"] = bm25_markdown_generator(query)

    try:
        if use_async_job:
            data = await crawl4ai_job(url, crawler_config=crawler_config)
        else:
            data = await crawl4ai_request(url, crawler_config=crawler_config)
    except Exception as e:
        logger.warning("Knowledge crawl failed for %s: %s", url, e)
        return None

    if "error" in data:
        logger.warning("Knowledge crawl error for %s: %s", url, data["error"])
        return None

    # Extract result — async job wraps in data['result']['results'], sync in data['results']
    results = data.get("results") or data.get("result", {}).get("results")
    if not results:
        return None

    result = results[0]
    if not result.get("success"):
        return None

    md = result.get("markdown", {})
    markdown = md.get("fit_markdown") or md.get("raw_markdown", "") if isinstance(md, dict) else str(md)
    title = result.get("metadata", {}).get("title", url)
    status = result.get("status_code", 0)

    if status >= 400:
        logger.warning("Knowledge crawl got HTTP %d for %s", status, url)
        return None

    output = {"markdown": markdown, "title": title, "status_code": status}

    if extract_links:
        links_data = result.get("links", {})
        internal = [link.get("href") for link in links_data.get("internal", []) if link.get("href")]
        external = [link.get("href") for link in links_data.get("external", []) if link.get("href")]
        output["links"] = internal + external

    return output


async def _is_already_crawled(url: str) -> bool:
    """Check if this URL was already stored as a memory."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchval(
            "SELECT 1 FROM memories WHERE source_ref = $1 AND category <> '_archived' LIMIT 1",
            url,
        )
        return row is not None


async def _is_queued_or_crawled(url: str) -> bool:
    """Check if URL is already in crawl_queue or stored as memory."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        in_queue = await conn.fetchval(
            "SELECT 1 FROM crawl_queue WHERE url = $1 LIMIT 1", url,
        )
        if in_queue:
            return True
        return await _is_already_crawled(url)


def _score_link(url: str, parent_tags: list[str]) -> float:
    """Score a discovered link for crawl priority (0.0 to 1.0)."""
    parsed = urlparse(url)
    domain = parsed.netloc.lower()
    path = parsed.path.lower()
    score = 0.0

    # Trusted domain bonus
    if domain in TRUSTED_DOMAINS:
        score += 0.4
    else:
        return 0.0  # Only queue links from trusted domains

    # Documentation path patterns
    if any(p in path for p in DOC_PATH_PATTERNS):
        score += 0.3

    # Avoid non-content paths
    skip_patterns = ("/search", "/login", "/signup", "/pricing", "/blog/", "/news/", "#")
    if any(p in path for p in skip_patterns):
        return 0.0

    # Avoid file downloads
    if path.endswith((".pdf", ".zip", ".tar.gz", ".png", ".jpg", ".svg")):
        return 0.0

    # Depth penalty (deeper paths = less likely to be overview docs)
    depth = len([p for p in path.split("/") if p])
    if depth <= 3:
        score += 0.2
    elif depth <= 5:
        score += 0.1

    # Same domain as seed URLs gets a small bonus
    seed_domains = {urlparse(u).netloc for u, _, _ in SEED_URLS}
    if domain in seed_domains:
        score += 0.1

    return min(score, 1.0)


async def _queue_discovered_links(
    links: list[str],
    parent_url: str,
    parent_tags: list[str],
    parent_category: str,
) -> int:
    """Score and queue interesting links discovered during crawling."""
    if not settings.link_discovery_enabled:
        return 0

    scored = []
    for link in links:
        # Normalize: strip fragments
        link = link.split("#")[0].rstrip("/")
        if not link or not link.startswith("http"):
            continue
        score = _score_link(link, parent_tags)
        if score >= settings.link_discovery_min_score:
            scored.append((link, score))

    # Sort by score, take top N
    scored.sort(key=lambda x: x[1], reverse=True)
    top_links = scored[: settings.link_discovery_max_per_page]

    queued = 0
    pool = await get_pool()
    for link, score in top_links:
        if await _is_queued_or_crawled(link):
            continue
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO crawl_queue (url, tags, category, metadata)
                    VALUES ($1, $2, $3, $4::jsonb)
                    ON CONFLICT (url) DO NOTHING
                    """,
                    link,
                    parent_tags,
                    parent_category,
                    f'{{"parent_url": "{parent_url}", "score": {score:.2f}}}',
                )
            queued += 1
            logger.info("Queued discovered link (score=%.2f): %s", score, link)
        except Exception:
            logger.warning("Failed to queue link: %s", link)

    return queued


async def _record_crawl_outcome(url: str, *, novel: bool) -> None:
    """Record whether a crawl yielded novel (non-duplicate) content."""
    domain = urlparse(url).netloc.lower()
    try:
        await queries.log_agent_event(
            event_type="crawl_outcome",
            description=f"{'novel' if novel else 'duplicate'}: {url}",
            metadata={"domain": domain, "url": url, "novel": novel},
        )
    except Exception:
        pass


async def _is_domain_saturated(url: str) -> bool:
    """Check if the domain of this URL is saturated (too many recent duplicates)."""
    domain = urlparse(url).netloc.lower()
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT metadata->>'novel' AS novel
            FROM agent_events
            WHERE event_type = 'crawl_outcome'
              AND metadata->>'domain' = $1
              AND created_at > NOW() - INTERVAL '1 day' * $2
            ORDER BY created_at DESC
            LIMIT 20
            """,
            domain, SATURATION_WINDOW_DAYS,
        )
        if len(rows) < SATURATION_MIN_CRAWLS:
            return False
        novel_count = sum(1 for r in rows if r["novel"] == "true")
        novelty_rate = novel_count / len(rows)
        if novelty_rate < SATURATION_NOVELTY_THRESHOLD:
            logger.info(
                "Domain %s is saturated (%.0f%% novelty from %d recent crawls), skipping",
                domain, novelty_rate * 100, len(rows),
            )
            return True
        return False


async def knowledge_crawl() -> dict:
    """Crawl a batch of documentation URLs and store as memories.

    Picks uncrawled URLs from the seed list + any queued URLs,
    respects rate limits, and stores results with entity extraction.
    Includes saturation detection: skips domains where recent crawls
    are mostly duplicates.
    """
    # Get queued URLs from DB (added via crawl_queue tool or link discovery)
    pool = await get_pool()
    async with pool.acquire() as conn:
        queued_rows = await conn.fetch(
            """
            SELECT url, tags, category FROM crawl_queue
            WHERE crawled_at IS NULL
            ORDER BY queued_at ASC
            LIMIT $1
            """,
            settings.knowledge_crawl_batch_size,
        )
    queued = [(r["url"], r["tags"] or [], r["category"] or "documentation") for r in queued_rows]

    # Fill remaining batch slots from seed list + sitemap discovery
    remaining = settings.knowledge_crawl_batch_size - len(queued)
    seed_candidates = []
    if remaining > 0:
        for url, tags, category in SEED_URLS:
            if not await _is_already_crawled(url):
                seed_candidates.append((url, tags, category))
            if len(seed_candidates) >= remaining:
                break

    # If seed list is exhausted, try sitemap discovery from seed domains
    if len(seed_candidates) < remaining and not seed_candidates:
        try:
            from nobrainr.crawler.client import discover_sitemap_urls

            seed_domains = {urlparse(u).scheme + "://" + urlparse(u).netloc for u, _, _ in SEED_URLS}
            for base_url in list(seed_domains)[:3]:  # Check up to 3 domains
                sm_urls = await discover_sitemap_urls(base_url, max_urls=20)
                for sm_url in sm_urls:
                    if not await _is_already_crawled(sm_url):
                        domain = urlparse(sm_url).netloc
                        seed_candidates.append((sm_url, ["crawled", "sitemap-discovered", domain], "documentation"))
                    if len(seed_candidates) >= remaining:
                        break
                if len(seed_candidates) >= remaining:
                    break
        except Exception:
            logger.debug("Sitemap discovery failed, using seed list only")

    to_crawl = queued + seed_candidates

    if not to_crawl:
        return {"crawled": 0, "stored": 0, "links_queued": 0, "message": "nothing to crawl", "ran_at": datetime.now().isoformat()}

    crawled = 0
    stored = 0
    links_queued = 0
    skipped_saturated = 0

    for url, tags, category in to_crawl:
        # Skip if already stored (race condition protection)
        if await _is_already_crawled(url):
            await _mark_queued_crawled(url)
            continue

        # Saturation detection: skip domains that are yielding mostly duplicates
        if await _is_domain_saturated(url):
            skipped_saturated += 1
            await _mark_queued_crawled(url, error="domain_saturated")
            continue

        # Crawl with link extraction enabled; use async job API for scheduler (avoids HTTP timeouts)
        result = await _crawl_url(url, extract_links=settings.link_discovery_enabled, use_async_job=True)
        if not result:
            crawled += 1
            await _mark_queued_crawled(url, error="crawl_failed")
            await asyncio.sleep(settings.knowledge_crawl_delay)
            continue

        markdown = result["markdown"][:MAX_CONTENT_CHARS]
        if len(markdown.strip()) < 100:
            crawled += 1
            await _mark_queued_crawled(url, error="too_short")
            await asyncio.sleep(settings.knowledge_crawl_delay)
            continue

        # Store via chunked ingestion (handles embedding + overlap + entity extraction)
        novel = False
        try:
            all_tags = list(tags) + ["crawled", "knowledge-base"]
            store_result = await store_document_chunked(
                content=markdown,
                title=result.get("title", url),
                summary=f"Crawled: {result['title']}"[:200],
                source_type="crawl",
                source_machine=settings.source_machine or "unknown",
                source_ref=url,
                tags=all_tags,
                category=category,
                confidence=0.8,
            )
            if store_result.get("status") in ("stored", "updated"):
                stored += store_result.get("chunks", 1)
                novel = True
            logger.info("Knowledge crawl stored: %s (%d chars, %d chunks)", result["title"], len(markdown), store_result.get("chunks", 1))
        except Exception:
            logger.exception("Knowledge crawl store failed for %s", url)

        # Record crawl outcome for saturation tracking
        await _record_crawl_outcome(url, novel=novel)

        # Queue discovered links
        if result.get("links"):
            lq = await _queue_discovered_links(result["links"], url, tags, category)
            links_queued += lq

        crawled += 1
        await _mark_queued_crawled(url)
        await asyncio.sleep(settings.knowledge_crawl_delay)

    return {
        "crawled": crawled,
        "stored": stored,
        "links_queued": links_queued,
        "skipped_saturated": skipped_saturated,
        "ran_at": datetime.now().isoformat(),
    }


async def get_stale_crawled_memories(limit: int = 5) -> list[dict]:
    """Find crawled memories that are old enough to re-crawl."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT m.id, m.source_ref, m.content, m.tags, m.category, m.created_at,
                   m.access_count, m.importance
            FROM memories m
            WHERE m.source_type = 'crawl'
              AND m.source_ref IS NOT NULL
              AND m.category <> '_archived'
              AND m.created_at < NOW() - INTERVAL '1 day' * $1
            ORDER BY m.access_count DESC, m.importance DESC
            LIMIT $2
            """,
            settings.freshness_max_age_days,
            limit,
        )
        return [dict(r) for r in rows]


async def freshness_recrawl() -> dict:
    """Re-crawl stale memories and update if content changed significantly."""
    stale = await get_stale_crawled_memories(settings.freshness_batch_size)
    if not stale:
        return {"recrawled": 0, "updated": 0, "gone": 0, "ran_at": datetime.now().isoformat()}

    recrawled = 0
    updated = 0
    gone = 0

    for mem in stale:
        url = mem["source_ref"]
        result = await _crawl_url(url, use_async_job=True)
        recrawled += 1

        if not result:
            # Page might be gone or temporarily down — mark it
            logger.warning("Freshness: page may be gone: %s", url)
            gone += 1
            await asyncio.sleep(settings.knowledge_crawl_delay)
            continue

        new_markdown = result["markdown"][:MAX_CONTENT_CHARS]
        old_content = mem["content"]

        # Simple diff check: significant change = >20% length difference or <80% overlap
        len_ratio = len(new_markdown) / max(len(old_content), 1)
        if 0.8 <= len_ratio <= 1.2 and new_markdown[:500] == old_content[:500]:
            # Content hasn't changed meaningfully
            await asyncio.sleep(settings.knowledge_crawl_delay)
            continue

        # Content changed — update the memory
        try:
            from nobrainr.embeddings.ollama import embed_text

            embedding = await embed_text(new_markdown)
            await queries.update_memory(
                str(mem["id"]),
                content=new_markdown,
                embedding=embedding,
                summary=f"Crawled: {result['title']} (refreshed)"[:200],
            )
            # Re-trigger entity extraction
            from nobrainr.extraction.pipeline import process_memory

            asyncio.create_task(process_memory(str(mem["id"]), new_markdown, mem.get("tags")))
            updated += 1
            logger.info("Freshness: updated %s (%s)", url, result["title"])
        except Exception:
            logger.exception("Freshness: failed to update %s", url)

        await asyncio.sleep(settings.knowledge_crawl_delay)

    return {
        "recrawled": recrawled,
        "updated": updated,
        "gone": gone,
        "ran_at": datetime.now().isoformat(),
    }


async def _mark_queued_crawled(url: str, *, error: str | None = None) -> None:
    """Mark a queued URL as crawled."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE crawl_queue SET crawled_at = NOW(), error = $2
            WHERE url = $1 AND crawled_at IS NULL
            """,
            url, error,
        )


async def ensure_crawl_queue_table() -> None:
    """Create the crawl_queue table if it doesn't exist (with metadata column)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS crawl_queue (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                url TEXT NOT NULL,
                tags TEXT[] DEFAULT '{}',
                category TEXT DEFAULT 'documentation',
                metadata JSONB DEFAULT '{}',
                queued_at TIMESTAMPTZ DEFAULT NOW(),
                crawled_at TIMESTAMPTZ,
                error TEXT,
                UNIQUE(url)
            )
        """)
        # Add metadata column if missing (existing tables from before)
        await conn.execute("""
            DO $$ BEGIN
                ALTER TABLE crawl_queue ADD COLUMN IF NOT EXISTS metadata JSONB DEFAULT '{}';
            EXCEPTION WHEN others THEN NULL;
            END $$;
        """)
