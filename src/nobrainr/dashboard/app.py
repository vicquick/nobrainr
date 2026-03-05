"""Parent ASGI app — mounts MCP server + JSON API."""

import asyncio
import logging
from contextlib import asynccontextmanager

from starlette.applications import Starlette
from starlette.routing import Mount

from nobrainr.config import settings
from nobrainr.db.pool import get_pool, close_pool
from nobrainr.db.schema import init_schema
from nobrainr.embeddings.ollama import check_model

from nobrainr.dashboard.api import api_routes

logger = logging.getLogger("nobrainr")


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

    yield

    if settings.scheduler_enabled:
        from nobrainr.scheduler import scheduler
        await scheduler.stop()
    backfill_task.cancel()
    await close_pool()
    logger.info("nobrainr shut down.")


def create_app():
    """Build the parent Starlette app with MCP + API mounted."""
    from nobrainr.mcp.server import mcp

    # Get the MCP ASGI app (SSE transport)
    mcp_app = mcp.sse_app()

    # Build all routes: API + MCP catch-all
    routes = [
        *api_routes,
        # MCP SSE app as catch-all (handles /sse and /messages/)
        Mount("/", app=mcp_app),
    ]

    app = Starlette(routes=routes, lifespan=lifespan)
    return app
