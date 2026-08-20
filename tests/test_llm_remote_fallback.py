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


def test_split_sends_batch_work_remote(monkeypatch, remote_on):
    """Scheduler work must leave the GPU alone in split mode."""
    monkeypatch.setattr(settings, "llm_remote_mode", "split")

    async def local():  # pragma: no cover - must never run
        raise AssertionError("local called for batch work in split mode")

    async def remote():
        return {"from": "remote"}

    calls = _stub(monkeypatch, local=local, remote=remote)
    result = asyncio.run(llm_mod.ollama_chat("s", "u", {}, caller_kind="scheduler"))

    assert result == {"from": "remote"}
    assert calls == {"local": 0, "remote": 1}


def test_split_keeps_live_work_local(monkeypatch, remote_on):
    """Interactive calls stay on the box — no network hop, no egress."""
    monkeypatch.setattr(settings, "llm_remote_mode", "split")

    async def local():
        return {"from": "local"}

    async def remote():  # pragma: no cover - must never run
        raise AssertionError("remote called for live work in split mode")

    calls = _stub(monkeypatch, local=local, remote=remote)
    result = asyncio.run(llm_mod.ollama_chat("s", "u", {}, caller_kind="live"))

    assert result == {"from": "local"}
    assert calls == {"local": 1, "remote": 0}


def test_split_batch_degrades_to_local_when_remote_fails(monkeypatch, remote_on):
    """A 429 or outage must not stall batch work — it lands back on the GPU."""
    monkeypatch.setattr(settings, "llm_remote_mode", "split")

    async def local():
        return {"from": "local"}

    async def remote():
        raise RuntimeError("429 rate limited")

    calls = _stub(monkeypatch, local=local, remote=remote)
    result = asyncio.run(llm_mod.ollama_chat("s", "u", {}, caller_kind="scheduler"))

    assert result == {"from": "local"}
    assert calls == {"local": 1, "remote": 1}


def test_split_live_still_falls_back_to_remote(monkeypatch, remote_on):
    """Live keeps the fallback safety net underneath the routing rule."""
    monkeypatch.setattr(settings, "llm_remote_mode", "split")

    async def local():
        raise llm_mod.LiveLLMSkipped("GPU busy")

    async def remote():
        return {"from": "remote"}

    _stub(monkeypatch, local=local, remote=remote)
    result = asyncio.run(llm_mod.ollama_chat("s", "u", {}, caller_kind="live"))

    assert result == {"from": "remote"}


def test_remote_concurrency_is_bounded(monkeypatch, remote_on):
    """The remote is shared and rate-limited — never open unbounded sockets."""
    monkeypatch.setattr(settings, "llm_remote_max_concurrency", 2)
    monkeypatch.setattr(llm_mod, "_remote_semaphore", None)
    monkeypatch.setattr(llm_mod, "_remote_semaphore_limit", None)

    peak = {"now": 0, "max": 0}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": '{"ok": true}'}}]}

    class FakeClient:
        async def post(self, *args, **kwargs):
            peak["now"] += 1
            peak["max"] = max(peak["max"], peak["now"])
            await asyncio.sleep(0.02)
            peak["now"] -= 1
            return FakeResponse()

    monkeypatch.setattr(llm_mod, "_get_remote_client", lambda: FakeClient())

    async def drive():
        await asyncio.gather(*[
            llm_mod._remote_chat("s", "u", {}) for _ in range(8)
        ])

    asyncio.run(drive())
    assert peak["max"] <= 2, f"opened {peak['max']} concurrent remote calls"


@pytest.mark.parametrize(
    "window,hour,expect_local",
    [
        ("23-07", 23, True),    # window start, inclusive
        ("23-07", 2, True),     # across midnight
        ("23-07", 6, True),     # last hour inside
        ("23-07", 7, False),    # window end, exclusive
        ("23-07", 12, False),   # daytime
        ("09-17", 12, True),    # non-wrapping window
        ("09-17", 8, False),
        ("", 3, False),         # disabled
        ("garbage", 3, False),  # malformed → disabled, never raises
        ("07-07", 7, False),    # zero-width → disabled
        ("5-99", 7, False),     # out of range → disabled
    ],
)
def test_local_batch_window_boundaries(monkeypatch, window, hour, expect_local):
    import datetime as dt

    monkeypatch.setattr(settings, "llm_local_batch_hours", window)
    when = dt.datetime(2026, 8, 20, hour, 30)
    assert llm_mod._in_local_batch_window(when) is expect_local


def test_split_keeps_batch_local_during_night_window(monkeypatch, remote_on):
    """At night the GPU is idle — batch should use it instead of the remote."""
    monkeypatch.setattr(settings, "llm_remote_mode", "split")
    monkeypatch.setattr(llm_mod, "_in_local_batch_window", lambda now=None: True)

    async def local():
        return {"from": "local"}

    async def remote():  # pragma: no cover - must never run
        raise AssertionError("remote used during the local night window")

    calls = _stub(monkeypatch, local=local, remote=remote)
    result = asyncio.run(llm_mod.ollama_chat("s", "u", {}, caller_kind="scheduler"))

    assert result == {"from": "local"}
    assert calls == {"local": 1, "remote": 0}


def test_night_window_never_pushes_live_work_remote(monkeypatch, remote_on):
    """The window moves batch only; live stays local in both directions."""
    monkeypatch.setattr(settings, "llm_remote_mode", "split")
    for in_window in (True, False):
        monkeypatch.setattr(
            llm_mod, "_in_local_batch_window", lambda now=None, v=in_window: v
        )
        assert llm_mod._split_prefers_remote("live") is False


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
