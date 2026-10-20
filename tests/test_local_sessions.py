"""0.21.4: right after `join` the operator learns which other Claude Code / Codex sessions run
on this machine, with the question for the human and the setup commands (Oliver 20.10.2026)."""
from __future__ import annotations

import asyncio
import json

import pytest

from voicehook_agent import activity, cli
from voicehook_agent import localsessions as ls

PS = """\
    1     0 /sbin/launchd
  100     1 /Users/o/Applications/Claude.app/Contents/MacOS/Claude
  200   100 /Users/o/Library/Application Support/Claude/claude-code/2.1.299/x/claude.app/Contents/MacOS/claude --output-format stream-json
  210   200 /bin/zsh -c source snapshot.sh
  220   210 /Users/o/.local/bin/voicehook-agent join https://voicehook.ai/r/x
  300   100 /Users/o/Library/Application Support/Claude/claude-code/2.1.299/x/claude.app/Contents/MacOS/claude --resume
  310   300 /Users/o/Library/Application Support/Claude/claude-code/2.1.299/x/claude.app/Contents/MacOS/claude --child
  400     1 claude
  500     1 node /usr/lib/node_modules/@anthropic-ai/claude-code/cli.js
  600     1 /opt/homebrew/bin/codex
  700     1 grep claude
  800     1 /Users/o/Applications/Claude.app/Contents/Frameworks/Claude Helper.app/Contents/MacOS/Claude Helper --type=gpu
"""


def test_agent_kind():
    assert ls.agent_kind("claude") == "claude"
    assert ls.agent_kind("/a b/c/claude --resume") == "claude"
    assert ls.agent_kind("node /x/@anthropic-ai/claude-code/cli.js") == "claude"
    assert ls.agent_kind("/opt/homebrew/bin/codex exec") == "codex"
    for a in ("grep claude", "/Applications/Claude.app/Contents/MacOS/Claude",
              "/x/Claude Helper.app/Contents/MacOS/Claude Helper --type=gpu", ""):
        assert ls.agent_kind(a) == ""


def test_other_sessions_without_own_and_without_agent_children():
    procs = ls.parse_ps(PS)
    got = ls.agent_sessions(procs, own_pid=220)        # the join runs under session 200
    assert [(s["pid"], s["agent"]) for s in got] == [(300, "claude"), (400, "claude"),
                                                       (500, "claude"), (600, "codex")]


def test_hint_asks_and_names_setup_when_hooks_missing():
    sessions = [{"agent": "claude", "name": "salesengine"}, {"agent": "codex", "name": "infra"}]
    h = ls.hint(sessions, claude_ok=False, codex_ok=False)
    assert h["_meta"] == "sessions" and h["count"] == 2
    assert h["ask"] == ("Soll ich die anderen 2 Sessions hier einbinden, damit sie ihren Status "
                        "in den Call bringen?")
    assert h["setup"] == [ls.INSTALL_CLAUDE, ls.INSTALL_CODEX]
    assert "claude: salesengine" in h["text"] and "/hooks" in h["text"]
    ok = ls.hint(sessions, claude_ok=True, codex_ok=True, registered=["salesengine 1a2b"])
    assert "ask" not in ok and "setup" not in ok and "installiert" in ok["text"]
    assert ok["reporting"] == ["salesengine 1a2b"]
    assert ls.hint([], False, False) is None


def test_detect_end_to_end_with_fakes(monkeypatch, tmp_path):
    monkeypatch.delenv(ls.ENV_OFF, raising=False)
    monkeypatch.setattr(activity, "default_settings_path", lambda: tmp_path / "settings.json")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    got = ls.detect(own_pid=220, ps=lambda: PS, cwd=lambda pid: {300: "kunden-c", 400: "voicehook"}.get(pid, ""))
    assert got["count"] == 4 and got["setup"] == [ls.INSTALL_CLAUDE, ls.INSTALL_CODEX]
    assert {"agent": "claude", "name": "kunden-c"} in got["sessions"]
    activity.install(tmp_path / "settings.json")      # now the Claude hooks are there
    got = ls.detect(own_pid=220, ps=lambda: PS, cwd=lambda pid: "")
    assert got["setup"] == [ls.INSTALL_CODEX] and got["hooks"]["claude"] is True
    monkeypatch.setenv(ls.ENV_OFF, "0")
    assert ls.detect(own_pid=220, ps=lambda: PS) is None


def test_detect_never_raises():
    def boom():
        raise RuntimeError("x")
    assert ls.detect(ps=boom) is None


@pytest.mark.parametrize("json_mode", [True, False])
def test_join_emits_the_hint_once(json_mode, capsys):
    h = ls.hint([{"agent": "claude", "name": "salesengine"}], False, True)
    out = asyncio.run(cli._emit_sessions_hint(json_mode, detect=lambda: h))
    assert out == h
    cap = capsys.readouterr()
    if json_mode:
        assert json.loads(cap.out) == h and cap.err == ""
    else:
        assert cap.out == "" and cap.err.startswith("[sessions] 1 weitere Agent-Session")
    assert asyncio.run(cli._emit_sessions_hint(json_mode, detect=lambda: None)) is None
    assert capsys.readouterr().out == ""
