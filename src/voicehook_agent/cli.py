"""voicehook-agent CLI.

Usage:
    voicehook-agent join <invite-url>           # interactive mode
    voicehook-agent join <invite-url> --json    # JSONL stream mode

stdout: incoming user turns + voice-ai turns, one per line
        plain mode:  [role] text
        json mode:   {"role":"user","text":"..."}\n
stdin:  one line per turn → published as senior.say  (voice-ai speaks it via TTS)
        json mode:   {"text":"..."} or {"topic":"senior.persona","text":"..."}

Relay-hardening flags (see PR "Relay hardening …"):
    --keep-alive / --no-keep-alive   stdin-EOF does NOT quit; reconnect on
                                     transient disconnect (default: on)        (#6)
    --notify-url <url>               POST a wake payload per finalized turn     (#12)
    --wake-only-user / --wake-all    only role=user wakes (default user-only)   (#12)
    --suppress-echo                  drop our own relayed TTS from the stream   (#10)
    --say-ttl <sec>                  drop stale/superseded senior.say           (#9)
    --strict-relay                   inject a strict-relay persona at connect   (#8)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

import httpx
from livekit import rtc

from . import relay

_SLUG_RX = re.compile(r"^[a-z]+-[a-z]+-[a-z]+-[A-Z0-9]{4,8}$")
_VERSION = "0.2.0"
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
# new `senior.backchannel` topic the CLI should pass through).
KNOWN_OUT_TOPICS = frozenset({
    "senior.say",
    "senior.persona",
    "senior.interrupt",
    "senior.inject",
    "senior.backchannel",  # operator <-> agent silent side-channel (#10/F8)
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


async def _mint_token(api_base: str, slug: str, identity: str) -> dict:
    """Calls /api/token?room=...&identity=...&invite=1 — invite=1 prevents
    a second voice-ai dispatch (voice-ai is presumably already in the room
    if a user is talking to it; we join as the additional agent participant)."""
    url = f"{api_base}/api/token"
    async with httpx.AsyncClient(timeout=10.0, headers={"user-agent": _USER_AGENT}) as cli:
        r = await cli.get(url, params={"room": slug, "identity": identity, "invite": "1"})
        r.raise_for_status()
        return r.json()


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
                "AUSSCHLIESSLICH Text den der Operator per senior.say liefert. "
                "Du generierst NIEMALS eigene fachliche Inhalte, Zahlen oder "
                "Behauptungen. Bei Luecken: neutraler Filler, niemals raten. "
                "Wenn der User dich direkt anspricht: warte auf die naechste "
                "senior.say und gib sie wieder, antworte NIE selbst."
            )
    return None


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


async def _stdin_publisher(
    room: rtc.Room,
    json_mode: bool,
    stop: asyncio.Event,
    *,
    keep_alive: bool,
    say_tracker: relay.SayTracker,
    echo: relay.EchoSuppressor,
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
            topic = obj.get("topic", "senior.say")
            if topic == "quit":
                stop.set()
                return
            payload = {k: v for k, v in obj.items() if k != "topic"}
        else:
            topic = "senior.say"
            payload = {"text": line}

        if topic.startswith("senior.") and topic not in KNOWN_OUT_TOPICS:
            print(f"[warn] unknown control topic {topic!r} (relaying anyway)",
                  file=sys.stderr, flush=True)

        # #9 — tag senior.say with seq/ts and drop if stale/superseded.
        if topic == "senior.say":
            extra = {k: v for k, v in payload.items() if k != "text"}
            say = say_tracker.tag(payload.get("text", ""), topic=topic, extra=extra)
            stale, reason = say_tracker.is_stale(say)
            if stale:
                print(f"[warn] dropped stale senior.say seq={say.seq}: {reason}",
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
) -> tuple[int, str | None]:
    """One connect→listen cycle. Returns (rc, disconnect_reason_name).
    disconnect_reason_name is None for a clean stdin-driven quit; otherwise the
    LK reason that ended the cycle (used by the reconnect loop in #6)."""
    try:
        tok = await _mint_token(api_base, slug, ident)
    except httpx.HTTPError as e:
        print(f"[error] token mint failed: {e!r}", file=sys.stderr)
        return 3, "TOKEN_MINT_FAILED"

    room = rtc.Room()
    stop = asyncio.Event()
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
            # #9 — a fresh user turn supersedes any older queued say.
            if role == "user":
                say_tracker.note_user_turn()
            # #12 — wake on finalized user turns (deduped, role-filtered).
            decision = notifier.consider(role, text, payload, room=slug)
            if decision.wake and decision.payload is not None:
                _emit_wake(json_mode, decision.payload)
                if notify_url:
                    asyncio.create_task(_post_webhook(http_client, notify_url, decision.payload))
        elif topic.startswith("senior."):
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
        await room.connect(tok["url"], tok["token"])
    except Exception as e:
        print(f"[error] livekit connect failed: {e!r}", file=sys.stderr)
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

    if persona_text:
        try:
            payload = json.dumps({"text": persona_text}, ensure_ascii=False).encode("utf-8")
            await room.local_participant.publish_data(payload, reliable=True, topic="senior.persona")
            _print_event(json_mode, "system", "persona auto-pushed", topic="_meta")
        except Exception as e:
            print(f"[error] persona auto-push failed: {e!r}", file=sys.stderr, flush=True)

    if not json_mode:
        print("[hint] type a line to senior.say (voice-ai speaks it). "
              "/q to quit (Ctrl-D no longer quits under --keep-alive).", flush=True)

    try:
        if read_stdin:
            stdin_task = asyncio.create_task(
                _stdin_publisher(room, json_mode, stop,
                                 keep_alive=keep_alive, say_tracker=say_tracker, echo=echo)
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
        hb_task.cancel()
        try:
            await hb_task
        except (asyncio.CancelledError, Exception):
            pass
        try:
            await room.disconnect()
        except Exception:
            pass
        _print_event(json_mode, "system", "disconnected", topic="_meta")

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
) -> int:
    try:
        api_base, slug = _parse_invite(invite_url)
    except ValueError as e:
        print(f"[error] {e}", file=sys.stderr)
        return 2

    if not identity:
        import socket
        host = re.sub(r"[^a-z0-9]", "", socket.gethostname().lower())[:12] or "host"
        name = (agent_name or "claude").lower()
        name = re.sub(r"[^a-z0-9]", "", name)[:16] or "claude"
        identity = f"{name}-{host}-{os.urandom(2).hex()}"
    ident = identity

    _print_event(json_mode, "system",
                 f"connecting room={slug} as identity={ident} via {api_base}", topic="_meta")

    # Shared state across reconnect cycles (#6): the notifier dedup, echo ring,
    # and say-seq counter persist so we don't re-wake on the same turn after a
    # transient reconnect.
    notifier = relay.TurnNotifier(wake_only_user=wake_only_user)
    echo = relay.EchoSuppressor(enabled=suppress_echo)
    say_tracker = relay.SayTracker(ttl=say_ttl)

    http_client = httpx.AsyncClient(headers={"user-agent": _USER_AGENT}) if notify_url else None

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
            )
            first = False
            # Clean quit (stdin /q or {"topic":"quit"}): reason is None.
            if reason is None:
                break
            # #6 — reconnect only on transient disconnects, and only when
            # keep-alive is on. Terminal reasons (host left / room closed /
            # client-initiated) end the session.
            if not keep_alive or relay.is_terminal_disconnect(reason):
                _print_event(json_mode, "system",
                             f"session ended (reason={reason}, keep_alive={keep_alive})",
                             topic="_meta")
                break
            delay = next(backoff)
            _print_event(json_mode, "system",
                         f"transient disconnect ({reason}) — reconnecting in {delay:.0f}s",
                         topic="_meta")
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                break
    finally:
        if http_client is not None:
            try:
                await http_client.aclose()
            except Exception:
                pass
    return rc


def main() -> None:
    ap = argparse.ArgumentParser(prog="voicehook-agent", description=__doc__)
    ap.add_argument("--version", action="version", version=f"voicehook-agent {_VERSION}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_join = sub.add_parser("join", help="join a voicehook.ai call as an agent")
    p_join.add_argument("invite_url", help="https://voicehook.ai/r/<slug>?go=1  OR  bare <slug>")
    p_join.add_argument("--name", default=None, help="agent brand-name shown in voice.html chip (e.g. 'claude', 'hermes', 'cursor', 'openclaw'). Becomes identity prefix. Default: 'claude'.")
    p_join.add_argument("--identity", default=None, help="explicit full identity (overrides --name)")
    p_join.add_argument("--json", action="store_true", help="JSONL stream mode on stdin/stdout")
    p_join.add_argument(
        "--persona", default=None,
        help="inline persona text to auto-push as senior.persona right after connect. Wins over --persona-file and --strict-relay.",
    )
    p_join.add_argument(
        "--persona-file", default=None,
        help="path to a UTF-8 file with persona text; auto-pushed as senior.persona after connect. Example: --persona-file personas/claude-default.txt",
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
        help="drop the agent's own relayed TTS (role=agent transcript matching a recent senior.say) from the operator stream. [#10]",
    )
    # #9 say TTL
    p_join.add_argument(
        "--say-ttl", type=float, default=None, metavar="SEC",
        help="drop a senior.say that is older than SEC seconds or superseded by a newer user-turn, instead of sending it stale. [#9]",
    )
    # #8 strict relay
    p_join.add_argument(
        "--strict-relay", action="store_true", default=False,
        help="inject a bundled strict-relay persona at connect: voicebot speaks ONLY pushed text, never self-generates. Overridden by --persona/--persona-file. [#8]",
    )
    args = ap.parse_args()
    if args.cmd == "join":
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
                say_ttl=args.say_ttl,
            ))
        except KeyboardInterrupt:
            rc = 130
        sys.exit(rc)


if __name__ == "__main__":
    main()
