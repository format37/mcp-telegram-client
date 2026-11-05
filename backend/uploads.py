"""Files an agent hands over for sending: upload tickets, a streaming upload endpoint, a store.

    upload_link()  ->  curl -T file <public>/upload/<ticket>/   (raw body, PUT or POST)
                   ->  {"id": "f_…", "name", "size", "sha256", "kind", …}
                   ->  send_message(files=[ids])
Uploading sends nothing to anyone. Only send_message does, through the same gate as text.

The ticket is the only credential on the upload route (TokenAuthMiddleware lets
<base>/upload/ through). It is not the MCP token: the URL lands in transcripts
and shell history, so a ticket lives in memory only, expires within minutes,
takes a limited number of files, and can do nothing but add files to this store
within its caps. It never sends.

On disk, under DATA_DIR/uploads:
    <id>/<name>   the bytes, 0600. The path keeps the real name: Telethon takes photo
                  or document, the mime type and the filename the recipient sees from it.
    <id>.json     the record (0600). The index is rebuilt from these at startup.
    <id>.part     an upload in progress, streamed straight to disk and never buffered.
"""
from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import time
import unicodedata
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator, Callable, Iterable

from starlette.convertors import Convertor, register_url_convertor
from starlette.requests import ClientDisconnect, Request
from starlette.responses import Response

import tg

logger = logging.getLogger(__name__)


class _Anything(Convertor[str]):
    """Like Starlette's `path` convertor, but across newlines too. With `path` (regex `.*`) a
    decoded %0A makes the upload route miss, and a request that skipped the token check because
    of its prefix would fall through to the next route."""

    regex = r"[\s\S]*"

    def convert(self, value: str) -> str:
        return value

    def to_string(self, value: str) -> str:
        return value


register_url_convertor("anything", _Anything())
REST = "{rest:anything}"  # the route is <base>/upload/{rest:anything}: every path under the prefix

FILES_PER_TICKET = 20
MAX_NAME_BYTES = 200
ID_RE = re.compile(r"f_[0-9a-f]{12}")
HINT = ("Put the file name at the end of the URL: curl -sS -T /path/to/file <upload URL>/ "
        "(with the trailing slash, curl appends the file's own name)")
# Bidirectional controls can make "invoice‮fdp.exe" display as "invoiceexe.pdf".
_BIDI = dict.fromkeys(map(ord, "؜‎‏‪‫‬‭‮⁦⁧⁨⁩"))
_STRIP = {"Cc", "Cs", "Zl", "Zp"}  # controls, lone surrogates, line and paragraph separators


class UploadError(Exception):
    def __init__(self, status: int, error: str, hint: str = ""):
        super().__init__(error)
        self.status, self.error, self.hint = status, error, hint


@dataclass
class Upload:
    id: str
    name: str
    size: int
    sha256: str
    mime: str
    kind: str
    created: float
    expires: float
    width: int | None = None
    height: int | None = None

    def public(self) -> dict:
        """The JSON an upload answers with."""
        out = {"id": self.id, "name": self.name, "size": self.size, "sha256": self.sha256,
               "mime": self.mime, "kind": self.kind}
        if self.width and self.height:
            out.update(width=self.width, height=self.height)
        out["expires"] = iso(self.expires)
        return out


@dataclass
class Ticket:
    token: str
    expires: float
    used: int = 0    # files stored with it
    active: int = 0  # its uploads running right now


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).astimezone().isoformat(timespec="seconds")


def human(n: int) -> str:
    """1.2 MB, 34 KB, 512 B, 100 MB."""
    if n < 1000:
        return f"{n} B"
    if n < 999_500:
        return f"{n / 1e3:.0f} KB"
    return f"{n / 1e6:.1f}".removesuffix(".0") + " MB"


def clean_name(raw: str) -> str:
    """The name the recipient will see, or "" if none is left: the last path segment, no
    control, separator or bidi characters, NFC (macOS hands curl decomposed letters), and at
    most 200 bytes of UTF-8, cut at a character boundary with the extension kept."""
    name = (raw or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(ch for ch in name if unicodedata.category(ch) not in _STRIP).translate(_BIDI)
    name = unicodedata.normalize("NFC", name).strip()
    if name in ("", ".", ".."):
        return ""
    if len(name.encode()) > MAX_NAME_BYTES:
        stem, ext = os.path.splitext(name)
        if len(ext.encode()) > 20:  # not an extension worth keeping
            stem, ext = name, ""
        stem = stem.encode()[: MAX_NAME_BYTES - len(ext.encode())].decode("utf-8", "ignore")
        name = stem.rstrip() + ext
    return name


def _remove(p: Path) -> None:
    with contextlib.suppress(FileNotFoundError):
        if p.is_dir() and not p.is_symlink():
            shutil.rmtree(p)
        else:
            p.unlink()


class Store:
    """Uploaded files and the tickets that admit them. Single event loop, no locks: every
    state change happens between awaits."""

    def __init__(self, root: Path, *, ttl_s: float, link_ttl_s: float, max_file: int, max_total: int):
        self.root = root
        self.ttl_s, self.link_ttl_s = ttl_s, link_ttl_s
        self.max_file, self.max_total = max_file, max_total
        self.files: dict[str, Upload] = {}
        self.tickets: dict[str, Ticket] = {}
        self.used = 0        # bytes of stored files
        self.inflight = 0    # bytes received so far by uploads still running
        self._receiving: set[str] = set()  # ids whose .part is being written
        self._busy: dict[str, int] = {}    # ids being sent right now; the sweep leaves them alone

    # ── records ──────────────────────────────────────────────────────────────
    def path(self, rec: Upload) -> Path:
        return self.root / rec.id / rec.name

    def _sidecar(self, fid: str) -> Path:
        return self.root / f"{fid}.json"

    def _save(self, rec: Upload) -> None:
        tg.write_private(self._sidecar(rec.id), json.dumps(asdict(rec), ensure_ascii=False))

    def load(self) -> None:
        """Rebuild the index from the sidecars, then drop whatever an interrupted run left."""
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.files, self.used = {}, 0
        known = {f.name for f in fields(Upload)}
        for side in self.root.glob("*.json"):
            try:
                data = json.loads(side.read_text("utf-8"))
                rec = Upload(**{k: v for k, v in data.items() if k in known})
                ok = (rec.id == side.stem and ID_RE.fullmatch(rec.id) and rec.name == clean_name(rec.name)
                      and self.path(rec).is_file())
            except (OSError, ValueError, TypeError):
                ok = False
            if not ok:
                side.unlink(missing_ok=True)
                continue
            self.files[rec.id] = rec
            self.used += rec.size
        self.sweep()
        logger.info("%d stored files, %s", len(self.files), human(self.used))

    def get(self, fid: str) -> Upload | None:
        rec = self.files.get(fid)
        return rec if rec is not None and time.time() <= rec.expires else None

    def resolve(self, ids: Iterable[str]) -> tuple[list[Upload], list[str]]:
        """(records whose bytes are still here, ids that are unknown, expired or gone)."""
        found, missing = [], []
        for fid in ids:
            rec = self.get(fid)
            if rec is not None and self.path(rec).is_file():
                found.append(rec)
            else:
                missing.append(fid)
        return found, missing

    def extend(self, recs: Iterable[Upload], until: float) -> None:
        """Keep these files at least until `until` (a draft that uses them may wait that long)."""
        for rec in recs:
            if rec.expires < until:
                rec.expires = until
                self._save(rec)

    @contextlib.contextmanager
    def in_use(self, recs: Iterable[Upload]):
        """The sweep does not delete these while the block runs (an upload to Telegram can be slow)."""
        ids = [r.id for r in recs]
        for i in ids:
            self._busy[i] = self._busy.get(i, 0) + 1
        try:
            yield
        finally:
            for i in ids:
                self._busy[i] -= 1
                if not self._busy[i]:
                    del self._busy[i]

    def listing(self) -> list[Upload]:
        now = time.time()
        return sorted((r for r in self.files.values() if now <= r.expires), key=lambda r: r.created)

    def summary(self) -> str:
        n = len(self.listing())
        return f"uploads: {n} file{'' if n == 1 else 's'}, {human(self.used)}"

    def _drop(self, rec: Upload) -> None:
        self.files.pop(rec.id, None)
        self.used -= rec.size
        self._sidecar(rec.id).unlink(missing_ok=True)
        _remove(self.root / rec.id)

    def sweep(self, now: float | None = None) -> int:
        """Forget expired tickets; delete expired files and anything no record owns."""
        now = time.time() if now is None else now
        for tok, t in list(self.tickets.items()):
            if now > t.expires and not t.active:
                del self.tickets[tok]
        gone = 0
        for rec in list(self.files.values()):
            if now > rec.expires and rec.id not in self._busy:
                self._drop(rec)
                gone += 1
        with contextlib.suppress(FileNotFoundError):
            for p in self.root.iterdir():
                fid = p.name[:-5] if p.name.endswith(".part") else p.name
                if p.suffix != ".json" and fid not in self.files and fid not in self._receiving:
                    _remove(p)
        return gone

    # ── tickets ──────────────────────────────────────────────────────────────
    def new_ticket(self) -> Ticket:
        t = Ticket(secrets.token_urlsafe(32), time.time() + self.link_ttl_s)
        self.tickets[t.token] = t
        return t

    def ticket(self, token: str) -> Ticket | None:
        """A ticket that may take one more file; None if unknown, expired or used up."""
        t = self.tickets.get(token or "")
        if t is None or time.time() > t.expires or t.used + t.active >= FILES_PER_TICKET:
            return None
        return t

    # ── receiving ────────────────────────────────────────────────────────────
    def _new_id(self) -> str:
        while True:
            fid = "f_" + secrets.token_hex(6)
            if fid not in self.files and fid not in self._receiving and not (self.root / fid).exists():
                return fid

    def _full(self) -> UploadError:
        return UploadError(507, f"the upload store is full ({human(self.max_total)}); "
                                "try again when older uploads expire")

    async def receive(self, ticket: Ticket, raw_name: str, chunks: AsyncIterator[bytes],
                      length: int | None = None) -> Upload:
        """Stream one file to disk under `ticket`. Raises UploadError; leaves nothing behind on failure."""
        name = clean_name(raw_name)
        if not name:
            raise UploadError(400, "no file name in the URL", HINT)
        if length is not None and length > self.max_file:
            raise UploadError(413, f"the file is over the {human(self.max_file)} limit")
        if length is not None and self.used + self.inflight + length > self.max_total:
            raise self._full()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        fid = self._new_id()
        part = self.root / f"{fid}.part"
        ticket.active += 1
        self._receiving.add(fid)
        size, digest = 0, hashlib.sha256()
        try:
            fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as fh:
                async for chunk in chunks:
                    if not chunk:
                        continue
                    size += len(chunk)
                    self.inflight += len(chunk)
                    if size > self.max_file:
                        raise UploadError(413, f"the file is over the {human(self.max_file)} limit")
                    if self.used + self.inflight > self.max_total:
                        raise self._full()
                    fh.write(chunk)
                    digest.update(chunk)
            if not size:
                raise UploadError(400, "the body is empty", HINT)
            rec = self._commit(fid, name, part, size, digest.hexdigest())
            ticket.used += 1
            return rec
        except OSError as e:
            if e.errno in (errno.ENOSPC, errno.EDQUOT):
                raise UploadError(507, "the server's disk is full") from e
            raise
        finally:
            ticket.active -= 1
            self.inflight -= size
            self._receiving.discard(fid)
            part.unlink(missing_ok=True)  # already moved into place if the upload succeeded

    def _commit(self, fid: str, name: str, part: Path, size: int, sha256: str) -> Upload:
        info = tg.probe(part, name)  # kind from the name; a photo's size from its header only
        now = time.time()
        rec = Upload(id=fid, name=name, size=size, sha256=sha256, mime=info.mime, kind=info.kind,
                     created=now, expires=now + self.ttl_s, width=info.width, height=info.height)
        (self.root / fid).mkdir(mode=0o700)
        os.replace(part, self.path(rec))
        self._save(rec)
        self.files[fid] = rec
        self.used += size
        return rec


def _json(status: int, body: dict, headers: dict | None = None) -> Response:
    return Response(json.dumps(body, ensure_ascii=False) + "\n", status_code=status,
                    media_type="application/json", headers=headers)


class Endpoint:
    """ASGI endpoint for everything under <base>/upload/ (route `<base>/upload/` + REST).

    It answers every method itself, so nothing under this token-free prefix can reach
    another app. The path arrives decoded once by uvicorn: take it as is, never unquote
    it again (a second decode turns a%41 into aA)."""

    def __init__(self, store: Store, *, locked: Callable[[], bool]):
        self.store, self.locked = store, locked

    async def __call__(self, scope, receive, send) -> None:
        request = Request(scope, receive)
        try:
            resp = _json(201, (await self._handle(request)).public())
        except UploadError as e:
            body = {"error": e.error, **({"hint": e.hint} if e.hint else {})}
            resp = _json(e.status, body, {"Allow": "PUT, POST"} if e.status == 405 else None)
        except ClientDisconnect:
            resp = _json(400, {"error": "the upload was cut off"})  # nobody is left to read it
        await resp(scope, receive, send)

    async def _handle(self, request: Request) -> Upload:
        if request.method not in ("PUT", "POST"):
            raise UploadError(405, "upload with PUT (curl -T file <url>/) or POST (curl --data-binary @file <url>/name)")
        token, _, name = (request.path_params.get("rest") or "").partition("/")
        ticket = self.store.ticket(token)
        if ticket is None:
            raise UploadError(404, "unknown, expired or used-up upload link; call upload_link() for a new one")
        if self.locked():
            raise UploadError(423, "the owner has locked telegram-client; uploads are refused until he unlocks it")
        cl = request.headers.get("content-length") or ""
        async with contextlib.aclosing(request.stream()) as chunks:
            return await self.store.receive(ticket, name, chunks, int(cl) if cl.isdigit() else None)
