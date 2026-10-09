"""0.14.2 version notice: the user always sees an outdated CLI.

- cli_latest newer: stderr [warn] with the PyPI update commands, plus one
  {"_meta":"update"} line in the --json stream; the join goes on (self-update off)
- HTTP 426 (below cli_min): stderr [error] + {"_meta":"outdated"}, exit 7, no traceback
- current version: silent (positive control for the two above)
- update_commands(): the command for this install first (uv tool / pip / uvx)
"""
from __future__ import annotations

import asyncio
import json

from test_self_update import (
    BODY_426,
    NEWER,
    _leave_soon,
    _mint_426,
    _mint_with,
    _Runner,
    clean_env,  # noqa: F401  (fixture)
)
from test_session import _FakeRoom, _join, fake_env  # noqa: F401  (fixture)

from voicehook_agent import __version__, cli
from voicehook_agent import selfupdate as su

CMDS = {"uv tool install --upgrade voicehook-agent", "pip install -U voicehook-agent",
        "uvx voicehook-agent@latest"}


def _meta(out: str, kind: str) -> list[dict]:
    rows = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
    return [r for r in rows if r.get("_meta") == kind]


def test_update_commands_order(tmp_path):
    assert su.update_commands("/home/u/.cache/uv/archive-v0/abc")[0] == su.INSTALL_UVX
    assert su.update_commands("/home/u/.local/share/uv/tools/voicehook-agent")[0] == su.INSTALL_UV
    assert su.update_commands(str(tmp_path))[0] == su.INSTALL_PIP
    for p in ("/x/uv/archive-v0/a", str(tmp_path)):
        assert set(su.update_commands(p)) == CMDS and len(su.update_commands(p)) == 3


def test_newer_latest_warns_on_stderr_and_json(clean_env, monkeypatch, capsys):  # noqa: F811
    monkeypatch.setattr(cli, "_mint_token", _mint_with(NEWER))
    runner = _Runner(0)
    upd = cli._Updater(True, runner=runner, json_mode=True)  # self-update off: notice still shown
    assert asyncio.run(_leave_soon(_join(idle_timeout=0, updater=upd))) == 0
    assert runner.calls == [] and len(_FakeRoom.instances) == 1  # joined anyway
    cap = capsys.readouterr()
    assert f"[warn] voicehook-agent {NEWER} is available (this is {__version__})" in cap.err
    assert all(c in cap.err for c in CMDS)
    (ev,) = _meta(cap.out, "update")
    assert ev["role"] == "system" and ev["topic"] == "_meta"
    assert ev["latest"] == NEWER and ev["current"] == __version__ and set(ev["commands"]) == CMDS


def test_notice_also_when_self_update_runs(clean_env, monkeypatch, capsys):  # noqa: F811
    monkeypatch.setattr(cli, "_mint_token", _mint_with(NEWER))
    upd = cli._Updater(runner=_Runner(0), json_mode=True)
    assert asyncio.run(asyncio.wait_for(_join(idle_timeout=0, updater=upd), 5)) == cli.RC_RESTART
    cap = capsys.readouterr()
    assert "is available" in cap.err and len(_meta(cap.out, "update")) == 1


def test_current_version_is_silent(clean_env, monkeypatch, capsys):  # noqa: F811
    monkeypatch.setattr(cli, "_mint_token", _mint_with(__version__))
    upd = cli._Updater(True, json_mode=True)
    assert asyncio.run(_leave_soon(_join(idle_timeout=0, updater=upd))) == 0
    cap = capsys.readouterr()
    assert "available" not in cap.err and "too old" not in cap.err
    assert _meta(cap.out, "update") == [] and _meta(cap.out, "outdated") == []
    assert "connected" in cap.out  # positive control: the join ran and printed events


def test_426_clean_message_json_and_exit_7(clean_env, monkeypatch, capsys):  # noqa: F811
    monkeypatch.setattr(cli, "_mint_token", _mint_426([]))
    upd = cli._Updater(True, json_mode=True)
    assert asyncio.run(asyncio.wait_for(_join(idle_timeout=0, updater=upd), 5)) == 7
    cap = capsys.readouterr()
    assert "Traceback" not in cap.err
    assert f"[error] voicehook-agent {__version__} is too old for this server (min 0.13.0" in cap.err
    assert all(c in cap.err for c in CMDS)
    (ev,) = _meta(cap.out, "outdated")
    assert ev["cli_min"] == BODY_426["detail"]["cli_min"] and ev["exit_code"] == 7
    assert set(ev["commands"]) == CMDS
