"""Default recall leaves agent messages out; the mailbox still finds all of them.

Measured 2026-11-15: agent messages (category "agent-comm", written by
msg_send) were 65-90 % of the memories added per day and crowded knowledge
out of memory_search and of what the prompt hook injects. They are mail, not
knowledge: memory_search, /api/brief and /api/smart-recall skip them unless
asked; msg_inbox, lookups by id and tag/category searches are untouched.
After 14 days a message goes to tier 3 (cold).
"""

from __future__ import annotations

import inspect
import json
import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.requests import Request

from nobrainr.db import queries

AUSSCHLUSS = "category <> ALL("


class _Verbindung:
    """Records every statement; answers the trigram seed lookup with one
    entity so the graph branch really builds its memory join."""

    def __init__(self):
        self.aufrufe: list[tuple[str, tuple]] = []

    async def fetch(self, sql, *params):
        self.aufrufe.append((sql, params))
        if "canonical_name % $1" in sql:
            return [{"id": "00000000-0000-0000-0000-000000000001", "sim": 0.9, "idf": 1.0}]
        return []

    async def execute(self, sql, *params):
        self.aufrufe.append((sql, params))
        return "UPDATE 0"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Pool:
    def __init__(self, verbindung):
        self.verbindung = verbindung

    def acquire(self):
        return self.verbindung


def _bindet_kategorien(params: tuple) -> bool:
    # not ``[...] in params``: the embedding is a numpy array and == on it
    # does not give a bool.
    return any(isinstance(p, list) and p == ["agent-comm"] for p in params)


def _platzhalter_passen(sql: str, params: tuple) -> bool:
    """Highest $N in the statement equals the number of bound parameters —
    the numbering mistake a mocked connection otherwise never notices."""
    nummern = [int(n) for n in re.findall(r"\$(\d+)", sql)]
    return (max(nummern) if nummern else 0) == len(params)


# ── which searches leave messages out ──────────────────────────────────────

def test_plain_recall_leaves_agent_messages_out():
    assert queries.default_exclude_categories(None, None) == ["agent-comm"]
    assert queries.default_exclude_categories(None, ["docker", "lesson"]) == ["agent-comm"]


@pytest.mark.parametrize("tags", [
    ["agent-msg"], ["agent-msg", "to:vpsfixer"], ["thread:01a1de43"],
    ["from:bimavo-ceo"], ["Reply-To:01a1"], ["docker", "to:all"],
])
def test_asking_for_messages_by_tag_finds_them(tags):
    assert queries.default_exclude_categories(None, tags) is None


def test_asking_for_a_category_is_never_overruled():
    assert queries.default_exclude_categories("agent-comm", None) is None
    assert queries.default_exclude_categories("debugging", None) is None


def test_a_message_search_is_recognised_and_reaches_cold_messages():
    assert queries.asks_for_messages("agent-comm", None)
    assert queries.asks_for_messages(None, ["thread:01a1de43"])
    assert queries.asks_for_messages(None, ["docker", "From:bimavo-ceo"])
    assert not queries.asks_for_messages("debugging", ["docker"])
    assert not queries.asks_for_messages(None, None)

    from nobrainr.mcp import server

    quelle = inspect.getsource(server.memory_search)
    erkennung = quelle.index("if queries.asks_for_messages(category, tags):")
    assert "include_cold = True" in quelle[erkennung:erkennung + 80]
    assert erkennung < quelle.index("queries.search_memories("), \
        "the cold tier must be opened before the search runs"


def test_empty_setting_switches_the_exclusion_off(monkeypatch):
    monkeypatch.setattr(queries._cfg, "search_exclude_categories", [])
    assert queries.default_exclude_categories(None, None) is None


# ── the filter reaches the SQL of every branch ─────────────────────────────

def test_filter_clause_binds_the_excluded_categories_last():
    clause, params, idx = queries._build_filter_clause(
        5, ["docker"], None, None, "bimavo", exclude_categories=["agent-comm"],
    )
    assert "(category IS NULL OR category <> ALL($7::text[]))" in clause
    assert params[-1] == ["agent-comm"] and idx == 8

    clause, params, _ = queries._build_filter_clause(5, None, None, None, None)
    assert clause == "" and params == []


async def test_vector_only_search_carries_the_exclusion():
    verbindung = _Verbindung()
    with patch.object(queries, "get_pool", AsyncMock(return_value=_Pool(verbindung))):
        await queries.search_memories([0.1] * 8, exclude_categories=["agent-comm"])
    (sql, params), = verbindung.aufrufe
    assert AUSSCHLUSS in sql and _bindet_kategorien(params)
    assert _platzhalter_passen(sql, params)


async def test_search_without_exclusion_is_unchanged():
    verbindung = _Verbindung()
    with patch.object(queries, "get_pool", AsyncMock(return_value=_Pool(verbindung))):
        await queries.search_memories([0.1] * 8, text_query="commit 798461a6 docker netz")
    assert verbindung.aufrufe and all(AUSSCHLUSS not in sql for sql, _ in verbindung.aufrufe)


async def test_all_five_hybrid_branches_carry_the_exclusion(monkeypatch):
    for schalter in ("graph_branch_enabled", "community_branch_enabled"):
        monkeypatch.setattr(queries._cfg, schalter, True)
    # PPR needs the in-process graph cache; the seed alone builds the join.
    monkeypatch.setattr(queries._cfg, "ppr_enabled", False)
    verbindung = _Verbindung()
    with patch.object(queries, "get_pool", AsyncMock(return_value=_Pool(verbindung))):
        await queries.search_memories(
            [0.1] * 8, text_query="commit 798461a6 docker netz",
            tags=["docker"], exclude_categories=["agent-comm"],
        )

    zweige = {
        "vec": [a for a in verbindung.aufrufe if "vec_candidates" in a[0]],
        "fts": [a for a in verbindung.aufrufe if "fts_rank" in a[0]],
        "id": [a for a in verbindung.aufrufe if "literal_score" in a[0]],
        "graph": [a for a in verbindung.aufrufe if "graph_score" in a[0]],
        "community": [a for a in verbindung.aufrufe if "matched_communities" in a[0]],
    }
    for name, aufrufe in zweige.items():
        assert len(aufrufe) == 1, f"branch {name} did not run"
        sql, params = aufrufe[0]
        assert AUSSCHLUSS in sql, f"branch {name} lets agent messages through"
        assert _bindet_kategorien(params), f"branch {name} does not bind the categories"
        assert _platzhalter_passen(sql, params), f"branch {name}: placeholders and parameters differ"


async def test_text_only_recall_carries_the_exclusion():
    verbindung = _Verbindung()
    with patch.object(queries, "get_pool", AsyncMock(return_value=_Pool(verbindung))):
        await queries.fts_memories("bimavo deploy", exclude_categories=["agent-comm"])
    (sql, params), = verbindung.aufrufe
    assert AUSSCHLUSS in sql and params[-1] == ["agent-comm"]
    assert _platzhalter_passen(sql, params)


async def test_recent_list_can_leave_messages_out():
    verbindung = _Verbindung()
    with patch.object(queries, "get_pool", AsyncMock(return_value=_Pool(verbindung))):
        await queries.query_memories(limit=5, exclude_categories=["agent-comm"])
    (sql, params), = verbindung.aufrufe
    assert AUSSCHLUSS in sql and params == (["agent-comm"], 5, 0)
    assert _platzhalter_passen(sql, params)


# ── the mailbox still finds everything ─────────────────────────────────────

async def test_inbox_reads_messages_of_any_tier_and_age():
    verbindung = _Verbindung()
    with patch.object(queries, "get_pool", AsyncMock(return_value=_Pool(verbindung))):
        await queries.fetch_agent_inbox("vpsfixer", thread_id="01a1de43")
    (sql, params), = verbindung.aufrufe
    assert "category = 'agent-comm'" in sql
    assert "tier" not in sql, "a cold message must stay in the inbox"
    assert AUSSCHLUSS not in sql
    assert "thread:01a1de43" in params


def test_lookup_by_id_has_no_category_or_tier_condition():
    quelle = inspect.getsource(queries.get_memory)
    assert "agent-comm" not in quelle and "tier <" not in quelle


# ── callers: recall and injection exclude, the dashboard does not ──────────

def test_memory_search_excludes_unless_asked():
    from nobrainr.mcp import server

    quelle = inspect.getsource(server.memory_search)
    assert "include_agent_comm: bool = False" in quelle
    assert "queries.default_exclude_categories(category, tags)" in quelle
    assert "exclude_categories=_exclude_categories" in quelle


def test_injection_endpoints_exclude_and_the_command_palette_does_not():
    from nobrainr.dashboard import api

    for handler in (api.api_brief, api.api_smart_recall):
        assert "default_exclude_categories(None, None)" in inspect.getsource(handler)
    # failed-fix lane searches by tag and must keep its own filter
    assert 'tags=["error-pattern"], include_cold=True' in inspect.getsource(api.api_brief)
    assert "exclude_categories" not in inspect.getsource(api.api_recall)
    assert "exclude_categories" not in inspect.getsource(api.api_search_preview)


def _anfrage(query: str) -> Request:
    return Request({"type": "http", "method": "GET", "path": "/api/memories",
                    "query_string": query.encode(), "headers": []})


async def test_memories_list_excludes_only_on_request(monkeypatch):
    from nobrainr.dashboard import api

    gesehen: list[dict] = []

    async def query_memories(**kw):
        gesehen.append(kw)
        return []

    monkeypatch.setattr(api.queries, "query_memories", query_memories)

    antwort = await api.api_memories(_anfrage("limit=5"))
    assert json.loads(antwort.body) == []
    assert gesehen[-1]["exclude_categories"] is None, "the dashboard list shows everything"

    await api.api_memories(_anfrage("limit=5&exclude_category=agent-comm"))
    assert gesehen[-1]["exclude_categories"] == ["agent-comm"]

    await api.api_memories(_anfrage("limit=5&exclude_category=agent-comm&category=agent-comm"))
    assert gesehen[-1]["exclude_categories"] is None, "an explicit category wins"


# ── messages go cold after 14 days ─────────────────────────────────────────

def _tier_pool():
    conn = AsyncMock()
    conn.execute = AsyncMock(return_value="UPDATE 0")
    pool = MagicMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=False)
    pool.acquire = MagicMock(return_value=cm)
    return pool, conn


async def test_old_messages_go_cold_last_and_pins_stay(monkeypatch):
    monkeypatch.setattr(queries._cfg, "agent_comm_cold_days", 14)
    pool, conn = _tier_pool()
    with patch.object(queries, "get_pool", AsyncMock(return_value=pool)):
        counts = await queries.auto_tier_memories()

    aufrufe = conn.execute.call_args_list
    sql, tage = aufrufe[-1].args
    assert "category = 'agent-comm'" in sql and "SET tier = 3" in sql
    assert "tier IN (1, 2)" in sql, "tier 0 (pinned) must not be demoted"
    assert "make_interval(days => $1::int)" in sql and tage == 14
    assert "agent_comm_cold" in counts


async def test_zero_days_switches_the_cold_rule_off(monkeypatch):
    monkeypatch.setattr(queries._cfg, "agent_comm_cold_days", 0)
    pool, conn = _tier_pool()
    with patch.object(queries, "get_pool", AsyncMock(return_value=pool)):
        counts = await queries.auto_tier_memories()

    assert all("agent-comm" not in c.args[0] for c in conn.execute.call_args_list)
    assert counts["agent_comm_cold"] == 0
