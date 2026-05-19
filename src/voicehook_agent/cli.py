"""voicehook-agent CLI.

Usage:
    voicehook-agent join <invite-url>           # interactive mode
    voicehook-agent join <invite-url> --json    # JSONL stream mode

stdout: incoming user turns + voice-ai turns, one per line
        plain mode:  [role] text
        json mode:   {"role":"user","text":"..."}\n
stdin:  one line per turn → published as senior.say  (voice-ai speaks it via TTS)
        json mode:   {"text":"..."} or {"topic":"senior.persona","text":"..."}
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from urllib.parse import urlparse

import httpx
from livekit import rtc

_SLUG_RX = re.compile(r"^[a-z]+-[a-z]+-[a-z]+-[A-Z0-9]{4,8}$")
_USER_AGENT = "voicehook-agent/0.1.0"


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


def _on_data_factory(json_mode: bool):
    def _on_data(pkt: rtc.DataPacket) -> None:
        topic = (pkt.topic or "").strip()
        try:
            payload = json.loads(bytes(pkt.data).decode("utf-8"))
        except Exception:
            return
        if topic == "transcript":
            _print_event(json_mode, payload.get("role", "?"), payload.get("text", ""), topic=topic)
        elif topic.startswith("senior."):
            # Don't echo our own senior.say (we sent it); but DO show what
            # OTHER seniors push (e.g. another agent in the same room).
            sender = getattr(pkt.participant, "identity", "?") if pkt.participant else "?"
            _print_event(json_mode, "system", f"({topic} from {sender}) {payload.get('text','')}", topic=topic)
    return _on_data


async def _stdin_publisher(room: rtc.Room, json_mode: bool, stop: asyncio.Event) -> None:
    """Reads stdin lines, publishes them. Plain mode: each line → senior.say.
    JSON mode: each line is parsed; {"topic":"...","text":"..."} or just {"text":"..."}.
    """
    loop = asyncio.get_running_loop()
    while not stop.is_set():
        try:
            line = await loop.run_in_executor(None, sys.stdin.readline)
        except (EOFError, KeyboardInterrupt):
            break
        if line == "":  # EOF
            break
        line = line.rstrip("\n")
        if not line:
            continue
        if json_mode:
            try:
                obj = json.loads(line)
            except Exception as e:
                print(f"[error] invalid json on stdin: {e!r}", file=sys.stderr, flush=True)
                continue
            topic = obj.get("topic", "senior.say")
            payload = {k: v for k, v in obj.items() if k != "topic"}
        else:
            topic = "senior.say"
            payload = {"text": line}
        try:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            await room.local_participant.publish_data(data, reliable=True, topic=topic)
        except Exception as e:
            print(f"[error] publish failed: {e!r}", file=sys.stderr, flush=True)
    stop.set()


def _load_persona(persona: str | None, persona_file: str | None) -> str | None:
    """Resolve --persona / --persona-file into a single text blob.
    --persona wins if both are passed. Returns None if neither is set.
    File contents are read as UTF-8 and stripped of trailing whitespace."""
    if persona:
        return persona
    if persona_file:
        with open(persona_file, "r", encoding="utf-8") as f:
            return f.read().rstrip()
    return None


def _kind_label(p) -> str:
    """ParticipantKind enum → short string ('user' / 'agent' / 'sip' / ...).
    LK proto: 0=standard(user), 1=ingress, 2=egress, 3=sip, 4=agent."""
    try:
        k = int(getattr(p, "kind", 0))
    except Exception:
        return "user"
    return {0: "user", 1: "ingress", 2: "egress", 3: "sip", 4: "agent"}.get(k, f"kind{k}")


async def _join(
    invite_url: str,
    identity: str | None,
    agent_name: str | None,
    json_mode: bool,
    persona_text: str | None = None,
) -> int:
    try:
        api_base, slug = _parse_invite(invite_url)
    except ValueError as e:
        print(f"[error] {e}", file=sys.stderr)
        return 2
    # Default identity: `<name>-<hostname>-<rand>` so frontend presence-bar
    # shows WHICH agent-brand is connected (Claude / Hermes / OpenClaw / Cursor).
    # `--name` flag overrides the prefix. Default = "claude" since voicehook-
    # agent was first designed for Claude-Code users.
    if not identity:
        import socket
        host = re.sub(r"[^a-z0-9]", "", socket.gethostname().lower())[:12] or "host"
        name = (agent_name or "claude").lower()
        name = re.sub(r"[^a-z0-9]", "", name)[:16] or "claude"
        identity = f"{name}-{host}-{os.urandom(2).hex()}"
    ident = identity
    _print_event(json_mode, "system", f"connecting room={slug} as identity={ident} via {api_base}", topic="_meta")
    try:
        tok = await _mint_token(api_base, slug, ident)
    except httpx.HTTPError as e:
        print(f"[error] token mint failed: {e!r}", file=sys.stderr)
        return 3
    room = rtc.Room()
    room.on("data_received", _on_data_factory(json_mode))
    stop = asyncio.Event()

    # ---- Live peer-state tracking ------------------------------------------
    # Senior brain needs to see WHO is in the room and WHO is active. We track
    # (a) which identities have an audible audio track subscribed, and
    # (b) which identities are currently in the active-speakers set.
    audible: set[str] = set()       # identities w/ at least one subscribed audio track
    speakers: set[str] = set()      # identities currently emitting voice

    def _emit_meta(text: str, **extra) -> None:
        _print_event(json_mode, "system", text, topic="_meta", **extra)

    # --- Participant join/leave (the prompt expects these even though the
    # previous version only logged initial peers).
    def _on_participant_connected(p) -> None:
        _emit_meta(f"peer-joined: {p.identity} ({_kind_label(p)})")

    def _on_participant_disconnected(p) -> None:
        ident_ = p.identity
        audible.discard(ident_)
        speakers.discard(ident_)
        _emit_meta(f"peer-left: {ident_} ({_kind_label(p)})")

    room.on("participant_connected", _on_participant_connected)
    room.on("participant_disconnected", _on_participant_disconnected)

    # --- Active speakers
    def _on_active_speakers(spk_list) -> None:
        new_set = {s.identity for s in spk_list}
        speakers.clear()
        speakers.update(new_set)
        idents = sorted(new_set)
        _emit_meta(f"speaking: [{', '.join(idents)}]", speakers=idents)

    room.on("active_speakers_changed", _on_active_speakers)

    # --- Audio track subscribe / unsubscribe
    def _on_track_subscribed(track, publication, participant) -> None:
        if int(getattr(track, "kind", 0)) != 1:  # 1 = KIND_AUDIO
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

    # --- Mute / unmute (audio only)
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

    # --- Connection-state hiccups
    room.on("reconnecting", lambda *_: _emit_meta("reconnecting"))
    room.on("reconnected", lambda *_: _emit_meta("reconnected"))

    # --- Disconnect (with reason)
    def _on_disconnected(*args) -> None:
        reason = args[0] if args else None
        try:
            reason_name = rtc.DisconnectReason.Name(int(reason)) if reason is not None else "UNKNOWN"
        except Exception:
            reason_name = str(reason)
        _emit_meta(f"room-disconnected reason={reason_name}")
        stop.set()

    room.on("disconnected", _on_disconnected)

    try:
        await room.connect(tok["url"], tok["token"])
    except Exception as e:
        print(f"[error] livekit connect failed: {e!r}", file=sys.stderr)
        return 4
    _print_event(
        json_mode, "system",
        f"connected — {len(room.remote_participants)} peers: "
        f"{[p.identity for p in room.remote_participants.values()]}",
        topic="_meta",
    )
    # Pre-populate `audible` from already-subscribed tracks (covers the case
    # where peers + their audio publications existed BEFORE we joined and the
    # track_subscribed event already fired during connect).
    for p in room.remote_participants.values():
        for pub in p.track_publications.values():
            if int(getattr(pub, "kind", 0)) == 1 and getattr(pub, "subscribed", False):
                audible.add(p.identity)

    # --- 10s heartbeat with dedup -------------------------------------------
    async def _heartbeat() -> None:
        last_sig: tuple | None = None
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=10.0)
                return  # stop got set
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
            # Build a stable signature for dedup.
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

    # Auto-push Hotswap-Persona BEFORE handing over to the stdin loop.
    # This solves the recurring "every call starts from zero" pain: cold-LLM
    # agents (and humans) routinely forget the manual senior.persona step.
    # If --persona / --persona-file is supplied, we push it here so voice-ai
    # immediately wears the senior agent's skin instead of its default
    # "voicehook Voice-Assistent" persona.
    if persona_text:
        try:
            payload = json.dumps({"text": persona_text}, ensure_ascii=False).encode("utf-8")
            await room.local_participant.publish_data(payload, reliable=True, topic="senior.persona")
            _print_event(json_mode, "system", "persona auto-pushed", topic="_meta")
        except Exception as e:
            print(f"[error] persona auto-push failed: {e!r}", file=sys.stderr, flush=True)
    if not json_mode:
        print("[hint] type a line to senior.say (voice-ai speaks it). Ctrl-D to quit.", flush=True)
    try:
        await _stdin_publisher(room, json_mode, stop)
    finally:
        stop.set()
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
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(prog="voicehook-agent", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_join = sub.add_parser("join", help="join a voicehook.ai call as an agent")
    p_join.add_argument("invite_url", help="https://voicehook.ai/r/<slug>?go=1  OR  bare <slug>")
    p_join.add_argument("--name", default=None, help="agent brand-name shown in voice.html chip (e.g. 'claude', 'hermes', 'cursor', 'openclaw'). Becomes identity prefix. Default: 'claude'.")
    p_join.add_argument("--identity", default=None, help="explicit full identity (overrides --name)")
    p_join.add_argument("--json", action="store_true", help="JSONL stream mode on stdin/stdout")
    p_join.add_argument(
        "--persona",
        default=None,
        help="inline persona text to auto-push as senior.persona right after connect (no more zero-context starts). Mutually exclusive winner over --persona-file if both passed.",
    )
    p_join.add_argument(
        "--persona-file",
        default=None,
        help="path to a UTF-8 file containing persona text; auto-pushed as senior.persona right after connect. Example: --persona-file personas/claude-default.txt",
    )
    args = ap.parse_args()
    if args.cmd == "join":
        try:
            persona_text = _load_persona(args.persona, args.persona_file)
        except OSError as e:
            print(f"[error] could not read --persona-file: {e!r}", file=sys.stderr)
            sys.exit(2)
        try:
            rc = asyncio.run(_join(args.invite_url, args.identity, args.name, args.json, persona_text))
        except KeyboardInterrupt:
            rc = 130
        sys.exit(rc)


if __name__ == "__main__":
    main()
