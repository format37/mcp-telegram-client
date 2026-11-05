"""Run by test_direct_mode.py in a subprocess with TGC_SEND_APPROVAL=false (main reads the mode
at import). Prints one JSON object: what an agent would see, and what a send actually does."""
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

sys.path.insert(0, os.environ["TGC_BACKEND"])

from telethon.tl import types as T  # noqa: E402

import main  # noqa: E402
from mcp.server.fastmcp.exceptions import ToolError  # noqa: E402


async def probe():
    tools = await main.mcp.list_tools()
    out = {
        "instructions": main.INSTRUCTIONS,
        "tools": {t.name: t.description for t in tools},
    }

    ivan = T.User(id=5, first_name="Ivan", username="ivan")
    sent, notes, file_sends = [], [], []

    class FakeClient:
        async def get_messages(self, entity, limit=1):
            return [object()]  # a prior conversation exists

        async def send_file(self, entity, file, **kw):
            file_sends.append([os.path.basename(file), kw.get("caption"), kw.get("force_document")])
            return SimpleNamespace(id=78, date=datetime.now(timezone.utc))

    async def fake_client():
        return FakeClient()

    async def fake_resolve(client, spec, policy, **kw):
        policy.check(ivan)
        return ivan

    async def fake_send(client, entity, text, **kw):
        sent.append(text)
        return SimpleNamespace(id=77, date=datetime.now(timezone.utc))

    async def fake_notify(text, **kw):
        notes.append({"text": text, **kw})
        return True

    main.account.client = fake_client
    main.tg.resolve_peer = fake_resolve
    main.tg.send_text = fake_send
    main.approvals.notify = fake_notify

    out["result"] = await main.send_message("@ivan", "Привет! Всё готово.")
    await asyncio.sleep(0.05)  # let the background log line go out
    out["sent"] = sent
    out["drafts"] = len(main.approvals.drafts)
    out["notes"] = list(notes)

    # a file, through the real upload store and tg.send_files
    notes.clear()
    main.sendlog.min_interval = 0
    out["upload_link"] = await main.upload_link()

    async def body():
        yield b"%PDF-1.4 the report"

    rec = await main.store.receive(main.store.new_ticket(), "отчёт за май.pdf", body())
    out["file_result"] = await main.send_message("@ivan", "Отчёт за май", files=[rec.id])
    await asyncio.sleep(0.05)
    out["file_sends"] = file_sends
    out["file_notes"] = notes

    main.approvals.locked = True
    try:
        await main.send_message("@ivan", "second")
        out["locked"] = "sent anyway"
    except ToolError as e:
        out["locked"] = str(e)
    out["sent_after_lock"] = len(sent)
    return out


print(json.dumps(asyncio.run(probe()), ensure_ascii=False))
