"""Connection pool management for asyncpg."""

import asyncio

import asyncpg
from pgvector.asyncpg import register_vector

from nobrainr.config import settings

_pool: asyncpg.Pool | None = None
_pool_lock = asyncio.Lock()


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is not None:
        return _pool
    async with _pool_lock:
        if _pool is None:
            _pool = await asyncpg.create_pool(
                settings.database_url,
                min_size=2,
                max_size=25,
                init=_init_connection,
                ssl=False,
                timeout=30,
            )
    return _pool


async def _init_connection(conn: asyncpg.Connection) -> None:
    await register_vector(conn)
    await conn.execute("SET hnsw.ef_search = 200")
    # pgvector 0.8.0+: iterative scan for filtered vector queries. The valid
    # values are 'off', 'relaxed_order' (approximate), 'strict_order' (exact).
    # Our hybrid search already re-ranks with full-precision memory_relevance
    # so we want speed over strict order here. 'on' silently becomes a no-op,
    # which is why queries with filters sometimes missed obvious hits on the
    # previous config.
    try:
        await conn.execute("SET hnsw.iterative_scan = 'relaxed_order'")
        await conn.execute("SET hnsw.max_scan_tuples = 20000")
    except Exception:
        pass  # older pgvector versions don't support these


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
