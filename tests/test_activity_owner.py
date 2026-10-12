"""0.14.4: the activity hook writes only for the Claude Code session that owns the join.

The hook is usually installed globally (~/.claude/settings.json, matcher "*"), so it runs
in EVERY Claude Code session on the machine. Before 0.14.4 it took "the one live join"
without asking whose join it was: tool calls of unrelated sessions (other projects)
showed up as activity lines in someone else's call.

Now `join` (and `bridge-session --save`) record the owner from the environment Claude
Code gives its Bash subprocesses (CLAUDE_CODE_SESSION_ID, CLAUDE_PID) and the hook
writes only when the hook event's `session_id` matches (CLAUDE_PID as fallback when the
owner has no session id). No owner, no match or ambiguity: 0 lines (fail closed).
VOICEHOOK_SESSION=<slug>/<identity> stays an explicit override.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
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


def test_pid_fallback_when_owner_has_no_session_id(home, monkeypatch):
    d = _join(owner={"claude_pid": 4242})
    monkeypatch.setenv("CLAUDE_PID", "4343")
    assert activity.pre_tool_use(_event(SID_A)) is False
    monkeypatch.setenv("CLAUDE_PID", "4242")
    assert activity.pre_tool_use(_event(SID_A)) is True
    assert len(_lines(d)) == 1


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


# ----- owner capture ------------------------------------------------------------------
def test_owner_from_env(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_PID", raising=False)
    assert activity.owner_from_env() == {}
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", f"  {SID_A} ")
    monkeypatch.setenv("CLAUDE_PID", "4242")
    assert activity.owner_from_env() == {"session_id": SID_A, "claude_pid": 4242}
    monkeypatch.setenv("CLAUDE_PID", "nope")
    assert activity.owner_from_env() == {"session_id": SID_A}


def test_join_records_owner_in_session_info():
    """`join` writes the owner into session.json (the hook reads it from there)."""
    src = Path(cli.__file__).read_text(encoding="utf-8")
    assert '"owner": vactivity.owner_from_env()' in src


# ----- bridge session (Quickstart A) --------------------------------------------------
@pytest.fixture
def posts(monkeypatch):
    sent: list[str] = []
    monkeypatch.setattr(activity, "_bridge_post", lambda b, s, t, timeout=2.0: sent.append(t) or 200)
    monkeypatch.setattr(activity, "_spawn_flusher", lambda delay: None)
    return SimpleNamespace(sent=sent)


def test_bridge_only_for_the_session_that_saved_it(home, posts, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID_A)
    activity.save_bridge_session({"session": "sess-abc"}, "https://voicehook.ai")
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
    stored = json.loads((activity.state_root() / activity.BRIDGE_SESSION_NAME).read_text())
    assert stored["owner"] == {"session_id": SID_A}
    assert activity.pre_tool_use(_event(SID_B)) is False
    assert posts.sent == []
    assert activity.pre_tool_use(_event(SID_A)) is True
    assert posts.sent == ["Run the tests"]


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
