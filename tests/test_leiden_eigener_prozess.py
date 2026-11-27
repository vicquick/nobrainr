"""Community detection must not freeze the event loop (vpsfixer 27.11.2026).

leidenalg holds the GIL in find_partition. Run through run_in_executor(None)
— a thread — Leiden on the live graph (221k nodes, 149 s) left nobrainr
unhealthy for ~2.5 min: health checks timed out, MCP connections closed.
Measured in the container: 11.9 s of Leiden starved a neighbour thread 8.43 s.
The kernels now run in a spawned process (_im_eigenen_prozess).
"""

from __future__ import annotations

import ast
import asyncio
import importlib
import inspect
import os
import random
import time

import pytest

from nobrainr.services import communities, graph_algo


def _graph(n: int, m: int) -> tuple[list[str], list[tuple[str, str, float]]]:
    rnd = random.Random(7)
    nodes = [f"n{i}" for i in range(n)]
    edges = []
    for _ in range(m):
        a, b = rnd.randrange(n), rnd.randrange(n)
        if a != b:
            edges.append((nodes[a], nodes[b], 1.0))
    return nodes, edges


async def _groesste_pause(coro) -> tuple[object, float]:
    """Await coro while a ticker on the same loop records the longest gap."""
    pausen: list[float] = []
    fertig = asyncio.Event()

    async def ticker():
        letzte = time.monotonic()
        while not fertig.is_set():
            await asyncio.sleep(0.01)
            jetzt = time.monotonic()
            pausen.append(jetzt - letzte)
            letzte = jetzt

    t = asyncio.create_task(ticker())
    try:
        ergebnis = await coro
    finally:
        fertig.set()
        await t
    return ergebnis, max(pausen or [0.0])


async def test_kernel_runs_in_another_process():
    pid = await communities._im_eigenen_prozess(os.getpid)
    assert pid != os.getpid()


async def test_event_loop_stays_responsive_during_leiden():
    nodes, edges = _graph(5_000, 20_000)
    t0 = time.monotonic()
    teile, pause = await _groesste_pause(
        communities._im_eigenen_prozess(graph_algo.run_leiden, nodes, edges, resolution=1.0)
    )
    dauer = time.monotonic() - t0
    assert sum(len(c) for c in teile) == len(nodes)
    # spawn start + pickling happen in the parent too; the point is that the
    # loop is never frozen for the run itself (thread version: ~70 % of it)
    assert pause < 0.5, f"loop stood {pause:.2f}s during a {dauer:.1f}s Leiden run"


async def test_timeout_kills_child_without_blocking():
    t0 = time.monotonic()
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(communities._im_eigenen_prozess(time.sleep, 30), timeout=2)
    assert time.monotonic() - t0 < 8, "cancel waited for the child instead of killing it"


async def test_import_error_reaches_caller_for_louvain_fallback():
    with pytest.raises(ImportError):
        await communities._im_eigenen_prozess(importlib.import_module, "gibt_es_nicht_xyz")


def test_kernel_module_imports_nothing_from_nobrainr():
    baum = ast.parse(inspect.getsource(graph_algo))
    module = {a.name for n in ast.walk(baum) if isinstance(n, ast.Import) for a in n.names}
    module |= {n.module or "" for n in ast.walk(baum) if isinstance(n, ast.ImportFrom)}
    assert not any(m.split(".")[0] == "nobrainr" for m in module), module


def test_detect_communities_uses_the_process_not_a_thread():
    quelle = inspect.getsource(communities.detect_communities)
    assert "_im_eigenen_prozess(" in quelle
    assert "run_in_executor(\n            None" not in quelle and "run_in_executor(None" not in quelle
