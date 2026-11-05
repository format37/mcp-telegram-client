"""Chats the server must never touch, whatever the caller asks."""
import time

import pytest
from telethon.tl import types as T

import tg


def user(uid, **kw):
    return T.User(id=uid, first_name=kw.pop("first_name", f"u{uid}"), **kw)


def test_service_chat_is_always_blocked():
    p = tg.Policy(allow_saved=True)
    with pytest.raises(tg.Fail, match="service chat"):
        p.check(user(777000, first_name="Telegram"))


def test_saved_messages_blocked_by_default():
    with pytest.raises(tg.Fail, match="Saved Messages"):
        tg.Policy().check(user(4242, is_self=True))
    tg.Policy(allow_saved=True).check(user(4242, is_self=True))


def test_approval_bot_blocked():
    p = tg.Policy()
    p.blocked_ids.add(999)
    with pytest.raises(tg.Fail, match="control bot"):
        p.check(user(999, bot=True))


def test_deny_list_by_id_and_marked_channel_id():
    chan = T.Channel(id=12345, title="c", photo=T.ChatPhotoEmpty(), date=None)
    p = tg.Policy(deny={tg.peer_id(chan), 55})
    with pytest.raises(tg.Fail):
        p.check(chan)
    with pytest.raises(tg.Fail):
        p.check(user(55))
    p.check(user(56))
    assert not p.visible(user(55)) and p.visible(user(56))


def test_sendlog_budget(tmp_path, monkeypatch):
    monkeypatch.setenv("SEND_PER_HOUR", "2")
    log = tg.SendLog(tmp_path / "s.jsonl")
    log.record(1, 10, False)
    log.record(1, 11, False)
    with pytest.raises(tg.Fail, match="hourly"):
        log.check(1, 1, False)
    assert (tmp_path / "s.jsonl").stat().st_mode & 0o777 == 0o600


def test_new_contact_budget(tmp_path, monkeypatch):
    monkeypatch.setenv("SEND_NEW_PEERS_PER_DAY", "1")
    log = tg.SendLog(tmp_path / "s.jsonl")
    log.record(7, 1, True)
    log.check(1, 7, True)  # same new peer again is fine
    with pytest.raises(tg.Fail, match="new-contact"):
        log.check(1, 8, True)


def test_flood_pauses_sends(tmp_path):
    log = tg.SendLog(tmp_path / "s.jsonl")
    log.record_flood(60)
    with pytest.raises(tg.Fail, match="paused"):
        log.check(1, 1, False)
    assert log.status()["flood_until"] > time.time()


def test_write_private_is_0600(tmp_path):
    f = tmp_path / "x" / "session.string"
    tg.write_private(f, "secret")
    assert f.read_text() == "secret" and f.stat().st_mode & 0o777 == 0o600


def test_saved_messages_can_be_send_only():
    me = user(4242, is_self=True)
    p = tg.Policy(send_saved=True)
    p.check(me, write=True)
    with pytest.raises(tg.Fail, match="send-only"):
        p.check(me)  # reading
    assert not p.visible(me)  # and never listed
    full = tg.Policy(allow_saved=True)
    full.check(me)
    full.check(me, write=True)
    with pytest.raises(tg.Fail, match="off limits"):
        tg.Policy().check(me, write=True)
    with pytest.raises(tg.Fail, match="service chat"):  # the other rules hold for writing too
        tg.Policy(send_saved=True).check(user(777000), write=True)


def test_resolve_peer_me_is_checked_for_what_it_is_used_for():
    import asyncio

    me = user(4242, is_self=True)

    class Client:
        async def get_me(self):
            return me

    p = tg.Policy(send_saved=True)
    assert asyncio.run(tg.resolve_peer(Client(), "me", p, write=True)) is me
    with pytest.raises(tg.Fail, match="send-only"):
        asyncio.run(tg.resolve_peer(Client(), "me", p))
