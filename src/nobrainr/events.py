"""In-memory SSE event bus for real-time dashboard updates."""

import asyncio
import json
import logging
from typing import AsyncGenerator

logger = logging.getLogger("nobrainr")

_subscribers: set[asyncio.Queue[str]] = set()

HEARTBEAT_INTERVAL = 30  # seconds
MAX_SUBSCRIBERS = 50


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
    """Yield SSE-formatted messages as they arrive, with heartbeat."""
    if len(_subscribers) >= MAX_SUBSCRIBERS:
        yield "data: {\"type\": \"error\", \"message\": \"Too many connections\"}\n\n"
        return

    q: asyncio.Queue[str] = asyncio.Queue(maxsize=256)
    _subscribers.add(q)
    try:
        while True:
            try:
                payload = await asyncio.wait_for(q.get(), timeout=HEARTBEAT_INTERVAL)
                yield f"data: {payload}\n\n"
            except asyncio.TimeoutError:
                yield ": heartbeat\n\n"
    finally:
        _subscribers.discard(q)
