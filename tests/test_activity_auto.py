"""0.13.0: activity flows automatically (Oliver 08.10.2026).

1. Hook -> bridge: with ~/.voicehook/bridge-session.json (Quickstart A, curl bridge) the
   Claude Code hook POSTs the line to <base>/api/bridge/activity {"text"} with the bridge
   session's Bearer token; never blocking (exit 0, 2 s timeout), at most one POST per 5 s,
   the latest line wins (a flusher sends it when the window ends).
2. Pointer files ~/.voicehook/joins/<pid>.json: the hook finds a join that runs with its
   own VOICEHOOK_AGENT_HOME (Quickstart B wrapper).
3. `next` carries activity_now {line, age_s} and board_now {doing, age_s}.
"""
from __future__ import annotations

import asyncio
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from voicehook_agent import activity, cli, relay
from voicehook_agent import session as vsession

BOARD = {"doing": "baut den Fix, ETA 5 min", "open": ["Tests"], "done": []}


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv(activity.STATE_ENV, str(tmp_path / "state"))
    monkeypatch.setenv("VOICEHOOK_AGENT_HOME", str(tmp_path / "vh"))
    monkeypatch.delenv("VOICEHOOK_SESSION", raising=False)
    return tmp_path / "state"


@pytest.fixture
def posts(monkeypatch):
    sent: list[tuple[str, str, str]] = []
    spawned: list[float] = []

    def fake_post(base, session, text, timeout=activity.BRIDGE_TIMEOUT_S):
        sent.append((base, session, text))
        return 200
    monkeypatch.setattr(activity, "_bridge_post", fake_post)
    monkeypatch.setattr(activity, "_spawn_flusher", lambda delay: spawned.append(delay))
    return SimpleNamespace(sent=sent, spawned=spawned)


def _event(phase="PreToolUse", **kw) -> str:
    ev = {"session_id": "s1", "hook_event_name": phase, "tool_name": "Bash", "tool_input": {}}
    ev.update(kw)
    return json.dumps(ev)


def _save_session(base="https://voicehook.ai", session="sess-abc"):
    activity.save_bridge_session({"ok": True, "session": session, "room": "x"}, base)


# ----- bridge session file ----------------------------------------------------------
def test_save_bridge_session_only_base_and_session_0600(state):
    activity.save_bridge_session({"session": "sess-abc", "token": "LK-SECRET", "room": "r"},
                                 "https://voicehook.ai/")
    p = state / activity.BRIDGE_SESSION_NAME
    assert json.loads(p.read_text()) == {"base": "https://voicehook.ai", "session": "sess-abc"}
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(state).st_mode) == 0o700
    activity.clear_bridge_session()
    assert not p.exists()


def test_save_bridge_session_refuses_missing_session_or_bad_base(state):
    with pytest.raises(ValueError):
        activity.save_bridge_session({"ok": True}, "https://voicehook.ai")
    with pytest.raises(ValueError):
        activity.save_bridge_session({"session": "s"}, "ftp://evil")


def test_cli_bridge_session_save_and_clear(state, tmp_path, capsys):
    ans = tmp_path / "join.json"
    ans.write_text(json.dumps({"ok": True, "session": "sess-xyz"}))
    with pytest.raises(SystemExit) as e:
        cli.main(["bridge-session", "--save", str(ans), "--base", "https://voicehook.ai"])
    assert e.value.code == 0
    assert "sess-xyz" not in capsys.readouterr().out      # never echo the token
    assert activity.load_bridge_session() == {"base": "https://voicehook.ai", "session": "sess-xyz"}
    with pytest.raises(SystemExit) as e:
        cli.main(["bridge-session", "--clear"])
    assert e.value.code == 0 and activity.load_bridge_session() is None


# ----- hook -> bridge POST ----------------------------------------------------------
def test_hook_posts_description_to_bridge(state, posts):
    _save_session()
    assert activity.pre_tool_use(_event(tool_use_id="t1", tool_input={
        "command": "deploy.sh", "description": "Sending deploy to JEV"}), now=1000.0) is True
    assert posts.sent == [("https://voicehook.ai", "sess-abc", "Sending deploy to JEV")]
    # Post of the same call: deduped, no second POST
    assert activity.post_tool_use(_event("PostToolUse", tool_use_id="t1", tool_input={
        "description": "Sending deploy to JEV"}), now=1010.0) is False
    assert len(posts.sent) == 1


def test_hook_bridge_text_for_file_tool_is_basename(state, posts):
    _save_session()
    activity.pre_tool_use(_event(tool_name="Read", tool_input={"file_path": "/home/x/p/relay.py"}),
                          now=1000.0)
    assert posts.sent[-1][2] == "Read relay.py"


def test_no_session_file_no_post(state, posts):
    assert activity.pre_tool_use(_event(tool_input={"description": "x"}), now=1000.0) is False
    assert posts.sent == [] and posts.spawned == []


def test_rate_limit_latest_wins_via_flusher(state, posts):
    _save_session()
    activity.pre_tool_use(_event(tool_input={"description": "eins"}), now=1000.0)
    activity.pre_tool_use(_event(tool_input={"description": "zwei"}), now=1001.0)
    activity.pre_tool_use(_event(tool_input={"description": "drei"}), now=1002.0)
    assert [s[2] for s in posts.sent] == ["eins"]
    assert posts.spawned == [pytest.approx(4.0)]          # one flusher, window ends at 1005
    activity.bridge_flush(now=1005.0, sleep=lambda s: None)
    assert [s[2] for s in posts.sent] == ["eins", "drei"]  # latest wins
    activity.bridge_flush(now=1006.0, sleep=lambda s: None)
    assert len(posts.sent) == 2                            # nothing pending
    activity.pre_tool_use(_event(tool_input={"description": "vier"}), now=1011.0)
    assert [s[2] for s in posts.sent][-1] == "vier"


def test_post_error_and_timeout_never_raise_and_exit_0(state, monkeypatch):
    _save_session()

    def boom(*a, **kw):
        raise TimeoutError("2 s")
    monkeypatch.setattr(activity, "_bridge_post", boom)
    monkeypatch.setattr(activity, "_spawn_flusher", lambda d: None)
    assert activity.pre_tool_use(_event(tool_input={"description": "x"}), now=1000.0) in (True, False)
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO(_event(tool_input={"description": "y"})))
    assert activity.main(["pre-tool-use"]) == 0


def test_real_post_against_dead_port_is_fast_and_silent(state):
    activity.save_bridge_session({"session": "s"}, "http://127.0.0.1:9")
    import time
    t0 = time.monotonic()
    assert activity._bridge_post("http://127.0.0.1:9", "s", "x") is None
    assert time.monotonic() - t0 < activity.BRIDGE_TIMEOUT_S + 1


@pytest.mark.parametrize("status", [401, 404, 410])
def test_session_gone_removes_file(state, monkeypatch, status):
    _save_session()
    monkeypatch.setattr(activity, "_bridge_post", lambda *a, **kw: status)
    monkeypatch.setattr(activity, "_spawn_flusher", lambda d: None)
    activity.pre_tool_use(_event(tool_input={"description": "x"}), now=1000.0)
    assert activity.load_bridge_session() is None


@pytest.mark.parametrize("status", [200, 429, 500, None])
def test_other_answers_keep_session_file(state, monkeypatch, status):
    _save_session()
    monkeypatch.setattr(activity, "_bridge_post", lambda *a, **kw: status)
    monkeypatch.setattr(activity, "_spawn_flusher", lambda d: None)
    activity.pre_tool_use(_event(tool_input={"description": "x"}), now=1000.0)
    assert activity.load_bridge_session() is not None


def test_hook_subprocess_exit_0_with_unreachable_bridge(state, tmp_path):
    activity.save_bridge_session({"session": "s"}, "http://127.0.0.1:9")
    env = {**os.environ, activity.STATE_ENV: str(state), "VOICEHOOK_AGENT_HOME": str(tmp_path / "vh"),
           "PYTHONPATH": str(Path(cli.__file__).parents[1])}
    r = subprocess.run([sys.executable, "-m", "voicehook_agent.activity", "pre-tool-use"],
                       input=_event(tool_input={"description": "x"}), capture_output=True,
                       text=True, env=env, timeout=15, check=False)
    assert r.returncode == 0 and r.stdout == ""


# ----- pointer files: join with its own VOICEHOOK_AGENT_HOME --------------------------
def test_hook_finds_join_under_other_home_via_pointer(state, tmp_path, monkeypatch):
    other = tmp_path / "vh-wrapper"
    monkeypatch.setenv("VOICEHOOK_AGENT_HOME", str(other))
    d = vsession.session_dir("blau-tiger-AB12", "claude-x")
    d.mkdir(parents=True)
    vsession.write_info(d, {"pid": os.getpid()})
    activity.write_pointer(d)
    monkeypatch.setenv("VOICEHOOK_AGENT_HOME", str(tmp_path / "default-home"))  # the hook's view
    assert activity.target_dir() == d
    assert activity.post_tool_use(_event("PostToolUse", tool_input={"description": "Tests"})) is True
    assert (d / activity.ACTIVITY_NAME).read_text().endswith(" Bash: Tests\n")
    p = activity.pointer_path()
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    activity.remove_pointer(d)
    assert not p.exists() and activity.target_dir() is None


def test_pointer_of_dead_pid_ignored_and_two_joins_write_nothing(state, tmp_path, monkeypatch):
    monkeypatch.setenv("VOICEHOOK_AGENT_HOME", str(tmp_path / "a"))
    a = vsession.session_dir("r1", "x")
    a.mkdir(parents=True)
    vsession.write_info(a, {"pid": os.getpid()})
    activity.write_pointer(a)
    # a stale pointer of a dead join
    dead = tmp_path / "dead"
    dead.mkdir()
    (state / "joins" / "999999.json").write_text(json.dumps({"dir": str(dead), "pid": 2 ** 22 + 7}))
    monkeypatch.setenv("VOICEHOOK_AGENT_HOME", str(tmp_path / "hook"))
    assert activity.target_dir() == a
    # a second live join under the default home -> ambiguous -> nothing
    monkeypatch.setenv("VOICEHOOK_AGENT_HOME", str(tmp_path / "hook"))
    b = vsession.session_dir("r2", "y")
    b.mkdir(parents=True)
    vsession.write_info(b, {"pid": os.getpid()})
    assert activity.target_dir() is None


# ----- activity_now / board_now in `next` ---------------------------------------------
class _Local:
    def __init__(self):
        self.n = 0

    async def publish_data(self, data, reliable=True, topic=""):
        self.n += 1


def test_next_carries_activity_now_and_board_now(monkeypatch, tmp_path):
    t = {"now": 1000.0}
    monkeypatch.setattr(relay.time, "monotonic", lambda: t["now"])
    log = tmp_path / activity.ACTIVITY_NAME

    async def go():
        ctl = cli._Control("blau-tiger", "claude-x", relay.SayTracker(), relay.EchoSuppressor(False), 0.0)
        ctl.attach(SimpleNamespace(local_participant=_Local(), remote_participants={}))
        ctl.activity_clock = lambda: t["now"]
        ctl.activity_path = log
        empty = await cli._control_handler(ctl, {"cmd": "next", "timeout": 0})
        await cli._control_handler(ctl, {"cmd": "board", "board": BOARD})
        # what the activity loop does on a change: publish -> remembered as sent
        assert await cli._publish_activity(ctl, ["17:00:00 Bash: eins",
                                                 "17:00:00 Bash: Sending deploy to JEV"])
        t["now"] += 12.0
        full = await cli._control_handler(ctl, {"cmd": "next", "timeout": 0})
        return empty, full
    empty, full = asyncio.run(go())
    assert empty["activity_now"] is None and empty["board_now"] is None
    assert full["activity_now"] == {"text": "Bash: Sending deploy to JEV", "age_s": 12.0}
    assert full["board_now"] == {"doing": "baut den Fix, ETA 5 min", "age_s": 12.0}
