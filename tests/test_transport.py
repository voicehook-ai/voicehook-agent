"""Transport choice, the HTTPS bridge client (against a fake SSE server) and
proxy-env handling (through a real local forward proxy)."""
from __future__ import annotations

import asyncio
import io
import json
import sys
from urllib.parse import urlparse

import pytest

from voicehook_agent import cli
from voicehook_agent import transport as vt

PROXY_ENV = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY",
             "all_proxy", "NO_PROXY", "no_proxy")


@pytest.fixture(autouse=True)
def _clean_proxy_env(monkeypatch):
    for k in PROXY_ENV:
        monkeypatch.delenv(k, raising=False)


# --------------------------------------------------------------------------- #
# transport choice
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("env,expect", [
    ({}, "webrtc"),
    ({"HTTPS_PROXY": "http://proxy:3128"}, "bridge"),
    ({"https_proxy": "http://proxy:3128"}, "bridge"),
    ({"ALL_PROXY": "socks5://p:1080"}, "bridge"),
    ({"HTTPS_PROXY": "  "}, "webrtc"),
    ({"HTTP_PROXY": "http://proxy:3128"}, "webrtc"),  # WebRTC signalling is wss -> HTTPS matters
])
def test_auto_picks_bridge_only_with_https_or_all_proxy(env, expect):
    t, why = vt.initial_transport("auto", env)
    assert t == expect
    assert why.startswith("auto")


@pytest.mark.parametrize("mode", ["webrtc", "bridge"])
def test_forced_transport_ignores_env(mode):
    assert vt.initial_transport(mode, {"HTTPS_PROXY": "http://p:1"})[0] == mode


def test_fallback_only_auto_webrtc_connect_failed():
    assert vt.should_fallback("auto", "webrtc", "CONNECT_FAILED")
    assert not vt.should_fallback("webrtc", "webrtc", "CONNECT_FAILED")
    assert not vt.should_fallback("auto", "bridge", "CONNECT_FAILED")
    assert not vt.should_fallback("auto", "webrtc", "ROOM_DELETED")
    assert not vt.should_fallback("auto", "webrtc", "TOKEN_MINT_FAILED")


def test_cli_flag_default_auto_and_choices():
    with pytest.raises(SystemExit):
        cli.main(["join", "x", "--name", "a", "--model", "b", "--transport", "carrier-pigeon"])


def test_invite_code_extracted():
    assert cli._invite_code("https://voicehook.ai/r/a-b-c-AB12?invite=xx.yy.zz") == "xx.yy.zz"
    assert cli._invite_code("https://voicehook.ai/r/a-b-c-AB12?go=1") is None


def test_parse_sse():
    async def lines():
        for line in [": ping", "", "event: data", 'data: {"type":"data","topic":"t"}', "",
                     "data: not-json", "", 'data: {"type":', 'data: "x"}', ""]:
            yield line

    async def go():
        return [e async for e in vt.parse_sse(lines())]
    assert asyncio.run(go()) == [{"type": "data", "topic": "t"}, {"type": "x"}]


# --------------------------------------------------------------------------- #
# fake bridge server + forward proxy (raw asyncio, no extra deps)
# --------------------------------------------------------------------------- #
PEERS = [
    {"identity": "voice-ai-1", "kind": 4, "name": "", "attributes": {}, "audio": True},
    {"identity": "host-user", "kind": 0, "name": "Oliver", "attributes": {}, "audio": True},
]


class FakeBridge:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict, bytes]] = []
        self.events: asyncio.Queue = asyncio.Queue()
        self.sent: list[dict] = []
        self.joins: list[dict] = []
        self.left = 0
        self.port = 0

    async def start(self) -> str:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{self.port}"

    async def stop(self) -> None:
        self.server.close()

    def push(self, ev: dict) -> None:
        self.events.put_nowait(f"event: {ev['type']}\ndata: {json.dumps(ev)}\n\n")

    async def _handle(self, reader, writer) -> None:
        try:
            line = (await reader.readline()).decode()
            method, target, _ = line.split(" ", 2)
            headers = {}
            while True:
                h = await reader.readline()
                if h in (b"\r\n", b"\n", b""):
                    break
                k, v = h.decode().split(":", 1)
                headers[k.strip().lower()] = v.strip()
            n = int(headers.get("content-length", "0") or 0)
            body = await reader.readexactly(n) if n else b""
            self.requests.append((method, target, headers, body))
            path = urlparse(target).path
            authed = headers.get("authorization") == "Bearer S3SSION"
            if path == "/api/bridge/join":
                self.joins.append(json.loads(body))
                await self._json(writer, {"session": "S3SSION", "expires_in": 3600,
                                          "room": "a-b-c-AB12", "identity": "x", "peers": PEERS})
            elif path == "/api/bridge/send" and authed:
                self.sent.append(json.loads(body))
                await self._json(writer, {"ok": True})
            elif path == "/api/bridge/leave" and authed:
                self.left += 1
                await self._json(writer, {"ok": True, "type": "leaving"})
            elif path == "/api/bridge/events" and authed:
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                             b"Cache-Control: no-cache\r\nConnection: close\r\n\r\n")
                await writer.drain()
                while True:
                    chunk = await self.events.get()
                    if chunk is None:
                        break
                    writer.write(chunk.encode())
                    await writer.drain()
            else:
                await self._json(writer, {"detail": "nope"}, status="404 Not Found")
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()

    @staticmethod
    async def _json(writer, obj, status="200 OK") -> None:
        b = json.dumps(obj).encode()
        writer.write(f"HTTP/1.1 {status}\r\nContent-Type: application/json\r\n"
                     f"Content-Length: {len(b)}\r\nConnection: close\r\n\r\n".encode() + b)
        await writer.drain()


class ForwardProxy:
    """Minimal HTTP proxy: CONNECT tunnels and absolute-form forwarding. Records
    every request line, so a test can prove traffic went through it."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    async def start(self) -> str:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return f"http://127.0.0.1:{self.server.sockets[0].getsockname()[1]}"

    async def _pipe(self, r, w) -> None:
        try:
            while data := await r.read(65536):
                w.write(data)
                await w.drain()
        except ConnectionError:
            pass
        finally:
            w.close()

    async def _handle(self, reader, writer) -> None:
        head = await reader.readuntil(b"\r\n\r\n")
        first, rest = head.split(b"\r\n", 1)
        method, target, ver = first.decode().split(" ")
        self.seen.append(f"{method} {target}")
        if method == "CONNECT":
            host, port = target.rsplit(":", 1)
            ur, uw = await asyncio.open_connection(host, int(port))
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        else:
            u = urlparse(target)
            ur, uw = await asyncio.open_connection(u.hostname, u.port or 80)
            origin = (u.path or "/") + (f"?{u.query}" if u.query else "")
            uw.write(f"{method} {origin} {ver}\r\n".encode() + rest)
        await asyncio.gather(self._pipe(reader, uw), self._pipe(ur, writer))


async def _until(pred, timeout=3.0) -> None:
    end = asyncio.get_running_loop().time() + timeout
    while not pred():
        if asyncio.get_running_loop().time() > end:
            raise AssertionError("condition not met")
        await asyncio.sleep(0.01)


# --------------------------------------------------------------------------- #
# BridgeRoom
# --------------------------------------------------------------------------- #
def test_bridge_room_join_events_send_leave():
    async def go():
        fb = FakeBridge()
        base = await fb.start()
        room = vt.BridgeRoom(base, "a-b-c-AB12", "claude-box-1", name="Claude",
                             model="opus-5.5", invite="i.n.v")
        got: dict[str, list] = {}
        for ev in ("data_received", "participant_connected", "participant_disconnected",
                   "active_speakers_changed", "track_subscribed", "track_muted", "disconnected"):
            room.on(ev, lambda *a, _ev=ev: got.setdefault(_ev, []).append(a))
        await room.connect()
        j = fb.joins[0]
        assert j == {"invite_url": f"{base}/r/a-b-c-AB12", "name": "Claude", "model": "opus-5.5",
                     "identity": "claude-box-1", "idle_timeout": 0, "invite": "i.n.v"}
        assert set(room.remote_participants) == {"voice-ai-1", "host-user"}
        assert room.remote_participants["voice-ai-1"].kind == 4
        assert cli._kind_label(room.remote_participants["voice-ai-1"]) == "agent"

        fb.push({"type": "hello", "peers": PEERS})
        fb.push({"type": "data", "topic": "transcript", "sender": "voice-ai-1",
                 "payload": {"role": "user", "text": "Hallo"}})
        fb.push({"type": "peer-joined", "identity": "gast", "kind": 0, "attributes": {}, "audio": True})
        fb.push({"type": "speakers", "speakers": ["host-user"]})
        fb.push({"type": "track", "identity": "gast", "state": "mute"})
        fb.push({"type": "peer-left", "identity": "gast", "kind": 0})
        await _until(lambda: "participant_disconnected" in got)
        (pkt,), = got["data_received"]
        assert pkt.topic == "transcript" and pkt.participant.identity == "voice-ai-1"
        assert json.loads(pkt.data) == {"role": "user", "text": "Hallo"}
        assert [a[0].identity for a in got["participant_connected"]] == ["gast"]  # hello added none
        assert got["track_subscribed"][0][2].identity == "gast"
        assert [s.identity for s in got["active_speakers_changed"][0][0]] == ["host-user"]
        assert "gast" not in room.remote_participants

        await room.local_participant.publish_data(
            json.dumps({"text": "Hi", "_seq": 1}).encode(), reliable=True, topic="operator.say")
        assert fb.sent == [{"topic": "operator.say", "payload": {"text": "Hi", "_seq": 1}, "force": True}]

        fb.push({"type": "ended", "reason": "ROOM_DELETED"})
        await _until(lambda: "disconnected" in got)
        assert got["disconnected"] == [("ROOM_DELETED",)]
        await room.disconnect()
        assert fb.left == 1
        # session token only ever in the Authorization header, never in a URL
        assert all("S3SSION" not in t for _, t, _, _ in fb.requests)
        await fb.stop()
    asyncio.run(go())


@pytest.mark.parametrize("reason,expect", [("left", "CLIENT_INITIATED"), ("max_duration", "ROOM_CLOSED"),
                                           ("sse_gone", "BRIDGE_LOST")])
def test_bridge_end_reason_mapping(reason, expect):
    async def go():
        fb = FakeBridge()
        base = await fb.start()
        room = vt.BridgeRoom(base, "a-b-c-AB12", "x", name="C", model="m")
        seen = []
        room.on("disconnected", lambda r: seen.append(r))
        await room.connect()
        fb.push({"type": "ended", "reason": reason})
        await _until(lambda: seen)
        await room.disconnect()
        await fb.stop()
        return seen
    assert asyncio.run(go()) == [expect]
    from voicehook_agent import relay
    assert relay.is_terminal_disconnect(expect) == (reason != "sse_gone")


def test_bridge_join_http_error_raises():
    async def go():
        fb = FakeBridge()
        base = await fb.start()
        room = vt.BridgeRoom(base + "/nope", "a-b-c-AB12", "x", name="C", model="m")
        with pytest.raises(vt.BridgeError, match="HTTP 404"):
            await room.connect()
        await fb.stop()
    asyncio.run(go())


def test_bridge_respects_proxy_env(monkeypatch):
    """HTTP(S)_PROXY from the env is used (httpx trust_env): every bridge request,
    including the SSE stream, goes through the proxy."""
    async def go():
        fb = FakeBridge()
        base = await fb.start()
        px = ForwardProxy()
        purl = await px.start()
        monkeypatch.setenv("HTTP_PROXY", purl)
        monkeypatch.setenv("HTTPS_PROXY", purl)
        room = vt.BridgeRoom(base, "a-b-c-AB12", "x", name="C", model="m")
        seen = []
        room.on("data_received", lambda p: seen.append(p.topic))
        await room.connect()
        fb.push({"type": "data", "topic": "operator.notice", "payload": {"kind": "x"}, "sender": "v"})
        await _until(lambda: seen)
        await room.disconnect()
        await fb.stop()
        return px.seen, base
    seen, base = asyncio.run(go())
    assert f"POST {base}/api/bridge/join" in seen
    assert f"GET {base}/api/bridge/events" in seen
    assert f"POST {base}/api/bridge/leave" in seen


# --------------------------------------------------------------------------- #
# CLI: auto falls back from a failing WebRTC connect to the bridge
# --------------------------------------------------------------------------- #
class _FailingRtcRoom:
    def __init__(self) -> None:
        self.local_participant = None
        self.remote_participants = {}

    def on(self, *a, **k):
        return None

    async def connect(self, url, token, *a, **k):
        raise RuntimeError("wait_pc_connection timed out")

    async def disconnect(self):
        return None


def _run_join(monkeypatch, capsys, transport: str, env: dict | None = None) -> tuple[str, str, list]:
    calls: list = []

    class FakeBridgeRoom(vt.BridgeRoom):
        async def connect(self, url=None, token=None, options=None):
            calls.append(("bridge-connect", self.identity, self.invite))
            self.remote_participants = {"voice-ai-1": vt.BridgeParticipant(PEERS[0])}
            asyncio.get_running_loop().call_later(0.2, self._fire_disconnected, "ROOM_DELETED")

        async def disconnect(self):
            calls.append(("bridge-disconnect",))

    async def fake_mint(*a, **k):
        calls.append(("mint",))
        return {"url": "ws://127.0.0.1:9", "token": "t"}

    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(cli.rtc, "Room", _FailingRtcRoom)
    monkeypatch.setattr(cli, "_mint_token", fake_mint)
    monkeypatch.setattr(vt, "BridgeRoom", FakeBridgeRoom)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    rc = asyncio.run(cli._join(
        "https://voicehook.example/r/blau-tiger-wald-AB12?invite=a.b.c", None, "Claude", True,
        model="opus-5.5", no_greet=True, idle_timeout=0, control=False, transport=transport))
    out, err = capsys.readouterr()
    return out, err, calls + [("rc", rc)]


def test_auto_falls_back_to_bridge_after_webrtc_failure(monkeypatch, capsys):
    out, err, calls = _run_join(monkeypatch, capsys, "auto")
    metas = [json.loads(line)["text"] for line in out.splitlines() if line.startswith("{")]
    assert any(m.startswith("transport=webrtc (auto") for m in metas)
    assert any("retrying once via the HTTPS bridge" in m for m in metas)
    assert any(m.startswith("connected — 1 peers") for m in metas)
    assert "livekit connect failed" in err and "retrying once via the HTTPS bridge" in err
    assert calls[0] == ("mint",)
    assert ("bridge-connect", calls[1][1], "a.b.c") == calls[1]
    assert calls[-1] == ("rc", 0)
    assert sum(1 for c in calls if c == ("mint",)) == 1  # exactly one WebRTC attempt


def test_auto_with_https_proxy_goes_straight_to_bridge(monkeypatch, capsys):
    out, err, calls = _run_join(monkeypatch, capsys, "auto", {"HTTPS_PROXY": "http://proxy:3128"})
    assert ("mint",) not in calls
    assert calls[0][0] == "bridge-connect"
    assert "transport=bridge (auto: HTTPS_PROXY is set" in out


def test_forced_webrtc_never_uses_bridge(monkeypatch, capsys):
    import voicehook_agent.relay as relay
    monkeypatch.setattr(relay, "backoff_delays", lambda **k: iter([]))  # stop after 1st failure
    bridged = []
    monkeypatch.setattr(vt.BridgeRoom, "connect", lambda *a, **k: bridged.append(1))
    with pytest.raises(RuntimeError):  # StopIteration from the empty backoff = loop wanted a retry
        _run_join(monkeypatch, capsys, "webrtc", {"HTTPS_PROXY": "http://proxy:3128"})
    out, err = capsys.readouterr()
    assert "transport=webrtc (--transport webrtc)" in out
    assert "retrying once via the HTTPS bridge" not in out and not bridged
