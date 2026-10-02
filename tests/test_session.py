"""Control channel (say / next / leave / status), idle guard and persona guard.

Pure parts (EventQueue, IdleWatchdog, relay helpers) are tested directly; the
join wiring is tested end-to-end against a fake LiveKit room, talking to the
real Unix control socket exactly like the one-shot commands do."""
from __future__ import annotations

import asyncio
import io
import json
import os
import signal
import socket
import time
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
        q.arm()
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


def _join(identity=None, **kw):
    kw.setdefault("model", "opus-5.5")
    return cli._join(URL, identity, "Claude", True, kw.pop("persona", None), **kw)


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
    assert {k: r_timeout[k] for k in ("ok", "type", "pending")} == {"ok": True, "type": "timeout", "pending": 0}
    assert r_timeout["status_reason"] == "empty"            # 0.9.0: no board yet
    assert r_status["connected"] is True and r_status["room"] == SLUG
    assert r_leave["type"] == "leaving" and says[-1]["text"] == "Tschuess"
    assert rc == 0
    assert vs.live_sessions() == []                      # socket gone after leave
    assert not vs.socket_path(vs.session_dir(SLUG, r_status["identity"])).exists()


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


def test_second_join_same_room_gets_own_socket(fake_env):
    """Two joins into the same room from one machine (multi-agent box) both
    run; each has its own socket, clients pick by --session slug/identity."""
    async def run():
        a = asyncio.create_task(_join(identity="claude-a", idle_timeout=0))
        await _call({"cmd": "status"})                   # only one -> auto-pick
        b = asyncio.create_task(_join(identity="claude-b", idle_timeout=0))
        for _ in range(50):
            if len(vs.live_sessions()) == 2:
                break
            await asyncio.sleep(0.1)
        live = [vs._label(d) for d in vs.live_sessions()]
        with pytest.raises(vs.SessionError) as auto:
            vs.resolve_socket(None)
        with pytest.raises(vs.SessionError) as by_slug:
            vs.resolve_socket(SLUG)
        sa = await asyncio.to_thread(vs.resolve_socket, f"{SLUG}/claude-a", 2)
        sb = await asyncio.to_thread(vs.resolve_socket, f"{SLUG}/claude-b", 2)
        st_a = await asyncio.to_thread(vs.request, sa, {"cmd": "status"}, 5)
        st_b = await asyncio.to_thread(vs.request, sb, {"cmd": "status"}, 5)
        await asyncio.to_thread(vs.request, sb, {"cmd": "leave"}, 5)
        rc_b = await asyncio.wait_for(b, 5)
        # one left -> auto-pick works again
        st_auto = await _call({"cmd": "status"})
        await _call({"cmd": "leave"})
        return live, auto, by_slug, st_a, st_b, rc_b, st_auto, await asyncio.wait_for(a, 5)

    live, auto, by_slug, st_a, st_b, rc_b, st_auto, rc_a = asyncio.run(run())
    assert live == [f"{SLUG}/claude-a", f"{SLUG}/claude-b"]
    assert "--session" in str(auto.value) and f"{SLUG}/claude-b" in str(auto.value)
    assert "--session" in str(by_slug.value)
    assert st_a["identity"] == "claude-a" and st_b["identity"] == "claude-b"
    assert st_auto["identity"] == "claude-a"
    assert rc_a == 0 and rc_b == 0


def test_same_identity_twice_refused_with_hint(fake_env, capsys):
    async def run():
        first = asyncio.create_task(_join(identity="claude-x", idle_timeout=0))
        await asyncio.to_thread(vs.resolve_socket, None, 5)
        rc2 = await _join(identity="claude-x", idle_timeout=0)
        await _call({"cmd": "leave"})
        return rc2, await asyncio.wait_for(first, 5)

    rc2, rc1 = asyncio.run(run())
    err = capsys.readouterr().err
    assert rc2 == 2 and rc1 == 0
    assert f"leave --session {SLUG}/claude-x" in err and "--no-control" in err


# --------------------------------------------------------------------------- #
# /tmp fallback: directory must be ours, 0700, no symlink (server + client)
# --------------------------------------------------------------------------- #
@pytest.fixture
def fallback(tmp_path, monkeypatch):
    import shutil
    import tempfile
    root = Path(tempfile.mkdtemp(prefix="vhfb", dir="/tmp"))  # short: AF_UNIX limit
    monkeypatch.setattr(vs, "FALLBACK_ROOT", root)
    yield_root = root
    deep = tmp_path / ("d" * 120)
    sock = vs.socket_path(deep)
    assert vs.is_fallback(sock) and sock.parent.parent == root
    yield sock
    for d in (sock.parent,):
        if d.is_symlink():
            d.unlink()
    shutil.rmtree(yield_root, ignore_errors=True)


def _start_server(sock):
    async def h(req):
        return {"ok": True}

    async def run():
        srv = vs.ControlServer(sock, h)
        await srv.start()
        await srv.close()
    asyncio.run(run())


def test_fallback_dir_created_private_and_usable(fallback):
    _start_server(fallback)                       # positive control: own dir works
    st = fallback.parent.lstat()
    assert st.st_mode & 0o777 == 0o700


def test_fallback_dir_refuses_loose_mode(fallback):
    fallback.parent.mkdir(mode=0o755)
    fallback.parent.chmod(0o755)
    with pytest.raises(vs.SessionError, match="mode 755"):
        _start_server(fallback)


def test_fallback_dir_refuses_symlink(fallback, tmp_path):
    target = tmp_path / "elsewhere"
    target.mkdir(mode=0o700)
    fallback.parent.symlink_to(target)
    with pytest.raises(vs.SessionError, match="symlink"):
        _start_server(fallback)


def test_fallback_dir_refuses_foreign_owner(fallback, monkeypatch):
    fallback.parent.mkdir(mode=0o700)
    real = os.getuid()
    monkeypatch.setattr(vs, "fallback_dir", lambda: fallback.parent)
    monkeypatch.setattr(os, "getuid", lambda: real + 4242)
    with pytest.raises(vs.SessionError, match="belongs to uid"):
        _start_server(fallback)


def test_client_refuses_socket_in_foreign_fallback_dir(fallback):
    _start_server(fallback)

    async def h(req):
        return {"ok": True}

    async def run():
        srv = vs.ControlServer(fallback, h)
        await srv.start()
        try:
            ok = await asyncio.to_thread(vs.resolve_socket, str(fallback), 1)  # control
            fallback.parent.chmod(0o755)                # someone loosened the dir
            try:
                await asyncio.to_thread(vs.resolve_socket, str(fallback), 0)
                bad = None
            except vs.SessionError as e:
                bad = e
            finally:
                fallback.parent.chmod(0o700)
            return ok, bad
        finally:
            await srv.close()

    ok, bad = asyncio.run(run())
    assert ok == fallback
    assert bad is not None and "mode 755" in str(bad)


# --------------------------------------------------------------------------- #
# SIGHUP respects nohup (SIG_IGN)
# --------------------------------------------------------------------------- #
def test_sighup_handler_only_when_not_ignored():
    prev = signal.getsignal(signal.SIGHUP)
    try:
        signal.signal(signal.SIGHUP, signal.SIG_DFL)
        assert signal.SIGHUP in cli._quit_signals()      # positive control
        signal.signal(signal.SIGHUP, signal.SIG_IGN)     # what nohup does
        sigs = cli._quit_signals()
        assert signal.SIGHUP not in sigs and signal.SIGTERM in sigs
    finally:
        signal.signal(signal.SIGHUP, prev)


def test_join_under_nohup_ignores_sighup(fake_env):
    prev = signal.getsignal(signal.SIGHUP)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    try:
        async def run():
            join = asyncio.create_task(_join(idle_timeout=0))
            await _call({"cmd": "status"})
            os.kill(os.getpid(), signal.SIGHUP)
            await asyncio.sleep(0.3)
            alive = not join.done()
            await _call({"cmd": "leave"})
            return alive, await asyncio.wait_for(join, 5)
        alive, rc = asyncio.run(run())
    finally:
        signal.signal(signal.SIGHUP, prev)
    assert alive and rc == 0


# --------------------------------------------------------------------------- #
# ControlServer.close() never hangs on a silent client
# --------------------------------------------------------------------------- #
def test_close_does_not_hang_on_silent_client(tmp_path):
    sock = tmp_path / "c.sock"

    async def slow(req):
        await asyncio.sleep(60)
        return {"ok": True}

    async def run():
        srv = vs.ControlServer(sock, slow, read_timeout=30, close_timeout=0.5)
        await srv.start()
        silent = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        silent.connect(str(sock))                       # connects, never sends
        busy = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        busy.connect(str(sock))
        busy.sendall(b'{"cmd":"say"}\n')                # handler hangs
        await asyncio.sleep(0.1)
        t0 = time.monotonic()
        await asyncio.wait_for(srv.close(), 5)
        took = time.monotonic() - t0
        silent.close(); busy.close()
        return took

    took = asyncio.run(run())
    assert took < 2.0 and not sock.exists()


def test_silent_client_is_dropped_after_read_timeout(tmp_path):
    sock = tmp_path / "r.sock"

    async def h(req):
        return {"ok": True}

    async def run():
        srv = vs.ControlServer(sock, h, read_timeout=0.2)
        await srv.start()
        try:
            r = await asyncio.to_thread(vs.request, sock, {"cmd": "status"}, 2)  # control
            def silent():
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.settimeout(3)
                s.connect(str(sock))
                try:
                    return s.recv(10)                  # b"" = server closed us
                finally:
                    s.close()
            got = await asyncio.to_thread(silent)
            return r, got, len(srv._clients)
        finally:
            await srv.close()

    r, got, left = asyncio.run(run())
    assert r == {"ok": True} and got == b"" and left == 0


# --------------------------------------------------------------------------- #
# EventQueue only collects once armed, and is bounded
# --------------------------------------------------------------------------- #
def test_event_queue_ignores_turns_before_armed_and_is_bounded():
    async def run():
        q = vs.EventQueue(maxlen=3)
        q.put_nowait({"type": "user", "text": "alt"})      # nobody uses next yet
        before = len(q)
        q.arm()
        for i in range(5):
            q.put_nowait({"type": "user", "text": str(i)})
        got = [(await q.get(0))["text"] for _ in range(3)]
        return before, got, q.dropped
    before, got, dropped = asyncio.run(run())
    assert before == 0
    assert got == ["2", "3", "4"] and dropped == 2


def test_turns_before_first_say_or_next_are_not_queued(fake_env):
    async def run():
        join = asyncio.create_task(_join(idle_timeout=0))
        await _call({"cmd": "status"})                    # status does not arm
        room = _FakeRoom.instances[0]
        room.emit("transcript", {"role": "user", "text": "von vor zehn Minuten"})
        stale = await _call({"cmd": "next", "timeout": 0})  # arms now
        room.emit("transcript", {"role": "user", "text": "frisch"})
        fresh = await _call({"cmd": "next", "timeout": 2})
        await _call({"cmd": "leave"})
        return stale, fresh, await asyncio.wait_for(join, 5)

    stale, fresh, rc = asyncio.run(run())
    assert stale["type"] == "timeout"
    assert fresh["type"] == "user" and fresh["text"] == "frisch" and rc == 0


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
