"""0.14.4: the activity hook writes only for the Claude Code session that owns the join.

The hook is usually installed globally (~/.claude/settings.json, matcher "*"), so it runs
in EVERY Claude Code session on the machine. Before 0.14.4 it took "the one live join"
without asking whose join it was: tool calls of unrelated sessions (other projects)
showed up as activity lines in someone else's call.

Now `join` (and `bridge-session --save`) record the owner from the environment Claude
Code gives its Bash subprocesses (CLAUDE_CODE_SESSION_ID + CLAUDE_PID), but only when
CLAUDE_PID's process tree provably contains the process (not an environment inherited
through a tmux server of another session). The hook writes only when the hook event's
`session_id` matches the owner's (no pid-only owner). No owner, no match or ambiguity:
0 lines (fail closed). A pending bridge line goes only to the bridge session it was
queued for. VOICEHOOK_SESSION=<slug>/<identity> stays an explicit override for a live join.
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from voicehook_agent import activity, cli
from voicehook_agent import session as vsession

SID_A = "aaaaaaaa-0000-4000-8000-00000000000a"
SID_B = "bbbbbbbb-0000-4000-8000-00000000000b"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("VOICEHOOK_AGENT_HOME", str(tmp_path / "vh"))
    monkeypatch.delenv("VOICEHOOK_SESSION", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_PID", raising=False)
    return tmp_path / "vh"


def _join(slug="blau-tiger-fluss-AB12", ident="claude-x", owner=None) -> Path:
    d = vsession.session_dir(slug, ident)
    d.mkdir(parents=True)
    info = {"pid": os.getpid(), "room": slug, "identity": ident}
    if owner is not None:
        info["owner"] = owner
    vsession.write_info(d, info)
    return d


def _event(session_id=SID_A, **kw) -> str:
    ev = {"session_id": session_id, "hook_event_name": "PreToolUse", "tool_name": "Bash",
          "tool_input": {"description": "Run the tests"}, "tool_use_id": f"toolu_{session_id[:8]}"}
    ev.update(kw)
    return json.dumps(ev)


def _lines(d: Path) -> list[str]:
    p = d / activity.ACTIVITY_NAME
    return p.read_text().splitlines() if p.exists() else []


# ----- repro: two sessions, one join -------------------------------------------------
def test_other_session_writes_nothing_owner_session_writes(home):
    d = _join(owner={"session_id": SID_A})
    assert activity.pre_tool_use(_event(SID_B)) is False
    assert _lines(d) == []
    assert activity.pre_tool_use(_event(SID_A)) is True
    assert len(_lines(d)) == 1 and _lines(d)[0].endswith(" Bash: Run the tests")


def test_join_without_owner_writes_nothing(home):
    """A join started before the update (or outside Claude Code) has no owner."""
    d = _join()
    assert activity.pre_tool_use(_event(SID_A)) is False
    assert activity.post_tool_use(_event(SID_A, hook_event_name="PostToolUse")) is False
    assert _lines(d) == []


def test_event_without_session_id_writes_nothing(home):
    d = _join(owner={"session_id": SID_A})
    ev = json.loads(_event(SID_A))
    del ev["session_id"]
    assert activity.pre_tool_use(json.dumps(ev)) is False
    assert _lines(d) == []


def test_session_id_from_env_when_event_has_none(home, monkeypatch):
    d = _join(owner={"session_id": SID_A})
    ev = json.loads(_event(SID_A))
    del ev["session_id"]
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID_A)
    assert activity.pre_tool_use(json.dumps(ev)) is True
    assert len(_lines(d)) == 1


def test_event_session_id_wins_over_env(home, monkeypatch):
    d = _join(owner={"session_id": SID_A})
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID_A)
    assert activity.pre_tool_use(_event(SID_B)) is False
    assert _lines(d) == []


def test_several_joins_only_own_one(home):
    a = _join(ident="claude-a", owner={"session_id": SID_A})
    b = _join(slug="rot-fuchs-berg-CD34", ident="claude-b", owner={"session_id": SID_B})
    legacy = _join(slug="gelb-wal-see-EF56", ident="claude-c")
    assert activity.pre_tool_use(_event(SID_A)) is True
    assert activity.pre_tool_use(_event(SID_B, tool_input={"description": "Build"})) is True
    assert activity.pre_tool_use(_event("cccccccc-0000-4000-8000-00000000000c")) is False
    assert [ln.split(" ", 1)[1] for ln in _lines(a)] == ["Bash: Run the tests"]
    assert [ln.split(" ", 1)[1] for ln in _lines(b)] == ["Bash: Build"]
    assert _lines(legacy) == []


def test_two_joins_of_the_same_session_stay_ambiguous(home):
    a = _join(ident="claude-a", owner={"session_id": SID_A})
    b = _join(slug="rot-fuchs-berg-CD34", ident="claude-b", owner={"session_id": SID_A})
    assert activity.pre_tool_use(_event(SID_A)) is False
    assert _lines(a) == [] and _lines(b) == []


def test_subagent_of_the_owner_session_writes(home):
    """Subagent tool calls carry the parent's session_id plus agent_id/agent_type."""
    d = _join(owner={"session_id": SID_A})
    assert activity.pre_tool_use(_event(SID_A, agent_id="agent-1", agent_type="Explore")) is True
    assert activity.pre_tool_use(_event(SID_B, agent_id="agent-2", agent_type="Explore")) is False
    assert len(_lines(d)) == 1


def test_pid_only_owner_writes_nothing(home, monkeypatch):
    """No pid fallback: Claude Code always sets CLAUDE_CODE_SESSION_ID next to CLAUDE_PID,
    and a pid alone cannot be checked against the event (pid reuse, /clear)."""
    d = _join(owner={"claude_pid": 4242})
    monkeypatch.setenv("CLAUDE_PID", "4242")       # e.g. an unrelated Claude reused pid 4242
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID_B)
    assert activity.pre_tool_use(_event(SID_B)) is False
    ev = json.loads(_event(SID_B))
    del ev["session_id"]
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
    assert activity.pre_tool_use(json.dumps(ev)) is False   # neither side has a session id
    assert _lines(d) == []


def test_session_id_mismatch_is_not_rescued_by_same_pid(home, monkeypatch):
    """Same Claude process, other session id (e.g. after /clear): fail closed."""
    d = _join(owner={"session_id": SID_A, "claude_pid": 4242})
    monkeypatch.setenv("CLAUDE_PID", "4242")
    assert activity.pre_tool_use(_event(SID_B)) is False
    assert _lines(d) == []


@pytest.mark.parametrize("owner", [{}, {"session_id": ""}, {"session_id": 5}, "x",
                                   {"claude_pid": 0}, {"claude_pid": "abc"}])
def test_malformed_owner_writes_nothing(home, monkeypatch, owner):
    monkeypatch.setenv("CLAUDE_PID", "0")
    d = _join(owner=owner)
    assert activity.pre_tool_use(_event(SID_A)) is False
    assert _lines(d) == []


# ----- pointer files (0.13.0) ---------------------------------------------------------
def test_pointer_join_only_for_its_owner(home, tmp_path, monkeypatch):
    monkeypatch.setenv("VOICEHOOK_AGENT_HOME", str(tmp_path / "vh-wrapper"))
    d = _join(owner={"session_id": SID_A})
    activity.write_pointer(d)
    monkeypatch.setenv("VOICEHOOK_AGENT_HOME", str(tmp_path / "default-home"))  # the hook's view
    assert activity.pre_tool_use(_event(SID_B)) is False
    assert activity.pre_tool_use(_event(SID_A)) is True
    assert len(_lines(d)) == 1


def test_pointer_join_without_owner_writes_nothing(home, tmp_path, monkeypatch):
    monkeypatch.setenv("VOICEHOOK_AGENT_HOME", str(tmp_path / "vh-wrapper"))
    d = _join()
    activity.write_pointer(d)
    monkeypatch.setenv("VOICEHOOK_AGENT_HOME", str(tmp_path / "default-home"))
    assert activity.pre_tool_use(_event(SID_A)) is False
    assert _lines(d) == []


# ----- VOICEHOOK_SESSION stays an explicit override -----------------------------------
def test_voicehook_session_env_unchanged(home, monkeypatch):
    a = _join(ident="claude-a", owner={"session_id": SID_A})
    b = _join(slug="rot-fuchs-berg-CD34", ident="claude-b")
    monkeypatch.setenv("VOICEHOOK_SESSION", "rot-fuchs-berg-CD34/claude-b")
    assert activity.pre_tool_use(_event(SID_B)) is True
    assert _lines(a) == [] and len(_lines(b)) == 1


def _dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


@pytest.mark.parametrize("info", [{"pid": "dead"}, {"pid": 0}, {"pid": "x"}, {}, None])
def test_voicehook_session_needs_a_live_join(home, monkeypatch, info):
    """A leftover dir of an ended join (or a stale global export) gets nothing."""
    d = vsession.session_dir("blau-tiger-fluss-AB12", "claude-x")
    d.mkdir(parents=True)
    if info is not None:
        if info.get("pid") == "dead":
            info = {"pid": _dead_pid()}
        vsession.write_info(d, {**info, "owner": {"session_id": SID_A}})
    monkeypatch.setenv("VOICEHOOK_SESSION", "blau-tiger-fluss-AB12/claude-x")
    assert activity.pre_tool_use(_event(SID_B)) is False
    assert activity.pre_tool_use(_event(SID_A)) is False
    assert _lines(d) == []


# ----- owner capture ------------------------------------------------------------------
def test_owner_from_env(monkeypatch):
    assert activity.resolve_owner() == ({}, None)          # outside Claude Code: silent
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", f"  {SID_A} ")
    monkeypatch.setenv("CLAUDE_PID", str(os.getppid()))    # an ancestor of this process
    assert activity.owner_from_env() == {"session_id": SID_A, "claude_pid": os.getppid()}
    monkeypatch.setenv("CLAUDE_PID", "nope")
    owner, why = activity.resolve_owner()
    assert owner == {} and "CLAUDE_PID" in why             # no pid-only / sid-only owner
    monkeypatch.setenv("CLAUDE_PID", str(os.getppid()))
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
    owner, why = activity.resolve_owner()
    assert owner == {} and "CLAUDE_CODE_SESSION_ID" in why


def test_owner_needs_claude_pid_in_the_process_tree(monkeypatch):
    """A live CLAUDE_PID that is neither an ancestor nor above an orphaned process:
    the environment was inherited from somewhere else -> no owner."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID_B)
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        monkeypatch.setenv("CLAUDE_PID", str(other.pid))    # a child, not an ancestor
        owner, why = activity.resolve_owner()
        assert owner == {} and "not an ancestor" in why
    finally:
        other.kill()
        other.wait()
    monkeypatch.setenv("CLAUDE_PID", str(other.pid))        # gone
    owner, why = activity.resolve_owner()
    assert owner == {} and "not running" in why


def test_owner_rejects_foreign_tmux_server_in_env(monkeypatch):
    """$TMUX names a server outside Claude Code's process tree: its global environment
    (CLAUDE_* included) came from whoever started it."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID_A)
    monkeypatch.setenv("CLAUDE_PID", str(os.getppid()))
    srv = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        monkeypatch.setenv("TMUX", f"/tmp/tmux-1000/default,{srv.pid},0")
        owner, why = activity.resolve_owner()
        assert owner == {} and "tmux/screen server" in why
        monkeypatch.delenv("TMUX")
        monkeypatch.setenv("STY", f"{srv.pid}.pts-0.host")
        assert activity.resolve_owner()[0] == {}
        monkeypatch.setenv("STY", f"{os.getppid()}.pts-0.host")   # Claude's own screen
        assert activity.owner_from_env() == {"session_id": SID_A, "claude_pid": os.getppid()}
    finally:
        srv.kill()
        srv.wait()


def test_owner_without_proc_is_none(monkeypatch, tmp_path):
    monkeypatch.setattr(activity, "PROC_ROOT", tmp_path / "no-proc")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID_A)
    monkeypatch.setenv("CLAUDE_PID", str(os.getppid()))
    owner, why = activity.resolve_owner()
    assert owner == {} and "/proc" in why


_PRINT_OWNER = ("import json, sys, time; time.sleep(float(sys.argv[2])); "
                "from voicehook_agent import activity; o, w = activity.resolve_owner(); "
                "open(sys.argv[1], 'w').write(json.dumps({'owner': o, 'why': w}))")


def _wait_json(path: Path, timeout: float = 15.0) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        if path.exists() and path.stat().st_size:
            return json.loads(path.read_text())
        time.sleep(0.05)
    raise AssertionError(f"{path} not written")


def _owner_env(**extra) -> dict:
    env = {**os.environ, "PYTHONPATH": str(Path(cli.__file__).parents[1]), **extra}
    for k in ("TMUX", "STY", "TMUX_PANE"):
        env.pop(k, None)
    return env


def test_owner_of_background_join_with_ampersand(tmp_path):
    """The documented `voicehook-agent join ... &` from the Bash tool: the shell exits
    at once and the join is re-parented to init (or a subreaper above Claude Code).
    That is still Claude Code's own tree -> owner recorded."""
    out = tmp_path / "owner.json"
    env = _owner_env(CLAUDE_CODE_SESSION_ID=SID_A, CLAUDE_PID=str(os.getpid()))
    # Claude Code runs each Bash command in its own session (setsid); so does this
    subprocess.run(["sh", "-c", f'"{sys.executable}" -c "$0" "$1" 0.5 &', _PRINT_OWNER,
                    str(out)], env=env, check=True, timeout=10, start_new_session=True)
    res = _wait_json(out)
    assert res == {"owner": {"session_id": SID_A, "claude_pid": os.getpid()}, "why": None}


@pytest.mark.skipif(shutil.which("tmux") is None, reason="no tmux")
def test_join_via_foreign_tmux_server_gets_no_owner(home, tmp_path):
    """Leak repro: session B's Bash started a tmux server; session A starts the join in
    a new tmux session on it. The join inherits B's CLAUDE_* from the server's global
    environment. Before the fix B became the owner of A's join (B's tool calls in A's
    call); now: no owner, a warning, and neither session writes."""
    sock = f"vhowner{os.getpid()}"
    out = tmp_path / "owner.json"
    b_claude = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    env_b = _owner_env(CLAUDE_CODE_SESSION_ID=SID_B, CLAUDE_PID=str(b_claude.pid))
    env_a = _owner_env(CLAUDE_CODE_SESSION_ID=SID_A, CLAUDE_PID=str(os.getpid()))
    script = tmp_path / "print_owner.py"
    script.write_text(_PRINT_OWNER.replace("; ", "\n"))
    try:
        subprocess.run(["tmux", "-L", sock, "-f", "/dev/null", "new-session", "-d", "-s", "b",
                        "sleep 30"], env=env_b, check=True, timeout=10)
        subprocess.run(["tmux", "-L", sock, "new-session", "-d", "-s", "a",
                        f"'{sys.executable}' '{script}' '{out}' 0"],
                       env=env_a, check=True, timeout=10)
        res = _wait_json(out)
    finally:
        subprocess.run(["tmux", "-L", sock, "kill-server"], capture_output=True, timeout=10, check=False)
        b_claude.kill()
        b_claude.wait()
    assert res["owner"] == {}
    assert res["why"]
    d = _join(owner=res["owner"])
    assert activity.pre_tool_use(_event(SID_B)) is False
    assert activity.pre_tool_use(_event(SID_A)) is False
    assert _lines(d) == []


# ----- join writes the owner into session.json (behaviour, fake LiveKit room) ---------
class _FakeLP:
    async def publish_data(self, data, reliable=True, topic=""):
        return None


class _FakeRoom:
    def __init__(self):
        self.handlers = {}
        self.remote_participants = {}
        self.local_participant = _FakeLP()

    def on(self, event, fn=None):
        self.handlers[event] = fn

    async def connect(self, url, token):
        return None

    async def disconnect(self):
        return None


def _run_join_and_read_info(monkeypatch) -> dict:
    monkeypatch.setattr(cli.rtc, "Room", _FakeRoom)

    async def _mint(*a, **k):
        return {"url": "wss://fake", "token": "t"}

    monkeypatch.setattr(cli, "_mint_token", _mint)
    monkeypatch.setattr("sys.stdin", io.StringIO(""))

    async def run():
        join = asyncio.create_task(cli._join("https://voicehook.example/r/abc-def-ghi-XYZ4",
                                             "claude-own", "Claude", True, None,
                                             model="opus-5.5", idle_timeout=0, no_greet=True))
        sock = await asyncio.to_thread(vsession.resolve_socket, None, 5)
        info = json.loads((vsession.session_dir("abc-def-ghi-XYZ4", "claude-own")
                           / vsession.INFO_NAME).read_text())
        await asyncio.to_thread(vsession.request, sock, {"cmd": "leave"}, 5)
        await asyncio.wait_for(join, 5)
        return info
    return asyncio.run(run())


def test_join_records_owner_in_session_info(home, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID_A)
    monkeypatch.setenv("CLAUDE_PID", str(os.getppid()))
    info = _run_join_and_read_info(monkeypatch)
    assert info["owner"] == {"session_id": SID_A, "claude_pid": os.getppid()}
    assert "no owner recorded" not in capsys.readouterr().err


def test_join_with_foreign_claude_pid_records_no_owner_and_warns(home, monkeypatch, capsys):
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID_B)
        monkeypatch.setenv("CLAUDE_PID", str(other.pid))
        info = _run_join_and_read_info(monkeypatch)
    finally:
        other.kill()
        other.wait()
    assert info["owner"] == {}
    err = capsys.readouterr().err
    assert "no owner recorded" in err and "VOICEHOOK_SESSION" in err


# ----- bridge session (Quickstart A) --------------------------------------------------
@pytest.fixture
def posts(monkeypatch):
    sent: list[str] = []
    monkeypatch.setattr(activity, "_bridge_post", lambda b, s, t, timeout=2.0: sent.append(t) or 200)
    monkeypatch.setattr(activity, "_spawn_flusher", lambda delay: None)
    return SimpleNamespace(sent=sent)


def test_bridge_only_for_the_session_that_saved_it(home, posts, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID_A)
    monkeypatch.setenv("CLAUDE_PID", str(os.getppid()))
    activity.save_bridge_session({"session": "sess-abc"}, "https://voicehook.ai")
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
    stored = json.loads((activity.state_root() / activity.BRIDGE_SESSION_NAME).read_text())
    assert stored["owner"] == {"session_id": SID_A, "claude_pid": os.getppid()}
    assert activity.pre_tool_use(_event(SID_B)) is False
    assert posts.sent == []
    assert activity.pre_tool_use(_event(SID_A)) is True
    assert posts.sent == ["Run the tests"]


def _save_bridge_as(monkeypatch, sid: str, token: str) -> None:
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", sid)
    monkeypatch.setenv("CLAUDE_PID", str(os.getppid()))
    activity.save_bridge_session({"session": token}, "https://voicehook.ai")


@pytest.fixture
def token_posts(monkeypatch):
    sent: list[tuple[str, str]] = []
    status = {"code": 200}
    monkeypatch.setattr(activity, "_bridge_post",
                        lambda b, s, t, timeout=2.0: sent.append((s, t)) or status["code"])
    monkeypatch.setattr(activity, "_spawn_flusher", lambda delay: None)
    return SimpleNamespace(sent=sent, status=status)


def test_pending_line_of_a_is_not_flushed_into_b_bridge(home, token_posts, monkeypatch):
    """A queues a line inside the 5 s window, then B saves its own bridge session: the
    flusher must drop A's line, never POST it with B's token."""
    _save_bridge_as(monkeypatch, SID_A, "tok-A")
    t = time.time()
    assert activity.pre_tool_use(_event(SID_A, tool_input={"description": "first of A"}), now=t)
    assert activity.pre_tool_use(_event(SID_A, tool_input={"description": "second of A"},
                                        tool_use_id="toolu_a2"), now=t + 1)   # pending
    _save_bridge_as(monkeypatch, SID_B, "tok-B")
    activity.bridge_flush(now=t + 5)
    assert token_posts.sent == [("tok-A", "first of A")]
    activity.bridge_flush(now=t + 6)                      # dropped, not kept for later
    assert token_posts.sent == [("tok-A", "first of A")]


def test_pending_line_flushes_to_its_own_bridge(home, token_posts, monkeypatch):
    """Positive control: same session, the pending line goes out."""
    _save_bridge_as(monkeypatch, SID_A, "tok-A")
    t = time.time()
    activity.pre_tool_use(_event(SID_A, tool_input={"description": "first"}), now=t)
    activity.pre_tool_use(_event(SID_A, tool_input={"description": "second"},
                                 tool_use_id="toolu_a2"), now=t + 1)
    activity.bridge_flush(now=t + 5)
    assert token_posts.sent == [("tok-A", "first"), ("tok-A", "second")]


def test_pending_line_dropped_when_same_token_gets_other_owner(home, token_posts, monkeypatch):
    _save_bridge_as(monkeypatch, SID_A, "tok-A")
    t = time.time()
    activity.pre_tool_use(_event(SID_A, tool_input={"description": "first"}), now=t)
    activity.pre_tool_use(_event(SID_A, tool_input={"description": "second"},
                                 tool_use_id="toolu_a2"), now=t + 1)
    _save_bridge_as(monkeypatch, SID_B, "tok-A")
    activity.bridge_flush(now=t + 5)
    assert token_posts.sent == [("tok-A", "first")]


def test_owner_check_and_send_use_the_same_bridge_session(home, token_posts, monkeypatch):
    """TOCTOU: B re-saves the bridge between the owner check and the send. The line
    of A goes to the session whose owner was checked (A), never to B's."""
    _save_bridge_as(monkeypatch, SID_A, "tok-A")
    real = activity.owned_by

    def racing(owner, ev):
        ok = real(owner, ev)
        _save_bridge_as(monkeypatch, SID_B, "tok-B")
        return ok
    monkeypatch.setattr(activity, "owned_by", racing)
    activity.pre_tool_use(_event(SID_A, tool_input={"description": "A line"}), now=time.time())
    assert [s for s, _ in token_posts.sent] == ["tok-A"]


def test_expired_session_of_a_does_not_clear_b_bridge(home, token_posts, monkeypatch):
    _save_bridge_as(monkeypatch, SID_A, "tok-A")
    real = activity.owned_by

    def racing(owner, ev):
        ok = real(owner, ev)
        _save_bridge_as(monkeypatch, SID_B, "tok-B")
        return ok
    monkeypatch.setattr(activity, "owned_by", racing)
    token_posts.status["code"] = 410
    activity.pre_tool_use(_event(SID_A, tool_input={"description": "A line"}), now=time.time())
    assert activity.load_bridge_session()["session"] == "tok-B"


def test_bridge_without_owner_posts_nothing(home, posts):
    root = activity.state_root()
    root.mkdir(parents=True, exist_ok=True)
    (root / activity.BRIDGE_SESSION_NAME).write_text(
        json.dumps({"base": "https://voicehook.ai", "session": "sess-abc"}))
    assert activity.pre_tool_use(_event(SID_A)) is False
    assert posts.sent == []


# ----- end to end: the hook as Claude Code runs it ------------------------------------
def test_hook_subprocess_other_session_writes_nothing(home):
    d = _join(owner={"session_id": SID_A})
    env = {**os.environ, "VOICEHOOK_AGENT_HOME": str(home),
           "PYTHONPATH": str(Path(cli.__file__).parents[1])}
    for k in ("VOICEHOOK_SESSION", "CLAUDE_CODE_SESSION_ID", "CLAUDE_PID"):
        env.pop(k, None)
    for sid in (SID_B, SID_A):
        r = subprocess.run([sys.executable, "-m", "voicehook_agent.activity", "pre-tool-use"],
                           input=_event(sid), capture_output=True, text=True, env={
                               **env, "CLAUDE_CODE_SESSION_ID": sid},
                           timeout=30, check=False)
        assert r.returncode == 0 and r.stdout == ""
    assert len(_lines(d)) == 1
