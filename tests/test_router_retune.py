"""Router retune (2026-07-22): live p95 was 7.1s because 80% of traffic
routed LLM-enhanced — sentence-length agent queries tripped the >=12-word
decompose rule. These tests lock in the selective thresholds.
"""

from __future__ import annotations

from nobrainr.mcp.server import _auto_route_query


def test_sentence_length_query_stays_plain():
    # 14 words — typical agent search; used to trip decompose at >=12
    q = "how did we configure the docker network aliases for the nobrainr container deploy path"
    r = _auto_route_query(q)
    assert not r.get("decompose", False)


def test_genuinely_multiclause_still_decomposes():
    q = ("compare the embedding model migration, the reranker rollout, and the "
         "trust flywheel changes, and summarize what each did to recall, and "
         "list the open risks for the next quarter overall")
    assert _auto_route_query(q).get("decompose") is True


def test_short_why_question_no_hyde():
    # 5 words — used to trip HyDE at >=5
    assert not _auto_route_query("why is search slow today").get("hyde", False)


def test_long_why_question_gets_hyde():
    q = "why does the reconciliation sweeper historicize executed plans instead of superseding them"
    assert _auto_route_query(q).get("hyde") is True


def test_short_query_still_expands():
    r = _auto_route_query("docker guard")
    assert r.get("expand") is True and r.get("hybrid") is False


def test_default_hybrid():
    r = _auto_route_query("nobrainr trust score formula weights")
    assert r == {"hybrid": True}
