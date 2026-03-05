"""In-memory SSE event bus for real-time dashboard updates."""

import asyncio
import json
import logging
from typing import AsyncGenerator

logger = logging.getLogger("nobrainr")

_subscribers: set[asyncio.Queue[str]] = set()


def publish(event_type: str, data: dict | None = None) -> None:
    """Publish an event to all connected SSE clients (fire-and-forget)."""
    payload = json.dumps({"type": event_type, **(data or {})})
    dead: list[asyncio.Queue[str]] = []
    for q in _subscribers:
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            dead.append(q)
    for q in dead:
        _subscribers.discard(q)


async def subscribe() -> AsyncGenerator[str, None]:
    """Yield SSE-formatted messages as they arrive."""
    q: asyncio.Queue[str] = asyncio.Queue(maxsize=256)
    _subscribers.add(q)
    try:
        while True:
            payload = await q.get()
            yield f"data: {payload}\n\n"
    finally:
        _subscribers.discard(q)
