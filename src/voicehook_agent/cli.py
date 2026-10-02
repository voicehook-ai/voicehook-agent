"""voicehook-agent CLI.

Usage:
    voicehook-agent join <invite-url> --name N --model M          # interactive mode
    voicehook-agent join <invite-url> --name N --model M --json   # JSONL stream mode

Agent loop without polling (against the running join, via a local socket):
    voicehook-agent say "Hallo, ich bin da."     # speak one line
    voicehook-agent next --timeout 60            # block until the next user turn
    voicehook-agent leave                        # end the join cleanly
    voicehook-agent status                       # connection + room state

stdout: incoming user turns + voice-ai turns, one per line
        plain mode:  [role] text
        json mode:   {"role":"user","text":"..."}\n
stdin:  one line per turn → published as operator.say  (voice-ai speaks it via TTS)
        json mode:   {"text":"..."} or {"topic":"operator.persona","text":"..."}
        live context: {"topic":"operator.graph","text":"<current state>"} — held in
        memory, pushed as operator.persona every --graph-interval sec + per turn.

Relay-hardening flags (see PR "Relay hardening …"):
    --keep-alive / --no-keep-alive   stdin-EOF does NOT quit; reconnect on
                                     transient disconnect (default: on)        (#6)
    --notify-url <url>               POST a wake payload per finalized turn     (#12)
    --wake-only-user / --wake-all    only role=user wakes (default user-only)   (#12)
    --suppress-echo                  drop our own relayed TTS from the stream   (#10)
    --say-ttl <sec>                  drop stale/superseded operator.say           (#9)
    --strict-relay                   inject a strict-relay persona at connect   (#8)
    --graph <file>                   optional live-context seed (stdin updates live)
    --graph-interval <sec>           cadence of the live-context push (default 60)

Transport (0.6.0):
    --transport auto|webrtc|bridge   auto (default): WebRTC, but the HTTPS bridge
                                     when HTTPS_PROXY/ALL_PROXY is set or WebRTC
                                     fails to connect (cloud sandboxes). Output,
                                     say/next/leave and the FIFO stay identical.

Status board (0.7.0):
    status [TEXT] [--doing T] [--open T]... [--done T]... [-f board.json]
                                     send operator.status {doing, open[], done[]};
                                     replaces the last board, never spoken; '' clears.
    next                             also yields {type:"status_request"} (the user asked
                                     what you are doing: send `status` at once) and adds
                                     status_stale / latency_warning hints.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import signal
import sys
import time
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import urlparse

import httpx
from livekit import rtc

from . import __version__, relay
from . import session as vsession
from . import transport as vtransport

_SLUG_RX = re.compile(r"^[a-z]+-[a-z]+-[a-z]+-[A-Z0-9]{4,8}$")
_VERSION = __version__
_USER_AGENT = f"voicehook-agent/{_VERSION}"

# Bundled persona template for --strict-relay (#8). Shipped inside the package
# (works for installed wheels); falls back to repo-root personas/ in a source
# checkout, then to an inline template (see _load_persona).
_STRICT_RELAY_PERSONA_CANDIDATES = (
    Path(__file__).resolve().parent / "strict-relay.txt",
    Path(__file__).resolve().parents[2] / "personas" / "strict-relay.txt",
)

# Control topics the CLI relays. The CLI is topic-agnostic on publish, but we
# document/validate the known set so a typo'd topic is visible (#10 mentions a
# new `operator.backchannel` topic the CLI should pass through).
KNOWN_OUT_TOPICS = frozenset({
    "operator.say",
    "operator.persona",
    "operator.mode",
    "operator.interrupt",
    "operator.inject",
    "operator.backchannel",  # operator <-> agent silent side-channel (#10/F8)
})


def _parse_invite(url: str) -> tuple[str, str]:
    """Returns (api_base, slug). Accepts:
      https://voicehook.ai/r/<slug>[?go=1]
      https://example.com/r/<slug>
      <slug>                                   (uses VOICEHOOK_API_BASE env)
    """
    if "/" not in url and _SLUG_RX.match(url):
        base = os.environ.get("VOICEHOOK_API_BASE", "https://voicehook.ai")
        return base.rstrip("/"), url
    parsed = urlparse(url)
    if not parsed.scheme:
        raise ValueError(f"invalid invite URL: {url}")
    m = re.search(r"/r/([^/?]+)", parsed.path)
    if not m or not _SLUG_RX.match(m.group(1)):
        raise ValueError(f"no valid room slug in URL path: {parsed.path}")
    base = f"{parsed.scheme}://{parsed.netloc}"
    return base, m.group(1)


def _invite_code(url: str) -> str | None:
    """HMAC `?invite=` of an invite URL (the bridge verifies it), else None."""
    from urllib.parse import parse_qs
    q = parse_qs(urlparse(url).query or "")
    return (q.get("invite") or [None])[0]


OPERATOR_INVITE_REQUIRED_ERROR = (
    "[error] operator invite required: the server only lets an agent join with the\n"
    "  full invite link, including its ?invite=... part, e.g.\n"
    "    voicehook-agent join 'https://voicehook.ai/r/<slug>?invite=<code>' --name N --model M\n"
    "  A bare slug or a link without ?invite= is rejected. Ask the host for the full link."
)


class TokenMintError(Exception):
    """Token mint rejected. The message never contains the request URL, so the
    operator invite (`op_invite`) cannot leak into logs or stderr."""

    def __init__(self, status: int, detail: str):
        self.status = status
        self.detail = detail
        super().__init__(f"HTTP {status} {detail}".strip())


def _operator_invite(url: str) -> str | None:
    """The HMAC operator invite of a join URL, or None for a bare slug, a link
    without `?invite=`, or the legacy flag value `?invite=1`."""
    code = _invite_code(url) if "/" in url else None
    if not code or code == "1":
        return None
    return code


async def _mint_token(api_base: str, slug: str, identity: str,
                      name: str | None = None, model: str | None = None,
                      op_invite: str | None = None) -> dict:
    """Calls /api/token?room=...&identity=...&invite=1 — invite=1 prevents
    a second voice-ai dispatch (voice-ai is presumably already in the room
    if a user is talking to it; we join as the additional agent participant).

    `name` / `model` (the mandatory self-report) ride along: the server puts
    them into the JWT as LiveKit `name` + `attributes` (vh.name / vh.model), so
    the web call UI shows "Name · model" in the Agent chip. A plain token has
    no canUpdateOwnMetadata grant, so a runtime set_name/set_attributes would be
    rejected — the claims are the only path. Older servers ignore the params.

    `op_invite` is the HMAC `?invite=` value of the invite link; the server
    verifies it on this operator path (invalid -> 403 "invalid invite: ...",
    missing -> 403 "operator invite required" once enforced). Old servers
    ignore it. It is URL-encoded by httpx and never logged."""
    url = f"{api_base}/api/token"
    params = {"room": slug, "identity": identity, "invite": "1"}
    if name:
        params["name"] = name
    if model:
        params["model"] = model
    if op_invite:
        params["op_invite"] = op_invite
    async with httpx.AsyncClient(timeout=10.0, headers={"user-agent": _USER_AGENT}) as cli:
        r = await cli.get(url, params=params)
        if r.status_code >= 400:
            try:
                detail = (r.text or "").strip()[:200]
            except Exception:
                detail = ""
            if op_invite:
                detail = detail.replace(op_invite, "***")
            raise TokenMintError(r.status_code, detail)
        return r.json()


SELF_REPORT_ERROR = (
    "[error] Selbstauskunft fehlt: `join` braucht --name UND --model.\n"
    "  --name   dein Anzeigename im Call (wie du dich nennst, z.B. 'Claude', 'Hermes', 'Cursor')\n"
    "  --model  das exakte Modell, auf dem du gerade laeufst (z.B. 'opus-5.5', 'deepseek-v4-pro')\n"
    "Beide erscheinen im Agent-Chip der Web-UI als 'Name · Modell'. Nie eine Marke raten,\n"
    "die du nicht bist.\n"
    "Beispiel:\n"
    "  voicehook-agent join https://voicehook.ai/r/<slug> --name Claude --model opus-5.5 --json"
)


def _missing_self_report(name: str | None, model: str | None) -> list[str]:
    """Returns the missing mandatory self-report flags (empty list = ok)."""
    missing = []
    if not (name or "").strip():
        missing.append("--name")
    if not (model or "").strip():
        missing.append("--model")
    return missing


def _print_event(json_mode: bool, role: str, text: str, **extra) -> None:
    if json_mode:
        obj = {"role": role, "text": text, **extra}
        print(json.dumps(obj, ensure_ascii=False), flush=True)
    else:
        print(f"[{role}] {text}", flush=True)


def _emit_wake(json_mode: bool, payload: dict) -> None:
    """Emit a distinct wake/notify marker on stdout (#12). Uses a dedicated
    topic `_wake` so a monitor can grep it without colliding with transcript."""
    if json_mode:
        obj = {"role": "system", "text": "user-turn", "topic": "_wake", **payload}
        print(json.dumps(obj, ensure_ascii=False), flush=True)
    else:
        print(f"[wake] user-turn role={payload.get('role')} text={payload.get('text')!r}", flush=True)


def _load_persona(persona: str | None, persona_file: str | None,
                  strict_relay: bool) -> str | None:
    """Resolve --persona / --persona-file / --strict-relay into one text blob.

    Precedence: --persona  >  --persona-file  >  bundled strict-relay template
    (only when --strict-relay is set). --strict-relay reuses --persona-file
    semantics with a bundled template (#8). Returns None if nothing applies."""
    if persona:
        return persona
    if persona_file:
        with open(persona_file, "r", encoding="utf-8") as f:
            return f.read().rstrip()
    if strict_relay:
        for cand in _STRICT_RELAY_PERSONA_CANDIDATES:
            try:
                return cand.read_text(encoding="utf-8").rstrip()
            except OSError:
                continue
        # Fallback inline template so --strict-relay never silently no-ops
        # if the bundled file is missing from an install.
        return (
                "STRICT RELAY MODE. Du bist ein reines Mundstueck. Du sprichst "
                "AUSSCHLIESSLICH Text den der Operator per operator.say liefert. "
                "Du generierst NIEMALS eigene fachliche Inhalte, Zahlen oder "
                "Behauptungen. Bei Luecken: neutraler Filler, niemals raten. "
                "Wenn der User dich direkt anspricht: warte auf die naechste "
                "operator.say und gib sie wieder, antworte NIE selbst."
            )
    return None


def _compose_greet(
    *,
    name: str,
    username: str | None = None,
    topic: str | None = None,
    prompt: str | None = None,
) -> str:
    """Voice-friendly join greeting built from the self-report fields.

    "Hallo {username}, hier ist {name}. Ich bin dem Call beigetreten
    [, wir waren gerade dabei {topic}] [. {prompt}]"
    """
    salutation = f"Hallo {username}," if username else "Hallo,"
    greet = f"{salutation} hier ist {name}. Ich bin dem Call beigetreten"
    if topic:
        greet += f", wir waren gerade dabei {topic}."
    else:
        greet += "."
    if prompt:
        greet += f" {prompt}"
    return greet


def _kind_label(p) -> str:
    """ParticipantKind enum → short string ('user' / 'agent' / 'sip' / ...).
    LK proto: 0=standard(user), 1=ingress, 2=egress, 3=sip, 4=agent."""
    try:
        k = int(getattr(p, "kind", 0))
    except Exception:
        return "user"
    return {0: "user", 1: "ingress", 2: "egress", 3: "sip", 4: "agent"}.get(k, f"kind{k}")


async def _post_webhook(client: httpx.AsyncClient | None, url: str, payload: dict) -> None:
    if client is None:
        return
    try:
        await client.post(url, json=payload, timeout=5.0)
    except Exception as e:
        print(f"[error] notify-url POST failed: {e!r}", file=sys.stderr, flush=True)


async def _push_persona(room: rtc.Room, text: str) -> None:
    """Publish an operator.persona blob — the voice-ai swaps its live instructions."""
    payload = json.dumps({"text": text}, ensure_ascii=False).encode("utf-8")
    await room.local_participant.publish_data(payload, reliable=True, topic="operator.persona")


async def _ollama_summarize(
    client: httpx.AsyncClient | None,
    url: str,
    model: str,
    prompt: str,
    timeout: float,
) -> str | None:
    """Call a local Ollama to digest turns → short summary. None on any failure
    (caller falls back to the deterministic digest)."""
    if client is None:
        return None
    try:
        r = await client.post(
            f"{url}/api/generate",
            json={"model": model, "prompt": prompt, "stream": False},
            timeout=timeout,
        )
        r.raise_for_status()
        return (r.json().get("response") or "").strip() or None
    except Exception as e:  # noqa: BLE001
        print(f"[error] ollama summarize failed: {e!r}", file=sys.stderr, flush=True)
        return None


DEFAULT_IDLE_SAY = (
    "Ich verlasse den Call jetzt, weil ich von meinem Agenten seit einer Weile "
    "nichts mehr hoere. Lade mich gern wieder ein."
)


def _quit_signals() -> list:
    """Signals that make `join` leave cleanly. SIGHUP only when it is not
    ignored: `nohup voicehook-agent join ... &` sets SIG_IGN and must keep the
    call alive when the terminal closes."""
    sigs = [signal.SIGTERM]
    hup = getattr(signal, "SIGHUP", None)
    if hup is not None:
        try:
            ignored = signal.getsignal(hup) is signal.SIG_IGN
        except (ValueError, OSError):
            ignored = False
        if not ignored:
            sigs.append(hup)
    return sigs


class _Control:
    """State shared between the join's reconnect cycles and the local control
    socket (say / next / leave / status)."""

    def __init__(self, slug: str, ident: str, say_tracker: relay.SayTracker,
                 echo: relay.EchoSuppressor, idle_timeout: float) -> None:
        self.slug = slug
        self.ident = ident
        self.say_tracker = say_tracker
        self.echo = echo
        self.events = vsession.EventQueue()
        self.watchdog = vsession.IdleWatchdog(timeout=idle_timeout)
        self.quit = asyncio.Event()
        self.room: rtc.Room | None = None
        self.room_ready = asyncio.Event()
        self.clock = relay.TurnClock()  # 0.7.0: latency_warning + status_stale for `next`

    def attach(self, room: rtc.Room) -> None:
        self.room = room
        self.room_ready.set()

    def detach(self) -> None:
        self.room = None
        self.room_ready.clear()

    async def wait_room(self, timeout: float) -> rtc.Room | None:
        if self.room is not None:
            return self.room
        try:
            await asyncio.wait_for(self.room_ready.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return None
        return self.room


async def _publish_say(room: rtc.Room, text: str, extra: dict,
                       say_tracker: relay.SayTracker,
                       echo: relay.EchoSuppressor) -> dict:
    """Tag + publish one operator.say (same envelope as the stdin path)."""
    say = say_tracker.tag(text, extra=extra)
    stale, reason = say_tracker.is_stale(say)
    if stale:
        return {"ok": False, "error": f"dropped stale say: {reason}", "seq": say.seq}
    payload = say_tracker.envelope(say)
    echo.record_sent(say.text)
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    await room.local_participant.publish_data(data, reliable=True, topic="operator.say")
    return {"ok": True, "seq": say.seq}


_SAY_MODES = ("revise", "overwrite", "append")


async def _control_handler(ctl: _Control, req: dict) -> dict:
    """One request from `voicehook-agent say|next|leave|status`."""
    cmd = req.get("cmd")
    if cmd == "say":
        ctl.watchdog.touch()
        ctl.events.arm()  # from now on user turns are queued for `next`
        text = str(req.get("text") or "").strip()
        if not text:
            return {"ok": False, "error": "empty text"}
        extra: dict = {}
        mode = req.get("mode")
        if mode:
            if mode not in _SAY_MODES:
                return {"ok": False, "error": f"mode must be one of {_SAY_MODES}"}
            extra["mode"] = mode
        ctl.clock.said()
        room = await ctl.wait_room(10.0)
        if room is None:
            return {"ok": False, "error": "not connected to the room (yet)"}
        return await _publish_say(room, text, extra, ctl.say_tracker, ctl.echo)
    if cmd == "board":
        ctl.watchdog.touch()
        board = req.get("board")
        if not isinstance(board, dict):
            return {"ok": False, "error": "board must be an object {doing, open, done}"}
        room = await ctl.wait_room(10.0)
        if room is None:
            return {"ok": False, "error": "not connected to the room (yet)"}
        data = json.dumps(board, ensure_ascii=False).encode("utf-8")
        await room.local_participant.publish_data(data, reliable=True, topic="operator.status")
        ctl.clock.board()
        return {"ok": True, "type": "board", "board": board}
    if cmd == "next":
        try:
            timeout = float(req.get("timeout", 60))
        except (TypeError, ValueError):
            return {"ok": False, "error": "timeout must be a number"}
        ctl.watchdog.enter()
        try:
            ev = await ctl.events.get(timeout)
        finally:
            ctl.watchdog.leave()
        hints = ctl.clock.hints()
        if ev is None:
            return {"ok": True, "type": "timeout", "pending": 0, **hints}
        if ev.get("type") == "user":
            ctl.clock.delivered()
        return {"ok": True, **ev, "pending": len(ctl.events), **hints}
    if cmd == "leave":
        ctl.watchdog.touch()
        text = str(req.get("say") or "").strip()
        if text and ctl.room is not None:
            try:
                await _publish_say(ctl.room, text, {"mode": "append"},
                                   ctl.say_tracker, ctl.echo)
            except Exception as e:  # noqa: BLE001
                print(f"[error] leave say failed: {e!r}", file=sys.stderr, flush=True)
        ctl.quit.set()
        return {"ok": True, "type": "leaving"}
    if cmd == "status":
        room = ctl.room
        peers = []
        if room is not None:
            for p in room.remote_participants.values():
                attrs = dict(getattr(p, "attributes", None) or {})
                peers.append({"identity": p.identity, "kind": _kind_label(p),
                              "name": attrs.get("vh.name"), "model": attrs.get("vh.model"),
                              "operator": attrs.get("vh.role") == "agent"
                              and _kind_label(p) != "agent"})
        return {"ok": True, "type": "status", "room": ctl.slug, "identity": ctl.ident,
                "connected": room is not None, "pending": len(ctl.events),
                "idle_s": round(ctl.watchdog.idle_for(), 1),
                "idle_timeout_s": ctl.watchdog.timeout,
                "peers": sorted(peers, key=lambda d: d["identity"])}
    return {"ok": False, "error": f"unknown cmd {cmd!r}"}


async def _idle_loop(ctl: _Control, json_mode: bool, announce: str | None) -> None:
    """Orphan guard: leave the call when the brain sent no say/next/stdin line
    for `watchdog.timeout` seconds."""
    if ctl.watchdog.timeout <= 0:
        return
    tick = min(5.0, max(0.05, ctl.watchdog.timeout / 4))
    while not ctl.quit.is_set():
        try:
            await asyncio.wait_for(ctl.quit.wait(), timeout=tick)
            return
        except asyncio.TimeoutError:
            pass
        if not ctl.watchdog.expired():
            continue
        _print_event(json_mode, "system",
                     f"idle-timeout: no say/next from the agent for "
                     f"{ctl.watchdog.timeout:.0f}s, leaving", topic="_meta")
        if announce and ctl.room is not None:
            try:
                await _publish_say(ctl.room, announce, {"mode": "append"},
                                   ctl.say_tracker, ctl.echo)
                await asyncio.sleep(1.0)
            except Exception as e:  # noqa: BLE001
                print(f"[error] idle announce failed: {e!r}", file=sys.stderr, flush=True)
        ctl.quit.set()
        return


async def _stdin_publisher(
    room: rtc.Room,
    json_mode: bool,
    stop: asyncio.Event,
    *,
    keep_alive: bool,
    say_tracker: relay.SayTracker,
    echo: relay.EchoSuppressor,
    graph: relay.GraphHolder,
    turn_event: asyncio.Event,
    on_activity=None,
) -> None:
    """Reads stdin, publishes lines. Newline-tolerant (#11): a final chunk
    without a trailing newline is processed (with a warning) instead of being
    silently swallowed. On EOF, if keep_alive is on, this returns WITHOUT
    setting `stop` — the room stays connected until an explicit quit / SIGTERM
    / terminal disconnect (#6)."""
    loop = asyncio.get_running_loop()
    line_buf = relay.LineBuffer()

    async def _handle_line(line: str) -> None:
        line = line.strip()
        if not line:
            return
        if on_activity is not None:
            on_activity()  # the brain is alive (idle watchdog)
        # Explicit quit command (the only stdin-driven way to end the session
        # under keep-alive). Plain mode: "/q" or "/quit"; json: {"topic":"quit"}.
        if line in ("/q", "/quit"):
            stop.set()
            return
        if json_mode:
            try:
                obj = json.loads(line)
            except Exception as e:
                print(f"[error] invalid json on stdin: {e!r}", file=sys.stderr, flush=True)
                return
            topic = obj.get("topic", "operator.say")
            if topic == "quit":
                stop.set()
                return
            payload = {k: v for k, v in obj.items() if k != "topic"}
        else:
            topic = "operator.say"
            payload = {"text": line}

        # operator.graph = live-context update: hold in memory, do NOT publish.
        # The cadence loop pushes the latest as operator.persona every interval.
        if topic == "operator.graph":
            text = (payload.get("text") or "").strip() or None
            graph.set(text)
            turn_event.set()  # wake the loop → push immediately, then heartbeat
            return

        if topic.startswith("operator.") and topic not in KNOWN_OUT_TOPICS:
            print(f"[warn] unknown control topic {topic!r} (relaying anyway)",
                  file=sys.stderr, flush=True)

        # #9 — tag operator.say with seq/ts and drop if stale/superseded.
        if topic == "operator.say":
            extra = {k: v for k, v in payload.items() if k != "text"}
            say = say_tracker.tag(payload.get("text", ""), topic=topic, extra=extra)
            stale, reason = say_tracker.is_stale(say)
            if stale:
                print(f"[warn] dropped stale operator.say seq={say.seq}: {reason}",
                      file=sys.stderr, flush=True)
                return
            payload = say_tracker.envelope(say)
            echo.record_sent(say.text)  # #10 — remember for echo suppression

        try:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            await room.local_participant.publish_data(data, reliable=True, topic=topic)
        except Exception as e:
            print(f"[error] publish failed: {e!r}", file=sys.stderr, flush=True)

    while not stop.is_set():
        try:
            chunk = await loop.run_in_executor(None, sys.stdin.readline)
        except (EOFError, KeyboardInterrupt):
            chunk = ""
        if chunk == "":  # EOF
            # #11 — surface any held partial line BEFORE deciding on EOF.
            for tail, complete in line_buf.flush():
                if not complete:
                    print(f"[warn] stdin closed with un-terminated line "
                          f"({len(tail)} chars, no trailing newline) — processing it. "
                          f"Always end FIFO control lines with a newline.",
                          file=sys.stderr, flush=True)
                await _handle_line(tail)
            if keep_alive:
                # #6 — EOF must NOT quit. The room stays up; we just stop
                # reading stdin. The session ends only on quit / SIGTERM /
                # terminal disconnect (handled by the caller via `stop`).
                _print_event(json_mode, "system",
                             "stdin-EOF (keep-alive on): staying connected, "
                             "no more stdin reads", topic="_meta")
                return
            stop.set()
            return
        for line in line_buf.feed(chunk):
            await _handle_line(line)


async def _connect_and_listen(
    api_base: str,
    slug: str,
    ident: str,
    json_mode: bool,
    persona_text: str | None,
    *,
    keep_alive: bool,
    notifier: relay.TurnNotifier,
    echo: relay.EchoSuppressor,
    say_tracker: relay.SayTracker,
    notify_url: str | None,
    http_client: httpx.AsyncClient | None,
    read_stdin: bool,
    greet_text: str | None = None,
    graph: relay.GraphHolder | None = None,
    graph_interval: float = 60.0,
    strict: bool = False,
    agent_name: str | None = None,
    model: str | None = None,
    ctl: _Control | None = None,
    force_persona: bool = False,
    transport: str = "webrtc",
    invite: str | None = None,
    connect_timeout: float = 45.0,
) -> tuple[int, str | None]:
    """One connect→listen cycle. Returns (rc, disconnect_reason_name).
    disconnect_reason_name is None for a clean stdin-driven quit; otherwise the
    LK reason that ended the cycle (used by the reconnect loop in #6)."""
    tok: dict | None = None
    if transport == "bridge":
        # Server joins for us over HTTPS (same token/attributes as /api/token?invite=1).
        room = vtransport.BridgeRoom(api_base, slug, ident, name=agent_name or "agent",
                                     model=model or "unbekannt", invite=invite,
                                     user_agent=_USER_AGENT)
    else:
        try:
            tok = await _mint_token(api_base, slug, ident, name=agent_name, model=model,
                                    op_invite=invite)
        except TokenMintError as e:
            if e.status == 403 and "operator invite required" in e.detail.lower():
                print(OPERATOR_INVITE_REQUIRED_ERROR, file=sys.stderr, flush=True)
                return 3, "TOKEN_FORBIDDEN"
            if e.status == 403:
                print(f"[error] token mint rejected: {e}", file=sys.stderr, flush=True)
                return 3, "TOKEN_FORBIDDEN"
            print(f"[error] token mint failed: {e}", file=sys.stderr, flush=True)
            return 3, "TOKEN_MINT_FAILED"
        except httpx.HTTPError as e:
            # type only: the exception text can carry the request URL (op_invite)
            print(f"[error] token mint failed: {type(e).__name__}", file=sys.stderr)
            return 3, "TOKEN_MINT_FAILED"
        room = rtc.Room()
    stop = asyncio.Event()
    turn_event = asyncio.Event()  # set on finalized user-turn → graph re-sync
    disconnect_reason: dict[str, str | None] = {"name": None}

    audible: set[str] = set()
    speakers: set[str] = set()

    def _emit_meta(text: str, **extra) -> None:
        _print_event(json_mode, "system", text, topic="_meta", **extra)

    # ---- transcript / control inbound ------------------------------------- #
    def _on_data(pkt: rtc.DataPacket) -> None:
        topic = (pkt.topic or "").strip()
        try:
            payload = json.loads(bytes(pkt.data).decode("utf-8"))
        except Exception:
            return
        if topic == "transcript":
            role = payload.get("role", "?")
            text = payload.get("text", "")
            # #10 — drop our own relayed TTS echo from the operator stream.
            if echo.should_suppress(role, text):
                return
            _print_event(json_mode, role, text, topic=topic)
            # `voicehook-agent next` — queue finalized user turns.
            if ctl is not None:
                ev = relay.user_turn_event(role, text, payload)
                if ev is not None:
                    ctl.events.put_nowait(ev)
                    ctl.clock.user()
            # #9 — a fresh user turn supersedes any older queued say.
            if role == "user":
                say_tracker.note_user_turn()
                turn_event.set()  # graph-per-turn: refresh live context
            # #12 — wake on finalized user turns (deduped, role-filtered).
            decision = notifier.consider(role, text, payload, room=slug)
            if decision.wake and decision.payload is not None:
                _emit_wake(json_mode, decision.payload)
                if notify_url:
                    asyncio.create_task(_post_webhook(http_client, notify_url, decision.payload))
        elif topic.startswith("operator."):
            if topic == "operator.revise" and ctl is not None:
                ctl.events.put_nowait(relay.revise_event(payload))
            elif topic == "operator.status_request" and ctl is not None:
                ctl.events.put_nowait(relay.status_request_event(payload))
            sender = getattr(pkt.participant, "identity", "?") if pkt.participant else "?"
            _print_event(json_mode, "system",
                         f"({topic} from {sender}) {payload.get('text','')}", topic=topic)

    room.on("data_received", _on_data)

    def _on_participant_connected(p) -> None:
        _emit_meta(f"peer-joined: {p.identity} ({_kind_label(p)})")

    def _on_participant_disconnected(p) -> None:
        ident_ = p.identity
        audible.discard(ident_)
        speakers.discard(ident_)
        _emit_meta(f"peer-left: {ident_} ({_kind_label(p)})")

    room.on("participant_connected", _on_participant_connected)
    room.on("participant_disconnected", _on_participant_disconnected)

    def _on_active_speakers(spk_list) -> None:
        new_set = {s.identity for s in spk_list}
        speakers.clear()
        speakers.update(new_set)
        idents = sorted(new_set)
        _emit_meta(f"speaking: [{', '.join(idents)}]", speakers=idents)

    room.on("active_speakers_changed", _on_active_speakers)

    def _on_track_subscribed(track, publication, participant) -> None:
        if int(getattr(track, "kind", 0)) != 1:
            return
        audible.add(participant.identity)
        _emit_meta(f"audio-track-on: {participant.identity}")

    def _on_track_unsubscribed(track, publication, participant) -> None:
        if int(getattr(track, "kind", 0)) != 1:
            return
        audible.discard(participant.identity)
        _emit_meta(f"audio-track-off: {participant.identity}")

    room.on("track_subscribed", _on_track_subscribed)
    room.on("track_unsubscribed", _on_track_unsubscribed)

    def _on_track_muted(participant, publication) -> None:
        if int(getattr(publication, "kind", 0)) != 1:
            return
        _emit_meta(f"mic-mute: {participant.identity}")

    def _on_track_unmuted(participant, publication) -> None:
        if int(getattr(publication, "kind", 0)) != 1:
            return
        _emit_meta(f"mic-unmute: {participant.identity}")

    room.on("track_muted", _on_track_muted)
    room.on("track_unmuted", _on_track_unmuted)

    room.on("reconnecting", lambda *_: _emit_meta("reconnecting"))
    room.on("reconnected", lambda *_: _emit_meta("reconnected"))

    def _on_disconnected(*args) -> None:
        reason = args[0] if args else None
        try:
            reason_name = rtc.DisconnectReason.Name(int(reason)) if reason is not None else "UNKNOWN"
        except Exception:
            reason_name = str(reason)
        disconnect_reason["name"] = reason_name
        _emit_meta(f"room-disconnected reason={reason_name}")
        stop.set()

    room.on("disconnected", _on_disconnected)

    try:
        if tok is None:
            await room.connect()
        else:
            await asyncio.wait_for(room.connect(tok["url"], tok["token"]), timeout=connect_timeout)
    except Exception as e:
        what = "bridge" if transport == "bridge" else "livekit"
        print(f"[error] {what} connect failed: {e!r}", file=sys.stderr)
        try:
            await room.disconnect()
        except Exception:  # noqa: BLE001
            pass
        return 4, "CONNECT_FAILED"

    _print_event(
        json_mode, "system",
        f"connected — {len(room.remote_participants)} peers: "
        f"{[p.identity for p in room.remote_participants.values()]}",
        topic="_meta",
    )
    for p in room.remote_participants.values():
        for pub in p.track_publications.values():
            if int(getattr(pub, "kind", 0)) == 1 and getattr(pub, "subscribed", False):
                audible.add(p.identity)

    async def _heartbeat() -> None:
        last_sig: tuple | None = None
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=10.0)
                return
            except asyncio.TimeoutError:
                pass
            peers_list = []
            for p in room.remote_participants.values():
                peers_list.append({
                    "identity": p.identity,
                    "kind": _kind_label(p),
                    "audio": p.identity in audible,
                    "speaking": p.identity in speakers,
                })
            peers_list.sort(key=lambda x: x["identity"])
            sig = tuple((d["identity"], d["kind"], d["audio"], d["speaking"]) for d in peers_list)
            if sig == last_sig:
                continue
            last_sig = sig
            if json_mode:
                obj = {"role": "system", "text": "room-state", "topic": "_meta", "peers": peers_list}
                print(json.dumps(obj, ensure_ascii=False), flush=True)
            else:
                summary = ", ".join(
                    f"{d['identity']}({d['kind']}{'*' if d['speaking'] else ''}"
                    f"{'' if d['audio'] else ' no-audio'})"
                    for d in peers_list
                ) or "<empty>"
                print(f"[system] room-state: {summary}", flush=True)

    hb_task = asyncio.create_task(_heartbeat())

    if ctl is not None:
        ctl.attach(room)

    def _other_operators() -> list[str]:
        return relay.other_operators(
            (p.identity, _kind_label(p), dict(getattr(p, "attributes", None) or {}))
            for p in room.remote_participants.values()
        )

    # Persona guard: never overwrite the persona / mode another operator agent
    # already set in this room (--force-persona overrides).
    if (persona_text or strict) and not force_persona:
        others = _other_operators()
        if others:
            msg = (f"persona/mode NOT pushed: other operator agent in the room "
                   f"({', '.join(others)}); pass --force-persona to override")
            _print_event(json_mode, "system", msg, topic="_meta")
            print(f"[warn] {msg}", file=sys.stderr, flush=True)
            persona_text = None
            strict = False

    if persona_text:
        try:
            payload = json.dumps({"text": persona_text}, ensure_ascii=False).encode("utf-8")
            await room.local_participant.publish_data(payload, reliable=True, topic="operator.persona")
            _print_event(json_mode, "system", "persona auto-pushed", topic="_meta")
        except Exception as e:
            print(f"[error] persona auto-push failed: {e!r}", file=sys.stderr, flush=True)

    if strict:
        try:
            payload = json.dumps({"mode": "strict"}).encode("utf-8")
            await room.local_participant.publish_data(payload, reliable=True, topic="operator.mode")
            _print_event(json_mode, "system", "mode auto-pushed (strict)", topic="_meta")
        except Exception as e:
            print(f"[error] mode auto-push failed: {e!r}", file=sys.stderr, flush=True)

    if greet_text:
        try:
            payload = json.dumps({"text": greet_text}, ensure_ascii=False).encode("utf-8")
            await room.local_participant.publish_data(payload, reliable=True, topic="operator.say")
            _print_event(json_mode, "system", "greet auto-pushed", topic="_meta")
        except Exception as e:
            print(f"[error] greet auto-push failed: {e!r}", file=sys.stderr, flush=True)

    # ---- live-context sync: cadence loop pushes the latest graph ---- #
    async def _push_latest_graph() -> None:
        if graph is None:
            return
        text = graph.latest
        if not text:
            return
        if not force_persona:
            others = _other_operators()
            if others:
                _print_event(json_mode, "system",
                             f"graph NOT pushed: other operator agent in the room "
                             f"({', '.join(others)})", topic="_meta")
                return
        try:
            await _push_persona(room, text)
            _print_event(json_mode, "system", "graph pushed", topic="_meta")
        except Exception as e:  # noqa: BLE001
            print(f"[error] graph push failed: {e!r}", file=sys.stderr, flush=True)

    async def _graph_loop() -> None:
        if graph is None:
            return
        while not stop.is_set():
            try:
                await asyncio.wait_for(turn_event.wait(), timeout=graph_interval)
            except asyncio.TimeoutError:
                pass
            turn_event.clear()
            await _push_latest_graph()

    graph_task = asyncio.create_task(_graph_loop()) if graph is not None else None

    async def _quit_watch() -> None:
        if ctl is None:
            return
        await ctl.quit.wait()
        stop.set()

    quit_task = asyncio.create_task(_quit_watch())

    if not json_mode:
        print("[hint] type a line to operator.say (voice-ai speaks it). "
              "/q to quit (Ctrl-D no longer quits under --keep-alive).", flush=True)

    try:
        if read_stdin:
            stdin_task = asyncio.create_task(
                _stdin_publisher(room, json_mode, stop,
                                 keep_alive=keep_alive, say_tracker=say_tracker,
                                 echo=echo, graph=graph, turn_event=turn_event,
                                 on_activity=ctl.watchdog.touch if ctl else None)
            )
            await stop.wait()
            stdin_task.cancel()
            try:
                await stdin_task
            except (asyncio.CancelledError, Exception):
                pass
        else:
            # Reconnect cycle: no fresh stdin reader (the first cycle owns it).
            await stop.wait()
    finally:
        if ctl is not None:
            ctl.detach()
        for t in (hb_task, quit_task):
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        if graph_task is not None:
            graph_task.cancel()
            try:
                await graph_task
            except (asyncio.CancelledError, Exception):
                pass
        try:
            await room.disconnect()
        except Exception:
            pass
        _print_event(json_mode, "system", "disconnected", topic="_meta")

    if ctl is not None and ctl.quit.is_set():
        return 0, None  # leave / idle-timeout / SIGTERM: clean, no reconnect
    return 0, disconnect_reason["name"]


async def _join(
    invite_url: str,
    identity: str | None,
    agent_name: str | None,
    json_mode: bool,
    persona_text: str | None = None,
    *,
    keep_alive: bool = True,
    notify_url: str | None = None,
    wake_only_user: bool = True,
    suppress_echo: bool = False,
    say_ttl: float | None = None,
    model: str | None = None,
    topic: str | None = None,
    username: str | None = None,
    prompt: str | None = None,
    greet: str | None = None,
    no_greet: bool = False,
    graph_path: str | None = None,
    graph_interval: float = 60.0,
    strict: bool = False,
    idle_timeout: float = 600.0,
    idle_say: str | None = DEFAULT_IDLE_SAY,
    force_persona: bool = False,
    control: bool = True,
    transport: str = "auto",
) -> int:
    missing = _missing_self_report(agent_name, model)
    if missing:
        print(f"{SELF_REPORT_ERROR}\n(fehlt: {', '.join(missing)})", file=sys.stderr)
        return 2
    agent_name = agent_name.strip()
    model = model.strip()
    try:
        api_base, slug = _parse_invite(invite_url)
    except ValueError as e:
        print(f"[error] {e}", file=sys.stderr)
        return 2

    if not identity:
        import socket
        host = re.sub(r"[^a-z0-9]", "", socket.gethostname().lower())[:12] or "host"
        name = (agent_name or "agent").lower()
        name = re.sub(r"[^a-z0-9]", "", name)[:16] or "agent"
        identity = f"{name}-{host}-{os.urandom(2).hex()}"
    ident = identity

    # Self-report → voice-friendly auto-greet. --name + --model are mandatory
    # (checked above) — never hardcode a brand.
    greet_text: str | None = None
    if not no_greet:
        if greet:
            greet_text = greet
        else:
            greet_text = _compose_greet(
                name=agent_name, username=username, topic=topic, prompt=prompt,
            )

    _print_event(json_mode, "system",
                 f"connecting room={slug} as identity={ident} via {api_base}", topic="_meta")
    cur_transport, why = vtransport.initial_transport(transport)
    _print_event(json_mode, "system", f"transport={cur_transport} ({why})", topic="_meta")
    invite = _operator_invite(invite_url)

    # Shared state across reconnect cycles (#6): the notifier dedup, echo ring,
    # and say-seq counter persist so we don't re-wake on the same turn after a
    # transient reconnect.
    notifier = relay.TurnNotifier(wake_only_user=wake_only_user)
    echo = relay.EchoSuppressor(enabled=suppress_echo)
    say_tracker = relay.SayTracker(ttl=say_ttl)

    # Live-context holder — seeded once from --graph (if any), then fed live
    # via `operator.graph` stdin lines. The cadence loop pushes it every interval.
    graph = relay.GraphHolder()
    if graph_path:
        seed = relay.read_graph(graph_path)
        if seed.text:
            graph.set(seed.text)

    http_client = httpx.AsyncClient(headers={"user-agent": _USER_AGENT}) if notify_url else None

    # Local control socket for `say` / `next` / `leave` / `status` + idle guard.
    ctl = _Control(slug, ident, say_tracker, echo, idle_timeout)
    server: vsession.ControlServer | None = None
    sess_dir = vsession.session_dir(slug, ident)
    if control and hasattr(asyncio, "start_unix_server"):
        sess_dir.mkdir(parents=True, exist_ok=True)
        server = vsession.ControlServer(vsession.socket_path(sess_dir),
                                        lambda req: _control_handler(ctl, req))
        try:
            await server.start()
        except vsession.SessionError as e:
            hint = ("Options: `voicehook-agent leave --session "
                    f"{slug}/{ident}` first, join with a different --name/--identity, "
                    "or pass --no-control (stdin/FIFO only)."
                    if isinstance(e, vsession.SessionBusy) else
                    "Fix the directory, set a shorter VOICEHOOK_AGENT_HOME, or pass "
                    "--no-control (stdin/FIFO only).")
            print(f"[error] control socket: {e}\n        {hint}",
                  file=sys.stderr, flush=True)
            if http_client is not None:
                await http_client.aclose()
            return 2
        except OSError as e:
            print(f"[warn] control socket unavailable ({e!r}); say/next/leave "
                  f"will not work, stdin still does", file=sys.stderr, flush=True)
            server = None
        if server is not None:
            vsession.write_info(sess_dir, {"pid": os.getpid(), "room": slug,
                                           "identity": ident, "api_base": api_base,
                                           "started": time.time()})
            _print_event(json_mode, "system",
                         f"control socket ready: {server.sock_path}", topic="_meta")
    loop = asyncio.get_running_loop()
    for sig in _quit_signals():
        try:
            loop.add_signal_handler(sig, ctl.quit.set)
        except (NotImplementedError, RuntimeError, ValueError):
            pass
    idle_task = asyncio.create_task(_idle_loop(ctl, json_mode, idle_say))

    rc = 0
    try:
        backoff = relay.backoff_delays(base=1.0, factor=2.0, cap=30.0)
        first = True
        while True:
            rc, reason = await _connect_and_listen(
                api_base, slug, ident, json_mode, persona_text,
                keep_alive=keep_alive, notifier=notifier, echo=echo,
                say_tracker=say_tracker, notify_url=notify_url,
                http_client=http_client, read_stdin=first,
                greet_text=greet_text if first else None,
                graph=graph, graph_interval=graph_interval,
                strict=strict, agent_name=agent_name, model=model,
                ctl=ctl, force_persona=force_persona,
                transport=cur_transport, invite=invite,
            )
            if first and vtransport.should_fallback(transport, cur_transport, reason):
                cur_transport = "bridge"
                msg = ("webrtc connect failed or timed out; retrying once via the HTTPS "
                       "bridge (transport=bridge)")
                _print_event(json_mode, "system", msg, topic="_meta")
                print(f"[warn] {msg}", file=sys.stderr, flush=True)
                continue
            first = False
            # Clean quit (stdin /q or {"topic":"quit"}): reason is None.
            if reason is None:
                break
            # #6 — reconnect only on transient disconnects, and only when
            # keep-alive is on. Terminal reasons (host left / room closed /
            # client-initiated) end the session.
            # A 403 on the token mint (invite missing/invalid) will not heal by retrying.
            if (not keep_alive or relay.is_terminal_disconnect(reason)
                    or reason == "TOKEN_FORBIDDEN"):
                _print_event(json_mode, "system",
                             f"session ended (reason={reason}, keep_alive={keep_alive})",
                             topic="_meta")
                break
            delay = next(backoff)
            _print_event(json_mode, "system",
                         f"transient disconnect ({reason}) — reconnecting in {delay:.0f}s",
                         topic="_meta")
            try:
                await asyncio.wait_for(ctl.quit.wait(), timeout=delay)
                break  # leave / idle-timeout / SIGTERM during backoff
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                break
    finally:
        ctl.quit.set()
        idle_task.cancel()
        try:
            await idle_task
        except (asyncio.CancelledError, Exception):
            pass
        await ctl.events.close()  # a blocked `next` returns {"type":"ended"}
        if server is not None:
            await asyncio.sleep(0.05)
            await server.close()
            vsession.remove_info(sess_dir)
            try:
                sess_dir.rmdir()
            except OSError:
                pass
        if http_client is not None:
            try:
                await http_client.aclose()
            except Exception:
                pass
    return rc


# --------------------------------------------------------------------------- #
# log-summary — the "2nd micro agent": digest the call log into operator.graph
# --------------------------------------------------------------------------- #
def _parse_transcript(line: str) -> tuple[str, str] | None:
    """Extract (role, text) from a `--json` transcript line. None for noise."""
    try:
        obj = json.loads(line)
    except ValueError:
        return None
    if obj.get("topic") != "transcript":
        return None
    role = obj.get("role")
    text = (obj.get("text") or "").strip()
    if role not in ("user", "agent") or not text:
        return None
    return role, text


async def _follow_lines(path: str, from_start: bool = False) -> AsyncIterator[str]:
    """Yield complete new lines appended to `path` (tail -f semantics)."""
    f = open(path, "rb")
    f.seek(0 if from_start else 2)
    buf = b""
    loop = asyncio.get_running_loop()
    while True:
        chunk = await loop.run_in_executor(None, f.read)
        if chunk:
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                yield line.decode("utf-8", "replace")
        else:
            f.seek(0, 1)  # clear EOF flag so a grown file is picked up
            await asyncio.sleep(0.5)


async def _emit_graph(out_path: str | None, base: str, text: str) -> None:
    body = f"{base}\n\nWas bisher passiert ist:\n{text}" if base else f"Was bisher passiert ist:\n{text}"
    line = json.dumps({"topic": "operator.graph", "text": body}, ensure_ascii=False)
    if out_path:
        with open(out_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    else:
        print(line, flush=True)


async def _run_log_summary(args) -> int:
    summary = relay.RollingSummary(max_turns=args.max_turns)
    base = ""
    if args.base:
        try:
            base = open(args.base, encoding="utf-8").read().rstrip()
        except OSError as e:
            print(f"[error] cannot read --base: {e!r}", file=sys.stderr, flush=True)

    client = None
    if args.summarize:
        client = httpx.AsyncClient(timeout=args.timeout)

    last_emit = time.monotonic()
    try:
        async for line in _follow_lines(args.log, from_start=args.from_start):
            parsed = _parse_transcript(line)
            if parsed:
                summary.add(*parsed)
            if time.monotonic() - last_emit >= args.interval:
                turns = summary.turns
                if turns:
                    text = summary.deterministic()
                    if args.summarize:
                        got = await _ollama_summarize(
                            client, args.ollama_url, args.model,
                            relay.build_summary_prompt(turns), args.timeout,
                        )
                        text = got if got else text  # LLM down → deterministic
                    await _emit_graph(args.out, base, text)
                last_emit = time.monotonic()
        # final flush on EOF/interrupt
        turns = summary.turns
        if turns:
            text = summary.deterministic()
            if args.summarize:
                got = await _ollama_summarize(
                    client, args.ollama_url, args.model,
                    relay.build_summary_prompt(turns), args.timeout,
                )
                text = got if got else text
            await _emit_graph(args.out, base, text)
    finally:
        if client is not None:
            await client.aclose()
    return 0


def _client_request(args) -> dict:
    if args.cmd == "say":
        text = " ".join(args.text)
        if text == "-":
            text = sys.stdin.read()
        req = {"cmd": "say", "text": text.strip()}
        if args.mode:
            req["mode"] = args.mode
        return req
    if args.cmd == "next":
        return {"cmd": "next", "timeout": args.timeout}
    if args.cmd == "leave":
        return {"cmd": "leave", "say": args.say} if args.say else {"cmd": "leave"}
    if args.text is not None or args.doing is not None or args.open or args.done or args.file:
        file_obj = None
        if args.file:
            with open(args.file, encoding="utf-8") as fh:
                file_obj = json.load(fh)
        return {"cmd": "board", "board": relay.build_board(
            args.text, args.doing, args.open, args.done, file_obj)}
    return {"cmd": "status"}


def _run_client(args) -> int:
    """say / next / leave / status: one JSON line out. Exit 0 = ok (incl.
    next timeout), 1 = request failed, 3 = no running join / join ended."""
    try:
        req = _client_request(args)
    except (OSError, ValueError) as e:
        print(json.dumps({"ok": False, "type": "error", "error": str(e)}, ensure_ascii=False), flush=True)
        return 1
    try:
        sock = vsession.resolve_socket(args.session, wait=args.wait)
        sock_timeout = None if args.cmd == "next" else 30.0
        reply = vsession.request(sock, req, timeout=sock_timeout)
    except vsession.SessionError as e:
        print(json.dumps({"ok": False, "type": "no-session", "error": str(e)},
                         ensure_ascii=False), flush=True)
        return 3
    except OSError as e:
        print(json.dumps({"ok": False, "type": "no-session", "error": repr(e)},
                         ensure_ascii=False), flush=True)
        return 3
    print(json.dumps(reply, ensure_ascii=False), flush=True)
    if reply.get("type") == "ended":
        return 3
    return 0 if reply.get("ok") else 1


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="voicehook-agent", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"voicehook-agent {_VERSION}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_join = sub.add_parser("join", help="join a voicehook.ai call as an agent")
    p_join.add_argument("invite_url", help="full invite link https://voicehook.ai/r/<slug>?invite=<code>  OR  bare <slug>")
    p_join.add_argument("--name", default=None, help="REQUIRED. Your display name in the call (e.g. 'Claude', 'Hermes', 'Cursor'). Shown in the web Agent chip as 'Name · model', becomes the identity prefix and the name spoken in the auto-greet. Never claim a vendor you are not.")
    p_join.add_argument("--identity", default=None, help="explicit full identity (overrides --name)")
    p_join.add_argument("--model", default=None, help="REQUIRED. Self-report: the exact model you run on (e.g. 'opus-5.5', 'deepseek-v4-pro'). Shown in the web Agent chip.")
    p_join.add_argument("--topic", default=None, help="self-report: what the call is about (<=5 words), spoken as 'wir waren gerade dabei {topic}'.")
    p_join.add_argument("--username", default=None, help="the host's name, spoken in the salutation ('Hallo {username},'). Omitted if unknown.")
    p_join.add_argument("--prompt", default=None, help="extra sentence appended after the auto-greet.")
    p_join.add_argument("--greet", default=None, help="full custom greeting (overrides the composed template).")
    p_join.add_argument("--no-greet", action="store_true", default=False, help="disable the auto-greet entirely.")
    p_join.add_argument("--json", action="store_true", help="JSONL stream mode on stdin/stdout")
    p_join.add_argument(
        "--persona", default=None,
        help="inline persona text to auto-push as operator.persona right after connect. Wins over --persona-file and --strict-relay.",
    )
    p_join.add_argument(
        "--persona-file", default=None,
        help="path to a UTF-8 file with persona text; auto-pushed as operator.persona after connect. Example: --persona-file personas/claude-default.txt",
    )
    # #6 keep-alive
    p_join.add_argument(
        "--keep-alive", dest="keep_alive", action="store_true", default=True,
        help="stdin-EOF does NOT quit; auto-reconnect on transient disconnect until host leaves / room closes / explicit quit (default: on). [#6]",
    )
    p_join.add_argument(
        "--no-keep-alive", dest="keep_alive", action="store_false",
        help="legacy behaviour: quit on stdin-EOF, no reconnect.",
    )
    # #12 notify / wake
    p_join.add_argument(
        "--notify-url", default=None,
        help="HTTP endpoint POSTed a wake payload {role,text,room,timestamp} on each finalized user-turn. [#12]",
    )
    p_join.add_argument(
        "--wake-only-user", dest="wake_only_user", action="store_true", default=True,
        help="only role=user finalized turns emit a wake marker (default: on; role=agent echo never wakes). [#12]",
    )
    p_join.add_argument(
        "--wake-all", dest="wake_only_user", action="store_false",
        help="emit wake markers for agent turns too (debug; risks loops).",
    )
    # #10 echo suppression
    p_join.add_argument(
        "--suppress-echo", action="store_true", default=False,
        help="drop the agent's own relayed TTS (role=agent transcript matching a recent operator.say) from the operator stream. [#10]",
    )
    # #9 say TTL
    p_join.add_argument(
        "--say-ttl", type=float, default=None, metavar="SEC",
        help="drop a operator.say that is older than SEC seconds or superseded by a newer user-turn, instead of sending it stale. [#9]",
    )
    # #8 strict relay
    p_join.add_argument(
        "--strict-relay", action="store_true", default=False,
        help="inject a bundled strict-relay persona at connect: voicebot speaks ONLY pushed text, never self-generates. Overridden by --persona/--persona-file. [#8]",
    )
    # live-context sync (status loop + graph-per-turn)
    p_join.add_argument(
        "--graph", default=None, metavar="PATH",
        help="optional seed file: read once at connect into the live-context holder. Live updates arrive via `operator.graph` stdin lines, pushed as operator.persona every --graph-interval sec + on each finalized user-turn.",
    )
    p_join.add_argument(
        "--graph-interval", type=float, default=60.0, metavar="SEC",
        help="seconds between graph auto-pushes (default 60).",
    )
    # persona guard + orphan guard + control socket
    p_join.add_argument(
        "--force-persona", action="store_true", default=False,
        help="push --persona/--persona-file/--strict-relay/--graph even when another operator agent is already in the room (default: skip, never overwrite someone else's persona).",
    )
    p_join.add_argument(
        "--idle-timeout", type=float, default=10.0, metavar="MIN",
        help="leave the call (with a short announcement) when the agent sent no say/next/stdin line for MIN minutes; prevents orphaned joins. 0 = off. Default 10.",
    )
    p_join.add_argument(
        "--idle-say", default=DEFAULT_IDLE_SAY,
        help="what voice-ai says before an idle-timeout leave ('' = leave silently).",
    )
    p_join.add_argument(
        "--transport", choices=vtransport.TRANSPORTS, default="auto",
        help="auto (default): WebRTC, switch to the HTTPS bridge when HTTPS_PROXY/ALL_PROXY is set or WebRTC fails to connect. webrtc / bridge force one.",
    )
    p_join.add_argument(
        "--no-control", action="store_true", default=False,
        help="do not open the local control socket (disables say/next/leave/status).",
    )
    # one-shot commands against a running join
    def _add_session(p):
        p.add_argument("--session", default=None, metavar="SLUG",
                       help="<slug>/<identity> of the running join (a slug alone is enough when only one join runs in that room); needed only when several joins run.")
        p.add_argument("--wait", type=float, default=30.0, metavar="SEC",
                       help="wait up to SEC seconds for the join to come up (default 30).")
    p_say = sub.add_parser("say", help="speak one line through voice-ai in the running join")
    p_say.add_argument("text", nargs="+", help="text to speak ('-' reads it from stdin)")
    p_say.add_argument("--mode", choices=_SAY_MODES, default=None,
                       help="operator.say mode (server default: revise). Answer an operator.revise with --mode overwrite.")
    _add_session(p_say)
    p_next = sub.add_parser("next", help="block until the next finalized user turn (or operator.revise); prints one JSON line")
    p_next.add_argument("--timeout", type=float, default=60.0, metavar="SEC",
                        help="give up after SEC seconds and print {\"type\":\"timeout\"} (default 60; 0 = only return what is queued).")
    _add_session(p_next)
    p_leave = sub.add_parser("leave", help="end the running join cleanly")
    p_leave.add_argument("--say", default=None, metavar="TEXT", help="goodbye line spoken before leaving")
    _add_session(p_leave)
    p_status = sub.add_parser(
        "status", help="no args: show the running join's state as JSON; with text/flags: send "
        "your status board (operator.status, replaces the last one, never spoken)")
    p_status.add_argument("text", nargs="?", default=None,
                          help="what you are doing right now, one sentence ('' = finished, clears)")
    p_status.add_argument("--doing", default=None, help="current task (same as TEXT)")
    p_status.add_argument("--open", action="append", default=[], metavar="TASK", help="open task (repeatable)")
    p_status.add_argument("--done", action="append", default=[], metavar="TASK", help="finished task (repeatable)")
    p_status.add_argument("-f", "--file", default=None, metavar="JSON",
                          help="board file {doing, open[], done[]}; flags add to it")
    _add_session(p_status)
    # log-summary: the 2nd micro agent (digests the call log into operator.graph)
    p_sum = sub.add_parser("log-summary", help="watch a call log and emit operator.graph digests")
    p_sum.add_argument("log", help="path to the CLI's --json transcript log (JSONL)")
    p_sum.add_argument("--interval", type=float, default=60.0, metavar="SEC",
                       help="emit cadence (default 60).")
    p_sum.add_argument("--max-turns", type=int, default=8,
                       help="turns kept in the rolling digest (default 8).")
    p_sum.add_argument("--out", default=None, metavar="PATH",
                       help="append operator.graph JSON lines here (FIFO/file); default stdout.")
    p_sum.add_argument("--base", default=None, metavar="FILE",
                       help="static context (identity + task) prepended to every digest.")
    p_sum.add_argument("--summarize", action="store_true", default=False,
                       help="use a local LLM (Ollama) to compress turns into a real summary.")
    p_sum.add_argument("--model", default="gemma3:4b", help="Ollama model (default gemma3:4b).")
    p_sum.add_argument("--ollama-url", default="http://127.0.0.1:11434",
                       help="Ollama base URL (default http://127.0.0.1:11434).")
    p_sum.add_argument("--timeout", type=float, default=30.0, metavar="SEC",
                       help="Ollama request timeout (default 30).")
    p_sum.add_argument("--from-start", action="store_true", default=False,
                       help="process existing log content too (default: tail from end).")
    args = ap.parse_args(argv)
    if args.cmd == "join":
        missing = _missing_self_report(args.name, args.model)
        if missing:
            print(f"{SELF_REPORT_ERROR}\n(fehlt: {', '.join(missing)})", file=sys.stderr)
            sys.exit(2)
        try:
            persona_text = _load_persona(args.persona, args.persona_file, args.strict_relay)
        except OSError as e:
            print(f"[error] could not read --persona-file: {e!r}", file=sys.stderr)
            sys.exit(2)
        try:
            rc = asyncio.run(_join(
                args.invite_url, args.identity, args.name, args.json, persona_text,
                keep_alive=args.keep_alive, notify_url=args.notify_url,
                wake_only_user=args.wake_only_user, suppress_echo=args.suppress_echo,
                say_ttl=args.say_ttl, model=args.model, topic=args.topic,
                username=args.username, prompt=args.prompt, greet=args.greet,
                no_greet=args.no_greet, graph_path=args.graph,
                graph_interval=args.graph_interval, strict=args.strict_relay,
                idle_timeout=max(0.0, args.idle_timeout) * 60.0,
                idle_say=args.idle_say or None,
                force_persona=args.force_persona, control=not args.no_control,
                transport=args.transport,
            ))
        except KeyboardInterrupt:
            rc = 130
        sys.exit(rc)
    if args.cmd in ("say", "next", "leave", "status"):
        sys.exit(_run_client(args))
    if args.cmd == "log-summary":
        try:
            rc = asyncio.run(_run_log_summary(args))
        except KeyboardInterrupt:
            rc = 130
        sys.exit(rc)


if __name__ == "__main__":
    main()
