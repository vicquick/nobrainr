"""/api/graph?format=compact — the graph as parallel arrays.

Measured 2026-11-10 on the live graph (3,498 nodes, 63,167 edges): the object
form is 12.4 MB, 8.6 MB of it the three UUIDs each edge carries. The compact
form must carry the same graph in a fraction of that, and the object form must
stay the default for every other caller.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid

from starlette.requests import Request


def _anfrage(query: str = "") -> Request:
    return Request({"type": "http", "method": "GET", "path": "/api/graph",
                    "query_string": query.encode(), "headers": []})


def _graph(n_nodes: int = 40, n_edges: int = 400) -> dict:
    ids = [str(uuid.UUID(int=i + 1)) for i in range(n_nodes)]
    nodes = [{"data": {"id": nid, "label": f"Knoten {i}", "type": "technology" if i % 2 else "person",
                       "description": None if i % 5 == 0 else f"Beschreibung {i}",
                       "mention_count": i * 3, "x": -3844.303526652706 + i, "y": 1899.6662701659134 - i,
                       "community": i % 4}} for i, nid in enumerate(ids)]
    edges = [{"data": {"id": str(uuid.UUID(int=10_000 + k)), "source": ids[k % n_nodes],
                       "target": ids[(k * 7 + 3) % n_nodes], "label": ("depends_on", "uses", "part_of")[k % 3],
                       "confidence": 0.8 if k % 2 else None}} for k in range(n_edges)]
    return {"nodes": nodes, "edges": edges}


def _entpacken(c: dict) -> dict:
    """Mirror of expandCompactGraph in dashboard/src/composables/useGraph.ts."""
    n, e = c["nodes"], c["edges"]
    nodes = [{"id": n["id"][i], "label": n["label"][i], "type": n["type"][i], "description": n["description"][i],
              "mention_count": n["mention_count"][i], "x": n["x"][i], "y": n["y"][i],
              "community": n["community"][i]} for i in range(len(n["id"]))]
    edges = [(n["id"][s], n["id"][e["target"][i]], c["edge_labels"][e["label"][i]], e["confidence"][i])
             for i, s in enumerate(e["source"])]
    return {"nodes": nodes, "edges": edges}


def test_compact_form_carries_the_same_graph():
    from nobrainr.dashboard import api

    voll = _graph()
    zurueck = _entpacken(api.graph_kompakt(voll))

    assert [k["id"] for k in zurueck["nodes"]] == [n["data"]["id"] for n in voll["nodes"]]
    for kompakt, original in zip(zurueck["nodes"], voll["nodes"]):
        d = original["data"]
        for feld in ("label", "type", "description", "mention_count", "community"):
            assert kompakt[feld] == d[feld], feld
        assert abs(kompakt["x"] - d["x"]) <= 0.05 and abs(kompakt["y"] - d["y"]) <= 0.05
    assert zurueck["edges"] == [(e["data"]["source"], e["data"]["target"], e["data"]["label"], e["data"]["confidence"])
                                for e in voll["edges"]]


def test_compact_form_is_a_fraction_of_the_object_form():
    from nobrainr.dashboard import api

    voll = _graph(n_nodes=300, n_edges=6000)
    gross = len(json.dumps(voll))
    klein = len(json.dumps(api.graph_kompakt(voll), separators=(",", ":")))
    assert klein * 5 < gross, f"{klein} vs {gross}"


def test_edges_to_unknown_nodes_are_dropped_and_missing_fields_get_defaults():
    from nobrainr.dashboard import api

    a, b = str(uuid.UUID(int=1)), str(uuid.UUID(int=2))
    data = {"nodes": [{"data": {"id": a, "label": "A", "type": "t"}}, {"data": {"id": b, "label": "B", "type": "t"}}],
            "edges": [{"data": {"id": "x", "source": a, "target": b, "label": None, "confidence": 0.123456}},
                      {"data": {"id": "y", "source": a, "target": str(uuid.UUID(int=99)), "label": "uses"}}]}
    c = api.graph_kompakt(data)
    assert c["format"] == "compact-1"
    assert c["edges"] == {"source": [0], "target": [1], "label": [0], "confidence": [0.12]}
    assert c["edge_labels"] == [""]
    assert c["nodes"]["x"] == [0.0, 0.0] and c["nodes"]["community"] == [-1, -1] and c["nodes"]["mention_count"] == [0, 0]


def _cache(tmp_path, monkeypatch):
    from nobrainr.dashboard import api

    pfad = tmp_path / "graph.json"
    pfad.write_text(json.dumps(_graph()))
    monkeypatch.setattr(api, "_GRAPH_CACHE_PATH", str(pfad))
    monkeypatch.setattr(api, "_graph_im_hintergrund_erneuern", lambda: None)
    return api, pfad


def test_endpoint_serves_object_form_by_default_and_compact_on_request(tmp_path, monkeypatch):
    api, pfad = _cache(tmp_path, monkeypatch)

    voll = json.loads(asyncio.run(api.api_graph(_anfrage())).body)
    assert "format" not in voll and voll["nodes"][0]["data"]["label"] == "Knoten 0"

    kompakt = json.loads(asyncio.run(api.api_graph(_anfrage("format=compact"))).body)
    assert kompakt["format"] == "compact-1"
    assert _entpacken(kompakt)["nodes"][0]["label"] == "Knoten 0"
    assert os.path.exists(str(pfad) + ".compact"), "kept beside the cache for the next caller"


def test_compact_file_is_rebuilt_when_the_cache_is_newer(tmp_path, monkeypatch):
    api, pfad = _cache(tmp_path, monkeypatch)

    erste = json.loads(asyncio.run(api.api_graph(_anfrage("format=compact"))).body)
    assert len(erste["nodes"]["id"]) == 40

    pfad.write_text(json.dumps(_graph(n_nodes=12, n_edges=30)))
    spaeter = time.time() + 5
    os.utime(pfad, (spaeter, spaeter))
    zweite = json.loads(asyncio.run(api.api_graph(_anfrage("format=compact"))).body)
    assert len(zweite["nodes"]["id"]) == 12, "a stale compact file must not outlive a new layout"


def test_broken_compact_file_is_ignored(tmp_path, monkeypatch):
    api, pfad = _cache(tmp_path, monkeypatch)
    kaputt = tmp_path / "graph.json.compact"
    kaputt.write_text('{"format":"compact-1","nodes":')
    spaeter = time.time() + 5
    os.utime(kaputt, (spaeter, spaeter))
    antwort = json.loads(asyncio.run(api.api_graph(_anfrage("format=compact"))).body)
    assert len(antwort["nodes"]["id"]) == 40
