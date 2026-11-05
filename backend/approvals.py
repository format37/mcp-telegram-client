"""Owner approval and remote control over a dedicated Telegram bot (Bot API, long polling).

Every send an agent asks for becomes a *draft*. The bot shows the owner the
exact recipient and text with ✅ / ❌ buttons, and only a tap from TGC_OWNER_ID
executes it. The same bot answers /status, /lock, /unlock and /pending, and
carries the alerts (session revoked, not logged in, server started).

Why an agent cannot approve its own draft: the MCP exposes no tool that can
press a button, and the bot's chat is blocked for reading and writing (see
tg.Policy), so the user session never touches this bot. The bot ignores
everyone but the owner and does not answer strangers at all.

This bot must have no other consumer: Telegram delivers each update to exactly
one getUpdates poller, so a second one would silently steal the taps.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

from tg import Fail, write_private

logger = logging.getLogger(__name__)

BOT_API = "https://api.telegram.org"
LOCKED_MSG = ("🔒 The owner has locked telegram-client from Telegram. Every tool refuses until he "
              "unlocks it. Stop and tell the user; do not retry.")
RULE = "━━━━━━━━━━━━"


def help_text(approval_mode: bool) -> str:
    sends = ("Drafts arrive here with ✅ Send / ❌ Cancel. Nothing is sent without your tap.\n\n"
             "/pending — drafts waiting for you\n" if approval_mode else
             "Sends go out directly (TGC_SEND_APPROVAL=false); each one is logged here silently.\n\n")
    return ("telegram-client control bot\n\n" + sends +
            "/status  — session, lock, sending mode, budget, recent tool calls\n"
            "/lock    — every MCP tool refuses, reads and sends" +
            ("; pending drafts are cancelled\n" if approval_mode else "\n") +
            "/unlock  — resume\n\n"
            "To kill the session itself: Telegram → Settings → Devices → terminate it.")


class BotError(Exception):
    pass


@dataclass
class Draft:
    peer_label: str
    peer_id: int
    text: str
    reply_to: int | None = None
    plain: bool = False
    silent: bool = False
    no_preview: bool = False
    is_new: bool = False
    entity: Any = field(default=None, repr=False)
    # Attachments, resolved at submit; the executor looks the files up again by id.
    files: list[dict] = field(default_factory=list)  # [{id, name, size (text), how}] as the card lists them
    as_file: bool = False
    album: bool = True
    text_first: bool = False  # the text goes out as its own message before the files
    grouping: str = ""        # the card's line under the files, e.g. "album of 3 · the text as its caption"
    id: str = field(default_factory=lambda: secrets.token_hex(4))
    created: float = field(default_factory=time.time)
    expires: float = 0.0
    status: str = "pending"  # pending | sending | sent | cancelled | withdrawn | expired | failed
    result: str = ""
    card_id: int | None = None
    done: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    def finish(self, status: str, result: str = "") -> None:
        self.status, self.result = status, result
        self.done.set()


def _hhmm(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).astimezone().strftime("%H:%M")


def short_name(name: str, n: int = 64) -> str:
    """A file name cut to fit a card line, extension kept (the files themselves carry it whole)."""
    if len(name) <= n:
        return name
    stem, ext = os.path.splitext(name)
    if 0 < len(ext) <= 12:
        return stem[: n - len(ext) - 1] + "…" + ext
    return name[: n - 1] + "…"


def files_block(files: list[dict], grouping: str) -> str:
    """One 📎 line per file, then how they go out."""
    lines = [f"📎 {short_name(f['name'])} · {f['size']} · {f['how']}" for f in files]
    return "\n".join(lines + ([grouping] if grouping else []))


def card_text(d: Draft) -> str:
    flags = []
    if d.reply_to:
        flags.append(f"reply to #{d.reply_to}")
    flags.append("plain text" if d.plain else "markdown")
    if d.silent:
        flags.append("silent")
    if d.no_preview:
        flags.append("no link preview")
    lines = [f"✉️ Send as you → {d.peer_label}", " · ".join(flags)]
    if d.is_new:
        lines.append("⚠️ First message to this person — there is no prior chat.")
    lines.append(RULE)
    if d.text or not d.files:
        lines += [d.text, RULE]
    if d.files:
        lines.append(files_block(d.files, d.grouping))
    lines.append(f"Draft {d.id} · expires {_hhmm(d.expires)}")
    return "\n".join(lines)


_OUTCOME = {
    "sending": "⏳ Approved — sending…",
    "sent": "✅ Sent",
    "cancelled": "❌ Cancelled by you",
    "withdrawn": "🗑 Withdrawn by the agent",
    "expired": "⌛ Expired — not sent",
    "failed": "⚠️ Approved, but sending failed",
}


class Approvals:
    def __init__(self, token: str, owner_id: int, *, state_file: Path, ttl_s: float = 1800,
                 max_pending: int = 5, max_per_hour: int = 30, http: Any = None,
                 approval_mode: bool = True):
        self.approval_mode = approval_mode
        self.token = token
        self.owner_id = owner_id
        self.ttl_s = ttl_s
        self.max_pending = max_pending
        self.max_per_hour = max_per_hour
        self.state_file = state_file
        self.http = http or httpx.AsyncClient(timeout=httpx.Timeout(70.0, connect=15.0))
        self.drafts: dict[str, Draft] = {}
        self.executor: Callable[[Draft], Awaitable[str]] | None = None  # performs an approved send
        self.status_fn: Callable[[], Awaitable[str]] | None = None      # text for /status
        self.bot_id: int | None = None
        self.bot_username: str | None = None
        self.locked = self._load_locked()
        self._offset = 0
        self._submits: list[float] = []
        self._alerts: dict[str, float] = {}
        self._tasks: list[asyncio.Task] = []

    # ── bot api ──────────────────────────────────────────────────────────────
    async def api(self, method: str, **params) -> Any:
        r = await self.http.post(f"{BOT_API}/bot{self.token}/{method}", json=params)
        try:
            data = r.json()
        except ValueError:
            raise BotError(f"{method}: HTTP {r.status_code}, not JSON")
        if not data.get("ok"):
            raise BotError(f"{method}: {data.get('description') or 'failed'}")
        return data["result"]

    async def start(self) -> None:
        me = await self.api("getMe")
        self.bot_id, self.bot_username = me["id"], me.get("username")
        self._tasks = [asyncio.create_task(self._poll(), name="bot-poll"),
                       asyncio.create_task(self._sweep(), name="draft-sweep")]
        logger.info("approval bot @%s up", self.bot_username)

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        try:
            await self.http.aclose()
        except Exception:
            pass

    async def notify(self, text: str, *, key: str | None = None, every_s: float = 0,
                     silent: bool = False) -> bool:
        """Message the owner; never raises. `key`/`every_s` rate-limit repeated alerts;
        `silent` delivers without a sound (the per-send log in direct mode)."""
        now = time.time()
        if key and now - self._alerts.get(key, 0) < every_s:
            return False
        try:
            await self.api("sendMessage", chat_id=self.owner_id, text=text[:4000],
                           link_preview_options={"is_disabled": True}, disable_notification=silent)
            if key:
                self._alerts[key] = now
            return True
        except Exception as e:
            logger.warning("notify failed: %s", e)
            return False

    def notify_bg(self, text: str, **kw) -> None:
        asyncio.get_running_loop().create_task(self.notify(text, **kw))

    # ── lock state ───────────────────────────────────────────────────────────
    def _load_locked(self) -> bool:
        try:
            return bool(json.loads(self.state_file.read_text("utf-8")).get("locked"))
        except (OSError, ValueError):
            return False

    def set_locked(self, value: bool) -> None:
        self.locked = value
        write_private(self.state_file, json.dumps({"locked": value, "since": time.time()}))

    # ── drafts ───────────────────────────────────────────────────────────────
    async def submit(self, d: Draft) -> Draft:
        if self.locked:
            raise Fail(LOCKED_MSG)
        now = time.time()
        self._submits = [t for t in self._submits if t > now - 3600]
        if len(self._submits) >= self.max_per_hour:
            raise Fail(f"{self.max_per_hour} drafts were already submitted in the last hour; wait.")
        pending = [x for x in self.drafts.values() if x.status == "pending"]
        if len(pending) >= self.max_pending:
            raise Fail(f"{len(pending)} drafts already wait for the owner's approval "
                       f"({', '.join(x.id for x in pending)}). Wait for them or cancel_send one.")
        d.expires = now + self.ttl_s
        self._submits.append(now)
        self.drafts[d.id] = d
        try:
            res = await self.api(
                "sendMessage", chat_id=self.owner_id, text=card_text(d),
                link_preview_options={"is_disabled": True},
                reply_markup={"inline_keyboard": [[
                    {"text": "✅ Send", "callback_data": f"ok:{d.id}"},
                    {"text": "❌ Cancel", "callback_data": f"no:{d.id}"},
                ]]},
            )
        except Exception as e:
            self.drafts.pop(d.id, None)
            raise Fail(f"Could not put the draft in front of the owner ({e}). "
                       "Has he pressed Start in the approval bot?")
        d.card_id = res["message_id"]
        return d

    async def withdraw(self, d: Draft) -> None:
        if d.status == "pending":
            d.finish("withdrawn", "Withdrawn by the agent before approval.")
            await self._update_card(d)

    async def _update_card(self, d: Draft) -> None:
        if not d.card_id:
            return
        tail = _OUTCOME.get(d.status, d.status)
        if d.result and d.status in ("sent", "failed"):
            tail += f": {d.result}"
        try:  # editing without reply_markup also removes the buttons
            await self.api("editMessageText", chat_id=self.owner_id, message_id=d.card_id,
                           text=f"{card_text(d)}\n\n{tail}"[:4096], link_preview_options={"is_disabled": True})
        except Exception as e:
            logger.warning("card update failed for draft %s: %s", d.id, e)

    async def _execute(self, d: Draft) -> None:
        try:
            if self.executor is None:
                raise Fail("no executor configured")
            d.finish("sent", await self.executor(d))
        except Exception as e:
            logger.warning("approved draft %s failed: %s", d.id, e)
            d.finish("failed", str(e)[:500])
        await self._update_card(d)

    # ── updates ──────────────────────────────────────────────────────────────
    async def _poll(self) -> None:
        while True:
            try:
                updates = await self.api("getUpdates", offset=self._offset, timeout=50,
                                         allowed_updates=["message", "callback_query"])
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("approval bot poll failed: %s", e)
                await asyncio.sleep(5)
                continue
            for u in updates:
                self._offset = u["update_id"] + 1
                try:
                    await self.handle(u)
                except Exception:
                    logger.exception("approval bot update failed")

    async def _sweep(self) -> None:
        while True:
            await asyncio.sleep(20)
            await self.expire_due()

    async def expire_due(self) -> None:
        now = time.time()
        for d in list(self.drafts.values()):
            if d.status == "pending" and now > d.expires:
                d.finish("expired", "Not approved in time.")
                await self._update_card(d)
            elif d.status not in ("pending", "sending") and now - d.created > 86400:
                self.drafts.pop(d.id, None)

    async def handle(self, u: dict) -> None:
        if "callback_query" in u:
            await self._on_callback(u["callback_query"])
        elif "message" in u:
            await self._on_message(u["message"])

    async def _answer(self, qid: str, text: str) -> None:
        try:
            await self.api("answerCallbackQuery", callback_query_id=qid, text=text[:190])
        except Exception as e:
            logger.warning("answerCallbackQuery failed: %s", e)

    async def _on_callback(self, q: dict) -> None:
        if (q.get("from") or {}).get("id") != self.owner_id:
            logger.warning("ignored a button tap from user id %s", (q.get("from") or {}).get("id"))
            await self._answer(q["id"], "Not yours.")
            return
        action, _, did = (q.get("data") or "").partition(":")
        d = self.drafts.get(did)
        if d is None:
            await self._answer(q["id"], "This draft no longer exists (expired, or the server restarted).")
            msg = q.get("message") or {}
            if msg.get("message_id"):
                try:
                    await self.api("editMessageReplyMarkup", chat_id=self.owner_id,
                                   message_id=msg["message_id"], reply_markup={"inline_keyboard": []})
                except Exception:
                    pass
            return
        if d.status != "pending":
            await self._answer(q["id"], f"Already {d.status}.")
            return
        if action == "ok":
            if self.locked:
                await self._answer(q["id"], "Locked — send /unlock first.")
                return
            if time.time() > d.expires:
                d.finish("expired", "Not approved in time.")
                await self._answer(q["id"], "Expired — not sent.")
                await self._update_card(d)
                return
            d.status = "sending"  # before any await: a double tap must not send twice
            await self._answer(q["id"], "Sending…")
            await self._update_card(d)
            asyncio.get_running_loop().create_task(self._execute(d))
        elif action == "no":
            d.finish("cancelled", "Declined by the owner.")
            await self._answer(q["id"], "Cancelled.")
            await self._update_card(d)
        else:
            await self._answer(q["id"], "Unknown action.")

    async def _reply(self, text: str) -> None:
        await self.notify(text)

    async def _on_message(self, m: dict) -> None:
        if (m.get("chat") or {}).get("type") != "private" or (m.get("from") or {}).get("id") != self.owner_id:
            return  # strangers get silence, not an error
        text = (m.get("text") or "").strip()
        cmd = text.split()[0].split("@")[0].lower() if text else ""
        if cmd in ("/start", "/help"):
            await self._reply(help_text(self.approval_mode))
        elif cmd == "/lock":
            self.set_locked(True)
            n = 0
            for d in list(self.drafts.values()):
                if d.status == "pending":
                    d.finish("cancelled", "Cancelled by /lock.")
                    await self._update_card(d)
                    n += 1
            await self._reply(f"🔒 Locked. Every tool refuses until /unlock. {n} pending draft(s) cancelled.")
        elif cmd == "/unlock":
            self.set_locked(False)
            await self._reply("🔓 Unlocked. Tools work again; sends still need your tap.")
        elif cmd == "/pending" and not self.approval_mode:
            await self._reply("Approval is off (TGC_SEND_APPROVAL=false): sends go out directly, "
                              "so nothing ever waits here.")
        elif cmd == "/pending":
            pend = [d for d in self.drafts.values() if d.status == "pending"]
            if not pend:
                await self._reply("No drafts are waiting.")
            for d in pend:
                await self._reply(f"Draft {d.id} → {d.peer_label}, expires {_hhmm(d.expires)} "
                                  "(the card with its buttons is above).")
        elif cmd == "/status":
            await self._reply(await self.status_fn() if self.status_fn else "status unavailable")
        else:
            await self._reply("Commands: /status /pending /lock /unlock /help")
