"""side_chat (2026-10-24): nobrainr's live helper calls use gemma only when
a slot is free and never take both, so the live app never queues behind them."""

from __future__ import annotations

import asyncio


def _setup(monkeypatch, slots, running=None):
    from nobrainr.config import settings
    from nobrainr.extraction import llm

    monkeypatch.setattr(settings, "llm_server_url", "http://llama-server:8080")
    monkeypatch.setattr(llm, "_remote_enabled", lambda: True)
    calls: list[str] = []

    async def fake_local(*a, **k):
        calls.append("local")
        return {"ok": "local"}

    async def fake_remote(*a, **k):
        calls.append("remote")
        return {"ok": "remote"}

    monkeypatch.setattr(llm, "ollama_chat", fake_local)
    monkeypatch.setattr(llm, "_remote_chat", fake_remote)

    class Resp:
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
            assert "/upstream/" not in url
            if url.endswith("/running"):
                return Resp({"running": running if running is not None else [
                    {"model": "gemma-4-26b-a4b", "state": "ready",
                     "proxy": "http://localhost:5801"}]})
            if isinstance(slots, Exception):
                raise slots
            return Resp(slots)

    monkeypatch.setattr(llm.httpx, "AsyncClient", lambda **k: Client())
    return llm, calls


def _call(llm):
    return llm.side_chat("s", "u", {}, model="gemma-4-26b-a4b", timeout=5)


def test_free_slot_goes_local(monkeypatch):
    llm, calls = _setup(monkeypatch, [{"is_processing": True}, {"is_processing": False}])
    assert asyncio.run(_call(llm)) == {"ok": "local"}
    assert calls == ["local"]


def test_both_slots_busy_goes_remote(monkeypatch):
    llm, calls = _setup(monkeypatch, [{"is_processing": True}, {"is_processing": True}])
    assert asyncio.run(_call(llm)) == {"ok": "remote"}


def test_model_not_resident_goes_remote(monkeypatch):
    llm, calls = _setup(monkeypatch, [{"is_processing": False}], running=[])
    assert asyncio.run(_call(llm)) == {"ok": "remote"}


def test_probe_error_goes_remote(monkeypatch):
    llm, calls = _setup(monkeypatch, RuntimeError("boom"))
    assert asyncio.run(_call(llm)) == {"ok": "remote"}


def test_second_side_call_never_takes_the_other_slot(monkeypatch):
    llm, calls = _setup(monkeypatch, [{"is_processing": False}, {"is_processing": False}])
    gate = asyncio.Event()

    async def slow_local(*a, **k):
        calls.append("local")
        await gate.wait()
        return {"ok": "local"}

    monkeypatch.setattr(llm, "ollama_chat", slow_local)

    async def run():
        first = asyncio.create_task(_call(llm))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        second = await _call(llm)
        gate.set()
        return await first, second

    llm._side_slot_lock = asyncio.Lock()
    assert asyncio.run(run()) == ({"ok": "local"}, {"ok": "remote"})
