"""0.13.0: the CLI keeps the activity log filled (Oliver 08.10.2026).

* PreToolUse hook: the line is in activity.log WHILE the tool runs; Post skips a
  tool_use_id that Pre already logged (dedupe), Post alone still works.
* Tools without `description` (Read/Edit/Write): the file's basename, never a path.
* `voicehook-agent activity "<text>"`: manual line `HH:MM:SS note: <text>` for agents
  without the hook (scrubbed + capped like the hook).
* `next` adds activity_due + activity_age_s + activity_hint when work is in progress and
  no new line came for N s (default 60, --activity-due / VOICEHOOK_ACTIVITY_DUE, 0 = off);
  silent while hook lines arrive; at most one hint per N s.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from voicehook_agent import activity, cli, relay
from voicehook_agent import session as vsession

LINE_RX = re.compile(r"^\d\d:\d\d:\d\d [A-Za-z0-9_.:\-]{1,40}(: .+)?$")
BOARD = {"doing": "baut den Fix, ETA 5 min", "open": ["Tests"], "done": []}
DONE_ONLY = {"doing": "", "open": [], "done": ["Fix live"]}


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("VOICEHOOK_AGENT_HOME", str(tmp_path / "vh"))
    monkeypatch.delenv("VOICEHOOK_SESSION", raising=False)
    monkeypatch.delenv(activity.ACTIVITY_DUE_ENV, raising=False)
    return tmp_path / "vh"


def _join_dir(slug="blau-tiger-fluss-AB12", ident="claude-x") -> Path:
    d = vsession.session_dir(slug, ident)
    d.mkdir(parents=True)
    vsession.write_info(d, {"pid": os.getpid(), "room": slug, "identity": ident})
    return d


def _event(phase="PostToolUse", **kw) -> str:
    ev = {"session_id": "s1", "cwd": "/home/x/secret-project", "hook_event_name": phase,
          "tool_name": "Bash", "tool_input": {}}
    if phase == "PostToolUse":
        ev["tool_response"] = {}
    ev.update(kw)
    return json.dumps(ev)


def _lines(d: Path) -> list[str]:
    p = d / activity.ACTIVITY_NAME
    return p.read_text().splitlines() if p.exists() else []


# ----- PreToolUse + dedupe ----------------------------------------------------------
def test_pre_tool_use_writes_line_while_tool_runs(home):
    d = _join_dir()
    now = time.mktime((2026, 10, 8, 17, 12, 3, 0, 0, -1))
    ok = activity.pre_tool_use(_event("PreToolUse", tool_use_id="toolu_1",
                                      tool_input={"command": "sleep 300",
                                                  "description": "Sending deploy to JEV"}), now=now)
    assert ok
    assert _lines(d) == ["17:12:03 Bash: Sending deploy to JEV"]


def test_post_skips_tool_use_id_already_logged_by_pre(home):
    d = _join_dir()
    pre = {"tool_use_id": "toolu_1", "tool_input": {"description": "Run the tests"}}
    assert activity.pre_tool_use(_event("PreToolUse", **pre)) is True
    assert activity.post_tool_use(_event("PostToolUse", **pre)) is False
    assert len(_lines(d)) == 1
    # the ids file is private and never published
    ids = d / activity.IDS_NAME
    assert ids.exists() and oct(os.stat(ids).st_mode & 0o777) == "0o600"
    assert "toolu_1" not in "".join(activity.read_tail(d / activity.ACTIVITY_NAME))


def test_post_without_pre_still_logs_and_parallel_ids_dedupe(home):
    d = _join_dir()
    activity.pre_tool_use(_event("PreToolUse", tool_use_id="a", tool_input={"description": "eins"}))
    activity.pre_tool_use(_event("PreToolUse", tool_use_id="b", tool_input={"description": "zwei"}))
    assert activity.post_tool_use(_event(tool_use_id="b", tool_input={"description": "zwei"})) is False
    assert activity.post_tool_use(_event(tool_use_id="a", tool_input={"description": "eins"})) is False
    assert activity.post_tool_use(_event(tool_use_id="c", tool_input={"description": "drei"})) is True
    assert activity.post_tool_use(_event(tool_input={"description": "ohne id"})) is True
    assert [ln.split(" ", 1)[1] for ln in _lines(d)] == [
        "Bash: eins", "Bash: zwei", "Bash: drei", "Bash: ohne id"]


def test_hook_cli_pre_tool_use_exit_0_silent(home):
    import subprocess
    import sys
    d = _join_dir()
    env = {**os.environ, "VOICEHOOK_AGENT_HOME": str(home),
           "PYTHONPATH": str(Path(cli.__file__).parents[1])}
    env.pop("VOICEHOOK_SESSION", None)
    for argv in (["-m", "voicehook_agent.activity", "pre-tool-use"],
                 ["-m", "voicehook_agent.cli", "hook", "pre-tool-use"]):
        r = subprocess.run([sys.executable, *argv],
                           input=_event("PreToolUse", tool_input={"description": "Build"}),
                           capture_output=True, text=True, env=env, timeout=30, check=False)
        assert r.returncode == 0 and r.stdout == ""
    assert len(_lines(d)) == 2


# ----- basename for tools without description ---------------------------------------
@pytest.mark.parametrize("tool,key", [("Read", "file_path"), ("Edit", "file_path"),
                                      ("Write", "file_path"), ("MultiEdit", "file_path"),
                                      ("NotebookEdit", "notebook_path")])
def test_file_tools_log_basename_only(home, tool, key):
    d = _join_dir()
    activity.post_tool_use(_event(tool_name=tool, tool_input={
        key: "/home/x/secret-project/src/relay.py", "old_string": "a = 1", "content": "BODY"}))
    (line,) = _lines(d)
    assert LINE_RX.match(line) and line.endswith(f" {tool}: relay.py")
    for leak in ("/home", "secret-project", "src/", "a = 1", "BODY"):
        assert leak not in line


def test_windows_path_and_secret_filename_scrubbed(home):
    d = _join_dir()
    activity.post_tool_use(_event(tool_name="Read", tool_input={"file_path": r"C:\Users\x\proj\app.ts"}))
    activity.post_tool_use(_event(tool_name="Read", tool_input={
        "file_path": "/tmp/ghp_abcdefghijklmnopqrstuvwxyz0123456789.txt"}))
    l1, l2 = _lines(d)
    assert l1.endswith(" Read: app.ts") and "Users" not in l1
    assert "ghp_" not in l2 and "[redacted]" in l2


@pytest.mark.parametrize("tool,inp", [
    ("Grep", {"pattern": "token=hunter2", "path": "/home/x/secret-project"}),
    ("Glob", {"pattern": "**/*.env", "path": "/home/x"})])
def test_grep_glob_only_tool_name(home, tool, inp):
    d = _join_dir()
    activity.post_tool_use(_event(tool_name=tool, tool_input=inp))
    (line,) = _lines(d)
    assert re.match(rf"^\d\d:\d\d:\d\d {tool}$", line)


def test_description_wins_over_basename(home):
    d = _join_dir()
    activity.post_tool_use(_event(tool_name="Read", tool_input={
        "file_path": "/a/b.py", "description": "Check config"}))
    assert _lines(d)[0].endswith(" Read: Check config")


# ----- installer: Pre + Post, idempotent upgrade --------------------------------------
def test_snippet_has_pre_and_post():
    s = activity.snippet()["hooks"]
    assert s["PreToolUse"][0]["hooks"][0]["command"] == "voicehook-agent-hook pre-tool-use || true"
    assert s["PostToolUse"][0]["hooks"][0]["command"] == "voicehook-agent-hook post-tool-use"


def test_install_upgrades_post_only_install_idempotently(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"model": "opus", "hooks": {"PostToolUse": [
        {"matcher": "*", "hooks": [{"type": "command",
                                    "command": "voicehook-agent hook post-tool-use", "timeout": 5}]}]}}))
    rc, msg = activity.install(p)
    assert rc == 0 and "PreToolUse" in msg
    data = json.loads(p.read_text())
    assert len(data["hooks"]["PostToolUse"]) == 1          # old post entry kept, not doubled
    assert data["hooks"]["PreToolUse"] == [activity.hook_entry("PreToolUse")]
    first = p.read_text()
    rc, msg = activity.install(p)
    assert rc == 0 and "already installed" in msg and p.read_text() == first


# ----- manual entry: `voicehook-agent activity "<text>"` ------------------------------
def test_note_line_scrubbed_and_capped():
    now = time.mktime((2026, 10, 8, 9, 5, 7, 0, 0, -1))
    assert activity.note_line("Sending deploy to JEV", now) == "09:05:07 note: Sending deploy to JEV"
    line = activity.note_line("Key token=hunter2 und\nsk_live_51Habcdefghijklmnop " + "x" * 300, now)
    assert "hunter2" not in line and "sk_live" not in line and "\n" not in line
    assert line.startswith("09:05:07 note: Key token=[redacted] und [redacted]")
    assert len(line.split(": ", 1)[1]) <= activity.DESC_MAX
    assert activity.scrub_line(line) == line
    with pytest.raises(ValueError):
        activity.note_line("   \n ", now)


def test_activity_command_appends_note_via_join(tmp_path, monkeypatch, capsys):
    """CLI -> control socket -> the join appends to its own activity.log."""
    ctl = cli._Control("blau-tiger", "claude-x", relay.SayTracker(), relay.EchoSuppressor(False), 0.0)
    ctl.activity_path = tmp_path / activity.ACTIVITY_NAME
    seen = {}

    def fake_request(sock, req, timeout=None):
        seen["req"] = req
        return asyncio.run(cli._control_handler(ctl, req))
    monkeypatch.setattr(vsession, "resolve_socket", lambda s, wait=0.0: tmp_path / "control.sock")
    monkeypatch.setattr(vsession, "request", fake_request)
    with pytest.raises(SystemExit) as e:
        cli.main(["activity", "Running", "migrations", "password=geheim"])
    assert e.value.code == 0
    out = json.loads(capsys.readouterr().out)
    assert seen["req"] == {"cmd": "activity", "text": "Running migrations password=geheim"}
    assert out["ok"] is True and out["type"] == "activity"
    (line,) = _lines(tmp_path)
    assert re.match(r"^\d\d:\d\d:\d\d note: Running migrations password=\[redacted\]$", line)
    assert out["line"] == line
    # empty text: refused before connecting
    seen.clear()
    with pytest.raises(SystemExit) as e:
        cli.main(["activity", "  "])
    assert e.value.code == 1 and "req" not in seen
    assert asyncio.run(cli._control_handler(ctl, {"cmd": "activity", "text": ""}))["ok"] is False


def test_cli_activity_command_without_join_exits_3(home, capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["activity", "hallo", "--wait", "0"])
    assert e.value.code == 3
    assert json.loads(capsys.readouterr().out)["type"] == "no-session"


# ----- ActivityDue -----------------------------------------------------------------
def test_due_after_n_seconds_without_line_while_working():
    a = activity.ActivityDue(due_s=60.0, now=0.0)
    assert a.check(59.0, working=True) == {}
    h = a.check(61.0, working=True)
    assert h["activity_due"] is True and h["activity_age_s"] == 61.0
    assert 'voicehook-agent activity "' in h["activity_hint"]
    assert "1:1" in h["activity_hint"]
    assert "hint" not in h                                   # status_due owns `hint`


def test_silent_with_fresh_line_and_age_counts_from_newest_line():
    a = activity.ActivityDue(due_s=60.0, now=0.0)
    a.observe(["10:00:00 note: eins"], 50.0)
    assert a.check(100.0, working=True) == {}                # 50 s old
    h = a.check(111.0, working=True)
    assert h["activity_age_s"] == 61.0


def test_silent_without_work_in_progress():
    a = activity.ActivityDue(due_s=60.0, now=0.0)
    assert a.check(10_000.0, working=False) == {}


def test_silent_while_hook_lines_arrive():
    a = activity.ActivityDue(due_s=60.0, now=0.0)
    a.observe(["10:00:00 Bash: Wait for next turn"], 1.0)
    assert a.check(120.0, working=True) == {}                # hook active: no nagging
    assert a.check(1.0 + activity.HOOK_QUIET_S + 1, working=True)["activity_due"] is True


def test_rate_limit_one_hint_per_n_seconds():
    a = activity.ActivityDue(due_s=60.0, now=0.0)
    assert a.check(61.0, working=True)["activity_due"] is True
    assert a.check(62.0, working=True) == {}
    assert a.check(120.0, working=True) == {}
    assert a.check(121.0, working=True)["activity_due"] is True


def test_zero_switches_off_and_env_flag(monkeypatch):
    monkeypatch.delenv(activity.ACTIVITY_DUE_ENV, raising=False)
    assert activity.activity_due_seconds() == 60.0
    monkeypatch.setenv(activity.ACTIVITY_DUE_ENV, "20")
    assert activity.activity_due_seconds() == 20.0
    assert activity.activity_due_seconds(90) == 90.0         # flag wins
    monkeypatch.setenv(activity.ACTIVITY_DUE_ENV, "0")
    a = activity.ActivityDue(due_s=activity.activity_due_seconds(), now=0.0)
    assert a.check(10_000.0, working=True) == {}


def test_turnclock_working_rule():
    c = relay.TurnClock(now=0.0)
    assert c.working(now=1.0) is False
    c.board(now=10.0, board=BOARD)
    assert c.working(now=10_000.0) is True                  # doing/open set
    c.board(now=20.0, board=DONE_ONLY)
    assert c.working(now=20.0 + relay.WORK_WINDOW_S - 1) is True   # board sent recently
    assert c.working(now=20.0 + relay.WORK_WINDOW_S + 1) is False
    c.said(now=1000.0, text="Moment, ich schaue nach.")
    assert c.working(now=1000.0 + relay.WORK_WINDOW_S - 1) is True  # spoke recently


# ----- `next` end to end ----------------------------------------------------------------
class _Local:
    async def publish_data(self, data, reliable=True, topic=""):
        pass


def _ctl(t, log=None) -> cli._Control:
    ctl = cli._Control("blau-tiger", "claude-x", relay.SayTracker(), relay.EchoSuppressor(False), 0.0)
    ctl.attach(SimpleNamespace(local_participant=_Local(), remote_participants={}))
    ctl.activity_clock = lambda: t["now"]
    ctl.activity_due = activity.ActivityDue(due_s=60.0, now=t["now"])
    ctl.activity_path = log
    return ctl


def test_next_carries_activity_due_then_silent_after_note(monkeypatch, tmp_path):
    t = {"now": 1000.0}
    monkeypatch.setattr(relay.time, "monotonic", lambda: t["now"])
    log = tmp_path / activity.ACTIVITY_NAME

    async def go():
        ctl = _ctl(t, log)
        await cli._control_handler(ctl, {"cmd": "board", "board": BOARD})
        fresh = await cli._control_handler(ctl, {"cmd": "next", "timeout": 0})
        t["now"] += 61.0
        old = await cli._control_handler(ctl, {"cmd": "next", "timeout": 0})
        activity.append_line(tmp_path, activity.note_line("Running migrations"))
        t["now"] += 1.0
        after = await cli._control_handler(ctl, {"cmd": "next", "timeout": 0})
        return fresh, old, after
    fresh, old, after = asyncio.run(go())
    assert "activity_due" not in fresh
    assert old["activity_due"] is True and old["activity_age_s"] == 61.0
    assert "voicehook-agent activity" in old["activity_hint"]
    assert "status_due" in old and old["hint"] == relay.STATUS_HINTS["stale"]  # untouched
    assert "activity_due" not in after


def test_next_without_work_never_activity_due(monkeypatch):
    t = {"now": 1000.0}
    monkeypatch.setattr(relay.time, "monotonic", lambda: t["now"])

    async def go():
        ctl = _ctl(t)
        t["now"] += 10_000.0
        return await cli._control_handler(ctl, {"cmd": "next", "timeout": 0})
    assert "activity_due" not in asyncio.run(go())


def test_activity_loop_feeds_due_tracker(tmp_path):
    log = tmp_path / activity.ACTIVITY_NAME
    t = {"now": 0.0}

    async def run():
        ctl = _ctl(t)
        ctl.activity_interval = 0.01
        task = asyncio.create_task(cli._activity_loop(ctl, log))
        log.write_text("17:00:00 Bash: eins\n")
        t["now"] = 5.0
        await asyncio.sleep(0.05)
        ctl.quit.set()
        await task
        return ctl.activity_due
    due = asyncio.run(run())
    assert due.line_at == 5.0 and due.hook_at == 5.0


def test_join_flag_parses(monkeypatch):
    seen = {}

    async def fake_join(*a, **kw):
        seen.update(kw)
        return 0
    monkeypatch.setattr(cli, "_join", fake_join)
    with pytest.raises(SystemExit):
        cli.main(["join", "https://voicehook.ai/r/blau-tiger-hase-AB12", "--name", "Claude",
                  "--model", "opus", "--activity-due", "30"])
    assert seen["activity_due"] == 30.0


def test_join_help_mentions_activity_log(capsys):
    with pytest.raises(SystemExit):
        cli.main(["join", "--help"])
    out = capsys.readouterr().out
    assert "--activity-due" in out and "VOICEHOOK_ACTIVITY_DUE" in out
    assert "voicehook-agent activity" in out and "hook install" in out


def test_pre_hook_command_never_blocks_with_old_binary(tmp_path):
    """PreToolUse exit 2 blocks the tool in Claude Code; an old voicehook-agent-hook
    (<0.13) exits 2 on the unknown `pre-tool-use`. The installed command must exit 0."""
    import subprocess
    old = tmp_path / "voicehook-agent-hook"
    old.write_text("#!/bin/sh\necho 'usage: ...' >&2\nexit 2\n")
    old.chmod(0o755)
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ.get('PATH', '')}"}
    r = subprocess.run(activity.HOOK_COMMAND_PRE, shell=True, input="{}", capture_output=True,
                       text=True, env=env, timeout=10, check=False)
    assert r.returncode == 0 and r.stdout == ""
