"""Scheduled knowledge crawler — safe, rate-limited documentation fetcher.

Crawls curated documentation URLs, stores as memories with entity extraction.
Designed to be polite: long delays, dedup, one domain at a time.
"""

import asyncio
import logging
from datetime import datetime

import httpx

from nobrainr.config import settings
from nobrainr.db import queries
from nobrainr.db.pool import get_pool
from nobrainr.embeddings.ollama import embed_text

logger = logging.getLogger("nobrainr")

# Curated seed URLs — safe public documentation.
# Each entry: (url, tags, category)
# Add more over time via the crawl_queue MCP tool or by editing this list.
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

MAX_CONTENT_CHARS = 8000  # Don't store huge pages


async def _crawl_url(url: str) -> dict | None:
    """Fetch a URL via Crawl4AI and return markdown content."""
    headers = {"Content-Type": "application/json"}
    if settings.crawl4ai_api_token:
        headers["Authorization"] = f"Bearer {settings.crawl4ai_api_token}"

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                f"{settings.crawl4ai_url}/crawl",
                json={
                    "urls": [url],
                    "cache_mode": "bypass",
                    "word_count_threshold": 50,
                },
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        logger.warning("Knowledge crawl failed for %s: %s", url, e)
        return None

    if not data.get("success") or not data.get("results"):
        return None

    result = data["results"][0]
    if not result.get("success"):
        return None

    md = result.get("markdown", {})
    markdown = md.get("raw_markdown", "") if isinstance(md, dict) else str(md)
    title = result.get("metadata", {}).get("title", url)
    status = result.get("status_code", 0)

    if status >= 400:
        logger.warning("Knowledge crawl got HTTP %d for %s", status, url)
        return None

    return {"markdown": markdown, "title": title, "status_code": status}


async def _is_already_crawled(url: str) -> bool:
    """Check if this URL was already stored as a memory."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchval(
            "SELECT 1 FROM memories WHERE source_ref = $1 AND category <> '_archived' LIMIT 1",
            url,
        )
        return row is not None


async def knowledge_crawl() -> dict:
    """Crawl a batch of documentation URLs and store as memories.

    Picks uncrawled URLs from the seed list + any queued URLs,
    respects rate limits, and stores results with entity extraction.
    """
    # Get queued URLs from DB (added via crawl_queue tool)
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

    # Fill remaining batch slots from seed list
    remaining = settings.knowledge_crawl_batch_size - len(queued)
    seed_candidates = []
    if remaining > 0:
        for url, tags, category in SEED_URLS:
            if not await _is_already_crawled(url):
                seed_candidates.append((url, tags, category))
            if len(seed_candidates) >= remaining:
                break

    to_crawl = queued + seed_candidates

    if not to_crawl:
        return {"crawled": 0, "stored": 0, "message": "nothing to crawl", "ran_at": datetime.now().isoformat()}

    crawled = 0
    stored = 0

    for url, tags, category in to_crawl:
        # Skip if already stored (race condition protection)
        if await _is_already_crawled(url):
            await _mark_queued_crawled(url)
            continue

        result = await _crawl_url(url)
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

        # Store as memory
        try:
            all_tags = list(tags) + ["crawled", "knowledge-base"]
            embedding = await embed_text(markdown[:6000])
            await queries.store_memory(
                content=markdown,
                embedding=embedding,
                summary=f"Crawled: {result['title']}"[:200],
                source_type="crawl",
                source_machine=settings.source_machine or "unknown",
                source_ref=url,
                tags=all_tags,
                category=category,
                confidence=0.8,
            )
            stored += 1
            logger.info("Knowledge crawl stored: %s (%d chars)", result["title"], len(markdown))
        except Exception:
            logger.exception("Knowledge crawl store failed for %s", url)

        crawled += 1
        await _mark_queued_crawled(url)

        # Be polite — wait between requests
        await asyncio.sleep(settings.knowledge_crawl_delay)

    return {
        "crawled": crawled,
        "stored": stored,
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
    """Create the crawl_queue table if it doesn't exist."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS crawl_queue (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                url TEXT NOT NULL,
                tags TEXT[] DEFAULT '{}',
                category TEXT DEFAULT 'documentation',
                queued_at TIMESTAMPTZ DEFAULT NOW(),
                crawled_at TIMESTAMPTZ,
                error TEXT,
                UNIQUE(url)
            )
        """)
