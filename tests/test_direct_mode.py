"""TGC_SEND_APPROVAL=false: sends go out at once, and nothing the agent reads suggests otherwise."""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from conftest import BACKEND

PROBE = Path(__file__).with_name("_direct_probe.py")
# Words that would make an agent wait for, or ask for, a confirmation that does not exist.
APPROVAL_VOCAB = ["draft", "approv", "✅ /", "❌", "tap", "send_status", "cancel_send", "card"]


@pytest.fixture(scope="module")
def probe():
    env = dict(os.environ, TGC_SEND_APPROVAL="false", TGC_BACKEND=str(BACKEND),
               DATA_DIR=tempfile.mkdtemp(prefix="tgc-direct-"))
    r = subprocess.run([sys.executable, str(PROBE)], env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_no_draft_tools(probe):
    assert set(probe["tools"]) == {"whoami", "list_dialogs", "find_peer", "read_history",
                                   "search_chat", "get_media", "upload_link", "list_uploads", "send_message"}


def test_agent_facing_text_has_no_approval_vocabulary(probe):
    texts = {"instructions": probe["instructions"], **probe["tools"]}
    for where, text in texts.items():
        low = (text or "").lower()
        for word in APPROVAL_VOCAB:
            assert word not in low, f"{where!r} mentions {word!r}"
    assert "immediately" in probe["tools"]["send_message"]
    assert "no confirmation step" in probe["instructions"]


def test_send_goes_out_at_once_without_a_draft(probe):
    assert probe["result"].startswith("✅ Sent #77 to Ivan")
    assert probe["sent"] == ["Привет! Всё готово."]
    assert probe["drafts"] == 0


def test_owner_gets_a_silent_log_line(probe):
    (note,) = probe["notes"]
    assert note.get("silent") is True and "Привет! Всё готово." in note["text"]


def test_lock_still_stops_sends(probe):
    assert "locked" in probe["locked"].lower()
    assert probe["sent_after_lock"] == 1
