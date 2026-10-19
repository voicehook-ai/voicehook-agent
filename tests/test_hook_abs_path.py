"""0.21.0: `hook install` writes the absolute path of voicehook-agent-hook. Sessions started by a
service (`claude --resume` from a systemd unit, crown 19.10.2026) run hooks under /bin/sh
without ~/.local/bin in PATH: a bare command was "not found" on every tool call.
`voicehook-agent doctor` checks exactly that."""
from __future__ import annotations

import json
import os
import stat

from voicehook_agent import activity

BARE = "voicehook-agent-hook"


def _bin(tmp_path, name="voicehook-agent-hook", sub="bin"):
    d = tmp_path / sub
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text("#!/bin/sh\nexit 0\n")
    p.chmod(p.stat().st_mode | stat.S_IXUSR)
    return p


def _commands(path):
    data = json.loads(path.read_text())
    return {ev: [h["command"] for e in data["hooks"][ev] for h in e["hooks"]] for ev in data["hooks"]}


def test_hook_bin_prefers_env_then_path_then_python_dir(tmp_path, monkeypatch):
    exe = _bin(tmp_path)
    monkeypatch.setenv(activity.HOOK_BIN_ENV, str(exe))
    assert activity.hook_bin() == str(exe)
    monkeypatch.delenv(activity.HOOK_BIN_ENV)
    monkeypatch.setenv("PATH", str(exe.parent))
    assert activity.hook_bin() == str(exe)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    py = _bin(tmp_path, sub="venv/bin")
    monkeypatch.setattr(activity.sys, "executable", str(py.parent / "python3"))
    assert activity.hook_bin() == str(py)


def test_hook_bin_keeps_symlink(tmp_path, monkeypatch):
    real = _bin(tmp_path, sub="tools/voicehook-agent/bin")
    link_dir = tmp_path / "local-bin"
    link_dir.mkdir()
    (link_dir / BARE).symlink_to(real)
    monkeypatch.delenv(activity.HOOK_BIN_ENV)
    monkeypatch.setenv("PATH", str(link_dir))
    assert activity.hook_bin() == str(link_dir / BARE)  # survives `uv tool upgrade`


def test_install_writes_absolute_and_upgrades_bare_in_place(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"hooks": {"PostToolUse": [{"matcher": "*", "hooks": [
        {"type": "command", "command": "voicehook-agent-hook post-tool-use", "timeout": 5}]}],
        "Stop": [{"hooks": [{"type": "command", "command": "other-tool stop"}]}]}, "model": "x"}))
    exe = "/home/oliver/.local/bin/voicehook-agent-hook"
    rc, msg = activity.install(settings, exe=exe)
    assert rc == 0 and "updated PostToolUse" in msg and "installed" in msg
    cmds = _commands(settings)
    assert cmds["PostToolUse"] == [f"{exe} post-tool-use"]           # rewritten, not duplicated
    assert cmds["PreToolUse"] == [f"{exe} pre-tool-use || true"]
    assert cmds["Stop"] == ["other-tool stop", f"{exe} stop || true"]  # foreign hook untouched
    assert json.loads(settings.read_text())["model"] == "x"
    rc, msg = activity.install(settings, exe=exe)
    assert rc == 0 and msg.startswith("already installed")
    # another path later (moved install): rewritten again, still one entry each
    rc, msg = activity.install(settings, exe="/opt/vh/bin/voicehook-agent-hook")
    assert "updated" in msg
    assert _commands(settings)["PostToolUse"] == ["/opt/vh/bin/voicehook-agent-hook post-tool-use"]


def test_path_with_space_is_quoted(tmp_path):
    settings = tmp_path / "s.json"
    exe = "/Users/o/Application Support/bin/voicehook-agent-hook"
    activity.install(settings, exe=exe)
    cmd = _commands(settings)["Stop"][0]
    assert cmd == f"'{exe}' stop || true"
    assert activity.normalize_command(cmd) == "voicehook-agent-hook stop || true"
    assert activity.install(settings, exe=exe)[1].startswith("already installed")


def test_doctor_flags_bare_command_and_passes_absolute(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    activity.install(settings, exe=BARE)
    rc, lines = activity.doctor(settings)
    assert rc == 1 and any(ln.startswith("FAIL PostToolUse") and "PATH=/usr/bin:/bin" in ln for ln in lines)
    exe = _bin(tmp_path)
    activity.install(settings, exe=str(exe))
    rc, lines = activity.doctor(settings)
    assert rc == 0, lines
    assert all(ln.startswith("ok   ") for ln in lines) and len(lines) == len(activity.HOOK_COMMANDS)


def test_doctor_missing_events_and_unreadable(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"hooks": {}}))
    rc, lines = activity.doctor(settings)
    assert rc == 1 and all("not installed" in ln for ln in lines)
    rc, lines = activity.doctor(tmp_path / "nope.json")
    assert rc == 1 and "not readable" in lines[0]


def test_doctor_cli_exit_code(tmp_path, capsys):
    settings = tmp_path / "settings.json"
    activity.install(settings, exe=BARE)
    assert activity.main(["doctor", "--settings", str(settings)]) == 1
    out = capsys.readouterr().out
    assert "FAIL" in out and "voicehook-agent hook install" in out


def test_real_hook_binary_of_this_venv_resolves(tmp_path, monkeypatch):
    """Positive control with the real console script next to this Python."""
    monkeypatch.delenv(activity.HOOK_BIN_ENV)
    exe = activity.hook_bin()
    if exe == BARE:  # not installed as a console script here
        return
    assert os.path.isabs(exe) and activity._resolves(exe)
