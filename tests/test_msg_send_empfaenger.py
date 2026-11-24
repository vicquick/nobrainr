"""msg_send reads several recipients in one string as a list.

Measured 2026-11-15: ``to="bimavo-viewer, bimavo-ceo"`` was stored under the
single tag ``to:bimavo-viewer,-bimavo-ceo`` — no inbox, no msg_wait and no
hook matches it. 239 messages in 30 days (120 in the last 7) reached nobody.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nobrainr.mcp import server


@pytest.mark.parametrize("to, erwartet", [
    ("bimavo-ceo", ["bimavo-ceo"]),
    ("bimavo-viewer, bimavo-ceo", ["bimavo-viewer", "bimavo-ceo"]),
    ("bimavo-ceo,bimavo-viewer,bimavo-cto", ["bimavo-ceo", "bimavo-viewer", "bimavo-cto"]),
    ('"bimavo-cto"', ["bimavo-cto"]),
    ('["bimavo-cto", "bimavo-ceo"]', ["bimavo-cto", "bimavo-ceo"]),
    ("[bimavo-cto, bimavo-ceo]", ["bimavo-cto", "bimavo-ceo"]),
    ("bimavo-cto; bimavo-ceo\nbimavo-sweep", ["bimavo-cto", "bimavo-ceo", "bimavo-sweep"]),
    (["bimavo-cto", " bimavo-ceo ", "bimavo-cto"], ["bimavo-cto", "bimavo-ceo"]),
    (["bimavo-cto, bimavo-ceo", "bimavo-sweep"], ["bimavo-cto", "bimavo-ceo", "bimavo-sweep"]),
    ("workserver:str", ["workserver:str"]),
    ("all", ["all"]),
    ("@bimavo-ceo", ["bimavo-ceo"]),
    ("", []), (" , ", []), ([], []), (None, []),
])
def test_recipients_are_read_from_strings_and_lists(to, erwartet):
    assert server._recipient_list(to) == erwartet


def test_a_name_is_not_split_at_spaces():
    assert server._recipient_list("my agent") == ["my agent"]


def _pool():
    conn = AsyncMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=cm)
    return pool, conn


async def test_comma_string_gets_one_tag_per_recipient_and_notifies_each():
    store = AsyncMock(return_value={"id": "m-1"})
    pool, conn = _pool()
    with (
        patch("nobrainr.services.memory.store_memory_with_extraction", store),
        patch("nobrainr.db.pool.get_pool", AsyncMock(return_value=pool)),
    ):
        out = await server.msg_send(
            to="bimavo-viewer, bimavo-ceo", subject="Gegenlesen", body="Bitte den Diff lesen.",
            from_agent='"bimavo-sweep"',
        )

    assert out["to"] == ["bimavo-viewer", "bimavo-ceo"]
    tags = store.await_args.kwargs["tags"]
    assert "to:bimavo-viewer" in tags and "to:bimavo-ceo" in tags
    assert "from:bimavo-sweep" in tags
    assert not [t for t in tags if "," in t or '"' in t], tags
    assert store.await_args.kwargs["summary"].startswith("[bimavo-sweep→bimavo-viewer, bimavo-ceo]")
    payload = json.loads(conn.execute.await_args.args[2])
    assert payload["to"] == ["bimavo-viewer", "bimavo-ceo"], "msg_wait matches on this list"


async def test_no_recipient_is_still_refused():
    assert "error" in await server.msg_send(to=" , ", subject="s", body="b", from_agent="a")
