"""Test environment: fake secrets, a throwaway DATA_DIR, backend/ on sys.path.

Set at import time, before any test module imports `tg` or `main` (both read the
environment when they are imported).
"""
import json
import os
import struct
import sys
import tempfile
import zlib
from pathlib import Path

BACKEND = Path(os.getenv("TGC_BACKEND") or Path(__file__).resolve().parent.parent / "backend")
sys.path.insert(0, str(BACKEND))

TOKEN = "t" * 40
BOT_TOKEN = "123456:" + "B" * 35
OWNER = 4242

os.environ.update({
    "DATA_DIR": tempfile.mkdtemp(prefix="tgc-test-"),
    "MCP_TOKENS": TOKEN,
    "TG_API_ID": "1",
    "TG_API_HASH": "h" * 32,
    "TGC_OWNER_ID": str(OWNER),
    "APPROVAL_BOT_TOKEN": BOT_TOKEN,
    "MCP_ALLOWED_HOSTS": "testserver",
    "APPROVAL_WAIT_S": "0.2",
})


class FakeResp:
    def __init__(self, payload):
        self._p = payload
        self.status_code = 200

    def json(self):
        return self._p


class FakeBotHTTP:
    """Stands in for httpx.AsyncClient against the Bot API; records every call."""

    def __init__(self, bot_id=999):
        self.calls = []
        self.bot_id = bot_id
        self._mid = 100

    async def post(self, url, json=None, data=None, files=None):
        method = url.rsplit("/", 1)[-1]
        payload = dict(json or data or {})
        for field, (name, fh, mime) in (files or {}).items():  # a multipart upload: keep what was sent
            payload["_file"] = {"field": field, "name": name, "mime": mime, "bytes": fh.read()}
        self.calls.append((method, payload))
        if method == "getMe":
            return FakeResp({"ok": True, "result": {"id": self.bot_id, "username": "tgc_test_bot"}})
        if method in ("sendMessage", "sendDocument"):
            self._mid += 1
            return FakeResp({"ok": True, "result": {"message_id": self._mid}})
        if method == "getUpdates":
            import asyncio
            await asyncio.sleep(3600)
        return FakeResp({"ok": True, "result": True})

    async def aclose(self):
        pass

    def sent(self, method):
        return [p for m, p in self.calls if m == method]


def dumps(x):
    return json.dumps(x, ensure_ascii=False)


def png(w, h):
    """A valid PNG header claiming w x h pixels, with a sliver of pixel data that is never decoded."""
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\0" * 64)) + chunk(b"IEND", b""))
