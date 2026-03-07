"""Ollama embedding client for nomic-embed-text."""

import asyncio
import logging

import httpx

from nobrainr.config import settings

logger = logging.getLogger("nobrainr")

_client: httpx.AsyncClient | None = None

MAX_RETRIES = 5
RETRYABLE_STATUS = {404, 503, 502, 429}


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(base_url=settings.ollama_url, timeout=60.0)
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
    """Generate embedding for a single text string."""
    data = await _embed_with_retry(
        {"model": settings.embedding_model, "input": text},
    )
    return data["embeddings"][0]


async def embed_batch(texts: list[str], batch_size: int = 32) -> list[list[float]]:
    """Generate embeddings for multiple texts."""
    all_embeddings = []
    for i in range(0, len(texts), batch_size):
        batch = texts[i : i + batch_size]
        data = await _embed_with_retry(
            {"model": settings.embedding_model, "input": batch},
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
