"""LLM pause switch: while the pause file holds a future end time, nobrainr
makes NO chat-model call — non-live work parks (nothing dropped), live paths
skip their LLM step. Covers the remote leg too, because llm_remote_url may be
the same local llama-server."""

import asyncio
import datetime as dt

import pytest

from nobrainr.config import settings
from nobrainr.extraction import llm


@pytest.fixture
def pause(tmp_path, monkeypatch):
    path = tmp_path / "nobrainr-llm-pause.until"
    monkeypatch.setattr(settings, "llm_pause_path", str(path))
    monkeypatch.setattr(settings, "gpu_lease_path", "")
    monkeypatch.setattr(settings, "gpu_yield_poll_s", 0.01)
    monkeypatch.setattr(settings, "llm_pause_resume_jitter_s", 0.0)
    monkeypatch.setattr(llm, "_LLM_PAUSE_CACHE_S", 0.0)
    monkeypatch.setattr(llm, "_llm_pause_cache", (-1e9, None))

    def setze(delta_s: float | None = None, raw: str | None = None):
        if raw is None:
            end = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=delta_s)
            raw = end.isoformat()
        path.write_text(raw + "\n", encoding="utf-8")

    setze.path = path
    return setze


def test_missing_file_is_no_pause(pause):
    assert llm.llm_paused() is False
    assert llm.llm_pause_snapshot()["until"] is None


def test_future_end_pauses(pause):
    pause(600)
    assert llm.llm_paused() is True
    assert llm.llm_pause_snapshot()["until"] is not None


def test_past_end_is_no_pause(pause):
    pause(-5)
    assert llm.llm_paused() is False


def test_empty_or_garbage_is_no_pause(pause):
    pause(raw="")
    assert llm.llm_paused() is False
    pause(raw="morgen frueh")
    assert llm.llm_paused() is False


def test_empty_path_disables(pause, monkeypatch):
    pause(600)
    monkeypatch.setattr(settings, "llm_pause_path", "")
    assert llm.llm_paused() is False


def test_naive_time_read_in_night_window_tz(pause, monkeypatch):
    monkeypatch.setattr(settings, "night_window_tz", "Europe/Berlin")
    from zoneinfo import ZoneInfo
    end = dt.datetime.now(ZoneInfo("Europe/Berlin")) + dt.timedelta(minutes=10)
    pause(raw=end.replace(tzinfo=None).isoformat(timespec="seconds"))
    until = llm.llm_pause_until()
    assert until is not None
    assert abs(until - end.timestamp()) < 2


def test_far_end_is_clamped(pause, monkeypatch):
    monkeypatch.setattr(settings, "llm_pause_max_h", 1.0)
    pause(10 * 24 * 3600)
    until = llm.llm_pause_until()
    import time
    assert until is not None and until <= time.time() + 3600 + 1


def _fakes(monkeypatch, calls):
    async def fake_local(*a, **kw):
        calls.append("local")
        return {"leg": "local"}

    async def fake_remote(*a, **kw):
        calls.append("remote")
        return {"leg": "remote"}

    monkeypatch.setattr(llm, "_ollama_chat_local", fake_local)
    monkeypatch.setattr(llm, "_remote_chat", fake_remote)


def test_live_chat_skips_without_any_call(pause, monkeypatch):
    calls = []
    _fakes(monkeypatch, calls)
    pause(600)
    before = llm.llm_pause_snapshot()["live_skipped_total"]
    with pytest.raises(llm.LiveLLMSkipped):
        asyncio.run(llm.ollama_chat("s", "u", {}, caller_kind="live"))
    assert calls == []
    assert llm.llm_pause_snapshot()["live_skipped_total"] == before + 1


def test_side_chat_and_generate_skip(pause, monkeypatch):
    calls = []
    _fakes(monkeypatch, calls)
    pause(600)
    with pytest.raises(llm.LiveLLMSkipped):
        asyncio.run(llm.side_chat("s", "u", {}, model="gemma", timeout=1.0))
    with pytest.raises(llm.LiveLLMSkipped):
        asyncio.run(llm.ollama_generate("p"))
    assert calls == []


@pytest.mark.parametrize("mode", ["off", "split"])
def test_batch_parks_then_resumes_on_both_legs(pause, monkeypatch, mode):
    calls = []
    _fakes(monkeypatch, calls)
    monkeypatch.setattr(settings, "llm_remote_mode", mode)
    monkeypatch.setattr(settings, "llm_remote_url", "http://llama-server:8080" if mode != "off" else "")
    monkeypatch.setattr(settings, "llm_local_batch_hours", "")
    pause(600)

    async def lauf():
        task = asyncio.create_task(llm.ollama_chat("s", "u", {}, caller_kind="scheduler"))
        await asyncio.sleep(0.15)
        assert calls == [], "no leg may be called while paused"
        assert not task.done()
        assert llm.llm_pause_snapshot()["parked_now"] == 1
        pause.path.write_text("", encoding="utf-8")  # operator lifts the pause
        res = await asyncio.wait_for(task, 2.0)
        assert llm.llm_pause_snapshot()["parked_now"] == 0
        return res

    res = asyncio.run(lauf())
    assert res["leg"] in ("local", "remote")
    assert len(calls) >= 1


def test_no_pause_no_delay(pause, monkeypatch):
    calls = []
    _fakes(monkeypatch, calls)
    monkeypatch.setattr(settings, "llm_remote_mode", "off")
    res = asyncio.run(asyncio.wait_for(llm.ollama_chat("s", "u", {}, caller_kind="scheduler"), 1.0))
    assert res == {"leg": "local"}
