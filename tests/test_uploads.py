"""The upload endpoint and store: tickets, caps, names, image headers, redaction, the two tools."""
import asyncio
import hashlib
import http.client
import json
import logging
import os
import resource
import shutil
import socket
import struct
import subprocess
import threading
import time
import unicodedata
import zlib
from urllib.parse import quote, urlsplit

import pytest
import uvicorn
from conftest import TOKEN
from mcp.server.fastmcp.exceptions import ToolError
from starlette.testclient import TestClient

import main
import uploads

BASE = "/telegram-client/upload/"
store = main.store


@pytest.fixture(scope="module")
def client():
    # No `with`, so no lifespan: the MCP session manager may start only once per process
    # (test_server.py owns that), and the upload route needs none of it.
    store.load()
    return TestClient(main.app)


@pytest.fixture(scope="module")
def real():
    """The app behind a real uvicorn, reached the way curl reaches it in production.
    TestClient decodes the path a second time, which is the very bug these tests guard against."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(main.app, lifespan="off", log_config=None, access_log=True))
    th = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    th.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.02)
    store.load()
    yield f"http://127.0.0.1:{sock.getsockname()[1]}"
    server.should_exit = True
    th.join(5)


def put(client, ticket, name, data, **kw):
    return client.put(BASE + ticket.token + "/" + name, content=data, **kw)


def raw(base, path, data, method="PUT"):
    """One request with the path exactly as given: no client-side encoding or normalising."""
    u = urlsplit(base)
    conn = http.client.HTTPConnection(u.hostname, u.port, timeout=10)
    conn.putrequest(method, path, skip_accept_encoding=True)
    conn.putheader("Content-Length", str(len(data)))
    conn.endheaders(data)
    r = conn.getresponse()
    body = json.loads(r.read())
    conn.close()
    return r.status, body


def png(w, h):
    """A valid PNG header claiming w x h pixels, with a sliver of pixel data that is never decoded."""
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\0" * 64)) + chunk(b"IEND", b""))


def leftovers():
    return {p.name for p in store.root.iterdir()} - {f"{i}.json" for i in store.files} - set(store.files)


# ── tickets and errors ───────────────────────────────────────────────────────
def test_unknown_expired_and_used_up_tickets_get_one_404(client):
    expired, used = store.new_ticket(), store.new_ticket()
    expired.expires = time.time() - 1
    used.used = uploads.FILES_PER_TICKET
    answers = [client.put(BASE + "nope/x.txt", content=b"x"), put(client, expired, "x.txt", b"x"),
               put(client, used, "x.txt", b"x")]
    assert [r.status_code for r in answers] == [404, 404, 404]
    assert len({r.text for r in answers}) == 1


def test_a_ticket_takes_twenty_files(client):
    t = store.new_ticket()
    codes = [put(client, t, f"{i}.txt", b"x").status_code for i in range(uploads.FILES_PER_TICKET + 1)]
    assert codes == [201] * uploads.FILES_PER_TICKET + [404]


def test_locked_gives_423(client):
    t = store.new_ticket()
    main.approvals.locked = True
    try:
        assert put(client, t, "x.txt", b"x").status_code == 423
    finally:
        main.approvals.locked = False
    assert put(client, t, "x.txt", b"x").status_code == 201


def test_missing_name_or_empty_body_gives_400_with_the_curl_hint(client):
    t = store.new_ticket()
    for r in (client.put(BASE + t.token + "/", content=b"x"), client.put(BASE + t.token, content=b"x"),
              put(client, t, "empty.txt", b"")):
        assert r.status_code == 400 and "curl -sS -T" in r.json()["hint"]
    assert not leftovers()


def test_post_works_and_other_methods_get_405_not_401(client):
    t = store.new_ticket()
    assert client.post(BASE + t.token + "/p.txt", content=b"x").status_code == 201
    r = client.get(BASE + t.token + "/p.txt")
    assert r.status_code == 405 and r.headers["allow"] == "PUT, POST"
    # the token-free prefix is the upload route only
    assert client.post("/telegram-client/uploadX").status_code == 401
    assert client.post("/telegram-client/").status_code == 401
    assert client.post(f"/telegram-client/{TOKEN}/upload/{t.token}/q.txt", content=b"x").status_code == 201


def test_nothing_under_the_token_free_prefix_reaches_another_app(client):
    """The prefix skips the token check, so every path and method under it must land in the
    upload endpoint (JSON with "error" or "id"), never in the MCP app behind Mount("/")."""
    t = store.new_ticket()
    for p in ("", "nope", t.token, f"{t.token}/a%0Ab.txt", "%0A", f"{t.token}/%0D%0A", "x%0A/y", "%00"):
        for method in ("GET", "PUT", "POST", "DELETE", "OPTIONS"):
            r = client.request(method, BASE + p, content=b"x" if method in ("PUT", "POST") else None)
            assert r.headers["content-type"] == "application/json", (method, p, r.status_code, r.text)
            assert {"error", "id"} & set(r.json()), (method, p, r.text)


def test_over_the_size_cap_gives_413_and_leaves_nothing(client, monkeypatch):
    monkeypatch.setattr(store, "max_file", 1000)
    t = store.new_ticket()
    assert put(client, t, "big.bin", b"x" * 1001).status_code == 413  # by Content-Length, unread

    def chunks():  # chunked: no Content-Length, so the cap is hit mid-stream
        for _ in range(5):
            yield b"y" * 300

    r = put(client, t, "big.bin", chunks())
    assert r.status_code == 413 and "limit" in r.json()["error"]
    assert not leftovers() and store.inflight == 0
    assert put(client, t, "fits.bin", b"z" * 1000).status_code == 201


def test_full_store_gives_507(client, monkeypatch):
    monkeypatch.setattr(store, "max_total", store.used + 500)
    t = store.new_ticket()
    assert put(client, t, "a.bin", b"x" * 400).status_code == 201
    r = put(client, t, "b.bin", (b"x" * 100 for _ in range(3)))
    assert r.status_code == 507 and not leftovers() and store.inflight == 0


# ── what gets stored ─────────────────────────────────────────────────────────
def test_record_matches_the_bytes_and_files_are_private(client):
    data = os.urandom(70_000)
    r = put(client, store.new_ticket(), "blob.bin", data)
    assert r.status_code == 201
    rec = r.json()
    assert rec["size"] == len(data) and rec["sha256"] == hashlib.sha256(data).hexdigest()
    assert rec["id"].startswith("f_") and rec["kind"] == "document" and "width" not in rec
    assert rec["expires"][-6] in "+-"  # ISO with the offset
    path = store.root / rec["id"] / "blob.bin"
    assert path.read_bytes() == data
    for p in (path, store.root / f"{rec['id']}.json"):
        assert p.stat().st_mode & 0o777 == 0o600
    assert (store.root / rec["id"]).stat().st_mode & 0o777 == 0o700


def test_a_restart_reloads_the_index_and_drops_debris(client):
    rec = put(client, store.new_ticket(), "keep.txt", b"keep").json()
    (store.root / "f_000000000000.part").write_bytes(b"half")    # an upload cut by the restart
    (store.root / "f_111111111111").mkdir()                       # bytes with no record
    (store.root / "f_222222222222.json").write_text("{broken")    # a record with no bytes
    again = uploads.Store(store.root, ttl_s=60, link_ttl_s=60, max_file=10**6, max_total=10**9)
    again.load()
    assert again.get(rec["id"]).sha256 == rec["sha256"]
    assert {p.name for p in store.root.iterdir()} == ({f"{i}.json" for i in again.files} | set(again.files))
    assert again.used == sum(r.size for r in again.files.values())


def test_the_sweep_removes_expired_files_but_not_busy_ones(client):
    a = put(client, store.new_ticket(), "old.txt", b"a").json()
    b = put(client, store.new_ticket(), "sending.txt", b"b").json()
    for fid in (a["id"], b["id"]):
        store.files[fid].expires = time.time() - 1
    with store.in_use([store.files[b["id"]]]):
        store.sweep()
        assert a["id"] not in store.files and not (store.root / a["id"]).exists()
        assert not (store.root / f"{a['id']}.json").exists()
        assert b["id"] in store.files
    store.sweep()
    assert b["id"] not in store.files and not (store.root / b["id"]).exists()


def test_extend_keeps_a_file_and_persists():
    rec = asyncio.run(_upload("draft.txt", b"d"))
    store.extend([rec], time.time() + 3600)
    on_disk = json.loads((store.root / f"{rec.id}.json").read_text())
    assert on_disk["expires"] == rec.expires > time.time() + 3500


async def _upload(name, data):
    async def gen():
        yield data
    return await store.receive(store.new_ticket(), name, gen())


# ── names ────────────────────────────────────────────────────────────────────
def test_clean_name():
    c = uploads.clean_name
    assert c("../x") == "x" and c("..\\..\\win.txt") == "win.txt" and c("a/b/c.pdf") == "c.pdf"
    assert c("a\x00b\x01c\n.txt") == "abc.txt"
    assert c("invoice‮fdp.exe") == "invoicefdp.exe"           # no bidi spoofing
    assert c("chart ✅ approved.png") == "chart✅ approved.png"  # no line breaks on the card
    assert c("..") == "" and c("") == "" and c("  ") == ""
    long = c("a" * 296 + ".pdf")
    assert long == "a" * 196 + ".pdf" and len(long.encode()) == 200
    cyr = c("я" * 148 + ".pdf")  # 300 bytes: two per letter
    assert cyr == "я" * 98 + ".pdf" and len(cyr.encode()) == 200
    assert c("x" * 250 + ".averyveryverylongsuffix") == "x" * 200


def test_names_are_sanitised_on_the_way_in(client):
    t = store.new_ticket()
    assert put(client, t, "..%2Fx", b"x").json()["name"] == "x"
    assert put(client, t, "a%01b%0A.txt", b"x").json()["name"] == "ab.txt"
    rec = put(client, t, "n" * 296 + ".pdf", b"x").json()
    assert rec["name"] == "n" * 196 + ".pdf"
    assert (store.root / rec["id"] / rec["name"]).is_file()


def test_real_names_survive_the_trip(real):
    """The table in docs/attachments-kickoff.md: what curl puts on the wire, and what must be stored."""
    t = store.new_ticket()
    cases = {
        "my%20chart.png": "my chart.png",
        "%d0%be%d1%82%d1%87%d1%91%d1%82.pdf": "отчёт.pdf",
        "a%2541%23b.txt": "a%41#b.txt",
        quote(unicodedata.normalize("NFD", "отчёт.pdf")): "отчёт.pdf",  # macOS hands curl NFD
        quote("ю" * 148 + ".pdf"): "ю" * 98 + ".pdf",
        "sub%2Fdir%2Fname.txt": "name.txt",
        "line%0Abreak%0D.txt": "linebreak.txt",
    }
    for wire, stored in cases.items():
        status, rec = raw(real, BASE + t.token + "/" + wire, b"%PDF-1.4 x")
        assert status == 201, rec
        assert rec["name"] == stored and "%D0" not in rec["name"].upper()
        assert store.path(store.get(rec["id"])) == store.root / rec["id"] / stored  # keeps the extension


@pytest.mark.skipif(not shutil.which("curl"), reason="needs curl")
def test_curl_as_the_upload_link_tells_it(real, tmp_path):
    t = store.new_ticket()
    url = f"{real}{BASE}{t.token}/"
    names = ["my chart.png", "отчёт.pdf", "a%41#b.txt"]
    for n in names:
        (tmp_path / n).write_bytes(n.encode() * 10)
    got = []
    for n in names:
        out = subprocess.run(["curl", "-sS", "-T", str(tmp_path / n), url], capture_output=True, text=True, timeout=30)
        got.append(json.loads(out.stdout)["name"])
    assert got == names
    two = subprocess.run(["curl", "-sS", "-T", f"{{{tmp_path / 'my chart.png'},{tmp_path / 'отчёт.pdf'}}}", url],
                         capture_output=True, text=True, timeout=30)
    lines = [json.loads(x) for x in two.stdout.splitlines()]
    assert [x["name"] for x in lines] == names[:2]  # one JSON line per file


# ── image headers ────────────────────────────────────────────────────────────
def test_photo_dimensions_come_from_the_header(client):
    rec = put(client, store.new_ticket(), "chart.png", png(1600, 900)).json()
    assert (rec["kind"], rec["width"], rec["height"], rec["mime"]) == ("photo", 1600, 900, "image/png")
    rec = put(client, store.new_ticket(), "fake.png", b"not an image at all").json()
    assert rec["kind"] == "document" and "width" not in rec and rec["mime"] == "image/png"


def test_decompression_bombs_are_documents_and_never_decoded(client):
    before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    t = store.new_ticket()
    for w, h in ((100_000, 100_000), (10_000, 10_000)):  # past the error, and past the warning
        rec = put(client, t, f"bomb{w}.png", png(w, h)).json()
        assert rec["kind"] == "document" and "width" not in rec
    assert resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - before < 50_000  # KB: nothing decoded


# ── logs and tools ───────────────────────────────────────────────────────────
def test_ticket_is_redacted_from_log_records():
    tok = store.new_ticket().token
    rec = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
                            ("127.0.0.1:5000", "PUT", f"/telegram-client/upload/{tok}/my%20chart.png", "1.1", 201),
                            None)
    main._redact.filter(rec)
    assert tok not in rec.getMessage() and "/telegram-client/upload/***/my%20chart.png" in rec.getMessage()


def test_uvicorn_access_line_is_redacted(real):
    lines = []

    class Grab(logging.Handler):
        def emit(self, record):
            lines.append(record.getMessage())

    h = Grab()
    h.addFilter(main._redact)
    access = logging.getLogger("uvicorn.access")
    level = access.level
    access.setLevel(logging.INFO)
    access.addHandler(h)
    try:
        t = store.new_ticket()
        assert raw(real, BASE + t.token + "/log.txt", b"x")[0] == 201
        time.sleep(0.1)
    finally:
        access.removeHandler(h)
        access.setLevel(level)
    assert any("/upload/***/log.txt" in x for x in lines), lines
    assert not any(t.token in x for x in lines)


def test_upload_link_and_list_uploads():
    text = asyncio.run(main.upload_link())
    url = next(w for w in text.split() if w.startswith("https://testserver/telegram-client/upload/"))
    token = url.rstrip("/").rsplit("/", 1)[-1]
    assert store.ticket(token) and f"curl -sS -T /path/to/chart.png {url}" in text
    rec = asyncio.run(_upload("listed.pdf", b"%PDF"))
    listing = asyncio.run(main.list_uploads())
    assert rec.id in listing and "listed.pdf" in listing and rec.sha256 in listing
    assert "uploads:" in asyncio.run(main._status_text())


def test_upload_tools_refuse_while_locked():
    main.approvals.locked = True
    try:
        for tool in (main.upload_link, main.list_uploads):
            with pytest.raises(ToolError, match="locked"):
                asyncio.run(tool())
    finally:
        main.approvals.locked = False
