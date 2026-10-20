"""0.21.5: the Pre/PostToolUse hook never logs call bookkeeping as activity (Delta read
"Read call event" / "Wait for next call event" / "Restart listening loop" aloud). Real work
stays; an ignore list (VOICEHOOK_ACTIVITY_IGNORE, ~/.voicehook/activity-ignore.txt) drops more."""
from __future__ import annotations

import json
import os

import pytest

from voicehook_agent import activity, activityfilter
from voicehook_agent import session as vsession


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("VOICEHOOK_AGENT_HOME", str(tmp_path / "vh"))
    monkeypatch.delenv("VOICEHOOK_SESSION", raising=False)
    monkeypatch.delenv(activityfilter.ENV_IGNORE, raising=False)
    monkeypatch.delenv(activityfilter.ENV_OFF, raising=False)
    d = vsession.session_dir("blau-tiger-fluss-AB12", "claude-x")
    d.mkdir(parents=True)
    vsession.write_info(d, {"pid": os.getpid(), "room": "blau-tiger-fluss-AB12", "identity": "claude-x"})
    return d


def _ev(command=None, description=None, tool="Bash", **inp):
    ti = dict(inp)
    if command is not None:
        ti["command"] = command
    if description is not None:
        ti["description"] = description
    return {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": ti,
            "session_id": "s1", "tool_use_id": f"t{abs(hash((command, description, tool)))}"}


def _logged(home, ev) -> list[str]:
    activity.pre_tool_use(json.dumps(ev))
    p = home / activity.ACTIVITY_NAME
    return p.read_text().splitlines() if p.exists() else []


ADMIN = [
    _ev("voicehook-agent next --timeout 10", "Read call event"),
    _ev("voicehook-agent listen", "Wait for next call event"),
    _ev("pkill -f 'voicehook-agent listen'; voicehook-agent listen", "Restart listening loop"),
    _ev("voicehook-agent status --doing 'Fix' --open 'Tests'", "Update status board"),
    _ev("voicehook-agent say 'fertig'", "Tell the user"),
    _ev("voicehook-agent send-data --to '*' --json '{}'", "Send data to Scout"),
    _ev("sleep 2; voicehook-agent decide --question 'PR mergen?'", "Ask about merge"),
    _ev("~/bin/vh-wait-events", "Block until new events"),
    _ev("bash ~/.voicehook/listen-waiter.sh", "Start waiter"),
    _ev("tail -n 5 /private/tmp/claude-1/tasks/abc123.output", "Show task output"),
    _ev("cat /tmp/x/tasks/b9.output | grep user", "Check what came in"),
    _ev("echo hi", "Check call events"),                       # description only about the call
    _ev(None, "Lese das Anrufereignis", tool="Agent"),
    _ev(None, None, tool="TaskOutput", task_id="b1"),
    _ev(None, None, tool="Read", file_path="/private/tmp/claude/tasks/b1.output"),
    _ev(None, None, tool="mcp__voicehook__next"),
]
WORK = [
    _ev("python -m pytest tests -q", "Run the test suite"),
    _ev("sleep 300", "Sending deploy to JEV"),
    _ev("gh pr merge 96 --squash", "Merge PR 96"),
    _ev("voicehook-agent join https://x/r/a-b-c-ABCD --name C --model M", "Join the call"),
    _ev("grep -rn listen src/", "Find the call handler code"),
    _ev(None, None, tool="Read", file_path="/repo/src/voicehook_agent/cli.py"),
    _ev("git commit -m x", "Commit event loop fix"),
]


@pytest.mark.parametrize("ev", ADMIN, ids=lambda e: e["tool_input"].get("description") or e["tool_name"])
def test_call_bookkeeping_is_not_logged(home, ev):
    assert activityfilter.skip(ev) is True
    assert _logged(home, ev) == []


@pytest.mark.parametrize("ev", WORK, ids=lambda e: e["tool_input"].get("description") or e["tool_name"])
def test_real_work_still_logged(home, ev):
    assert activityfilter.skip(ev) is False
    assert len(_logged(home, ev)) == 1


def test_ignore_list_from_env_and_file(home, monkeypatch, tmp_path):
    ev = _ev("make deploy-preview", "Poll the preview deploy")
    assert activityfilter.skip(ev) is False
    monkeypatch.setenv(activityfilter.ENV_IGNORE, r"^poll the preview;;[")  # broken regex skipped
    assert activityfilter.skip(ev) is True
    monkeypatch.delenv(activityfilter.ENV_IGNORE)
    state = activity.state_root()
    state.mkdir(parents=True, exist_ok=True)
    (state / activityfilter.IGNORE_FILE).write_text("# my noise\nmake deploy-preview\n")
    assert activityfilter.skip(ev) is True
    assert _logged(home, ev) == []


def test_filter_off_keeps_builtins_out_but_ignore_list_applies(home, monkeypatch):
    monkeypatch.setenv(activityfilter.ENV_OFF, "0")
    assert activityfilter.skip(_ev("voicehook-agent next", "Read call event")) is False
    monkeypatch.setenv(activityfilter.ENV_IGNORE, "read call")
    assert activityfilter.skip(_ev("voicehook-agent next", "Read call event")) is True


# --------------------------------------------------------------------------- #
# subagents (0.21.5): a background agent's steps are not the operator's own activity
# --------------------------------------------------------------------------- #
def _sub(desc, agent="a1b2c3", kind="general-purpose", **kw):
    ev = _ev("python scripts/check.py", desc)
    ev.update({"agent_id": agent, "agent_type": kind, **kw})
    return ev


def test_subagent_tool_calls_are_dropped_by_default(home, monkeypatch):
    monkeypatch.delenv(activityfilter.ENV_SUBAGENT, raising=False)
    assert _logged(home, _sub("Feile an der ersten Kampagnenauswahl")) == []
    assert _logged(home, _sub("Skript für Als-Kunde-ansehen hinzufügen")) == []
    # transcript under subagents/ without agent_id (older payload) counts too
    ev = _ev("make test", "Run checks")
    ev["transcript_path"] = "/Users/x/.claude/projects/p/abc/subagents/agent-77.jsonl"
    assert activityfilter.subagent(ev) == "agent-77"
    assert _logged(home, ev) == []
    # the operator's own call stays
    assert _logged(home, _ev("python -m pytest -q", "Run the test suite")) != []


def test_subagent_bundle_mode_one_line_per_agent(home, monkeypatch):
    monkeypatch.setenv(activityfilter.ENV_SUBAGENT, "bundle")
    _logged(home, _sub("Schritt eins", kind="Explore"))
    _logged(home, _sub("Schritt zwei", kind="Explore"))
    lines = _logged(home, _sub("Schritt eins", agent="zz9", kind="Plan"))
    texts = [ln.split(" ", 1)[1] for ln in lines]
    assert texts == ["note: Hintergrund-Agent für Explore arbeitet",
                     "note: Hintergrund-Agent für Plan arbeitet"]
    assert not any("Schritt" in ln for ln in lines)
