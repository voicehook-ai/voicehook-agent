"""0.21.5: the join is the listener. Events go into a durable spool (spool.py), `next` /
`listen` / MCP `next` lease + ack them with a cursor, the join keeps the agent present while
it works, `status` reports listening / unread / last_consumed_s, and the Stop hook warns on
unread events or a dead join (listenguard.py).

Live 20.10.2026: a Claude Code agent was marked absent (listening 0, Delta "not reachable",
Stop hook) whenever it worked between two `next` calls."""
from __future__ import annotations

import asyncio
import json
import os
import stat
import subprocess
import sys
import time
import types
from types import SimpleNamespace

import pytest
from test_session import (  # noqa: F401  (fixture)
    SLUG,
    _call,
    _FakeRoom,
    _join,
    fake_env,
)

from voicehook_agent import cli, listenguard, multisession, relay, spool
from voicehook_agent import session as vs

U = {"type": "user", "role": "user", "text": "eins", "ts": 1.0}


def _u(text: str) -> dict:
    return {**U, "text": text}


def _mode(path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


# --------------------------------------------------------------------------- #
# cursor correctness (pure)
# --------------------------------------------------------------------------- #
def test_lease_ack_cursor_in_order(tmp_path):
    async def run():
        q = spool.SpoolQueue(tmp_path, armed=True)
        for t in ("a", "b", "c"):
            q.put_nowait(_u(t))
        s1, e1 = await q.lease(0)
        s2, e2 = await q.lease(0)   # a second consumer gets the NEXT one, not the same
        assert len(q) == 1 and q.unread() == 3
        assert q.ack(s2) and not q.ack(s2)          # acked twice: no-op
        cur = json.loads((tmp_path / spool.CURSOR_NAME).read_text())
        assert cur["cursor"] == 0 and cur["acked"] == [s2]  # out of order: above the cursor
        assert q.ack(s1)
        cur = json.loads((tmp_path / spool.CURSOR_NAME).read_text())
        assert cur["cursor"] == 2 and cur["acked"] == []
        s3, e3 = await q.lease(0)
        q.ack(s3)
        assert await q.lease(0) is None and q.unread() == 0
        return [e1["text"], e2["text"], e3["text"]], (s1, s2, s3)

    texts, seqs = asyncio.run(run())
    assert texts == ["a", "b", "c"] and seqs == (1, 2, 3)


def test_front_events_first_newest_first_and_superseded(tmp_path):
    async def run():
        q = spool.SpoolQueue(tmp_path, armed=True)
        q.put_nowait(_u("eins"))
        q.put_front_nowait(relay.status_request_event({"text": "Stand?"}))
        q.put_front_nowait(relay.status_request_event({"text": "und, Stand?"}))
        q.put_front_nowait({"type": "call_end", "reason": "x"})
        return [(await q.get(0)) for _ in range(3)], await q.get(0)

    got, rest = asyncio.run(run())
    assert [e["type"] for e in got] == ["call_end", "status_request", "user"]
    assert got[1]["text"] == "und, Stand?"   # the older status_request was superseded
    assert rest is None


def test_unacked_lease_comes_again_after_lease_s(tmp_path):
    t = [100.0]

    async def run():
        q = spool.SpoolQueue(tmp_path, armed=True, clock=lambda: t[0])
        q.put_nowait(_u("hallo"))
        seq, _ = await q.lease(0)                  # consumer dies before the ack
        assert await q.lease(0) is None            # still handed out
        t[0] += spool.LEASE_S + 0.1
        again = await q.lease(0)
        q.ack(again[0])
        return seq, again

    seq, again = asyncio.run(run())
    assert again[0] == seq and again[1]["text"] == "hallo"


def test_blocked_lease_wakes_on_put_and_ends_on_close(tmp_path):
    async def run():
        q = spool.SpoolQueue(tmp_path, armed=True)
        waiter = asyncio.create_task(q.lease(5))
        await asyncio.sleep(0.05)
        q.put_nowait(_u("da"))
        got = await asyncio.wait_for(waiter, 2)
        q.ack(got[0])
        ender = asyncio.create_task(q.lease(5))
        await asyncio.sleep(0.05)
        await q.close()
        return got, await asyncio.wait_for(ender, 2)

    got, ended = asyncio.run(run())
    assert got[1]["text"] == "da" and ended == (None, {"type": "ended"})


def test_not_armed_collects_nothing_and_is_bounded(tmp_path):
    async def run():
        q = spool.SpoolQueue(tmp_path, maxlen=3)
        q.put_nowait(_u("vorher"))
        q.arm()
        for i in range(5):
            q.put_nowait(_u(str(i)))
        return [(await q.get(0))["text"] for _ in range(3)], q.dropped

    texts, dropped = asyncio.run(run())
    assert texts == ["2", "3", "4"] and dropped == 2


def test_compaction_keeps_unread_only(tmp_path, monkeypatch):
    monkeypatch.setattr(spool, "COMPACT_AT", 5)

    async def run():
        q = spool.SpoolQueue(tmp_path, armed=True)
        for i in range(8):
            q.put_nowait(_u(str(i)))
        for _ in range(6):
            await q.get(0)
        return q

    q = asyncio.run(run())
    lines = (tmp_path / spool.EVENTS_NAME).read_text().splitlines()
    assert len(lines) <= 3 and q.unread() == 2
    assert spool.unread_on_disk(tmp_path) == (2, 2)


# --------------------------------------------------------------------------- #
# file permissions, cleanup, restart of the join
# --------------------------------------------------------------------------- #
def test_spool_files_private(tmp_path):
    d = tmp_path / "sess"
    d.mkdir(mode=0o755)
    old = os.umask(0)
    try:
        async def run():
            q = spool.SpoolQueue(d)
            q.resume()
            q.arm()
            q.put_nowait(_u("geheim"))
            await q.get(0)
        asyncio.run(run())
    finally:
        os.umask(old)
    assert _mode(d / spool.EVENTS_NAME) == 0o600
    assert _mode(d / spool.CURSOR_NAME) == 0o600
    assert _mode(d) == 0o700


def test_spool_never_follows_a_symlink(tmp_path):
    target = tmp_path / "elsewhere"
    target.write_text("")
    d = tmp_path / "sess"
    d.mkdir()
    (d / spool.EVENTS_NAME).symlink_to(target)

    async def run():
        q = spool.SpoolQueue(d, armed=True)
        q.put_nowait(_u("x"))
        return await q.get(0)

    assert asyncio.run(run())["text"] == "x"      # memory fallback, the call goes on
    assert target.read_text() == ""


def test_resume_after_join_restart_and_stale_spool_dropped(tmp_path):
    async def first():
        q = spool.SpoolQueue(tmp_path, armed=True)
        for t in ("a", "b", "c"):
            q.put_nowait(_u(t))
        q.put_front_nowait({"type": "restarting", "reason": "self_update"})
        await q.get(0)            # the restarting marker, to the old waiting `next`
        seq, _ = await q.lease(0)
        q.ack(seq)                # "a" consumed
        await q.lease(0)          # "b" handed out, never acked: the consumer died

    async def second():
        q = spool.SpoolQueue(tmp_path)
        n = q.resume()
        texts = []
        while (ev := await q.get(0)) is not None:
            texts.append(ev["text"])
        q.put_nowait(_u("neu"))
        return n, texts, (await q.lease(0))

    asyncio.run(first())
    n, texts, nxt = asyncio.run(second())
    assert n == 2 and texts == ["b", "c"]
    assert nxt[0] > 4             # seq never reused after a restart

    os.utime(tmp_path / spool.EVENTS_NAME, (time.time() - spool.RESUME_S - 5,) * 2)
    q = spool.SpoolQueue(tmp_path)
    assert q.resume() == 0 and not (tmp_path / spool.EVENTS_NAME).exists()


# --------------------------------------------------------------------------- #
# end to end against a real join (fake room, real control socket)
# --------------------------------------------------------------------------- #
def _sess_dir(ident: str):
    return vs.session_dir(SLUG, ident)


@pytest.mark.usefixtures("fake_env")
def test_no_event_loss_across_consumer_restarts(monkeypatch):
    """A consumer that dies after the join handed it an event (no ack) loses nothing: the
    next consumer gets it again; acked events never come twice; across many restarts every
    turn arrives exactly once."""
    monkeypatch.setattr(spool, "LEASE_S", 0.3)

    async def run():
        join = asyncio.create_task(_join(idle_timeout=0))
        st = await _call({"cmd": "status"})
        await _call({"cmd": "say", "text": "hallo"})                 # arms the spool
        room = _FakeRoom.instances[0]
        for i in range(6):
            room.emit("transcript", {"role": "user", "text": f"satz {i}"})
        await asyncio.sleep(0.05)
        d = _sess_dir(st["identity"])
        on_disk = spool.unread_on_disk(d)
        lost = await _call({"cmd": "next", "timeout": 1, "lease": True})   # killed: no ack
        got = []
        for _ in range(10):  # every following consumer is a fresh process: lease, print, ack
            r = await _call({"cmd": "next", "timeout": 1, "lease": True})
            if r["type"] == "timeout":
                break
            got.append(r["text"])
            await _call({"cmd": "ack", "seq": r["seq"]})
        st2 = await _call({"cmd": "status"})
        await _call({"cmd": "leave"})
        await asyncio.wait_for(join, 10)
        return lost, got, st2, on_disk, d

    lost, got, st2, on_disk, d = asyncio.run(run())
    assert on_disk == (6, 6)
    assert lost["text"] == "satz 0" and "seq" in lost
    assert sorted(got) == [f"satz {i}" for i in range(6)]   # satz 0 came back, nothing twice
    assert len(got) == len(set(got))
    assert st2["unread"] == 0 and st2["last_consumed_s"] is not None
    assert not (d / spool.EVENTS_NAME).exists()             # leave: no turn stays on disk


@pytest.mark.usefixtures("fake_env")
def test_listen_cli_acks_after_print_and_restarted_listen_gets_the_rest(capsys):
    """`voicehook-agent listen` as separate consumer runs: each run prints exactly one event
    and acks it; the next run continues at the cursor."""
    async def run():
        join = asyncio.create_task(_join(idle_timeout=0))
        await _call({"cmd": "say", "text": "hallo"})
        room = _FakeRoom.instances[0]
        room.emit("transcript", {"role": "user", "text": "erste"})
        room.emit("transcript", {"role": "user", "text": "zweite"})
        await asyncio.sleep(0.05)
        args = types.SimpleNamespace(session=None, wait=1.0, timeout=2.0, poll=10.0)
        rc1 = await asyncio.to_thread(cli._run_listen, args)
        rc2 = await asyncio.to_thread(cli._run_listen, args)
        st = await _call({"cmd": "status"})
        await _call({"cmd": "leave"})
        await asyncio.wait_for(join, 10)
        return rc1, rc2, st

    rc1, rc2, st = asyncio.run(run())
    lines = [json.loads(x) for x in capsys.readouterr().out.strip().splitlines()]
    texts = [x["text"] for x in lines if x.get("type") == "user"]
    assert (rc1, rc2) == (0, 0) and texts == ["erste", "zweite"]
    assert st["unread"] == 0 and st["pending"] == 0


def test_presence_held_while_no_consumer_polls_for_120s(monkeypatch):
    """The agent works for 120 s without say/next: the join still sends operator.alive every
    interval with idle_s 0 (the server's reachable + polling rule), status says listening."""
    from voicehook_agent import session as vsession

    class _Local:
        def __init__(self):
            self.sent = []

        async def publish_data(self, data, reliable=True, topic=""):
            self.sent.append((topic, json.loads(data)))

    async def run():
        ctl = cli._Control(SLUG, "claude-x", relay.SayTracker(), relay.EchoSuppressor(False), 0.0)
        lp = _Local()
        ctl.attach(SimpleNamespace(local_participant=lp, remote_participants={}))
        ctl.alive_interval = 0.05
        await cli._control_handler(ctl, {"cmd": "next", "timeout": 0})   # first next: armed
        ctl.watchdog = vsession.IdleWatchdog(timeout=0.0, last=time.monotonic() - 120.0)
        task = asyncio.create_task(cli._alive_loop(ctl))
        await asyncio.sleep(0.4)
        st = await cli._control_handler(ctl, {"cmd": "status"})
        ctl.quit.set()
        await task
        return [p for t, p in lp.sent if t == "operator.alive"], st

    pkts, st = asyncio.run(run())
    assert len(pkts) >= 3
    assert all(p["alive"] is True and p["idle_s"] == 0.0 for p in pkts)
    assert all(p["consumer_idle_s"] >= 120.0 for p in pkts)
    assert st["listening"] is True and st["consumers"] == 0 and st["idle_s"] >= 120.0


@pytest.mark.usefixtures("fake_env")
def test_status_counts_unread_wake_and_last_consumed():
    async def run():
        join = asyncio.create_task(_join(idle_timeout=0))
        await _call({"cmd": "say", "text": "hallo"})
        room = _FakeRoom.instances[0]
        room.emit("transcript", {"role": "user", "text": "frage"})
        await asyncio.sleep(0.05)
        st1 = await _call({"cmd": "status"})
        await _call({"cmd": "next", "timeout": 0})
        st2 = await _call({"cmd": "status"})
        await _call({"cmd": "leave"})
        await asyncio.wait_for(join, 10)
        return st1, st2

    st1, st2 = asyncio.run(run())
    assert st1["listening"] is True and st1["unread"] == 1 and st1["unread_wake"] == 1
    assert st1["last_consumed_s"] is None
    assert st2["unread"] == 0 and isinstance(st2["last_consumed_s"], float)


# --------------------------------------------------------------------------- #
# Stop hook guard (0.21.5)
# --------------------------------------------------------------------------- #
NOW = 1_000_000.0


def _st(**kw):
    st = {"ok": True, "type": "status", "connected": True, "pending": 0, "listening": True,
          "consumers": 0, "unread": 0, "unread_wake": 0, "listen_idle_s": 1.0, "leaving": False}
    st.update(kw)
    return st


def test_guard_blocks_on_unread_wake_events_without_consumer():
    out = listenguard.decide({}, _st(unread=2, unread_wake=2, pending=2), None, NOW)
    assert out["decision"] == "block" and "2 events for you wait unread" in out["reason"]
    assert listenguard.LISTEN_CMD in out["reason"] and "run_in_background" in out["reason"]


def test_guard_lets_stop_through_while_working_with_listener_or_nothing_unread():
    assert listenguard.decide({}, _st(consumers=1, unread_wake=3), None, NOW) is None
    assert listenguard.decide({}, _st(unread=1, unread_wake=0), None, NOW) is None  # human_state
    assert listenguard.decide({}, _st(leaving=True, unread_wake=1), None, NOW) is None


def test_guard_old_join_status_judged_as_before():
    old = {"ok": True, "connected": True, "pending": 1, "listening": 0, "listen_idle_s": 30.0}
    assert listenguard.decide({}, old, None, NOW)["decision"] == "block"
    assert listenguard.decide({}, {**old, "listening": 1}, None, NOW) is None


@pytest.mark.usefixtures("fake_env")
def test_guard_blocks_once_for_a_dead_join(monkeypatch, tmp_path):
    sid = "sess-dead-join"
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", sid)
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    d = vs.session_dir(SLUG, "claude-dead")
    d.mkdir(parents=True)
    vs.write_info(d, {"pid": dead.pid, "owner_session": multisession.own_session_hash()})

    async def fill():
        q = spool.SpoolQueue(d, armed=True)
        q.put_nowait(_u("hallo?"))
    asyncio.run(fill())
    ev = json.dumps({"hook_event_name": "Stop", "session_id": sid})
    first = listenguard.handle(ev)
    second = listenguard.handle(ev)
    foreign = listenguard.handle(json.dumps({"hook_event_name": "Stop", "session_id": "x"}))
    assert first["decision"] == "block" and "ended without leaving" in first["reason"]
    assert "1 unread event" in first["reason"] and listenguard.LISTEN_CMD in first["reason"]
    assert second is None and foreign is None


@pytest.mark.usefixtures("fake_env")
def test_clean_leave_leaves_no_dead_join(monkeypatch):
    sid = "sess-clean"
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", sid)

    async def run():
        join = asyncio.create_task(_join(idle_timeout=0))
        await _call({"cmd": "say", "text": "hallo"})
        _FakeRoom.instances[0].emit("transcript", {"role": "user", "text": "tschüss"})
        await asyncio.sleep(0.05)
        await _call({"cmd": "leave"})
        await asyncio.wait_for(join, 10)

    asyncio.run(run())
    assert listenguard.dead_join_dirs({"session_id": sid}) == []
    assert listenguard.handle(json.dumps({"hook_event_name": "Stop", "session_id": sid})) is None


def test_listen_poll_default_10(capsys):
    assert cli.LISTEN_POLL_S == 10.0
    with pytest.raises(SystemExit):
        cli.main(["listen", "--help"])
    assert "default 10)" in " ".join(capsys.readouterr().out.split())
