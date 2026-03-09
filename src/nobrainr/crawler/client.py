"""Shared Crawl4AI HTTP client — used by MCP tools, scheduler, and crawler.

Provides a single function to call the Crawl4AI /crawl endpoint with proper
crawler_config format, content filtering defaults, and auth.
"""

import asyncio
import logging

import httpx

from nobrainr.config import settings

logger = logging.getLogger("nobrainr")

# Default content filter: PruningContentFilter strips boilerplate (nav, sidebars, ads)
DEFAULT_MARKDOWN_GENERATOR = {
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


def _auth_headers() -> dict:
    headers = {"Content-Type": "application/json"}
    if settings.crawl4ai_api_token:
        headers["Authorization"] = f"Bearer {settings.crawl4ai_api_token}"
    return headers


async def crawl4ai_request(
    url: str,
    *,
    crawler_config: dict | None = None,
    timeout: float = 120.0,
) -> dict:
    """Send a crawl request to Crawl4AI and return parsed results.

    Args:
        url: URL to crawl.
        crawler_config: Override crawler_config dict. If markdown_generator is not
            set, the default PruningContentFilter is applied automatically.
        timeout: HTTP timeout in seconds.

    Returns:
        On success: {"success": True, "results": [...]}
        On failure: {"error": "...", "url": "..."}
    """
    config = dict(crawler_config or {})
    config.setdefault("cache_mode", "bypass")
    config.setdefault("word_count_threshold", 20)
    config.setdefault("exclude_social_media_links", True)
    config.setdefault("remove_overlay_elements", True)

    # Apply default content filter if none specified
    if "markdown_generator" not in config:
        config["markdown_generator"] = DEFAULT_MARKDOWN_GENERATOR

    payload = {"urls": [url], "crawler_config": config}

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{settings.crawl4ai_url}/crawl",
                json=payload,
                headers=_auth_headers(),
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        return {"error": f"Crawl failed: {e}", "url": url}

    if not data.get("success") or not data.get("results"):
        return {"error": "Crawl returned no results", "url": url, "raw": str(data)[:500]}

    return data


async def crawl4ai_job(
    url: str,
    *,
    crawler_config: dict | None = None,
    poll_interval: float = 2.0,
    max_wait: float = 300.0,
) -> dict:
    """Submit an async crawl job and poll until completion.

    Uses POST /crawl/job + GET /crawl/job/{task_id} to avoid HTTP timeouts
    on slow pages. Recommended for scheduler jobs.

    Returns same format as crawl4ai_request.
    """
    config = dict(crawler_config or {})
    config.setdefault("cache_mode", "bypass")
    config.setdefault("word_count_threshold", 20)
    config.setdefault("exclude_social_media_links", True)
    config.setdefault("remove_overlay_elements", True)
    if "markdown_generator" not in config:
        config["markdown_generator"] = DEFAULT_MARKDOWN_GENERATOR

    payload = {"urls": [url], "crawler_config": config}

    try:
        async with httpx.AsyncClient(timeout=max_wait + 30) as client:
            # Submit job
            resp = await client.post(
                f"{settings.crawl4ai_url}/crawl/job",
                json=payload,
                headers=_auth_headers(),
            )
            resp.raise_for_status()
            job = resp.json()
            task_id = job.get("task_id")
            if not task_id:
                return {"error": "No task_id returned from /crawl/job", "url": url}

            # Poll for completion
            elapsed = 0.0
            while elapsed < max_wait:
                await asyncio.sleep(poll_interval)
                elapsed += poll_interval

                status_resp = await client.get(
                    f"{settings.crawl4ai_url}/crawl/job/{task_id}",
                    headers=_auth_headers(),
                )
                status_resp.raise_for_status()
                status_data = status_resp.json()

                if status_data.get("status") == "completed":
                    result = status_data.get("result", {})
                    if result.get("success") and result.get("results"):
                        return result
                    return {"error": "Job completed but no results", "url": url}

                if status_data.get("status") == "failed":
                    return {"error": f"Crawl job failed: {status_data}", "url": url}

            return {"error": f"Crawl job timed out after {max_wait}s", "url": url}

    except Exception as e:
        return {"error": f"Crawl job failed: {e}", "url": url}


def bm25_markdown_generator(query: str, threshold: float = 1.0) -> dict:
    """Build a markdown_generator config with BM25 query-aware filtering."""
    return {
        "type": "DefaultMarkdownGenerator",
        "params": {
            "content_filter": {
                "type": "BM25ContentFilter",
                "params": {"user_query": query, "bm25_threshold": threshold},
            }
        },
    }


async def discover_sitemap_urls(base_url: str, *, max_urls: int = 100) -> list[str]:
    """Discover page URLs from a site's sitemap.xml or robots.txt.

    Tries /sitemap.xml first, falls back to parsing sitemap directives from
    /robots.txt. Returns up to max_urls unique page URLs.
    """
    from xml.etree import ElementTree

    urls: list[str] = []
    base = base_url.rstrip("/")

    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        # Try /sitemap.xml
        sitemap_locations = [f"{base}/sitemap.xml"]

        # Also check robots.txt for Sitemap directives
        try:
            resp = await client.get(f"{base}/robots.txt", headers=_auth_headers())
            if resp.status_code == 200:
                for line in resp.text.splitlines():
                    line = line.strip()
                    if line.lower().startswith("sitemap:"):
                        sm_url = line.split(":", 1)[1].strip()
                        if sm_url and sm_url not in sitemap_locations:
                            sitemap_locations.append(sm_url)
        except Exception:
            pass

        for sm_url in sitemap_locations:
            try:
                resp = await client.get(sm_url, headers=_auth_headers())
                if resp.status_code != 200:
                    continue
                root = ElementTree.fromstring(resp.text)
                ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

                # Handle sitemap index (contains other sitemaps)
                for sitemap_el in root.findall(".//sm:sitemap/sm:loc", ns):
                    if sitemap_el.text and len(urls) < max_urls:
                        try:
                            sub_resp = await client.get(sitemap_el.text.strip(), headers=_auth_headers())
                            if sub_resp.status_code == 200:
                                sub_root = ElementTree.fromstring(sub_resp.text)
                                for loc in sub_root.findall(".//sm:url/sm:loc", ns):
                                    if loc.text and loc.text.strip() not in urls:
                                        urls.append(loc.text.strip())
                                    if len(urls) >= max_urls:
                                        break
                        except Exception:
                            continue

                # Handle regular sitemap
                for loc in root.findall(".//sm:url/sm:loc", ns):
                    if loc.text and loc.text.strip() not in urls:
                        urls.append(loc.text.strip())
                    if len(urls) >= max_urls:
                        break

            except Exception:
                continue

    return urls[:max_urls]


async def crawl4ai_deep(
    start_url: str,
    *,
    strategy: str = "bfs",
    max_pages: int = 10,
    max_depth: int = 3,
    crawler_config: dict | None = None,
    include_patterns: list[str] | None = None,
    exclude_patterns: list[str] | None = None,
    poll_interval: float = 3.0,
    max_wait: float = 600.0,
) -> dict:
    """Run a deep crawl starting from a URL.

    Uses Crawl4AI's deep crawl via the async job API with BFS/DFS strategy.
    Returns all crawled pages with their markdown content.

    Args:
        start_url: Starting URL for the crawl.
        strategy: Crawl strategy — "bfs" (breadth-first) or "dfs" (depth-first).
        max_pages: Maximum pages to crawl (default 10).
        max_depth: Maximum link depth from start URL (default 3).
        crawler_config: Additional crawler config overrides.
        include_patterns: URL regex patterns to include (e.g. ["/docs/.*"]).
        exclude_patterns: URL regex patterns to exclude.
        poll_interval: Seconds between status polls.
        max_wait: Maximum wait time in seconds.

    Returns:
        On success: {"success": True, "pages": [...], "total_pages": N}
        On failure: {"error": "..."}
    """
    config = dict(crawler_config or {})
    config.setdefault("cache_mode", "bypass")
    config.setdefault("word_count_threshold", 20)
    config.setdefault("exclude_social_media_links", True)
    config.setdefault("remove_overlay_elements", True)
    if "markdown_generator" not in config:
        config["markdown_generator"] = DEFAULT_MARKDOWN_GENERATOR

    # Deep crawl config
    deep_crawl_config = {
        "type": strategy.upper(),
        "params": {
            "max_depth": max_depth,
            "max_pages": max_pages,
        },
    }
    if include_patterns:
        deep_crawl_config["params"]["include_patterns"] = include_patterns
    if exclude_patterns:
        deep_crawl_config["params"]["exclude_patterns"] = exclude_patterns

    payload = {
        "urls": [start_url],
        "crawler_config": config,
        "deep_crawl_config": deep_crawl_config,
    }

    try:
        async with httpx.AsyncClient(timeout=max_wait + 30) as client:
            # Submit deep crawl job
            resp = await client.post(
                f"{settings.crawl4ai_url}/crawl/job",
                json=payload,
                headers=_auth_headers(),
            )
            resp.raise_for_status()
            job = resp.json()
            task_id = job.get("task_id")
            if not task_id:
                return {"error": "No task_id returned from /crawl/job"}

            # Poll for completion
            elapsed = 0.0
            while elapsed < max_wait:
                await asyncio.sleep(poll_interval)
                elapsed += poll_interval

                status_resp = await client.get(
                    f"{settings.crawl4ai_url}/crawl/job/{task_id}",
                    headers=_auth_headers(),
                )
                status_resp.raise_for_status()
                status_data = status_resp.json()

                if status_data.get("status") == "completed":
                    result = status_data.get("result", {})
                    results = result.get("results") or []
                    pages = []
                    for r in results:
                        if not r.get("success"):
                            continue
                        md = r.get("markdown", {})
                        markdown = md.get("fit_markdown") or md.get("raw_markdown", "") if isinstance(md, dict) else str(md)
                        pages.append({
                            "url": r.get("url", ""),
                            "title": r.get("metadata", {}).get("title", ""),
                            "markdown": markdown,
                            "status_code": r.get("status_code"),
                            "links": r.get("links", {}),
                        })
                    return {"success": True, "pages": pages, "total_pages": len(pages)}

                if status_data.get("status") == "failed":
                    return {"error": f"Deep crawl failed: {status_data}"}

            return {"error": f"Deep crawl timed out after {max_wait}s"}

    except Exception as e:
        return {"error": f"Deep crawl failed: {e}"}
