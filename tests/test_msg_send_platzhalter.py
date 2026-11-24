"""msg_send refuses empty and placeholder messages (G6 step 1, ceo 2026-11-24).

At least five times (18.11., 20.11., 23.11. twice, 24.11.) a session fired msg_send
by accident with subject/body "placeholder", "x" or empty — the message was stored
and pushed to real recipients. A message that says nothing is now refused before
anything is stored or notified.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from nobrainr.mcp import server


@pytest.mark.parametrize("subject, body", [
    ("", "Inhalt der Nachricht"),
    ("Betreff", ""),
    ("   ", "Inhalt der Nachricht"),
    ("placeholder", "Inhalt der Nachricht"),
    ("Betreff", "placeholder"),
    ("Betreff", "PLACEHOLDER"),
    ("x", "Inhalt der Nachricht"),
    ("Betreff", "x"),
    ("test", "Inhalt der Nachricht"),
    ("Betreff", " Test "),
    ("Betreff", "…"),
    ("Betreff", "ok"),
    ("Betreff", "todo"),
    ("Betreff", "tbd"),
    ("Betreff", "..."),
    ("Betreff", "-"),
])
async def test_placeholder_or_too_short_is_refused_before_storing(subject, body):
    store = AsyncMock(return_value={"id": "m-1"})
    with patch("nobrainr.services.memory.store_memory_with_extraction", store):
        out = await server.msg_send(to="bimavo-ceo", subject=subject, body=body, from_agent="bimavo-vpsfixer")
    assert "error" in out, out
    store.assert_not_awaited()


async def test_empty_from_agent_is_refused():
    store = AsyncMock(return_value={"id": "m-1"})
    with patch("nobrainr.services.memory.store_memory_with_extraction", store):
        out = await server.msg_send(to="bimavo-ceo", subject="Betreff", body="Ein echter Satz.", from_agent=" ")
    assert "error" in out
    store.assert_not_awaited()


@pytest.mark.parametrize("subject, body", [
    ("vpsfixer: B2 fertig", "12/12 kopiert, Nachzählung = Soll."),
    ("OK?", "Bitte kurz bestätigen, ob 22:15 passt."),
    ("ja", "ja — Freigabe für den Lauf"),           # short subject alone is fine if it is not a placeholder word
])
def test_real_messages_pass_the_check(subject, body):
    assert server._msg_text_fehler(subject, body) is None
