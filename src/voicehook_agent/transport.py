"""Transport choice + the HTTPS bridge client.

Default transport is WebRTC (livekit.rtc, direct to the LiveKit server). In
cloud sandboxes that only allow HTTPS through an HTTP CONNECT proxy, WebRTC
cannot connect (libwebrtc ignores HTTPS_PROXY, the server has no TURN). Then
the CLI talks to the voicehook server's HTTPS bridge instead: the server joins
the room for us with the same token and attributes, and relays the data
channel over plain HTTPS (POST up, Server-Sent Events down).

`BridgeRoom` duck-types the parts of `livekit.rtc.Room` the CLI uses (`on`,
`connect`, `disconnect`, `remote_participants`, `local_participant.publish_data`),
so the join loop, its output and the control socket stay identical.

httpx reads HTTP(S)_PROXY / ALL_PROXY / NO_PROXY from the environment
(trust_env, the default), so the bridge works through the sandbox proxy.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import AsyncIterator, Callable
from types import SimpleNamespace
from typing import Any

import httpx

from . import __version__

TRANSPORTS = ("auto", "webrtc", "bridge")
PROXY_VARS = ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy")

# Bridge end reasons -> the LiveKit DisconnectReason names the join loop knows.
# Terminal ones end the session, everything else makes the CLI rejoin.
_END_REASON = {
    "left": "CLIENT_INITIATED",
    "idle_timeout": "CLIENT_INITIATED",
    "max_duration": "ROOM_CLOSED",
    "sse_gone": "BRIDGE_LOST",
}

SSE_READ_TIMEOUT = 45.0   # server pings every 15 s
# Server restart (deploy): the server keeps bridge sessions and restores them under the
# SAME session token (voicehook-v4 #158). So a dropped stream, a 502/503/504 from the
# proxy in front, or an event with "reconnect": true is retried with that token for up
# to BRIDGE_RETRY_S; only then the bridge counts as lost (and the join loop rejoins).
BRIDGE_RETRY_S = 30.0
RETRY_BACKOFF_MAX_S = 4.0
RETRY_STATUS = frozenset({502, 503, 504})
# Errors where the request never reached the app (safe to resend a say).
_SEND_RETRY_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout,
                      httpx.RemoteProtocolError)


class BridgeError(RuntimeError):
    def __init__(self, msg: str, status: int | None = None, body: dict | None = None) -> None:
        super().__init__(msg)
        self.status = status  # HTTP status of a rejected bridge call (410 = call ended)
        self.body = body      # parsed JSON body (426: detail.cli_min/cli_latest/upgrade)


def proxy_env(env: dict | None = None) -> str | None:
    """Name of the first set proxy variable (HTTPS or ALL), else None."""
    env = os.environ if env is None else env
    for k in PROXY_VARS:
        if (env.get(k) or "").strip():
            return k
    return None


def initial_transport(mode: str, env: dict | None = None) -> tuple[str, str]:
    """(transport, reason) for the first connect attempt."""
    if mode == "webrtc":
        return "webrtc", "--transport webrtc"
    if mode == "bridge":
        return "bridge", "--transport bridge"
    var = proxy_env(env)
    if var:
        return "bridge", f"auto: {var} is set (WebRTC cannot pass an HTTP proxy)"
    return "webrtc", "auto: no proxy in env, WebRTC first, HTTPS bridge as fallback"


def should_fallback(mode: str, transport: str, reason: str | None) -> bool:
    """auto + WebRTC connect failed/timed out -> retry once via the bridge."""
    return mode == "auto" and transport == "webrtc" and reason == "CONNECT_FAILED"


async def parse_sse(lines: AsyncIterator[str]) -> AsyncIterator[dict]:
    """SSE lines -> event dicts (JSON `data:`). Comments (`: ping`) are skipped."""
    data: list[str] = []
    async for raw in lines:
        line = raw.rstrip("\r")
        if line == "":
            if data:
                try:
                    ev = json.loads("\n".join(data))
                except ValueError:
                    ev = None
                data = []
                if isinstance(ev, dict):
                    yield ev
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            data.append(line[5:].lstrip(" "))


class _Pub(SimpleNamespace):
    pass


class BridgeParticipant:
    """Remote participant as seen through the bridge (rtc.RemoteParticipant subset)."""

    def __init__(self, info: dict) -> None:
        self.identity = info.get("identity", "?")
        self.track_publications: dict[str, _Pub] = {}
        self.update(info)

    def update(self, info: dict) -> None:
        self.kind = int(info.get("kind", 0) or 0)
        self.name = info.get("name", "") or ""
        self.attributes = dict(info.get("attributes") or {})
        self.set_audio(bool(info.get("audio")))

    def set_audio(self, on: bool) -> None:
        if on:
            self.track_publications["audio"] = _Pub(kind=1, subscribed=True, muted=False)
        else:
            self.track_publications.pop("audio", None)


class _BridgeLocal:
    def __init__(self, room: BridgeRoom) -> None:
        self._room = room

    async def publish_data(self, payload: bytes | str, reliable: bool = True,
                           topic: str = "", **_: Any) -> None:
        if isinstance(payload, (bytes, bytearray)):
            payload = payload.decode("utf-8")
        await self._room._send(topic, json.loads(payload))


def _client(user_agent: str, timeout: Any = 15.0) -> httpx.AsyncClient:
    # trust_env=True (httpx default): HTTPS_PROXY/HTTP_PROXY/ALL_PROXY/NO_PROXY apply.
    # 0.12.0: X-VH-CLI = the server's version gate (426 below cli_min, see selfupdate.py)
    return httpx.AsyncClient(timeout=timeout, trust_env=True,
                             headers={"user-agent": user_agent, "x-vh-cli": __version__})


class BridgeRoom:
    """rtc.Room look-alike backed by POST /api/bridge/* + SSE /api/bridge/events."""

    def __init__(self, api_base: str, slug: str, identity: str, *, name: str, model: str,
                 invite: str | None = None, user_agent: str = "voicehook-agent",
                 client_factory: Callable[..., httpx.AsyncClient] | None = None,
                 username: str | None = None, voice: str | None = None) -> None:
        self.api_base = api_base.rstrip("/")
        self.slug = slug
        self.identity = identity
        self.name = name
        self.model = model
        self.invite = invite
        self.username = username
        self.voice = voice
        self.user_agent = user_agent
        self._client_factory = client_factory or _client
        self._http: httpx.AsyncClient | None = None
        self._session: str | None = None
        self._handlers: dict[str, list[Callable]] = {}
        self._sse_task: asyncio.Task | None = None
        self._closed = False
        self._fired_disconnect = False
        self.remote_participants: dict[str, BridgeParticipant] = {}
        self.local_participant = _BridgeLocal(self)
        self.expires_in: int | None = None
        self.cli_min: str | None = None      # 0.12.0: from the join answer
        self.cli_latest: str | None = None

    # ---- rtc.Room surface -------------------------------------------------- #
    def on(self, event: str, callback: Callable | None = None):
        def _reg(cb: Callable) -> Callable:
            self._handlers.setdefault(event, []).append(cb)
            return cb
        return _reg(callback) if callback is not None else _reg

    def emit(self, event: str, *args: Any) -> None:
        for cb in list(self._handlers.get(event, [])):
            try:
                cb(*args)
            except Exception as e:  # noqa: BLE001
                print(f"[error] bridge handler {event}: {e!r}", file=sys.stderr, flush=True)

    async def connect(self, url: str | None = None, token: str | None = None,
                      options: Any = None) -> None:
        self._http = self._client_factory(self.user_agent)
        body: dict = {"invite_url": f"{self.api_base}/r/{self.slug}", "name": self.name,
                      "model": self.model, "identity": self.identity,
                      # the CLI runs its own idle guard (--idle-timeout) and persona guard
                      "idle_timeout": 0}
        if self.invite:
            body["invite"] = self.invite
        if self.username:  # 0.9.0: -> participant attribute vh.user
            body["username"] = self.username
        if self.voice:  # 0.11.0: -> participant attribute vh.voice
            body["voice"] = self.voice
        try:
            r = await self._http.post(f"{self.api_base}/api/bridge/join", json=body)
        except httpx.HTTPError as e:
            await self._close_http()
            raise BridgeError(f"bridge join failed: {e!r}") from e
        if r.status_code != 200:
            detail = _detail(r)
            try:
                body = r.json()
            except Exception:  # noqa: BLE001
                body = None
            await self._close_http()
            raise BridgeError(f"bridge join failed: HTTP {r.status_code} {detail}",
                              status=r.status_code, body=body if isinstance(body, dict) else None)
        j = r.json()
        self._session = j["session"]
        self.cli_min = j.get("cli_min")
        self.cli_latest = j.get("cli_latest")
        self.identity = j.get("identity") or self.identity
        self.expires_in = j.get("expires_in")
        for info in j.get("peers") or []:
            p = BridgeParticipant(info)
            self.remote_participants[p.identity] = p
        self._sse_task = asyncio.create_task(self._sse_loop())

    async def disconnect(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._session and self._http is not None:
            try:
                await self._http.post(f"{self.api_base}/api/bridge/leave", headers=self._auth(),
                                      json={})
            except httpx.HTTPError:
                pass
        if self._sse_task is not None:
            self._sse_task.cancel()
            try:
                await self._sse_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await self._close_http()

    # ---- internals ---------------------------------------------------------- #
    def _auth(self) -> dict:
        return {"authorization": f"Bearer {self._session}"}

    async def _close_http(self) -> None:
        if self._http is not None:
            try:
                await self._http.aclose()
            except Exception:  # noqa: BLE001
                pass
            self._http = None

    async def _send(self, topic: str, payload: Any) -> None:
        """POST /api/bridge/send. During a server restart (connection refused, 502/503/504)
        the same request is retried with the same token for up to BRIDGE_RETRY_S, so a
        `say` sent mid-deploy is delivered late instead of lost."""
        if self._http is None or not self._session or self._closed:
            raise BridgeError("bridge not connected")
        loop = asyncio.get_running_loop()
        deadline: float | None = None
        delay = 0.5
        while True:
            err: Exception | None = None
            try:
                r = await self._http.post(f"{self.api_base}/api/bridge/send", headers=self._auth(),
                                          json={"topic": topic, "payload": payload, "force": True})
            except _SEND_RETRY_ERRORS as e:
                r, err = None, e
            if r is not None and r.status_code not in RETRY_STATUS:
                break
            now = loop.time()
            deadline = now + BRIDGE_RETRY_S if deadline is None else deadline
            if now >= deadline or self._closed or self._http is None:
                what = f"HTTP {r.status_code}" if r is not None else repr(err)
                raise BridgeError(f"bridge send {topic} failed after retry: {what}",
                                  status=r.status_code if r is not None else None)
            await asyncio.sleep(min(delay, max(0.0, deadline - now)))
            delay = min(delay * 2, RETRY_BACKOFF_MAX_S)
        if r.status_code != 200:
            raise BridgeError(f"bridge send {topic} failed: HTTP {r.status_code} {_detail(r)}",
                              status=r.status_code)

    def _fire_disconnected(self, reason: str) -> None:
        if self._fired_disconnect:
            return
        self._fired_disconnect = True
        self.emit("disconnected", reason)

    async def _sse_loop(self) -> None:
        """Own connection for the stream. Dropped stream, 5xx or `"reconnect": true` ->
        reconnect with the SAME session token for up to BRIDGE_RETRY_S (server restart,
        deploy, proxy hiccup); 401/404/410 or a longer outage -> BRIDGE_LOST."""
        loop = asyncio.get_running_loop()
        fails = 0
        down_since: float | None = None
        timeout = httpx.Timeout(15.0, read=SSE_READ_TIMEOUT)
        async with self._client_factory(self.user_agent, timeout) as cli:
            while not self._closed:
                reconnect_now = False
                try:
                    async with cli.stream("GET", f"{self.api_base}/api/bridge/events",
                                          headers={**self._auth(), "accept": "text/event-stream"}) as r:
                        if r.status_code in (401, 404, 410):
                            self._fire_disconnected("BRIDGE_LOST")
                            return
                        r.raise_for_status()
                        if down_since is not None:
                            self.emit("reconnected")
                        fails, down_since = 0, None
                        async for ev in parse_sse(r.aiter_lines()):
                            if ev.get("type") == "ended":
                                reason = str(ev.get("reason") or "UNKNOWN")
                                self._fire_disconnected(_END_REASON.get(reason, reason))
                                return
                            if ev.get("reconnect") is True:  # server restarts: ask again now
                                reconnect_now = True
                                break
                            self._dispatch(ev)
                except asyncio.CancelledError:
                    raise
                except (httpx.HTTPError, ValueError):
                    pass
                if self._closed:
                    return
                now = loop.time()
                if down_since is None:
                    down_since = now
                    self.emit("reconnecting")
                elif now - down_since >= BRIDGE_RETRY_S:
                    self._fire_disconnected("BRIDGE_LOST")
                    return
                fails += 1
                wait = 0.2 if reconnect_now else min(0.5 * 2.0 ** (fails - 1), RETRY_BACKOFF_MAX_S)
                await asyncio.sleep(min(wait, max(0.0, down_since + BRIDGE_RETRY_S - now)))

    def _participant(self, info: dict) -> BridgeParticipant:
        ident = info.get("identity", "?")
        p = self.remote_participants.get(ident)
        if p is None:
            p = BridgeParticipant(info)
            self.remote_participants[ident] = p
        else:
            p.update(info)
        return p

    def _dispatch(self, ev: dict) -> None:
        t = ev.get("type")
        if t == "data":
            sender = ev.get("sender")
            pkt = SimpleNamespace(
                topic=ev.get("topic") or "",
                data=json.dumps(ev.get("payload"), ensure_ascii=False).encode("utf-8"),
                participant=SimpleNamespace(identity=sender) if sender else None,
            )
            self.emit("data_received", pkt)
        elif t == "peer-joined":
            known = ev.get("identity") in self.remote_participants
            p = self._participant(ev)
            if not known:
                self.emit("participant_connected", p)
                if ev.get("audio"):
                    self.emit("track_subscribed", _Pub(kind=1), _Pub(kind=1), p)
        elif t == "peer-left":
            p = self.remote_participants.pop(ev.get("identity"), None) or BridgeParticipant(ev)
            self.emit("participant_disconnected", p)
        elif t == "peer-updated":
            self._participant(ev)
        elif t == "speakers":
            self.emit("active_speakers_changed",
                      [SimpleNamespace(identity=i) for i in ev.get("speakers") or []])
        elif t == "track":
            p = self.remote_participants.get(ev.get("identity"))
            if p is None:
                return
            state = ev.get("state")
            pub = _Pub(kind=1)
            if state == "on":
                p.set_audio(True)
                self.emit("track_subscribed", pub, pub, p)
            elif state == "off":
                p.set_audio(False)
                self.emit("track_unsubscribed", pub, pub, p)
            elif state == "mute":
                self.emit("track_muted", p, pub)
            elif state == "unmute":
                self.emit("track_unmuted", p, pub)
        elif t in ("hello", "room-state"):
            for info in ev.get("peers") or []:
                known = info.get("identity") in self.remote_participants
                p = self._participant(info)
                if not known:
                    self.emit("participant_connected", p)
        elif t in ("reconnecting", "reconnected"):
            self.emit(t)


def _detail(r: httpx.Response) -> str:
    try:
        return str(r.json().get("detail", ""))[:300]
    except Exception:  # noqa: BLE001
        return (r.text or "")[:300]
