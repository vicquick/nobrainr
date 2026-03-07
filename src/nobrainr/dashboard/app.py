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


async def _auto_backfill():
    """Background task: extract entities from any unprocessed memories on startup."""
    if not settings.extraction_enabled:
        return
    try:
        from nobrainr.extraction.pipeline import backfill
        total = await backfill(batch_size=5)
        if total:
            logger.info("Auto-backfill complete: %d memories extracted", total)
    except Exception:
        logger.exception("Auto-backfill failed (will retry next restart)")


@asynccontextmanager
async def lifespan(app):
    """Shared lifespan: init DB, check models, start backfill, yield, cleanup."""
    logger.info("nobrainr starting up...")
    pool = await get_pool()
    await init_schema(pool)

    model_ok = await check_model()
    if model_ok:
        logger.info(f"Embedding model '{settings.embedding_model}' ready")
    else:
        logger.warning(
            f"Embedding model '{settings.embedding_model}' not found. "
            f"Run: ollama pull {settings.embedding_model}"
        )

    # Fire-and-forget backfill for any unextracted memories
    backfill_task = asyncio.create_task(_auto_backfill())

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
    streamable_app = mcp.streamable_http_app()

    # streamable_http_app() creates its own /mcp route internally and needs
    # its session_manager.run() called in the parent lifespan (done above).
    # Mount at "/" so the internal /mcp path isn't doubled to /mcp/mcp.
    routes = [
        *api_routes,
        # Streamable HTTP transport (serves /mcp)
        Mount("/", app=streamable_app),
        # SSE transport (backward compat, handles /sse and /messages/)
        Mount("/", app=sse_app),
    ]

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
