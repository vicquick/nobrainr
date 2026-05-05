"""Ollama embedding client for Qwen3-Embedding (qwen3-embedding-cpu).

Uses /api/embed. Ollama serialises embedding requests internally but does NOT
limit concurrent HTTP connections — if 5 callers hit /api/embed simultaneously,
Ollama queues them and each waits for all previous to finish. On the i5-13500
(CPU-only, ~7s per embed) that means caller 5 waits ~35s.

The global _EMBED_SEM semaphore caps in-flight embed requests to 1 so that
background jobs (write_queue, scheduler backfills) cannot starve interactive
search. Search callers should use embed_text_with_timeout() which raises
EmbedTimeout on budget exhaustion — the search layer then falls back to
FTS+graph-only ranking rather than blocking for 50s+.
"""

import asyncio
import logging

import httpx

from nobrainr.config import settings

logger = logging.getLogger("nobrainr")

_client: httpx.AsyncClient | None = None

MAX_RETRIES = 3
RETRYABLE_STATUS = {404, 503, 502, 429}

# Reduce batch size: tuned for 20-core EPYC but we have i5-13500.
# i5-13500 needs ~7s per single embed; batch=8 stays within 60s Ollama timeout.
DEFAULT_BATCH_SIZE = 8

# Global semaphore: only 1 concurrent embed to Ollama.
# i5-13500 is single-threaded for GGML inference; concurrent requests queue
# inside Ollama and each waits for all previous. SEM(1) makes the queue visible
# on the Python side where we can apply timeouts and priorities.
_EMBED_SEM: asyncio.Semaphore | None = None


def _get_sem() -> asyncio.Semaphore:
    global _EMBED_SEM
    if _EMBED_SEM is None:
        _EMBED_SEM = asyncio.Semaphore(1)
    return _EMBED_SEM


class EmbedTimeout(Exception):
    """Embed could not complete within the requested budget."""


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(base_url=settings.ollama_url, timeout=90.0)
    return _client


async def _embed_with_retry(payload: dict) -> dict:
    """POST to /api/embed with retry on transient errors."""
    client = _get_client()
    last_exc: Exception | None = None

    for attempt in range(MAX_RETRIES):
        try:
            resp = await client.post("/api/embed", json=payload)
            if resp.status_code in RETRYABLE_STATUS:
                delay = 2 ** attempt
                logger.warning(
                    "Ollama embed returned %d, retrying in %ds (attempt %d/%d)",
                    resp.status_code, delay, attempt + 1, MAX_RETRIES,
                )
                await asyncio.sleep(delay)
                last_exc = httpx.HTTPStatusError(
                    f"HTTP {resp.status_code}", request=resp.request, response=resp,
                )
                continue
            resp.raise_for_status()
            return resp.json()
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout) as exc:
            delay = 2 ** attempt
            logger.warning(
                "Ollama embed connection error: %s, retrying in %ds (attempt %d/%d)",
                exc, delay, attempt + 1, MAX_RETRIES,
            )
            await asyncio.sleep(delay)
            last_exc = exc

    raise last_exc or RuntimeError("Embedding failed after retries")


async def embed_text(text: str) -> list[float]:
    """Generate embedding for a single text string (background-safe, no timeout)."""
    sem = _get_sem()
    async with sem:
        data = await _embed_with_retry(
            {"model": settings.embedding_model, "input": text, "keep_alive": "24h"},
        )
    return data["embeddings"][0]


async def embed_text_with_timeout(text: str, timeout_s: float = 15.0) -> list[float]:
    """Embed with a budget. Raises EmbedTimeout if semaphore wait + inference > timeout_s.

    Use this in the interactive search path so a flooded Ollama queue causes
    graceful FTS fallback rather than a 50s hang.
    """
    sem = _get_sem()
    try:
        # Wait for semaphore + run inference, all within timeout_s.
        async def _do():
            async with sem:
                data = await _embed_with_retry(
                    {"model": settings.embedding_model, "input": text, "keep_alive": "24h"},
                )
            return data["embeddings"][0]

        return await asyncio.wait_for(_do(), timeout=timeout_s)
    except asyncio.TimeoutError as e:
        raise EmbedTimeout(f"embed exceeded {timeout_s}s budget") from e


async def embed_batch(
    texts: list[str],
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> list[list[float]]:
    """Generate embeddings for multiple texts (background-safe, serialised)."""
    sem = _get_sem()
    all_embeddings = []
    for i in range(0, len(texts), batch_size):
        batch = texts[i : i + batch_size]
        async with sem:
            data = await _embed_with_retry(
                {"model": settings.embedding_model, "input": batch, "keep_alive": "24h"},
            )
        all_embeddings.extend(data["embeddings"])
    return all_embeddings


async def check_model() -> bool:
    """Check if the embedding model is available in Ollama."""
    try:
        client = _get_client()
        resp = await client.get("/api/tags")
        resp.raise_for_status()
        models = resp.json().get("models", [])
        return any(m["name"].startswith(settings.embedding_model) for m in models)
    except Exception:
        return False
