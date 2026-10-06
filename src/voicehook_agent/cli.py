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
    status [TEXT] [--doing T] [--open T]... [--done T]... [--faq 'F::A']... [-f board.json]
                                     send operator.status {doing, open[], done[]};
                                     replaces the last board, never spoken; '' clears.
                                     0.10.0: --faq 'Frage::Antwort' (max 6) adds faq[{q, a}]:
                                     the user's likely next questions, answered in advance.
    next                             also yields {type:"status_request"} (the user asked
                                     what you are doing: send `status` at once) and adds
                                     status_stale / latency_warning hints.

Keep the board fresh (0.9.0): Delta answers the user from your board while you work.
    next                             adds status_due:true + status_reason + hint (the exact
                                     command) when the board is empty, a status_request is
                                     open (that event comes first in line), or the board is
                                     older than --status-due SEC (default 45, env
                                     VOICEHOOK_STATUS_DUE) while doing/open is set.
    say                              a progress line ("fertig", "live", "deploye") without a
                                     board push since the last say returns the same hint.

Say receipts (0.9.0, voicebot sends operator.say_status {seq, state, spoken_chars}):
    say                              returns {"ok":true,"seq":N}
    next                             adds "say_status": [{"seq":N,"state":"spoken"}, ...]
                                     (changes since the last next) and "say_hint" when a
                                     say sits in queued/requeued for more than 20 s.
    says                             last state of every own say (sent until the first
                                     receipt): {"type":"says","says":[{seq,state,...}]}
    join --username NAME             also sent to the server (vh.user): Delta knows the user.

Several operators (0.11.0): one shared say queue for all agents in the call.
    say [--mode append]              default: queue at the end, starts right after the
                                     running say (no gap); a running say is never cut
    say --mode overwrite             replace only YOUR not-started (queued) says at their
                                     place, else append; a requeued rest counts as started
    say --mode revise                overwrite + operator.revise to you only (replaced
                                     texts in unspoken, plus new); nothing held or cut
    say --urgent                     priority urgent: interrupt whoever speaks, go first
    join --voice NAME                own Chirp3-HD voice (e.g. Puck) -> vh.voice; without it
                                     the server picks one per identity, never Delta's
    revise / say_status              only for the say's owner (field owner); transcript
                                     lines carry speaker (display name) + op (identity)

Activity feed (0.10.0): Delta sees what you are doing, one line per tool call.
    hook install [--settings PATH]   add the Claude Code PostToolUse hook to settings.json
    hook print                       print the settings.json snippet
    hook post-tool-use               the hook itself (fast path: voicehook-agent-hook)
                                     join publishes the newest 15 lines as operator.activity
                                     {lines, ts}, on change, at most every 5 s.

Self-update (0.12.0): the server names cli_min / cli_latest in every join answer.
    join                             cli_latest newer than this CLI: update in place (uv tool
                                     upgrade, else pip --upgrade) and restart the join with the
                                     same arguments (once per join). Update fails: warn, go on.
                                     HTTP 426 (below cli_min): print the upgrade command, try
                                     one self-update, else exit 7.
    join --no-self-update            off (env VOICEHOOK_NO_SELF_UPDATE=1)
    self-update                      update now; --version shows "neue Version verfügbar"
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import signal
import sys
import threading
import time
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import urlparse

import httpx
from livekit import rtc

from . import __version__, relay
from . import activity as vactivity
from . import selfupdate as vselfupdate
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
    "operator.alive",        # 0.8.0: sign of life while the brain serves say/next
    "operator.activity",     # 0.10.0: newest tool-call lines of the coding agent
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

    def __init__(self, status: int, detail: str, body: dict | None = None):
        self.status = status
        self.detail = detail
        self.body = body  # parsed JSON (426: detail.cli_min/cli_latest/upgrade)
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
                      op_invite: str | None = None, username: str | None = None,
                      voice: str | None = None) -> dict:
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
    if username:  # 0.9.0: lands as participant attribute vh.user (Delta's core knows the user)
        params["username"] = username
    if voice:  # 0.11.0: wish voice for our says (Chirp3-HD, e.g. 'Puck') -> vh.voice
        params["voice"] = voice
    # 0.12.0: X-VH-CLI = the server's version gate (426 below cli_min)
    async with httpx.AsyncClient(timeout=10.0, headers={"user-agent": _USER_AGENT,
                                                        "x-vh-cli": _VERSION}) as cli:
        r = await cli.get(url, params=params)
        if r.status_code >= 400:
            try:
                detail = (r.text or "").strip()[:200]
            except Exception:
                detail = ""
            if op_invite:
                detail = detail.replace(op_invite, "***")
            try:
                body = r.json() if r.status_code == 426 else None
            except ValueError:
                body = None
            raise TokenMintError(r.status_code, detail, body if isinstance(body, dict) else None)
        return r.json()


SELF_REPORT_ERROR = (
    "[error] Selbstauskunft fehlt: `join` braucht --name UND --model.\n"
    "  --name   dein eigener Anzeigename im Call (dein echter Modell-/Herstellername)\n"
    "  --model  das exakte Modell, auf dem du gerade laeufst (z.B. 'opus-5.5', 'deepseek-v4-pro')\n"
    "Beide erscheinen im Agent-Chip der Web-UI als 'Name · Modell'. Nenn deinen echten\n"
    "Modell-/Herstellernamen, nie 'Claude', wenn du nicht Claude bist.\n"
    "Beispiel:\n"
    "  voicehook-agent join https://voicehook.ai/r/<slug> --name <dein-eigener-Name> --model <dein-Modellname> --json"
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
OWNER_GONE_SAY = (
    "Ich verlasse den Call jetzt, weil die Sitzung meines Agenten beendet ist. "
    "Lade mich gern wieder ein."
)

# 0.8.0 sign of life: `operator.alive` every ALIVE_INTERVAL s while the brain
# served say/next/stdin within the last ALIVE_WINDOW s (a blocked `next` counts
# as serving). Nothing is sent while orphaned, so the web UI can dim the chip.
ALIVE_INTERVAL = 10.0
ALIVE_WINDOW = 15.0
OWNER_POLL = 2.0
# 0.10.1: a join with no human in the room leaves after this many seconds
# (--no-human-timeout MIN). Separate from --idle-timeout (the brain is silent).
# 150 s: just above the server grace (VH_IDLE_NO_HUMAN_SECONDS 120 s, Oliver 05.10.),
# so the server ends the call first and the CLI never leaves a call the server keeps.
NO_HUMAN_TIMEOUT = 150.0
CALL_ENDED_MSG = "call ended by the server, leaving (no reconnect)"

# 0.10.2: the server refuses an operator join while no human is in the room (HTTP 409,
# voicehook-v4 #152). That heals as soon as the human (re)joins: wait and retry every
# NO_HUMAN_RETRY_S, at most NO_HUMAN_WAIT_MAX_S, then give up. 410 stays terminal.
NO_HUMAN_RETRY_S = 5.0
NO_HUMAN_WAIT_MAX_S = 120.0
NO_HUMAN_WAIT_MSG = ("no human in the room yet (HTTP 409): waiting for the person to join, "
                     "retrying every {every:.0f}s for up to {max:.0f}s")
NO_HUMAN_GIVE_UP_MSG = ("[error] still no human in the room after {max:.0f}s (HTTP 409), not joining. "
                        "Open the call in the browser first, then run join again.")


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
                 echo: relay.EchoSuppressor, idle_timeout: float,
                 no_human_timeout: float = NO_HUMAN_TIMEOUT) -> None:
        self.slug = slug
        self.ident = ident
        self.say_tracker = say_tracker
        self.echo = echo
        self.events = vsession.EventQueue()
        self.watchdog = vsession.IdleWatchdog(timeout=idle_timeout)
        self.quit = asyncio.Event()
        self.room: rtc.Room | None = None
        self.room_ready = asyncio.Event()
        self.alive_interval = ALIVE_INTERVAL
        self.alive_window = ALIVE_WINDOW
        # 0.10.0 activity feed: poll activity.log, publish at most once per window
        self.activity_interval = vactivity.POLL_INTERVAL
        self.activity_window = vactivity.PUBLISH_WINDOW
        self.activity_lines = vactivity.PUBLISH_LINES
        self.activity_clock = time.monotonic
        self.leaving = False
        # 0.10.1: no-human guard. Starts at join (nobody seen yet), stops while a
        # human is in the room, restarts when the last one leaves. Spans reconnects.
        self.no_human_timeout = no_human_timeout
        self.no_human_since: float | None = time.monotonic()
        self.call_ended: str | None = None  # reason of a server-side call end
        # 0.7.0: latency_warning + status_stale; 0.9.0: status_due + hint for `next`
        self.clock = relay.TurnClock(due_s=relay.status_due_seconds())
        self.agent_said = relay.AgentSaid()  # 0.7.0: Delta's own lines for `next`
        self.say_status = relay.SayStatus()  # 0.9.0: operator.say_status per own say

    def attach(self, room: rtc.Room) -> None:
        self.room = room
        self.room_ready.set()

    def detach(self) -> None:
        self.room = None
        self.room_ready.clear()

    def humans_changed(self, room) -> None:
        """Recount humans in `room` (see relay.is_human_peer), arm/stop the guard."""
        n = sum(1 for p in room.remote_participants.values()
                if relay.is_human_peer(_kind_label(p), dict(getattr(p, "attributes", None) or {})))
        if n:
            self.no_human_since = None
        elif self.no_human_since is None:
            self.no_human_since = time.monotonic()

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


# 0.11.0 (multi-operator): append is the default; a running say is never cut (only own
# operator.interrupt / --urgent). overwrite/revise replace only our OWN not-started says.
_SAY_MODES = ("append", "overwrite", "revise")
SAY_DEFAULT_MODE = "append"


async def _control_handler(ctl: _Control, req: dict) -> dict:
    """One request from `voicehook-agent say|next|leave|status`."""
    cmd = req.get("cmd")
    if cmd == "say":
        ctl.watchdog.touch()
        ctl.events.arm()  # from now on user turns are queued for `next`
        text = str(req.get("text") or "").strip()
        if not text:
            return {"ok": False, "error": "empty text"}
        # mode is always sent explicitly (older servers defaulted to revise)
        mode = req.get("mode") or SAY_DEFAULT_MODE
        if mode not in _SAY_MODES:
            return {"ok": False, "error": f"mode must be one of {_SAY_MODES}"}
        extra: dict = {"mode": mode}
        if req.get("urgent"):
            extra["priority"] = "urgent"
        nudge = ctl.clock.said(text=text)  # 0.9.0: progress said, board older -> hint
        room = await ctl.wait_room(10.0)
        if room is None:
            return {"ok": False, "error": "not connected to the room (yet)"}
        res = await _publish_say(room, text, extra, ctl.say_tracker, ctl.echo)
        if res.get("ok"):
            ctl.say_status.sent(res["seq"], text)
        return {**res, **nudge} if res.get("ok") else res
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
        ctl.clock.board(board=board)
        return {"ok": True, "type": "board", "board": board}
    if cmd == "next":
        try:
            timeout = float(req.get("timeout", 60))
        except (TypeError, ValueError):
            return {"ok": False, "error": "timeout must be a number"}
        # A blocked `next` counts as "brain alive". Cap it at the idle timeout so
        # an orphaned `next --timeout 86400` cannot keep a ghost join alive.
        if ctl.watchdog.timeout > 0:
            timeout = min(timeout, ctl.watchdog.timeout)
        ctl.watchdog.enter()
        try:
            ev = await ctl.events.get(timeout)
        finally:
            ctl.watchdog.leave()
        hints = ctl.clock.hints()
        if ev is None or ev.get("type") != "ended":
            hints.update(ctl.agent_said.take())
            hints.update(ctl.say_status.take())
        if ev is None:
            return {"ok": True, "type": "timeout", "pending": 0, **hints}
        if ev.get("type") == "user":
            ctl.clock.delivered()
        return {"ok": True, **ev, "pending": len(ctl.events), **hints}
    if cmd == "leave":
        ctl.watchdog.touch()
        text = str(req.get("say") or "").strip()
        ctl.leaving = True
        await _publish_alive(ctl, False)
        if text and ctl.room is not None:
            try:
                await _publish_say(ctl.room, text, {"mode": "append"},
                                   ctl.say_tracker, ctl.echo)
            except Exception as e:  # noqa: BLE001
                print(f"[error] leave say failed: {e!r}", file=sys.stderr, flush=True)
        ctl.quit.set()
        return {"ok": True, "type": "leaving"}
    if cmd == "says":
        return {"ok": True, "type": "says", "says": ctl.say_status.table(),
                **({"say_hint": h} if (h := ctl.say_status.stuck()) else {})}
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


async def _publish_alive(ctl: _Control, alive: bool) -> bool:
    """One `operator.alive` packet; best effort (never raises)."""
    room = ctl.room
    if room is None:
        return False
    payload = {"alive": alive, "ts": time.time(),
               "idle_s": round(ctl.watchdog.idle_for(), 1)}
    try:
        await room.local_participant.publish_data(
            json.dumps(payload).encode("utf-8"), reliable=True, topic="operator.alive")
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[warn] operator.alive publish failed: {e!r}", file=sys.stderr, flush=True)
        return False


async def _alive_loop(ctl: _Control) -> None:
    """Sign of life: send `operator.alive` every `alive_interval` s while the
    brain served say/next within `alive_window` s, and at once when it comes
    back. Silent while orphaned (the UI dims the chip after ~20 s)."""
    tick = max(0.05, min(1.0, ctl.alive_interval / 4))
    last_sent = 0.0
    was_active = False
    while not ctl.quit.is_set():
        active = ctl.room is not None and ctl.watchdog.idle_for() < ctl.alive_window
        now = time.monotonic()
        due = not was_active or now - last_sent >= ctl.alive_interval
        if active and not ctl.leaving and due and await _publish_alive(ctl, True):
            last_sent = now
        was_active = active
        try:
            await asyncio.wait_for(ctl.quit.wait(), timeout=tick)
            return
        except asyncio.TimeoutError:
            pass


async def _publish_activity(ctl: _Control, lines: list[str]) -> bool:
    """One `operator.activity` packet {lines, ts}; best effort (never raises)."""
    room = ctl.room
    if room is None:
        return False
    payload = {"lines": lines, "ts": time.time()}
    try:
        await room.local_participant.publish_data(
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            reliable=True, topic=vactivity.TOPIC)
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[warn] operator.activity publish failed: {e!r}", file=sys.stderr, flush=True)
        return False


async def _activity_loop(ctl: _Control, path: Path) -> None:
    """0.10.0: publish the newest `activity_lines` lines of activity.log (written by
    the Claude Code PostToolUse hook) as `operator.activity`. Only on change, at
    most once per `activity_window` s; a change inside the window goes out when
    it ends (last one wins). Never crashes the join."""
    pub = vactivity.ActivityPublisher(ctl.activity_window)
    while not ctl.quit.is_set():
        try:
            pub.window = ctl.activity_window
            lines = vactivity.read_tail(path, ctl.activity_lines)
            now = ctl.activity_clock()
            if ctl.room is not None and not ctl.leaving and pub.due(lines, now):
                if await _publish_activity(ctl, lines):
                    pub.sent(lines, now)
        except Exception as e:  # noqa: BLE001
            print(f"[warn] activity loop: {e!r}", file=sys.stderr, flush=True)
        try:
            await asyncio.wait_for(ctl.quit.wait(), timeout=max(0.01, ctl.activity_interval))
            return
        except asyncio.TimeoutError:
            pass


async def _leave_with(ctl: _Control, json_mode: bool, meta: str,
                      announce: str | None) -> None:
    """Shared exit path of the orphan guards: tell the room, then quit."""
    ctl.leaving = True
    _print_event(json_mode, "system", meta, topic="_meta")
    await _publish_alive(ctl, False)
    if announce and ctl.room is not None:
        try:
            await _publish_say(ctl.room, announce, {"mode": "append"},
                               ctl.say_tracker, ctl.echo)
            await asyncio.sleep(1.0)
        except Exception as e:  # noqa: BLE001
            print(f"[error] leave announce failed: {e!r}", file=sys.stderr, flush=True)
    ctl.quit.set()


async def _idle_loop(ctl: _Control, json_mode: bool, announce: str | None) -> None:
    """Orphan guard: leave the call when the brain sent no say/next/stdin line
    for `watchdog.timeout` seconds. The watchdog lives on `_Control`, so it
    spans reconnect cycles: a reconnect is no sign of life and never resets it."""
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
        await _leave_with(ctl, json_mode,
                          f"idle-timeout: no say/next from the agent for "
                          f"{ctl.watchdog.timeout:.0f}s, leaving", announce)
        return


async def _no_human_loop(ctl: _Control, json_mode: bool) -> None:
    """0.10.1 ghost guard: leave when no human was in the room for
    `ctl.no_human_timeout` seconds (Vorfall 04.10.: Claude sat alone in the room
    after Delta had ended the call). A reconnect is no human: never resets."""
    if ctl.no_human_timeout <= 0:
        return
    tick = min(5.0, max(0.05, ctl.no_human_timeout / 4))
    while not ctl.quit.is_set():
        try:
            await asyncio.wait_for(ctl.quit.wait(), timeout=tick)
            return
        except asyncio.TimeoutError:
            pass
        since = ctl.no_human_since
        if since is None or time.monotonic() - since < ctl.no_human_timeout:
            continue
        await _leave_with(ctl, json_mode,
                          f"no-human-timeout: no human in the room for "
                          f"{ctl.no_human_timeout:.0f}s, leaving (no reconnect)", None)
        return


async def _end_call(ctl: _Control, json_mode: bool, reason: str) -> None:
    """Server ended the call (topic `call_end`): tell the brain, leave, no reconnect."""
    if ctl.call_ended is not None:
        return
    ctl.call_ended = reason or "unknown"
    ctl.events.put_front_nowait({"type": "call_end", "reason": ctl.call_ended})
    _print_event(json_mode, "system", f"call-ended reason={ctl.call_ended}",
                 topic="call_end", reason=ctl.call_ended)
    await _leave_with(ctl, json_mode, CALL_ENDED_MSG, None)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _owner_pids(explicit: list[int] | None) -> list[int]:
    """Processes whose end ends the join: --owner-pid / VOICEHOOK_OWNER_PID
    (the calling agent session) and the FIFO holder from the skill quickstart
    ($VOICEHOOK_AGENT_HOME/holder)."""
    pids: list[int] = [p for p in (explicit or []) if p and p > 1]
    env = os.environ.get("VOICEHOOK_OWNER_PID", "").strip()
    if env.isdigit() and int(env) > 1:
        pids.append(int(env))
    home = os.environ.get("VOICEHOOK_AGENT_HOME")
    if home:
        try:
            raw = (Path(home) / "holder").read_text().strip()
            if raw.isdigit() and int(raw) > 1:
                pids.append(int(raw))
        except OSError:
            pass
    return sorted(set(pids))


async def _owner_loop(ctl: _Control, pids: list[int], json_mode: bool,
                      announce: str | None, poll: float | None = None) -> None:
    """Orphan guard 2: leave as soon as the calling session (owner pid) or the
    FIFO holder is gone, instead of waiting for the idle timeout."""
    if not pids:
        return
    poll = OWNER_POLL if poll is None else poll
    while not ctl.quit.is_set():
        gone = [p for p in pids if not _pid_alive(p)]
        if gone:
            await _leave_with(ctl, json_mode,
                              f"owner-gone: process {gone[0]} ended, leaving", announce)
            return
        try:
            await asyncio.wait_for(ctl.quit.wait(), timeout=poll)
            return
        except asyncio.TimeoutError:
            pass


def _daemon_readline(loop: asyncio.AbstractEventLoop, stream) -> asyncio.Future:
    """`stream.readline()` in a DAEMON thread. The default executor's worker
    threads are joined at interpreter exit, so a readline blocked on a FIFO
    whose writer (the skill's `sleep 86400` holder) stays open kept every ended
    join process alive for up to 24 h. A daemon thread dies with the process."""
    fut: asyncio.Future = loop.create_future()

    def _run() -> None:
        try:
            res, exc = stream.readline(), None
        except BaseException as e:  # noqa: BLE001
            res, exc = None, e

        def _done() -> None:
            if fut.done():
                return
            if exc is not None:
                fut.set_exception(exc)
            else:
                fut.set_result(res)
        try:
            loop.call_soon_threadsafe(_done)
        except RuntimeError:
            pass  # loop already closed

    threading.Thread(target=_run, name="vh-stdin", daemon=True).start()
    return fut


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
            chunk = await _daemon_readline(loop, sys.stdin)
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


RC_RESTART = 75  # internal: _join left the call for a self-update, main() re-execs


class _Updater:
    """0.12.0 self-update state of one join (see selfupdate.py). `restart` + `identity`
    tell main() to re-exec after _join has cleaned up."""

    def __init__(self, opt_out: bool = False, *, env=None, runner=None) -> None:
        self.env = os.environ if env is None else env
        self.opt_out = vselfupdate.opted_out(opt_out, self.env)
        self.restarted = vselfupdate.already_restarted(self.env)
        self.runner = runner
        self.tried = False
        self.restart = False
        self.identity: str | None = None

    async def _upgrade(self) -> bool:
        self.tried = True
        cmd = vselfupdate.upgrade_command()
        kw = {"runner": self.runner} if self.runner is not None else {}
        return await asyncio.to_thread(vselfupdate.run_upgrade, cmd, **kw)

    async def on_versions(self, info: dict, emit, *, check: bool) -> bool:
        """After a successful join answer. True = updated, leave and restart now."""
        vselfupdate.remember(info)
        latest = info.get("cli_latest")
        if not check:
            return False
        why = vselfupdate.decide(latest, _VERSION, opt_out=self.opt_out,
                                 restarted=self.restarted or self.tried)
        if why == "current":
            return False
        if why == "opt-out":
            msg = (f"voicehook-agent {latest} available (this is {_VERSION}, self-update off): "
                   "voicehook-agent self-update")
            emit(msg)
            print(f"[info] {msg}", file=sys.stderr, flush=True)
            return False
        if why == "restarted":
            msg = (f"self-update ran, but this is still {_VERSION} (latest {latest}); "
                   f"{vselfupdate.manual_hint()}")
            emit(msg)
            print(f"[warn] {msg}", file=sys.stderr, flush=True)
            return False
        emit(f"self-update: {_VERSION} -> {latest}, restarting the join afterwards")
        print(f"[info] self-update: voicehook-agent {_VERSION} -> {latest} ...",
              file=sys.stderr, flush=True)
        if await self._upgrade():
            self.restart = True
            return True
        emit(f"self-update failed, continuing with {_VERSION}")
        print(f"[warn] continuing with voicehook-agent {_VERSION}", file=sys.stderr, flush=True)
        return False

    async def on_outdated(self, body: dict | None) -> bool:
        """HTTP 426 (below cli_min). Prints the upgrade command, tries one self-update.
        True = updated, restart now; False = exit 7."""
        d = (body or {}).get("detail") if isinstance(body, dict) else None
        d = d if isinstance(d, dict) else {}
        vselfupdate.remember(d)
        lo, hi = d.get("cli_min", "?"), d.get("cli_latest", "?")
        print(f"[error] voicehook-agent {_VERSION} is too old for this server (min {lo}, "
              f"latest {hi}).\n        Upgrade: {d.get('upgrade') or vselfupdate.UPGRADE_UV}\n"
              f"        {vselfupdate.manual_hint()}", file=sys.stderr, flush=True)
        if self.opt_out or self.restarted or self.tried:
            return False
        print("[info] trying a self-update ...", file=sys.stderr, flush=True)
        if await self._upgrade():
            self.restart = True
            return True
        return False


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
    username: str | None = None,
    voice: str | None = None,
    updater: _Updater | None = None,
    update_check: bool = False,
) -> tuple[int, str | None]:
    """One connect→listen cycle. Returns (rc, disconnect_reason_name).
    disconnect_reason_name is None for a clean stdin-driven quit; otherwise the
    LK reason that ended the cycle (used by the reconnect loop in #6)."""
    tok: dict | None = None
    if transport == "bridge":
        # Server joins for us over HTTPS (same token/attributes as /api/token?invite=1).
        room = vtransport.BridgeRoom(api_base, slug, ident, name=agent_name or "agent",
                                     model=model or "unbekannt", invite=invite,
                                     user_agent=_USER_AGENT, username=username, voice=voice)
    else:
        try:
            tok = await _mint_token(api_base, slug, ident, name=agent_name, model=model,
                                    op_invite=invite, username=username, voice=voice)
        except TokenMintError as e:
            if e.status == 426:  # 0.12.0: CLI below the server's cli_min
                if updater is not None and await updater.on_outdated(e.body):
                    return 0, "SELF_UPDATE"
                return vselfupdate.EXIT_OUTDATED, "CLI_OUTDATED"
            if e.status == 410:  # 0.10.1: call ended / room gone -> never rejoin
                print(f"[error] call ended ({e}), not joining", file=sys.stderr, flush=True)
                return 5, "CALL_ENDED"
            if e.status == 409:  # 0.10.2: no human in the room yet -> the caller waits + retries
                return 6, "NO_HUMAN_YET"
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
        if updater is not None and await updater.on_versions(
                tok, lambda m: _print_event(json_mode, "system", m, topic="_meta"),
                check=update_check):
            return 0, "SELF_UPDATE"  # never connected: nothing to leave
        room = rtc.Room()
    stop = asyncio.Event()
    turn_event = asyncio.Event()  # set on finalized user-turn → graph re-sync
    disconnect_reason: dict[str, str | None] = {"name": None}

    audible: set[str] = set()
    speakers: set[str] = set()

    def _emit_meta(text: str, **extra) -> None:
        _print_event(json_mode, "system", text, topic="_meta", **extra)

    def _own_ids() -> set[str]:
        lp = getattr(room, "local_participant", None)
        return {i for i in (ident, getattr(lp, "identity", None),
                            getattr(room, "identity", None)) if i}

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
            # #10 — drop our own relayed TTS echo from the operator stream; a line of
            # another operator (payload.op = their identity) is never our echo.
            if not relay.foreign_owner(payload, _own_ids(), key="op") and echo.should_suppress(role, text):
                return
            # 0.11.0: v4 stamps speaker (display name) + op (operator identity) on
            # transcript lines; old servers send neither -> output unchanged.
            who = {k: payload[k] for k in ("speaker", "op") if payload.get(k)}
            _print_event(json_mode, role, text, topic=topic, **who)
            # `voicehook-agent next` — queue finalized user turns.
            if ctl is not None and role == "agent" and relay.TurnNotifier._is_final(payload):
                ctl.agent_said.add(text)  # Delta's own answer (not an echo of our say)
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
        elif topic == "call_end":
            # 0.10.1: the worker ended the call (idle_no_human, max_duration, ...).
            reason = str(payload.get("reason") or "unknown")
            if ctl is not None:
                asyncio.ensure_future(_end_call(ctl, json_mode, reason))
            else:
                _emit_meta(f"call-ended reason={reason}")
                disconnect_reason["name"] = "CALL_ENDED"
                stop.set()
        elif topic.startswith("operator."):
            if topic in ("operator.revise", "operator.say_status") and relay.foreign_owner(
                    payload, _own_ids()):
                return  # 0.11.0: belongs to another operator (server routes per owner)
            if topic == "operator.revise" and ctl is not None:
                ctl.events.put_nowait(relay.revise_event(payload))
            elif topic == "operator.say_status":
                if ctl is not None:
                    ctl.say_status.update(payload)
            elif topic == "operator.status_request" and ctl is not None:
                # 0.9.0: first in line for `next`, status_due until the next board
                ctl.events.put_front_nowait(relay.status_request_event(payload))
                ctl.clock.requested()
            sender = getattr(pkt.participant, "identity", "?") if pkt.participant else "?"
            line = payload.get("text", "")
            if topic == "operator.say_status":
                line = f"seq={payload.get('seq')} state={payload.get('state')}"
            _print_event(json_mode, "system", f"({topic} from {sender}) {line}", topic=topic)

    room.on("data_received", _on_data)

    def _on_participant_connected(p) -> None:
        _emit_meta(f"peer-joined: {p.identity} ({_kind_label(p)})")
        if ctl is not None:
            ctl.humans_changed(room)

    def _on_participant_disconnected(p) -> None:
        ident_ = p.identity
        audible.discard(ident_)
        speakers.discard(ident_)
        _emit_meta(f"peer-left: {ident_} ({_kind_label(p)})")
        if ctl is not None:
            ctl.humans_changed(room)

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
        if isinstance(e, vtransport.BridgeError) and e.status == 426:  # 0.12.0
            if updater is not None and await updater.on_outdated(e.body):
                return 0, "SELF_UPDATE"
            return vselfupdate.EXIT_OUTDATED, "CLI_OUTDATED"
        if isinstance(e, vtransport.BridgeError) and e.status == 410:
            print("[error] call ended (bridge HTTP 410), not joining", file=sys.stderr, flush=True)
            return 5, "CALL_ENDED"
        if isinstance(e, vtransport.BridgeError) and e.status == 409:
            return 6, "NO_HUMAN_YET"
        what = "bridge" if transport == "bridge" else "livekit"
        print(f"[error] {what} connect failed: {e!r}", file=sys.stderr)
        try:
            await room.disconnect()
        except Exception:  # noqa: BLE001
            pass
        return 4, "CONNECT_FAILED"

    if tok is None and updater is not None and await updater.on_versions(
            {"cli_min": getattr(room, "cli_min", None),
             "cli_latest": getattr(room, "cli_latest", None)},
            lambda m: _print_event(json_mode, "system", m, topic="_meta"), check=update_check):
        try:
            await room.disconnect()  # bridge: leave silently (no goodbye), the restart rejoins
        except Exception:  # noqa: BLE001, S110  (best effort, the server's guard ends it too)
            pass
        return 0, "SELF_UPDATE"

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
        ctl.humans_changed(room)

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
    no_human_timeout: float = NO_HUMAN_TIMEOUT,
    idle_say: str | None = DEFAULT_IDLE_SAY,
    owner_pids: list[int] | None = None,
    owner_say: str | None = OWNER_GONE_SAY,
    force_persona: bool = False,
    control: bool = True,
    transport: str = "auto",
    voice: str | None = None,
    status_due: float | None = None,
    updater: _Updater | None = None,
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
    if updater is None:
        updater = _Updater()
    updater.identity = ident

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
    ctl = _Control(slug, ident, say_tracker, echo, idle_timeout, no_human_timeout)
    if status_due is not None:
        ctl.clock.due_s = status_due
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
    vactivity.clear(sess_dir)  # 0.10.0: no activity lines of a previous call leak in
    loop = asyncio.get_running_loop()
    for sig in _quit_signals():
        try:
            loop.add_signal_handler(sig, ctl.quit.set)
        except (NotImplementedError, RuntimeError, ValueError):
            pass
    idle_task = asyncio.create_task(_idle_loop(ctl, json_mode, idle_say))
    watched = _owner_pids(owner_pids)
    if watched:
        _print_event(json_mode, "system", f"owner guard: leaving when pid(s) {watched} end",
                     topic="_meta")
    guard_tasks = [asyncio.create_task(_owner_loop(ctl, watched, json_mode, owner_say)),
                   asyncio.create_task(_alive_loop(ctl)),
                   asyncio.create_task(_no_human_loop(ctl, json_mode)),
                   asyncio.create_task(_activity_loop(ctl, sess_dir / vactivity.ACTIVITY_NAME))]

    rc = 0
    try:
        backoff = relay.backoff_delays(base=1.0, factor=2.0, cap=30.0)
        first = True
        no_human_waited: float | None = None  # seconds spent waiting on 409 (None = not waiting)
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
                transport=cur_transport, invite=invite, username=username,
                voice=voice, updater=updater, update_check=first,
            )
            if reason == "SELF_UPDATE":  # 0.12.0: updated, main() restarts the join
                rc = RC_RESTART
                break
            if reason == "CLI_OUTDATED":  # 0.12.0: 426 and no self-update -> exit 7
                break
            if first and vtransport.should_fallback(transport, cur_transport, reason):
                cur_transport = "bridge"
                msg = ("webrtc connect failed or timed out; retrying once via the HTTPS "
                       "bridge (transport=bridge)")
                _print_event(json_mode, "system", msg, topic="_meta")
                print(f"[warn] {msg}", file=sys.stderr, flush=True)
                continue
            if reason == "NO_HUMAN_YET":
                if no_human_waited is None:
                    no_human_waited = 0.0
                    msg = NO_HUMAN_WAIT_MSG.format(every=NO_HUMAN_RETRY_S, max=NO_HUMAN_WAIT_MAX_S)
                    _print_event(json_mode, "system", msg, topic="_meta")
                    print(f"[info] {msg}", file=sys.stderr, flush=True)
                if no_human_waited >= NO_HUMAN_WAIT_MAX_S:
                    print(NO_HUMAN_GIVE_UP_MSG.format(max=NO_HUMAN_WAIT_MAX_S), file=sys.stderr, flush=True)
                    break  # rc 6
                try:
                    await asyncio.wait_for(ctl.quit.wait(), timeout=NO_HUMAN_RETRY_S)
                    break  # leave / SIGTERM while waiting
                except asyncio.TimeoutError:
                    pass
                except asyncio.CancelledError:
                    break
                no_human_waited += NO_HUMAN_RETRY_S
                continue  # nothing joined yet: `first` (stdin, greeting) stays as it was
            no_human_waited = None
            if reason == "CALL_ENDED" and not first:
                rc = 0  # the call we were in ended: a clean end, not a join failure
            first = False
            # Clean quit (stdin /q or {"topic":"quit"}): reason is None.
            if reason is None:
                break
            # #6 — reconnect only on transient disconnects, and only when
            # keep-alive is on. Terminal reasons (host left / room closed /
            # client-initiated) end the session.
            # A 403 on the token mint (invite missing/invalid) will not heal by retrying.
            # 0.10.1: CALL_ENDED (call_end topic, 410 on rejoin) is terminal too.
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
        if rc == RC_RESTART:  # a waiting `next` learns why, instead of "ended"
            ctl.events.put_front_nowait({
                "ok": True, "type": "restarting", "reason": "self_update",
                "message": "voicehook-agent updated itself and rejoins the call: run next again"})
        for t in (idle_task, *guard_tasks):
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        await ctl.events.close()  # a blocked `next` returns {"type":"ended"}
        if server is not None:
            await asyncio.sleep(0.05)
            await server.close()
            vsession.remove_info(sess_dir)
            vactivity.clear(sess_dir)
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
        req = {"cmd": "say", "text": text.strip(), "mode": args.mode or SAY_DEFAULT_MODE}
        if getattr(args, "urgent", False):
            req["urgent"] = True
        return req
    if args.cmd == "next":
        return {"cmd": "next", "timeout": args.timeout}
    if args.cmd == "says":
        return {"cmd": "says"}
    if args.cmd == "leave":
        return {"cmd": "leave", "say": args.say} if args.say else {"cmd": "leave"}
    faq_raw = getattr(args, "faq", None) or []
    if (args.text is not None or args.doing is not None or args.open or args.done or args.file
            or faq_raw):
        file_obj = None
        if args.file:
            with open(args.file, encoding="utf-8") as fh:
                file_obj = json.load(fh)
        faq = relay.parse_faq(faq_raw, warn=lambda m: print(m, file=sys.stderr, flush=True))
        return {"cmd": "board", "board": relay.build_board(
            args.text, args.doing, args.open, args.done, file_obj, faq=faq)}
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


def _run_self_update(dry_run: bool = False, runner=None) -> int:
    """`voicehook-agent self-update`: run the upgrade for this install now."""
    cmd = vselfupdate.upgrade_command()
    print(f"voicehook-agent {_VERSION}, install via {vselfupdate.install_method()}: "
          f"{' '.join(cmd) if cmd else '(no updater found)'}", flush=True)
    if dry_run:
        return 0
    kw = {"runner": runner} if runner is not None else {}
    if not vselfupdate.run_upgrade(cmd, **kw):
        return 1
    print("updated; the next `voicehook-agent --version` shows the new version", flush=True)
    return 0


def _restart_after_update(argv: list[str] | None, updater: _Updater, execve=None) -> None:
    """Re-exec the join with the same arguments (+ same identity) on the updated CLI."""
    full = sys.argv if argv is None else ["voicehook-agent", *argv]
    print("[info] self-update done, restarting the join ...", file=sys.stderr, flush=True)
    try:
        kw = {"execve": execve} if execve is not None else {}
        vselfupdate.restart(full, _VERSION, identity=updater.identity, **kw)
    except OSError as e:
        print(f"[error] restart after self-update failed ({e!r}); run the join again",
              file=sys.stderr, flush=True)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="voicehook-agent", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=vselfupdate.version_text(_VERSION))
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_join = sub.add_parser("join", help="join a voicehook.ai call as an agent")
    p_join.add_argument("invite_url", help="full invite link https://voicehook.ai/r/<slug>?invite=<code>  OR  bare <slug>")
    p_join.add_argument("--name", default=None, help="REQUIRED. Your OWN display name in the call (<dein-eigener-Name>). Shown in the web Agent chip as 'Name · model', becomes the identity prefix and the name spoken in the auto-greet. Nenn deinen echten Modell-/Herstellernamen, nie 'Claude', wenn du nicht Claude bist.")
    p_join.add_argument("--identity", default=None, help="explicit full identity (overrides --name)")
    p_join.add_argument("--model", default=None, help="REQUIRED. Self-report: the exact model you run on (e.g. 'opus-5.5', 'deepseek-v4-pro'). Shown in the web Agent chip.")
    p_join.add_argument("--topic", default=None, help="self-report: what the call is about (<=5 words), spoken as 'wir waren gerade dabei {topic}'.")
    p_join.add_argument("--voice", default=None, help="optional wish voice for your says (Google Chirp3-HD, e.g. 'Puck' or 'de-DE-Chirp3-HD-Puck'); sent to the server (token `voice=` / bridge join `voice`) as attribute vh.voice. Without it the server picks a fixed voice per identity, never Delta's. Normal mode only.")
    p_join.add_argument("--username", default=None, help="the user's first name: spoken in the salutation ('Hallo {username},') and, since 0.9.0, sent to the server (token `username=` / bridge join `username`) so the voicebot knows whom it talks to (participant attribute vh.user). Omitted if unknown.")
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
        "--no-human-timeout", type=float, default=NO_HUMAN_TIMEOUT / 60.0, metavar="MIN",
        help="leave the call when no human has been in the room for MIN minutes (counted from the join, reset only by a human, not by a reconnect). Unlike --idle-timeout (your brain went silent) this ends ghost joins in empty rooms. 0 = off. Default 2.5 (just above the server grace of 120 s).",
    )
    p_join.add_argument(
        "--idle-say", default=DEFAULT_IDLE_SAY,
        help="what voice-ai says before an idle-timeout leave ('' = leave silently).",
    )
    p_join.add_argument(
        "--owner-pid", type=int, action="append", default=None, metavar="PID",
        help="leave the call (with a short announcement) as soon as process PID ends; pass your agent session, e.g. --owner-pid $PPID (env VOICEHOOK_OWNER_PID works too). $VOICEHOOK_AGENT_HOME/holder is watched automatically.",
    )
    p_join.add_argument(
        "--transport", choices=vtransport.TRANSPORTS, default="auto",
        help="auto (default): WebRTC, switch to the HTTPS bridge when HTTPS_PROXY/ALL_PROXY is set or WebRTC fails to connect. webrtc / bridge force one.",
    )
    p_join.add_argument(
        "--status-due", type=float, default=None, metavar="SEC",
        help=f"`next` adds status_due + hint when your board is older than SEC seconds while work is in progress (default {relay.STATUS_DUE_S:g}, env {relay.STATUS_DUE_ENV}; 0 = age rule off). Empty board and status_request always count.",
    )
    p_join.add_argument(
        "--no-control", action="store_true", default=False,
        help="do not open the local control socket (disables say/next/leave/status).",
    )
    p_join.add_argument(
        "--no-self-update", action="store_true", default=False,
        help=f"0.12.0: do not update this CLI when the server names a newer cli_latest (env "
             f"{vselfupdate.ENV_OPT_OUT}=1). Default: update in place and restart the join once.",
    )
    # one-shot commands against a running join
    def _add_session(p):
        p.add_argument("--session", default=None, metavar="SLUG",
                       help="<slug>/<identity> of the running join (a slug alone is enough when only one join runs in that room); needed only when several joins run.")
        p.add_argument("--wait", type=float, default=30.0, metavar="SEC",
                       help="wait up to SEC seconds for the join to come up (default 30).")
    p_say = sub.add_parser("say", help="speak one line through voice-ai in the running join")
    p_say.add_argument("text", nargs="+", help="text to speak ('-' reads it from stdin)")
    p_say.add_argument("--mode", choices=_SAY_MODES, default=SAY_DEFAULT_MODE,
                       help="append (default): queue at the end, starts right after the running say "
                            "(a running say is never cut). overwrite: replace your OWN not-started (queued) "
                            "says at their place, else append; never another operator's. revise: like "
                            "overwrite, plus operator.revise to you only listing the replaced texts; nothing "
                            "is held or cut.")
    p_say.add_argument("--urgent", action="store_true", default=False,
                       help="priority urgent: interrupt whoever is speaking (Delta or another operator) "
                            "and go first. Use sparingly.")
    _add_session(p_say)
    p_next = sub.add_parser("next", help="block until the next finalized user turn (or operator.revise); prints one JSON line")
    p_next.add_argument("--timeout", type=float, default=60.0, metavar="SEC",
                        help="give up after SEC seconds and print {\"type\":\"timeout\"} (default 60; 0 = only return what is queued).")
    _add_session(p_next)
    p_says = sub.add_parser(
        "says", help="last state of your own says (operator.say_status from the voicebot: "
        "sent/queued/spoken/interrupted/requeued/replaced) as JSON")
    _add_session(p_says)
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
    p_status.add_argument("--faq", action="append", default=[], metavar="'FRAGE::ANTWORT'",
                          help="0.10.0: a question the user will likely ask next + your answer, "
                          "split on the first '::' (repeatable, max 6, 200 chars each). Sent as "
                          "board field faq[{q, a}]; refresh it on every board update.")
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
    # 0.10.0 activity feed (Claude Code PostToolUse hook); the fast path without the
    # livekit import is the `voicehook-agent-hook` console script.
    p_su = sub.add_parser("self-update", help="update this CLI now (uv tool upgrade, else pip --upgrade)")
    p_su.add_argument("--dry-run", action="store_true", default=False,
                      help="only print the command that would run")
    p_hook = sub.add_parser("hook", help="activity feed: Claude Code PostToolUse hook (post-tool-use | print | install [--settings PATH])")
    p_hook.add_argument("action", choices=["post-tool-use", "print", "install"])
    p_hook.add_argument("--settings", default=None, metavar="PATH",
                        help="install: settings.json to merge into (default ~/.claude/settings.json)")
    args = ap.parse_args(argv)
    if args.cmd == "self-update":
        sys.exit(_run_self_update(args.dry_run))
    if args.cmd == "hook":
        hook_argv = [args.action] + (["--settings", args.settings] if args.settings else [])
        sys.exit(vactivity.main(hook_argv))
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
        updater = _Updater(args.no_self_update)
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
                no_human_timeout=max(0.0, args.no_human_timeout) * 60.0,
                idle_say=args.idle_say or None,
                owner_pids=args.owner_pid,
                force_persona=args.force_persona, control=not args.no_control,
                transport=args.transport, status_due=args.status_due,
                voice=args.voice, updater=updater,
            ))
        except KeyboardInterrupt:
            rc = 130
        if rc == RC_RESTART:
            _restart_after_update(argv, updater)
            rc = 1  # only reached when the exec failed
        sys.exit(rc)
    if args.cmd in ("say", "next", "says", "leave", "status"):
        sys.exit(_run_client(args))
    if args.cmd == "log-summary":
        try:
            rc = asyncio.run(_run_log_summary(args))
        except KeyboardInterrupt:
            rc = 130
        sys.exit(rc)


if __name__ == "__main__":
    main()
