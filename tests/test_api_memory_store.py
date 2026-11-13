"""POST /api/memory-store — the REST door to the write queue.

Two host watchdogs (nightly image scan, config drift check) have posted to this
path since October 2026 and got 404 every night; their findings never arrived.
"""

from __future__ import annotations

import asyncio
import json

from starlette.requests import Request

from nobrainr.dashboard import api


def _post(body: bytes) -> Request:
    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {"type": "http", "method": "POST", "path": "/api/memory-store", "query_string": b"",
         "headers": [(b"content-type", b"application/json")]},
        receive,
    )


def _run(body) -> tuple[int, dict]:
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    resp = asyncio.run(api.api_memory_store(_post(raw)))
    return resp.status_code, json.loads(resp.body)


def test_route_is_registered_for_post():
    treffer = [r for r in api.api_routes if getattr(r, "path", "") == "/api/memory-store"]
    assert len(treffer) == 1
    assert "POST" in treffer[0].methods


def test_queues_the_write(monkeypatch):
    gesehen = {}

    async def fake_enqueue(**kw):
        gesehen.update(kw)
        return {"queue_id": "q-1", "enqueued_at": "2026-11-13T10:00:00+00:00"}

    from nobrainr.db import write_queue
    monkeypatch.setattr(write_queue, "enqueue_memory_write", fake_enqueue)

    status, out = _run({
        "content": "Trivy nightly: 2 new fixable findings", "summary": "s" * 900,
        "category": "infrastructure", "tags": ["trivy-nightly", "cve"],
        "source_machine": "bimavo", "source_type": "agent", "confidence": 7,
    })
    assert status == 202
    assert out == {"status": "queued", "queue_id": "q-1", "enqueued_at": "2026-11-13T10:00:00+00:00"}
    assert gesehen["content"].startswith("Trivy nightly")
    assert gesehen["tags"] == ["trivy-nightly", "cve"]
    assert gesehen["source_machine"] == "bimavo"
    assert len(gesehen["summary"]) == 500          # cut, not rejected
    assert gesehen["confidence"] == 1.0            # clamped to [0, 1]


def test_bad_requests_are_400_and_queue_nothing(monkeypatch):
    aufrufe = []

    async def fake_enqueue(**kw):
        aufrufe.append(kw)
        return {"queue_id": "x", "enqueued_at": "x"}

    from nobrainr.db import write_queue
    monkeypatch.setattr(write_queue, "enqueue_memory_write", fake_enqueue)

    for schlecht in (b"not json", [1, 2], {}, {"content": "   "}, {"content": 5},
                     {"content": "ok", "tags": "a,b"}, {"content": "ok", "tags": [1]},
                     {"content": "ok", "confidence": "hoch"}, {"content": "ok", "metadata": "x"}):
        status, out = _run(schlecht)
        assert status == 400, schlecht
        assert "error" in out
    assert aufrufe == []


def test_too_large_is_413(monkeypatch):
    from nobrainr.config import settings
    monkeypatch.setattr(settings, "max_content_length", 10)
    status, out = _run({"content": "x" * 11})
    assert status == 413 and "too large" in out["error"]
