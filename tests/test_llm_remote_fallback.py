"""Remote-LLM dispatch: off / fallback / parallel.

Covers the wrapper added 2026-08-20 that lets nobrainr borrow a second
OpenAI-compatible endpoint (Hetzner Experiments) when the single local GPU
slot is the bottleneck. The local call path itself is not exercised here —
it is stubbed — because the point under test is *which leg runs*, not what
llama-server returns.
"""

import asyncio

import pytest

from nobrainr.config import settings
from nobrainr.extraction import llm as llm_mod


@pytest.fixture
def remote_on(monkeypatch):
    """Configure a usable remote endpoint without touching the network."""
    monkeypatch.setattr(settings, "llm_remote_url", "https://remote.invalid/api")
    monkeypatch.setattr(settings, "llm_remote_api_key", "test-key")
    monkeypatch.setattr(settings, "llm_remote_model", "Qwen3.8-27B")
    return settings


def _stub(monkeypatch, *, local, remote):
    """Replace both legs with controllable coroutines."""
    calls = {"local": 0, "remote": 0}

    async def fake_local(*args, **kwargs):
        calls["local"] += 1
        return await local()

    async def fake_remote(*args, **kwargs):
        calls["remote"] += 1
        return await remote()

    monkeypatch.setattr(llm_mod, "_ollama_chat_local", fake_local)
    monkeypatch.setattr(llm_mod, "_remote_chat", fake_remote)
    return calls


async def _ok(value):
    return value


def test_mode_off_never_touches_remote(monkeypatch, remote_on):
    """Default mode must be a pure passthrough — no behaviour change."""
    monkeypatch.setattr(settings, "llm_remote_mode", "off")

    async def local():
        return {"from": "local"}

    async def remote():  # pragma: no cover - must never run
        raise AssertionError("remote called while mode=off")

    calls = _stub(monkeypatch, local=local, remote=remote)
    result = asyncio.run(llm_mod.ollama_chat("s", "u", {}))

    assert result == {"from": "local"}
    assert calls == {"local": 1, "remote": 0}


def test_fallback_prefers_local_when_it_works(monkeypatch, remote_on):
    monkeypatch.setattr(settings, "llm_remote_mode", "fallback")

    async def local():
        return {"from": "local"}

    async def remote():  # pragma: no cover - must never run
        raise AssertionError("remote called though local succeeded")

    calls = _stub(monkeypatch, local=local, remote=remote)
    result = asyncio.run(llm_mod.ollama_chat("s", "u", {}))

    assert result == {"from": "local"}
    assert calls["remote"] == 0


def test_fallback_routes_to_remote_on_local_failure(monkeypatch, remote_on):
    monkeypatch.setattr(settings, "llm_remote_mode", "fallback")

    async def local():
        raise RuntimeError("llama-server down")

    async def remote():
        return {"from": "remote"}

    calls = _stub(monkeypatch, local=local, remote=remote)
    result = asyncio.run(llm_mod.ollama_chat("s", "u", {}))

    assert result == {"from": "remote"}
    assert calls == {"local": 1, "remote": 1}


def test_fallback_covers_live_skip(monkeypatch, remote_on):
    """GPU-busy live calls are dropped today; the remote should catch them."""
    monkeypatch.setattr(settings, "llm_remote_mode", "fallback")

    async def local():
        raise llm_mod.LiveLLMSkipped("GPU busy with 2 scheduler call(s)")

    async def remote():
        return {"from": "remote"}

    _stub(monkeypatch, local=local, remote=remote)
    result = asyncio.run(llm_mod.ollama_chat("s", "u", {}, caller_kind="live"))

    assert result == {"from": "remote"}


def test_parallel_returns_first_winner_and_cancels_loser(monkeypatch, remote_on):
    monkeypatch.setattr(settings, "llm_remote_mode", "parallel")
    slow_cancelled = {"yes": False}

    async def local():
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            slow_cancelled["yes"] = True
            raise
        return {"from": "local"}

    async def remote():
        return {"from": "remote"}

    _stub(monkeypatch, local=local, remote=remote)

    async def drive():
        result = await llm_mod.ollama_chat("s", "u", {})
        # let the cancellation actually land before asserting on it
        await asyncio.sleep(0)
        return result

    result = asyncio.run(drive())

    assert result == {"from": "remote"}
    assert slow_cancelled["yes"] is True


def test_parallel_survives_one_failing_leg(monkeypatch, remote_on):
    """A fast failure must not beat a slower success."""
    monkeypatch.setattr(settings, "llm_remote_mode", "parallel")

    async def local():
        await asyncio.sleep(0.05)
        return {"from": "local"}

    async def remote():
        raise RuntimeError("429 rate limited")

    _stub(monkeypatch, local=local, remote=remote)
    result = asyncio.run(llm_mod.ollama_chat("s", "u", {}))

    assert result == {"from": "local"}


def test_parallel_raises_when_both_legs_fail(monkeypatch, remote_on):
    monkeypatch.setattr(settings, "llm_remote_mode", "parallel")

    async def local():
        raise RuntimeError("llama-server down")

    async def remote():
        raise RuntimeError("429 rate limited")

    _stub(monkeypatch, local=local, remote=remote)
    with pytest.raises(RuntimeError, match="llama-server down"):
        asyncio.run(llm_mod.ollama_chat("s", "u", {}))


def test_remote_ignored_when_incompletely_configured(monkeypatch, remote_on):
    """Mode alone is not enough — a missing model must not enable the remote."""
    monkeypatch.setattr(settings, "llm_remote_mode", "fallback")
    monkeypatch.setattr(settings, "llm_remote_model", "")

    async def local():
        raise RuntimeError("llama-server down")

    async def remote():  # pragma: no cover - must never run
        raise AssertionError("remote called though model is unset")

    _stub(monkeypatch, local=local, remote=remote)
    with pytest.raises(RuntimeError, match="llama-server down"):
        asyncio.run(llm_mod.ollama_chat("s", "u", {}))
