"""Search preview: full-text matches at once, the full search replaces them.

Measured 2026-11-10 on 147k memories: ranking every match of a frequent word
took seconds ("bimavo" 5.1s, "der" 5.7s, "docker" 1.0s) because ts_rank
rebuilds the tsvector per row. fts_memories ranks a capped candidate set
(0.5s / 0.46s / 0.38s). The preview endpoint must stay free of side effects.
"""

from __future__ import annotations

import asyncio
import inspect
import json

from starlette.requests import Request


def _anfrage(query: str = "") -> Request:
    return Request({"type": "http", "method": "GET", "path": "/api/search/preview",
                    "query_string": query.encode(), "headers": []})


class _Verbindung:
    def __init__(self, zeilen):
        self.zeilen = zeilen
        self.aufrufe: list[tuple] = []

    async def fetch(self, sql, *params):
        self.aufrufe.append((sql, params))
        return self.zeilen

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Pool:
    def __init__(self, verbindung):
        self.verbindung = verbindung

    def acquire(self):
        return self.verbindung


def _fts(monkeypatch, **kw):
    from nobrainr.db import queries

    verbindung = _Verbindung([])

    async def get_pool():
        return _Pool(verbindung)

    monkeypatch.setattr(queries, "get_pool", get_pool)
    asyncio.run(queries.fts_memories("bimavo deploy", **kw))
    (sql, params), = verbindung.aufrufe
    return sql, params


def test_ranking_runs_on_a_capped_candidate_set(monkeypatch):
    sql, params = _fts(monkeypatch, limit=50)
    innen = sql[sql.index("FROM ("):sql.index(") AS kandidaten")]
    assert "ts_rank" not in innen, "the candidate cut must not rebuild a tsvector per match"
    assert "ORDER BY importance DESC, created_at DESC" in innen and "LIMIT $3" in innen
    assert "ts_rank" in sql[:sql.index("FROM (")] and sql.rstrip().endswith("LIMIT $2")
    assert params == ("bimavo deploy", 50, 300)


def test_candidate_set_grows_with_the_limit_and_filters_are_bound(monkeypatch):
    sql, params = _fts(monkeypatch, limit=200, tags=["to:vpsfixer"], category="agent-comm", source_machine="bimavo")
    assert params[:3] == ("bimavo deploy", 200, 800)
    assert "tags && $4::text[]" in sql and "category = $5" in sql and "source_machine = $6" in sql
    assert params[4:] == ("agent-comm", "bimavo")
    assert "to:vpsfixer" not in sql and "agent-comm" not in sql, "filter values are bound, never interpolated"


def test_superseded_expired_and_cold_memories_are_left_out(monkeypatch):
    sql, _ = _fts(monkeypatch)
    assert "tier < 3" in sql and "superseded_by IS NULL" in sql and "valid_to IS NULL OR valid_to > now()" in sql


def test_preview_passes_filters_and_has_no_side_effects(monkeypatch):
    from nobrainr.dashboard import api

    aufrufe: list[tuple] = []

    async def fts_memories(q, *, limit=50, tags=None, category=None, source_machine=None):
        aufrufe.append((q, limit, tags, category, source_machine))
        return [{"id": "a", "quality_score": 0.9}, {"id": "b", "quality_score": 0.2}, {"id": "c", "quality_score": None}]

    monkeypatch.setattr(api.queries, "fts_memories", fts_memories)

    async def lauf():
        alle = await api.api_search_preview(_anfrage("q=traefik&tags=infra,vpn&category=insight&source_machine=bimavo&limit=20"))
        gut = await api.api_search_preview(_anfrage("q=traefik&min_quality=0.5"))
        leer = await api.api_search_preview(_anfrage("q=%20"))
        kaputt = await api.api_search_preview(_anfrage("q=x&limit=viele"))
        return alle, gut, leer, kaputt

    alle, gut, leer, kaputt = asyncio.run(lauf())
    assert aufrufe[0] == ("traefik", 20, ["infra", "vpn"], "insight", "bimavo")
    assert [m["id"] for m in json.loads(alle.body)] == ["a", "b", "c"]
    assert [m["id"] for m in json.loads(gut.body)] == ["a"]
    assert json.loads(leer.body) == [] and len(aufrufe) == 2, "no query, no database call"
    assert kaputt.status_code == 400

    quelle = inspect.getsource(api.api_search_preview)
    code = quelle.split('"""', 2)[2]  # body without the docstring
    for verboten in ("access_count", "search_trace_id", "_log_auto_negative_outcomes", "embed_text", "rerank"):
        assert verboten not in code, f"a preview must not {verboten}"


def test_preview_route_is_registered():
    from nobrainr.dashboard import api

    assert 'Route("/api/search/preview", api_search_preview)' in inspect.getsource(api)
