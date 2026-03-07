"""Shared Ollama chat helper for LLM-powered tasks."""

import asyncio
import json
import logging

import httpx

from nobrainr.config import settings

logger = logging.getLogger("nobrainr")

_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(base_url=settings.ollama_url, timeout=180.0)
    return _client


async def ollama_chat(
    system: str,
    user: str,
    schema: dict,
    *,
    model: str | None = None,
    temperature: float = 0.1,
    num_ctx: int = 4096,
    timeout: float = 180.0,
    keep_alive: str = "24h",
) -> dict:
    """Send a structured-output chat request to Ollama.

    Args:
        system: System prompt.
        user: User message.
        schema: JSON schema for structured output.
        model: Ollama model name (defaults to settings.extraction_model).
        temperature: LLM temperature.
        num_ctx: Context window size.
        timeout: HTTP timeout in seconds.
        keep_alive: How long to keep the model loaded.

    Returns:
        Parsed JSON dict from the LLM response.

    Raises:
        Exception on HTTP or parsing errors.
    """
    client = _get_client()
    # Disable qwen3 thinking mode for structured output tasks (2x+ faster)
    if "/nothink" not in system:
        system = system + " /nothink"
    payload = {
        "model": model or settings.extraction_model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "format": schema,
        "stream": False,
        "options": {
            "temperature": temperature,
            "num_ctx": num_ctx,
        },
        "keep_alive": keep_alive,
    }

    last_exc = None
    for attempt in range(5):
        resp = await client.post("/api/chat", json=payload, timeout=timeout)
        if resp.status_code == 404:
            last_exc = httpx.HTTPStatusError(
                f"404 on attempt {attempt + 1}", request=resp.request, response=resp,
            )
            wait = 2 ** attempt
            logger.warning("Ollama /api/chat returned 404 (attempt %d/5), retrying in %ds", attempt + 1, wait)
            await asyncio.sleep(wait)
            continue
        resp.raise_for_status()
        data = resp.json()
        return json.loads(data["message"]["content"])

    raise last_exc
