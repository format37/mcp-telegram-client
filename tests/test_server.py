"""The HTTP surface: token gate, redaction, and a real MCP round trip with no session yet."""
import json
import logging

import pytest
from conftest import BOT_TOKEN, OWNER, TOKEN, FakeBotHTTP
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

import main

HDRS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}


def echo_app():
    async def echo(request):
        return PlainTextResponse(request.url.path)

    app = Starlette(routes=[Route("/{p:path}", echo, methods=["GET", "POST"])])
    app.add_middleware(main.TokenAuthMiddleware)
    return TestClient(app)


def test_requests_without_token_are_rejected():
    c = echo_app()
    assert c.post("/telegram-client/").status_code == 401
    assert c.post("/telegram-client/" + "x" * 40 + "/").status_code == 401
    assert c.post("/telegram-client/", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_path_token_is_stripped_before_the_app_sees_it():
    c = echo_app()
    r = c.post(f"/telegram-client/{TOKEN}/")
    assert r.status_code == 200 and r.text == "/telegram-client/"
    r = c.post(f"/telegram-client/{TOKEN}")
    assert r.text == "/telegram-client/"


def test_header_and_query_tokens():
    c = echo_app()
    assert c.post("/telegram-client/", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200
    assert c.post(f"/telegram-client/?token={TOKEN}").status_code == 200


def test_health_is_public():
    c = echo_app()
    assert c.get("/health").status_code == 200
    assert c.get("/telegram-client/health").status_code == 200


def test_secrets_are_redacted_from_logs(caplog):
    rec = logging.LogRecord("x", logging.INFO, __file__, 1, "POST /telegram-client/%s/ via bot%s", (TOKEN, BOT_TOKEN), None)
    main._redact.filter(rec)
    assert TOKEN not in rec.getMessage() and BOT_TOKEN not in rec.getMessage()


@pytest.fixture(scope="module")
def live():
    # Module scope: the MCP session manager's run() may be entered once per process.
    fake = FakeBotHTTP()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(main.approvals, "http", fake)
        with TestClient(main.app) as c:
            yield c, fake


def _rpc(c, method, params=None, sid=None, id_=1):
    h = dict(HDRS)
    if sid:
        h["mcp-session-id"] = sid
    return c.post(f"/telegram-client/{TOKEN}/", headers=h,
                  json={"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}})


def test_mcp_round_trip_without_a_session(live):
    c, fake = live
    r = _rpc(c, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                               "clientInfo": {"name": "t", "version": "0"}})
    assert r.status_code == 200, r.text
    init = r.json()["result"]
    assert "draft" in init["instructions"].lower()  # approval mode (the default) is described
    sid = r.headers.get("mcp-session-id")
    c.post(f"/telegram-client/{TOKEN}/", headers={**HDRS, "mcp-session-id": sid},
           json={"jsonrpc": "2.0", "method": "notifications/initialized"})

    tools = {t["name"] for t in _rpc(c, "tools/list", sid=sid, id_=2).json()["result"]["tools"]}
    assert tools == {"whoami", "list_dialogs", "find_peer", "read_history", "search_chat",
                     "get_media", "upload_link", "list_uploads", "send_message", "send_status", "cancel_send"}

    res = _rpc(c, "tools/call", {"name": "read_history", "arguments": {"peer": "@ivan"}}, sid=sid, id_=3).json()["result"]
    assert res["isError"] and "not logged in" in json.dumps(res)
    # the owner hears about it through the approval bot
    assert any("not logged in" in p.get("text", "") for p in fake.sent("sendMessage"))


def test_locked_server_refuses_reads(live):
    c, _ = live
    main.approvals.locked = True
    try:
        r = _rpc(c, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                   "clientInfo": {"name": "t", "version": "0"}})
        sid = r.headers.get("mcp-session-id")
        res = _rpc(c, "tools/call", {"name": "list_dialogs", "arguments": {}}, sid=sid, id_=2).json()["result"]
        assert res["isError"] and "locked" in json.dumps(res).lower()
    finally:
        main.approvals.locked = False
