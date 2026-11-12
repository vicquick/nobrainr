"""The galaxy reduction must not run in the event loop.

Measured 2026-11-12: ten minutes after a restart the galaxy pre-warm ran PCA +
UMAP on 10,000 points inside the event loop for 2 min 15 s (05:42:00–05:44:15,
host under load). For that time the health check and every MCP call hung, the
container went unhealthy, agents saw timeouts on msg_send. The same code runs
on every stale-while-revalidate refresh.
"""

from __future__ import annotations

import asyncio
import threading
import time

import numpy as np

from nobrainr.dashboard import api


def test_reduktion_liefert_3d_in_minus_eins_bis_eins():
    rng = np.random.default_rng(1)
    matrix = rng.normal(size=(60, 32)).astype(np.float32)
    coords = api._galaxy_reduce(matrix, 60, 32)
    assert coords.shape == (60, 3)
    assert float(np.abs(coords).max()) <= 1.0 + 1e-6


def test_reduktion_laeuft_nicht_im_schleifen_thread(monkeypatch):
    """While the reduction runs, the loop keeps answering."""
    schleifen_thread = threading.get_ident()
    gesehen: dict = {}

    def langsam(matrix, count, dim):
        gesehen["thread"] = threading.get_ident()
        time.sleep(0.4)  # stands in for minutes of PCA + UMAP
        return np.zeros((count, 3), dtype=np.float32)

    monkeypatch.setattr(api, "_galaxy_reduce", langsam)

    async def lauf() -> int:
        ticks = 0

        async def herzschlag():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.02)
                ticks += 1

        h = asyncio.create_task(herzschlag())
        await asyncio.to_thread(api._galaxy_reduce, np.zeros((5, 8), dtype=np.float32), 5, 8)
        h.cancel()
        return ticks

    ticks = asyncio.run(lauf())
    assert gesehen["thread"] != schleifen_thread
    assert ticks >= 10, f"loop answered only {ticks} times in 0.4 s"


def test_compute_galaxy_ruft_die_reduktion_ueber_einen_thread():
    """Guard against a refactor that puts the call back into the coroutine."""
    import inspect

    quelle = inspect.getsource(api._compute_galaxy)
    assert "asyncio.to_thread(_galaxy_reduce" in quelle
    assert "fit_transform" not in quelle, "reduction code is back inside the coroutine"
