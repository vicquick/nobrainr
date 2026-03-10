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
    keep_alive: str = "5m",
    think: bool = True,
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
        think: Enable model thinking/reasoning (disable for simple structured tasks).

    Returns:
        Parsed JSON dict from the LLM response.

    Raises:
        Exception on HTTP or parsing errors.
    """
    client = _get_client()
    payload = {
        "model": model or settings.extraction_model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "format": schema,
        "stream": False,
        "think": think,
        "options": {
            "temperature": temperature,
            "num_ctx": num_ctx,
        },
        "keep_alive": keep_alive,
    }

    retryable_status = {404, 503, 502, 429}
    last_exc: Exception | None = None
    for attempt in range(5):
        try:
            resp = await client.post("/api/chat", json=payload, timeout=timeout)
            if resp.status_code in retryable_status:
                last_exc = httpx.HTTPStatusError(
                    f"HTTP {resp.status_code} on attempt {attempt + 1}",
                    request=resp.request, response=resp,
                )
                wait = 2 ** attempt
                logger.warning(
                    "Ollama /api/chat returned %d (attempt %d/5), retrying in %ds",
                    resp.status_code, attempt + 1, wait,
                )
                await asyncio.sleep(wait)
                continue
            resp.raise_for_status()
            data = resp.json()
            content = data["message"]["content"]
            if not content or not content.strip():
                wait = 2 ** attempt
                logger.warning(
                    "Ollama returned empty content (attempt %d/5), retrying in %ds",
                    attempt + 1, wait,
                )
                last_exc = ValueError("Empty LLM response")
                await asyncio.sleep(wait)
                continue
            return json.loads(content)
        except json.JSONDecodeError as exc:
            wait = 2 ** attempt
            logger.warning(
                "Ollama returned malformed JSON (attempt %d/5), retrying in %ds: %.80s",
                attempt + 1, wait, str(exc),
            )
            last_exc = exc
            await asyncio.sleep(wait)
            continue
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout) as exc:
            wait = 2 ** attempt
            logger.warning(
                "Ollama /api/chat %s (attempt %d/5), retrying in %ds",
                type(exc).__name__, attempt + 1, wait,
            )
            await asyncio.sleep(wait)
            last_exc = exc

    raise last_exc or RuntimeError("ollama_chat failed after retries")
