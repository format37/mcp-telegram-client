"""Telethon layer of the telegram-client MCP: one user session, peers, transcripts, sends.

Adapted from the local telegram-client skill (``tgc.py``): peer resolution, the
transcript line format, media description and the send budget behave the same,
so a chat read through this server looks exactly like one read on the desktop.

What differs is the session. Here it is a long-lived *StringSession* kept in
``DATA_DIR/session.string`` (0600), written only by ``login.py`` and read only
by this process. One auth key = one client: the server never shares that file
with anything, and never logs in by itself — a dead session is renamed out of
the way and the owner is alerted to log in again.
"""
from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import re
import time
import warnings
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from telethon import TelegramClient, errors, functions, utils
from telethon.sessions import StringSession
from telethon.tl import types as T

logger = logging.getLogger(__name__)

DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
SESSION_FILE = DATA_DIR / "session.string"
DEVICE_MODEL = os.getenv("DEVICE_MODEL", "telegram-client MCP")  # name in Settings -> Devices
APP_VERSION = "tgc-mcp 1.1"
DIALOG_SCAN = 500
LOGIN_CMD = "sudo docker exec -it mcp-telegram-client python login.py"

# Telegram's own service account. Login codes and "new login" alerts arrive here,
# so reading it would let anyone holding an MCP token finish a login on a new
# device. Unreachable through every tool, and deliberately not configurable.
SERVICE_IDS = frozenset({777000})

# Errors that mean the auth key itself is gone: revoked in Devices, expired,
# terminated, or used by a second client at once (AUTH_KEY_DUPLICATED).
UNAUTHORIZED = (errors.UnauthorizedError, errors.AuthKeyDuplicatedError)


class Fail(Exception):
    """A refusal or error whose message is meant for the caller, verbatim."""


class NotLoggedIn(Fail):
    pass


def env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    return default if v is None or not v.strip() else v.strip().lower() in ("1", "true", "yes", "on")


def env_ids(name: str) -> set[int]:
    out = set()
    for part in (os.getenv(name) or "").replace(";", ",").split(","):
        part = part.strip()
        if re.fullmatch(r"-?\d+", part):
            out.add(int(part))
    return out


# ── peers ────────────────────────────────────────────────────────────────────
_PHONE = re.compile(r"\+?\d[\d\s\-()]{6,}\d")


def kind_of(ent) -> str:
    if isinstance(ent, T.User):
        return "bot" if ent.bot else ("me" if ent.is_self else "user")
    if isinstance(ent, T.Chat):
        return "group"
    if isinstance(ent, T.Channel):
        return "group" if ent.megagroup else "channel"
    return type(ent).__name__


def username_of(ent) -> str | None:
    u = getattr(ent, "username", None)
    if not u:
        for x in getattr(ent, "usernames", None) or []:
            if getattr(x, "active", True):
                return x.username
    return u


def name_of(ent) -> str:
    n = utils.get_display_name(ent)
    return n or ("Deleted account" if getattr(ent, "deleted", False) else f"id {ent.id}")


def peer_id(ent) -> int:
    """The id form that resolves back through get_entity (-100… for channels)."""
    return utils.get_peer_id(ent)


def label(ent) -> str:
    u = username_of(ent)
    return f"{name_of(ent)} ({'@' + u + ', ' if u else ''}{kind_of(ent)}, id {peer_id(ent)})"


def ts(dt: datetime) -> str:
    return dt.astimezone().strftime("%Y-%m-%d %H:%M")


class Policy:
    """Which chats the server refuses to touch, for reading AND writing."""

    def __init__(self, *, allow_saved: bool = False, deny: set[int] | None = None):
        self.allow_saved = allow_saved
        self.deny = set(deny or ())
        self.blocked_ids: set[int] = set()  # filled at startup with the approval bot's id

    @classmethod
    def from_env(cls) -> "Policy":
        return cls(allow_saved=env_bool("TGC_ALLOW_SAVED_MESSAGES"), deny=env_ids("TGC_DENY_PEERS"))

    def check(self, ent) -> None:
        raw = getattr(ent, "id", None)
        if raw in SERVICE_IDS:
            raise Fail("That is Telegram's service chat (login codes); it is never accessible through this server.")
        if raw in self.blocked_ids:
            raise Fail("That is this server's control bot; its chat is not accessible through this server.")
        if isinstance(ent, T.User) and ent.is_self and not self.allow_saved:
            raise Fail("Saved Messages are off limits on this server (TGC_ALLOW_SAVED_MESSAGES is off).")
        if raw in self.deny or peer_id(ent) in self.deny:
            raise Fail("The owner has blocked this chat for this server (TGC_DENY_PEERS).")

    def visible(self, ent) -> bool:
        try:
            self.check(ent)
            return True
        except Fail:
            return False


async def resolve_peer(client: TelegramClient, spec: str, policy: Policy):
    """Resolve and policy-check a peer: @username, t.me link, phone, numeric id, 'me', or part of a name."""
    s = (spec or "").strip()
    if not s:
        raise Fail("peer is empty")
    ent = None
    if s.lower() in ("me", "self", "saved"):
        ent = await client.get_me()
    elif re.fullmatch(r"-?\d+", s):
        try:
            ent = await client.get_entity(int(s))
        except (ValueError, TypeError):
            async for d in client.iter_dialogs(limit=DIALOG_SCAN):
                if peer_id(d.entity) == int(s) or d.entity.id == abs(int(s)):
                    ent = d.entity
                    break
        if ent is None:
            raise Fail(f"No dialog with id {s} (ids resolve only for peers this account has seen)")
    elif s.startswith("@") or "t.me/" in s or _PHONE.fullmatch(s):
        try:
            ent = await client.get_entity(s)
        except (ValueError, TypeError) as e:
            raise Fail(f"Cannot resolve '{spec}': {e}")
    else:
        q = s.casefold()
        exact, partial = [], []
        async for d in client.iter_dialogs(limit=DIALOG_SCAN):
            if not policy.visible(d.entity):
                continue
            name = (d.name or "").casefold()
            u = (username_of(d.entity) or "").casefold()
            if q in (name, u):
                exact.append(d.entity)
            elif q in name or (u and q in u):
                partial.append(d.entity)
        hits = exact or partial
        if not hits:
            raise Fail(f"No dialog matches '{spec}'. Try @username, a phone number, or find_peer(\"{spec}\").")
        if len(hits) > 1:
            raise Fail(f"'{spec}' is ambiguous:\n" + "\n".join(f"  {label(e)}" for e in hits[:15])
                       + "\nUse the id or @username.")
        ent = hits[0]
    policy.check(ent)
    return ent


# ── media ────────────────────────────────────────────────────────────────────
NO_DOWNLOAD = {"sticker", "webpage", "geo", "contact", "poll"}


def media_info(m) -> dict | None:
    med = m.media
    if med is None:
        return None
    if isinstance(med, T.MessageMediaPhoto):
        return {"kind": "photo"}
    if isinstance(med, T.MessageMediaDocument) and m.document:
        doc = m.document
        info: dict = {"kind": "document", "size": doc.size, "mime": doc.mime_type}
        flags = set()
        for a in doc.attributes:
            if isinstance(a, T.DocumentAttributeFilename):
                info["name"] = a.file_name
            elif isinstance(a, T.DocumentAttributeAudio):
                info["duration"] = a.duration
                flags.add("voice" if a.voice else "audio")
                if a.title:
                    info["title"] = a.title
                if a.performer:
                    info["performer"] = a.performer
            elif isinstance(a, T.DocumentAttributeVideo):
                info["duration"] = a.duration
                flags.add("video_note" if a.round_message else "video")
            elif isinstance(a, T.DocumentAttributeSticker):
                flags.add("sticker")
                info["alt"] = a.alt
            elif isinstance(a, T.DocumentAttributeAnimated):
                flags.add("gif")
        for k in ("sticker", "gif", "voice", "video_note", "audio", "video"):
            if k in flags:
                info["kind"] = k
                break
        return info
    if isinstance(med, T.MessageMediaWebPage):
        wp = med.webpage
        return {"kind": "webpage", "url": getattr(wp, "url", None), "title": getattr(wp, "title", None)}
    if isinstance(med, (T.MessageMediaGeo, T.MessageMediaGeoLive, T.MessageMediaVenue)):
        geo = getattr(med, "geo", None)
        return {"kind": "geo", "lat": getattr(geo, "lat", None), "long": getattr(geo, "long", None),
                "title": getattr(med, "title", None)}
    if isinstance(med, T.MessageMediaContact):
        return {"kind": "contact", "name": " ".join(x for x in (med.first_name, med.last_name) if x),
                "phone": med.phone_number}
    if isinstance(med, T.MessageMediaPoll):
        q = med.poll.question
        return {"kind": "poll", "question": getattr(q, "text", q)}
    return {"kind": type(med).__name__.replace("MessageMedia", "").lower()}


def _dur(s) -> str:
    return f"{int(s) // 60}:{int(s) % 60:02d}" if s else ""


def _mb(n) -> str:
    return f"{n / 1e6:.1f}MB" if n else ""


_KIND_WORD = {"photo": "photo", "voice": "voice", "audio": "audio", "video": "video",
              "video_note": "video note", "gif": "gif", "document": "file"}


def media_tag(info: dict | None) -> str:
    if not info:
        return ""
    k = info["kind"]
    if k == "webpage":
        return f"[preview: {info['title']}]" if info.get("title") else ""
    if k == "sticker":
        return f"[sticker {info.get('alt') or ''}]".replace(" ]", "]")
    if k == "geo":
        return f"[location {info.get('lat')},{info.get('long')}{' ' + info['title'] if info.get('title') else ''}]"
    if k == "contact":
        return f"[contact {info.get('name')} {info.get('phone')}]"
    if k == "poll":
        return f"[poll: {info.get('question')}]"
    bits = [_KIND_WORD.get(k, k)]
    if info.get("name"):
        bits.append(info["name"])
    if info.get("duration"):
        bits.append(_dur(info["duration"]))
    if info.get("size") and k != "voice":
        bits.append(_mb(info["size"]))
    return f"[{' '.join(bits)}]"


# ── messages -> transcript lines ─────────────────────────────────────────────
def hidden_links(m) -> list[str]:
    """URLs behind words (text-url entities) that are not visible in the text itself."""
    text = m.message or ""
    return [e.url for e in (m.entities or ()) if isinstance(e, T.MessageEntityTextUrl) and e.url not in text]


async def sender_name(m, chat) -> str:
    if m.out:
        return "me"
    s = m.sender
    if s is None:
        try:
            s = await m.get_sender()
        except Exception:
            s = None
    if s is None:
        return getattr(m, "post_author", None) or name_of(chat)
    return name_of(s)


async def fwd_label(m) -> str | None:
    f = m.forward
    if not f:
        return None
    try:
        ent = await f.get_sender() or await f.get_chat()
        if ent:
            return name_of(ent)
    except Exception:
        pass
    return f.from_name or "unknown"


def fmt_line(m, who: str, tag: str, links: list[str], fwd: str | None) -> str:
    head = []
    if m.reply_to and getattr(m.reply_to, "reply_to_msg_id", None):
        head.append(f"↩#{m.reply_to.reply_to_msg_id}")
    if fwd:
        head.append(f"[fwd from {fwd}]")
    if tag:
        head.append(tag)
    text = (m.message or "").strip()
    body = " ".join(head)
    if text:
        body = (body + " " if body else "") + text
    if links:
        body += "\n[links: " + ", ".join(links) + "]"
    if m.edit_date:
        body += " (edited)"
    return f"#{m.id} [{ts(m.date)}] {who}: {body.replace(chr(10), chr(10) + '    ')}"


def parse_since(s: str) -> datetime:
    m = re.fullmatch(r"(\d+)\s*([mhdw])", s.strip())
    if m:
        n, u = int(m.group(1)), m.group(2)
        delta = {"m": timedelta(minutes=n), "h": timedelta(hours=n), "d": timedelta(days=n),
                 "w": timedelta(weeks=n)}[u]
        return datetime.now(timezone.utc) - delta
    try:
        dt = datetime.fromisoformat(s.strip())
    except ValueError:
        raise Fail(f"since takes 30m / 24h / 7d / 2w or an ISO date, not '{s}'")
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.astimezone(timezone.utc)


async def collect(client, entity, *, since=None, after_id=None, limit=None, search=None) -> list:
    """Messages oldest first. `since`/`after_id` read forward from there; otherwise the newest `limit`."""
    kw: dict = {}
    if since:
        kw.update(offset_date=since, reverse=True)
    if after_id:
        kw.update(min_id=after_id, reverse=True)
    if search:
        kw["search"] = search
    msgs = []
    async for m in client.iter_messages(entity, limit=limit, **kw):
        if isinstance(m, T.MessageService):
            continue  # "joined", "pinned", calls
        if since and m.date < since:
            continue
        msgs.append(m)
    if not kw.get("reverse"):
        msgs.reverse()
    return msgs


async def render(entity, msgs) -> tuple[list[str], int]:
    """Transcript lines + how many messages carry viewable media (photos, files)."""
    lines, n_media = [], 0
    for m in msgs:
        info = media_info(m)
        if info and info["kind"] not in NO_DOWNLOAD:
            n_media += 1
        who = await sender_name(m, entity)
        lines.append(fmt_line(m, who, media_tag(info), hidden_links(m), await fwd_label(m)))
    return lines, n_media


def header(entity, me, msgs, extra: list[str]) -> str:
    me_u = username_of(me)
    first = f"# {label(entity)} · account {'@' + me_u if me_u else name_of(me)}"
    span = f"{ts(msgs[0].date)} → {ts(msgs[-1].date)}" if msgs else "no messages"
    return first + "\n# " + " · ".join([f"{len(msgs)} messages", span] + extra)


def snippet(msg, n: int = 70) -> str:
    if msg is None:
        return ""
    if isinstance(msg, T.MessageService):
        return "[service]"
    t = (msg.message or "").replace("\n", " ").strip()
    if not t and msg.media:
        t = media_tag(media_info(msg))
    return (t[: n - 1] + "…") if len(t) > n else t


async def contacts_search(client, q: str) -> list:
    """contacts.search: contacts by name + global @username lookup; best-effort."""
    try:
        r = await client(functions.contacts.SearchRequest(q=q, limit=20))
        return list(r.users) + list(r.chats)
    except UNAUTHORIZED:
        raise
    except Exception:
        return []


# ── sending ──────────────────────────────────────────────────────────────────
def ulen(s: str) -> int:
    """Telegram counts UTF-16 code units."""
    return len(s.encode("utf-16-le")) // 2


class SendLog:
    """Send ledger enforcing the SEND_* budget (24 h window, pruned after 48 h)."""

    def __init__(self, path: Path | None = None):
        self.path = path or (DATA_DIR / "sendlog.jsonl")
        self.min_interval = float(os.getenv("SEND_MIN_INTERVAL_S", "5"))
        self.per_hour = int(os.getenv("SEND_PER_HOUR", "20"))
        self.per_day = int(os.getenv("SEND_PER_DAY", "60"))
        self.new_per_day = int(os.getenv("SEND_NEW_PEERS_PER_DAY", "3"))
        self.rows: list[dict] = []
        if self.path.exists():
            for line in self.path.read_text("utf-8").splitlines():
                try:
                    self.rows.append(json.loads(line))
                except ValueError:
                    pass

    def _sends(self, window: float) -> list[dict]:
        cut = time.time() - window
        return [r for r in self.rows if r.get("type", "send") == "send" and r["ts"] > cut]

    def status(self) -> dict:
        hour, day = self._sends(3600), self._sends(86400)
        return {"hour": len(hour), "day": len(day),
                "new_peers": {r["peer"] for r in day if r.get("new")},
                "last": max((r["ts"] for r in day), default=0),
                "next_hour_slot": (min(r["ts"] for r in hour) + 3600) if hour else 0,
                "next_day_slot": (min(r["ts"] for r in day) + 86400) if day else 0,
                "flood_until": max((r["until"] for r in self.rows if r.get("type") == "flood"), default=0)}

    def summary(self) -> str:
        st = self.status()
        s = (f"sends: last hour {st['hour']}/{self.per_hour} · last 24h {st['day']}/{self.per_day}"
             f" · new contacts 24h {len(st['new_peers'])}/{self.new_per_day}")
        if st["flood_until"] > time.time():
            s += f" · PAUSED until {ts(datetime.fromtimestamp(st['flood_until'], timezone.utc))} (flood wait)"
        return s

    def check(self, n: int, peer: int, is_new: bool) -> None:
        st = self.status()
        now = time.time()
        if st["flood_until"] > now:
            raise Fail(f"sends are paused until {ts(datetime.fromtimestamp(st['flood_until'], timezone.utc))} "
                       "(Telegram flood wait / peer flood earlier)")
        if st["hour"] + n > self.per_hour:
            raise Fail(f"hourly send budget used ({st['hour']}/{self.per_hour}); next slot at "
                       f"{ts(datetime.fromtimestamp(st['next_hour_slot'], timezone.utc))}")
        if st["day"] + n > self.per_day:
            raise Fail(f"daily send budget used ({st['day']}/{self.per_day}); next slot at "
                       f"{ts(datetime.fromtimestamp(st['next_day_slot'], timezone.utc))}")
        if is_new and peer not in st["new_peers"] and len(st["new_peers"]) >= self.new_per_day:
            raise Fail(f"new-contact budget used ({len(st['new_peers'])}/{self.new_per_day} in 24h) — "
                       "first messages to strangers are what gets accounts limited")

    async def pace(self) -> None:
        wait = self.status()["last"] + self.min_interval - time.time()
        if wait > 0:
            await asyncio.sleep(wait)

    def record(self, peer: int, msg_id: int, is_new: bool) -> None:
        self.rows.append({"type": "send", "ts": time.time(), "peer": peer, "msg": msg_id, "new": is_new})
        self._save()

    def record_flood(self, seconds: float) -> None:
        self.rows.append({"type": "flood", "ts": time.time(), "until": time.time() + seconds})
        self._save()

    def _save(self) -> None:
        now = time.time()
        keep = [r for r in self.rows if r["ts"] > now - 172800 or r.get("until", 0) > now]
        self.rows = keep
        write_private(self.path, "".join(json.dumps(r) + "\n" for r in keep))


def _send_failure(e: Exception, entity, log: SendLog) -> Fail | None:
    """The caller-facing Fail for a Telegram error raised by a send, or None if it is not one.
    A flood wait or PeerFlood pauses every send. Call only after UNAUTHORIZED has been re-raised."""
    if isinstance(e, errors.FloodWaitError):
        log.record_flood(e.seconds)
        return Fail(f"Telegram flood wait {e.seconds}s — sends paused for that long. Do not retry in a loop.")
    if isinstance(e, errors.PeerFloodError):
        log.record_flood(24 * 3600)
        return Fail("PeerFlood: Telegram flagged this account for spam-like activity. Sends paused for 24h.")
    if isinstance(e, (errors.UserPrivacyRestrictedError, errors.UserIsBlockedError, errors.ChatWriteForbiddenError)):
        return Fail(f"Cannot write to {label(entity)}: {type(e).__name__}")
    if isinstance(e, errors.RPCError):
        return Fail(f"Telegram refused it: {type(e).__name__} ({e})")
    return None


async def send_text(client, entity, text: str, *, reply_to: int | None, plain: bool, silent: bool,
                    no_preview: bool, log: SendLog, is_new: bool):
    """One message, paced and preceded by a short 'typing…'. Flood errors pause all sends."""
    pid = peer_id(entity)
    try:
        await log.pace()
        async with client.action(entity, "typing"):
            await asyncio.sleep(min(1.0 + len(text) / 30, 6.0))
        msg = await client.send_message(entity, text, reply_to=reply_to or None,
                                        parse_mode=None if plain else "md", silent=silent,
                                        link_preview=not no_preview)
    except UNAUTHORIZED:
        raise
    except errors.RPCError as e:
        raise _send_failure(e, entity, log) from e
    log.record(pid, msg.id, is_new)
    return msg


# ── outgoing files: what Telethon will make of a path ────────────────────────
# Telethon sends a path as a photo only for .jpg/.jpeg/.png (utils.is_image), and
# recognises audio and video by the mime type of the extension. Register every
# extension named here, so that does not depend on the image's /etc/mime.types.
PHOTO_EXT = (".jpg", ".jpeg", ".png")
KIND_EXT = {**dict.fromkeys(PHOTO_EXT, "photo"),
            **dict.fromkeys((".mp4", ".mov", ".webm", ".mkv"), "video"),
            **dict.fromkeys((".mp3", ".m4a", ".ogg", ".flac", ".wav"), "audio"),
            ".gif": "gif"}
for _ext, _mime in ((".jpg", "image/jpeg"), (".jpeg", "image/jpeg"), (".png", "image/png"),
                    (".gif", "image/gif"), (".webp", "image/webp"), (".heic", "image/heic"),
                    (".mp4", "video/mp4"), (".mov", "video/quicktime"), (".webm", "video/webm"),
                    (".mkv", "video/x-matroska"), (".mp3", "audio/mpeg"), (".m4a", "audio/mp4"),
                    (".ogg", "audio/ogg"), (".flac", "audio/flac"), (".wav", "audio/x-wav")):
    mimetypes.add_type(_mime, _ext)


@dataclass
class FileInfo:
    mime: str
    kind: str  # photo | video | audio | gif | document
    width: int | None = None
    height: int | None = None


def image_size(path) -> tuple[int, int] | None:
    """Width and height of a PNG or JPEG from its header alone, or None.

    Never load(), verify(), convert() or thumbnail(): they decode pixels, and a
    decompression bomb would take the container past its memory cap. Image.open
    raises DecompressionBombError on a huge header and only warns on a large
    one, so the warning is made an error here too; past either limit the file
    is not an image as far as sending goes."""
    try:
        from PIL import Image

        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path, formats=("PNG", "JPEG")) as im:
                w, h = im.size
        cap = Image.MAX_IMAGE_PIXELS or 89_478_485
    except Exception:
        return None
    return (w, h) if 0 < w and 0 < h and w * h <= cap else None


def gif_size(path) -> tuple[int, int] | None:
    """The logical screen size from the 10-byte GIF header, or None if it is not a GIF."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(10)
    except OSError:
        return None
    if len(head) < 10 or head[:6] not in (b"GIF87a", b"GIF89a"):
        return None
    w, h = int.from_bytes(head[6:8], "little"), int.from_bytes(head[8:10], "little")
    return (w, h) if w and h else None


def probe(path, name: str | None = None) -> FileInfo:
    """Kind and mime type from the extension of `name` (default: the path's basename); for a
    photo or a GIF, its size from the header. A file whose header does not read cleanly is a
    document, so nothing ever hands it to Telethon's photo resize or sends it as an animation."""
    ext = os.path.splitext(name or os.path.basename(path))[1].lower()
    mime = (mimetypes.guess_type("x" + ext)[0] if ext else None) or "application/octet-stream"
    kind = KIND_EXT.get(ext, "document")
    if kind in ("photo", "gif"):
        dims = image_size(path) if kind == "photo" else gif_size(path)
        if dims is None:
            return FileInfo(mime, "document")
        return FileInfo(mime, kind, *dims)
    return FileInfo(mime, kind)


# ── sending files ────────────────────────────────────────────────────────────
ALBUM_MAX = 10
PHOTO_MAX_BYTES = 10_000_000   # Telegram's limits for a photo; past them it goes as a file.
PHOTO_MAX_SIDES = 10_000       # width + height. Telethon decodes and shrinks photos over 2560 px,
PHOTO_MAX_RATIO = 20           # so these also bound that decode at about 5000 x 5000.
MODE_WORD = {"photo": "photo", "video": "video", "audio": "audio", "animation": "GIF", "document": "file"}
_ALBUM_GROUP = {"photo": "media", "video": "media", "audio": "audio", "document": "document"}  # a GIF: none


def photo_refusal(size: int, width: int | None, height: int | None) -> str:
    """Why Telegram would not take this image as a photo, or ''."""
    if size > PHOTO_MAX_BYTES:
        return "over 10 MB, too big for a photo"
    if not (width and height):
        return "no readable image header"
    if width + height > PHOTO_MAX_SIDES:
        return f"{width}x{height} is past the photo limit (width + height at most 10000 px)"
    if max(width, height) > PHOTO_MAX_RATIO * min(width, height):
        return f"{width}x{height} is too narrow for a photo (more than 20:1)"
    return ""


def send_mode(info: FileInfo, size: int, as_file: bool) -> tuple[str, str]:
    """How one file goes out, (photo | video | audio | animation | document, note): the note
    says why a photo goes as a file. as_file sends everything as a file, original bytes."""
    if as_file:
        return "document", ""
    if info.kind == "photo":
        why = photo_refusal(size, info.width, info.height)
        return ("document", why) if why else ("photo", "")
    return {"video": "video", "audio": "audio", "gif": "animation"}.get(info.kind, "document"), ""


@dataclass
class FilePlan:
    modes: list[str]            # per file, see send_mode
    notes: list[str]            # per file: why a photo goes as a file
    batches: list[list[int]]    # file indexes per Telegram message: an album, or one file


def plan_files(files: list[tuple[str, int, FileInfo]], *, as_file: bool, album: bool) -> FilePlan:
    """Group (name, size, info) files into messages. Telegram puts photos and videos together,
    audio only with audio, files only with files, at most 10 per album, and never a GIF."""
    pairs = [send_mode(info, size, as_file) for _, size, info in files]
    modes, notes = [m for m, _ in pairs], [why for _, why in pairs]
    n = len(files)
    if not album or n == 1:
        return FilePlan(modes, notes, [[i] for i in range(n)])
    groups = {_ALBUM_GROUP.get(m) for m in modes}
    if None in groups or len(groups) > 1:
        each = ", ".join(f"{name} → {MODE_WORD[m]}" for (name, _, _), m in zip(files, modes))
        raise Fail("These files cannot share an album: Telegram groups photos and videos together, audio "
                   "only with audio and files only with files, and a GIF never joins an album. "
                   f"Here: {each}. Send them with as_file=true (one album of files, original bytes) "
                   "or with album=false (one message per file).")
    return FilePlan(modes, notes, [list(range(i, min(i + ALBUM_MAX, n))) for i in range(0, n, ALBUM_MAX)])


def caption_len(text: str, plain: bool) -> int:
    """What Telegram counts against the caption limit: UTF-16 units of the text it stores,
    i.e. after Telethon's markdown is parsed out (markup can be most of the raw length)."""
    if not plain:
        from telethon.extensions import markdown

        text = markdown.parse(text)[0]
    return ulen(text)


async def _upload_arg(client, path: str, info: FileInfo, mode: str):
    """What send_file gets for one file: the path, except for audio. Without hachoir Telethon
    attaches no audio attribute and an .mp3 arrives as a plain file, so audio goes up first and
    is sent as media carrying that attribute. Its duration is sent as 0; Telegram measures the
    real one itself (an mp3 sent so on 2026-11-05 showed 0:04)."""
    if mode != "audio":
        return path
    handle = await client.upload_file(path)
    return T.InputMediaUploadedDocument(
        file=handle, mime_type=info.mime,
        attributes=[T.DocumentAttributeAudio(duration=0),
                    T.DocumentAttributeFilename(os.path.basename(path))])


async def send_files(client, entity, paths, *, caption: str | None, as_file: bool, album: bool,
                     reply_to: int | None, plain: bool, silent: bool, log: SendLog, is_new: bool) -> list[list]:
    """Files as albums of up to 10, or one message per file; `caption` on the first message.
    An album is one send for the budget. Returns the sent messages, one list per Telegram message.

    Every path must end with the name the recipient should see: Telethon takes photo or
    document, the mime type and the shown filename from it."""
    paths = [str(p) for p in paths]
    infos = [probe(p) for p in paths]
    plan = plan_files([(os.path.basename(p), os.path.getsize(p), i) for p, i in zip(paths, infos)],
                      as_file=as_file, album=album)
    pid = peer_id(entity)
    log.check(len(plan.batches), pid, is_new)
    sent: list[list] = []
    try:
        for k, batch in enumerate(plan.batches):
            await log.pace()
            items = [await _upload_arg(client, paths[i], infos[i], plan.modes[i]) for i in batch]
            video = any(plan.modes[i] == "video" for i in batch)
            extra = {}
            if plan.modes[batch[0]] == "animation":  # a GIF, always alone
                # As the API docs ask (Animated) and Telegram Desktop sends it (plus the image
                # size); Telethon adds neither. Telegram then converts the GIF to an MP4
                # animation: seen with a 250 KB ffmpeg GIF. A 4.6 KB two-colour test GIF stayed
                # a plain file however it was sent.
                g = infos[batch[0]]
                extra["attributes"] = [T.DocumentAttributeImageSize(g.width, g.height), T.DocumentAttributeAnimated()]
            res = await client.send_file(
                entity, items if len(items) > 1 else items[0], caption=caption if k == 0 else None,
                parse_mode=None if plain else "md", reply_to=reply_to or None, silent=silent,
                force_document=plan.modes[batch[0]] == "document",  # a batch is all files or none
                supports_streaming=video, nosound_video=True if video else None, **extra)
            # Telethon returns None for a message it cannot match in Telegram's answer. The
            # send still happened: count it, never report it as not sent.
            msgs = [m for m in (res if isinstance(res, list) else [res]) if m is not None]
            log.record(pid, msgs[0].id if msgs else None, is_new)
            sent.append(msgs)
    except UNAUTHORIZED:
        raise
    except Exception as e:
        fail = _send_failure(e, entity, log)
        if fail is None:
            logger.exception("sending files to %s failed", pid)
            fail = Fail(f"Sending the files failed: {type(e).__name__}: {e}")
        went = ", ".join(sent_ids(m) for m in sent)
        raise Fail(f"{fail} " + (f"{len(sent)} of {len(plan.batches)} messages went out first ({went}); "
                                 "do not send those again." if sent else "Nothing went out.")) from e
    return sent


def sent_ids(msgs: list) -> str:
    """#101, or #101–#103 for an album; "id unknown" when Telethon could not say."""
    if not msgs:
        return "id unknown"
    return f"#{msgs[0].id}" + (f"–#{msgs[-1].id}" if len(msgs) > 1 else "")


# ── files ────────────────────────────────────────────────────────────────────
def write_private(path: Path, text: str) -> None:
    """Atomic write, 0600 from the first byte (a session string is full account access)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


# ── the account ──────────────────────────────────────────────────────────────
def new_client(session: str, api_id: int, api_hash: str, *, updates: bool = False) -> TelegramClient:
    return TelegramClient(StringSession(session), api_id, api_hash, receive_updates=updates,
                          flood_sleep_threshold=30, request_retries=3, connection_retries=3, timeout=20,
                          device_model=DEVICE_MODEL, app_version=APP_VERSION)


class Account:
    """The one Telegram session this server acts as. Connects lazily, re-reads the
    session file after a re-login, and retires a dead session instead of retrying it."""

    def __init__(self, api_id: int, api_hash: str, owner_id: int, on_dead=None):
        self.api_id, self.api_hash, self.owner_id = api_id, api_hash, owner_id
        self.on_dead = on_dead  # async callable(reason: str)
        self.state = "not_logged_in"  # | authorized | revoked | wrong_account
        self.me = None
        self._client: TelegramClient | None = None
        self._mtime: int | None = None
        self._lock = asyncio.Lock()

    async def _drop(self) -> None:
        c, self._client, self.me = self._client, None, None
        if c is not None:
            try:
                await c.disconnect()
            except Exception:
                pass

    async def client(self) -> TelegramClient:
        dead_reason = None
        async with self._lock:
            if not SESSION_FILE.exists():
                await self._drop()
                if self.state != "revoked":
                    self.state = "not_logged_in"
                raise NotLoggedIn(f"Telegram is not logged in on the server. The owner must run on the VPS: {LOGIN_CMD}")
            mtime = SESSION_FILE.stat().st_mtime_ns
            if self._client is not None and mtime != self._mtime:
                logger.info("session file changed (re-login) -> reconnecting")
                await self._drop()
            if self._client is None:
                c = new_client(SESSION_FILE.read_text("utf-8").strip(), self.api_id, self.api_hash)
                await c.connect()
                try:
                    ok = await c.is_user_authorized()
                    me = await c.get_me() if ok else None
                except UNAUTHORIZED as e:
                    ok, me = False, None
                    dead_reason = type(e).__name__
                if not ok:
                    await c.disconnect()
                    dead_reason = dead_reason or "not authorized"
                    self._retire_file()
                else:
                    if self.owner_id and me.id != self.owner_id:
                        await c.disconnect()
                        self.state = "wrong_account"
                        raise Fail(f"The session belongs to user id {me.id}, not TGC_OWNER_ID. "
                                   f"Log in again with the right account: {LOGIN_CMD} --force")
                    self._client, self.me, self._mtime = c, me, mtime
                    self.state = "authorized"
                    logger.info("Telegram session up as user id %s", me.id)
            elif not self._client.is_connected():
                await self._client.connect()
            client = self._client
        if dead_reason:
            await self._dead(dead_reason)
            raise NotLoggedIn(f"The Telegram session is no longer valid ({dead_reason}). "
                              f"The owner has been alerted to log in again: {LOGIN_CMD}")
        return client

    def _retire_file(self) -> None:
        if SESSION_FILE.exists():
            dead = SESSION_FILE.with_name(f"session.dead-{int(time.time())}")
            os.replace(SESSION_FILE, dead)
            for old in sorted(SESSION_FILE.parent.glob("session.dead-*"))[:-3]:
                old.unlink(missing_ok=True)
        self.state = "revoked"

    async def mark_dead(self, reason: str) -> None:
        async with self._lock:
            await self._drop()
            self._retire_file()
        await self._dead(reason)

    async def _dead(self, reason: str) -> None:
        logger.warning("Telegram session retired: %s", reason)
        if self.on_dead:
            try:
                await self.on_dead(reason)
            except Exception:
                logger.exception("on_dead callback failed")

    async def close(self) -> None:
        async with self._lock:
            await self._drop()
