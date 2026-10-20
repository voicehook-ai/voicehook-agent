"""0.21.4: mirrored permission prompts are real questions, never from unattended runs.

Live (20.10.2026): the board showed "Freigabe für Bash?" (no detail; Oliver: "Das ist keine
richtige Frage"), and a per-minute `claude -p` timer on crown posted a new "Freigabe für
Read: b.json?" on every run that nobody could answer."""
from __future__ import annotations

import json
import os

import pytest

from voicehook_agent import asks, notify
from voicehook_agent import multisession as ms

URL = "https://voicehook.ai/api/activity/relay"
TOKEN = "fake.relay.test.only"  # fake, low entropy (gitleaks)


@pytest.fixture(autouse=True)
def relay(monkeypatch, tmp_path):
    for k in (ms.MACHINE_LABEL_ENV, ms.NO_RELAY_ENV, "VOICEHOOK_SESSION"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("VOICEHOOK_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv(ms.LABEL_ENV, "Kunde C")
    ms.write_relay(URL, TOKEN, "room-a", 3600, owner_pid=os.getpid(),
                   owner_session=ms.session_hash("JOINER"))
    monkeypatch.setattr(ms, "_spawn_post", lambda *a: None)


def _perm(tool="Bash", sid="s-1", **inp):
    return json.dumps({"session_id": sid, "hook_event_name": "PermissionRequest", "cwd": "/w/p",
                       "transcript_path": "", "tool_name": tool, "tool_input": inp})


def _q(raw, label="Kunde C"):
    got = notify.permission_question(json.loads(raw), label)
    return got[0] if got else None


def test_bash_names_session_and_command():
    assert _q(_perm(command="git worktree remove /Users/o/dev/wt-a", description="Worktree weg")) == \
        "Kunde C will ausführen: git worktree remove wt-a – erlauben?"


def test_bash_secret_redacted_and_long_command_cut():
    q = _q(_perm(command="deploy --token=aaaaaaaaaaaaaaaaaaaa --to https://api.x.io/v1/a"))
    assert "aaaaaaaa" not in q and "[redacted]" in q
    assert q.endswith(" api.x.io – erlauben?")             # the URL keeps only its host
    q = _q(_perm(command="python run.py " + "--flag " * 60))
    assert q.endswith("… – erlauben?") and len(q) <= notify.QUESTION_MAX
    # only the first line of a multi-line command
    assert _q(_perm(command="\nset -e\nrm -rf build")) == "Kunde C will ausführen: set -e – erlauben?"


@pytest.mark.parametrize("tool,inp,want", [
    ("Read", {"file_path": "/Users/o/kunden/acme/b.json"}, "Kunde C will lesen: b.json – erlauben?"),
    ("Write", {"file_path": "/x/y/out.md"}, "Kunde C will schreiben: out.md – erlauben?"),
    ("WebFetch", {"url": "https://intern.example.com/a?b=c"},
     "Kunde C will abrufen: intern.example.com – erlauben?"),
    ("Agent", {"description": "Tests reparieren", "prompt": "..."},
     "Kunde C will Agent nutzen: Tests reparieren – erlauben?"),
])
def test_other_tools_name_the_concrete_target(tool, inp, want):
    assert _q(_perm(tool=tool, **inp)) == want


@pytest.mark.parametrize("tool,inp", [("Bash", {}), ("Bash", {"command": "   "}),
                                      ("Read", {}), ("SomeTool", {"x": 1})])
def test_no_detail_no_decision_only_activity(tool, inp):
    raw = _perm(tool=tool, **inp)
    assert _q(raw) is None
    jobs: list = []
    assert notify.handle(raw, now=1000.0, post=jobs.append) is None and jobs == []


def test_generic_permission_notification_never_a_decision():
    raw = json.dumps({"session_id": "s-1", "hook_event_name": "Notification", "cwd": "/w/p",
                      "message": "Claude needs your permission to use Bash",
                      "notification_type": "permission_prompt"})
    jobs: list = []
    assert notify.handle(raw, now=1000.0, post=jobs.append) is None and jobs == []


@pytest.mark.parametrize("env,want", [
    ({}, False),
    ({"CLAUDE_CODE_ENTRYPOINT": "cli"}, False),
    ({"CLAUDE_CODE_ENTRYPOINT": "claude-desktop", "CLAUDE_CODE_SESSION_ATTENDED": "1"}, False),
    ({"CLAUDE_CODE_ENTRYPOINT": "sdk-cli"}, True),          # claude -p
    ({"CLAUDE_CODE_ENTRYPOINT": "sdk-ts"}, True),
    ({"CLAUDE_CODE_ENTRYPOINT": "sdk-ts", "CLAUDE_CODE_SESSION_ATTENDED": "1"}, False),
    ({"CLAUDE_CODE_SESSION_ATTENDED": "0"}, True),
    ({"CLAUDE_CODE_ENTRYPOINT": "sdk-cli", "VOICEHOOK_ATTENDED": "1"}, False),
    ({"CLAUDE_CODE_ENTRYPOINT": "cli", "VOICEHOOK_ATTENDED": "0"}, True),
])
def test_unattended_detection(env, want):
    assert notify.unattended(env) is want


def test_unattended_session_posts_no_question(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "sdk-cli")
    jobs: list = []
    raw = _perm(tool="Read", file_path="/x/b.json")
    assert notify.handle(raw, now=1000.0, post=jobs.append) is None and jobs == []
    ask = json.dumps({"session_id": "s-1", "hook_event_name": "PreToolUse", "cwd": "/w/p",
                      "tool_name": "AskUserQuestion", "tool_use_id": "t1",
                      "tool_input": {"questions": [{"question": "Welche Farbe nehmen wir?",
                                                    "options": [{"label": "Teal"}, {"label": "Grau"}]}]}})
    assert notify.handle(ask, now=1001.0, post=jobs.append) is None and jobs == []
    stop = json.dumps({"session_id": "s-1", "hook_event_name": "Stop", "cwd": "/w/p",
                       "last_assistant_message": "Soll ich mergen?"})
    assert asks.stop_ask(stop, create=lambda job: 1 / 0) == (0, "")
    assert asks.ask_wait(ask) is None


def test_same_question_same_label_once_across_sessions():
    jobs: list = []
    raw = lambda sid: _perm(tool="Read", sid=sid, file_path="/x/b.json")
    assert notify.handle(raw("run-1"), now=1000.0, post=jobs.append) is not None
    assert notify.handle(raw("run-2"), now=1060.0, post=jobs.append) is None   # next run
    assert notify.handle(raw("run-3"), now=1000.0 + notify.RECENT_DEDUPE_S + 1,
                         post=jobs.append) is not None
    assert len(jobs) == 2
