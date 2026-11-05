"""telegram-client MCP — the owner's own Telegram account (Telethon user session) over MCP.

Reads: list_dialogs, find_peer, read_history, search_chat, get_media.
Writes: send_message only, text and files. Files reach it through upload_link: the agent
uploads with curl to a short-lived ticket URL (uploads.py), which sends nothing by itself.
Two modes (TGC_SEND_APPROVAL):
  on  (default) — every send is a draft the owner approves with a tap in the
        control bot (approvals.py); the agent cannot approve its own draft.
  off — send_message sends at once; the control bot logs each send silently.
Either way the owner can /lock everything from Telegram, and the agent-facing
text (instructions, tool descriptions) describes only the active mode.

Never exposed: login, logout, session management, deleting or editing
messages, Telegram's service chat (login codes), the approval bot's chat and,
by default, Saved Messages.
"""
from __future__ import annotations

import asyncio
import contextlib
import hmac
import io
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

for _candidate in (Path(".env"), Path(__file__).resolve().parent / ".env",
                   Path(__file__).resolve().parent.parent / ".env"):
    if _candidate.is_file():
        load_dotenv(_candidate)
        break

import uvicorn  # noqa: E402
from mcp.server.fastmcp import FastMCP  # noqa: E402
from mcp.server.fastmcp import Image as MCPImage  # noqa: E402
from mcp.server.fastmcp.exceptions import ToolError  # noqa: E402
from mcp.server.transport_security import TransportSecuritySettings  # noqa: E402
from mcp.types import TextContent  # noqa: E402
from starlette.applications import Starlette  # noqa: E402
from starlette.middleware.base import BaseHTTPMiddleware  # noqa: E402
from starlette.responses import JSONResponse  # noqa: E402
from starlette.routing import Mount, Route  # noqa: E402

import tg  # noqa: E402
import uploads  # noqa: E402
from approvals import LOCKED_MSG, RULE, Approvals, Draft, files_block  # noqa: E402

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("tgc")
# httpx logs every request URL at INFO, and the Bot API URL carries the bot token.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telethon").setLevel(logging.WARNING)

# ── config (refuse to start rather than run half-guarded) ────────────────────
MCP_NAME = os.getenv("MCP_NAME", "telegram-client")
_safe_name = re.sub(r"[^a-z0-9_-]", "-", MCP_NAME.lower()).strip("-") or "service"
BASE_PATH = f"/{_safe_name}"
STREAM_PATH = f"{BASE_PATH}/"
PORT = int(os.getenv("PORT", "8023"))


def _required(name: str) -> str:
    v = (os.getenv(name) or "").strip()
    if not v:
        raise SystemExit(f"{name} is required — see .env.example")
    return v


TOKENS = {t.strip() for t in os.getenv("MCP_TOKENS", "").split(",") if t.strip()}
if not TOKENS:
    raise SystemExit("MCP_TOKENS is required: this server is full access to a Telegram account. "
                     "Generate one with: openssl rand -hex 24")
if any(len(t) < 32 for t in TOKENS):
    raise SystemExit("every MCP token must be at least 32 characters (openssl rand -hex 24)")
API_ID = int(_required("TG_API_ID"))
API_HASH = _required("TG_API_HASH")
OWNER_ID = int(_required("TGC_OWNER_ID"))
BOT_TOKEN = _required("APPROVAL_BOT_TOKEN")

SEND_APPROVAL = tg.env_bool("TGC_SEND_APPROVAL", True)   # off: sends go out without a tap
NOTIFY_SENDS = tg.env_bool("TGC_NOTIFY_SENDS", True)      # off-mode: silent log line per send
APPROVAL_TTL_S = 60 * float(os.getenv("APPROVAL_TTL_MIN", "30"))
APPROVAL_WAIT_S = float(os.getenv("APPROVAL_WAIT_S", "75"))
NOTIFY_READS = tg.env_bool("TGC_NOTIFY_READS")
MAX_DRAFT_CHARS = 3500          # the approval card must show the whole text in one bot message
MAX_HISTORY = 1000
MAX_MEDIA_PER_CALL = 8
MAX_MEDIA_BYTES = int(float(os.getenv("MAX_MEDIA_MB", "20")) * 1e6)
MAX_DOC_CHARS = 60_000
IMAGE_MAX_PX = 1600
AUDIT_FILE = tg.DATA_DIR / "audit.jsonl"

# Files for send_message: uploaded with curl to <PUBLIC_URL>/upload/<ticket>/<name> (uploads.py).
MAX_UPLOAD_BYTES = int(float(os.getenv("MAX_UPLOAD_MB", "100")) * 1e6)
UPLOAD_STORE_BYTES = int(float(os.getenv("UPLOAD_STORE_MB", "1000")) * 1e6)
UPLOAD_TTL_S = 60 * float(os.getenv("UPLOAD_TTL_MIN", "120"))
UPLOAD_LINK_TTL_S = 60 * float(os.getenv("UPLOAD_LINK_TTL_MIN", "15"))
_public_host = next((h.strip() for h in os.getenv("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()), "")
PUBLIC_URL = (os.getenv("TGC_PUBLIC_URL", "").strip()
              or (f"https://{_public_host}{BASE_PATH}" if _public_host else f"http://localhost:{PORT}{BASE_PATH}")
              ).rstrip("/")
UPLOAD_PREFIX = f"{BASE_PATH}/upload/"
CAPTION_MAX = int(os.getenv("CAPTION_MAX", "1024"))  # Telegram's caption limit; 4096 with Premium
MAX_FILES_PER_SEND = 20                              # also bounds the file list on the approval card


class _Redact(logging.Filter):
    """Secrets ride in URLs (MCP path tokens, the Bot API path, upload tickets); keep them out of
    every log line. Tickets are minted at runtime, so they go by position: /upload/<ticket>/."""

    _TICKET = re.compile(r"(/upload/)[^/\s\"'?#]+")

    def __init__(self, secrets_: set[str]):
        super().__init__()
        self._s = sorted((s for s in secrets_ if s and len(s) >= 8), key=len, reverse=True)

    def filter(self, record):
        try:
            msg = record.getMessage()
            red = msg
            for s in self._s:
                red = red.replace(s, "***")
            if "/upload/" in red:
                red = self._TICKET.sub(r"\1***", red)
            if red != msg:
                record.msg, record.args = red, ()
        except Exception:
            pass
        return True


_redact = _Redact(TOKENS | {BOT_TOKEN, API_HASH})
for _h in logging.getLogger().handlers:
    _h.addFilter(_redact)


class _StreamErrorFilter(logging.Filter):
    def filter(self, record):
        return "ClosedResourceError" not in str(record.getMessage())


logging.getLogger("mcp.server.streamable_http").addFilter(_StreamErrorFilter())

# ── state ────────────────────────────────────────────────────────────────────
policy = tg.Policy.from_env()
sendlog = tg.SendLog()
store = uploads.Store(tg.DATA_DIR / "uploads", ttl_s=UPLOAD_TTL_S, link_ttl_s=UPLOAD_LINK_TTL_S,
                      max_file=MAX_UPLOAD_BYTES, max_total=UPLOAD_STORE_BYTES)
approvals = Approvals(BOT_TOKEN, OWNER_ID, state_file=tg.DATA_DIR / "state.json", ttl_s=APPROVAL_TTL_S,
                      approval_mode=SEND_APPROVAL)


async def _on_session_dead(reason: str) -> None:
    await approvals.notify(
        f"🔴 telegram-client: the Telegram session stopped working ({reason}) — revoked in Devices, "
        f"expired, or used by a second client. Log in again on the VPS:\n\n{tg.LOGIN_CMD}",
        key="dead", every_s=6 * 3600)


account = tg.Account(API_ID, API_HASH, OWNER_ID, on_dead=_on_session_dead)


def _audit(tool: str, peer: str, outcome: str) -> None:
    """Who-touched-what log, never message text. Shown by /status; trimmed to the last 5000 rows."""
    try:
        tg.DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(AUDIT_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": time.time(), "tool": tool, "peer": peer[:120],
                                 "outcome": outcome[:200]}, ensure_ascii=False) + "\n")
        if AUDIT_FILE.stat().st_size > 2_000_000:
            rows = AUDIT_FILE.read_text("utf-8").splitlines()[-5000:]
            tg.write_private(AUDIT_FILE, "\n".join(rows) + "\n")
    except Exception:
        logger.exception("audit write failed")


def _recent_audit(n: int = 8) -> list[str]:
    try:
        rows = AUDIT_FILE.read_text("utf-8").splitlines()[-n:]
    except OSError:
        return []
    out = []
    for line in rows:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        when = datetime.fromtimestamp(r["ts"], timezone.utc).astimezone().strftime("%m-%d %H:%M")
        out.append(f"{when} {r['tool']} {r['peer']} — {r['outcome']}")
    return out


async def _status_text() -> str:
    lines = ["telegram-client status"]
    try:
        await account.client()
        me = account.me
        lines.append(f"session: ✅ {tg.label(me)}")
    except tg.Fail as e:
        lines.append(f"session: ⚠️ {account.state} — {e}")
    except Exception as e:
        lines.append(f"session: ⚠️ unreachable — {type(e).__name__}: {e}")
    lines.append("lock: 🔒 LOCKED (/unlock)" if approvals.locked else "lock: 🔓 open")
    if SEND_APPROVAL:
        pend = [d for d in approvals.drafts.values() if d.status == "pending"]
        lines.append(f"sends: need your ✅ · pending drafts: {len(pend)}")
    else:
        lines.append("sends: direct, no approval (TGC_SEND_APPROVAL=false)")
    lines.append(sendlog.summary())
    lines.append(store.summary())
    recent = _recent_audit()
    if recent:
        lines += ["", "recent tool calls:"] + recent
    return "\n".join(lines)


approvals.status_fn = _status_text


# ── sending text and files (both modes) ──────────────────────────────────────
_HOW = {"photo": "photo (compressed)", "video": "video", "audio": "audio", "animation": "GIF", "document": "file"}


def _plan(recs: list[uploads.Upload], as_file: bool, album: bool) -> tg.FilePlan:
    return tg.plan_files([(r.name, r.size, tg.FileInfo(r.mime, r.kind, r.width, r.height)) for r in recs],
                         as_file=as_file, album=album)


def _file_rows(recs: list[uploads.Upload], plan: tg.FilePlan) -> list[dict]:
    """Each file as the card and the send log show it: how it will go out."""
    return [{"id": r.id, "name": r.name, "size": uploads.human(r.size),
             "how": _HOW[mode] + (f" — {note}" if note else "")}
            for r, mode, note in zip(recs, plan.modes, plan.notes)]


def _grouping(plan: tg.FilePlan, album: bool, text: str) -> str:
    """How the files go out and where the text goes; `text` is "caption", "first" or "" (none)."""
    n, k = len(plan.modes), len(plan.batches)
    if n == 1:
        shape = "1 file"
    elif k == 1:
        shape = f"album of {n}"
    elif album:
        shape = f"{k} albums ({' + '.join(str(len(b)) for b in plan.batches)})"
    else:
        shape = f"{n} separate messages"
    if text == "first":
        return f"{shape} · the text first, as its own message (over {CAPTION_MAX} characters for a caption)"
    if text == "caption":
        return f"{shape} · the text as {'its caption' if k == 1 else 'the caption of the first'}"
    return f"{shape}, no text"


def _delivered(first, batches: list[list]) -> str:
    """What went out: "#77", or "#76 (text), #77–#79" when files followed the text."""
    parts = [tg.sent_ids(b) for b in batches]
    if first is not None:
        parts.insert(0, f"#{first.id} (text)" if parts else f"#{first.id}")
    return ", ".join(parts)


def _when(first, batches: list[list]) -> str:
    for m in [first] + [m for b in batches for m in b]:
        if getattr(m, "date", None):
            return tg.ts(m.date)
    return tg.ts(datetime.now(timezone.utc))


async def _deliver(client, entity, body: str, recs: list[uploads.Upload], *, text_first: bool, as_file: bool,
                   album: bool, reply_to: int | None, plain: bool, silent: bool, no_preview: bool, is_new: bool):
    """One send_message: the text alone, the files with the text as their caption, or the text
    first as its own message and the files after it. Returns (text message or None, file batches)."""
    first = None
    if body and (text_first or not recs):
        first = await tg.send_text(client, entity, body, reply_to=reply_to, plain=plain, silent=silent,
                                   no_preview=no_preview, log=sendlog, is_new=is_new)
    if not recs:
        return first, []
    try:
        with store.in_use(recs):  # the sweep must not delete them mid-send
            batches = await tg.send_files(client, entity, [store.path(r) for r in recs],
                                          caption=body if body and not text_first else None, as_file=as_file,
                                          album=album, reply_to=reply_to, plain=plain, silent=silent,
                                          log=sendlog, is_new=is_new)
    except tg.Fail as e:
        if first is not None:
            raise tg.Fail(f"The text went out as #{first.id}; the files did not: {e}") from e
        raise
    return first, batches


async def _execute_draft(d: Draft) -> str:
    """Runs only after the owner's ✅. Re-checks lock, files and budget: all may have changed."""
    if approvals.locked:
        raise tg.Fail("locked")
    recs, missing = store.resolve(f["id"] for f in d.files)
    if missing:
        gone = ", ".join(f["name"] for f in d.files if f["id"] in missing)
        raise tg.Fail(f"no longer on the server (expired or deleted): {gone}. Nothing was sent.")
    n = len(_plan(recs, d.as_file, d.album).batches) + d.text_first if recs else 1
    try:
        client = await account.client()
        sendlog.check(n, d.peer_id, d.is_new)
        first, batches = await _deliver(client, d.entity, d.text, recs, text_first=d.text_first,
                                        as_file=d.as_file, album=d.album, reply_to=d.reply_to, plain=d.plain,
                                        silent=d.silent, no_preview=d.no_preview, is_new=d.is_new)
    except tg.UNAUTHORIZED as e:
        await account.mark_dead(type(e).__name__)
        raise tg.Fail("the Telegram session was revoked")
    sent = _delivered(first, batches)
    _audit("send (approved)", d.peer_label, f"sent {sent}")
    return f"{sent} at {_when(first, batches)}"


approvals.executor = _execute_draft

# ── MCP ──────────────────────────────────────────────────────────────────────
_READING = """\
This server acts as the owner's OWN Telegram account (a user session, not a bot).

Reading: list_dialogs -> find_peer -> read_history / search_chat -> get_media. Reading is
silent (no read receipts). Transcript lines are `#<id> [date] <who>: <text>`; `me` is the
owner, `↩#id` a reply, `[fwd from …]` a forward; media appear as tags like `[photo]` or
`[file brief.pdf 2.1MB]`. Look at photos and screenshots with get_media(peer, [ids]) — that
is often where the real requirements are — and read text/PDF documents the same way.
Voice notes are not transcribed: say so. Telegram's service chat, this server's control
bot and (by default) Saved Messages are never accessible.
"""

_SENDING_APPROVAL = """
Sending: send_message never sends by itself. It creates a draft and the owner gets a card
in Telegram with ✅ / ❌; only his tap sends it.
1. Draft exactly the text you mean — the owner approves this text, not an intention.
2. One draft = one message. Never resubmit a pending draft; check send_status(draft_id).
   If the owner declined, do not resend — ask what to change.
3. Write only to people the task is about. A first message to someone with no prior chat
   needs allow_new=true AND an explicit request from the owner to contact that person.
   Budgets (per hour, per day, new contacts per day) are enforced by the server.
4. Match how the owner writes in that chat (language, tone, length). Short and human,
   plain text for DMs; Telethon markdown (**bold**, __italic__, `code`) unless plain=true.
"""

_SENDING_DIRECT = """
Sending: send_message delivers the message immediately, from the owner's account and in
his name. The owner has authorized you to send on his behalf: no confirmation step exists,
so do not wait for one or ask the server for one. A sent message is final.
1. Send only what the task calls for, to the people it is about.
2. One call = one message, delivered on success. Never repeat a call that succeeded; if a
   call fails, read the error before deciding to try again — never retry in a loop.
3. A first message to someone with no prior chat needs allow_new=true AND an explicit
   request from the owner to contact that person. Budgets (per hour, per day, new contacts
   per day) are enforced by the server.
4. Match how the owner writes in that chat (language, tone, length). Short and human,
   plain text for DMs; Telethon markdown (**bold**, __italic__, `code`) unless plain=true.
"""

_FILES = """
Files (charts, PDFs, photos, audio, anything you made): upload_link() gives a URL; upload each
file from your shell with the curl line it shows, which prints one JSON line per file (id, size,
sha256, kind); then send_message(peer, text, files=[ids]). An upload id is not a message:
nothing is sent until send_message. By default photos go compressed and several files go as one
album; as_file=true sends the original files at full resolution, album=false one message per
file. When integrity matters, compare sha256 with `sha256sum`.
"""

_TROUBLE = """
If a tool says the server is locked, stop: the owner locked it from Telegram on purpose.
If it says not logged in or the session was revoked, tell the owner; never retry in a loop.
"""

INSTRUCTIONS = _READING + (_SENDING_APPROVAL if SEND_APPROVAL else _SENDING_DIRECT) + _FILES + _TROUBLE

_allowed_hosts = ["localhost", "127.0.0.1", "0.0.0.0", _safe_name, f"mcp-{_safe_name}"]
_allowed_hosts += [f"{h}:*" for h in list(_allowed_hosts)]
_allowed_origins = ["http://localhost", "http://127.0.0.1"]
for _h in [x.strip() for x in os.getenv("MCP_ALLOWED_HOSTS", "").split(",") if x.strip()]:
    _allowed_hosts += [_h, f"{_h}:*"]
    _allowed_origins.append(f"https://{_h}")

mcp = FastMCP(
    _safe_name,
    instructions=INSTRUCTIONS,
    streamable_http_path=STREAM_PATH,
    json_response=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=_allowed_hosts,
        allowed_origins=_allowed_origins,
    ),
)


async def _guarded(tool: str, peer: str, fn, *, read: bool = True, audit_ok: bool = True):
    """Lock check, session, error mapping and audit around every Telegram-touching tool."""
    if approvals.locked:
        _audit(tool, peer, "refused: locked")
        raise ToolError(LOCKED_MSG)
    try:
        client = await account.client()
        out = await fn(client)
    except tg.NotLoggedIn as e:
        _audit(tool, peer, f"refused: {account.state}")
        await approvals.notify(f"⚪ An agent called {tool}, but telegram-client is not logged in. "
                               f"On the VPS:\n\n{tg.LOGIN_CMD}", key="login", every_s=6 * 3600)
        raise ToolError(str(e))
    except tg.Fail as e:
        _audit(tool, peer, f"refused: {e}")
        raise ToolError(str(e))
    except tg.UNAUTHORIZED as e:
        await account.mark_dead(type(e).__name__)
        _audit(tool, peer, f"session dead: {type(e).__name__}")
        raise ToolError("The Telegram session was revoked or expired. The owner has been alerted; stop.")
    except tg.errors.FloodWaitError as e:
        _audit(tool, peer, f"flood wait {e.seconds}s")
        raise ToolError(f"Telegram flood wait {e.seconds}s — try much later; do not loop.")
    if audit_ok:
        _audit(tool, peer, "ok")
    if read and NOTIFY_READS:
        approvals.notify_bg(f"👁 {tool}: {peer}")
    return out


@mcp.tool()
async def whoami() -> str:
    """Which Telegram account this server acts as, whether its session is live, whether the
    owner has locked the server, how sending works, and the send budget left. Call it first
    when anything fails."""
    lines = []
    try:
        if approvals.locked:
            lines.append(LOCKED_MSG)
        else:
            await account.client()
            lines.append(f"Acting as {tg.label(account.me)}.")
    except tg.Fail as e:
        lines.append(f"Session: {account.state} — {e}")
    lines.append(sendlog.summary())
    if SEND_APPROVAL:
        lines.append("Sending: each message waits for the owner's ✅ in Telegram.")
        pend = [d.id for d in approvals.drafts.values() if d.status == "pending"]
        if pend:
            lines.append(f"Drafts waiting for the owner: {', '.join(pend)}")
    else:
        lines.append("Sending: send_message delivers immediately; no confirmation step.")
    return "\n".join(lines)


@mcp.tool()
async def list_dialogs(limit: int = 30, unread_only: bool = False, kind: str = "") -> str:
    """Recent chats, newest first: id, kind, unread count (`!` = unread mention), last message.

    Args:
        limit: how many chats to return (max 200).
        unread_only: only chats with unread messages — "who is waiting for an answer".
        kind: "user", "bot", "group" or "channel"; empty for all.
    """
    limit = max(1, min(int(limit or 30), 200))
    kind = (kind or "").strip().lower()

    async def run(client):
        rows = []
        async for d in client.iter_dialogs(limit=tg.DIALOG_SCAN):
            ent = d.entity
            if not policy.visible(ent):
                continue
            k = tg.kind_of(ent)
            if kind and k != kind:
                continue
            if unread_only and not (d.unread_count or d.unread_mentions_count):
                continue
            rows.append((d, ent, k))
            if len(rows) >= limit:
                break
        if not rows:
            return "(nothing matched)"
        out = [f"{'id':>15}  {'kind':<8}{'unread':>7}  {'last':<16}  name — last message"]
        for d, e, k in rows:
            unread = (str(d.unread_count) if d.unread_count else "") + ("!" if d.unread_mentions_count else "")
            u = tg.username_of(e)
            who = "me: " if d.message and d.message.out else ""
            out.append(f"{tg.peer_id(e):>15}  {k:<8}{unread:>7}  {tg.ts(d.date) if d.date else '':<16}  "
                       f"{tg.name_of(e)}{' @' + u if u else ''} — {who}{tg.snippet(d.message)}")
        return "\n".join(out)

    return await _guarded("list_dialogs", f"{kind or 'all'}{' unread' if unread_only else ''}", run)


@mcp.tool()
async def find_peer(query: str) -> str:
    """Resolve a name, @username or phone to peers (your dialogs, contacts, global @username).
    Returns ids to use as `peer` elsewhere, and whether a chat with them already exists."""
    q = (query or "").strip()
    if not q:
        raise ToolError("query is empty")

    async def run(client):
        qf = q.lstrip("@").casefold()
        found: dict[int, tuple] = {}
        async for d in client.iter_dialogs(limit=tg.DIALOG_SCAN):
            name = (d.name or "").casefold()
            u = (tg.username_of(d.entity) or "").casefold()
            if qf and (qf in name or (u and qf in u)):
                found[d.entity.id] = (d.entity, d.date)
        for ent in await tg.contacts_search(client, q.lstrip("@")):
            found.setdefault(ent.id, (ent, None))
        if q.startswith("@") or "t.me/" in q or tg._PHONE.fullmatch(q):
            with contextlib.suppress(Exception):
                ent = await client.get_entity(q)
                found.setdefault(ent.id, (ent, None))
        rows = [(e, dt) for e, dt in found.values() if policy.visible(e)]
        if not rows:
            return f"Nothing matches '{q}'."
        rows.sort(key=lambda t: (t[1] is None, -(t[1].timestamp() if t[1] else 0)))
        return "\n".join(f"{tg.peer_id(e):>15}  {tg.kind_of(e):<8}  "
                         f"{('dialog ' + tg.ts(dt)) if dt else 'not a dialog yet':<23}  {tg.label(e)}"
                         for e, dt in rows)

    return await _guarded("find_peer", q, run)


@mcp.tool()
async def read_history(peer: str, since: str = "", after_id: int = 0, limit: int = 0) -> str:
    """A chat as a transcript, oldest first: `#<id> [YYYY-MM-DD HH:MM] <who>: <text>`.

    Args:
        peer: @username, t.me link, phone, numeric id from list_dialogs/find_peer, or a unique
            part of the dialog name.
        since: read forward from 30m / 24h / 7d / 2w ago, or from an ISO date.
        after_id: only messages newer than this id — "what is new since #4210".
        limit: max messages (default 200, or 500 with since/after_id; hard cap 1000).
    Media show as tags; pass their ids to get_media to see photos or read documents.
    """
    since_dt = tg.parse_since(since) if since else None
    lim = int(limit or (500 if (since_dt or after_id) else 200))
    lim = max(1, min(lim, MAX_HISTORY))

    async def run(client):
        entity = await tg.resolve_peer(client, peer, policy)
        me = account.me or await client.get_me()
        msgs = await tg.collect(client, entity, since=since_dt, after_id=after_id or None, limit=lim)
        lines, n_media = await tg.render(entity, msgs)
        extra = []
        if n_media:
            extra.append(f"{n_media} messages with photos/files — get_media(peer, [ids]) to view")
        if len(msgs) >= lim:
            extra.append(f"limit {lim} reached — older messages exist; raise limit or use since")
        return tg.header(entity, me, msgs, extra) + "\n" + "\n".join(lines)

    return await _guarded("read_history", peer, run)


@mcp.tool()
async def search_chat(peer: str, query: str, limit: int = 50) -> str:
    """Server-side text search inside one chat; matching messages as transcript lines."""
    if not (query or "").strip():
        raise ToolError("query is empty")
    lim = max(1, min(int(limit or 50), 200))

    async def run(client):
        entity = await tg.resolve_peer(client, peer, policy)
        me = account.me or await client.get_me()
        msgs = await tg.collect(client, entity, search=query, limit=lim)
        lines, _ = await tg.render(entity, msgs)
        return tg.header(entity, me, msgs, [f"search '{query}'"]) + "\n" + "\n".join(lines)

    return await _guarded("search_chat", peer, run)


_TEXT_EXT = {".txt", ".md", ".csv", ".json", ".py", ".js", ".ts", ".yaml", ".yml", ".xml", ".html",
             ".log", ".ini", ".toml", ".sql", ".sh"}


def _to_jpeg(data: bytes) -> tuple[bytes, int, int]:
    from PIL import Image, ImageOps

    im = Image.open(io.BytesIO(data))
    im = ImageOps.exif_transpose(im)
    if im.mode not in ("RGB", "L"):
        im = im.convert("RGB")
    im.thumbnail((IMAGE_MAX_PX, IMAGE_MAX_PX))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=82, optimize=True)
    return buf.getvalue(), im.width, im.height


def _pdf_text(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    parts = []
    for i, page in enumerate(reader.pages[:40]):
        parts.append(f"--- page {i + 1} ---\n{(page.extract_text() or '').strip()}")
        if sum(len(p) for p in parts) > MAX_DOC_CHARS:
            break
    return "\n".join(parts)[:MAX_DOC_CHARS]


@mcp.tool()
async def get_media(peer: str, message_ids: list[int]) -> list:
    """Look at the media of specific messages (ids from read_history): photos and image files
    come back as images; text files and PDFs as their text. Up to 8 messages per call.
    Voice, audio, video and stickers are described only — they are not transcribed."""
    ids = [int(i) for i in (message_ids or [])][:MAX_MEDIA_PER_CALL]
    if not ids:
        raise ToolError("message_ids is empty")

    async def run(client):
        entity = await tg.resolve_peer(client, peer, policy)
        msgs = await client.get_messages(entity, ids=ids)
        notes, blocks = [], []
        for mid, m in zip(ids, msgs):
            if m is None:
                notes.append(f"#{mid}: no such message")
                continue
            info = tg.media_info(m)
            if not info:
                notes.append(f"#{mid}: no media")
                continue
            tag, kind = tg.media_tag(info), info["kind"]
            mime = (info.get("mime") or "").lower()
            name = (info.get("name") or "").lower()
            size = info.get("size") or 0
            if size > MAX_MEDIA_BYTES:
                notes.append(f"#{mid}: {tag} — over {MAX_MEDIA_BYTES // 1_000_000} MB, not fetched")
                continue
            is_image = kind == "photo" or (kind == "document" and mime.startswith("image/"))
            is_pdf = kind == "document" and (mime == "application/pdf" or name.endswith(".pdf"))
            is_text = kind == "document" and (mime.startswith("text/") or Path(name).suffix in _TEXT_EXT)
            if not (is_image or is_pdf or is_text):
                notes.append(f"#{mid}: {tag} — not viewable here")
                continue
            data = await client.download_media(m, file=bytes)
            if not data:
                notes.append(f"#{mid}: {tag} — download returned nothing")
                continue
            try:
                if is_image:
                    jpeg, w, h = await asyncio.to_thread(_to_jpeg, data)
                    blocks.append(MCPImage(data=jpeg, format="jpeg").to_image_content())
                    notes.append(f"#{mid}: {tag} {w}x{h} → image {sum(1 for b in blocks if b.type == 'image')} below")
                elif is_pdf:
                    text = await asyncio.to_thread(_pdf_text, data)
                    blocks.append(TextContent(type="text", text=f"#{mid} {tag}\n{text or '(no extractable text — a scan?)'}"))
                    notes.append(f"#{mid}: {tag} → text below")
                else:
                    text = data[: MAX_DOC_CHARS * 4].decode("utf-8", errors="replace")[:MAX_DOC_CHARS]
                    blocks.append(TextContent(type="text", text=f"#{mid} {tag}\n{text}"))
                    notes.append(f"#{mid}: {tag} → text below")
            except Exception as e:
                notes.append(f"#{mid}: {tag} — could not decode ({type(e).__name__})")
        return [TextContent(type="text", text="\n".join(notes))] + blocks

    return await _guarded("get_media", f"{peer} {ids}", run)


@mcp.tool()
async def upload_link() -> str:
    """A short-lived URL for uploading files from your shell with curl, to attach them with
    send_message(files=[...]). Returns the exact curl command, the limits and the expiry.
    Uploading sends nothing to anyone."""
    if approvals.locked:
        _audit("upload_link", "-", "refused: locked")
        raise ToolError(LOCKED_MSG)
    t = store.new_ticket()
    url = f"{PUBLIC_URL}/upload/{t.token}/"
    _audit("upload_link", "-", "ok")
    return (
        f"Upload URL, valid until {uploads.iso(t.expires)} for up to {uploads.FILES_PER_TICKET} files "
        f"of at most {uploads.human(MAX_UPLOAD_BYTES)} each:\n{url}\n\n"
        "Upload from your shell. Keep the trailing '/': curl then appends the file's own name.\n"
        f"  curl -sS -T /path/to/chart.png {url}\n"
        f"  curl -sS -T \"{{/path/a.png,/path/b.pdf}}\" {url}   (several files)\n"
        "Each upload prints one JSON line: id, name, size, sha256, kind (photo, video, audio, gif or "
        "document).\nThen pass the ids: send_message(peer, text, files=[\"f_…\"]). An upload is not a "
        "message: nothing reaches anyone until send_message.\n"
        f"Uploads are kept {UPLOAD_TTL_S / 60:.0f} minutes; list_uploads() shows them. When integrity "
        "matters, compare sha256 with `sha256sum <file>`.\n"
        f"If curl reports a 403 from a proxy or a failed CONNECT, {urlsplit(PUBLIC_URL).hostname} is "
        "missing from your sandbox's network allow-list: tell the owner, and do not retry in a loop.")


@mcp.tool()
async def list_uploads() -> str:
    """Uploaded files that have not expired: id, kind, size, expiry, name, sha256. For when the
    ids printed by the uploads are lost."""
    if approvals.locked:
        _audit("list_uploads", "-", "refused: locked")
        raise ToolError(LOCKED_MSG)
    rows = store.listing()
    _audit("list_uploads", "-", f"{len(rows)} files")
    if not rows:
        return "No uploads. upload_link() gives a URL to upload files from your shell."
    return "\n".join(f"{r.id}  {r.kind:<8} {uploads.human(r.size):>9}  until {uploads.iso(r.expires)}  "
                     f"{r.name}  sha256 {r.sha256}" for r in rows)


def _describe(d: Draft) -> str:
    if d.status == "sent":
        return f"✅ Sent {d.result} to {d.peer_label} — approved by the owner (draft {d.id})."
    if d.status == "pending":
        return (f"⏳ Draft {d.id} waits for the owner's ✅ in Telegram (expires "
                f"{datetime.fromtimestamp(d.expires, timezone.utc).astimezone():%H:%M}). Nothing is sent yet. "
                f"Do not resubmit; check send_status('{d.id}') later.")
    if d.status == "sending":
        return f"⏳ The owner approved draft {d.id}; it is being sent. Check send_status('{d.id}')."
    if d.status == "cancelled":
        return f"❌ The owner declined draft {d.id} ({d.result}). Do not resend it; ask what to change."
    if d.status == "withdrawn":
        return f"🗑 Draft {d.id} was withdrawn before approval; nothing was sent."
    if d.status == "expired":
        return f"⌛ Draft {d.id} expired without approval; nothing was sent."
    return f"⚠️ Draft {d.id} was approved but sending failed: {d.result}"


_SEND_ARGS = f"""
    Args:
        peer: as in read_history.
        text: the exact message (max 3500 chars). Telethon markdown unless plain=true. May be empty
            when files are given. With files it becomes their caption if its visible text fits
            {CAPTION_MAX} characters; a longer text goes first, as its own message.
        files: up to {MAX_FILES_PER_SEND} upload ids (from upload_link + curl, or list_uploads).
        as_file: true sends every file as a document: original bytes, full resolution. Default
            false: photos are compressed, videos, audio and GIFs play inline, the rest are documents.
        album: true (default) groups the files into one message, up to 10 per album: photos with
            videos, audio with audio, documents with documents, and a GIF always alone. false sends
            one message per file.
        reply_to: message id to reply to.
        plain: send as literal text (no markdown parsing).
        silent: no notification sound for the recipient.
        no_preview: no link preview.
        allow_new: allow a FIRST message to someone with no prior chat — only when the owner
            explicitly asked to contact that person.
"""
SEND_DESC_APPROVAL = (
    "Ask the owner to send `text` and any uploaded `files` to `peer` from his account. Creates a\n"
    "draft; the owner gets a card in Telegram with the exact recipient, text and files and ✅ / ❌.\n"
    "Only his tap sends it. Waits up to ~75 s for the decision, then returns the draft id — poll\n"
    "send_status, never resubmit.\n"
    + _SEND_ARGS)
SEND_DESC_DIRECT = (
    "Send `text` and any uploaded `files` to `peer` from the owner's account, immediately, in his\n"
    "name. On success the message is already delivered (the result gives its id) and is final;\n"
    "nothing else is needed.\n"
    + _SEND_ARGS)


async def send_message(peer: str, text: str = "", files: list[str] = [], as_file: bool = False,
                       album: bool = True, reply_to: int = 0, plain: bool = False, silent: bool = False,
                       no_preview: bool = False, allow_new: bool = False) -> str:
    body = (text or "").strip("\n")
    if not body.strip():
        body = ""
    ids = list(dict.fromkeys(str(f).strip() for f in files or () if str(f).strip()))
    if not body and not ids:
        raise ToolError("empty message: give text, files, or both")
    if len(ids) > MAX_FILES_PER_SEND:
        raise ToolError(f"{len(ids)} files; at most {MAX_FILES_PER_SEND} go in one call. Split them over "
                        "several calls.")
    recs, missing = store.resolve(ids)
    if missing:
        raise ToolError(f"Unknown or expired upload ids: {', '.join(missing)}. Nothing was sent. "
                        "list_uploads() shows the files still here; upload the others again.")
    rows, grouping, text_first, n_msgs = [], "", False, 1
    if recs:
        try:
            plan = _plan(recs, as_file, album)
        except tg.Fail as e:
            raise ToolError(str(e))
        text_first = bool(body) and tg.caption_len(body, plain) > CAPTION_MAX
        rows = _file_rows(recs, plan)
        grouping = _grouping(plan, album, "first" if text_first else "caption" if body else "")
        n_msgs = len(plan.batches) + text_first
    limit = MAX_DRAFT_CHARS - (tg.ulen(files_block(rows, grouping)) if SEND_APPROVAL and rows else 0)
    if tg.ulen(body) > limit:
        why = "one message" if limit == MAX_DRAFT_CHARS else "the owner's card lists the files too"
        raise ToolError(f"text is {tg.ulen(body)} chars; the limit is {limit} ({why}). "
                        "Shorten it, or split it into separate messages.")

    async def prepare(client):
        entity = await tg.resolve_peer(client, peer, policy)
        prior = await client.get_messages(entity, limit=1)
        is_new = len(prior) == 0 and tg.kind_of(entity) in ("user", "bot")
        if is_new and not allow_new:
            raise tg.Fail(f"No prior conversation with {tg.label(entity)}. A first message to a stranger "
                          "needs allow_new=true, and only when the owner explicitly asked to contact them.")
        sendlog.check(n_msgs, tg.peer_id(entity), is_new)
        return entity, is_new

    if not SEND_APPROVAL:
        async def run_direct(client):
            entity, is_new = await prepare(client)
            first, batches = await _deliver(client, entity, body, recs, text_first=text_first, as_file=as_file,
                                            album=album, reply_to=reply_to or None, plain=plain, silent=silent,
                                            no_preview=no_preview, is_new=is_new)
            return entity, first, batches

        entity, first, batches = await _guarded("send_message", peer, run_direct, read=False, audit_ok=False)
        lbl, sent = tg.label(entity), _delivered(first, batches)
        _audit("send_message", lbl, f"sent {sent}")
        if NOTIFY_SENDS:
            line = f"📤 Sent as you → {lbl} · {sent}"
            if body:
                line += f"\n{RULE}\n{body}"
            if rows:
                line += f"\n{RULE}\n{files_block(rows, grouping)}"
            approvals.notify_bg(line, silent=True)
        return f"✅ Sent {sent} to {lbl} at {_when(first, batches)}" + (f" ({grouping})." if rows else ".")

    async def run(client):
        entity, is_new = await prepare(client)
        d = Draft(peer_label=tg.label(entity), peer_id=tg.peer_id(entity), text=body,
                  reply_to=reply_to or None, plain=plain, silent=silent, no_preview=no_preview,
                  is_new=is_new, entity=entity, files=rows, as_file=as_file, album=album,
                  text_first=text_first, grouping=grouping)
        if recs:  # the files outlive the draft, with room left for a slow upload after the tap
            store.extend(recs, time.time() + APPROVAL_TTL_S + 600)
        await approvals.submit(d)
        return d

    d = await _guarded("send_message", peer, run, read=False, audit_ok=False)
    _audit("send_message", d.peer_label,
           f"draft {d.id} awaiting approval" + (f" ({len(rows)} files)" if rows else ""))
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(d.done.wait(), timeout=APPROVAL_WAIT_S)
    return _describe(d)


mcp.tool(description=SEND_DESC_APPROVAL if SEND_APPROVAL else SEND_DESC_DIRECT)(send_message)


async def send_status(draft_id: str, wait_seconds: int = 30) -> str:
    """State of a draft from send_message: waiting, sent, declined, withdrawn, expired or failed.
    While it is still waiting, blocks up to `wait_seconds` (max 75) for the owner's decision."""
    d = approvals.drafts.get((draft_id or "").strip())
    if d is None:
        return (f"No draft '{draft_id}' — it finished more than a day ago or the server restarted "
                "(pending drafts do not survive a restart). Nothing is waiting under that id.")
    if d.status in ("pending", "sending") and wait_seconds:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(d.done.wait(), timeout=max(0, min(int(wait_seconds), 75)))
    return _describe(d)


async def cancel_send(draft_id: str) -> str:
    """Withdraw your own draft before the owner decides (e.g. the text turned out wrong)."""
    d = approvals.drafts.get((draft_id or "").strip())
    if d is None:
        return f"No draft '{draft_id}'."
    await approvals.withdraw(d)
    _audit("cancel_send", d.peer_label, f"draft {d.id}: {d.status}")
    return _describe(d)


if SEND_APPROVAL:  # draft tools exist only where drafts do
    mcp.tool()(send_status)
    mcp.tool()(cancel_send)


# ── app ──────────────────────────────────────────────────────────────────────
async def health(_request):
    return JSONResponse({"status": "healthy", "service": _safe_name, "session": account.state,
                         "locked": approvals.locked})


async def _watch_session() -> None:
    """Announce startup, alert on a missing/dead session, notice a fresh login within a minute."""
    first, last_full = True, 0.0
    while True:
        try:
            was = account.state
            if tg.SESSION_FILE.exists():
                client = await account.client()
                if time.time() - last_full > 900:
                    await client.get_me()  # a revoked key fails here, not at the next tool call
                    last_full = time.time()
                if first or was != "authorized":
                    how = ("Sends wait for your ✅ here." if SEND_APPROVAL else
                           "Sends go out directly; each is logged here silently. /lock stops everything.")
                    await approvals.notify(f"🟢 telegram-client is live as {tg.label(account.me)}. "
                                           f"{how} /help for commands.")
            elif first or was == "authorized":
                await approvals.notify(f"⚪ telegram-client is up but not logged in. On the VPS:\n\n{tg.LOGIN_CMD}",
                                       key="login", every_s=12 * 3600)
        except tg.NotLoggedIn:
            pass  # already alerted by the dead-session path
        except tg.UNAUTHORIZED as e:
            await account.mark_dead(type(e).__name__)
        except Exception as e:
            logger.warning("session watch: %s: %s", type(e).__name__, e)
        first = False
        await asyncio.sleep(60)


async def _sweep_uploads() -> None:
    while True:
        await asyncio.sleep(60)
        try:
            store.sweep()
        except Exception:
            logger.exception("upload sweep failed")


@contextlib.asynccontextmanager
async def lifespan(_app):
    tg.DATA_DIR.mkdir(parents=True, exist_ok=True)
    store.load()
    await approvals.start()  # a bad bot token stops the server here, on purpose
    policy.blocked_ids.add(approvals.bot_id)
    tasks = [asyncio.create_task(_watch_session(), name="session-watch"),
             asyncio.create_task(_sweep_uploads(), name="upload-sweep")]
    try:
        async with mcp.session_manager.run():
            yield
    finally:
        for t in tasks:
            t.cancel()
            with contextlib.suppress(BaseException):
                await t
        await approvals.stop()
        await account.close()


mcp_asgi = mcp.streamable_http_app()
routes = [
    Route("/health", health, methods=["GET"]),
    Route(f"{BASE_PATH}/health", health, methods=["GET"]),
    # No MCP token here (see TokenAuthMiddleware): the ticket in the path is the credential, and
    # the endpoint answers every method under the prefix itself.
    Route(UPLOAD_PREFIX + uploads.REST, uploads.Endpoint(store, locked=lambda: approvals.locked)),
    Mount("/", app=mcp_asgi),  # LAST: it matches every path
]
app = Starlette(routes=routes, lifespan=lifespan)


def _token_ok(candidate: str | None) -> bool:
    return bool(candidate) and any(hmac.compare_digest(candidate, t) for t in TOKENS)


class TokenAuthMiddleware(BaseHTTPMiddleware):
    """Bearer header, ?token= or /telegram-client/<token>/ (a claude.ai/Duet connector can only
    carry the secret in the URL). Only /health is public, and /telegram-client/upload/..., where
    the upload ticket in the path is the credential. There is no "auth off" mode."""

    async def dispatch(self, request, call_next):
        path = request.url.path or "/"
        if path in ("/health", f"{BASE_PATH}/health") or not path.startswith(BASE_PATH):
            return await call_next(request)
        if request.scope.get("path", "").startswith(UPLOAD_PREFIX):  # the path the router matches
            return await call_next(request)
        auth = request.headers.get("authorization") or ""
        if auth.lower().startswith("bearer ") and _token_ok(auth.split(" ", 1)[1].strip()):
            return await call_next(request)
        if _token_ok(request.query_params.get("token")):
            return await call_next(request)
        segs = [s for s in path.split("/") if s]
        if len(segs) >= 2 and segs[0] == _safe_name and _token_ok(segs[1]):
            rest = "/".join([_safe_name] + segs[2:])
            new_path = "/" + rest + ("/" if path.endswith("/") or len(segs) == 2 else "")
            request.scope["path"] = new_path
            request.scope["raw_path"] = new_path.encode()
            return await call_next(request)
        return JSONResponse({"detail": "Unauthorized"}, status_code=401, headers={"WWW-Authenticate": "Bearer"})


app.add_middleware(TokenAuthMiddleware)


def main():
    logger.info("Starting %s MCP on :%s%s", MCP_NAME, PORT, STREAM_PATH)
    uvicorn.run(app=app, host=os.getenv("HOST", "0.0.0.0"), port=PORT,
                log_level=os.getenv("LOG_LEVEL", "info").lower(), access_log=True,
                log_config=None,  # access lines reach the root handler, and so the redaction filter
                proxy_headers=True, forwarded_allow_ips="*", timeout_keep_alive=660)


if __name__ == "__main__":
    main()
