"""Host GPU lease: nobrainr must never load a local chat model mid-render."""

import asyncio
import fcntl
import os

import pytest

from nobrainr.config import settings
from nobrainr.extraction import llm


@pytest.fixture
def lease(tmp_path, monkeypatch):
    path = tmp_path / "gpu0.lock"
    path.touch()
    monkeypatch.setattr(settings, "gpu_lease_path", str(path))
    fd = os.open(path, os.O_RDWR)
    yield fd
    os.close(fd)


def test_free_lease_is_not_held(lease):
    assert llm.gpu_lease_held() is False


def test_exclusive_lease_is_held(lease):
    fcntl.flock(lease, fcntl.LOCK_EX)
    assert llm.gpu_lease_held() is True
    fcntl.flock(lease, fcntl.LOCK_UN)
    assert llm.gpu_lease_held() is False


def test_probe_never_keeps_a_lock(lease):
    llm.gpu_lease_held()
    # holder must still be able to take LOCK_EX right after a probe
    fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_missing_file_means_not_held(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "gpu_lease_path", str(tmp_path / "nope.lock"))
    assert llm.gpu_lease_held() is False


def test_empty_path_disables(monkeypatch):
    monkeypatch.setattr(settings, "gpu_lease_path", "")
    assert llm.gpu_lease_held() is False


def test_batch_goes_remote_only_while_held(lease, monkeypatch):
    fcntl.flock(lease, fcntl.LOCK_EX)
    calls = []

    async def fake_local(*a, **kw):
        calls.append("local")
        return {"leg": "local"}

    async def fake_remote(*a, **kw):
        calls.append("remote")
        return {"leg": "remote"}

    monkeypatch.setattr(llm, "_remote_enabled", lambda: True)
    monkeypatch.setattr(llm, "_ollama_chat_local", fake_local)
    monkeypatch.setattr(llm, "_remote_chat", fake_remote)
    # night window would normally route batch work local
    monkeypatch.setattr(llm, "_in_local_batch_window", lambda now=None: True)
    monkeypatch.setattr(settings, "llm_remote_mode", "split")

    out = asyncio.run(llm.ollama_chat("s", "u", {}, caller_kind="scheduler"))
    assert out == {"leg": "remote"}
    assert calls == ["remote"]


def test_batch_fallback_off_never_touches_local(monkeypatch):
    calls = []

    async def fake_local(*a, **kw):
        calls.append("local")
        return {"leg": "local"}

    async def failing_remote(*a, **kw):
        calls.append("remote")
        raise RuntimeError("remote down")

    monkeypatch.setattr(settings, "gpu_lease_path", "")
    monkeypatch.setattr(llm, "_remote_enabled", lambda: True)
    monkeypatch.setattr(llm, "_ollama_chat_local", fake_local)
    monkeypatch.setattr(llm, "_remote_chat", failing_remote)
    monkeypatch.setattr(llm, "_in_local_batch_window", lambda now=None: False)
    monkeypatch.setattr(settings, "llm_remote_mode", "split")
    monkeypatch.setattr(settings, "llm_batch_local_fallback", False)
    monkeypatch.setattr(settings, "llm_batch_local_fallback_when_idle", False)

    with pytest.raises(RuntimeError):
        asyncio.run(llm.ollama_chat("s", "u", {}, caller_kind="scheduler"))
    assert calls == ["remote"]


def _idle_fallback_setup(monkeypatch, calls):
    async def fake_local(*a, **kw):
        calls.append("local")
        return {"leg": "local"}

    async def failing_remote(*a, **kw):
        calls.append("remote")
        raise RuntimeError("remote down")

    monkeypatch.setattr(llm, "_remote_enabled", lambda: True)
    monkeypatch.setattr(llm, "_ollama_chat_local", fake_local)
    monkeypatch.setattr(llm, "_remote_chat", failing_remote)
    monkeypatch.setattr(llm, "_in_local_batch_window", lambda now=None: False)
    monkeypatch.setattr(settings, "llm_remote_mode", "split")
    monkeypatch.setattr(settings, "llm_batch_local_fallback", False)
    monkeypatch.setattr(settings, "llm_batch_local_fallback_when_idle", True)


def test_failed_remote_falls_back_local_when_gpu_idle(lease, monkeypatch):
    calls = []
    _idle_fallback_setup(monkeypatch, calls)

    async def no_live():
        return False

    monkeypatch.setattr(llm, "_live_model_active", no_live)
    out = asyncio.run(llm.ollama_chat("s", "u", {}, caller_kind="scheduler"))
    assert out == {"leg": "local"}
    assert calls == ["remote", "local"]


def test_failed_remote_fails_while_live_model_busy(lease, monkeypatch):
    calls = []
    _idle_fallback_setup(monkeypatch, calls)

    async def live():
        return True

    monkeypatch.setattr(llm, "_live_model_active", live)
    with pytest.raises(RuntimeError):
        asyncio.run(llm.ollama_chat("s", "u", {}, caller_kind="scheduler"))
    assert calls == ["remote"]


def test_failed_remote_fails_when_lease_taken_mid_call(lease, monkeypatch):
    calls = []
    _idle_fallback_setup(monkeypatch, calls)

    async def failing_remote_then_render(*a, **kw):
        calls.append("remote")
        fcntl.flock(lease, fcntl.LOCK_EX)  # a render starts meanwhile
        raise RuntimeError("remote down")

    async def no_live():
        return False

    monkeypatch.setattr(llm, "_remote_chat", failing_remote_then_render)
    monkeypatch.setattr(llm, "_live_model_active", no_live)
    with pytest.raises(RuntimeError):
        asyncio.run(llm.ollama_chat("s", "u", {}, caller_kind="scheduler"))
    assert calls == ["remote"]


def test_live_call_skips_while_held(lease):
    fcntl.flock(lease, fcntl.LOCK_EX)
    with pytest.raises(llm.LiveLLMSkipped):
        asyncio.run(llm._ollama_chat_local("s", "u", {}, caller_kind="live"))
