"""0.8.0: orphaned joins end themselves (idle guard across reconnects, owner/holder
gone, no 24 h exit hang on a held FIFO) and the join sends `operator.alive`."""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import textwrap
import time

import pytest

from voicehook_agent import cli
from test_session import _FakeRoom, _call, _join, fake_env  # noqa: F401  (fixture)

SIGNAL_CLOSE = 9  # transient LiveKit disconnect reason -> CLI reconnects


def _alive(room):
    return room.topics("operator.alive")


# --------------------------------------------------------------------------- #
# idle guard spans reconnects
# --------------------------------------------------------------------------- #
def test_orphan_leaves_after_idle_timeout_despite_reconnect(fake_env):
    async def run():
        t0 = time.monotonic()
        join = asyncio.create_task(_join(idle_timeout=2.0, idle_say="Ich gehe."))
        await asyncio.sleep(0.3)
        _FakeRoom.instances[0].handlers["disconnected"](SIGNAL_CLOSE)  # transient
        rc = await asyncio.wait_for(join, 10)
        return rc, time.monotonic() - t0

    rc, took = asyncio.run(run())
    assert rc == 0
    assert len(_FakeRoom.instances) >= 2, "no reconnect happened"
    last = _FakeRoom.instances[-1]
    assert last.topics("operator.say")[-1]["text"] == "Ich gehe."
    # leave = idle (2 s, 0.5 s ticks) + 1 s announce pause <= ~3.5 s. Had the
    # reconnect (~1.3 s) reset the timer it would be >= 1.3 + 2 + 1 = 4.3 s.
    assert took < 3.9, took


def test_positive_control_active_brain_survives_reconnect(fake_env):
    async def run():
        join = asyncio.create_task(_join(idle_timeout=1.0, idle_say=None))
        await asyncio.sleep(0.2)
        _FakeRoom.instances[0].handlers["disconnected"](SIGNAL_CLOSE)
        for _ in range(8):                       # 2.4 s of activity > 1 s timeout
            await _call({"cmd": "next", "timeout": 0.3})
        alive = not join.done()
        await _call({"cmd": "leave"})
        return alive, await asyncio.wait_for(join, 10)

    alive, rc = asyncio.run(run())
    assert alive and rc == 0


def test_blocked_next_cannot_outlive_idle_timeout(fake_env):
    async def run():
        join = asyncio.create_task(_join(idle_timeout=0.5, idle_say=None))
        t0 = time.monotonic()
        res = await _call({"cmd": "next", "timeout": 3600})
        took = time.monotonic() - t0
        rc = await asyncio.wait_for(join, 10)
        return res, took, rc

    res, took, rc = asyncio.run(run())
    assert res["type"] in ("timeout", "ended") and took < 2.0 and rc == 0


# --------------------------------------------------------------------------- #
# owner / holder gone
# --------------------------------------------------------------------------- #
def test_owner_pid_gone_leaves_with_announcement(fake_env, monkeypatch):
    monkeypatch.setattr(cli, "OWNER_POLL", 0.1)
    owner = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])

    async def run():
        join = asyncio.create_task(_join(idle_timeout=0, owner_pids=[owner.pid],
                                         owner_say="Sitzung weg."))
        await asyncio.sleep(0.4)
        still = not join.done()
        owner.kill()
        owner.wait()
        return still, await asyncio.wait_for(join, 10)

    try:
        still, rc = asyncio.run(run())
    finally:
        owner.kill()
    room = _FakeRoom.instances[0]
    assert still and rc == 0
    assert room.topics("operator.say")[-1]["text"] == "Sitzung weg."
    assert _alive(room)[-1]["alive"] is False


def test_dead_fifo_holder_is_detected_from_agent_home(fake_env, monkeypatch):
    monkeypatch.setattr(cli, "OWNER_POLL", 0.1)
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    fake_env.mkdir(parents=True, exist_ok=True)
    (fake_env / "holder").write_text(f"{dead.pid}\n")

    async def run():
        return await asyncio.wait_for(_join(idle_timeout=0, owner_say=None), 10)

    assert asyncio.run(run()) == 0


# --------------------------------------------------------------------------- #
# operator.alive
# --------------------------------------------------------------------------- #
def test_alive_on_while_served_off_when_idle(fake_env, monkeypatch):
    monkeypatch.setattr(cli, "ALIVE_INTERVAL", 0.1)
    monkeypatch.setattr(cli, "ALIVE_WINDOW", 0.3)

    async def run():
        join = asyncio.create_task(_join(idle_timeout=0))
        await _call({"cmd": "say", "text": "hallo"})
        for _ in range(3):
            await _call({"cmd": "next", "timeout": 0.2})
        room = _FakeRoom.instances[0]
        served = len(_alive(room))
        await asyncio.sleep(0.5)                 # stop serving: window runs out
        quiet_from = len(_alive(room))
        await asyncio.sleep(0.6)
        quiet_to = len(_alive(room))
        await _call({"cmd": "say", "text": "wieder da"})
        await asyncio.sleep(0.15)
        back = len(_alive(room))
        await _call({"cmd": "leave"})
        await asyncio.wait_for(join, 5)
        return served, quiet_from, quiet_to, back, _alive(room)

    served, quiet_from, quiet_to, back, pkts = asyncio.run(run())
    assert served >= 3, "no sign of life while serving"
    assert quiet_to == quiet_from, "alive kept flowing while nobody served"
    assert back > quiet_to, "alive did not resume on activity"
    assert all(p["alive"] is True for p in pkts[:-1]) and pkts[-1]["alive"] is False
    assert all(isinstance(p["ts"], float) for p in pkts)


# --------------------------------------------------------------------------- #
# exit hang: readline blocked on a FIFO whose holder stays open
# --------------------------------------------------------------------------- #
_HANG = textwrap.dedent("""
    import asyncio, sys
    from voicehook_agent import cli
    MODE = sys.argv[1]
    async def main():
        loop = asyncio.get_running_loop()
        if MODE == "daemon":
            fut = cli._daemon_readline(loop, sys.stdin)
        else:
            fut = asyncio.ensure_future(loop.run_in_executor(None, sys.stdin.readline))
        await asyncio.sleep(0.2)
        fut.cancel()
    asyncio.run(main())
    print("exited", flush=True)
""")


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs FIFOs")
@pytest.mark.parametrize("mode,hangs", [("daemon", False), ("executor", True)])
def test_process_exits_although_fifo_holder_lives(tmp_path, mode, hangs):
    fifo = tmp_path / "in"
    os.mkfifo(fifo)
    holder = subprocess.Popen(["sh", "-c", f"sleep 30 > {fifo}"])
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path))
    try:
        with open(fifo) as stdin:
            proc = subprocess.Popen([sys.executable, "-c", _HANG, mode], stdin=stdin,
                                    stdout=subprocess.PIPE, env=env)
            try:
                out, _ = proc.communicate(timeout=4)
                exited = b"exited" in out and proc.returncode == 0
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
                exited = False
    finally:
        holder.kill()
    # "executor" is the positive control: the old code path really hangs.
    assert exited is (not hangs)
