"""Ollama embedding client for nomic-embed-text."""

import httpx

from nobrainr.config import settings


async def embed_text(text: str) -> list[float]:
    """Generate embedding for a single text string."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{settings.ollama_url}/api/embed",
            json={"model": settings.embedding_model, "input": text},
        )
        resp.raise_for_status()
        data = resp.json()
        return data["embeddings"][0]


async def embed_batch(texts: list[str], batch_size: int = 32) -> list[list[float]]:
    """Generate embeddings for multiple texts."""
    all_embeddings = []
    async with httpx.AsyncClient(timeout=60.0) as client:
        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]
            resp = await client.post(
                f"{settings.ollama_url}/api/embed",
                json={"model": settings.embedding_model, "input": batch},
            )
            resp.raise_for_status()
            data = resp.json()
            all_embeddings.extend(data["embeddings"])
    return all_embeddings


async def check_model() -> bool:
    """Check if the embedding model is available in Ollama."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{settings.ollama_url}/api/tags")
            resp.raise_for_status()
            models = resp.json().get("models", [])
            return any(m["name"].startswith(settings.embedding_model) for m in models)
    except Exception:
        return False
