"""Sending files: grouping into albums, photo limits, captions, budget, and the fake client's view."""
import asyncio
import os
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from conftest import gif, png
from telethon import TelegramClient, errors, functions
from telethon.sessions import StringSession
from telethon.tl import types as T

import tg

IVAN = T.User(id=5, first_name="Ivan", username="ivan")


class OfflineTelethon(TelegramClient):
    """Telethon's own send_file, cut off only where a request would leave for the network.
    Telegram's answer is left empty, so Telethon returns None for the messages."""

    def __init__(self):
        super().__init__(StringSession(), 1, "h" * 32, receive_updates=False)
        self.requests = []

    async def __call__(self, request, ordered=False, flood_sleep_threshold=None):
        self.requests.append(request)
        if isinstance(request, functions.messages.UploadMediaRequest):
            m = request.media
            if isinstance(m, T.InputMediaUploadedPhoto):
                return T.MessageMediaPhoto(photo=T.Photo(id=1, access_hash=1, file_reference=b"", date=None,
                                                         sizes=[], dc_id=1))
            return T.MessageMediaDocument(document=T.Document(id=2, access_hash=2, file_reference=b"", date=None,
                                                              mime_type=m.mime_type, size=1, dc_id=1,
                                                              attributes=m.attributes))
        return T.Updates(updates=[], users=[], chats=[], date=None, seq=0)

    async def get_input_entity(self, peer):
        return T.InputPeerUser(5, 0)

    async def upload_file(self, file, **kw):
        return T.InputFile(id=1, parts=1, name=os.path.basename(str(file)), md5_checksum="")

    def uploaded(self):
        """The media as built from our files: per item of an album, or of the single message."""
        ups = [r.media for r in self.requests if isinstance(r, functions.messages.UploadMediaRequest)]
        return ups or [r.media for r in self.requests if isinstance(r, functions.messages.SendMediaRequest)]


class FakeClient:
    """Records send_file the way Telethon would receive it; `fail` = {call index: exception}."""

    def __init__(self, fail=None):
        self.calls, self.uploads, self.fail = [], [], fail or {}
        self._id = 100

    def _msg(self):
        self._id += 1
        return SimpleNamespace(id=self._id, date=datetime.now(timezone.utc))

    async def send_file(self, entity, file, **kw):
        if len(self.calls) in self.fail:
            raise self.fail.pop(len(self.calls))
        self.calls.append((file, kw))
        return [self._msg() for _ in file] if isinstance(file, list) else self._msg()

    async def upload_file(self, path, **kw):
        self.uploads.append(path)
        return T.InputFile(id=len(self.uploads), parts=1, name=os.path.basename(path), md5_checksum="")


@pytest.fixture
def log(tmp_path):
    lg = tg.SendLog(tmp_path / "sendlog.jsonl")
    lg.min_interval = 0
    return lg


@pytest.fixture
def files(tmp_path):
    def make(*names, data=None):
        out = []
        for n in names:
            p = tmp_path / n
            if data is not None:
                p.write_bytes(data)
            elif n.endswith((".png", ".jpg")):
                p.write_bytes(png(1600, 900))
            elif n.endswith(".gif"):
                p.write_bytes(gif(240, 180))
            else:
                p.write_bytes(b"x" * 100)
            out.append(p)
        return out
    return make


def send(client, paths, log, **kw):
    opts = dict(caption=None, as_file=False, album=True, reply_to=None, plain=False, silent=False,
                log=log, is_new=False)
    opts.update(kw)
    return asyncio.run(tg.send_files(client, IVAN, paths, **opts))


def names(file_arg):
    return [os.path.basename(f) for f in (file_arg if isinstance(file_arg, list) else [file_arg])]


# ── albums ───────────────────────────────────────────────────────────────────
def test_twelve_photos_are_two_albums_with_the_caption_on_the_first(files, log):
    c = FakeClient()
    sent = send(c, files(*[f"p{i}.png" for i in range(12)]), log, caption="Charts")
    assert [len(f) for f, _ in c.calls] == [10, 2]
    assert [kw["caption"] for _, kw in c.calls] == ["Charts", None]
    assert all(kw["force_document"] is False for _, kw in c.calls)
    assert [len(m) for m in sent] == [10, 2] and log.status()["hour"] == 2


def test_photo_and_pdf_cannot_share_an_album(files, log):
    c = FakeClient()
    paths = files("chart.png", "report.pdf")
    with pytest.raises(tg.Fail) as e:
        send(c, paths, log)
    assert "as_file=true" in str(e.value) and "album=false" in str(e.value)
    assert "chart.png → photo" in str(e.value) and "report.pdf → file" in str(e.value)
    assert c.calls == [] and log.status()["hour"] == 0
    send(c, paths, log, as_file=True)
    ((f, kw),) = c.calls
    assert names(f) == ["chart.png", "report.pdf"] and kw["force_document"] is True


@pytest.mark.parametrize("pair", [("chart.png", "loop.gif"), ("a.gif", "b.gif")])
def test_a_gif_never_joins_an_album(files, log, pair):
    c = FakeClient()
    with pytest.raises(tg.Fail, match="GIF never joins an album"):
        send(c, files(*pair), log)
    assert c.calls == []


def test_a_gif_alone_is_one_animation_and_as_file_makes_a_file_album(files, log):
    c = FakeClient()
    send(c, files("loop.gif"), log)
    ((f, kw),) = c.calls
    assert names(f) == ["loop.gif"] and not isinstance(f, list) and kw["force_document"] is False
    size, animated = kw["attributes"]
    assert (size.w, size.h) == (240, 180) and isinstance(animated, T.DocumentAttributeAnimated)
    c = FakeClient()
    send(c, files("chart.png", "loop.gif"), log, as_file=True)
    ((f, kw),) = c.calls
    assert names(f) == ["chart.png", "loop.gif"] and kw["force_document"] is True and "attributes" not in kw


def test_album_false_is_one_message_each_paced_and_counted(files, log, monkeypatch):
    paced = []

    async def pace():
        paced.append(len(c.calls))

    monkeypatch.setattr(log, "pace", pace)
    c = FakeClient()
    sent = send(c, files("chart.png", "report.pdf", "loop.gif"), log, album=False, caption="Three")
    assert [names(f) for f, _ in c.calls] == [["chart.png"], ["report.pdf"], ["loop.gif"]]
    assert [kw["caption"] for _, kw in c.calls] == ["Three", None, None]
    assert [kw["force_document"] for _, kw in c.calls] == [False, True, False]
    assert paced == [0, 1, 2] and log.status()["hour"] == 3 and len(sent) == 3


def test_budget_is_checked_for_every_message_up_front(files, log):
    log.per_hour = 2
    c = FakeClient()
    with pytest.raises(tg.Fail, match="hourly"):
        send(c, files("a.png", "b.png", "c.png"), log, album=False)
    assert c.calls == []


# ── what each file becomes ───────────────────────────────────────────────────
def test_paths_keep_the_original_names(files, log):
    c = FakeClient()
    send(c, files("my chart.png", "отчёт.pdf"), log, album=False)
    assert [f for f, _ in c.calls] == [str(p) for p in files("my chart.png", "отчёт.pdf")]
    assert c.calls[0][0].endswith("/my chart.png")


def test_webp_goes_as_a_document_even_as_media(files, log):
    c = FakeClient()
    send(c, files("sticker-bait.webp"), log)
    assert c.calls[0][1]["force_document"] is True


def test_photo_past_telegram_limits_goes_as_a_file(tmp_path, log):
    wide, ok = tmp_path / "panorama.png", tmp_path / "ok.png"
    wide.write_bytes(png(12_000, 800))
    ok.write_bytes(png(800, 600))
    assert tg.send_mode(tg.probe(wide), wide.stat().st_size, False) == (
        "document", "12000x800 is past the photo limit (width + height at most 10000 px)")
    assert tg.photo_refusal(11_000_000, 100, 100) and tg.photo_refusal(100, 4100, 200)  # 10 MB, 20:1
    assert tg.photo_refusal(100, 4000, 200) == ""
    c = FakeClient()
    send(c, [wide], log)
    assert c.calls[0][1]["force_document"] is True
    with pytest.raises(tg.Fail, match="panorama.png → file"):
        send(c, [ok, wide], log)


def test_a_png_that_is_no_image_is_a_document(tmp_path, log):
    p = tmp_path / "fake.png"
    p.write_bytes(b"not a png")
    assert tg.probe(p).kind == "document"
    c = FakeClient()
    send(c, [p], log)
    assert c.calls[0][1]["force_document"] is True  # Telethon must not try to decode it


def test_video_streams_and_audio_carries_an_audio_attribute(files, log):
    c = FakeClient()
    send(c, files("clip.mp4", "talk.mp3"), log, album=False)
    (v, vkw), (a, akw) = c.calls
    assert v.endswith("clip.mp4") and vkw["supports_streaming"] is True and vkw["nosound_video"] is True
    assert isinstance(a, T.InputMediaUploadedDocument) and a.mime_type == "audio/mpeg"
    kinds = {type(x) for x in a.attributes}
    assert T.DocumentAttributeAudio in kinds
    assert [x.file_name for x in a.attributes if isinstance(x, T.DocumentAttributeFilename)] == ["talk.mp3"]
    assert c.uploads == [str(files("talk.mp3")[0])]


def test_audio_album_goes_as_one_album_of_audio(files, log):
    c = FakeClient()
    send(c, files("a.mp3", "b.m4a", "c.ogg"), log)
    ((f, kw),) = c.calls
    assert len(f) == 3 and all(isinstance(x, T.InputMediaUploadedDocument) for x in f)


def test_photos_and_videos_share_an_album(files, log):
    c = FakeClient()
    send(c, files("a.png", "b.mp4"), log)
    ((f, kw),) = c.calls
    assert names(f) == ["a.png", "b.mp4"] and kw["force_document"] is False and kw["supports_streaming"]


def test_reply_silent_and_plain_reach_every_message(files, log):
    c = FakeClient()
    send(c, files("a.pdf", "b.pdf"), log, album=False, reply_to=42, silent=True, plain=True, caption="x")
    for _, kw in c.calls:
        assert kw["reply_to"] == 42 and kw["silent"] is True and kw["parse_mode"] is None
    c = FakeClient()
    send(c, files("a.pdf"), log, caption="**x**")
    assert c.calls[0][1]["parse_mode"] == "md"


# ── captions ─────────────────────────────────────────────────────────────────
def test_caption_length_is_the_parsed_text():
    md = "**bold** __it__ `code` [link](https://example.com/very/long/path?x=1) and more text here ok"
    assert tg.caption_len(md, plain=False) == 39 < len(md) == tg.caption_len(md, plain=True)
    assert tg.caption_len("ёж 😀", plain=True) == 5  # UTF-16 units, as Telegram counts


# ── the real Telethon ────────────────────────────────────────────────────────
def test_real_telethon_builds_the_requests_we_rely_on(tmp_path, log):
    """No fake: Telethon turns our arguments into MTProto requests and only the network is cut.
    What this pins down is what decides how a file arrives; a Telethon upgrade must keep it."""
    def run(name, data=b"x" * 64, **kw):
        p = tmp_path / name
        p.write_bytes(data)
        c = OfflineTelethon()
        send(c, [p], log, caption="see **this**", **kw)
        (m,) = c.uploaded()
        return m, c.requests[-1]

    def attr(m, cls):
        return next((a for a in m.attributes if isinstance(a, cls)), None)

    m, req = run("my chart.png", png(1600, 900))
    assert isinstance(m, T.InputMediaUploadedPhoto)  # header read, within 2560 px: never decoded
    assert req.message == "see this" and isinstance(req.entities[0], T.MessageEntityBold)
    m, _ = run("my chart.png", png(1600, 900), as_file=True)
    assert isinstance(m, T.InputMediaUploadedDocument) and attr(m, T.DocumentAttributeFilename).file_name == "my chart.png"
    m, _ = run("bomb.png", png(100_000, 100_000))
    assert isinstance(m, T.InputMediaUploadedDocument) and m.mime_type == "image/png"
    m, _ = run("a.webp")
    assert m.force_file is True  # never a sticker
    m, _ = run("loop.gif", gif(320, 240))  # image/gif + image size + Animated, as Telegram Desktop sends it
    assert m.mime_type == "image/gif" and not m.force_file and attr(m, T.DocumentAttributeAnimated)
    assert (attr(m, T.DocumentAttributeImageSize).w, attr(m, T.DocumentAttributeImageSize).h) == (320, 240)
    assert attr(m, T.DocumentAttributeFilename).file_name == "loop.gif"
    m, _ = run("loop.gif", gif(320, 240), as_file=True)
    assert m.force_file is True and attr(m, T.DocumentAttributeAnimated) is None
    m, _ = run("clip.mkv")
    assert m.mime_type == "video/x-matroska" and m.nosound_video is True
    assert attr(m, T.DocumentAttributeVideo).supports_streaming is True
    m, _ = run("talk.m4a")
    assert attr(m, T.DocumentAttributeAudio) is not None and m.mime_type == "audio/mp4"
    m, _ = run("отчёт.pdf")
    assert attr(m, T.DocumentAttributeFilename).file_name == "отчёт.pdf" and m.force_file is True


def test_real_telethon_album_puts_the_caption_on_the_first_item(tmp_path, log):
    a, b = tmp_path / "a.png", tmp_path / "b.mp4"
    a.write_bytes(png(800, 600))
    b.write_bytes(b"v" * 64)
    c = OfflineTelethon()
    sent = send(c, [a, b], log, caption="two")
    (req,) = [r for r in c.requests if isinstance(r, functions.messages.SendMultiMediaRequest)]
    assert [type(x.media) for x in req.multi_media] == [T.InputMediaPhoto, T.InputMediaDocument]
    assert [x.message for x in req.multi_media] == ["two", ""]
    assert sent == [[]] and log.status()["hour"] == 1  # unmatched answer: still sent, still counted


def test_a_send_telethon_cannot_match_still_counts(files, log):
    class NoAnswer(FakeClient):
        async def send_file(self, entity, file, **kw):
            await super().send_file(entity, file, **kw)
            return None

    sent = send(NoAnswer(), files("a.pdf"), log)
    assert sent == [[]] and log.status()["hour"] == 1 and tg.sent_ids(sent[0]) == "id unknown"


# ── failures ─────────────────────────────────────────────────────────────────
def test_flood_wait_midway_says_what_went_out_and_pauses_sends(files, log):
    c = FakeClient(fail={1: errors.FloodWaitError(request=None, capture=120)})
    with pytest.raises(tg.Fail) as e:
        send(c, files("a.pdf", "b.pdf", "c.pdf"), log, album=False)
    msg = str(e.value)
    assert "flood wait 120s" in msg and "1 of 3 messages went out first (#101)" in msg
    assert "do not send those again" in msg and len(c.calls) == 1
    assert log.status()["flood_until"] > 0 and log.status()["hour"] == 1


def test_refusal_before_anything_went_out_says_so(files, log):
    c = FakeClient(fail={0: errors.ChatSendMediaForbiddenError(request=None)})
    with pytest.raises(tg.Fail, match="ChatSendMediaForbidden.*Nothing went out"):
        send(c, files("a.png"), log)


def test_session_errors_are_not_translated(files, log):
    c = FakeClient(fail={0: errors.AuthKeyUnregisteredError(request=None)})
    with pytest.raises(errors.AuthKeyUnregisteredError):
        send(c, files("a.png"), log)


# ── send_message: approval mode (the default) ────────────────────────────────
import re  # noqa: E402

from conftest import OWNER, FakeBotHTTP  # noqa: E402
from mcp.server.fastmcp.exceptions import ToolError  # noqa: E402

import main  # noqa: E402
import uploads  # noqa: E402


@pytest.fixture
def wired(monkeypatch, tmp_path):
    """main.send_message with Telegram faked: Ivan, a prior chat, a FakeClient taking the sends.
    A text send shows up in client.calls as ("TEXT", text), so the order is visible."""
    c = FakeClient()
    log = tg.SendLog(tmp_path / "sendlog.jsonl")
    log.min_interval = 0
    bot = FakeBotHTTP()

    async def get_messages(entity, limit=1):
        return [object()]

    async def fake_client():
        return c

    async def fake_resolve(client, spec, policy):
        return IVAN

    async def fake_text(client, entity, text, **kw):
        c.calls.append(("TEXT", text))
        msg = c._msg()
        kw["log"].record(tg.peer_id(entity), msg.id, kw["is_new"])
        return msg

    c.get_messages = get_messages
    monkeypatch.setattr(main.account, "client", fake_client)
    monkeypatch.setattr(main.tg, "resolve_peer", fake_resolve)
    monkeypatch.setattr(main.tg, "send_text", fake_text)
    monkeypatch.setattr(main, "sendlog", log)
    monkeypatch.setattr(main.approvals, "http", bot)
    monkeypatch.setattr(main.approvals, "drafts", {})
    monkeypatch.setattr(main.approvals, "_submits", [])
    return SimpleNamespace(client=c, bot=bot, log=log)


def upload(name, data=None):
    async def gen():
        yield data if data is not None else (png(1600, 900) if name.endswith(".png") else b"x" * 100)
    return asyncio.run(main.store.receive(main.store.new_ticket(), name, gen()))


async def tap(d, action="ok"):
    await main.approvals.handle({"update_id": 1, "callback_query": {
        "id": "q", "from": {"id": OWNER}, "data": f"{action}:{d.id}", "message": {"message_id": d.card_id}}})


async def submit(*args, **kw):
    """send_message, and the draft it created."""
    before = set(main.approvals.drafts)
    out = await main.send_message(*args, **kw)
    (d,) = [x for k, x in main.approvals.drafts.items() if k not in before]
    return out, d


def test_card_lists_the_files_and_one_tap_sends_them_once(wired):
    a, b = upload("chart.png"), upload("plot.png")

    async def go():
        out, d = await submit("@ivan", "Two charts", files=[a.id, b.id])
        assert "waits for the owner" in out and "Nothing is sent yet" in out
        assert wired.client.calls == []
        card = wired.bot.sent("sendMessage")[-1]["text"]
        assert f"📎 chart.png · {uploads.human(a.size)} · photo (compressed)" in card
        assert "📎 plot.png" in card and "album of 2 · the text as its caption" in card
        assert card.index("Two charts") < card.index("📎 chart.png")
        await tap(d)
        await tap(d)  # a double tap must not send twice
        await asyncio.wait_for(d.done.wait(), 5)
        return d

    d = asyncio.run(go())
    assert d.status == "sent", d.result
    ((f, kw),) = wired.client.calls
    assert names(f) == ["chart.png", "plot.png"] and kw["caption"] == "Two charts"
    assert wired.log.status()["hour"] == 1 and d.result.startswith("#101–#102 at ")


def test_a_file_gone_before_the_tap_fails_the_draft(wired):
    rec = upload("gone.pdf")

    async def go():
        _, d = await submit("@ivan", "The report", files=[rec.id])
        os.remove(main.store.path(rec))
        await tap(d)
        await asyncio.wait_for(d.done.wait(), 5)
        return d

    d = asyncio.run(go())
    assert d.status == "failed" and "gone.pdf" in d.result and "Nothing was sent" in d.result
    assert wired.client.calls == [] and wired.log.status()["hour"] == 0


def test_files_outlive_the_draft(wired):
    rec = upload("later.pdf")
    _, d = asyncio.run(submit("@ivan", "", files=[rec.id]))
    assert main.store.get(rec.id).expires > d.expires


def test_caption_or_text_first_by_parsed_length(wired, monkeypatch):
    monkeypatch.setattr(main, "CAPTION_MAX", 40)
    rec = upload("a.pdf")
    md = "**" + "b" * 30 + "** [x](https://example.com/" + "p" * 40 + ")"  # raw 82, visible 31
    cases = [(md, False, False), ("c" * 41, False, True), (md, True, True)]
    for text, plain, first in cases:
        main.approvals.drafts.clear()
        _, d = asyncio.run(submit("@ivan", text, files=[rec.id], plain=plain))
        assert d.text_first is first, (text, plain)
        card = wired.bot.sent("sendMessage")[-1]["text"]
        assert ("the text first, as its own message (over 40 characters" in card) is first
        assert ("1 file · the text as its caption" in card) is not first


def test_long_text_goes_first_and_the_files_follow_without_a_caption(wired, monkeypatch):
    monkeypatch.setattr(main, "CAPTION_MAX", 10)
    a, b = upload("a.pdf"), upload("b.pdf")

    async def go():
        _, d = await submit("@ivan", "A text longer than ten", files=[a.id, b.id], reply_to=7)
        await tap(d)
        await asyncio.wait_for(d.done.wait(), 5)
        return d

    d = asyncio.run(go())
    assert d.status == "sent", d.result
    (t, text), (f, kw) = wired.client.calls
    assert (t, text) == ("TEXT", "A text longer than ten")
    assert names(f) == ["a.pdf", "b.pdf"] and kw["caption"] is None and kw["reply_to"] == 7
    assert wired.log.status()["hour"] == 2 and d.result.startswith("#101 (text), #102–#103 at ")


def test_empty_text_needs_files(wired):
    rec = upload("only.pdf")
    out, d = asyncio.run(submit("@ivan", "", files=[rec.id]))
    assert "1 file, no text" in wired.bot.sent("sendMessage")[-1]["text"] and d.text == ""
    with pytest.raises(ToolError, match="empty message"):
        asyncio.run(main.send_message("@ivan", " \n"))


def test_unknown_ids_send_nothing(wired):
    rec = upload("real.pdf")
    with pytest.raises(ToolError) as e:
        asyncio.run(main.send_message("@ivan", "hi", files=[rec.id, "f_000000000000"]))
    assert "f_000000000000" in str(e.value) and rec.id not in str(e.value) and "Nothing was sent" in str(e.value)
    assert main.approvals.drafts == {} and wired.client.calls == []


def test_a_mixed_album_is_refused_before_any_draft(wired):
    p, d = upload("chart.png"), upload("report.pdf")
    with pytest.raises(ToolError, match="as_file=true.*album=false"):
        asyncio.run(main.send_message("@ivan", "hi", files=[p.id, d.id]))
    assert main.approvals.drafts == {}
    asyncio.run(submit("@ivan", "hi", files=[p.id, d.id], as_file=True))
    asyncio.run(submit("@ivan", "hi", files=[p.id, d.id], album=False))


def test_the_card_fits_one_bot_message_at_twenty_long_names(wired):
    recs = [upload("ж" * 95 + f"{i:02d}.pdf") for i in range(20)]
    ids = [r.id for r in recs]
    with pytest.raises(ToolError, match="at most 20"):
        asyncio.run(main.send_message("@ivan", "x", files=ids + [upload("21.pdf").id]))
    with pytest.raises(ToolError, match="lists the files") as e:
        asyncio.run(main.send_message("@ivan", "x" * 3500, files=ids))
    limit = int(re.search(r"the limit is (\d+)", str(e.value)).group(1))
    _, d = asyncio.run(submit("@ivan", "x" * limit, files=ids, silent=True, reply_to=5))
    card = wired.bot.sent("sendMessage")[-1]["text"]
    assert len(card.encode("utf-16-le")) // 2 <= 4096 and card.count("📎") == 20
    assert "2 albums (10 + 10) · the text first, as its own message" in card  # over 1024: not a caption


# ── the 👀 Files button ──────────────────────────────────────────────────────
import approvals  # noqa: E402


async def preview(d, who=OWNER):
    await main.approvals.handle({"update_id": 2, "callback_query": {
        "id": "p", "from": {"id": who}, "data": f"pv:{d.id}", "message": {"message_id": d.card_id}}})
    for _ in range(200):
        await asyncio.sleep(0.01)
        if d.id not in main.approvals._previewing:
            return


def test_preview_sends_the_owner_the_files_and_the_draft_stays_pending(wired):
    a, b = upload("chart.png"), upload("отчёт.pdf", b"%PDF-1.4 x")

    async def go():
        _, plain = await submit("@ivan", "text only")
        assert len(wired.bot.sent("sendMessage")[-1]["reply_markup"]["inline_keyboard"]) == 1
        _, d = await submit("@ivan", "Look", files=[a.id, b.id], as_file=True)
        rows = wired.bot.sent("sendMessage")[-1]["reply_markup"]["inline_keyboard"]
        assert rows[1] == [{"text": "👀 Files", "callback_data": f"pv:{d.id}"}]
        await preview(d, who=OWNER + 1)  # not the owner: nothing
        assert wired.bot.sent("sendDocument") == []
        await preview(d)
        return d

    d = asyncio.run(go())
    docs = wired.bot.sent("sendDocument")
    assert [x["_file"]["name"] for x in docs] == ["chart.png", "отчёт.pdf"]
    assert docs[1]["_file"]["bytes"] == b"%PDF-1.4 x" and docs[1]["_file"]["mime"] == "application/pdf"
    assert all(x["chat_id"] == str(OWNER) for x in docs) and docs[0]["caption"] == f"👀 Draft {d.id} · 1/2"
    assert d.status == "pending" and wired.client.calls == []


def test_preview_of_a_file_over_the_bot_limit_gives_its_size_and_sha256(wired, monkeypatch):
    monkeypatch.setattr(approvals, "PREVIEW_MAX", 10)
    rec = upload("big.pdf", b"x" * 100)

    async def go():
        _, d = await submit("@ivan", "", files=[rec.id])
        await preview(d)

    asyncio.run(go())
    assert wired.bot.sent("sendDocument") == []
    note = wired.bot.sent("sendMessage")[-1]["text"]
    assert "big.pdf · 100 B is too big to preview here" in note and rec.sha256 in note


def test_a_photo_past_the_limits_is_noted_on_the_card_and_in_the_result(wired):
    rec = upload("panorama.png", png(12_000, 800))
    out, d = asyncio.run(submit("@ivan", "", files=[rec.id]))
    why = "12000x800 is past the photo limit (width + height at most 10000 px)"
    assert f"panorama.png goes as a file: {why}." in out
    assert f"📎 panorama.png · {uploads.human(rec.size)} · file — {why}" in wired.bot.sent("sendMessage")[-1]["text"]


def test_a_gif_gets_its_size_from_the_header_and_a_fake_one_is_a_file(tmp_path, log):
    real, fake = tmp_path / "real.gif", tmp_path / "fake.gif"
    real.write_bytes(gif(480, 270))
    fake.write_bytes(b"not a gif at all")
    assert (tg.probe(real).kind, tg.probe(real).width, tg.probe(real).height) == ("gif", 480, 270)
    assert tg.probe(fake).kind == "document"
    c = FakeClient()
    send(c, [fake], log)
    assert c.calls[0][1]["force_document"] is True and "attributes" not in c.calls[0][1]
