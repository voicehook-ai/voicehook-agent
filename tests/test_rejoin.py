"""0.25.0 rejoin: restart the running join in place (os.execve, the self-update mechanism) so
identity, queue/spool and the status board survive, instead of leaving and re-joining.

- `rejoin` over the control socket makes the join return RC_RESTART (not a clean leave), and
  main() re-execs with the SAME --identity; the durable spool is kept (not cleared).
- the helper hub is NOT ended on a rejoin (POST /api/helper/pump/end would 410 every helper
  credential); only a real `leave` ends it.
- a running join on an OLDER CLI than this binary is flagged with a warning naming `rejoin`.
- with several local joins, a bare `rejoin` errors and lists them (--session required); no
  silent "rejoin all".
- positive control: `leave` still ends cleanly (rc 0), not RC_RESTART.
"""
from __future__ import annotations

import asyncio
import json

import pytest
from test_session import (  # noqa: F401  (fixtures + helpers)
    SLUG,
    _call,
    _FakeRoom,
    _join,
    _peer,
    fake_env,
)

from voicehook_agent import __version__, cli
from voicehook_agent import session as vs


async def _rejoin_soon(coro, cmd=None):
    """Start a join, send `cmd` (default: rejoin) over the socket, return (rc, reply)."""
    join = asyncio.ensure_future(coro)
    await asyncio.sleep(0.3)
    reply = await _call(cmd or {"cmd": "rejoin"})
    rc = await asyncio.wait_for(join, 5)
    return rc, reply


# --------------------------------------------------------------------------- #
# rejoin keeps identity + spool + board (returns RC_RESTART, re-exec same identity)
# --------------------------------------------------------------------------- #
def test_rejoin_returns_restart_and_keeps_identity(fake_env):  # noqa: F811
    _FakeRoom.peers = {"host-ut46y7": _peer("host-ut46y7", kind=0)}

    async def run():
        upd = cli._Updater()
        join = asyncio.ensure_future(_join(identity="claude-x", idle_timeout=0, updater=upd))
        await _call({"cmd": "say", "text": "Hallo"})          # arm spool, connect
        reply = await _call({"cmd": "rejoin"})
        rc = await asyncio.wait_for(join, 5)
        return rc, reply, upd

    rc, reply, upd = asyncio.run(run())
    assert rc == cli.RC_RESTART                                # re-exec, not a clean leave
    assert reply["ok"] and reply["type"] == "rejoining"
    assert reply["identity"] == "claude-x" and reply["room"] == SLUG
    assert reply["in_call"] is True and "audio gap" in reply["note"]
    assert upd.identity == "claude-x"                          # main() restarts with this identity


def test_rejoin_keeps_spool_not_cleared(fake_env):  # noqa: F811
    """The durable event spool is kept on a rejoin (like self-update), so a waiting `next`
    picks up again; a real leave clears it."""
    _FakeRoom.peers = {"host-ut46y7": _peer("host-ut46y7", kind=0)}

    async def run(cmd):
        join = asyncio.ensure_future(_join(identity="claude-x", idle_timeout=0))
        await _call({"cmd": "next", "timeout": 0})             # arm the spool
        room = _FakeRoom.instances[-1]
        room.emit("transcript", {"role": "user", "text": "noch nicht gelesen"})
        await asyncio.sleep(0.1)
        reply = await _call(cmd)
        rc = await asyncio.wait_for(join, 5)
        sess_dir = vs.session_dir(SLUG, "claude-x")
        from voicehook_agent import spool as vspool
        kept = vspool.SpoolQueue(sess_dir).resume()
        return rc, reply, kept

    rc, reply, kept = asyncio.run(run({"cmd": "rejoin"}))
    assert rc == cli.RC_RESTART and reply["type"] == "rejoining"
    assert kept >= 1                                            # the unread turn survived the rejoin
    _FakeRoom.instances = []
    rc2, _reply2, kept2 = asyncio.run(run({"cmd": "leave"}))
    assert rc2 == 0 and kept2 == 0                             # a real leave clears the spool


def test_rejoin_waiting_next_gets_restarting_reason_rejoin(fake_env):  # noqa: F811
    """A `next` blocked across the rejoin gets a {type:restarting, reason:rejoin} marker instead
    of {type:ended}, so the agent loop re-runs `next` after the re-exec."""
    _FakeRoom.peers = {"host-ut46y7": _peer("host-ut46y7", kind=0)}

    async def run():
        join = asyncio.ensure_future(_join(identity="claude-x", idle_timeout=0))
        await asyncio.sleep(0.3)
        waiting = asyncio.ensure_future(_call({"cmd": "next", "timeout": 10}))
        await asyncio.sleep(0.2)
        await _call({"cmd": "rejoin"})
        ev = await asyncio.wait_for(waiting, 5)
        return await asyncio.wait_for(join, 5), ev

    rc, ev = asyncio.run(run())
    assert rc == cli.RC_RESTART
    assert ev["type"] == "restarting" and ev["reason"] == "rejoin"
    assert "rejoins the call in place" in ev["message"]


# --------------------------------------------------------------------------- #
# positive control: leave stays a clean quit (rc 0), not RC_RESTART
# --------------------------------------------------------------------------- #
def test_leave_is_not_rejoin(fake_env):  # noqa: F811
    _FakeRoom.peers = {"host-ut46y7": _peer("host-ut46y7", kind=0)}
    rc, reply = asyncio.run(_rejoin_soon(_join(identity="claude-x", idle_timeout=0),
                                         cmd={"cmd": "leave"}))
    assert rc == 0 and reply["type"] == "leaving"
    assert vs.live_sessions() == []


# --------------------------------------------------------------------------- #
# helper hub is NOT ended on a rejoin (creds survive); only a real leave ends it
# --------------------------------------------------------------------------- #
class _FakePump:
    def __init__(self):
        self.ended = 0
        self.token = "pump-tok"

    async def end(self, http):
        self.ended += 1


def _run_with_pump(fake_env, monkeypatch, cmd):  # noqa: F811
    """Capture the _Control the join builds, attach a fake helper pump, then send `cmd`.
    Returns (rc, pump.ended): 0 means the hub was NOT ended (helper creds survive)."""
    import httpx

    _FakeRoom.peers = {"host-ut46y7": _peer("host-ut46y7", kind=0)}
    captured: list = []
    real_ctl = cli._Control

    def _spy(*a, **k):
        c = real_ctl(*a, **k)
        captured.append(c)
        return c

    monkeypatch.setattr(cli, "_Control", _spy)

    async def run():
        pump = _FakePump()
        join = asyncio.ensure_future(_join(identity="claude-x", idle_timeout=0))
        await _call({"cmd": "say", "text": "Hallo"})           # connect + ensure ctl exists
        assert captured, "no _Control captured"
        ctl = captured[-1]
        ctl.helper_pump = pump
        ctl.helper_http = httpx.AsyncClient()
        await _call(cmd)
        rc = await asyncio.wait_for(join, 5)
        return rc, pump.ended

    return asyncio.run(run())


def test_rejoin_does_not_end_helper_hub(fake_env, monkeypatch):  # noqa: F811
    rc, ended = _run_with_pump(fake_env, monkeypatch, {"cmd": "rejoin"})
    assert rc == cli.RC_RESTART and ended == 0                 # helper creds survive the rejoin


def test_leave_does_end_helper_hub(fake_env, monkeypatch):  # noqa: F811
    """Positive control: a real leave DOES end the hub (helper creds 410)."""
    rc, ended = _run_with_pump(fake_env, monkeypatch, {"cmd": "leave"})
    assert rc == 0 and ended == 1


# --------------------------------------------------------------------------- #
# version warning: a running join on an older CLI than this binary
# --------------------------------------------------------------------------- #
def test_stale_running_joins_detects_older_version(fake_env, tmp_path):  # noqa: F811
    """A live session dir whose session.json records an older cli_version is reported; the
    current version (positive control) and a newer one are not."""
    sess = vs.session_dir(SLUG, "claude-old")
    sess.mkdir(parents=True, exist_ok=True)

    # no live socket yet -> live_sessions() empty -> nothing stale (needs a running join)
    assert cli.stale_running_joins() == []

    async def run():
        join = asyncio.ensure_future(_join(identity="claude-run", idle_timeout=0))
        await _call({"cmd": "status"})                         # ensure it is live
        # overwrite its session.json to pretend it runs an older CLI
        d = vs.session_dir(SLUG, "claude-run")
        info = vs.read_info(d)
        assert info["cli_version"] == __version__              # it recorded its own version
        info["cli_version"] = "0.1.0"
        vs.write_info(d, info)
        stale = cli.stale_running_joins()
        stale_future = cli.stale_running_joins(current="0.0.1")   # binary older than the join
        await _call({"cmd": "leave"})
        await asyncio.wait_for(join, 5)
        return stale, stale_future

    stale, stale_future = asyncio.run(run())
    assert [s["session"] for s in stale] == [f"{SLUG}/claude-run"]
    assert stale[0]["version"] == "0.1.0"
    assert stale_future == []                                  # not stale against an older binary


def test_stale_join_notice_emitted(fake_env, capsys):  # noqa: F811
    cli._emit_stale_join_notice(False, [{"session": f"{SLUG}/claude-x", "version": "0.1.0"}])
    err = capsys.readouterr().err
    assert "older voicehook-agent" in err and "voicehook-agent rejoin" in err
    cli._emit_stale_join_notice(True, [{"session": f"{SLUG}/a", "version": None},
                                       {"session": f"{SLUG}/b", "version": "0.1.0"}])
    out = capsys.readouterr().out
    ev = json.loads(next(ln for ln in out.splitlines() if ln.startswith("{")))
    assert ev["_meta"] == "stale_join" and len(ev["stale"]) == 2
    assert "--session" in ev["text"]                           # several -> name --session


# --------------------------------------------------------------------------- #
# several local joins: bare rejoin errors + lists them (--session required)
# --------------------------------------------------------------------------- #
def test_rejoin_requires_session_with_several_joins(fake_env):  # noqa: F811
    _FakeRoom.peers = {"host-ut46y7": _peer("host-ut46y7", kind=0)}

    async def run():
        a = asyncio.ensure_future(_join(identity="claude-a", idle_timeout=0))
        b = asyncio.ensure_future(_join(identity="claude-b", idle_timeout=0))
        for _ in range(50):
            if len(vs.live_sessions()) == 2:
                break
            await asyncio.sleep(0.1)
        # bare rejoin: ambiguous -> SessionError listing both, nothing restarted
        with pytest.raises(vs.SessionError) as ei:
            vs.resolve_socket(None)
        # with --session it resolves to exactly one
        sa = await asyncio.to_thread(vs.resolve_socket, f"{SLUG}/claude-a", 2)
        reply_a = await asyncio.to_thread(vs.request, sa, {"cmd": "rejoin"}, 5)
        rc_a = await asyncio.wait_for(a, 5)
        # b is untouched, still live
        live_b = vs.live_sessions()
        await asyncio.to_thread(vs.request,
                                await asyncio.to_thread(vs.resolve_socket, f"{SLUG}/claude-b", 2),
                                {"cmd": "leave"}, 5)
        rc_b = await asyncio.wait_for(b, 5)
        return ei.value, reply_a, rc_a, live_b, rc_b

    err, reply_a, rc_a, live_b, rc_b = asyncio.run(run())
    assert "--session" in str(err) and f"{SLUG}/claude-a" in str(err) and f"{SLUG}/claude-b" in str(err)
    assert reply_a["type"] == "rejoining" and reply_a["identity"] == "claude-a"
    assert rc_a == cli.RC_RESTART
    assert [vs._label(d) for d in live_b] == [f"{SLUG}/claude-b"]   # only a rejoined, not all
    assert rc_b == 0


def test_rejoin_cli_flag_parses_and_routes(fake_env, monkeypatch):  # noqa: F811
    """`rejoin` is a registered subcommand routed through the one-shot client; --session is
    accepted. With no running join the client exits 3 (no-session), nothing restarted."""
    with pytest.raises(SystemExit) as ei:
        cli.main(["rejoin", "--session", f"{SLUG}/none", "--wait", "0"])
    assert ei.value.code == 3
