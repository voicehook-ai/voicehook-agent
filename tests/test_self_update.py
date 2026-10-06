"""0.12.0 self-update + server version gate.

- version compare (numeric), decision matrix (update / current / opt-out / restarted)
- install method + upgrade command (uv, pip in venv, pip --user + PEP 668)
- run_upgrade never raises (mock subprocess), restart = execve with loop marker
- join: cli_latest newer -> update + restart, never connected (WebRTC) / left (bridge)
- no loop: restarted join never updates again; opt-out; failed update goes on
- 426: upgrade command printed, one self-update, else exit 7
- X-VH-CLI is sent on the token mint and the bridge join
- positive control: cli_latest == own version -> plain join, no updater call
"""
from __future__ import annotations

import asyncio
import subprocess

import httpx
import pytest
from test_session import (  # noqa: F401  (fixture)
    _call,
    _FakeRoom,
    _join,
    _peer,
    fake_env,
)

from voicehook_agent import __version__, cli
from voicehook_agent import selfupdate as su

HUMAN = {"host-ut46y7": _peer("host-ut46y7", kind=0)}
NEWER = "9.9.9"


class _Runner:
    """subprocess.run stand-in: records argv, returns the given exit code."""

    def __init__(self, rc: int = 0, delay: float = 0.0, exc: Exception | None = None):
        self.rc, self.delay, self.exc = rc, delay, exc
        self.calls: list[list[str]] = []

    def __call__(self, cmd, **kw):
        import time
        self.calls.append(list(cmd))
        if self.delay:
            time.sleep(self.delay)
        if self.exc:
            raise self.exc
        return subprocess.CompletedProcess(cmd, self.rc, stdout="boom\nlast line")


@pytest.fixture
def clean_env(fake_env, monkeypatch, tmp_path):  # noqa: F811
    monkeypatch.delenv(su.ENV_MARKER, raising=False)
    monkeypatch.delenv(su.ENV_OPT_OUT, raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    _FakeRoom.peers = HUMAN
    return fake_env


def _mint_with(latest, calls=None):
    async def mint(*a, **k):
        if calls is not None:
            calls.append(1)
        return {"url": "wss://fake", "token": "t", "cli_min": "0.10.2", "cli_latest": latest}
    return mint


async def _leave_soon(coro):
    join = asyncio.ensure_future(coro)
    await asyncio.sleep(0.3)
    await _call({"cmd": "leave"})
    return await asyncio.wait_for(join, 5)


# --------------------------------------------------------------------------- #
# pure logic
# --------------------------------------------------------------------------- #
def test_version_compare_is_numeric():
    assert su.parse_version("0.12.0") == (0, 12, 0)
    assert su.parse_version("0.12") == (0, 12, 0)
    assert su.parse_version("0.12.0.dev3") == (0, 12, 0)
    assert su.parse_version("kaputt") is None
    assert su.is_newer("0.10.2", "0.9.0")       # lexically "0.10" < "0.9"
    assert su.is_newer("0.12.1", "0.12.0")
    assert not su.is_newer("0.12.0", "0.12.0")
    assert not su.is_newer("0.11.0", "0.12.0")
    assert not su.is_newer(None, "0.12.0") and not su.is_newer("x", "0.12.0")


@pytest.mark.parametrize("latest,opt,restarted,want", [
    ("0.13.0", False, False, "update"),
    ("0.12.0", False, False, "current"),
    (None, False, False, "current"),            # old server without cli_latest
    ("0.13.0", True, False, "opt-out"),
    ("0.13.0", False, True, "restarted"),       # loop guard
])
def test_decide(latest, opt, restarted, want):
    assert su.decide(latest, "0.12.0", opt_out=opt, restarted=restarted) == want


def test_opt_out_flag_and_env():
    assert su.opted_out(True, {})
    assert su.opted_out(False, {su.ENV_OPT_OUT: "1"})
    assert su.opted_out(False, {su.ENV_OPT_OUT: "true"})
    assert not su.opted_out(False, {su.ENV_OPT_OUT: "0"})
    assert not su.opted_out(False, {})
    assert su.already_restarted({su.ENV_MARKER: "0.12.0"}) and not su.already_restarted({})


def test_install_method(tmp_path):
    uvdir = tmp_path / "uv" / "tools" / "voicehook-agent"
    uvdir.mkdir(parents=True)
    assert su.install_method(str(uvdir)) == "uv"
    receipt = tmp_path / "custom"
    receipt.mkdir()
    (receipt / "uv-receipt.toml").write_text("[tool]\n")
    assert su.install_method(str(receipt)) == "uv"
    assert su.install_method(str(tmp_path / "venv")) == "pip"


def test_upgrade_command_variants():
    assert su.upgrade_command("uv", which=lambda n: "/x/uv") == ["/x/uv", "tool", "upgrade", "voicehook-agent"]
    assert su.upgrade_command("uv", which=lambda n: None) is None
    venv = su.upgrade_command("pip", executable="/v/bin/python", prefix="/v", base_prefix="/usr")
    assert venv == ["/v/bin/python", "-m", "pip", "install", "--upgrade", "--quiet", su.REPO]
    system = su.upgrade_command("pip", executable="/usr/bin/python3", prefix="/usr",
                                base_prefix="/usr", pep668=True)
    assert system[-3:] == ["--user", "--break-system-packages", su.REPO]
    plain = su.upgrade_command("pip", executable="/usr/bin/python3", prefix="/usr",
                               base_prefix="/usr", pep668=False)
    assert "--user" in plain and "--break-system-packages" not in plain


def test_run_upgrade_mocked_subprocess():
    warns: list[str] = []
    ok = _Runner(0)
    assert su.run_upgrade(["uv", "tool", "upgrade", "voicehook-agent"], runner=ok, warn=warns.append)
    assert ok.calls == [["uv", "tool", "upgrade", "voicehook-agent"]] and not warns
    assert not su.run_upgrade(["x"], runner=_Runner(1), warn=warns.append)
    assert "exit 1" in warns[-1] and "uv tool upgrade voicehook-agent" in warns[-1]
    assert not su.run_upgrade(["x"], runner=_Runner(exc=OSError("nope")), warn=warns.append)
    assert not su.run_upgrade(["x"], runner=_Runner(exc=subprocess.TimeoutExpired("x", 1)),
                              warn=warns.append)
    assert not su.run_upgrade(None, runner=ok, warn=warns.append)  # no uv in a uv venv
    assert len(ok.calls) == 1


def test_restart_sets_marker_keeps_args_and_identity(tmp_path):
    exe = tmp_path / "voicehook-agent"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    seen = {}

    def execve(path, args, env):
        seen.update(path=path, args=args, env=env)

    argv = [str(exe), "join", "https://x/r/a-b-c-AB12?invite=q", "--name", "N", "--model", "m", "--json"]
    su.restart(argv, "0.12.0", identity="n-host-ab12", execve=execve, env={"PATH": "/bin"})
    assert seen["path"] == str(exe)
    assert seen["args"] == [str(exe), *argv[1:], "--identity", "n-host-ab12"]
    assert seen["env"][su.ENV_MARKER] == "0.12.0" and seen["env"]["PATH"] == "/bin"
    # an explicit --identity is kept as is, never doubled
    su.restart([str(exe), "join", "u", "--identity", "mine"], "0.12.0", identity="x",
               execve=execve, env={})
    assert seen["args"].count("--identity") == 1 and seen["args"][-1] == "mine"


def test_version_text_hint_from_last_server_answer(tmp_path):
    env = {"XDG_CACHE_HOME": str(tmp_path)}
    assert su.version_text("0.12.0", env) == "voicehook-agent 0.12.0"
    su.remember({"cli_min": "0.10.2", "cli_latest": "0.13.0"}, env)
    assert "neue Version verfügbar: 0.13.0" in su.version_text("0.12.0", env)
    su.remember({"cli_min": "0.10.2", "cli_latest": "0.12.0"}, env)
    assert su.version_text("0.12.0", env) == "voicehook-agent 0.12.0"


# --------------------------------------------------------------------------- #
# join: self-update decision
# --------------------------------------------------------------------------- #
def test_join_updates_and_restarts_before_connecting(clean_env, monkeypatch):
    monkeypatch.setattr(cli, "_mint_token", _mint_with(NEWER))
    runner = _Runner(0)
    upd = cli._Updater(runner=runner)
    rc = asyncio.run(asyncio.wait_for(_join(idle_timeout=0, updater=upd), 5))
    assert rc == cli.RC_RESTART and upd.restart
    assert len(runner.calls) == 1
    assert _FakeRoom.instances == []          # never joined the room with the old CLI
    assert upd.identity                        # main() restarts with the same identity


def test_positive_control_current_version_joins_plain(clean_env, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_mint_token", _mint_with(__version__))
    runner = _Runner(0)
    rc = asyncio.run(_leave_soon(_join(idle_timeout=0, updater=cli._Updater(runner=runner))))
    assert rc == 0 and runner.calls == [] and len(_FakeRoom.instances) == 1
    assert "self-update" not in capsys.readouterr().err


def test_restarted_join_never_updates_again(clean_env, monkeypatch, capsys):
    monkeypatch.setenv(su.ENV_MARKER, "0.11.0")
    monkeypatch.setattr(cli, "_mint_token", _mint_with(NEWER))
    runner = _Runner(0)

    async def run():
        return await _leave_soon(_join(idle_timeout=0, updater=cli._Updater(runner=runner)))

    assert asyncio.run(run()) == 0
    assert runner.calls == [] and len(_FakeRoom.instances) == 1
    assert "still" in capsys.readouterr().err


@pytest.mark.parametrize("how", ["flag", "env"])
def test_opt_out(clean_env, monkeypatch, capsys, how):
    if how == "env":
        monkeypatch.setenv(su.ENV_OPT_OUT, "1")
    monkeypatch.setattr(cli, "_mint_token", _mint_with(NEWER))
    runner = _Runner(0)
    upd = cli._Updater(how == "flag", runner=runner)
    assert asyncio.run(_leave_soon(_join(idle_timeout=0, updater=upd))) == 0
    assert runner.calls == [] and len(_FakeRoom.instances) == 1
    assert "voicehook-agent self-update" in capsys.readouterr().err


def test_failed_update_warns_and_joins_with_old_version(clean_env, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_mint_token", _mint_with(NEWER))
    runner = _Runner(1)
    upd = cli._Updater(runner=runner)
    assert asyncio.run(_leave_soon(_join(idle_timeout=0, updater=upd))) == 0
    assert len(runner.calls) == 1 and len(_FakeRoom.instances) == 1 and not upd.restart
    err = capsys.readouterr().err
    assert "self-update failed" in err and f"continuing with voicehook-agent {__version__}" in err


def test_reconnect_does_not_update_mid_call(clean_env, monkeypatch):
    """Only the first connect may update: a reconnect inside a running call never restarts."""
    answers = iter([__version__, NEWER, NEWER])

    async def mint(*a, **k):
        return {"url": "wss://fake", "token": "t", "cli_latest": next(answers)}

    monkeypatch.setattr(cli, "_mint_token", mint)
    runner = _Runner(0)

    async def run():
        join = asyncio.ensure_future(_join(idle_timeout=0, updater=cli._Updater(runner=runner)))
        await asyncio.sleep(0.3)
        _FakeRoom.instances[0].handlers["disconnected"](9)  # SIGNAL_CLOSE: transient
        await asyncio.sleep(1.6)                              # backoff 1 s, then rejoin
        await _call({"cmd": "leave"})
        return await asyncio.wait_for(join, 5)

    assert asyncio.run(run()) == 0
    assert runner.calls == [] and len(_FakeRoom.instances) == 2


def test_waiting_next_gets_restarting_not_ended(clean_env, monkeypatch):
    monkeypatch.setattr(cli, "_mint_token", _mint_with(NEWER))
    runner = _Runner(0, delay=0.8)

    async def run():
        join = asyncio.ensure_future(_join(idle_timeout=0, updater=cli._Updater(runner=runner)))
        await asyncio.sleep(0.3)
        ev = await _call({"cmd": "next", "timeout": 10})
        return await asyncio.wait_for(join, 5), ev

    rc, ev = asyncio.run(run())
    assert rc == cli.RC_RESTART
    assert ev["type"] == "restarting" and "next again" in ev["message"]


# --------------------------------------------------------------------------- #
# 426 Upgrade Required
# --------------------------------------------------------------------------- #
BODY_426 = {"detail": {"message": "too old", "error": "cli_outdated", "cli_min": "0.13.0",
                       "cli_latest": "0.13.1", "upgrade": "uv tool upgrade voicehook-agent"}}


def _mint_426(calls):
    async def mint(*a, **k):
        calls.append(1)
        raise cli.TokenMintError(426, "{...}", BODY_426)
    return mint


def test_426_opt_out_prints_command_and_exits_7(clean_env, monkeypatch, capsys):
    calls: list = []
    monkeypatch.setattr(cli, "_mint_token", _mint_426(calls))
    runner = _Runner(0)
    rc = asyncio.run(asyncio.wait_for(
        _join(idle_timeout=0, updater=cli._Updater(True, runner=runner)), 5))
    assert rc == su.EXIT_OUTDATED == 7
    assert runner.calls == [] and len(calls) == 1 and _FakeRoom.instances == []
    err = capsys.readouterr().err
    assert "too old for this server (min 0.13.0" in err and "uv tool upgrade voicehook-agent" in err


def test_426_self_update_then_restart(clean_env, monkeypatch):
    monkeypatch.setattr(cli, "_mint_token", _mint_426([]))
    runner = _Runner(0)
    rc = asyncio.run(asyncio.wait_for(_join(idle_timeout=0, updater=cli._Updater(runner=runner)), 5))
    assert rc == cli.RC_RESTART and len(runner.calls) == 1


def test_426_failed_update_exits_7_once(clean_env, monkeypatch):
    calls: list = []
    monkeypatch.setattr(cli, "_mint_token", _mint_426(calls))
    runner = _Runner(1)
    rc = asyncio.run(asyncio.wait_for(_join(idle_timeout=0, updater=cli._Updater(runner=runner)), 5))
    assert rc == 7 and len(runner.calls) == 1 and len(calls) == 1  # no retry loop


def test_426_after_restart_exits_7_without_update(clean_env, monkeypatch):
    monkeypatch.setenv(su.ENV_MARKER, "0.11.0")
    monkeypatch.setattr(cli, "_mint_token", _mint_426([]))
    runner = _Runner(0)
    rc = asyncio.run(asyncio.wait_for(_join(idle_timeout=0, updater=cli._Updater(runner=runner)), 5))
    assert rc == 7 and runner.calls == []


def test_bridge_426_and_bridge_update(clean_env, monkeypatch):
    class _Old(_FakeRoom):
        async def connect(self, url=None, token=None):
            raise cli.vtransport.BridgeError("HTTP 426", status=426, body=BODY_426)

    monkeypatch.setattr(cli.vtransport, "BridgeRoom", lambda *a, **k: _Old())
    rc = asyncio.run(asyncio.wait_for(
        _join(idle_timeout=0, transport="bridge", updater=cli._Updater(True)), 5))
    assert rc == 7

    left = []

    class _New(_FakeRoom):
        cli_min, cli_latest = "0.10.2", NEWER

        async def connect(self, url=None, token=None):
            return None

        async def disconnect(self):
            left.append(1)

    monkeypatch.setattr(cli.vtransport, "BridgeRoom", lambda *a, **k: _New())
    runner = _Runner(0)
    rc = asyncio.run(asyncio.wait_for(
        _join(idle_timeout=0, transport="bridge", updater=cli._Updater(runner=runner)), 5))
    assert rc == cli.RC_RESTART and len(runner.calls) == 1 and left  # bridge session left


# --------------------------------------------------------------------------- #
# wire: X-VH-CLI header, main() restart, self-update command
# --------------------------------------------------------------------------- #
def test_token_mint_sends_x_vh_cli_and_parses_426(monkeypatch):
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.headers)
        if len(seen) == 1:
            return httpx.Response(200, json={"url": "u", "token": "t", "cli_latest": "0.12.0"})
        return httpx.Response(426, json=BODY_426)

    real = httpx.AsyncClient
    monkeypatch.setattr(cli.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    tok = asyncio.run(cli._mint_token("https://x", "a-b-c-AB12", "id"))
    assert tok["cli_latest"] == "0.12.0"
    assert seen[0]["x-vh-cli"] == __version__
    assert seen[0]["user-agent"] == f"voicehook-agent/{__version__}"
    with pytest.raises(cli.TokenMintError) as ei:
        asyncio.run(cli._mint_token("https://x", "a-b-c-AB12", "id"))
    assert ei.value.status == 426 and ei.value.body == BODY_426


def test_bridge_client_sends_x_vh_cli():
    c = cli.vtransport._client("voicehook-agent/x")
    try:
        assert c.headers["x-vh-cli"] == __version__
    finally:
        asyncio.run(c.aclose())


def test_main_restart_after_update(tmp_path):
    seen = {}
    upd = cli._Updater()
    upd.identity = "claude-host-ab12"
    cli._restart_after_update(["join", "u", "--name", "N", "--model", "m"], upd,
                              execve=lambda p, a, e: seen.update(a=a, e=e))
    assert seen["a"][1:] == ["join", "u", "--name", "N", "--model", "m",
                             "--identity", "claude-host-ab12"]
    assert seen["e"][su.ENV_MARKER] == __version__


def test_self_update_command(monkeypatch, capsys):
    runner = _Runner(0)
    assert cli._run_self_update(runner=runner) == 0 and len(runner.calls) == 1
    assert cli._run_self_update(runner=_Runner(1)) == 1
    assert cli._run_self_update(dry_run=True, runner=runner) == 0 and len(runner.calls) == 1
    assert "install via" in capsys.readouterr().out


def test_cli_flags_parse(monkeypatch):
    """--no-self-update reaches the updater; `self-update --dry-run` exits 0."""
    got = {}

    async def fake_join(*a, **k):
        got["opt_out"] = k["updater"].opt_out
        return 0

    monkeypatch.setattr(cli, "_join", fake_join)
    monkeypatch.delenv(su.ENV_OPT_OUT, raising=False)
    with pytest.raises(SystemExit) as ei:
        cli.main(["join", "https://x/r/a-b-c-AB12", "--name", "N", "--model", "m", "--no-self-update"])
    assert ei.value.code == 0 and got["opt_out"] is True
    with pytest.raises(SystemExit) as ei:
        cli.main(["self-update", "--dry-run"])
    assert ei.value.code == 0


def test_bridge_room_reads_versions_and_426_body():
    def ok(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"session": "s", "cli_min": "0.10.2", "cli_latest": "0.13.0"})

    def old(req: httpx.Request) -> httpx.Response:
        return httpx.Response(426, json=BODY_426)

    async def run(handler):
        room = cli.vtransport.BridgeRoom(
            "https://x", "a-b-c-AB12", "id", name="N", model="m",
            client_factory=lambda ua, timeout=15.0: httpx.AsyncClient(
                transport=httpx.MockTransport(handler)))
        try:
            await room.connect()
            return room
        finally:
            if room._sse_task is not None:
                room._sse_task.cancel()
            await room._close_http()

    room = asyncio.run(run(ok))
    assert (room.cli_min, room.cli_latest) == ("0.10.2", "0.13.0")
    with pytest.raises(cli.vtransport.BridgeError) as ei:
        asyncio.run(run(old))
    assert ei.value.status == 426 and ei.value.body == BODY_426
