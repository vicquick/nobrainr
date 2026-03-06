"""Ollama embedding client for nomic-embed-text."""

import httpx

from nobrainr.config import settings

_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(base_url=settings.ollama_url, timeout=60.0)
    return _client


async def embed_text(text: str) -> list[float]:
    """Generate embedding for a single text string."""
    client = _get_client()
    resp = await client.post(
        "/api/embed",
        json={"model": settings.embedding_model, "input": text},
    )
    resp.raise_for_status()
    data = resp.json()
    return data["embeddings"][0]


async def embed_batch(texts: list[str], batch_size: int = 32) -> list[list[float]]:
    """Generate embeddings for multiple texts."""
    all_embeddings = []
    client = _get_client()
    for i in range(0, len(texts), batch_size):
        batch = texts[i : i + batch_size]
        resp = await client.post(
            "/api/embed",
            json={"model": settings.embedding_model, "input": batch},
        )
        resp.raise_for_status()
        data = resp.json()
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
