"""Dashboard /api/llm/queue must read slots on the upstream's own port:
llama-swap's /upstream/<model>/ loads the model it names."""

from __future__ import annotations

import asyncio
import json


def test_queue_probe_skips_upstream_route(monkeypatch):
    from nobrainr.config import settings
    from nobrainr.dashboard import api

    monkeypatch.setattr(settings, "llm_server_url", "http://llama-server:8080")
    seen: list[str] = []

    class Resp:
        status_code = 200

        def __init__(self, data):
            self._data = data

        def raise_for_status(self):
            pass

        def json(self):
            return self._data

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, **k):
            seen.append(url)
            if url.endswith("/running"):
                return Resp({"running": [
                    {"model": "gemma-4-26b-a4b", "state": "ready", "proxy": "http://localhost:5801"},
                    {"model": "qwen3-8b", "state": "stopping", "proxy": "http://localhost:5802"},
                ]})
            return Resp([{"is_processing": True, "n_ctx": 65536}, {"is_processing": False}])

    monkeypatch.setattr(api.httpx, "AsyncClient", lambda **k: Client())
    body = json.loads(asyncio.run(api.api_llm_queue(None)).body)
    assert seen == ["http://llama-server:8080/running", "http://llama-server:5801/slots"]
    assert body["models"]["gemma-4-26b-a4b"]["slots_busy"] == 1
