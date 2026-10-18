"""SearXNG health probe (2026-10-18): a SearXNG whose engines are all
captcha-suspended stays container-"healthy" but returns 0 results, and
web_search silently drains the Brave quota. The probe must flag that,
and monitor_health must mail once per outage, on the 2nd dead probe."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from nobrainr import monitoring
from nobrainr.config import settings


def _client(payload=None, exc=None):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value=payload)
    client = MagicMock()
    client.get = AsyncMock(side_effect=exc, return_value=resp)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=client)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


async def _probe(payload=None, exc=None):
    with (
        patch.object(settings, "searxng_url", "http://searxng:8080"),
        patch.object(httpx, "AsyncClient", return_value=_client(payload, exc)),
    ):
        return await monitoring.check_search_backend()


async def test_results_mean_healthy():
    assert await _probe({"results": [{"url": "https://e.org"}]}) == []


async def test_zero_results_names_dead_engines():
    w = await _probe({"results": [], "unresponsive_engines": [["brave", "CAPTCHA"]]})
    assert len(w) == 1 and "0 results" in w[0] and "brave (CAPTCHA)" in w[0]


async def test_transport_error_is_a_warning():
    w = await _probe(exc=httpx.ConnectError("down"))
    assert w and "probe failed" in w[0]


async def test_unconfigured_is_silent():
    with patch.object(settings, "searxng_url", ""):
        assert await monitoring.check_search_backend() == []


@pytest.fixture
def quiet_monitor():
    with (
        patch.object(monitoring, "check_docker_health", AsyncMock(return_value={
            "unhealthy": [], "healthy": [], "restarting": [], "missing": [],
            "oom_killed": []})),
        patch.object(monitoring, "check_system_resources",
                     AsyncMock(return_value={"warnings": []})),
        patch.object(monitoring, "check_pipeline_anomalies", AsyncMock(return_value=[])),
        patch.object(settings, "monitoring_email_enabled", True),
        patch.object(settings, "monitoring_smtp_host", "mail"),
        patch.object(settings, "monitoring_smtp_to", "x@y"),
        patch.object(monitoring, "_searxng_dead_streak", 0),
        patch.object(monitoring, "_send_smtp") as smtp,
    ):
        yield smtp


async def test_alert_mails_once_on_second_dead_probe(quiet_monitor):
    dead = AsyncMock(return_value=["SearXNG returned 0 results"])
    with patch.object(monitoring, "check_search_backend", dead):
        out = await monitoring.monitor_health()
        assert out["searxng_ok"] is False
        quiet_monitor.assert_not_called()
        await monitoring.monitor_health()
        assert quiet_monitor.call_count == 1
        await monitoring.monitor_health()
        assert quiet_monitor.call_count == 1


async def test_recovery_resets_streak(quiet_monitor):
    seq = [["dead"], [], ["dead"], ["dead"]]
    with patch.object(monitoring, "check_search_backend",
                      AsyncMock(side_effect=seq)):
        for _ in seq:
            await monitoring.monitor_health()
    assert quiet_monitor.call_count == 1
