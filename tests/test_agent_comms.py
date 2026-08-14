"""Agent-comms layer (2026-08-15): fast-lane defer, msg routing filter,
presence upsert invariants, msg_wait pool isolation."""

from __future__ import annotations

import inspect


def test_store_supports_defer_extraction():
    from nobrainr.services import memory

    sig = inspect.signature(memory.store_memory_with_extraction)
    assert "defer_extraction" in sig.parameters
    src = inspect.getsource(memory.store_memory_with_extraction)
    assert "not defer_extraction" in src


def test_msg_send_uses_fast_lane_and_notify():
    from nobrainr.mcp import server

    src = inspect.getsource(server.msg_send)
    assert "defer_extraction=True" in src
    assert "skip_dedup=True" in src
    assert "pg_notify" in src
    assert '"agent-msg"' in src


def test_msg_wait_filters_recipient_and_isolates_connection():
    from nobrainr.mcp import server

    src = inspect.getsource(server.msg_wait)
    # 'all' broadcast + addressed delivery only
    assert '(agent, "all")' in src
    # Dedicated connection, never the app pool — LISTEN holds it for
    # the full wait and must not starve other queries.
    assert "asyncpg.connect" in src
    assert "get_pool" not in src
    # Hard timeout ceiling
    # 50s ceiling: MCP clients/proxy kill calls ~60s — a 120s wait died
    # as a raw client timeout (live-tested 2026-08-15). Loop to wait longer.
    assert "min(timeout_s, 50)" in src


def test_presence_upsert_refreshes_last_seen():
    from nobrainr.db import queries

    src = inspect.getsource(queries.upsert_agent_presence)
    assert "ON CONFLICT (agent, machine) DO UPDATE" in src
    assert "last_seen = now()" in src
