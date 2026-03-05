"""Shared Ollama chat helper for LLM-powered tasks."""

import json
import logging

import httpx

from nobrainr.config import settings

logger = logging.getLogger("nobrainr")


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
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            f"{settings.ollama_url}/api/chat",
            json={
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
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return json.loads(data["message"]["content"])
