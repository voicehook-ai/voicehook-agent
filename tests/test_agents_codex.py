"""0.20.0 Codex CLI: hooks adapter (Claude-Code-compatible hooks.json events), notify
fallback, installer (backup first) and the local MCP server. Fixtures only, no Codex."""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys

import pytest

from voicehook_agent import activity, agents, mcpserver
from voicehook_agent import multisession as ms
from voicehook_agent import relayops as ro
from voicehook_agent import session as vsession

URL = "https://voicehook.ai/api/activity/relay"
TOKEN = "fake.relay.test.only"  # fake, low entropy (gitleaks)
CWD = "/home/x/shop"
THREAD = "019a-codex-thread"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    for k in ("CLAUDE_PID", "VOICEHOOK_OWNER_PID", ms.LABEL_ENV, ms.MACHINE_LABEL_ENV,
              ms.NO_RELAY_ENV, "VOICEHOOK_SESSION", "CODEX_HOME"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("VOICEHOOK_AGENT_HOME", str(tmp_path / "home"))


def _ev(name, sid=THREAD, **kw) -> str:
    ev = {"session_id": sid, "hook_event_name": name, "cwd": CWD, "transcript_path": None,
          "model": "gpt-5-codex", "turn_id": "t1"}
    ev.update(kw)
    return json.dumps(ev)


def _join(owner: str):
    d = vsession.session_dir("room-a", "codex-x")
    d.mkdir(parents=True)
    vsession.write_info(d, {"pid": os.getpid(), "room": "room-a", "owner_session": owner})
    return d


def _log(d):
    return activity.read_tail(d / activity.ACTIVITY_NAME)


# ----- normalize --------------------------------------------------------------------
def test_bash_gets_category_never_the_command():
    ev = agents.normalize_codex(json.loads(_ev(
        "PreToolUse", tool_name="Bash", tool_input={"command": "pytest -q tests/secret_customer.py"})))
    assert ev["tool_input"] == {"command": "pytest -q tests/secret_customer.py",
                                "description": "Tests"}
    line = activity.format_line(ev, now=0)
    assert line.endswith(" Bash: Tests") and "secret" not in line
    argv = agents.normalize_codex({"hook_event_name": "PreToolUse", "tool_name": "shell",
                                   "tool_input": {"command": ["bash", "-lc", "git push origin x"]}})
    assert argv["tool_name"] == "Bash" and argv["tool_input"]["description"] == "Git"
    plain = agents.normalize_codex({"hook_event_name": "PreToolUse", "tool_name": "exec_command",
                                    "tool_input": {"cmd": "ls -la"}})
    assert plain["tool_input"] == {"command": "ls -la"}  # no category: tool name only
    assert agents.command_category("wrangler deploy") == "Deploy"
    assert agents.command_category("echo 'pytest'") == ""  # quoted text is no command


def test_apply_patch_logs_basename_only():
    patch = "*** Begin Patch\n*** Update File: src/kunden/geheim.py\n@@\n-a\n+b\n*** End Patch"
    ev = agents.normalize_codex({"hook_event_name": "PostToolUse", "tool_name": "apply_patch",
                                 "tool_input": {"command": patch}})
    assert ev["tool_name"] == "Edit" and ev["tool_input"] == {"file_path": "src/kunden/geheim.py"}
    assert activity.format_line(ev, now=0).endswith(" Edit: geheim.py")


def test_failed_bash_and_permission_reason():
    ev = agents.normalize_codex({"hook_event_name": "PostToolUse", "tool_name": "Bash",
                                 "tool_input": {"command": "npm test"},
                                 "tool_response": "Exit code: 1\nWall time: 2s\nOutput:\nboom"})
    assert ev["tool_response"]["is_error"] is True
    assert ms.classify(ev) == ("error", "Fehler bei Bash")
    ok = agents.normalize_codex({"hook_event_name": "PostToolUse", "tool_name": "Bash",
                                 "tool_input": {"command": "npm test"},
                                 "tool_response": "Exit code: 0\nOutput:\nfine"})
    assert ok["tool_response"] == "Exit code: 0\nOutput:\nfine"
    perm = agents.normalize_codex({"hook_event_name": "PermissionRequest", "tool_name": "Bash",
                                   "tool_input": {"command": "rm -rf build",
                                                  "description": "Build-Ordner löschen"}})
    from voicehook_agent import notify
    assert notify.classify(perm)["question"] == "Freigabe für Bash: Build-Ordner löschen?"


def test_codex_thread_id_is_the_own_session(monkeypatch):
    monkeypatch.setenv("CODEX_THREAD_ID", THREAD)
    assert ms.own_session_id() == THREAD
    assert ms.own_session_hash() == ms.session_hash(THREAD)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "cc-1")  # Claude Code wins
    assert ms.own_session_id() == "cc-1"


# ----- own join (Codex itself ran `join`) ---------------------------------------------
def test_own_join_tool_lines_and_one_turn_end():
    d = _join(ms.session_hash(THREAD))
    assert agents.codex_hook("pre-tool-use", _ev(
        "PreToolUse", tool_name="Bash", tool_use_id="c1",
        tool_input={"command": "uv run pytest"})) == ""
    assert agents.codex_hook("post-tool-use", _ev(
        "PostToolUse", tool_name="Bash", tool_use_id="c1", tool_input={"command": "uv run pytest"},
        tool_response="Exit code: 0")) == ""  # Pre already logged it
    assert agents.codex_hook("user-prompt-submit", _ev(
        "UserPromptSubmit", prompt="Fix the failing tests for kunde Müller")) == ""
    assert agents.codex_hook("stop", _ev("Stop", stop_hook_active=False,
                                         last_assistant_message="done")) == "{}"
    agents.codex_notify(json.dumps({"type": "agent-turn-complete", "thread-id": THREAD,
                                    "turn-id": "t1", "cwd": CWD, "input-messages": ["x"],
                                    "last-assistant-message": "done"}))  # same turn: no 2nd line
    lines = [ln.split(" ", 1)[1] for ln in _log(d)]
    assert lines == ["Bash: Tests", "Codex: neuer Auftrag: Tests, Bugfix", "Codex: Turn fertig"]
    assert "Müller" not in json.dumps(_log(d))


def test_notify_alone_reports_turn_end():
    d = _join(ms.session_hash(THREAD))
    payload = {"type": "agent-turn-complete", "thread-id": THREAD, "turn-id": "t9", "cwd": CWD,
               "input-messages": ["geheim"], "last-assistant-message": "geheim"}
    agents.codex_notify(json.dumps(payload))
    agents.codex_notify(json.dumps(payload))
    agents.codex_notify(json.dumps({"type": "something-else"}))
    agents.codex_notify("not json")
    assert [ln.split(" ", 1)[1] for ln in _log(d)] == ["Codex: Turn fertig"]


def test_foreign_join_gets_no_own_lines():
    d = _join(ms.session_hash("CLAUDE-JOINER"))
    agents.codex_hook("stop", _ev("Stop"))
    assert _log(d) == []


# ----- helper (another session owns the join) -----------------------------------------
def test_helper_turn_and_pr_over_local_relay(monkeypatch):
    monkeypatch.setenv(ms.MACHINE_LABEL_ENV, "crown")
    d = _join(ms.session_hash("CLAUDE-JOINER"))
    ms.write_local_relay(d, "room-a", owner_session=ms.session_hash("CLAUDE-JOINER"))
    agents.codex_hook("post-tool-use", _ev(
        "PostToolUse", tool_name="Bash", tool_input={"command": "gh pr create --fill"},
        tool_response="Exit code: 0\nOutput:\nhttps://github.com/o/r/pull/42"))
    agents.codex_hook("stop", _ev("Stop", turn_id="t2"))
    label = "shop " + ms.label_suffix(THREAD)
    assert [ln.split(" ", 1)[1] for ln in _log(d)] == [
        f"relay: Helfer crown: {label}: PR #42 geöffnet",
        f"relay: Helfer crown: {label}: Turn fertig"]
    # the MCP server of the same agent in that cwd uses the same name
    assert agents.session_label("codex", cwd=CWD) == label


def test_real_hook_process_stop_prints_json(tmp_path):
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
    r = subprocess.run([sys.executable, "-m", "voicehook_agent.activity", "codex", "stop"],
                       input=_ev("Stop"), capture_output=True, text=True, env=env, timeout=30,
                       check=False)
    assert r.returncode == 0 and json.loads(r.stdout) == {}
    r = subprocess.run([sys.executable, "-m", "voicehook_agent.activity", "codex", "pre-tool-use"],
                       input="garbage", capture_output=True, text=True, env=env, timeout=30,
                       check=False)
    assert r.returncode == 0 and r.stdout == ""  # never blocks a tool
    r = subprocess.run([sys.executable, "-m", "voicehook_agent.activity", "codex-notify",
                        json.dumps({"type": "agent-turn-complete", "thread-id": "x"})],
                       capture_output=True, text=True, env=env, timeout=30, check=False)
    assert r.returncode == 0 and r.stdout == ""


# ----- installer ----------------------------------------------------------------------
def _tomllib():
    return pytest.importorskip("tomllib")


def test_install_fresh_and_idempotent(tmp_path):
    home = tmp_path / "codex"
    rc, msg = agents.install_codex(home, now=0)
    assert rc == 0 and "/hooks" in msg
    hooks = json.loads((home / "hooks.json").read_text())["hooks"]
    assert set(hooks) == {"PreToolUse", "PostToolUse", "UserPromptSubmit", "Stop",
                          "PermissionRequest", "SessionStart"}
    assert hooks["Stop"][0]["hooks"][0]["command"] == "voicehook-agent-hook codex stop || true"
    assert hooks["PreToolUse"][0]["matcher"] == "*"
    cfg = _tomllib().loads((home / "config.toml").read_text())
    assert cfg["notify"] == ["voicehook-agent-hook", "codex-notify"]
    assert cfg["mcp_servers"]["voicehook-local"]["args"] == ["mcp", "--agent", "codex"]
    before = sorted(p.name for p in home.iterdir())
    rc, msg = agents.install_codex(home, now=60)
    assert rc == 0 and "already" in msg and agents.TRUST_HINT not in msg
    assert sorted(p.name for p in home.iterdir()) == before  # no change, no backup


def test_install_keeps_existing_config_with_backup(tmp_path):
    home = tmp_path / "codex"
    home.mkdir()
    cfg_old = ('model = "gpt-5-codex"\nnotify = ["my-notifier"]\n\n[mcp_servers.docs]\n'
               'command = "docs-mcp"\n')
    (home / "config.toml").write_text(cfg_old)
    hooks_old = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "say done"}]}]}}
    (home / "hooks.json").write_text(json.dumps(hooks_old))
    rc, msg = agents.install_codex(home, now=0)
    assert rc == 0 and "own `notify`" in msg
    backups = sorted(p.name for p in home.iterdir() if ".voicehook-backup-" in p.name)
    assert backups == ["config.toml.voicehook-backup-19700101T000000Z",
                       "hooks.json.voicehook-backup-19700101T000000Z"]
    assert (home / backups[0]).read_text() == cfg_old
    cfg = _tomllib().loads((home / "config.toml").read_text())
    assert cfg["notify"] == ["my-notifier"] and cfg["model"] == "gpt-5-codex"
    assert set(cfg["mcp_servers"]) == {"docs", "voicehook-local"}
    stop = json.loads((home / "hooks.json").read_text())["hooks"]["Stop"]
    assert [h["hooks"][0]["command"] for h in stop] == ["say done",
                                                        "voicehook-agent-hook codex stop || true"]


def test_install_puts_notify_before_first_table(tmp_path):
    home = tmp_path / "codex"
    home.mkdir()
    (home / "config.toml").write_text('model = "o3"\n[profiles.fast]\nmodel = "mini"\n')
    assert agents.install_codex(home, now=0)[0] == 0
    cfg = _tomllib().loads((home / "config.toml").read_text())
    assert cfg["notify"] == ["voicehook-agent-hook", "codex-notify"]
    assert cfg["profiles"]["fast"] == {"model": "mini"}  # notify did not land in a table


def test_install_refuses_broken_files(tmp_path):
    home = tmp_path / "codex"
    home.mkdir()
    (home / "hooks.json").write_text("{broken")
    rc, _msg = agents.install_codex(home)
    assert rc == 1 and (home / "hooks.json").read_text() == "{broken"
    assert not (home / "config.toml").exists()
    (home / "hooks.json").unlink()
    _tomllib()
    (home / "config.toml").write_text("model = \n")
    rc, _msg = agents.install_codex(home)
    assert rc == 1 and not (home / "hooks.json").exists()


def test_hook_cli_install_and_print(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "ch"))
    assert activity.main(["install", "--agent", "codex"]) == 0
    assert (tmp_path / "ch" / "hooks.json").exists()
    assert activity.main(["print", "--agent", "codex"]) == 0
    assert "voicehook-agent-hook codex pre-tool-use" in capsys.readouterr().out
    assert activity.main(["install", "--agent", "gemini"]) == 2
    assert activity.main(["install", "--codex-home", str(tmp_path)]) == 2  # needs --agent codex
    settings = tmp_path / "s.json"
    assert activity.main(["install", "--settings", str(settings)]) == 0  # Claude Code unchanged
    assert "PreToolUse" in json.loads(settings.read_text())["hooks"]


# ----- MCP server ---------------------------------------------------------------------
def _rpc(msgs, agent="codex"):
    out = io.StringIO()
    mcpserver.serve(io.StringIO("".join(json.dumps(m) + "\n" for m in msgs)), out, agent=agent)
    return [json.loads(x) for x in out.getvalue().splitlines()]


def _call(name, args, rid=9):
    res = _rpc([{"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                 "params": {"name": name, "arguments": args}}])[0]["result"]
    return json.loads(res["content"][0]["text"]), res.get("isError", False)


def test_mcp_handshake_and_tools():
    out = _rpc([{"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                            "clientInfo": {"name": "codex-mcp-client", "version": "0.163"}}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "ping"},
                {"jsonrpc": "2.0", "id": 4, "method": "nope"}])
    assert [m["id"] for m in out] == [1, 2, 3, 4]  # the notification got no answer
    assert out[0]["result"]["protocolVersion"] == "2025-06-18"
    assert out[0]["result"]["serverInfo"]["name"] == "voicehook-local"
    assert {t["name"] for t in out[1]["result"]["tools"]} == {"status", "decide", "next",
                                                               "activity"}
    assert out[2]["result"] == {} and out[3]["error"]["code"] == -32601


def test_mcp_without_call_is_an_error_not_a_crash():
    res, err = _call("status", {"doing": "Tests"})
    assert err and "no voicehook call" in res["error"]
    res, err = _call("decide", {"question": ""})
    assert err and "question" in res["error"]
    res, err = _call("bogus", {})
    assert err


def test_mcp_helper_over_remote_relay(monkeypatch):
    ms.write_relay(URL, TOKEN, "room-a", 3600, owner_pid=os.getpid(),
                   owner_session=ms.session_hash("CLAUDE-JOINER"))
    monkeypatch.chdir("/")
    monkeypatch.setenv(ms.LABEL_ENV, "Codex Shop")
    calls = []

    def fake_http(rel, method, url, body, timeout):
        calls.append((method, url.split("/api/activity/relay/")[1], body))
        if url.endswith("/decide"):
            return 200, {"ok": True, "id": "d0a1b2c3d", "number": 3}
        if "/next" in url:
            return 200, {"events": [{"type": "decision", "id": "d0a1b2c3d", "verdict": "yes"}]}
        return 200, {"ok": True}
    monkeypatch.setattr(ro, "_http", fake_http)
    res, err = _call("status", {"doing": "Tests fixen", "open": ["PR"]})
    assert not err and res == {"ok": True, "type": "board", "from": "Codex Shop", "via": "relay"}
    assert calls[-1] == ("POST", "status", {"from": "Codex Shop", "doing": "Tests fixen",
                                            "open": ["PR"], "done": []})
    res, err = _call("decide", {"question": "Mergen?", "options": ["Ja", "Nein"], "wait_s": 1})
    assert not err and res["id"] == "d0a1b2c3d" and res["decision"]["verdict"] == "yes"
    res, err = _call("next", {"timeout_s": 0})
    assert not err and res["type"] in ("timeout", "events")
    sent = []
    monkeypatch.setattr(ms, "post_line", sent.append)
    res, err = _call("activity", {"text": "Tests laufen token=geheimwort"})
    assert not err and res["sent"] is True
    assert sent == ["Codex Shop: Tests laufen token=[redacted]"]
    assert TOKEN not in json.dumps(calls)


def test_mcp_own_join_goes_to_the_control_socket(monkeypatch):
    seen = []
    monkeypatch.setattr(vsession, "resolve_socket", lambda s, wait=0.0: "/x/ctl.sock")
    monkeypatch.setattr(vsession, "request",
                        lambda sock, req, timeout=None: seen.append(req) or {"ok": True, **req})
    _res, err = _call("status", {"doing": "Release"})
    assert not err and seen[-1] == {"cmd": "board", "board": {"doing": "Release", "open": [],
                                                                "done": []}}
    _call("next", {"timeout_s": 5})
    assert seen[-1] == {"cmd": "next", "timeout": 5.0}
    _call("activity", {"text": "Release gebaut"})
    assert seen[-1] == {"cmd": "activity", "text": "Release gebaut"}


def test_board_answer_passes_through_on_codex_events(monkeypatch):
    """board.py (0.19.0) answers with additionalContext / a Stop decision: Codex reads the
    same JSON, the adapter prints it unchanged (only on events Codex reads stdout of)."""
    seen = []

    def fake_board(raw):
        ev = json.loads(raw)
        seen.append(ev["hook_event_name"])
        if ev["hook_event_name"] == "Stop":
            return {"decision": "block", "reason": "Antwort: Ja"}
        return {"hookSpecificOutput": {"hookEventName": ev["hook_event_name"],
                                       "additionalContext": "Board 4 min alt"}}
    monkeypatch.setattr(activity, "board_hook", fake_board, raising=False)
    out = agents.codex_hook("post-tool-use", _ev("PostToolUse", tool_name="Bash",
                                                 tool_input={"command": "ls"}))
    assert json.loads(out)["hookSpecificOutput"]["additionalContext"] == "Board 4 min alt"
    assert json.loads(agents.codex_hook("session-start", _ev("SessionStart", source="startup")))
    assert agents.codex_hook("pre-tool-use", _ev("PreToolUse", tool_name="Bash",
                                                 tool_input={"command": "ls"})) == ""
    assert json.loads(agents.codex_hook("stop", _ev("Stop", turn_id="t7"))) == {
        "decision": "block", "reason": "Antwort: Ja"}
    assert agents.codex_hook("permission-request", _ev(
        "PermissionRequest", tool_name="Bash", tool_input={"command": "ls"})) == ""
    assert seen == ["PostToolUse", "SessionStart", "PreToolUse", "Stop"]  # never on permission
