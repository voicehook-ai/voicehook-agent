"""0.25.0 opencode: the plugin shells out to `voicehook-agent-hook opencode <event>` with a
Claude-Code-shaped event (PostToolUse, Stop) on stdin; the Python adapter reuses the Codex
normalizer and handlers. Installer writes one JS plugin (backup first, idempotent). Fixtures
only, no opencode."""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from voicehook_agent import activity, agents
from voicehook_agent import multisession as ms
from voicehook_agent import session as vsession

CWD = "/home/x/shop"
SID = "oc-session-01abc"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    for k in ("CLAUDE_PID", "VOICEHOOK_OWNER_PID", ms.LABEL_ENV, ms.MACHINE_LABEL_ENV,
              ms.NO_RELAY_ENV, "VOICEHOOK_SESSION", "CODEX_HOME", "CODEX_THREAD_ID",
              "OPENCODE_SESSION_ID", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("VOICEHOOK_AGENT_HOME", str(tmp_path / "home"))


def _ev(name, sid=SID, **kw) -> str:
    ev = {"session_id": sid, "hook_event_name": name, "cwd": CWD}
    ev.update(kw)
    return json.dumps(ev)


def _join(owner: str):
    d = vsession.session_dir("room-a", "oc-x")
    d.mkdir(parents=True)
    vsession.write_info(d, {"pid": os.getpid(), "room": "room-a", "owner_session": owner})
    return d


def _log(d):
    return activity.read_tail(d / activity.ACTIVITY_NAME)


# ----- session identity --------------------------------------------------------------
def test_opencode_session_id_is_the_own_session(monkeypatch):
    monkeypatch.setenv("OPENCODE_SESSION_ID", SID)
    assert ms.own_session_id() == SID
    assert ms.own_session_hash() == ms.session_hash(SID)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "cc-1")  # Claude Code still wins
    assert ms.own_session_id() == "cc-1"


# ----- adapter: own join (opencode itself ran `join`) --------------------------------
def test_tool_line_gets_category_and_turn_end_once():
    d = _join(ms.session_hash(SID))
    # the plugin only fires PostToolUse (no Pre): one line per tool, with a category
    assert agents.opencode_hook("post-tool-use", _ev(
        "PostToolUse", tool_name="Bash",
        tool_input={"command": "pytest -q tests/secret_customer.py"},
        tool_response="Exit code: 0")) == ""
    assert agents.opencode_hook("stop", _ev("Stop", turn_id="t1")) == ""
    assert agents.opencode_hook("stop", _ev("Stop", turn_id="t1")) == ""  # same turn: no 2nd line
    lines = [ln.split(" ", 1)[1] for ln in _log(d)]
    assert lines == ["Bash: Tests", "opencode: Turn fertig"]
    assert "secret" not in json.dumps(_log(d))


def test_failed_tool_is_classified_as_error():
    """A non-zero exit normalizes to is_error -> classify "Fehler bei Bash" (the relay/notify
    path reports it; the own-join activity line stays the plain tool line)."""
    ev = json.loads(_ev("PostToolUse", tool_name="Bash", tool_input={"command": "npm test"},
                        tool_response="Exit code: 1\nOutput:\nboom"))
    norm = agents.normalize_codex({**ev, "hook_event_name": "PostToolUse"})
    assert norm["tool_response"]["is_error"] is True
    assert ms.classify(norm) == ("error", "Fehler bei Bash")
    assert agents.opencode_hook("post-tool-use", _ev(
        "PostToolUse", tool_name="Bash", tool_input={"command": "npm test"},
        tool_response="Exit code: 1\nOutput:\nboom")) == ""  # never raises, never prints


def test_foreign_join_gets_no_own_lines():
    d = _join(ms.session_hash("CLAUDE-JOINER"))
    agents.opencode_hook("stop", _ev("Stop"))
    assert _log(d) == []


def test_unknown_event_and_garbage_never_raise():
    _join(ms.session_hash(SID))
    assert agents.opencode_hook("pre-tool-use", _ev("PreToolUse")) == ""  # not an opencode event
    assert agents.opencode_hook("post-tool-use", "not json") == ""
    assert agents.opencode_hook("stop", "") == ""


# ----- helper (another session owns the join) ----------------------------------------
def test_helper_turn_and_pr_over_local_relay(monkeypatch):
    monkeypatch.setenv(ms.MACHINE_LABEL_ENV, "crown")
    monkeypatch.setenv("OPENCODE_SESSION_ID", SID)
    d = _join(ms.session_hash("CLAUDE-JOINER"))
    ms.write_local_relay(d, "room-a", owner_session=ms.session_hash("CLAUDE-JOINER"))
    agents.opencode_hook("post-tool-use", _ev(
        "PostToolUse", tool_name="Bash", tool_input={"command": "gh pr create --fill"},
        tool_response="Exit code: 0\nOutput:\nhttps://github.com/o/r/pull/42"))
    agents.opencode_hook("stop", _ev("Stop", turn_id="t2"))
    label = "shop " + ms.label_suffix(SID)
    assert [ln.split(" ", 1)[1] for ln in _log(d)] == [
        f"relay: Helfer crown: {label}: PR #42 geöffnet",
        f"relay: Helfer crown: {label}: Turn fertig"]


def test_real_hook_process_never_blocks_a_tool(tmp_path):
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
    r = subprocess.run([sys.executable, "-m", "voicehook_agent.activity", "opencode", "post-tool-use"],
                       input=_ev("PostToolUse", tool_name="Bash", tool_input={"command": "ls"}),
                       capture_output=True, text=True, env=env, timeout=30, check=False)
    assert r.returncode == 0 and r.stdout == ""  # opencode ignores stdout; never a decision block
    r = subprocess.run([sys.executable, "-m", "voicehook_agent.activity", "opencode", "stop"],
                       input="garbage", capture_output=True, text=True, env=env, timeout=30,
                       check=False)
    assert r.returncode == 0 and r.stdout == ""


# ----- installer ----------------------------------------------------------------------
def test_install_fresh_and_idempotent(tmp_path):
    home = tmp_path / "oc"
    rc, msg = agents.install_opencode(home, now=0)
    assert rc == 0 and "installed" in msg
    plugin = home / "plugin" / agents.OPENCODE_PLUGIN_NAME
    js = plugin.read_text()
    assert "voicehook-agent-hook" in js and '"opencode"' in js
    assert "tool.execute.after" in js and "session.idle" in js
    assert "export default" in js  # opencode auto-loads the default export
    rc, msg = agents.install_opencode(home, now=60)
    assert rc == 0 and "already installed" in msg
    assert sorted(p.name for p in (home / "plugin").iterdir()) == [agents.OPENCODE_PLUGIN_NAME]


def test_install_backs_up_a_different_existing_plugin(tmp_path):
    home = tmp_path / "oc"
    pdir = home / "plugin"
    pdir.mkdir(parents=True)
    (pdir / agents.OPENCODE_PLUGIN_NAME).write_text("// an older version\n")
    rc, msg = agents.install_opencode(home, now=0)
    assert rc == 0 and "updated" in msg
    backups = [p.name for p in pdir.iterdir() if ".voicehook-backup-" in p.name]
    assert backups == [f"{agents.OPENCODE_PLUGIN_NAME}.voicehook-backup-19700101T000000Z"]
    assert (pdir / backups[0]).read_text() == "// an older version\n"
    assert "voicehook-agent-hook" in (pdir / agents.OPENCODE_PLUGIN_NAME).read_text()


def test_config_home_honours_xdg(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    assert agents.opencode_config_home() == tmp_path / "cfg" / "opencode"
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    assert agents.opencode_config_home().name == "opencode"


def test_print_snippet_shows_the_plugin():
    out = agents.print_snippet(agents.OPENCODE)
    assert agents.OPENCODE_PLUGIN_NAME in out and "tool.execute.after" in out


# ----- CLI dispatch -------------------------------------------------------------------
def test_hook_cli_install_and_print(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    assert activity.main(["install", "--agent", "opencode"]) == 0
    assert (tmp_path / "cfg" / "opencode" / "plugin" / agents.OPENCODE_PLUGIN_NAME).exists()
    assert activity.main(["print", "--agent", "opencode"]) == 0
    assert "tool.execute.after" in capsys.readouterr().out
    # --opencode-home wins over XDG
    other = tmp_path / "oh"
    assert activity.main(["install", "--agent", "opencode", "--opencode-home", str(other)]) == 0
    assert (other / "plugin" / agents.OPENCODE_PLUGIN_NAME).exists()
    # wrong flag combinations are rejected, no file written
    assert activity.main(["install", "--agent", "opencode", "--codex-home", str(tmp_path)]) == 2
    assert activity.main(["install", "--opencode-home", str(tmp_path)]) == 2  # needs --agent opencode
    assert activity.main(["install", "--agent", "gemini"]) == 2
