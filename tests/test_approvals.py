"""The approval gate: only the owner's ✅ sends, exactly once, and /lock stops everything."""
import asyncio
import tempfile
import time
from pathlib import Path

import pytest
from conftest import OWNER, FakeBotHTTP

from approvals import Approvals, Draft
from tg import Fail


def make(**kw):
    http = FakeBotHTTP()
    a = Approvals("123:x", OWNER, state_file=Path(tempfile.mkdtemp()) / "state.json", http=http, **kw)
    sent = []

    async def executor(d):
        sent.append(d.id)
        return "#77 at now"

    a.executor = executor
    return a, http, sent


def draft(text="Привет! Посмотрел, вопросы ниже."):
    return Draft(peer_label="Ivan (@ivan, user, id 5)", peer_id=5, text=text)


def tap(a, d, action="ok", who=OWNER):
    return a.handle({"update_id": 1, "callback_query": {
        "id": "q1", "from": {"id": who}, "data": f"{action}:{d.id}",
        "message": {"message_id": d.card_id}}})


async def settle():
    for _ in range(5):
        await asyncio.sleep(0)


def run(coro):
    return asyncio.run(coro)


def test_card_shows_exact_text_and_buttons_to_owner():
    async def go():
        a, http, _ = make()
        d = await a.submit(draft())
        msg = http.sent("sendMessage")[0]
        assert msg["chat_id"] == OWNER
        assert "Привет! Посмотрел, вопросы ниже." in msg["text"]
        assert "Ivan (@ivan" in msg["text"]
        assert "parse_mode" not in msg  # shown literally, no formatting injection
        buttons = msg["reply_markup"]["inline_keyboard"][0]
        assert [b["callback_data"] for b in buttons] == [f"ok:{d.id}", f"no:{d.id}"]
        assert d.status == "pending" and d.card_id
    run(go())


def test_stranger_tap_is_ignored():
    async def go():
        a, _, sent = make()
        d = await a.submit(draft())
        await tap(a, d, "ok", who=OWNER + 1)
        await settle()
        assert sent == [] and d.status == "pending"
    run(go())


def test_owner_approval_sends_exactly_once():
    async def go():
        a, http, sent = make()
        d = await a.submit(draft())
        await tap(a, d, "ok")
        await tap(a, d, "ok")  # double tap
        await settle()
        assert sent == [d.id]
        assert d.status == "sent" and d.done.is_set()
        assert any("✅ Sent" in p["text"] for p in http.sent("editMessageText"))
    run(go())


def test_owner_decline_does_not_send():
    async def go():
        a, _, sent = make()
        d = await a.submit(draft())
        await tap(a, d, "no")
        await settle()
        assert sent == [] and d.status == "cancelled"
    run(go())


def test_lock_blocks_submit_and_cancels_pending():
    async def go():
        a, _, sent = make()
        d = await a.submit(draft())
        await a.handle({"update_id": 2, "message": {"chat": {"type": "private"}, "from": {"id": OWNER},
                                                    "text": "/lock"}})
        assert a.locked and d.status == "cancelled"
        with pytest.raises(Fail):
            await a.submit(draft())
        await tap(a, d, "ok")
        await settle()
        assert sent == []
    run(go())


def test_lock_from_stranger_is_ignored():
    async def go():
        a, http, _ = make()
        await a.handle({"update_id": 3, "message": {"chat": {"type": "private"}, "from": {"id": 1},
                                                    "text": "/lock"}})
        assert not a.locked and http.sent("sendMessage") == []
    run(go())


def test_lock_survives_restart():
    async def go():
        a, _, _ = make()
        a.set_locked(True)
        b = Approvals("123:x", OWNER, state_file=a.state_file, http=FakeBotHTTP())
        assert b.locked
    run(go())


def test_expired_draft_cannot_be_approved():
    async def go():
        a, _, sent = make(ttl_s=0.01)
        d = await a.submit(draft())
        await asyncio.sleep(0.05)
        await a.expire_due()
        assert d.status == "expired"
        await tap(a, d, "ok")
        await settle()
        assert sent == []
    run(go())


def test_pending_cap():
    async def go():
        a, _, _ = make(max_pending=2)
        await a.submit(draft())
        await a.submit(draft())
        with pytest.raises(Fail):
            await a.submit(draft())
    run(go())


def test_withdraw():
    async def go():
        a, _, sent = make()
        d = await a.submit(draft())
        await a.withdraw(d)
        await tap(a, d, "ok")
        await settle()
        assert d.status == "withdrawn" and sent == []
    run(go())


def test_executor_failure_is_reported_not_raised():
    async def go():
        a, http, _ = make()

        async def boom(d):
            raise Fail("Cannot write to Ivan: UserPrivacyRestrictedError")

        a.executor = boom
        d = await a.submit(draft())
        await tap(a, d, "ok")
        await settle()
        assert d.status == "failed" and "UserPrivacyRestricted" in d.result
    run(go())


def test_unknown_draft_tap_strips_buttons():
    async def go():
        a, http, _ = make()
        await a.handle({"update_id": 4, "callback_query": {"id": "q", "from": {"id": OWNER},
                                                           "data": "ok:deadbeef", "message": {"message_id": 5}}})
        assert http.sent("editMessageReplyMarkup")
    run(go())
