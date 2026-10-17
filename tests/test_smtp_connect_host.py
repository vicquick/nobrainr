"""SMTP: TCP goes to monitoring_smtp_connect_host, TLS name stays smtp_host."""

import socket

import pytest

from nobrainr import monitoring
from nobrainr.config import settings


@pytest.fixture
def smtp_env(monkeypatch):
    monkeypatch.setattr(settings, "monitoring_smtp_host", "mail.example.org")
    monkeypatch.setattr(settings, "monitoring_smtp_connect_host", "10.0.0.1")
    monkeypatch.setattr(settings, "monitoring_smtp_to", "a@example.org")
    monkeypatch.setattr(settings, "monitoring_smtp_from", "b@example.org")
    seen = []

    def fake_create_connection(addr, *a, **kw):
        seen.append(addr)
        raise ConnectionRefusedError("stop here")

    monkeypatch.setattr(socket, "create_connection", fake_create_connection)
    return seen


@pytest.mark.parametrize("port", [587, 465])
def test_socket_goes_to_connect_host(smtp_env, monkeypatch, port):
    monkeypatch.setattr(settings, "monitoring_smtp_port", port)
    with pytest.raises(ConnectionRefusedError):
        monitoring._send_smtp("s", "b")
    assert smtp_env == [("10.0.0.1", port)]


def test_empty_connect_host_uses_smtp_host(smtp_env, monkeypatch):
    monkeypatch.setattr(settings, "monitoring_smtp_connect_host", "")
    monkeypatch.setattr(settings, "monitoring_smtp_port", 587)
    with pytest.raises(ConnectionRefusedError):
        monitoring._send_smtp("s", "b")
    assert smtp_env == [("mail.example.org", 587)]
