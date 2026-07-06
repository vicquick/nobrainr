"""Parent ASGI app — mounts MCP server + JSON API."""

import asyncio
import logging
from contextlib import asynccontextmanager

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.routing import Mount

from nobrainr.config import settings
from nobrainr.db.pool import get_pool, close_pool
from nobrainr.db.schema import init_schema
from nobrainr.embeddings.ollama import check_model

from nobrainr.dashboard.api import api_routes

logger = logging.getLogger("nobrainr")


# ──────────────────────────────────────────────
# Workaround: MCP SSE initialization race condition
# ──────────────────────────────────────────────
# Some MCP clients (including Claude Code) occasionally send tool call
# requests before the initialize/initialized handshake completes. The
# MCP library (mcp>=1.26) raises RuntimeError which becomes a -32602
# "Invalid request parameters" error — permanently breaking the session.
#
# Fix: patch ServerSession._received_request to auto-promote the session
# to Initialized state instead of raising.
# ──────────────────────────────────────────────
def _patch_mcp_session_init_race():
    try:
        from mcp.server.session import ServerSession, InitializationState

        _original = ServerSession._received_request

        async def _tolerant_received_request(self, responder):
            if self._initialization_state != InitializationState.Initialized:
                req_type = type(responder.request.root).__name__
                if req_type not in ("InitializeRequest", "PingRequest"):
                    logger.warning(
                        "MCP request before initialization complete — auto-promoting session "
                        "(client likely skipped handshake). Request: %s",
                        req_type,
                    )
                    self._initialization_state = InitializationState.Initialized
            return await _original(self, responder)

        ServerSession._received_request = _tolerant_received_request
        logger.info("Patched MCP ServerSession for initialization race tolerance")
    except Exception:
        logger.warning("Could not patch MCP session init race — upgrade mcp package if issues persist")


_patch_mcp_session_init_race()


async def _warm_graph_cache():
    """Background task: pre-compute graph layout cache on startup.

    Deferred to +5min on 2026-04-19: networkx spring_layout on 3500+
    nodes pegs CPU for several minutes and starves the reranker /
    memory_search pipeline. The graph cache only serves the dashboard
    /api/graph endpoint — it should never delay MCP search.
    """
    import os
    from nobrainr.dashboard.api import _GRAPH_CACHE_PATH
    if os.path.exists(_GRAPH_CACHE_PATH):
        return  # already cached (shouldn't happen after redeploy, but be safe)
    try:
        await asyncio.sleep(300)
        from nobrainr.dashboard.api import api_graph
        from starlette.requests import Request
        scope = {"type": "http", "method": "GET", "path": "/api/graph", "query_string": b"refresh=true", "headers": []}
        request = Request(scope)
        await api_graph(request)
        logger.info("Graph layout cache pre-warmed on startup")
    except Exception:
        logger.warning("Graph cache pre-warm failed (will compute on first request)")


async def _warm_ppr_cache():
    """Background task: build the HippoRAG-2 PPR sparse adjacency on startup.

    The first MCP retrieval that triggers the graph branch otherwise pays
    a ~290ms cache-build cost (50K entities, 290K edges → CSR matrix).
    Pre-warming it keeps p99 retrieval latency stable from request #1.
    Cheap (<300ms) and runs in the background after lifespan completes.
    """
    try:
        # Tiny initial delay so we don't compete with the (heavier) reranker
        # model load that happens immediately on startup.
        await asyncio.sleep(30)
        from nobrainr.services.ppr import get_cache
        cache = await get_cache()
        logger.info(
            "PPR cache pre-warmed: %d entities, %d edges",
            cache.n_entities, cache.n_edges,
        )
    except Exception:
        logger.warning("PPR cache pre-warm failed (will build on first query)")


async def _auto_backfill():
    """Background task: extract entities from any unprocessed memories on startup."""
    if not settings.extraction_enabled:
        return
    try:
        from nobrainr.extraction.pipeline import backfill
        total = await backfill(batch_size=10, concurrency=4)
        if total:
            logger.info("Auto-backfill complete: %d memories extracted", total)
    except Exception:
        logger.exception("Auto-backfill failed (will retry next restart)")


@asynccontextmanager
async def lifespan(app):
    """Shared lifespan: init DB, check models, start backfill, yield, cleanup."""
    logger.info("nobrainr starting up...")
    # Retry DB connection (network may not be ready immediately after deploy)
    pool = None
    for attempt in range(1, 6):
        try:
            pool = await get_pool()
            break
        except Exception as exc:
            logger.warning("DB connection attempt %d/5 failed: %s", attempt, exc)
            if attempt < 5:
                await asyncio.sleep(3 * attempt)
            else:
                raise
    await init_schema(pool)

    model_ok = await check_model()
    if model_ok:
        logger.info(f"Embedding model '{settings.embedding_model}' ready")
    else:
        logger.warning(
            f"Embedding model '{settings.embedding_model}' not found. "
            f"Run: ollama pull {settings.embedding_model}"
        )

    # Normalize categories on startup (idempotent)
    try:
        from nobrainr.db import queries as q
        from nobrainr.utils.categories import _CATEGORY_MAP
        norm_count = await q.normalize_categories(_CATEGORY_MAP)
        if norm_count:
            logger.info("Normalized %d memory categories on startup", norm_count)
    except Exception:
        logger.exception("Category normalization failed")

    # Ensure crawl_queue table exists for knowledge crawler
    try:
        from nobrainr.crawler.knowledge import ensure_crawl_queue_table
        await ensure_crawl_queue_table()
    except Exception:
        logger.exception("Failed to create crawl_queue table")

    # Ensure interest_signals table exists for interest tracking (Phase 5)
    try:
        from nobrainr.db.queries import ensure_interest_signals_table
        await ensure_interest_signals_table()
    except Exception:
        logger.exception("Failed to create interest_signals table")

    # Fire-and-forget backfill for any unextracted memories
    backfill_task = asyncio.create_task(_auto_backfill())

    # Pre-warm graph layout cache (runs in background, ~60s)
    asyncio.create_task(_warm_graph_cache())

    # Pre-warm HippoRAG-2 PPR sparse adjacency cache (runs in background,
    # ~300ms after a 30s delay). Keeps the first graph-branch retrieval
    # from paying the cache-build cost.
    asyncio.create_task(_warm_ppr_cache())

    # Pre-warm UMAP galaxy cache so the first dashboard load is instant.
    # Pushed to +10min on 2026-04-19: Galaxy + graph-layout pre-warm both
    # peg CPU for minutes and on a 14-vCPU container they starve live
    # MCP memory_search (reranker is CPU too). Galaxy only benefits the
    # dashboard UI — it should never delay search.
    async def _warm_galaxy():
        try:
            await asyncio.sleep(600)
            from nobrainr.dashboard.api import api_galaxy
            from starlette.requests import Request
            scope = {"type": "http", "method": "GET", "path": "/api/galaxy",
                     "query_string": b"limit=10000", "headers": []}
            await api_galaxy(Request(scope))
            logger.info("Galaxy UMAP cache pre-warmed on startup")
        except Exception:
            logger.warning("Galaxy pre-warm failed (will compute on first request)")
    asyncio.create_task(_warm_galaxy())

    # Pre-warm the cross-encoder reranker so the first user search doesn't
    # pay the 2-5s cold-start of loading BAAI/bge-reranker-v2-m3 from HF.
    # 2026-05-05: now also runs a sample inference so CUDA kernels are
    # compiled/warm before the first real query. Without the inference
    # warmup the reranker model loads but the first GPU forward pass still
    # takes ~5-10s.
    async def _warm_reranker():
        try:
            from nobrainr.services import reranker
            # Only warm the in-process CrossEncoder when it can actually be
            # used (2026-07-06): with backend="http" and the in-process
            # fallback disabled, warming loaded 2GB into the server for a
            # path that never runs — it was the single biggest chunk of
            # this process's chronic ~1.8GB swap footprint.
            _inprocess_possible = (
                settings.reranker_backend != "http"
                or settings.reranker_inprocess_fallback
            )
            if settings.reranker_enabled and _inprocess_possible:
                model = await asyncio.to_thread(reranker._get_st_reranker)
                # Sample inference to warm CUDA kernels
                await asyncio.to_thread(
                    model.predict,
                    [("warmup query", "warmup document text")],
                )
                logger.info(
                    "Reranker pre-warmed (model + GPU kernels): %s on %s",
                    settings.reranker_model, settings.reranker_device,
                )
        except Exception:
            logger.warning("Reranker pre-warm failed — will load on first search")
    asyncio.create_task(_warm_reranker())

    # Pre-populate the query embedding LRU cache with the top-50 most-
    # frequent recent queries from memory_outcomes. After a deploy the cache
    # is empty; without this the first user query of a recurring pattern
    # still pays 7s embed time. Background, non-blocking.
    async def _warm_query_cache():
        try:
            await asyncio.sleep(15)  # let other warmups breathe
            from nobrainr.db.pool import get_pool as _gp
            from nobrainr.embeddings.ollama import embed_text, _qcache_put
            pool2 = await _gp()
            async with pool2.acquire() as conn2:
                rows = await conn2.fetch(
                    """
                    SELECT query_text, COUNT(*) AS hits
                    FROM memory_outcomes
                    WHERE query_text IS NOT NULL
                      AND length(query_text) > 0
                      AND created_at > NOW() - INTERVAL '30 days'
                    GROUP BY query_text
                    ORDER BY hits DESC
                    LIMIT 50
                    """
                )
            warmed = 0
            for r in rows:
                try:
                    q = r["query_text"]
                    if not q:
                        continue
                    vec = await embed_text(q)
                    _qcache_put(q, vec)
                    warmed += 1
                except Exception:
                    pass
            logger.info("Query embedding cache pre-warmed with %d top queries", warmed)
        except Exception:
            logger.warning("Query cache pre-warm failed — cache will fill lazily")
    asyncio.create_task(_warm_query_cache())

    # Start background scheduler for maintenance + feedback integration
    if settings.scheduler_enabled:
        from nobrainr.scheduler import scheduler
        scheduler.start()

    # Start the streamable-http session manager (needs its own task group)
    from nobrainr.mcp.server import mcp as _mcp_server
    if _mcp_server._session_manager is not None:
        streamable_cm = _mcp_server._session_manager.run()
        await streamable_cm.__aenter__()
    else:
        streamable_cm = None

    yield

    if streamable_cm is not None:
        await streamable_cm.__aexit__(None, None, None)
    if settings.scheduler_enabled:
        from nobrainr.scheduler import scheduler
        await scheduler.stop()
    backfill_task.cancel()
    await close_pool()
    logger.info("nobrainr shut down.")


def create_app():
    """Build the parent Starlette app with MCP + API mounted."""
    from nobrainr.mcp.server import mcp

    # Get MCP ASGI apps for both transports
    sse_app = mcp.sse_app()

    # For streamable HTTP: extract the actual ASGI handler from the app
    # and mount it as a direct Route at /mcp (the app internally creates
    # a /mcp route, so we grab the handler to avoid path doubling).
    streamable_starlette = mcp.streamable_http_app()
    streamable_handler = None
    for route in streamable_starlette.routes:
        if hasattr(route, 'path') and route.path == '/mcp':
            streamable_handler = route.endpoint
            break

    from starlette.routing import Route

    routes = [
        *api_routes,
    ]

    # Streamable HTTP at /mcp (preferred transport)
    if streamable_handler is not None:
        routes.append(Route("/mcp", endpoint=streamable_handler, methods=["GET", "POST", "DELETE"]))

    # SSE transport (backward compat, handles /sse and /messages/)
    routes.append(Mount("/", app=sse_app))

    middleware = [
        Middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_methods=["GET", "POST", "DELETE"],
            allow_headers=["*"],
        ),
    ]

    app = Starlette(routes=routes, lifespan=lifespan, middleware=middleware)
    return app
