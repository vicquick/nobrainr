"""GET /api/memories?tags=to:X&exclude_tags=read:X returns only unread mail.

Measured 27.11.2026: the inbox readers (agent-wake daemon limit 20, prompt
hook limit 10) fetched the newest rows tagged to:<name> and dropped the read
ones client-side. bimavo-ceo had >200 such rows in 48 h, so an unread message
at row 191 was never shown by the hook and never nudged by the daemon. With
exclude_tags the database drops read rows before the limit.
"""

from __future__ import annotations

import json
import re
from unittest.mock import AsyncMock, patch

from starlette.requests import Request

from nobrainr.db import queries


class _Verbindung:
    def __init__(self):
        self.aufrufe: list[tuple[str, tuple]] = []

    async def fetch(self, sql, *params):
        self.aufrufe.append((sql, params))
        return []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Pool:
    def __init__(self, verbindung):
        self.verbindung = verbindung

    def acquire(self):
        return self.verbindung


def _platzhalter(sql: str) -> set[int]:
    return {int(n) for n in re.findall(r"\$(\d+)", sql)}


async def test_exclude_tags_binds_its_own_placeholder_before_limit():
    verbindung = _Verbindung()
    with patch.object(queries, "get_pool", AsyncMock(return_value=_Pool(verbindung))):
        await queries.query_memories(
            tags=["to:bimavo-ceo"],
            exclude_tags=["read:bimavo-ceo", "undelivered:bimavo-ceo"],
            limit=20,
        )
    (sql, params), = verbindung.aufrufe
    assert "NOT (COALESCE(tags, '{}') && $2::text[])" in sql
    assert params[0] == ["to:bimavo-ceo"]
    assert "read:bimavo-ceo" in params[1] and "undelivered:bimavo-ceo" in params[1]
    assert params[-2:] == (20, 0)
    assert _platzhalter(sql) == set(range(1, len(params) + 1))


async def test_exclude_tags_combines_with_exclude_categories():
    verbindung = _Verbindung()
    with patch.object(queries, "get_pool", AsyncMock(return_value=_Pool(verbindung))):
        await queries.query_memories(
            tags=["to:x"], exclude_categories=["debugging"], exclude_tags=["read:x"], limit=5,
        )
    (sql, params), = verbindung.aufrufe
    assert "category <> ALL($1::text[])" in sql
    assert "tags && $2::text[]" in sql
    assert "NOT (COALESCE(tags, '{}') && $3::text[])" in sql
    assert _platzhalter(sql) == set(range(1, len(params) + 1))


async def test_without_exclude_tags_the_sql_is_unchanged():
    verbindung = _Verbindung()
    with patch.object(queries, "get_pool", AsyncMock(return_value=_Pool(verbindung))):
        await queries.query_memories(tags=["to:x"], limit=5)
    (sql, params), = verbindung.aufrufe
    assert "NOT (" not in sql
    assert params[-2:] == (5, 0)


def _anfrage(query: str) -> Request:
    return Request({"type": "http", "method": "GET", "path": "/api/memories",
                    "query_string": query.encode(), "headers": []})


async def test_api_passes_exclude_tags_through(monkeypatch):
    from nobrainr.dashboard import api

    gesehen: list[dict] = []

    async def query_memories(**kw):
        gesehen.append(kw)
        return []

    monkeypatch.setattr(api.queries, "query_memories", query_memories)

    antwort = await api.api_memories(_anfrage("tags=to:x&limit=20"))
    assert json.loads(antwort.body) == []
    assert gesehen[-1]["exclude_tags"] is None

    await api.api_memories(_anfrage("tags=to:x&exclude_tags=read:x,%20undelivered:x,&limit=20"))
    assert gesehen[-1]["exclude_tags"] == ["read:x", "undelivered:x"]


# ── add_tags: append in SQL, no read-modify-write race ─────────────────────
# 27.11.2026: on a message to 12 agents, read:bimavo-cto was set 09:00:13 and
# gone 09:00:14, read:bimavo-vpsfixer set 09:00:26 and gone 09:02:39 — another
# recipient POSTed its stale full tag list. agent-wake then nudged again.

class _ZeilenVerbindung(_Verbindung):
    async def fetchrow(self, sql, *params):
        self.aufrufe.append((sql, params))
        return None

    def transaction(self):
        return self


async def test_add_tags_appends_in_sql_against_the_current_row():
    verbindung = _ZeilenVerbindung()
    with patch.object(queries, "get_pool", AsyncMock(return_value=_Pool(verbindung))):
        await queries.update_memory(
            "00000000-0000-0000-0000-000000000001", add_tags=["read:bimavo-cto"])
    (sql, params), = verbindung.aufrufe
    assert "tags = COALESCE(tags, '{}') || ARRAY(SELECT DISTINCT t FROM unnest($1::text[]) t" in sql
    assert "WHERE t <> ALL(COALESCE(tags, '{}'))" in sql
    assert "read:bimavo-cto" in params[0]
    assert _platzhalter(sql) == set(range(1, len(params) + 1))


async def test_tags_replace_wins_over_add_tags():
    verbindung = _ZeilenVerbindung()
    with patch.object(queries, "get_pool", AsyncMock(return_value=_Pool(verbindung))):
        await queries.update_memory(
            "00000000-0000-0000-0000-000000000001", tags=["a"], add_tags=["b"])
    (sql, params), = verbindung.aufrufe
    assert "unnest(" not in sql and "tags = $1" in sql


def _post(body: dict) -> Request:
    roh = json.dumps(body).encode()

    async def empfangen():
        return {"type": "http.request", "body": roh, "more_body": False}

    return Request({"type": "http", "method": "POST",
                    "path": "/api/memories/00000000-0000-0000-0000-000000000001",
                    "path_params": {"memory_id": "00000000-0000-0000-0000-000000000001"},
                    "query_string": b"", "headers": []}, empfangen)


async def test_api_update_passes_add_tags_through(monkeypatch):
    from nobrainr.dashboard import api

    gesehen: list[dict] = []

    async def update_memory(memory_id, **kw):
        gesehen.append(kw)
        return {"id": memory_id}

    monkeypatch.setattr(api.queries, "update_memory", update_memory)

    await api.api_memory_update(_post({"add_tags": ["read:x", " ", 3]}))
    assert gesehen[-1]["add_tags"] == ["read:x"] and gesehen[-1]["tags"] is None

    await api.api_memory_update(_post({"tags": ["a", "b"]}))
    assert gesehen[-1]["add_tags"] is None and gesehen[-1]["tags"] == ["a", "b"]
