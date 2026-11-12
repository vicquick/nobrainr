"""Agent meetings v1-lite (2026-10-30): blind answers, deadline reveal,
close → decision_store, multi-recipient msg_send, msg_inbox, msg_wait routing."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from nobrainr.mcp import server


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _meeting(answers=(), *, deadline_in_s=600, status="open", participants=("cto", "recht")):
    return {
        "id": uuid4(),
        "question": "Ship X before Y?",
        "moderator": "ceo",
        "participants": list(participants),
        "deadline": _now() + timedelta(seconds=deadline_in_s),
        "status": status,
        "decision_ref": None,
        "answers": [
            {"agent": a, "answer": text, "answered_at": _now()} for a, text in answers
        ],
    }


# ── reveal rule ──────────────────────────────


def test_blind_until_everyone_answered():
    m = _meeting([("cto", "yes")])
    assert server._meeting_revealed(m) is False
    m = _meeting([("cto", "yes"), ("recht", "no")])
    assert server._meeting_revealed(m) is True


def test_deadline_reveals_partial_answers():
    m = _meeting([("cto", "yes")], deadline_in_s=-1)
    assert server._meeting_revealed(m) is True


def test_closed_meeting_is_revealed():
    assert server._meeting_revealed(_meeting(status="closed")) is True


# ── blinding is server-side ──────────────────


async def test_status_hides_other_answers_before_reveal():
    m = _meeting([("cto", "yes, ship"), ("recht", "")][:1])
    with patch.object(server.queries, "get_meeting", AsyncMock(return_value=m)):
        as_recht = await server.meeting_status(str(m["id"]), "recht")
        as_cto = await server.meeting_status(str(m["id"]), "cto")
    assert as_recht["revealed"] is False
    assert "answers" not in as_recht
    assert as_recht["own_answer"] is None
    assert "yes, ship" not in json.dumps(as_recht, default=str)
    assert as_recht["answered"] == ["cto"]  # who answered is fine, what is not
    assert as_cto["own_answer"] == "yes, ship"


async def test_status_shows_all_answers_after_deadline():
    m = _meeting([("cto", "yes")], deadline_in_s=-5)
    with patch.object(server.queries, "get_meeting", AsyncMock(return_value=m)):
        view = await server.meeting_status(str(m["id"]), "recht")
    assert view["revealed"] is True
    assert [a["answer"] for a in view["answers"]] == ["yes"]


# ── answering ────────────────────────────────


async def test_answer_rejected_for_non_participant_and_after_reveal():
    m = _meeting([("cto", "yes"), ("recht", "no")])
    with patch.object(server.queries, "get_meeting", AsyncMock(return_value=m)):
        assert "error" in await server.meeting_answer(str(m["id"]), "kosten", "x")
        assert "revealed" in (await server.meeting_answer(str(m["id"]), "cto", "x"))["error"]


async def test_last_answer_reveals_and_notifies_moderator():
    before = _meeting([("cto", "yes")])
    after = dict(before, answers=before["answers"] + [
        {"agent": "recht", "answer": "no", "answered_at": _now()},
    ])
    upsert = AsyncMock()
    send = AsyncMock(return_value={"sent": True})
    with (
        patch.object(server.queries, "get_meeting", AsyncMock(side_effect=[before, after])),
        patch.object(server.queries, "upsert_meeting_answer", upsert),
        patch.object(server, "msg_send", send),
    ):
        out = await server.meeting_answer(str(before["id"]), "recht", " no ")
    assert out == {"recorded": True, "revealed": True}
    upsert.assert_awaited_once_with(str(before["id"]), "recht", "no")
    assert send.await_args.kwargs["to"] == "ceo"


# ── opening ──────────────────────────────────


async def test_ask_many_validates_size_and_messages_participants():
    assert "error" in await server.ask_many("q?", ["cto"], from_agent="ceo")
    assert "error" in await server.ask_many("q?", list("abcdef"), from_agent="ceo")

    m = _meeting()
    create = AsyncMock(return_value=m)
    send = AsyncMock(return_value={"sent": True})
    with (
        patch.object(server.queries, "create_meeting", create),
        patch.object(server, "msg_send", send),
    ):
        out = await server.ask_many("Ship X?", ["cto", "recht", "cto"], from_agent="ceo")
    assert out["participants"] == ["cto", "recht"]
    assert create.await_args.args[2] == ["cto", "recht"]
    kw = send.await_args.kwargs
    assert kw["to"] == ["cto", "recht"]
    assert kw["thread_id"] == f"meeting:{m['id']}"


# ── closing → decision_store ─────────────────


async def test_close_requires_moderator_and_reveal():
    m = _meeting([("cto", "yes")])
    with patch.object(server.queries, "get_meeting", AsyncMock(return_value=m)):
        assert "moderator" in (await server.meeting_close(
            str(m["id"]), "cto", "s", "d", "r"))["error"]
        assert "not revealed" in (await server.meeting_close(
            str(m["id"]), "ceo", "s", "d", "r"))["error"]


async def test_close_stores_decision_with_positions_and_dissent():
    m = _meeting([("cto", "ship now"), ("recht", "wait for AVV")])
    store = AsyncMock(return_value={"status": "queued", "queue_id": "q-1"})
    close = AsyncMock()
    send = AsyncMock(return_value={"sent": True})
    with (
        patch.object(server.queries, "get_meeting", AsyncMock(return_value=m)),
        patch.object(server.queries, "close_meeting", close),
        patch.object(server, "decision_store", store),
        patch.object(server, "msg_send", send),
    ):
        out = await server.meeting_close(
            str(m["id"]), "ceo", "bimavo/release", "Wait for AVV",
            "Legal risk outweighs a week", dissent=["cto: would ship now"],
        )
    assert out == {"closed": True, "decision_ref": "q-1"}
    kw = store.await_args.kwargs
    assert kw["alternatives_rejected"] == ["cto: ship now", "recht: wait for AVV"]
    assert "cto: would ship now" in kw["rationale"]
    assert f"meeting:{m['id']}" in kw["tags"]
    close.assert_awaited_once_with(str(m["id"]), "q-1")
    assert send.await_args.kwargs["to"] == ["cto", "recht"]


# ── messaging ────────────────────────────────


def _pool_mock():
    conn = MagicMock()
    conn.execute = AsyncMock()
    acq = MagicMock()
    acq.__aenter__ = AsyncMock(return_value=conn)
    acq.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acq)
    return pool, conn


async def test_msg_send_list_recipients_tags_and_thread():
    pool, conn = _pool_mock()
    store = AsyncMock(return_value={"id": "m-1"})
    with (
        patch("nobrainr.services.memory.store_memory_with_extraction", store),
        patch("nobrainr.db.pool.get_pool", AsyncMock(return_value=pool)),
    ):
        out = await server.msg_send(
            to=["cto", "recht", "cto"], subject="s", body="b",
            from_agent="ceo", reply_to="m-0",
        )
    tags = store.await_args.kwargs["tags"]
    assert {"to:cto", "to:recht", "from:ceo", "thread:m-0", "reply-to:m-0"} <= set(tags)
    assert tags.count("to:cto") == 1
    payload = json.loads(conn.execute.await_args.args[2])
    assert payload["to"] == ["cto", "recht"]
    assert out["thread_id"] == "m-0"


async def test_msg_send_single_recipient_unchanged_shape():
    pool, conn = _pool_mock()
    store = AsyncMock(return_value={"id": "m-1"})
    with (
        patch("nobrainr.services.memory.store_memory_with_extraction", store),
        patch("nobrainr.db.pool.get_pool", AsyncMock(return_value=pool)),
    ):
        await server.msg_send(to="cto", subject="s", body="b", from_agent="ceo")
    tags = store.await_args.kwargs["tags"]
    assert tags == ["agent-msg", "from:ceo", "to:cto"]


async def test_msg_inbox_parses_tags_and_rejects_bad_since():
    row = {
        "id": uuid4(), "summary": "[ceo→cto] s", "content": "body",
        "tags": ["agent-msg", "from:ceo", "to:cto", "thread:meeting:1"],
        "created_at": _now(),
    }
    fetch = AsyncMock(return_value=[row])
    with patch.object(server.queries, "fetch_agent_inbox", fetch):
        out = await server.msg_inbox("cto", since="2026-10-30T10:00:00Z", limit=500)
        bad = await server.msg_inbox("cto", since="yesterday")
    assert out["messages"][0]["from"] == "ceo"
    assert out["messages"][0]["thread_id"] == "meeting:1"
    assert fetch.await_args.kwargs["limit"] == 100
    assert "error" in bad
