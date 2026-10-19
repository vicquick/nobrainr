"""GPU-yield probe filter (2026-08-15): 1-token heartbeats must not park
the 27b — only real conversations do. Rewritten 2026-10-15 for the
slot-polling probe (_poll_gpu_yield_once) that replaced /api/metrics."""

from __future__ import annotations

import asyncio


def _run_poll(monkeypatch, slots, running=None, seen=None):
    from nobrainr.config import settings
    from nobrainr.extraction import llm

    monkeypatch.setattr(settings, "gpu_yield_models", ["qwen3-8b"])
    monkeypatch.setattr(llm, "_gpu_yield_last_busy_ts", -1e12)

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
            if seen is not None:
                seen.append(url)
            if url.endswith("/running"):
                return Resp({"running": running if running is not None else [
                    {"model": "qwen3-8b", "state": "ready",
                     "proxy": "http://localhost:5802"}]})
            if isinstance(slots, Exception):
                raise slots
            return Resp(slots)

    monkeypatch.setattr(llm.httpx, "AsyncClient", lambda **k: Client())
    asyncio.run(llm._poll_gpu_yield_once())
    return asyncio.run(llm._live_model_active())


def test_probe_sized_requests_do_not_arm_the_park(monkeypatch):
    heartbeat = [{"is_processing": True, "n_prompt_tokens": 3, "params": {"max_tokens": 1}}]
    assert _run_poll(monkeypatch, heartbeat) is False


def test_real_requests_arm_the_park(monkeypatch):
    real = [{"is_processing": True, "n_prompt_tokens": 900, "params": {"max_tokens": 512}}]
    assert _run_poll(monkeypatch, real) is True


def test_idle_slots_do_not_arm_the_park(monkeypatch):
    idle = [{"is_processing": False, "n_prompt_tokens": 900, "params": {"max_tokens": 512}}]
    assert _run_poll(monkeypatch, idle) is False


def test_probe_never_goes_through_llama_swap_upstream(monkeypatch):
    """/upstream/<model>/ STARTS the model in llama-swap (2026-10-19: the
    probe reloaded qwen3-8b and evicted gemma). Hit the upstream port."""
    from nobrainr.config import settings

    monkeypatch.setattr(settings, "llm_server_url", "http://llama-server:8080")
    seen: list[str] = []
    _run_poll(monkeypatch, [], seen=seen)
    assert seen == ["http://llama-server:8080/running",
                    "http://llama-server:5802/slots"]
    assert not any("/upstream/" in u for u in seen)


def test_models_not_ready_are_not_probed(monkeypatch):
    seen: list[str] = []
    running = [{"model": "qwen3-8b", "state": "stopping",
                "proxy": "http://localhost:5802"}]
    _run_poll(monkeypatch, [], running=running, seen=seen)
    assert len(seen) == 1 and seen[0].endswith("/running")


def test_evicted_between_running_and_probe_is_quiet(monkeypatch, caplog):
    import httpx
    import logging

    from nobrainr.extraction import llm

    monkeypatch.setattr(llm, "_gpu_yield_warned_at", -1e12)
    with caplog.at_level(logging.WARNING, logger="nobrainr"):
        assert _run_poll(monkeypatch, httpx.ConnectError("refused")) is False
    assert not [r for r in caplog.records if "probe broken" in r.message]
