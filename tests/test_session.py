"""Control channel (say / next / leave / status), idle guard and persona guard.

Pure parts (EventQueue, IdleWatchdog, relay helpers) are tested directly; the
join wiring is tested end-to-end against a fake LiveKit room, talking to the
real Unix control socket exactly like the one-shot commands do."""
from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from voicehook_agent import relay
from voicehook_agent import session as vs

cli = pytest.importorskip(
    "voicehook_agent.cli",
    reason="livekit/httpx not installed in this environment",
)

SLUG = "abc-def-ghi-XYZ4"
URL = f"https://voicehook.example/r/{SLUG}"


# --------------------------------------------------------------------------- #
# pure helpers
# --------------------------------------------------------------------------- #
def test_other_operators_detects_only_foreign_operator_agents():
    peers = [
        ("agent-AJ_worker", "agent", {}),                       # voice-ai worker
        ("host-browser", "user", {}),                           # the human
        ("hermes-box-1a2b", "user", {"vh.role": "agent", "vh.name": "Hermes"}),
    ]
    assert relay.other_operators(peers) == ["hermes-box-1a2b"]


def test_other_operators_empty_room():
    assert relay.other_operators([("host", "user", None)]) == []


def test_user_turn_event_only_final_user_text():
    assert relay.user_turn_event("agent", "x") is None
    assert relay.user_turn_event("user", "  ") is None
    assert relay.user_turn_event("user", "hi", {"final": False}) is None
    ev = relay.user_turn_event("user", " Hallo ", {}, now=5.0)
    assert ev == {"type": "user", "role": "user", "text": "Hallo", "ts": 5.0}


def test_revise_event_carries_unspoken():
    ev = relay.revise_event({"text": "REVISE: ...", "unspoken": ["a"], "new": "b"}, now=1.0)
    assert ev["type"] == "revise" and ev["unspoken"] == ["a"] and ev["new"] == "b"


def test_event_queue_keeps_events_until_next():
    async def run():
        q = vs.EventQueue()
        await q.put({"type": "user", "text": "eins"})
        await q.put({"type": "user", "text": "zwei"})
        a = await q.get(1.0)
        b = await q.get(0)
        c = await q.get(0.05)
        return a, b, c
    a, b, c = asyncio.run(run())
    assert a["text"] == "eins" and b["text"] == "zwei" and c is None


def test_event_queue_wakes_blocked_getter_and_ends():
    async def run():
        q = vs.EventQueue()
        getter = asyncio.create_task(q.get(5.0))
        await asyncio.sleep(0.05)
        q.put_nowait({"type": "user", "text": "jetzt"})
        got = await getter
        await q.close()
        return got, await q.get(5.0)
    got, ended = asyncio.run(run())
    assert got["text"] == "jetzt"
    assert ended == {"type": "ended"}


def test_idle_watchdog():
    w = vs.IdleWatchdog(timeout=10, last=0.0)
    assert not w.expired(now=9.0)
    assert w.expired(now=10.0)
    w.enter()                      # blocked `next` = alive
    assert not w.expired(now=100.0)
    w.leave(now=100.0)
    assert not w.expired(now=105.0)
    assert w.expired(now=110.0)
    assert not vs.IdleWatchdog(timeout=0, last=0.0).expired(now=1e9)


# --------------------------------------------------------------------------- #
# fake LiveKit room
# --------------------------------------------------------------------------- #
class _FakeLP:
    def __init__(self):
        self.sent: list[tuple[str, dict]] = []

    async def publish_data(self, data, reliable=True, topic=""):
        self.sent.append((topic, json.loads(bytes(data).decode("utf-8"))))


class _FakeRoom:
    instances: list["_FakeRoom"] = []
    peers: dict = {}

    def __init__(self):
        self.handlers = {}
        self.remote_participants = dict(_FakeRoom.peers)
        self.local_participant = _FakeLP()
        _FakeRoom.instances.append(self)

    def on(self, event, fn=None):
        self.handlers[event] = fn

    async def connect(self, url, token):
        return None

    async def disconnect(self):
        return None

    def emit(self, topic, obj, sender="agent-AJ_worker"):
        pkt = SimpleNamespace(topic=topic, data=json.dumps(obj).encode("utf-8"),
                              participant=SimpleNamespace(identity=sender))
        self.handlers["data_received"](pkt)

    def topics(self, topic):
        return [p for t, p in self.local_participant.sent if t == topic]


def _peer(identity, kind=0, attrs=None):
    return SimpleNamespace(identity=identity, kind=kind, attributes=attrs or {},
                           track_publications={})


@pytest.fixture
def fake_env(tmp_path, monkeypatch):
    home = tmp_path / "vh"
    monkeypatch.setenv("VOICEHOOK_AGENT_HOME", str(home))
    monkeypatch.setattr(cli.rtc, "Room", _FakeRoom)

    async def _mint(*a, **k):
        return {"url": "wss://fake", "token": "t"}

    monkeypatch.setattr(cli, "_mint_token", _mint)
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    _FakeRoom.instances = []
    _FakeRoom.peers = {}
    return home


async def _call(req, wait=5.0):
    sock = await asyncio.to_thread(vs.resolve_socket, None, wait)
    return await asyncio.to_thread(vs.request, sock, req, 10.0)


def _join(**kw):
    kw.setdefault("model", "opus-5.5")
    return cli._join(URL, None, "Claude", True, kw.pop("persona", None), **kw)


# --------------------------------------------------------------------------- #
# say -> next -> leave loop (no polling)
# --------------------------------------------------------------------------- #
def test_say_next_leave_loop(fake_env):
    async def run():
        join = asyncio.create_task(_join(idle_timeout=0))
        r_say = await _call({"cmd": "say", "text": "Hallo Oliver", "mode": "append"})
        room = _FakeRoom.instances[0]
        # user speaks while nobody waits -> must be queued, not lost
        room.emit("transcript", {"role": "agent", "text": "eigene Antwort"})
        room.emit("transcript", {"role": "user", "text": "Wie geht's?"})
        r_next = await _call({"cmd": "next", "timeout": 5})
        # blocked next is woken by the next user turn
        pending = asyncio.create_task(_call({"cmd": "next", "timeout": 5}))
        await asyncio.sleep(0.2)
        room.emit("operator.revise", {"text": "REVISE: ...", "unspoken": ["x"], "new": "y"})
        r_rev = await pending
        r_timeout = await _call({"cmd": "next", "timeout": 0.1})
        r_status = await _call({"cmd": "status"})
        r_leave = await _call({"cmd": "leave", "say": "Tschuess"})
        rc = await asyncio.wait_for(join, 5)
        return room, rc, r_say, r_next, r_rev, r_timeout, r_status, r_leave

    room, rc, r_say, r_next, r_rev, r_timeout, r_status, r_leave = asyncio.run(run())
    says = room.topics("operator.say")
    assert says[0]["text"].startswith("Hallo, hier ist Claude")  # auto-greet still first
    assert r_say["ok"] and any(s["text"] == "Hallo Oliver" and s["mode"] == "append" for s in says)
    assert r_next["type"] == "user" and r_next["text"] == "Wie geht's?"
    assert r_rev["type"] == "revise" and r_rev["unspoken"] == ["x"]
    assert r_timeout == {"ok": True, "type": "timeout", "pending": 0}
    assert r_status["connected"] is True and r_status["room"] == SLUG
    assert r_leave["type"] == "leaving" and says[-1]["text"] == "Tschuess"
    assert rc == 0
    assert vs.live_sessions() == []                      # socket gone after leave
    assert not vs.socket_path(vs.session_dir(SLUG)).exists()


def test_blocked_next_returns_ended_when_join_stops(fake_env):
    async def run():
        join = asyncio.create_task(_join(idle_timeout=0))
        waiting = asyncio.create_task(_call({"cmd": "next", "timeout": 30}))
        await asyncio.sleep(0.3)
        sock = await asyncio.to_thread(vs.resolve_socket, None, 5)
        await asyncio.to_thread(vs.request, sock, {"cmd": "leave"}, 5)
        return await asyncio.wait_for(waiting, 5), await asyncio.wait_for(join, 5)

    ended, rc = asyncio.run(run())
    assert ended["type"] == "ended" and rc == 0


def test_client_exit_codes(fake_env, capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["say", "hallo", "--wait", "0"])
    assert e.value.code == 3
    out = json.loads(capsys.readouterr().out)
    assert out["type"] == "no-session"


def test_second_join_same_room_refused(fake_env):
    async def run():
        first = asyncio.create_task(_join(idle_timeout=0))
        await asyncio.to_thread(vs.resolve_socket, None, 5)
        rc2 = await _join(idle_timeout=0)
        await _call({"cmd": "leave"})
        return rc2, await asyncio.wait_for(first, 5)

    rc2, rc1 = asyncio.run(run())
    assert rc2 == 2 and rc1 == 0


# --------------------------------------------------------------------------- #
# orphan guard
# --------------------------------------------------------------------------- #
def test_idle_timeout_leaves_with_announcement(fake_env):
    async def run():
        return await asyncio.wait_for(_join(idle_timeout=0.3, idle_say="Ich gehe."), 10)

    rc = asyncio.run(run())
    room = _FakeRoom.instances[0]
    assert rc == 0
    assert room.topics("operator.say")[-1]["text"] == "Ich gehe."
    assert vs.live_sessions() == []


def test_activity_keeps_join_alive(fake_env):
    async def run():
        join = asyncio.create_task(_join(idle_timeout=0.6, idle_say=None))
        for _ in range(4):                     # 4 x 0.3 s > timeout, but active
            await _call({"cmd": "say", "text": "ping"})
            await asyncio.sleep(0.3)
        alive = not join.done()
        rc = await asyncio.wait_for(join, 10)  # then idle -> leaves by itself
        return alive, rc

    alive, rc = asyncio.run(run())
    assert alive and rc == 0


# --------------------------------------------------------------------------- #
# persona guard
# --------------------------------------------------------------------------- #
def _persona_run(peers, force=False):
    _FakeRoom.peers = peers

    async def run():
        join = asyncio.create_task(_join(persona="MEINE PERSONA", strict=True,
                                         idle_timeout=0, force_persona=force))
        await _call({"cmd": "status"})
        await _call({"cmd": "leave"})
        return await asyncio.wait_for(join, 5)

    assert asyncio.run(run()) == 0
    room = _FakeRoom.instances[-1]
    return room.topics("operator.persona"), room.topics("operator.mode")


def test_persona_pushed_when_room_free(fake_env):
    persona, mode = _persona_run({"w": _peer("agent-AJ_w", kind=4), "h": _peer("host")})
    assert persona == [{"text": "MEINE PERSONA"}] and mode == [{"mode": "strict"}]


def test_persona_not_pushed_over_other_operator(fake_env):
    other = _peer("hermes-box-1a2b", attrs={"vh.role": "agent", "vh.name": "Hermes"})
    persona, mode = _persona_run({"w": _peer("agent-AJ_w", kind=4), "o": other})
    assert persona == [] and mode == []


def test_force_persona_overrides_guard(fake_env):
    other = _peer("hermes-box-1a2b", attrs={"vh.role": "agent"})
    persona, _ = _persona_run({"o": other}, force=True)
    assert persona == [{"text": "MEINE PERSONA"}]


def test_socket_path_falls_back_when_too_long(tmp_path):
    deep = tmp_path / ("x" * 120)
    p = vs.socket_path(deep)
    assert len(str(p)) <= 100 and p.suffix == ".sock"
    assert vs.socket_path(deep) == p          # deterministic for join + clients
    assert vs.socket_path(Path("/h/s")) == Path("/h/s") / vs.SOCKET_NAME


def test_say_next_with_long_home(tmp_path, monkeypatch, fake_env):
    monkeypatch.setenv("VOICEHOOK_AGENT_HOME", str(tmp_path / ("h" * 90)))

    async def run():
        join = asyncio.create_task(_join(idle_timeout=0))
        r = await _call({"cmd": "say", "text": "hallo"})
        await _call({"cmd": "leave"})
        return r, await asyncio.wait_for(join, 5)

    r, rc = asyncio.run(run())
    assert r["ok"] and rc == 0
