"""0.10.1: a join never outlives the call (Vorfall 04.10., Raum drift-quartz-ember-34EH:
Delta published `call_end` idle_no_human, the CLI ignored it and sat alone in the room).

- `call_end` from the worker -> leave at once, no reconnect, also with keep-alive
- ROOM_DELETED and HTTP 410 on the rejoin token are terminal
- no-human guard: no human for --no-human-timeout -> leave
- positive controls: a human keeps the join, a real network drop still reconnects
"""
from __future__ import annotations

import asyncio
import time

from test_session import (  # noqa: F401  (fixture)
    _call,
    _FakeRoom,
    _join,
    _peer,
    fake_env,
)

from voicehook_agent import cli, relay

SIGNAL_CLOSE = 9   # transient LiveKit disconnect -> reconnect
ROOM_DELETED = 5   # livekit.rtc.DisconnectReason.ROOM_DELETED

HUMAN = {"host-ut46y7": _peer("host-ut46y7", kind=0)}


def _no_reconnect(rooms_before: int) -> bool:
    return len(_FakeRoom.instances) == rooms_before


def test_room_deleted_reason_value_matches_sdk():
    assert cli.rtc.DisconnectReason.Name(ROOM_DELETED) == "ROOM_DELETED"
    assert relay.is_terminal_disconnect("ROOM_DELETED")
    assert relay.is_terminal_disconnect("CALL_ENDED")


def test_is_human_peer():
    assert relay.is_human_peer("user", {})
    assert relay.is_human_peer("sip", None)
    assert not relay.is_human_peer("agent", {})                    # Delta
    assert not relay.is_human_peer("user", {"vh.role": "agent"})   # another operator CLI


def test_call_end_topic_leaves_without_reconnect_despite_keep_alive(fake_env, capsys):
    _FakeRoom.peers = HUMAN

    async def run():
        join = asyncio.create_task(_join(idle_timeout=0, keep_alive=True))
        await asyncio.sleep(0.3)
        nxt = asyncio.create_task(_call({"cmd": "next", "timeout": 30}))
        await asyncio.sleep(0.2)
        _FakeRoom.instances[0].emit("call_end", {"reason": "idle_no_human"})
        rc = await asyncio.wait_for(join, 5)
        return rc, await asyncio.wait_for(nxt, 5)

    t0 = time.monotonic()
    rc, ev = asyncio.run(run())
    assert rc == 0 and time.monotonic() - t0 < 3
    assert len(_FakeRoom.instances) == 1, "reconnected after call_end"
    assert ev["type"] == "call_end" and ev["reason"] == "idle_no_human"
    out = capsys.readouterr().out
    assert '"topic": "call_end"' in out and '"reason": "idle_no_human"' in out
    assert _FakeRoom.instances[0].topics("operator.alive")[-1]["alive"] is False


def test_room_deleted_is_terminal(fake_env):
    _FakeRoom.peers = HUMAN

    async def run():
        join = asyncio.create_task(_join(idle_timeout=0, keep_alive=True))
        await asyncio.sleep(0.3)
        _FakeRoom.instances[0].handlers["disconnected"](ROOM_DELETED)
        return await asyncio.wait_for(join, 5)

    assert asyncio.run(run()) == 0
    assert len(_FakeRoom.instances) == 1


def test_410_on_rejoin_stops_without_more_attempts(fake_env, monkeypatch):
    _FakeRoom.peers = HUMAN
    calls = []

    async def mint(*a, **k):
        calls.append(time.monotonic())
        if len(calls) == 1:
            return {"url": "wss://fake", "token": "t"}
        raise cli.TokenMintError(410, '{"detail":"call has ended"}')

    monkeypatch.setattr(cli, "_mint_token", mint)

    async def run():
        join = asyncio.create_task(_join(idle_timeout=0, keep_alive=True))
        await asyncio.sleep(0.3)
        _FakeRoom.instances[0].handlers["disconnected"](SIGNAL_CLOSE)  # network drop
        rc = await asyncio.wait_for(join, 8)
        await asyncio.sleep(2.5)  # a backoff loop would have minted again by now
        return rc

    assert asyncio.run(run()) == 0
    assert len(calls) == 2, f"minted {len(calls)}x, expected join + one rejoin"


def test_410_on_first_join_fails_fast(fake_env, monkeypatch):
    async def mint(*a, **k):
        raise cli.TokenMintError(410, "call has ended")

    monkeypatch.setattr(cli, "_mint_token", mint)
    rc = asyncio.run(asyncio.wait_for(_join(idle_timeout=0, keep_alive=True), 5))
    assert rc == 5 and _FakeRoom.instances == []


def test_bridge_410_is_call_ended(fake_env, monkeypatch):
    class _Gone(_FakeRoom):
        async def connect(self, url=None, token=None):
            raise cli.vtransport.BridgeError("bridge join failed: HTTP 410", status=410)

    monkeypatch.setattr(cli.vtransport, "BridgeRoom", lambda *a, **k: _Gone())
    rc = asyncio.run(asyncio.wait_for(_join(idle_timeout=0, transport="bridge"), 5))
    assert rc == 5 and len(_FakeRoom.instances) == 1


def test_no_human_guard_leaves_alone_join(fake_env):
    _FakeRoom.peers = {"agent-AJ_x": _peer("agent-AJ_x", kind=4),
                       "claude-2": _peer("claude-2", attrs={"vh.role": "agent"})}

    async def run():
        t0 = time.monotonic()
        rc = await asyncio.wait_for(_join(idle_timeout=0, no_human_timeout=1.0), 6)
        return rc, time.monotonic() - t0

    rc, took = asyncio.run(run())
    assert rc == 0 and took < 3.0, took
    assert len(_FakeRoom.instances) == 1


def test_positive_control_human_keeps_join(fake_env):
    _FakeRoom.peers = HUMAN

    async def run():
        join = asyncio.create_task(_join(idle_timeout=0, no_human_timeout=0.5))
        await asyncio.sleep(1.5)
        alive = not join.done()
        await _call({"cmd": "leave"})
        return alive, await asyncio.wait_for(join, 5)

    alive, rc = asyncio.run(run())
    assert alive and rc == 0


def test_human_joining_cancels_and_leaving_restarts_timer(fake_env):
    async def run():
        join = asyncio.create_task(_join(idle_timeout=0, no_human_timeout=1.0))
        await asyncio.sleep(0.6)
        room = _FakeRoom.instances[0]
        human = _peer("host-ut46y7")
        room.remote_participants[human.identity] = human
        room.handlers["participant_connected"](human)
        await asyncio.sleep(1.2)  # past the original deadline
        alive_with_human = not join.done()
        del room.remote_participants[human.identity]
        room.handlers["participant_disconnected"](human)
        t0 = time.monotonic()
        rc = await asyncio.wait_for(join, 5)
        return alive_with_human, rc, time.monotonic() - t0

    alive, rc, took = asyncio.run(run())
    assert alive and rc == 0 and 0.8 <= took < 2.5, took


def test_positive_control_network_drop_still_reconnects(fake_env):
    _FakeRoom.peers = HUMAN

    async def run():
        join = asyncio.create_task(_join(idle_timeout=0, keep_alive=True))
        await asyncio.sleep(0.3)
        _FakeRoom.instances[0].handlers["disconnected"](SIGNAL_CLOSE)
        await asyncio.sleep(1.8)  # backoff 1 s
        n = len(_FakeRoom.instances)
        await _call({"cmd": "leave"})
        return n, await asyncio.wait_for(join, 5)

    n, rc = asyncio.run(run())
    assert n == 2 and rc == 0


def test_no_human_timeout_default_is_above_server_grace():
    """Oliver 05.10.: Server-Frist 120 s; die CLI geht erst danach (150 s), nie vorher."""
    import inspect

    from voicehook_agent import cli
    assert cli.NO_HUMAN_TIMEOUT == 150.0 > 120.0
    assert "default=NO_HUMAN_TIMEOUT / 60.0" in inspect.getsource(cli.main)  # Flag-Default = Konstante


# -- 0.10.2: 409 "no human in the room" -> clear message, wait, retry; 410 stays terminal --
def test_409_waits_for_human_then_joins(fake_env, monkeypatch, capsys):
    _FakeRoom.peers = HUMAN
    monkeypatch.setattr(cli, "NO_HUMAN_RETRY_S", 0.1, raising=False)
    calls = []

    async def mint(*a, **k):
        calls.append(time.monotonic())
        if len(calls) < 3:
            raise cli.TokenMintError(409, '{"detail":"no human in the room"}')
        return {"url": "wss://fake", "token": "t"}

    monkeypatch.setattr(cli, "_mint_token", mint)

    async def run():
        join = asyncio.create_task(_join(idle_timeout=0, keep_alive=True))
        await asyncio.sleep(0.6)
        joined = len(_FakeRoom.instances)
        _FakeRoom.instances[0].emit("call_end", {"reason": "hangup"})
        return joined, await asyncio.wait_for(join, 5)

    joined, rc = asyncio.run(run())
    assert len(calls) == 3 and joined == 1 and rc == 0
    err = capsys.readouterr().err
    assert err.count("no human in the room yet (HTTP 409)") == 1      # one clear message, no spam


def test_409_gives_up_after_max_wait(fake_env, monkeypatch, capsys):
    monkeypatch.setattr(cli, "NO_HUMAN_RETRY_S", 0.05, raising=False)
    monkeypatch.setattr(cli, "NO_HUMAN_WAIT_MAX_S", 0.3, raising=False)
    calls = []

    async def mint(*a, **k):
        calls.append(1)
        raise cli.TokenMintError(409, "no human in the room")

    monkeypatch.setattr(cli, "_mint_token", mint)
    rc = asyncio.run(asyncio.wait_for(_join(idle_timeout=0, keep_alive=False), 5))
    assert rc == 6 and _FakeRoom.instances == []
    assert 5 <= len(calls) <= 9                                       # 0.3 s / 0.05 s + first try
    assert "still no human in the room after" in capsys.readouterr().err


def test_410_after_409_stops_at_once(fake_env, monkeypatch):
    monkeypatch.setattr(cli, "NO_HUMAN_RETRY_S", 0.05, raising=False)
    calls = []

    async def mint(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise cli.TokenMintError(409, "no human in the room")
        raise cli.TokenMintError(410, "call has ended")

    monkeypatch.setattr(cli, "_mint_token", mint)
    rc = asyncio.run(asyncio.wait_for(_join(idle_timeout=0, keep_alive=True), 5))
    assert rc == 5 and len(calls) == 2


def test_bridge_409_retries(fake_env, monkeypatch):
    _FakeRoom.peers = HUMAN
    monkeypatch.setattr(cli, "NO_HUMAN_RETRY_S", 0.05, raising=False)
    tries = []

    class _Wait(_FakeRoom):
        async def connect(self, url=None, token=None):
            tries.append(1)
            if len(tries) < 3:
                raise cli.vtransport.BridgeError("bridge join failed: HTTP 409", status=409)
            return await super().connect(url, token)

    monkeypatch.setattr(cli.vtransport, "BridgeRoom", lambda *a, **k: _Wait())

    async def run():
        join = asyncio.create_task(_join(idle_timeout=0, transport="bridge"))
        await asyncio.sleep(0.6)
        _FakeRoom.instances[-1].emit("call_end", {"reason": "hangup"})
        return await asyncio.wait_for(join, 5)

    assert asyncio.run(run()) == 0 and len(tries) == 3
