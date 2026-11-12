"""Dashboard API response times (measured 2026-11-09, fixed in the same change).

Each test pins one of the fixes so it cannot quietly regress:

* /api/recall must use the expression idx_memories_content_fts covers
  (it was a sequential scan over all memories: 10-15s on 147k rows)
* /api/stats is computed once per 30s, not once per caller (2s, 151 calls/h)
* /api/tags is capped and searchable (the full list was 1.4 MB)
* the interactive search gives up on a slow reranker instead of waiting
  (same query: 3s, then 49s on a busy GPU)
* /api/graph serves an old cache at once and refreshes in the background
  (a cold call took 60s)
* JSON answers of /api/* are gzipped; streaming endpoints and MCP are not
"""

from __future__ import annotations

import asyncio
import gzip
import inspect
import json
import os
import time

from starlette.requests import Request


def _anfrage(pfad: str, query: str = "") -> Request:
    return Request({"type": "http", "method": "GET", "path": pfad,
                    "query_string": query.encode(), "headers": []})


def _json(antwort) -> object:
    return json.loads(antwort.body)


# --- /api/recall ------------------------------------------------------------


def test_recall_uses_the_indexed_fts_expression():
    from nobrainr.dashboard import api
    from nobrainr.db import queries

    assert "queries.fts_memories(" in inspect.getsource(api.api_recall)
    ausdruck = ("to_tsvector('simple', nb_unaccent(content || ' ' || COALESCE(search_keys, '') "
                "|| ' ' || COALESCE(fts_context, '')))")
    assert queries._FTS_AUSDRUCK == ausdruck, "must be written exactly like idx_memories_content_fts"
    quelle = inspect.getsource(queries.fts_memories)
    assert quelle.count("{_FTS_AUSDRUCK}") == 2, "WHERE and ts_rank must both use the index expression"
    assert "plainto_tsquery('simple', nb_unaccent($1))" in quelle
    assert "'english'" not in quelle, "to_tsvector('english', content) has no index -> sequential scan"


# --- /api/stats -------------------------------------------------------------


def test_stats_is_computed_once_within_the_ttl(monkeypatch):
    from nobrainr.dashboard import api

    laeufe = {"n": 0}

    async def get_stats():
        laeufe["n"] += 1
        return {"total_memories": laeufe["n"]}

    async def get_feedback_stats():
        return {"feedback": 0}

    monkeypatch.setattr(api.queries, "get_stats", get_stats)
    monkeypatch.setattr(api.queries, "get_feedback_stats", get_feedback_stats)
    monkeypatch.setattr(api, "_stats_cache", {"ts": 0.0, "data": None})

    async def lauf():
        erste = await asyncio.gather(*(api.api_stats(_anfrage("/api/stats")) for _ in range(5)))
        zweite = await api.api_stats(_anfrage("/api/stats"))
        frisch = await api.api_stats(_anfrage("/api/stats", "refresh=true"))
        return erste, zweite, frisch

    erste, zweite, frisch = asyncio.run(lauf())
    assert {_json(a)["total_memories"] for a in erste} == {1}, "five concurrent callers, one computation"
    assert _json(zweite)["total_memories"] == 1
    assert _json(frisch)["total_memories"] == 2, "refresh=true bypasses the cache"
    assert laeufe["n"] == 2


def test_stats_recomputes_after_the_ttl(monkeypatch):
    from nobrainr.dashboard import api

    laeufe = {"n": 0}

    async def get_stats():
        laeufe["n"] += 1
        return {"n": laeufe["n"]}

    async def get_feedback_stats():
        return {}

    monkeypatch.setattr(api.queries, "get_stats", get_stats)
    monkeypatch.setattr(api.queries, "get_feedback_stats", get_feedback_stats)
    monkeypatch.setattr(api, "_stats_cache", {"ts": time.monotonic() - api._STATS_TTL_S - 1, "data": {"n": 0}})

    antwort = asyncio.run(api.api_stats(_anfrage("/api/stats")))
    assert _json(antwort)["n"] == 1


# --- /api/tags --------------------------------------------------------------


def test_tags_default_is_capped_and_parameters_reach_the_query(monkeypatch):
    from nobrainr.dashboard import api

    aufrufe: list[tuple] = []

    async def get_all_tags(limit=None, q=None):
        aufrufe.append((limit, q))
        return [{"tag": "docker", "cnt": 3}]

    monkeypatch.setattr(api.queries, "get_all_tags", get_all_tags)
    monkeypatch.setattr(api, "_tags_cache", {})

    async def lauf():
        await api.api_tags(_anfrage("/api/tags"))
        await api.api_tags(_anfrage("/api/tags"))  # cache hit
        await api.api_tags(_anfrage("/api/tags", "q=dock&limit=50"))
        await api.api_tags(_anfrage("/api/tags", "limit=0"))
        return await api.api_tags(_anfrage("/api/tags", "limit=viele"))

    kaputt = asyncio.run(lauf())
    assert aufrufe == [(api._TAGS_STANDARD, None), (50, "dock"), (None, None)]
    assert kaputt.status_code == 400


def test_tag_search_escapes_like_wildcards():
    from nobrainr.db import queries

    quelle = inspect.getsource(queries.get_all_tags)
    assert "ILIKE $1" in quelle and "LIMIT $2" in quelle, "search text and limit are bound, never interpolated"
    assert '.replace("%", "\\\\%")' in quelle and '.replace("_", "\\\\_")' in quelle


# --- interactive search: reranker budget -------------------------------------


def _treffer(n: int) -> list[dict]:
    return [{"id": str(i), "content": f"m{i}"} for i in range(n)]


def test_slow_reranker_returns_the_fused_order(monkeypatch):
    from nobrainr.dashboard import api
    from nobrainr.services import reranker

    async def langsam(q, results, *, limit=10):
        await asyncio.sleep(5)
        return list(reversed(results))

    monkeypatch.setattr(reranker, "rerank", langsam)
    monkeypatch.setattr(api, "_SUCHE_RERANK_BUDGET_S", 0.05)

    vorher = _treffer(30)
    t0 = time.monotonic()
    nachher = asyncio.run(api._rerank_im_budget("q", vorher, 30))
    assert time.monotonic() - t0 < 2
    assert nachher == vorher


def test_reranker_sees_only_the_head_and_the_tail_keeps_its_order(monkeypatch):
    from nobrainr.dashboard import api
    from nobrainr.services import reranker

    gesehen: list[int] = []

    async def umgekehrt(q, results, *, limit=10):
        gesehen.append(len(results))
        return list(reversed(results))[:limit]

    monkeypatch.setattr(reranker, "rerank", umgekehrt)

    vorher = _treffer(30)
    nachher = asyncio.run(api._rerank_im_budget("q", vorher, 30))
    kopf = api._SUCHE_RERANK_KOPF
    assert gesehen == [kopf]
    assert [m["id"] for m in nachher[:kopf]] == [str(i) for i in reversed(range(kopf))]
    assert nachher[kopf:] == vorher[kopf:]
    assert len(asyncio.run(api._rerank_im_budget("q", vorher, 5))) == 5


def test_failing_reranker_returns_the_list_unchanged(monkeypatch):
    from nobrainr.dashboard import api
    from nobrainr.services import reranker

    async def kaputt(q, results, *, limit=10):
        raise RuntimeError("reranker down")

    monkeypatch.setattr(reranker, "rerank", kaputt)
    vorher = _treffer(8)
    assert asyncio.run(api._rerank_im_budget("q", vorher, 8)) == vorher


# --- /api/graph -------------------------------------------------------------


def _graph_cache(tmp_path, monkeypatch, alter_s: float):
    from nobrainr.dashboard import api

    pfad = tmp_path / "graph.json"
    pfad.write_text(json.dumps({"nodes": [{"data": {"id": "a"}}], "edges": []}))
    alt = time.time() - alter_s
    os.utime(pfad, (alt, alt))
    monkeypatch.setattr(api, "_GRAPH_CACHE_PATH", str(pfad))
    erneuert = {"n": 0}
    monkeypatch.setattr(api, "_graph_im_hintergrund_erneuern", lambda: erneuert.__setitem__("n", erneuert["n"] + 1))
    return api, erneuert


def test_stale_graph_cache_is_served_at_once_and_refreshed_in_the_background(tmp_path, monkeypatch):
    api, erneuert = _graph_cache(tmp_path, monkeypatch, alter_s=3 * 86400)
    antwort = asyncio.run(api.api_graph(_anfrage("/api/graph")))
    assert antwort.status_code == 200
    assert _json(antwort)["nodes"][0]["data"]["id"] == "a"
    assert erneuert["n"] == 1


def test_fresh_graph_cache_is_served_without_a_refresh(tmp_path, monkeypatch):
    api, erneuert = _graph_cache(tmp_path, monkeypatch, alter_s=60)
    antwort = asyncio.run(api.api_graph(_anfrage("/api/graph")))
    assert _json(antwort)["edges"] == []
    assert erneuert["n"] == 0


# --- gzip -------------------------------------------------------------------


def test_gzip_applies_to_api_json_only():
    from nobrainr.dashboard.app import ApiGzip

    for pfad in ("/api/graph", "/api/tags", "/api/memories", "/api/stats"):
        assert ApiGzip.gilt_fuer(pfad), pfad
    for pfad in ("/api/events", "/api/chat", "/api/tts", "/api/transcribe",
                 "/mcp", "/sse", "/messages/abc", "/", "/apix"):
        assert not ApiGzip.gilt_fuer(pfad), pfad


def test_gzip_compresses_a_large_api_answer_and_leaves_precompressed_ones_alone():
    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.responses import JSONResponse, Response
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from nobrainr.dashboard.app import ApiGzip

    gross = {"tags": ["x" * 20] * 500}
    fertig = gzip.compress(json.dumps({"vor": "komprimiert" * 300}).encode())

    async def tags(request):
        return JSONResponse(gross)

    async def galaxy(request):
        return Response(fertig, media_type="application/json", headers={"Content-Encoding": "gzip"})

    async def mcp(request):
        return JSONResponse(gross)

    app = Starlette(routes=[Route("/api/tags", tags), Route("/api/galaxy", galaxy), Route("/mcp", mcp)],
                    middleware=[Middleware(ApiGzip)])
    with TestClient(app) as client:
        a = client.get("/api/tags", headers={"Accept-Encoding": "gzip"})
        assert a.headers.get("content-encoding") == "gzip"
        assert a.json() == gross
        assert int(a.headers["content-length"]) < len(json.dumps(gross)) // 5

        b = client.get("/api/galaxy", headers={"Accept-Encoding": "gzip"})
        assert b.headers.get("content-encoding") == "gzip"
        assert b.json()["vor"].startswith("komprimiert"), "must not be compressed twice"

        c = client.get("/mcp", headers={"Accept-Encoding": "gzip"})
        assert c.headers.get("content-encoding") is None

        d = client.get("/api/tags", headers={"Accept-Encoding": "identity"})
        assert d.headers.get("content-encoding") is None
