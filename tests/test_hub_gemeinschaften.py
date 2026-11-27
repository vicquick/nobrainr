"""Hub re-assignment after Leiden: one pass over the edges (vpsfixer 27.11.2026).

After #189 Leiden ran in a child process, but nobrainr still answered nothing
for 30 s (10:47:46–10:48:16): the hub loop ran over all 383k edges once per
hub (635 hubs) in the event loop.
"""

from __future__ import annotations

import random
import time
from collections import Counter

from nobrainr.services.communities import _hub_gemeinschaften


def _alt(hub_ids, edge_tuples, assignments):
    """The old per-hub loop, restricted to non-hub voters (no hub-hub edges in the data)."""
    aus = {}
    for hub in hub_ids:
        stimmen: Counter = Counter()
        for src, tgt, _ in edge_tuples:
            if src == hub and tgt in assignments:
                stimmen[assignments[tgt]] += 1
            elif tgt == hub and src in assignments:
                stimmen[assignments[src]] += 1
        if stimmen:
            aus[hub] = stimmen.most_common(1)[0][0]
    return aus


def _daten(n_hubs, n_rest, n_kanten, hub_hub=False, seed=3):
    rnd = random.Random(seed)
    hubs = {f"h{i}" for i in range(n_hubs)}
    rest = [f"n{i}" for i in range(n_rest)]
    zuordnung = {n: rnd.randrange(50) for n in rest}
    alle = sorted(hubs) + rest
    kanten = []
    for _ in range(n_kanten):
        a, b = rnd.choice(alle), rnd.choice(alle)
        if a == b or (not hub_hub and a in hubs and b in hubs):
            continue
        kanten.append((a, b, 1.0))
    return hubs, kanten, zuordnung


def test_same_result_as_old_loop_without_hub_hub_edges():
    hubs, kanten, zuordnung = _daten(40, 2_000, 20_000)
    assert _hub_gemeinschaften(hubs, kanten, zuordnung) == _alt(hubs, kanten, zuordnung)


def test_hub_hub_edges_do_not_vote_and_result_is_order_free():
    zuordnung = {"a": 1, "b": 2}
    kanten = [("h1", "a", 1.0), ("h1", "h2", 1.0), ("h2", "b", 1.0), ("h2", "b", 1.0), ("h2", "h1", 1.0)]
    erg = _hub_gemeinschaften({"h1", "h2"}, kanten, zuordnung)
    assert erg == {"h1": 1, "h2": 2}
    assert _hub_gemeinschaften({"h2", "h1"}, list(reversed(kanten)), zuordnung)["h1"] == 1


def test_hub_without_assigned_neighbour_gets_nothing():
    assert _hub_gemeinschaften({"h"}, [("h", "x", 1.0)], {}) == {}


def test_input_dict_is_not_changed():
    zuordnung = {"a": 1}
    _hub_gemeinschaften({"h"}, [("h", "a", 1.0)], zuordnung)
    assert zuordnung == {"a": 1}


def test_live_size_runs_in_well_under_a_second_per_100k_edges():
    # live 27.11.: 635 hubs, 383 780 edges → old loop ~30 s in the event loop
    hubs, kanten, zuordnung = _daten(635, 220_000, 383_780, hub_hub=True)
    t0 = time.monotonic()
    _hub_gemeinschaften(hubs, kanten, zuordnung)
    assert time.monotonic() - t0 < 2.0
