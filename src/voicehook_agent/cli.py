"""voicehook-agent CLI: the pro tool for several agents or Claude Code sessions on one machine in one
voicehook.ai call; its multi-session relay and hook feed the activity log, status and decisions of all
local sessions into the call, which MCP and bridge cannot do: they run on the server and cannot see the
user's local sessions.

Usage:
    voicehook-agent join <invite-url> --name N --model M          # interactive mode
    voicehook-agent join <invite-url> --name N --model M --json   # JSONL stream mode

Agent loop without polling (against the running join, via a local socket):
    voicehook-agent say "Hallo, ich bin da."     # speak one line
    voicehook-agent next --timeout 60            # block until the next user turn
    voicehook-agent listen                       # 0.21.4: gapless next loop, exits on the
                                                 # first actionable event (run_in_background)
                                                 # 0.21.5: reads the join's durable event
                                                 # spool with a cursor; the join itself
                                                 # keeps you present while you work
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
    WebRTC blocked (0.14.3)          wait_pc_connection / ICE timeout and no transport
                                     left: stderr hint to the remote MCP server
                                     https://voicehook.ai/mcp (+ curl example), exit 4.
                                     CLAUDE_CODE_REMOTE=true or a proxy: one [info] line
                                     before connecting.

Status board (0.7.0):
    status [TEXT] [--doing T] [--open T]... [--done T]... [--faq 'F::A']... [-f board.json]
                                     send operator.status {doing, open[], done[]};
                                     replaces the last board, never spoken; '' clears.
                                     0.10.0: --faq 'Frage::Antwort' (max 3 per item, after it):
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
                                     Live mode (0.12.0): state "covered" + "note" = Delta
                                     already said it in his answer, do not send it again.
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
    revise stale=true (0.21.9)       your queued says older than 60 s (server env
                                     VOICEHOOK_SAY_STALE_S) were dropped unspoken: items
                                     [{seq,text}] + hint. Never repeat them; still relevant:
                                     ONE short current summary (say --mode overwrite)

Shapes in the ring (show): draw when a picture explains better than words.
    show --preset check              preset: arrow_up arrow_right check cross question
                                     loop split3 scale heart bolt one two three
    show --polygon "x,y x,y ..."     own form, coordinates 0..1 (0,0 top left); --open =
                                     not closed; --path "M0.1,0.9 L0.9,0.1" (only M L Q C Z)
    show --json '<shape>'            full shape; several --preset/--polygon/--path = multi
    show ... --label T --hold-ms N   label <= 24 chars, hold 800..8000 ms; --emotion LABEL;
                                     one shape per 2 s, faster: "type":"rate_limited"
    say --shape-preset NAME          shape/emotion ride along with the say (operator.say
    say --shape-json J --emotion L   fields shape, emotion), drawn when it is spoken
                                     WebRTC: topic operator.visual; bridge: POST
                                     /api/bridge/visual. Invalid shape: exit 2, nothing sent.

Activity feed (0.10.0): Delta sees what you are doing, one line per tool call.
    hook install [--settings PATH]   add the Claude Code PreToolUse + PostToolUse hooks to
                                     settings.json (0.13.0: idempotent, adds Pre to an
                                     older Post-only install)
    hook print                       print the settings.json snippet
    hook pre-tool-use|post-tool-use  the hooks themselves (fast path: voicehook-agent-hook)
    hook notification|permission-request  0.18.0: a session waits for the human (permission
                                     prompt, AskUserQuestion, idle): a decision on the
                                     Decision Board + a feed line; never auto-approves
                                     join publishes the newest 15 lines as operator.activity
                                     {lines, ts}, on change, at most every 5 s.

Self-update (0.12.0): the server names cli_min / cli_latest in every join answer.
    join                             cli_latest newer than this CLI: update in place (uv tool
                                     upgrade, else pip --upgrade) and restart the join with the
                                     same arguments (once per join). Update fails: warn, go on.
                                     HTTP 426 (below cli_min): print the upgrade command, try
                                     one self-update, else exit 7.
    join --no-self-update            off (env VOICEHOOK_NO_SELF_UPDATE=1)
    version notice (0.14.2)          always, also with self-update off: stderr [warn] plus,
                                     with --json, {"_meta":"update","latest":..,"commands":[..]}
                                     (uv tool install --upgrade / pip install -U / uvx @latest);
                                     HTTP 426: stderr [error] plus {"_meta":"outdated"}, exit 7

Join hint (0.14.0): once per join, after the connect, one line for YOU (never spoken):
    plain mode                       stderr: joined via voicehook.ai · Talk with your agents · <url>
    --json                           stdout: {"role":"system","topic":"_meta","_meta":"hint","text":..,"url":..}
    join --quiet                     off (env VOICEHOOK_QUIET=1)
    self-update                      update now; --version shows "neue Version verfügbar"

Activity log (0.13.0): keep it filled, Delta reads it in speech pauses.
    Delta only reads lines younger than 60 s ("what is happening right now"). Claude
    Code: run `voicehook-agent hook install` once (recommended): every tool call logs its
    own short description 1:1 when it starts (PreToolUse), file tools log the basename,
    never a path. Agents without hooks:
    voicehook-agent activity "<3-8 words>"
                                     append `HH:MM:SS note: <text>` to activity.log of the
                                     running join (scrubbed, capped at 120 chars); send
                                     your own short status line 1:1, do not rephrase
    next                             adds activity_due:true + activity_age_s +
                                     activity_hint when work is in progress (doing/open
                                     set, or you spoke / sent a board in the last 5 min)
                                     and no new line came for --activity-due SEC (default
                                     60, env VOICEHOOK_ACTIVITY_DUE, 0 = off). Silent while
                                     hook lines arrive; at most one hint per SEC.
    next                             adds stale_error {status_age_s, activity_age_s, message}
                                     on EVERY output (no rate limit) while you are active
                                     (doing/open set, a say in the last 5 min, or no board
                                     yet and the join older than SEC) and the board or the
                                     log is older than --stale-error SEC (default 60, env
                                     VOICEHOOK_STALE_ERROR_S, 0 = off); an age is null when
                                     that part is fresh. Never set = age since the join;
                                     while hook lines arrive only the board counts.
                                     Plain (non --json) join output: `!! stale: <message>`
                                     after every user turn.
    next                             always adds activity_now {text, age_s} and board_now
                                     {doing, age_s}: what Delta knows about you (null = none).
    Automatic: join writes ~/.voicehook/joins/<pid>.json so the hook finds it under any
    VOICEHOOK_AGENT_HOME. Curl bridge join (Quickstart A): `voicehook-agent bridge-session
    --save join.json --base URL` (only base + session, 0600, --clear on leave) lets the hook
    POST each line to /api/bridge/activity (2 s timeout, max one per 5 s, latest wins).

Multi-session relay (0.15.0): the joining session writes ~/.voicehook/relay.json (0600,
one relay token, label = this machine; bridge join and `bridge-session --save`; a WebRTC
join writes a local relay: the hooks append to its own activity.log) and deletes its own
file on leave. The hooks
of every OTHER Claude Code session here report turn goal, PR opened/merged, deploy and
errors under their session name (docs/CLI.md). Off: VOICEHOOK_NO_RELAY=1.

Helper sessions (0.16.0): a Claude Code session without its own join, while another one
of this machine is in the call: `decide` and `status --doing/--open/--done` go over the
relay (via:"relay", sender/sub = its session name); `relay-next [--timeout S]` prints the
answers; `decide --wait [--timeout S]` waits for this one (docs/CLI.md).

Decision background + page control (0.21.0):
    decide ... --context TEXT        background for Delta (max 1500 chars, never shown): what,
                                     why, what happens on yes / on no; --recent TEXT (max 600)
                                     = the last turn of the asking session. Also with --id and
                                     over the relay (helper sessions). Longer: error, nothing sent.
    next                             a decision event may carry verdict "unclear" + text: the
                                     human does not understand it; delete it, ask more simply.
    ui open|close|min|medium|large|max <window>   windows log dec board wrap cards
    ui layout <preset>               halves quarters thirds bigmid bar
    ui move <window> <slot> [--preset P]          slots l m r b tl tr bl br main side
    ui focus <project|all>           voicehook salesengine kunden sonstiges all
                                     WebRTC: topic delta.ui only to the humans in the room;
                                     bridge: POST /api/bridge/ui. Prints `ui: window log large`.
    status ... --project NAME        field project (1-40 chars) on operator.status;
    activity ... --project NAME      on operator.activity (the join keeps it for later packets)
    next                             Silent Mode: human_state {mic_muted, speaker_muted, away} on
                                     every output, event type human_state on a change. While
                                     speaker_muted/away nobody hears: says wait, Delta sums up on
                                     return; put what matters on board, cards or decide.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import secrets
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
from . import a2a as va2a
from . import activity as vactivity
from . import boardlist as vboardlist
from . import cards as vcards
from . import datachan as vdata
from . import helperaccess as vhelp
from . import localsessions as vlocal
from . import midturn as vmidturn
from . import multisession as vmulti
from . import plaintext as vplain
from . import relayops as vrelayops
from . import selfupdate as vselfupdate
from . import session as vsession
from . import spool as vspool
from . import transport as vtransport
from . import ui as vui
from . import visual as vvisual
from . import waitcheck as vwait

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


# 0.14.0: one discreet line per join for the agent's own user (terminal only, never
# spoken, never in persona/say). Off with --quiet or VOICEHOOK_QUIET=1.
SHARE_TEXT = "joined via voicehook.ai · Talk with your agents"
SHARE_URL = "https://voicehook.ai/?utm_source=agent-join&utm_campaign=viral"
QUIET_ENV = "VOICEHOOK_QUIET"


def _quiet_from_env(env=None) -> bool:
    val = (os.environ if env is None else env).get(QUIET_ENV, "")
    return val.strip().lower() in ("1", "true", "yes", "on")


def _emit_share_hint(json_mode: bool) -> None:
    """Plain: one line on stderr (stdout stays the transcript). --json: one `_meta`
    event with `_meta: "hint"` on stdout, so JSON parsers never see free text."""
    if json_mode:
        obj = {"role": "system", "topic": "_meta", "_meta": "hint",
               "text": SHARE_TEXT, "url": SHARE_URL}
        print(json.dumps(obj, ensure_ascii=False), flush=True)
    else:
        print(f"{SHARE_TEXT} · {SHARE_URL}", file=sys.stderr, flush=True)


async def _emit_sessions_hint(json_mode: bool, detect=None) -> dict | None:
    """0.21.4: which other Claude Code / Codex sessions run on this machine, with the
    question for the human and the setup commands (localsessions.py). --json: one `_meta`
    event `sessions` on stdout; plain: one line on stderr. For the operator, never spoken."""
    try:
        got = await asyncio.to_thread(detect or vlocal.detect)
    except Exception:  # noqa: BLE001
        return None
    if not got:
        return None
    if json_mode:
        print(json.dumps(got, ensure_ascii=False), flush=True)
    else:
        print(f"[sessions] {got['text']}", file=sys.stderr, flush=True)
    return got


# 0.21.5 onboarding: right after the join the agent learns how to bring more agents into the
# call (multi-agent). Never spoken, no credential (placeholders only). Off: VOICEHOOK_ONBOARDING_HINT=0.
ONBOARDING_ENV = "VOICEHOOK_ONBOARDING_HINT"
ONBOARDING_INVITE = [
    "voicehook-agent hook install",
    "voicehook-agent hook install --agent codex",
    "voicehook-agent join <invite-url> --name <Name> --model <Model> --json",
    "voicehook-agent helper-credential --label <host> | ssh <host> 'voicehook-agent helper-import --as <host>'",
]
ONBOARDING_SHARE = [
    'voicehook-agent status --doing "<step + ETA>" --open "<task>"',
    'voicehook-agent decide --question "<question>?" --context "<what, why, yes/no>"',
    "voicehook-agent board",
]
_ONBOARDING_TEXT = {
    "de": ("Multi-Agent: du kannst weitere Agenten in diesen Call holen. Frag den Menschen, ob du "
           "das einrichten sollst. (1) Einladen: jede weitere Claude-Code-Session dieses Rechners "
           "meldet sich nach einmal `{i0}` (Codex: `{i1}`) als Helfer im Call; soll sie mit eigener "
           "Stimme sprechen (jeder Agent eigene Stimme), tritt sie selbst bei: `{i2}` (derselbe "
           "Einladungslink). Anderer Rechner: `{i3}` (nie in einen Chat kopieren). (2) Einblenden "
           "und teilen: Helfer zeigen ihr Status-Board mit `{s0}` und offene Entscheidungen mit "
           "`{s1}` im Call (über den Relay), ihr Aktivitätslog kommt per Hook von selbst; `{s2}` "
           "zeigt dir alle Boards und offenen Entscheidungen. (3) Klartext-Regel für alle: "
           "Entscheidungen und Board-Einträge als ganzer Satz mit Produkt oder Bereich, was "
           "passiert und wozu, keine nackten PR-Nummern, Ziffernketten oder unerklärten "
           "Abkürzungen, Entscheidungen mit ? und --context; schlecht: \"{bad}\", gut: "
           "\"{good}\". (4) {wait_de} (5) {midturn_de}"),
    "en": ("Multi-agent: you can bring more agents into this call. Ask the human whether to set "
           "it up. (1) Invite: every other Claude Code session on this machine reports as a "
           "helper in the call after running `{i0}` once (Codex: `{i1}`); to speak with its own "
           "voice (every agent has its own voice), it joins itself: `{i2}` (the same invite "
           "link). Another machine: `{i3}` (never paste it into a chat). (2) Show and share: "
           "helpers put their status board into the call with `{s0}` and open decisions with "
           "`{s1}` (over the relay); their activity log comes by itself through the hook; `{s2}` "
           "shows you every board and open decision. (3) Plain-language rule for everyone: "
           "decisions and board items as a whole sentence naming the product or area, what "
           "happens and why, no bare PR numbers, digit chains or unexplained abbreviations, "
           "decisions with ? and --context; bad: \"{bad}\", good: \"{good}\". (4) {wait_en} (5) {midturn_en}"),
}


def onboarding_lang(env=None) -> str:
    """'de' when the locale is German (LC_ALL, LC_MESSAGES, LANG), else 'en'."""
    env = os.environ if env is None else env
    for k in ("LC_ALL", "LC_MESSAGES", "LANG"):
        v = (env.get(k) or "").strip()
        if v:
            return "de" if v.lower().startswith("de") else "en"
    return "en"


def onboarding_hint(env=None) -> dict | None:
    """The `_meta: onboarding` event (None: off)."""
    env = os.environ if env is None else env
    if (env.get(ONBOARDING_ENV) or "").strip().lower() in ("0", "false", "no", "off"):
        return None
    lang = onboarding_lang(env)
    fmt = {**{f"i{n}": c for n, c in enumerate(ONBOARDING_INVITE)},
           **{f"s{n}": c for n, c in enumerate(ONBOARDING_SHARE)},
           "bad": vplain.BAD_EXAMPLE, "good": vplain.GOOD_EXAMPLE,
           "wait_de": vwait.RULE + " Sichtbar wartend: `voicehook-agent status --waiting \"...\" "
                      "--question \"...?\" --context \"...\"`.",
           "midturn_de": vmidturn.RULE + " Neue Äußerungen kommen per Hook mitten in deinen Turn.",
           "midturn_en": vmidturn.RULE_EN + " New utterances reach you mid-turn through the hook.",
           "wait_en": vwait.RULE_EN + " Shown as waiting: `voicehook-agent status --waiting \"...\" "
                      "--question \"...?\" --context \"...\"`."}
    return {"role": "system", "topic": "_meta", "_meta": "onboarding", "lang": lang,
            "text": _ONBOARDING_TEXT[lang].format(**fmt),
            "commands": {"invite": list(ONBOARDING_INVITE), "share": list(ONBOARDING_SHARE)},
            "rule": {"text": vplain.RULE, "bad": vplain.BAD_EXAMPLE, "good": vplain.GOOD_EXAMPLE,
                     "waiting": vwait.RULE, "midturn": vmidturn.RULE}}


ONBOARDING_EVENT_TEXT = {
    "de": ("Biete {who} an, weitere Sessions einzuladen (helper-credential | helper-import oder "
           "eigener Join) und deren Boards zu teilen."),
    "en": ("Offer {who} to invite more sessions (helper-credential | helper-import or their own "
           "join) and to share their boards."),
}


def onboarding_event(username: str | None = None, env=None) -> dict | None:
    """0.21.8: the onboarding as the first `next`/`listen` event of the join (type onboarding,
    wake-worthy; midturn.py shows it inside a running turn), so an agent that never reads the
    join's stdout gets it too. None: VOICEHOOK_ONBOARDING_HINT=0."""
    hint = onboarding_hint(env)
    if hint is None:
        return None
    lang = hint["lang"]
    who = (username or "").strip()[:40] or ("dem Menschen" if lang == "de" else "the human")
    return {"type": "onboarding", "text": ONBOARDING_EVENT_TEXT[lang].format(who=who),
            "details": hint["text"], "commands": hint["commands"], "rule": hint["rule"],
            "lang": hint["lang"], "ts": time.time()}


def _emit_onboarding_hint(json_mode: bool, env=None) -> dict | None:
    """0.21.5: --json: one `_meta` event `onboarding` on stdout; plain: one line on stderr."""
    got = onboarding_hint(env)
    if got is None:
        return None
    if json_mode:
        print(json.dumps(got, ensure_ascii=False), flush=True)
    else:
        print(f"[onboarding] {got['text']}", file=sys.stderr, flush=True)
    return got


def _emit_version_notice(json_mode: bool, kind: str, text: str, **extra) -> None:
    """0.14.2: version notice the user always sees. stderr always (also with --json);
    --json adds one `_meta` event on stdout: kind "update" (cli_latest newer, join goes on)
    or "outdated" (HTTP 426 below cli_min, exit 7)."""
    print(f"[{'warn' if kind == 'update' else 'error'}] {text}", file=sys.stderr, flush=True)
    if json_mode:
        obj = {"role": "system", "topic": "_meta", "_meta": kind, "text": text,
               "current": _VERSION, **extra}
        print(json.dumps(obj, ensure_ascii=False), flush=True)


def _update_text(cmds: list[str]) -> str:
    return f"Update: {cmds[0]}" + "".join(f"  or: {c}" for c in cmds[1:])


def stale_running_joins(current: str = _VERSION) -> list[dict]:
    """0.25.0: running joins on this machine whose CLI version is older than `current` (this
    binary). Each entry: {session '<slug>/<identity>', version}. A join that records no version
    (pre-0.25.0) counts as stale too. Best effort, never raises."""
    out: list[dict] = []
    try:
        for d in vsession.live_sessions():
            info = vsession.read_info(d)
            ver = info.get("cli_version") if isinstance(info, dict) else None
            ver = ver if isinstance(ver, str) else None
            if ver is None or vselfupdate.is_newer(current, ver):
                out.append({"session": vsession._label(d), "version": ver})
    except Exception:  # noqa: BLE001
        return out
    return out


def _emit_stale_join_notice(json_mode: bool, stale: list[dict]) -> None:
    """0.25.0: warn (never spoken) that a running join is on an older CLI than this binary, and
    name the fix `voicehook-agent rejoin`. stderr always; --json adds one `_meta: "stale_join"`."""
    if not stale:
        return
    names = ", ".join(f"{s['session']} ({s['version'] or 'unknown'})" for s in stale)
    text = (f"a running join is on an older voicehook-agent than this one ({_VERSION}): {names}. "
            "Rejoin it in place (identity, queue and board kept): voicehook-agent rejoin"
            + (" --session <slug>/<identity>" if len(stale) > 1 else ""))
    print(f"[warn] {text}", file=sys.stderr, flush=True)
    if json_mode:
        print(json.dumps({"role": "system", "topic": "_meta", "_meta": "stale_join",
                          "text": text, "current": _VERSION, "stale": stale},
                         ensure_ascii=False), flush=True)


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


_BG_TASKS: set = set()


async def _publish_relay(room, tok: dict | None, slug: str, json_mode: bool,
                         local_dir: Path | None = None, *, api_base: str = "",
                         ctl: _Control | None = None, stop: asyncio.Event | None = None) -> None:
    """0.15.0: write ~/.voicehook/relay.json so the hooks of the OTHER Claude Code sessions
    of this machine feed this call (multisession.py). Bridge: mint a relay token with the
    bridge session; a `relay` in the join answer wins. Otherwise (WebRTC, the default, or
    a failed mint) a LOCAL relay: the hooks append labeled lines to this join's own
    activity.log (`local_dir`), which the activity loop publishes. Never raises, never
    prints the token."""
    try:
        # WebRTC: /api/token itself has token+url (LiveKit), so only a nested `relay` counts
        nested = tok.get("relay") if isinstance(tok, dict) else None
        rel = vmulti.relay_from_answer(nested) if isinstance(nested, dict) else None
        if rel is None and hasattr(room, "mint_relay"):
            rel = await room.mint_relay(vmulti.machine_label())
        if rel is None and ctl is not None and api_base and isinstance(tok, dict):
            rel = await _helper_hub(ctl, api_base, str(tok.get("token") or ""))  # 0.17.0
        if rel is not None:
            path = vmulti.write_relay(rel["url"], rel["token"], slug, rel.get("expires_in"))
            kind = ""
        elif local_dir is not None:
            path = vmulti.write_local_relay(local_dir, slug)
            kind = " (local)"
        else:
            return
        _print_event(json_mode, "system",
                     f"activity relay ready for the other Claude Code sessions here{kind}: {path}",
                     topic="_meta")
    except Exception as e:  # noqa: BLE001
        print(f"[warn] activity relay not written ({type(e).__name__})", file=sys.stderr, flush=True)
    if ctl is not None and ctl.helper_pump is not None and stop is not None:
        await _run_helper_pump(room, ctl, stop)


VOCAB_PATH = "/api/bridge/vocab"
VOCAB_TERM_MAX = 50


def vocab_request(term: str, wrongs: list[str] | None) -> dict:
    """`vocab add TERM [--wrong W ...]` -> control request; ValueError on bad input."""
    t = " ".join(str(term or "").split())
    ws = [" ".join(str(w or "").split()) for w in (wrongs or [])]
    if not t or any(not w for w in ws):
        raise ValueError("vocab add: empty word")
    if len(t) > VOCAB_TERM_MAX or any(len(w) > VOCAB_TERM_MAX for w in ws):
        raise ValueError(f"vocab add: at most {VOCAB_TERM_MAX} characters per word")
    if any(w.casefold() == t.casefold() for w in ws):
        raise ValueError("vocab add: --wrong equals the word")
    if ws:
        return {"cmd": "vocab", "fixes": [{"wrong": w, "right": t} for w in ws]}
    return {"cmd": "vocab", "terms": [t]}


async def _vocab_add(ctl: _Control, body: dict) -> dict:
    """POST /api/bridge/vocab with this join's operator credential (bridge: its session,
    WebRTC: its LiveKit token, after it expired (404, 1 h) the helper pump token).
    One JSON answer, never the token."""
    room = ctl.room
    bearers = [b for b in (ctl.lk_jwt, getattr(ctl.helper_pump, "token", None)) if b]
    if room is not None and hasattr(room, "post_vocab"):
        status, data = await room.post_vocab(body)
    elif bearers and ctl.api_base:
        status, data = 0, {}
        try:
            async with httpx.AsyncClient(timeout=10.0, headers={"user-agent": _USER_AGENT,
                                                                "x-vh-cli": _VERSION}) as http:
                for bearer in bearers:   # LiveKit token expired (404) -> pump token
                    r = await http.post(ctl.api_base.rstrip("/") + VOCAB_PATH, json=body,
                                        headers={"authorization": f"Bearer {bearer}"})
                    status = r.status_code
                    try:
                        data = r.json()
                    except ValueError:
                        data = {}
                    if status != 404 or len(bearers) == 1:
                        break
        except httpx.HTTPError as e:
            return {"ok": False, "type": "vocab", "error": f"request failed ({type(e).__name__})"}
    else:
        return {"ok": False, "type": "vocab", "error": "not connected to the call (yet)"}
    if status == 200 and isinstance(data, dict):
        return {**data, "ok": True, "type": "vocab"}
    detail = data.get("detail") if isinstance(data, dict) else None
    if status in (404, 405) and not detail:
        detail = "server has no vocabulary endpoint (too old)"
    return {"ok": False, "type": "vocab", "status": status, "error": str(detail or f"HTTP {status}")}


# notify-me / call-me: the server's existing features (voicehook-v4 billing/notify_me.py,
# billing/call_me.py). WebRTC join: POST /api/cli/<kind> with its LiveKit token (after it
# expired: the helper pump token), bridge join: /api/bridge/<kind> with its session.
PHONE_KINDS = ("notify_me", "call_me")
PHONE_REASONS = ("done", "blocked", "decision")
PHONE_MESSAGE_MAX = {"notify_me": 120, "call_me": 200}
CLI_PHONE_PATH = "/api/cli/{kind}"
PHONE_HTTP_TIMEOUT_S = {"notify_me": 20.0, "call_me": 720.0}   # server dials up to 690 s
PHONE_SOCK_TIMEOUT_S = {"notify_me": 30.0, "call_me": 750.0, "data": 120.0,  # data: upload
                        "card": 60.0}  # 0.23.1: image upload + wait for the call pages


def phone_request(kind: str, reason: str, message: str | None) -> dict:
    """`notify-me|call-me --reason R [--message M]` -> control request; ValueError on bad input."""
    if kind not in PHONE_KINDS:
        raise ValueError(f"unknown kind {kind!r}")
    if reason not in PHONE_REASONS:
        raise ValueError(f"--reason must be one of {', '.join(PHONE_REASONS)}")
    req: dict = {"cmd": kind, "reason": reason}
    if message is not None:
        m = " ".join(str(message).split())
        if not m:
            raise ValueError("--message is empty")
        if len(m) > PHONE_MESSAGE_MAX[kind]:
            raise ValueError(f"--message max {PHONE_MESSAGE_MAX[kind]} characters")
        req["message"] = m
    return req


async def _phone_post(ctl: _Control, kind: str, body: dict) -> dict:
    """POST notify_me / call_me with this join's operator credential -> the server's JSON
    ({status, why?, phone_call_id?, ...}) plus ok/type. Never the token in the answer."""
    room = ctl.room
    if room is not None and hasattr(room, "post_phone"):
        status, data = await room.post_phone(kind, body, PHONE_HTTP_TIMEOUT_S[kind])
    else:
        bearers = [b for b in (ctl.lk_jwt, getattr(ctl.helper_pump, "token", None)) if b]
        if not ctl.api_base or not bearers:
            return {"ok": False, "type": kind, "error": "not connected to the call (yet)"}
        url = ctl.api_base.rstrip("/") + CLI_PHONE_PATH.format(kind=kind)
        status, data = 0, {}
        try:
            async with httpx.AsyncClient(timeout=PHONE_HTTP_TIMEOUT_S[kind],
                                         headers={"user-agent": _USER_AGENT, "x-vh-cli": _VERSION}) as http:
                for bearer in bearers:   # LiveKit token expired (404) -> pump token
                    r = await http.post(url, json=body, headers={"authorization": f"Bearer {bearer}"})
                    status = r.status_code
                    try:
                        data = r.json()
                    except ValueError:
                        data = {}
                    if status != 404 or len(bearers) == 1:
                        break
        except httpx.HTTPError as e:
            return {"ok": False, "type": kind, "error": f"request failed ({type(e).__name__})"}
        data = data if isinstance(data, dict) else {}
    if status == 200 and isinstance(data, dict):
        return {"ok": True, "type": kind, **data}
    detail = data.get("detail") if isinstance(data, dict) else None
    if status in (404, 405) and detail in (None, "", "Not Found", "Method Not Allowed"):
        detail = f"server has no {kind} endpoint for the CLI (too old)"
    if status == 429 and not detail:
        detail = "too many requests, try again in a minute"
    return {"ok": False, "type": kind, "status": status, "error": str(detail or f"HTTP {status}")[:300]}


async def _helper_hub(ctl: _Control, api_base: str, jwt: str) -> dict | None:
    """0.17.0, WebRTC join: helper credential + pump token from the server (Bearer = our
    LiveKit token), once per join; reconnects reuse it. None: older server (local relay)."""
    if ctl.helper_rel is not None:
        return ctl.helper_rel
    if not jwt:
        return None
    if ctl.helper_http is None:
        ctl.helper_http = httpx.AsyncClient(timeout=vhelp.HTTP_TIMEOUT_S,
                                            headers={"user-agent": _USER_AGENT, "x-vh-cli": _VERSION})
    rel = await vhelp.mint(ctl.helper_http, api_base, jwt, vmulti.machine_label())
    if rel is None or not rel.get("pump"):
        return None
    ctl.helper_pump = vhelp.Pump(api_base, rel["pump"]["url"], rel["pump"]["token"])
    ctl.helper_rel = {k: rel[k] for k in ("token", "url", "expires_in")}
    return ctl.helper_rel


async def _run_helper_pump(room, ctl: _Control, stop: asyncio.Event) -> None:
    """Publish the helpers' packets as this join until the cycle ends (stop)."""
    pump, http = ctl.helper_pump, ctl.helper_http
    if pump is None or http is None:
        return

    async def publish_status(payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        await room.local_participant.publish_data(data, reliable=True, topic="operator.status")
        ctl.ledger.board(ctl.ident, payload)  # `board` (Router-Fund 21.10.): our own packets never come back

    async def decide(body: dict) -> dict:
        out = await _decide_rpc(ctl, body)
        ctl.ledger.created(body, out, frm=str(body.get("sender") or ""))  # `board` (Router-Fund 21.10.)
        return out

    def append_activity(line: str) -> None:
        if ctl.activity_path is not None:
            vactivity.append_line(ctl.activity_path.parent, vactivity.scrub_line(line))

    await pump.run(http, publish_status=publish_status,
                   decide=decide,
                   append_activity=append_activity, stop=stop,
                   show_card=lambda card: _publish_helper_card(ctl, room, card))


async def _publish_helper_card(ctl: _Control, room, card: dict) -> dict:
    """0.23.1: a helper's card (server hub): publish as ourselves with its `from`; an image
    only counts once a call page loaded it. -> pump result {ok, shown?} | {ok: false, status, detail}."""
    pkt = vcards.relay_card(card, ctl.card_seq + 1)
    if pkt is None:
        return {"ok": False, "status": 422, "detail": "invalid card"}
    if "url" in pkt:
        ctl.card_acks.expect(pkt["id"])
    try:
        await room.local_participant.publish_data(json.dumps(pkt, ensure_ascii=False).encode("utf-8"),
                                                  reliable=True, topic=vcards.TOPIC)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "status": 503, "detail": f"card not sent: {type(e).__name__}"}
    ctl.card_seq += 1
    if "url" not in pkt:
        return {"ok": True}
    humans = _human_ids(room)
    loaded, failed = await ctl.card_acks.wait(pkt["id"], humans)
    res = vcards.ack_result({"ok": True}, loaded, failed, len(humans))
    if res.get("ok"):
        return {k: res[k] for k in ("ok", "shown", "not_loaded") if k in res}
    return {"ok": False, "status": 409, "detail": res.get("error", "not shown")}


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
                 no_human_timeout: float = NO_HUMAN_TIMEOUT,
                 data_channel: bool = False) -> None:
        self.slug = slug
        self.ident = ident
        self.say_tracker = say_tracker
        self.echo = echo
        # 0.24.1 data-channel mode (--data-channel): a user turn wakes only when addressed to
        # us (name/mention or routed_by_delta); broad turns are read-only context (since_last).
        # Decisions / helper_decisions wake regardless. Off by default (behaviour unchanged).
        self.data_channel = data_channel
        # 0.21.5: the join is the listener: events go into a durable spool (spool.py), `next`
        # leases + acks them; _join() points it at the session dir (memory only until then)
        self.events = vspool.SpoolQueue()
        # 0.24.0 targeted wake: user turns not meant for us, delivered with the next woken event
        self.quiet = relay.QuietTurns()
        self.events.before_add = self.quiet.attach
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
        # 0.13.0 activity_due: remind the agent when the log stays silent while it works
        self.activity_due = vactivity.ActivityDue(due_s=vactivity.activity_due_seconds())
        self.activity_path: Path | None = None
        self.stale_s = relay.stale_error_seconds()  # 0.13.0 stale_error threshold
        self.activity_sent: tuple[str, float] | None = None  # 0.13.0 activity_now
        self.activity_project: str | None = None  # 0.21.0 `activity --project`, kept for later packets
        self.activity_meta: dict[str, dict] = {}  # 0.21.4 line -> {context, faq}
        self.leaving = False
        # 0.25.0 rejoin: `voicehook-agent rejoin` asked this join to re-exec in place (same
        # identity, spool and board kept) instead of leaving. Set by the control handler; _join
        # turns the clean quit into RC_RESTART so main() re-execs with the same --identity.
        self.rejoining = False
        # 0.10.1: no-human guard. Starts at join (nobody seen yet), stops while a
        # human is in the room, restarts when the last one leaves. Spans reconnects.
        self.no_human_timeout = no_human_timeout
        self.no_human_since: float | None = time.monotonic()
        self.call_ended: str | None = None  # reason of a server-side call end
        # 0.7.0: latency_warning + status_stale; 0.9.0: status_due + hint for `next`
        self.clock = relay.TurnClock(due_s=relay.status_due_seconds())
        self.agent_said = relay.AgentSaid()  # 0.7.0: Delta's own lines for `next`
        self.say_status = relay.SayStatus()  # 0.9.0: operator.say_status per own say
        # 0.16.0: helper sessions (local relay): sender -> from, for decision events
        self.relay_senders: dict[str, str] = {}
        # 0.18.1: local relay: decision id -> question, for the helper_decision copy
        self.relay_questions: dict[str, str] = {}
        # 0.21.4 `board` / `decide --list`: decisions + boards seen by this join
        self.ledger = vboardlist.Ledger()
        # 0.21.4: local helpers: sender -> {"board": last board, "state": last session state},
        # so a state update keeps the board and a board update keeps the state (like the server)
        self.relay_helpers: dict[str, dict] = {}
        # 0.17.0 helper access: the server's helper hub for this join (WebRTC), its
        # credential (relay.json) and one HTTP client for the pump; spans reconnects
        self.helper_pump: vhelp.Pump | None = None
        self.helper_rel: dict | None = None
        self.helper_http = None
        # 0.21.0 cards (show --card): own seq, the api base + LiveKit token of the current
        # WebRTC cycle (image upload)
        self.card_seq = 0
        self.card_acks = vcards.Acks()  # 0.23.1: vh.card_ack of the call pages
        self.api_base = ""
        self.lk_jwt = ""
        # 0.21.0 Silent Mode: aggregate of the humans' vh.mic_muted/vh.speaker_muted/vh.away
        self.human_state = relay.human_state([])
        # 0.21.4 listen guard: waiting `next` requests, monotonic end of the last one
        self.listening = 0
        self.listen_end: float | None = None
        self.last_say_at: float | None = None  # 0.21.7 (midturn.py): wall clock of the last say
        self.onboarding: dict | None = None  # 0.21.8: queued once, with the first say/next

    def attach(self, room: rtc.Room) -> None:
        self.room = room
        self.room_ready.set()

    def detach(self) -> None:
        self.room = None
        self.room_ready.clear()

    def humans_changed(self, room) -> None:
        """Recount humans in `room` (see relay.is_human_peer), arm/stop the guard. 0.21.0: also
        the human state (Silent Mode); a change is queued for `next` as type human_state."""
        peers = [(_kind_label(p), dict(getattr(p, "attributes", None) or {}))
                 for p in room.remote_participants.values()]
        humans = [attrs for kind, attrs in peers if relay.is_human_peer(kind, attrs)]
        if humans:
            self.no_human_since = None
        elif self.no_human_since is None:
            self.no_human_since = time.monotonic()
        st = relay.human_state(humans)
        if st != self.human_state:
            self.human_state = st
            self.events.put_nowait(relay.human_state_event(st))

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


def _human_identities(room) -> list[str]:
    """The humans in the room (relay.is_human_peer: no operator agent, not the worker)."""
    return sorted(p.identity for p in room.remote_participants.values()
                  if relay.is_human_peer(_kind_label(p), dict(getattr(p, "attributes", None) or {})))


async def _publish_ui(room, body: dict) -> dict:
    """0.21.0 page control: bridge -> POST /api/bridge/ui (body without v/via), WebRTC ->
    topic delta.ui (v2 packet) ONLY to the humans in the room. No human: nothing is sent
    (an empty destination list would reach everyone)."""
    send_ui = getattr(room, "send_ui", None)
    try:
        if send_ui is not None:
            out = await send_ui(body)
            applied = out.get("applied") if isinstance(out.get("applied"), dict) else body
            res = {"ok": True, "type": "ui", "via": "bridge", "applied": applied}
            if isinstance(out.get("humans"), int):
                res["humans"] = out["humans"]
            return res
        humans = _human_identities(room)
        if humans:
            data = json.dumps(vui.packet(body), ensure_ascii=False).encode("utf-8")
            await room.local_participant.publish_data(data, reliable=True, topic=vui.TOPIC,
                                                      destination_identities=humans)
        return {"ok": True, "type": "ui", "via": "datachannel", "applied": body,
                "humans": len(humans)}
    except vtransport.BridgeError as e:
        detail = (e.body or {}).get("detail") if isinstance(e.body, dict) else None
        if e.status == 422:
            return {"ok": False, "type": "invalid", "error": f"server rejected the ui command: {detail or e}"}
        return {"ok": False, "type": "ui", "status": e.status, "error": f"ui not sent: {e}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "type": "ui", "error": f"ui not sent: {type(e).__name__}"}


async def _publish_visual(room, body: dict) -> dict:
    """Shape for the ring: bridge -> POST /api/bridge/visual, WebRTC -> data channel
    topic operator.visual. Same body either way."""
    send_visual = getattr(room, "send_visual", None)
    try:
        if send_visual is not None:
            await send_visual(body)
            via = "bridge"
        else:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            await room.local_participant.publish_data(data, reliable=True, topic=vvisual.TOPIC)
            via = "datachannel"
    except vtransport.BridgeError as e:
        if e.status == 429:
            return {"ok": False, "type": "rate_limited", "error": VISUAL_RATE_ERROR}
        if e.status == 400:
            return {"ok": False, "type": "invalid", "error": f"server rejected the shape: {e}"}
        return {"ok": False, "type": "visual", "error": f"visual not sent: {e}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "type": "visual", "error": f"visual not sent: {e}"}
    return {"ok": True, "type": "visual", "via": via, "shape": body["shape"]["type"]}


VISUAL_RATE_ERROR = ("rate limit: at most one shape per 2 s per operator, "
                     "nothing drawn; send it again in 2 s or skip it")
CARD_IMAGE_OLD_SERVER = ("image cards over WebRTC need a newer server (POST /api/card/image "
                         "missing); text, code and link cards work")


async def _upload_card_image(ctl: _Control, image: str) -> dict:
    """0.21.0: store an image card on the server (POST /api/card/image, Bearer = the LiveKit
    token of this cycle, after it expired the helper pump token) -> {ok, id, url, mime}.
    Never prints a token."""
    bearers = [b for b in (ctl.lk_jwt, getattr(ctl.helper_pump, "token", None)) if b]
    if not ctl.api_base or not bearers:
        return {"ok": False, "type": "card", "error": "not connected to the room (yet)"}
    r = None
    async with httpx.AsyncClient(timeout=20.0, headers={"user-agent": _USER_AGENT,
                                                        "x-vh-cli": _VERSION}) as http:
        for bearer in bearers:
            try:
                r = await http.post(ctl.api_base.rstrip("/") + vcards.IMAGE_PATH, json={"image": image},
                                    headers={"authorization": f"Bearer {bearer}"})
            except httpx.HTTPError as e:
                return {"ok": False, "type": "card", "error": f"image not sent: {type(e).__name__}"}
            if r.status_code != 404 or len(bearers) == 1:
                break
    try:
        j = r.json()
    except ValueError:
        j = {}
    j = j if isinstance(j, dict) else {}
    detail = str(j.get("detail") or "")[:300]
    if r.status_code == 200 and vcards.image_url_ok(j.get("url"), j.get("id")):
        return {"ok": True, "id": j["id"], "url": j["url"], "mime": str(j.get("mime") or "")}
    if r.status_code in (404, 405) and detail in ("", "Not Found", "Method Not Allowed"):
        return {"ok": False, "type": "card", "error": CARD_IMAGE_OLD_SERVER}
    if r.status_code == 422:
        return {"ok": False, "type": "invalid", "error": f"server rejected the image: {detail}"}
    if r.status_code == 429:
        return {"ok": False, "type": "rate_limited", "error": "too many images, try again in a minute"}
    return {"ok": False, "type": "card", "error": f"image not sent: HTTP {r.status_code} {detail}".strip()}


def _human_ids(room) -> set[str]:
    """Identities of the humans in the room (call pages)."""
    return {p.identity for p in room.remote_participants.values()
            if relay.is_human_peer(_kind_label(p), dict(getattr(p, "attributes", None) or {}))}


async def _publish_card(ctl: _Control, room, body: dict) -> dict:
    """0.21.0 `show --card`: bridge -> POST /api/bridge/show (same body); WebRTC -> the card
    as topic operator.card (image: stored on the server first) and the panel as delta.ui to
    the humans only. Answer {ok, type: card, via, id?, seq?, kind?, panel?}."""
    send_card = getattr(room, "send_card", None)
    if send_card is not None:
        try:
            out = await send_card(body)
        except vtransport.BridgeError as e:
            if e.status == 429:
                return {"ok": False, "type": "rate_limited", "error": "too many sends, try again shortly"}
            if e.status == 422:
                return {"ok": False, "type": "invalid", "error": f"server rejected the card: {e}"}
            return {"ok": False, "type": "card", "error": f"card not sent: {e}"}
        out = out if isinstance(out, dict) else {}
        res = {"ok": True, "type": "card", "via": "bridge"}
        res.update({k: out[k] for k in ("id", "seq", "panel", "large") if k in out})
        if "kind" in body:
            res["kind"] = body["kind"]
        return res
    res: dict = {"ok": True, "type": "card", "via": "datachannel"}
    if "kind" in body:
        url = mime = cid = None
        if body["kind"] == "image":
            up = await _upload_card_image(ctl, body["image"])
            if not up.get("ok"):
                return up
            url, mime, cid = up["url"], up["mime"], up["id"]
        try:
            card = vcards.make_card(seq=ctl.card_seq + 1, body=body, url=url, mime=mime, cid=cid)
        except vcards.CardError as e:
            return {"ok": False, "type": "invalid", "error": str(e)}
        data = json.dumps(card, ensure_ascii=False).encode("utf-8")
        if url is not None:
            ctl.card_acks.expect(card["id"])
        try:
            await room.local_participant.publish_data(data, reliable=True, topic=vcards.TOPIC)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "type": "card", "error": f"card not sent: {type(e).__name__}"}
        ctl.card_seq += 1
        res.update(id=card["id"], seq=card["seq"], kind=card["kind"])
        if url is not None:
            # 0.23.1: ok only when a call page loaded the image (vh.card_ack)
            humans = _human_ids(room)
            loaded, failed = await ctl.card_acks.wait(card["id"], humans)
            res = vcards.ack_result(res, loaded, failed, len(humans))
            if not res.get("ok"):
                return res
    panel, large = body.get("panel"), body.get("large") is True
    if panel is not None:
        humans = sorted(_human_ids(room))
        if humans:
            try:
                for pkt in vcards.ui_packets(panel, large=large, cid=res.get("id")):
                    await room.local_participant.publish_data(
                        json.dumps(pkt).encode("utf-8"), reliable=True, topic=vcards.UI_TOPIC,
                        destination_identities=humans)
            except Exception as e:  # noqa: BLE001
                return {**res, "ok": False, "error": f"panel not opened: {type(e).__name__}"}
        else:
            res["hint"] = "no human in the call: panel not opened"
        res["panel"] = panel
        if large:
            res["large"] = True
    return res


SHOW_HELP = """\
Draw a shape in the ring (Kringel) while you explain something by voice.

When to draw: a sequence or loop (arrow_right, loop), a comparison (scale, split3),
a structure (own polygon, multi with label), yes or no (check, cross), a count of
one to three (one, two, three). When not: not with every sentence, at most every few
turns, never instead of speaking. Your says stay whole, natural, phone-ready sentences.

Coordinates 0..1, (0,0) top left. Max 200 points, path d max 2 KB, whole shape
max 8 KB, multi max 4 items, label max 24 chars (no control chars, emoji, <>),
hold 800..8000 ms. Invalid input: exit 2, nothing sent. At most one shape per 2 s
per operator: faster gives {"type":"rate_limited"}, exit 1.

Examples:
  voicehook-agent show --preset check --emotion joy
  voicehook-agent show --polygon "0.2,0.9 0.2,0.45 0.5,0.15 0.8,0.45 0.8,0.9" --label Haus
  voicehook-agent show --label "Vorher, nachher" --json '{"type":"multi","items":[
      {"type":"polygon","points":[[0.1,0.4],[0.3,0.4],[0.3,0.6],[0.1,0.6]]},
      {"type":"path","d":"M0.38,0.5 L0.62,0.5"},
      {"type":"polygon","points":[[0.7,0.25],[0.9,0.25],[0.9,0.75],[0.7,0.75]]}]}'
  voicehook-agent say "Erst testen, dann ausrollen." --shape-preset arrow_right

Cards on the call page (0.21.0, same as bridge/MCP show, never spoken; a new card shows up
as a small preview top left). --card text|code|link|image with --text T ('-' = stdin) or
--file PATH (image: svg/png/jpeg/webp), optional --title. --panel cards|board|log|decisions|
none opens that panel for the humans (none closes all). --large opens the card view big in
the middle with this card at full size, for diagrams (swimlanes, many steps and arrows).
Limits: text 4000 chars, title 80, SVG 300 KB (no <script>, <foreignObject>, on* attributes
or external references), PNG/JPEG/WebP 1 MB; invalid: exit 2, nothing sent.
An image answers ok (with "shown") only after a call page loaded it; else ok false, exit 1
(not_shown / no_human). A helper session (no own join, relay.json) sends cards over the
relay with itself as sender (no --panel/--large).
  voicehook-agent show --card code --title Fix --text "git push origin main"
  voicehook-agent show --card link --text https://voicehook.ai --panel cards
  voicehook-agent show --card image --title Ablauf --file swimlane.svg --large
  voicehook-agent show --panel board
"""


UI_HELP = f"""\
Control the call page of the humans in the room (0.21.0), like their own clicks.

  voicehook-agent ui open|close|min|medium|large|max <window>
  voicehook-agent ui layout <preset>
  voicehook-agent ui move <window> <slot> [--preset P]
  voicehook-agent ui focus <project|all>

Windows:  {', '.join(vui.WINDOWS)}  (activity/aktivitaet/feed = log, decisions/entscheidungen
          = dec, status/statusboard = board, transcript/transkript/chat = wrap, karten = cards)
Presets:  {', '.join(vui.PRESETS)}  (dreiteiler/drittel = thirds, viertel = quarters,
          haelften = halves, gross-mitte/big = bigmid, leiste = bar)
Slots:    {', '.join(vui.SLOTS)}  (left/links, middle/mitte, right/rechts, bottom/unten)
Projects: {', '.join(vui.PROJECTS)}  (sales engine/se, customers, other, alles)

WebRTC: data packet topic delta.ui (v2) only to the humans; bridge: POST /api/bridge/ui.
Unknown value: exit 2, nothing sent. Success prints one line, e.g. `ui: window log large`.

Examples:
  voicehook-agent ui large decisions
  voicehook-agent ui layout dreiteiler
  voicehook-agent ui move activity links --preset thirds
  voicehook-agent ui focus sales engine
"""


# 0.11.0 (multi-operator): append is the default; a running say is never cut (only own
# operator.interrupt / --urgent). overwrite/revise replace only our OWN not-started says.
_SAY_MODES = ("append", "overwrite", "revise")
SAY_DEFAULT_MODE = "append"
_DECIDE_RPC = "vh.decide"  # decisions.RPC_METHOD on the worker (Decision Board)
_LIST_RPC = "vh.decisions"  # decisions.RPC_LIST: open/recent decisions + helper boards
DECIDE_CONTEXT_MAX = 1500  # 0.21.0 decisions.CONTEXT_MAX: background for Delta, never shown
DECIDE_RECENT_MAX = 600    # 0.21.0 decisions.RECENT_MAX: last turn of the asking session


def _decide_text(value: str | None, flag: str, limit: int) -> str | None:
    """--context / --recent: whitespace collapsed (like the server), over `limit`: ValueError
    before anything is sent. None = not given."""
    if value is None:
        return None
    t = " ".join(str(value).split())
    if len(t) > limit:
        raise ValueError(f"decide {flag}: max {limit} characters (got {len(t)}), nothing sent")
    return t


async def _send_data(ctl: _Control, req: dict) -> dict:
    """0.21.4: agent-to-agent data (datachan.py). Bridge: POST /api/bridge/data (a large file
    first via /api/bridge/upload); WebRTC: the packet itself to the targets' identities."""
    to = str(req.get("to") or "").strip()
    kind = str(req.get("kind") or "")
    name = vdata.clean_name(req.get("name") or "")
    mime = str(req.get("mime") or "")
    payload = req.get("payload")
    if kind == "file":
        path = str(req.get("path") or "")
        try:
            size = os.path.getsize(path)
            if size > vdata.FILE_MAX_BYTES:
                return {"ok": False, "status": 413, "type": "data",
                        "error": f"file too large (max {vdata.FILE_MAX_BYTES} bytes)"}
            payload = await asyncio.to_thread(Path(path).read_bytes)
        except OSError as e:
            return {"ok": False, "type": "data", "error": f"file not readable: {type(e).__name__}"}
        name = name or vdata.clean_name(os.path.basename(path))
    room = await ctl.wait_room(10.0)
    if room is None:
        return {"ok": False, "type": "data", "error": "not connected to the room (yet)"}
    send = getattr(room, "send_data", None)
    if send is not None:  # bridge transport: the server resolves and builds
        body: dict = {"to": to, "kind": kind}
        try:
            if kind == "file":
                if vdata.fits_inline(payload, name, mime):
                    import base64
                    body["payload"] = base64.b64encode(payload).decode("ascii")
                else:
                    up = await room.upload_file(payload, name, mime)
                    body["file_id"] = up.get("file_id")
            else:
                body["payload"] = payload
            for k, v in (("name", name), ("mime", mime), ("id", req.get("id"))):
                if v:
                    body[k] = v
            out = await send(body)
        except vtransport.BridgeError as e:
            detail = (e.body or {}).get("detail") if isinstance(e.body, dict) else None
            return {"ok": False, "type": "data", "status": e.status or 503, "error": str(detail or e)}
        return {**out, "type": "data"}
    try:
        targets = vdata.resolve_targets(room.remote_participants.values(), to)
        pkt, size = vdata.build_packet(sender_identity=ctl.ident, sender_label=ctl.ident, to=to,
                                       targets=[p.identity for p in targets], kind=kind,
                                       payload=payload, name=name, mime=mime,
                                       msg_id=req.get("id") or None)
    except vdata.DataError as e:
        hint = (" WebRTC join: no file store; send a smaller file or join with "
                "--transport bridge (upload up to 10 MB)") if e.status == 413 else ""
        return {"ok": False, "type": "data", "status": e.status, "error": e.detail + hint}
    try:
        await room.local_participant.publish_data(
            json.dumps(pkt, ensure_ascii=False).encode("utf-8"), reliable=True, topic=vdata.TOPIC,
            destination_identities=[p.identity for p in targets])
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "type": "data", "status": 503, "error": f"publish failed: {type(e).__name__}"}
    return {"ok": True, "type": "data", "id": pkt["id"], "to": [vdata.label(p) for p in targets],
            "size": size}


_DELTA_RPC = "vh.delta_notes"  # deltanotes.RPC_METHOD on the worker (rules/facts for Delta)
DELTA_KINDS = {"delta-rule": "rule", "delta-note": "note"}
DELTA_TEXT_MAX = 300           # deltanotes.TEXT_MAX (server: 422 over it, never cut)


def delta_request(cmd: str, action: str, arg: str | None, persist: bool = False) -> dict:
    """`delta-rule|delta-note add TEXT | list | rm ID` -> the request for the join. Bad input:
    ValueError before anything is sent."""
    kind = DELTA_KINDS[cmd]
    req: dict = {"cmd": "delta", "kind": kind, "op": action}
    if action == "add":
        text = " ".join(str(arg or "").split())
        if text == "-":
            text = " ".join(sys.stdin.read().split())
        if not text:
            raise ValueError(f"{cmd} add needs the text")
        if len(text) > DELTA_TEXT_MAX:
            raise ValueError(f"{cmd} add: max {DELTA_TEXT_MAX} characters (got {len(text)}), nothing sent")
        req["text"] = text
        if persist:
            req["persist"] = True
    elif action == "rm":
        if not arg:
            raise ValueError(f"{cmd} rm needs the id (see {cmd} list)")
        req["id"] = str(arg).strip()
    elif arg:
        raise ValueError(f"{cmd} list takes no argument")
    return req


async def _delta_rpc(ctl: _Control, req: dict) -> dict:
    """0.23.0: rules/facts for Delta. WebRTC: vh.delta_notes RPC to the voicebot (only the
    operator itself, never with a helper `sender`); bridge: POST /api/bridge/delta."""
    body = {k: req[k] for k in ("kind", "op", "text", "id", "persist") if req.get(k) is not None}
    room = await ctl.wait_room(10.0)
    if room is None:
        return {"ok": False, "type": "delta", "error": "not connected to the room (yet)"}
    send = getattr(room, "send_delta", None)
    if send is not None:  # bridge: the server runs the RPC for us
        try:
            out = await send(body)
        except vtransport.BridgeError as e:
            detail = (e.body or {}).get("detail") if isinstance(e.body, dict) else None
            return {"ok": False, "type": "delta", "status": e.status or 503, "error": str(detail or e)}
        return {**out, "type": "delta"}
    worker = next((p.identity for p in room.remote_participants.values()
                   if _kind_label(p) == "agent"), None)
    if worker is None:
        return {"ok": False, "type": "delta", "status": 503, "error": "voicebot not in the call, try again shortly"}
    deadline = time.monotonic() + 8.0
    while True:
        try:
            raw = await room.local_participant.perform_rpc(
                destination_identity=worker, method=_DELTA_RPC,
                payload=json.dumps(body, ensure_ascii=False), response_timeout=8.0)
            out = json.loads(raw or "{}")
        except ValueError:
            return {"ok": False, "type": "delta", "status": 502, "error": "bad answer"}
        except Exception as e:  # noqa: BLE001  RPC error/timeout (old server: unsupported method)
            return {"ok": False, "type": "delta", "status": 503, "error": f"delta rpc failed: {type(e).__name__}"}
        if not isinstance(out, dict):
            return {"ok": False, "type": "delta", "status": 502, "error": "bad answer"}
        if out.get("status") != 425 or time.monotonic() >= deadline:
            if out.get("status") == 425:
                return {"ok": False, "type": "delta", "status": 503, "error": "voicebot not ready, try again shortly"}
            if not out.get("ok") and "detail" in out:
                out = {**out, "error": out.pop("detail")}
            return {**out, "type": "delta"}
        await asyncio.sleep(0.25)


async def _list_rpc(ctl: _Control) -> dict:
    """Router-Fund 21.10.: the call's decisions and helper boards from the server, read-only: WebRTC =
    RPC vh.decisions to the voicebot, bridge = GET /api/bridge/decisions. Never logs content."""
    room = await ctl.wait_room(10.0)
    if room is None:
        return {"ok": False, "error": "not connected to the room (yet)"}
    list_decisions = getattr(room, "list_decisions", None)
    if list_decisions is not None:  # bridge
        try:
            return await list_decisions()
        except vtransport.BridgeError as e:
            return {"ok": False, "status": e.status or 503, "error": str(e)}
    worker = next((p.identity for p in room.remote_participants.values()
                   if _kind_label(p) == "agent"), None)
    if worker is None:
        return {"ok": False, "status": 503, "error": "voicebot not in the call, try again shortly"}
    deadline = time.monotonic() + 8.0
    while True:
        try:
            raw = await room.local_participant.perform_rpc(
                destination_identity=worker, method=_LIST_RPC, payload="{}", response_timeout=8.0)
            out = json.loads(raw or "{}")
        except Exception as e:  # noqa: BLE001  RPC error/timeout/bad JSON: only the class
            return {"ok": False, "status": 503, "error": f"decisions rpc failed: {type(e).__name__}"}
        if not isinstance(out, dict):
            return {"ok": False, "status": 502, "error": "bad answer"}
        if out.get("status") != 425 or time.monotonic() >= deadline:
            return out
        await asyncio.sleep(0.25)


async def _decide_rpc(ctl: _Control, req: dict) -> dict:
    """vh.decide RPC to the voicebot (own decide, and 0.16.0 helper requests with sender;
    a helper request never touches the idle watchdog). 0.21.0: context/recent ride along;
    bridge transport: POST /api/bridge/decide instead of the RPC."""
    body = {k: v for k, v in (
        ("question", req.get("question")), ("options", req.get("options") or None),
        ("recommend", req.get("recommend")), ("id", req.get("id")),
        ("delete", True if req.get("delete") else None),
        ("context", req.get("context")), ("recent", req.get("recent")),
        ("faq", req.get("faq") or None)) if v is not None}  # 0.21.4 faq: hidden, for Delta
    if req.get("sender"):  # 0.16.0: a helper session's question (Vertrag A)
        body["sender"] = str(req["sender"])
    room = await ctl.wait_room(10.0)
    if room is None:
        return {"ok": False, "error": "not connected to the room (yet)"}
    send_decide = getattr(room, "send_decide", None)
    if send_decide is not None:  # bridge: the server runs the RPC for us
        try:
            out = await send_decide(body)
        except vtransport.BridgeError as e:
            detail = (e.body or {}).get("detail") if isinstance(e.body, dict) else None
            return {"ok": False, "status": e.status or 503, "error": str(detail or e)}
        return {**out, "type": "decide"} if "type" not in out else out
    worker = next((p.identity for p in room.remote_participants.values()
                   if _kind_label(p) == "agent"), None)
    if worker is None:
        return {"ok": False, "status": 503, "error": "voicebot not in the call, try again shortly"}
    deadline = time.monotonic() + 8.0  # decisions.DECIDE_READY_S: 425 right after join
    while True:
        try:
            raw = await room.local_participant.perform_rpc(
                destination_identity=worker, method=_DECIDE_RPC,
                payload=json.dumps(body, ensure_ascii=False), response_timeout=8.0)
        except Exception as e:  # noqa: BLE001  RPC error/timeout: only the class
            return {"ok": False, "status": 503, "error": f"decide rpc failed: {type(e).__name__}"}
        try:
            out = json.loads(raw or "{}")
        except ValueError:
            return {"ok": False, "status": 502, "error": "bad answer"}
        if not isinstance(out, dict):
            return {"ok": False, "status": 502, "error": "bad answer"}
        if out.get("status") != 425 or time.monotonic() >= deadline:
            if out.get("status") == 425:
                return {"ok": False, "status": 503, "error": "voicebot not ready, try again shortly"}
            return {**out, "type": "decide"} if "type" not in out else out
        await asyncio.sleep(0.25)


async def _control_handler(ctl: _Control, req: dict) -> dict:
    """One request from `voicehook-agent say|next|leave|status|activity`."""
    cmd = req.get("cmd")
    if ctl.onboarding is not None and cmd in ("say", "next"):
        # 0.21.8: the first say/next arms the spool; the onboarding is its first event, so an
        # agent that never reads the join's stdout (--json into a file) still gets it
        ev, ctl.onboarding = ctl.onboarding, None
        ctl.events.arm()
        ctl.events.put_nowait(ev)
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
        try:  # shape / emotion for the ring, checked again (stdin-free path, same rules)
            if req.get("shape") is not None:
                extra["shape"] = vvisual.validate_shape(req["shape"])
            if req.get("emotion") is not None:
                extra["emotion"] = vvisual.validate_emotion(req["emotion"])
        except vvisual.VisualError as e:
            return {"ok": False, "type": "invalid", "error": str(e)}
        nudge = ctl.clock.said(text=text)  # 0.9.0: progress said, board older -> hint
        room = await ctl.wait_room(10.0)
        if room is None:
            return {"ok": False, "error": "not connected to the room (yet)"}
        res = await _publish_say(room, text, extra, ctl.say_tracker, ctl.echo)
        if res.get("ok"):
            ctl.say_status.sent(res["seq"], text)
            ctl.last_say_at = time.time()  # 0.21.7 midturn: answered since the user spoke?
        return {**res, **nudge} if res.get("ok") else res
    if cmd == "visual":
        ctl.watchdog.touch()
        try:
            shape = vvisual.validate_shape(req.get("shape"))
            emo = req.get("emotion")
            emo = vvisual.validate_emotion(emo) if emo is not None else None
        except vvisual.VisualError as e:
            return {"ok": False, "type": "invalid", "error": str(e)}
        # the server draws one shape per 2 s per operator; the data channel drops the
        # rest silently, so refuse here with the same message the bridge gives (429)
        now = time.monotonic()
        last = getattr(ctl, "visual_at", None)
        if last is not None and now - last < vvisual.RATE_S:
            return {"ok": False, "type": "rate_limited", "retry_after_s":
                    round(vvisual.RATE_S - (now - last), 1), "error": VISUAL_RATE_ERROR}
        room = await ctl.wait_room(10.0)
        if room is None:
            return {"ok": False, "error": "not connected to the room (yet)"}
        res = await _publish_visual(room, vvisual.payload(shape, emo))
        if res.get("ok"):
            ctl.visual_at = now
        return res
    if cmd == "card":
        ctl.watchdog.touch()
        try:  # same rules as the client, checked again (stdin-free path)
            body = vcards.validate({k: req.get(k) for k in ("kind", "title", "text", "image", "panel",
                                                            "large") if req.get(k) is not None})
        except vcards.CardError as e:
            return {"ok": False, "type": "invalid", "error": str(e)}
        room = await ctl.wait_room(10.0)
        if room is None:
            return {"ok": False, "error": "not connected to the room (yet)"}
        return await _publish_card(ctl, room, body)
    if cmd == "ui":
        # 0.21.0: window/layout/move/focus on the humans' call page (checked again here)
        ctl.watchdog.touch()
        try:
            body = vui.validate(req.get("body"))
        except vui.UiError as e:
            return {"ok": False, "type": "invalid", "error": str(e)}
        room = await ctl.wait_room(10.0)
        if room is None:
            return {"ok": False, "error": "not connected to the room (yet)"}
        return await _publish_ui(room, body)
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
        ctl.ledger.board("", board, own=True)
        return {"ok": True, "type": "board", "board": board}
    if cmd == "decide":
        # Decision Board: create/update/delete. Unlike say/status this is a LiveKit RPC
        # to the voicebot (worker, method vh.decide), mirroring Session.decide in the
        # bridge; the result arrives later as a `next` event type "decision".
        ctl.watchdog.touch()
        out = await _decide_rpc(ctl, req)
        ctl.ledger.created(req, out)  # 0.21.4 `board`
        return out
    if cmd == "delta":  # 0.23.0 `voicehook-agent delta-rule|delta-note` (rules/facts for Delta)
        ctl.watchdog.touch()
        return await _delta_rpc(ctl, req)
    if cmd == "data":  # 0.21.4 `voicehook-agent send-data` (datachan.py)
        ctl.watchdog.touch()
        return await _send_data(ctl, req)
    if cmd == "board_list":  # 0.21.4 `voicehook-agent board` / `decide --list`
        # Router-Fund 21.10.: the server's listing (all helpers of the room) merged with our own ledger
        return ctl.ledger.merged(await _list_rpc(ctl), only_open=bool(req.get("open")))
    if cmd == "vocab":
        # 0.21.0: teach the call's speech recognition (server adds to the payer's account)
        ctl.watchdog.touch()
        return await _vocab_add(ctl, {"terms": req.get("terms") or [], "fixes": req.get("fixes") or []})
    if cmd in PHONE_KINDS:
        # notify-me / call-me: SMS with a single-use link / phone call to the human (server rules)
        try:
            body = phone_request(cmd, str(req.get("reason") or ""), req.get("message"))
        except ValueError as e:
            return {"ok": False, "type": "invalid", "error": str(e)}
        body.pop("cmd")
        ctl.watchdog.enter()   # call_me blocks while the phone rings: counts as alive
        try:
            return await _phone_post(ctl, cmd, body)
        finally:
            ctl.watchdog.leave()
    if cmd == "helper_credential":
        # 0.17.0: a helper credential for another machine (pipe it into ssh, never print it
        # into a chat): WebRTC via the server's helper hub, bridge via the bridge session
        label = str(req.get("label") or vmulti.machine_label())
        rel = None
        if ctl.helper_pump is not None and ctl.helper_http is not None:
            rel = await ctl.helper_pump.credential(ctl.helper_http, label, bool(req.get("rotate")))
        elif ctl.room is not None and hasattr(ctl.room, "mint_relay") and not req.get("rotate"):
            rel = await ctl.room.mint_relay(label)
        if rel is None:
            return {"ok": False, "error": "no helper credential available (server too old, or "
                                          "rotate needs a WebRTC join)"}
        return {"ok": True, "type": "helper_credential", "token": rel["token"], "url": rel["url"],
                "expires_in": rel.get("expires_in"), "label": label}
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
        listen = timeout > 0  # 0.21.4 listen guard: a waiting `next` = a consumer waits
        if listen:
            ctl.listening += 1
        seq = None
        try:
            if req.get("lease"):  # 0.21.5: handed out until the consumer acks it (spool.py)
                got = await ctl.events.lease(timeout)
                ev = None if got is None else got[1]
                seq = None if got is None else got[0]
            else:  # older clients: consumed at once
                ev = await ctl.events.get(timeout)
        finally:
            ctl.watchdog.leave()
            if listen:
                ctl.listening = max(0, ctl.listening - 1)
                ctl.listen_end = time.monotonic()
        hints = ctl.clock.hints()
        if ev is None or ev.get("type") != "ended":
            hints.update(ctl.agent_said.take())
            hints.update(ctl.say_status.take())
            hints.update(_activity_hints(ctl))
            stale = _stale_error(ctl)
            if stale:
                hints["stale_error"] = stale
            hints.update(_now_view(ctl))
        hints["human_state"] = dict(ctl.human_state)  # 0.21.0 Silent Mode, on every next
        if ev is None:
            return {"ok": True, "type": "timeout", "pending": 0, **hints}
        if ev.get("type") == "user":
            ctl.clock.delivered()
        out = {"ok": True, **ev, "pending": len(ctl.events), **hints}
        if ev.get("stale") is True and "hint" in ev:
            # 0.21.9: the stale event's own hint wins; the board hint moves to status_hint
            if "hint" in hints and hints["hint"] != ev["hint"]:
                out["status_hint"] = hints["hint"]
            out["hint"] = ev["hint"]
        if seq is not None:
            out["seq"] = seq  # 0.21.5: the consumer sends {"cmd":"ack","seq":N} once it has it
        return out
    if cmd == "task_wake":  # 0.24.0 (a2a.py): a2a `send` nudged this join; drain its inbox and
        # enqueue the pending tasks as front events so its `next`/`listen` wakes at once. The
        # durable copy stays the inbox until it is consumed here; never logs the task text.
        ctl.watchdog.touch()
        ctl.events.arm()
        label = va2a._clean_label(req.get("label"))
        n = _drain_tasks_into_spool(ctl, label)
        return {"ok": True, "type": "task_wake", "queued": n}
    if cmd == "take_wake":  # 0.21.7 (midturn.py): the hook shows them inside the running turn
        types = [t for t in (req.get("types") or vmidturn.TYPES) if t in vmidturn.TYPES]
        try:
            n = max(1, min(10, int(req.get("max") or 5)))
        except (TypeError, ValueError):
            n = 5
        evs = ctl.events.take(types, n)
        return {"ok": True, "type": "take_wake", "events": evs, "now": time.time(),
                "last_say_at": ctl.last_say_at, "pending": len(ctl.events)}
    if cmd == "ack":  # 0.21.5: the consumer printed the event, never hand it out again
        acked = ctl.events.ack(req.get("seq"))
        return {"ok": True, "type": "ack", "seq": req.get("seq"), "acked": acked,
                "pending": len(ctl.events)}
    if cmd == "rejoin":
        # 0.25.0: re-exec this join in place (same self-update mechanism, os.execve) so identity
        # (slug/label/model), spool/queue and the status board survive, instead of leaving and
        # re-joining. Default: a rejoin mid-call is allowed, with a visible hint that there may be
        # a short audio gap while the process swaps. The handler only flags it and ends the cycle
        # cleanly; _join turns that into RC_RESTART (restart with the same --identity).
        ctl.watchdog.touch()
        ctl.rejoining = True
        gap = bool(ctl.room is not None)  # connected -> there may be a short audio gap
        ctl.quit.set()  # like leave: the reply is written first, then the cycle ends -> RC_RESTART
        return {"ok": True, "type": "rejoining", "identity": ctl.ident, "room": ctl.slug,
                "in_call": gap,
                "note": ("rejoining in place: identity, queue and status board are kept"
                         + ("; there may be a short audio gap while the process restarts" if gap else ""))}
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
    if cmd == "activity":
        # 0.13.0: manual line for agents without the hook; the join owns the log
        try:
            line = vactivity.note_line(str(req.get("text") or ""))
            project = vui.project_field(req.get("project"))
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        if ctl.activity_path is None:
            return {"ok": False, "error": "activity log not available in this join"}
        if project is not None:  # 0.21.0: field project on this and every later operator.activity
            ctl.activity_project = project
        try:
            vactivity.append_line(ctl.activity_path.parent, line)
        except OSError as e:
            return {"ok": False, "error": f"activity log not writable: {e!r}"}
        meta = vrelay_meta(req)
        if meta:  # 0.21.4: hidden {context, faq} of this line, sent with operator.activity
            ctl.activity_meta[line] = meta
            while len(ctl.activity_meta) > ACTIVITY_META_KEEP:
                ctl.activity_meta.pop(next(iter(ctl.activity_meta)))
        out = {"ok": True, "type": "activity", "line": line}
        if ctl.activity_project:
            out["project"] = ctl.activity_project
        return out
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
                "cli_version": _VERSION,  # 0.25.0: version of the running join (stale-join check)
                "connected": room is not None, "pending": len(ctl.events),
                "idle_s": round(ctl.watchdog.idle_for(), 1),
                # 0.21.5 (listenguard.py): listening = the join + its spool hold the call
                # (true from the first say/next on, also while you work); consumers = `next`
                # / `listen` requests waiting right now; unread / unread_wake = spool events
                # not acked yet (all / the ones that need you); last_consumed_s = seconds
                # since the last ack (null: none yet); listen_idle_s = since the last `next`
                # ended (null: none yet)
                "listening": _spool_listening(ctl), "consumers": ctl.listening,
                "unread": ctl.events.unread(), "unread_wake": ctl.events.unread_wake(),
                "last_consumed_s": ctl.events.last_consumed_s(),
                "listen_idle_s": _listen_idle(ctl),
                # 0.21.6 (waitcheck.py): own board text that waits for the human (null: none,
                # or shown with status --waiting) and the open own decisions
                **_waiting_view(ctl),
                "leaving": ctl.leaving,
                "idle_timeout_s": ctl.watchdog.timeout,
                "peers": sorted(peers, key=lambda d: d["identity"])}
    return {"ok": False, "error": f"unknown cmd {cmd!r}"}


def _waiting_view(ctl: _Control) -> dict:
    own = ctl.ledger.own_board or {}
    doing = own.get("doing") if isinstance(own.get("doing"), str) else ""
    waits = bool(doing) and own.get("state") != vwait.STATE and bool(vwait.waiting(doing))
    opened = [it for it in ctl.ledger.listing(only_open=True).get("open") or []
              if it.get("from") == "self"]
    return {"board_waiting": doing[:200] if waits else None, "open_decisions": len(opened)}


def _spool_listening(ctl: _Control) -> bool:
    """0.21.5: the join itself listens for the agent: connected, spool armed and open."""
    return (ctl.room is not None and bool(ctl.events.armed) and not ctl.events.closed
            and not ctl.leaving)


def _listen_idle(ctl: _Control) -> float | None:
    if ctl.listening:
        return 0.0
    if ctl.listen_end is None:
        return None
    return round(max(0.0, time.monotonic() - ctl.listen_end), 1)


async def _publish_alive(ctl: _Control, alive: bool) -> bool:
    """One `operator.alive` packet; best effort (never raises)."""
    room = ctl.room
    if room is None:
        return False
    consumer_idle = round(ctl.watchdog.idle_for(), 1)
    # 0.21.5: while the join's spool listens, the join itself is "in next" for the server
    # (idle_s 0); consumer_idle_s = how long the agent has not served say/next (info only)
    payload = {"alive": alive, "ts": time.time(),
               "idle_s": 0.0 if alive and _spool_listening(ctl) else consumer_idle,
               "consumer_idle_s": consumer_idle}
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
    back. Silent while orphaned (the UI dims the chip after ~20 s).
    0.21.5: also the whole time the join's spool listens (_spool_listening): the agent
    stays present while it works between two `next`; the orphan guards still count
    only the agent's own say/next (watchdog)."""
    tick = max(0.05, min(1.0, ctl.alive_interval / 4))
    last_sent = 0.0
    was_active = False
    while not ctl.quit.is_set():
        active = ctl.room is not None and (_spool_listening(ctl)
                                           or ctl.watchdog.idle_for() < ctl.alive_window)
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


def _activity_hints(ctl: _Control) -> dict:
    """0.13.0: activity_due keys for `next` (never raises)."""
    try:
        now = ctl.activity_clock()
        if ctl.activity_path is not None:
            ctl.activity_due.observe(vactivity.read_tail(ctl.activity_path, ctl.activity_lines), now)
        return ctl.activity_due.check(now, ctl.clock.working())
    except Exception as e:  # noqa: BLE001
        print(f"[warn] activity_due: {e!r}", file=sys.stderr, flush=True)
        return {}


def _stale_error(ctl: _Control) -> dict:
    """0.13.0: stale_error payload for `next` / the plain stream, or {} (never raises).
    No rate limit: every call while stale returns it."""
    try:
        s = ctl.stale_s
        if s <= 0 or not ctl.clock.active(stale_s=s):
            return {}
        st = ctl.clock.status_age()
        now = ctl.activity_clock()
        act = None
        if not ctl.activity_due.hook_active(now):  # hook fills the log: only the board counts
            a = ctl.activity_due.age(now)
            act = a if a > s else None
        return relay.stale_error_payload(st if st > s else None, act)
    except Exception as e:  # noqa: BLE001
        print(f"[warn] stale_error: {e!r}", file=sys.stderr, flush=True)
        return {}


def _now_view(ctl: _Control) -> dict:
    """0.13.0: what Delta currently knows about you: activity_now {text, age_s} (the
    newest line last published as operator.activity, without its time) and board_now
    {doing, age_s}; null when nothing was sent. Same shape as the server's bridge `next`."""
    act = None
    if ctl.activity_sent is not None:
        line, at = ctl.activity_sent
        text = re.sub(r"^\d\d:\d\d:\d\d ", "", line)
        act = {"text": text, "age_s": round(ctl.activity_clock() - at, 1)}
    return {"activity_now": act, "board_now": ctl.clock.board_now()}


def _print_stale(ctl: _Control, json_mode: bool) -> None:
    """Plain (non --json) join output: `!! stale: <message>` while stale."""
    if json_mode:
        return
    err = _stale_error(ctl)
    if err:
        print(f"!! stale: {err['message']}", flush=True)


async def _publish_activity(ctl: _Control, lines: list[str]) -> bool:
    """One `operator.activity` packet {lines, ts}; best effort (never raises)."""
    room = ctl.room
    if room is None:
        return False
    payload: dict = {"lines": lines, "ts": time.time()}
    meta = [ctl.activity_meta.get(ln) or {} for ln in lines]
    if any(meta):  # 0.21.4: per line {context, faq}, aligned with lines
        payload["meta"] = meta
    if ctl.activity_project:  # 0.21.0 `activity --project`: overrides the derived project tag
        payload["project"] = ctl.activity_project
    try:
        await room.local_participant.publish_data(
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            reliable=True, topic=vactivity.TOPIC)
        own = [ln for ln in lines if not vactivity.is_relay(ln)]  # 0.15.0: not other sessions
        if own:
            ctl.activity_sent = (own[-1], ctl.activity_clock())  # 0.13.0 activity_now
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
            ctl.activity_due.observe(lines, now)  # 0.13.0 activity_due
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


def _route_helper_decision(ctl: _Control, payload: dict) -> bool:
    """0.16.0: a decision event whose `sender` names a helper session of this machine
    (local relay) goes into that helper's answer file, not into our own `next`."""
    snd = str(payload.get("sender") or "")
    frm = ctl.relay_senders.get(snd)
    if not frm or ctl.activity_path is None:
        return False
    try:
        vrelayops.append_jsonl(vrelayops.answer_path(ctl.activity_path.parent, frm), payload)
    except (OSError, vrelayops.RelayError) as e:
        print(f"[warn] relay answer not written ({type(e).__name__})", file=sys.stderr, flush=True)
    # 0.18.1: a copy for our own `next`, so we can wake the (often idle) helper session
    label = snd[: -len(frm) - 2] if snd.endswith(": " + frm) else ""
    ctl.events.put_nowait(relay.helper_decision_event(
        payload, frm=frm, label=label, question=ctl.relay_questions.get(str(payload.get("id") or ""), "")))
    return True


def _route_helper_user_turn(ctl: _Control, payload: dict, own: set[str]) -> bool:
    """0.22.0 (server 21.10.2026): a user turn addressed to a helper session by name
    (to_kind helper, to_op = us). A helper of this machine (to_key = our sender for it) gets
    it in its answer file (local relay: its `relay-next`, MCP `next` and hooks bring it);
    otherwise the server hub's inbox (POST /api/helper/pump/user_turn). Never logs content."""
    w = relay.wake_info(payload)
    if payload.get("role") != "user" or (payload.get("to_kind") != "helper"
                                         and (w or {}).get("scope") != "helper"):
        return False
    to = payload.get("to") if isinstance(payload.get("to"), list) else []
    key = str(payload.get("to_key") or "")
    if w is not None:
        # 0.24.0 (server #606): only scope helper, we forward (ids = [to_op]), the helper
        # named in wake.helper; nobody else is woken
        if w["scope"] != "helper" or not w["helper"] or not (set(w["ids"]) & own
                                                             or str(payload.get("to_op") or "") in own):
            return False
        if key and key != w["helper"]:
            return False
        key = w["helper"]
        if not to and ": " in key:
            to = [key.split(": ", 1)[1]]  # the helper's relay name (from)
    elif str(payload.get("to_op") or "") not in own:
        return False
    text = " ".join(str(payload.get("text") or "").split())
    if len(to) != 1 or not text:
        return False
    name = str(to[0])[:80]
    speaker = str(payload.get("speaker") or "")
    frm = key.split(": ", 1)[1] if ": " in key else ""
    if frm and ctl.activity_path is not None and key == vrelayops.sender_for(frm):
        try:
            vrelayops.append_jsonl(vrelayops.answer_path(ctl.activity_path.parent, frm),
                                   relay.helper_user_turn_event(text, name, speaker))
            return True
        except (OSError, vrelayops.RelayError) as e:
            print(f"[warn] relay user turn not written ({type(e).__name__})", file=sys.stderr, flush=True)
    if ctl.helper_pump is not None and ctl.helper_http is not None:
        asyncio.ensure_future(ctl.helper_pump.user_turn(ctl.helper_http, name, key, text, speaker))
        return True
    return False


def _drain_tasks_into_spool(ctl: _Control, label: str) -> int:
    """0.24.0: take the pending `task` events out of `label`'s inbox and put them as front
    events into this join's spool (so `next`/`listen` wakes at once), acking each back to its
    sender. Returns how many were queued. Never raises, never logs the task text."""
    if not label:
        return 0
    try:
        tasks = vrelayops.take_jsonl(vrelayops.inbox_path(label),
                                     lambda o: o.get("type") == va2a.TASK)
    except (OSError, vrelayops.RelayError):
        return 0
    n = 0
    for raw in tasks:
        ev = va2a.valid_task(raw)
        if ev is None:
            continue
        ctl.events.put_front_nowait(ev)
        _ack_task_back(label, ev)
        n += 1
    return n


def _route_question_check(ctl: _Control, payload: dict, transport: str) -> bool:
    """0.22.1 (Live-Bug 21.10.2026): the server's question_check (a board/feed line sounds like
    a question; ask it with decide or confirm no_question, else the server puts it on the board
    itself after wait_s). With `sender` it belongs to a HELPER: local relay -> its answer file,
    WebRTC hub -> the server inbox (pump/answer); bridge: the server already did that. Without
    `sender` it is ours: own `next`. Before, the join dropped it, the helper never knew, and
    the automatic decision later looked like someone else's question."""
    ev = {k: payload[k] for k in ("type", "id", "quote", "kind", "wait_s", "text", "sender") if k in payload}
    snd = str(payload.get("sender") or "")
    if not snd:
        ctl.events.put_nowait({**ev, "ts": time.time()})
        return True
    if transport == "bridge":
        return False
    frm = ctl.relay_senders.get(snd)
    if frm and ctl.activity_path is not None:
        try:
            vrelayops.append_jsonl(vrelayops.answer_path(ctl.activity_path.parent, frm), ev)
            return True
        except (OSError, vrelayops.RelayError) as e:
            print(f"[warn] relay question_check not written ({type(e).__name__})", file=sys.stderr, flush=True)
            return False
    if ctl.helper_pump is not None and ctl.helper_http is not None:
        t = asyncio.ensure_future(ctl.helper_pump.answer(ctl.helper_http, snd, ev))
        _BG_TASKS.add(t)
        t.add_done_callback(_BG_TASKS.discard)
        return True
    return False


def _route_helper_stale(ctl: _Control, ev: dict) -> bool:
    """0.21.9: a stale event whose `sender` names a local helper session also goes into that
    helper's answer file (its `relay-next` / MCP `next` brings it); our own `next` keeps it."""
    snd = str(ev.get("sender") or "")
    frm = ctl.relay_senders.get(snd)
    if not frm or ctl.activity_path is None:
        return False
    try:
        vrelayops.append_jsonl(vrelayops.answer_path(ctl.activity_path.parent, frm), ev)
    except (OSError, vrelayops.RelayError) as e:
        print(f"[warn] relay stale event not written ({type(e).__name__})", file=sys.stderr, flush=True)
        return False
    return True


_RELAY_QUESTIONS_MAX = 200


def _note_relay_question(ctl: _Control, body: dict, out: dict) -> None:
    """0.18.1: remember a local helper's question by decision id (for helper_decision)."""
    did = str(out.get("id") or body.get("id") or "")
    if not did or not out.get("ok"):
        return
    if out.get("deleted"):
        ctl.relay_questions.pop(did, None)
        return
    if isinstance(body.get("question"), str):
        ctl.relay_questions.pop(did, None)
        ctl.relay_questions[did] = " ".join(body["question"].split())
        while len(ctl.relay_questions) > _RELAY_QUESTIONS_MAX:
            ctl.relay_questions.pop(next(iter(ctl.relay_questions)))


async def _answer_helper(ctl: _Control, sender: str, payload: dict) -> None:
    """0.17.0: the human's answer to a helper question goes to the server hub (the helper's
    inbox). 0.18.1: the server returns a copy (helper_decision) for our own `next`."""
    out = await ctl.helper_pump.answer(ctl.helper_http, sender, payload)
    hd = out.get("helper_decision") if isinstance(out, dict) else None
    ctl.events.put_nowait(relay.helper_decision_event(hd if isinstance(hd, dict) else payload))


async def _serve_relay_request(ctl: _Control, dir_: Path, obj: dict) -> None:
    """0.16.0 (local relay, Vertrag A): one helper request -> decide RPC with `sender` or
    operator.status with `sub`; the result goes to the helper's answer file."""
    parsed = vrelayops.parse_request(obj)
    if parsed is None:
        return
    rid, frm, op, body = parsed
    sender = vrelayops.sender_for(frm)
    if body is None:
        res: dict = {"ok": False, "status": 422, "error": "invalid relay request"}
    elif op == "card":  # 0.23.1: text/code/link card of a local helper, with its sender
        room = await ctl.wait_room(10.0)
        pkt = vcards.relay_card({**body, "id": secrets.token_hex(8), "from": sender},
                                ctl.card_seq + 1) if room is not None else None
        if room is None:
            res = {"ok": False, "status": 503, "error": "not connected to the room (yet)"}
        elif pkt is None:
            res = {"ok": False, "status": 422, "error": "invalid card"}
        else:
            await room.local_participant.publish_data(json.dumps(pkt, ensure_ascii=False).encode("utf-8"),
                                                      reliable=True, topic=vcards.TOPIC)
            ctl.card_seq += 1
            res = {"ok": True, "id": pkt["id"], "kind": pkt["kind"]}
    elif op == "decide":
        ctl.relay_senders[sender] = frm  # before the RPC: an answer can be quick
        out = await _decide_rpc(ctl, {**body, "sender": sender})
        _note_relay_question(ctl, body, out)
        ctl.ledger.created(body, out, frm=sender)
        res = {k: v for k, v in out.items() if k != "type"}
    else:
        room = await ctl.wait_room(10.0)
        if room is None:
            res = {"ok": False, "status": 503, "error": "not connected to the room (yet)"}
        else:
            ctl.relay_senders[sender] = frm  # 0.22.1: a board-only helper gets its question_check
            helper = ctl.relay_helpers.setdefault(sender, {})
            if op == "state":  # 0.21.4: the helper's session state from its hooks
                helper["state"] = body["state"]
                payload = {**(helper.get("board") or {"doing": "", "open": [], "done": []})}
            else:
                helper["board"] = dict(body)
                payload = dict(body)
            if helper.get("state"):
                payload["state"] = helper["state"]
            data = json.dumps({**payload, "sub": sender}, ensure_ascii=False).encode("utf-8")
            await room.local_participant.publish_data(data, reliable=True, topic="operator.status")
            ledger = getattr(ctl, "ledger", None)  # `board` (Router-Fund 21.10.)
            if ledger is not None:
                ledger.board(ctl.ident, {**payload, "sub": sender})
            res = {"ok": True}
    vrelayops.append_jsonl(vrelayops.answer_path(dir_, frm), {"type": "result", "rid": rid, **res})


async def _relay_request_loop(ctl: _Control, dir_: Path) -> None:
    """0.16.0: serve helper requests from <join dir>/relay-req.jsonl. Never crashes the
    join; logs only the error class (never a question or board text)."""
    while not ctl.quit.is_set():
        try:
            for obj in vrelayops.drain_requests(dir_):
                try:
                    await _serve_relay_request(ctl, dir_, obj)
                except Exception as e:  # noqa: BLE001
                    print(f"[warn] relay request failed ({type(e).__name__})", file=sys.stderr, flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[warn] relay request loop ({type(e).__name__})", file=sys.stderr, flush=True)
        try:
            await asyncio.wait_for(ctl.quit.wait(), timeout=vrelayops.POLL_S)
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

    def __init__(self, opt_out: bool = False, *, env=None, runner=None,
                 json_mode: bool = False) -> None:
        self.env = os.environ if env is None else env
        self.json_mode = json_mode  # 0.14.2: version notices also as a --json _meta event
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
        if vselfupdate.is_newer(latest, _VERSION):  # 0.14.2: always visible, once per join
            cmds = vselfupdate.update_commands()
            _emit_version_notice(
                self.json_mode, "update",
                f"voicehook-agent {latest} is available (this is {_VERSION}). {_update_text(cmds)}",
                latest=latest, commands=cmds)
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
        cmds = vselfupdate.update_commands()  # 0.14.2: PyPI commands, this install first
        _emit_version_notice(
            self.json_mode, "outdated",
            f"voicehook-agent {_VERSION} is too old for this server (min {lo}, latest {hi}). "
            f"{_update_text(cmds)}", cli_min=lo, latest=hi, commands=cmds,
            exit_code=vselfupdate.EXIT_OUTDATED)
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
    share_hint: dict | None = None,
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
        if ctl is not None:  # 0.21.0: image cards (POST /api/card/image), newest token per cycle
            ctl.api_base, ctl.lk_jwt = api_base, str(tok.get("token") or "")
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
                    hf = relay.helper_turn_fields(payload)
                    if hf:  # 0.22.0: the human spoke to a helper by name: info copy for us
                        ev.update(hf)
                    if hf or (relay.wake_info(payload) or {}).get("scope") == "helper":
                        _route_helper_user_turn(ctl, payload, _own_ids())
                    # 0.24.0 (server #606): wake our LLM only if `wake` names us (scope !=
                    # helper); otherwise keep the turn for the next woken event (since_last).
                    # 0.24.1 data-channel mode: wake only on a turn explicitly addressed to us
                    # (name/mention or routed_by_delta); a broad turn is context only.
                    woken = (relay.wakes_data_channel(payload, _own_ids()) if ctl.data_channel
                             else relay.wakes_main(payload, _own_ids()))
                    if woken:
                        ctl.events.put_nowait(ev)
                        ctl.clock.user()
                        _print_stale(ctl, json_mode)  # 0.13.0
                    elif ctl.events.armed:
                        ctl.quiet.add(ev, (relay.wake_info(payload) or {}).get("scope", ""))
            # #9 — a fresh user turn supersedes any older queued say.
            if role == "user":
                say_tracker.note_user_turn()
                turn_event.set()  # graph-per-turn: refresh live context
            # #12 — wake on finalized user turns (deduped, role-filtered).
            decision = notifier.consider(role, text, payload, room=slug)
            _woken_dc = (relay.wakes_data_channel(payload, _own_ids()) if (ctl is not None and ctl.data_channel)
                         else relay.wakes_main(payload, _own_ids()))
            if role == "user" and not _woken_dc:
                pass  # 0.24.0/0.24.1: not meant for us, no wake line / webhook
            elif decision.wake and decision.payload is not None:
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
        elif topic == vcards.ACK_TOPIC:
            # 0.23.1: a call page loaded (or failed to load) one of our image cards
            if ctl is not None and pkt.participant is not None:
                ctl.card_acks.note(payload, getattr(pkt.participant, "identity", ""))
        elif topic == vdata.TOPIC:
            # 0.21.4 agent-to-agent data: a `next` event (never printed with its content)
            ev = vdata.receive_event(ctl.ident if ctl is not None else "", pkt.participant, payload)
            if ev is not None:
                if ctl is not None:
                    ctl.events.put_nowait(ev)
                _print_event(json_mode, "system", ev["summary"], topic=topic,
                             **{k: ev[k] for k in ("from", "kind", "id", "name", "size") if k in ev})
        elif topic.startswith("operator."):
            if topic in ("operator.revise", "operator.say_status") and relay.foreign_owner(
                    payload, _own_ids()):
                return  # 0.11.0: belongs to another operator (server routes per owner)
            if topic == "operator.revise" and ctl is not None:
                ev = relay.revise_event(payload)
                if ev.get("stale"):
                    # 0.21.9: one clear line on stdout (count/age only, never the texts);
                    # the texts go to the agent via next/listen and the mid-turn hook
                    _emit_meta(relay.stale_meta(ev), stale=True, n=len(ev.get("items") or []))
                    _route_helper_stale(ctl, ev)
                ctl.events.put_nowait(ev)
            elif topic == "operator.say_status":
                if ctl is not None:
                    ctl.say_status.update(payload)
            elif topic == "operator.status_request" and ctl is not None:
                # 0.9.0: first in line for `next`, status_due until the next board
                ctl.events.put_front_nowait(relay.status_request_event(payload))
                ctl.clock.requested()
            if ctl is not None and payload.get("type") in ("decision", "helper_decision") \
                    and topic in ("operator.decision", "operator.helper_decision"):
                ctl.ledger.answered(payload, str(payload.get("sender") or payload.get("label")
                                                 or ""))  # 0.21.4 `board`
            if topic == "operator.status" and ctl is not None and pkt.participant is not None:
                ctl.ledger.board(getattr(pkt.participant, "identity", "?"), payload)
            elif (topic == "operator.decision" and ctl is not None
                    and payload.get("type") == "decision"
                    and not _route_helper_decision(ctl, payload)):
                # Decision Board: the worker sends the result only to the creator
                # (destination = our identity), so no owner filter is needed. 0.17.0: a
                # helper's question via the server hub (sender) goes back to it, never into our
                # next as `decision`. 0.18.1: we get a copy as `helper_decision` instead.
                snd = payload.get("sender")
                if not (isinstance(snd, str) and snd):
                    ctl.events.put_nowait(payload)
                elif transport == "bridge":
                    pass  # the server fills the helper's inbox and sends operator.helper_decision
                elif ctl.helper_pump is not None and ctl.helper_http is not None:
                    t = asyncio.ensure_future(_answer_helper(ctl, snd, payload))
                    _BG_TASKS.add(t)
                    t.add_done_callback(_BG_TASKS.discard)
                else:
                    ctl.events.put_nowait(relay.helper_decision_event(payload))
            elif (topic == "operator.question_check" and ctl is not None
                    and payload.get("type") == "question_check"
                    and getattr(pkt.participant, "kind", None) == 4):
                # 0.22.1: only from the worker (agent kind), like the server bridge
                _route_question_check(ctl, payload, transport)
            elif (topic == "operator.helper_decision" and ctl is not None
                    and payload.get("type") == "helper_decision"):
                # 0.18.1, bridge transport: the server's copy of a helper's answer
                ctl.events.put_nowait(relay.helper_decision_event(payload))
            sender = getattr(pkt.participant, "identity", "?") if pkt.participant else "?"
            line = payload.get("text", "")
            if topic == "operator.say_status":
                line = f"seq={payload.get('seq')} state={payload.get('state')}"
            elif topic == "operator.revise" and relay.is_stale(payload):
                line = "stale=true"  # 0.21.9: the dropped texts only via next, never on stdout
            _print_event(json_mode, "system", f"({topic} from {sender}) {line}", topic=topic)

    room.on("data_received", _on_data)

    voicebot = relay.VoicebotWatch()  # 0.21.0: voicebot_left / voicebot_back

    def _voicebot_event(ev: dict | None) -> None:
        if ev is None:
            return
        if ctl is not None:
            ctl.events.put_nowait(ev)
        _print_event(json_mode, "system", ev["text"], **{k: v for k, v in ev.items()
                                                         if k not in ("role", "text")})

    def _room_name() -> str:
        return str(getattr(room, "name", "") or "")

    def _on_participant_connected(p) -> None:
        _emit_meta(f"peer-joined: {p.identity} ({_kind_label(p)})")
        if ctl is not None:
            ctl.humans_changed(room)
        _voicebot_event(voicebot.joined(_kind_label(p), _room_name(), stop.is_set()))

    def _on_participant_disconnected(p) -> None:
        ident_ = p.identity
        audible.discard(ident_)
        speakers.discard(ident_)
        _emit_meta(f"peer-left: {ident_} ({_kind_label(p)})")
        if ctl is not None:
            ctl.humans_changed(room)
        rest = [o for o in room.remote_participants.values()
                if o is not p and getattr(o, "identity", None) != ident_]
        humans = any(relay.is_human_peer(_kind_label(o), dict(getattr(o, "attributes", None) or {}))
                     for o in rest)
        _voicebot_event(voicebot.left(_kind_label(p), [_kind_label(o) for o in rest], humans,
                                      _room_name(), stop.is_set()))

    def _on_attributes_changed(changed, p) -> None:
        if ctl is not None:  # 0.21.0 Silent Mode: vh.mic_muted / vh.speaker_muted / vh.away
            ctl.humans_changed(room)

    room.on("participant_connected", _on_participant_connected)
    room.on("participant_disconnected", _on_participant_disconnected)
    room.on("participant_attributes_changed", _on_attributes_changed)

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
        if transport != "bridge" and vtransport.is_webrtc_blocked(e):
            return 4, "WEBRTC_BLOCKED"  # no peer connection: UDP/ICE blocked (sandbox?)
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
    cycle_tasks: list = []  # 0.17.0: relay + helper pump of this cycle, cancelled at its end
    if not vmulti.relay_disabled():  # 0.15.0: relay.json for the other sessions of this machine
        local_dir = ctl.activity_path.parent if ctl is not None and ctl.activity_path else None
        relay_task = asyncio.create_task(_publish_relay(room, tok, slug, json_mode, local_dir,
                                                        api_base=api_base, ctl=ctl, stop=stop))
        _BG_TASKS.add(relay_task)
        relay_task.add_done_callback(_BG_TASKS.discard)
        cycle_tasks.append(relay_task)
    if share_hint is not None and not share_hint.get("shown"):
        share_hint["shown"] = True  # once per join: never again on a reconnect cycle
        _emit_share_hint(json_mode)
    if share_hint is not None and not share_hint.get("onboarding"):
        share_hint["onboarding"] = True  # 0.21.5: multi-agent onboarding, once per join
        _emit_onboarding_hint(json_mode)
    if share_hint is not None and not share_hint.get("sessions"):
        share_hint["sessions"] = True  # 0.21.4: the other agent sessions of this machine, once
        st = asyncio.create_task(_emit_sessions_hint(json_mode))
        _BG_TASKS.add(st)
        st.add_done_callback(_BG_TASKS.discard)
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
        for t in (hb_task, quit_task, *cycle_tasks):
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
    activity_due: float | None = None,
    stale_error: float | None = None,
    updater: _Updater | None = None,
    quiet: bool = False,
    data_channel: bool = False,
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
    sandbox = vtransport.sandbox_env()
    if sandbox:  # heads-up before connecting: the remote MCP server always works here
        print(vtransport.mcp_hint(early=sandbox), file=sys.stderr, flush=True)
    invite = _operator_invite(invite_url)

    # Shared state across reconnect cycles (#6): the notifier dedup, echo ring,
    # and say-seq counter persist so we don't re-wake on the same turn after a
    # transient reconnect.
    notifier = relay.TurnNotifier(wake_only_user=wake_only_user)
    echo = relay.EchoSuppressor(enabled=suppress_echo)
    # 0.14.0: the join hint, shared by all reconnect cycles -> at most once per join.
    share_hint = {"shown": quiet or _quiet_from_env(), "sessions": False}
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
    ctl = _Control(slug, ident, say_tracker, echo, idle_timeout, no_human_timeout,
                   data_channel=data_channel)
    if data_channel:
        _print_event(json_mode, "system", "data-channel mode: only turns addressed to this "
                     "session (name/mention or routed by Delta) and its own decisions wake it; "
                     "broad turns stay as context", topic="_meta")
    ctl.onboarding = onboarding_event(username)  # 0.21.8: also as the first next/listen event
    if status_due is not None:
        ctl.clock.due_s = status_due
    if activity_due is not None:
        ctl.activity_due.due_s = activity_due
    if stale_error is not None:
        ctl.stale_s = stale_error
    server: vsession.ControlServer | None = None
    sess_dir = vsession.session_dir(slug, ident)
    # 0.25.0: a join already running on an OLDER CLI than this binary -> one warning (never
    # spoken) naming `voicehook-agent rejoin`, so it can re-exec in place. Only before we start
    # our own socket, so this join never flags itself; skipped on a self-update/rejoin re-exec.
    if control and not vselfupdate.already_restarted():
        _emit_stale_join_notice(json_mode, stale_running_joins())
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
                                           "started": time.time(),
                                           # 0.25.0: the CLI version this join runs on, so a newer
                                           # binary (status / a starting join) can spot a stale
                                           # running join and suggest `voicehook-agent rejoin`
                                           "cli_version": _VERSION,
                                           # 0.15.0: hash only; hooks of other sessions skip this join
                                           "owner_session": vmulti.own_session_hash(),
                                           # 0.22.2: the agent process that started us; only
                                           # its hooks get mid-turn events (listenguard)
                                           "owner_agent_pid": vboardpid(),
                                           # 0.24.1: data-channel mode (--data-channel); broad
                                           # turns do not wake, only addressed turns + own
                                           # decisions. The Stop hook / status read it from here.
                                           **({"data_channel": True} if data_channel else {})})
            vactivity.write_pointer(sess_dir)  # 0.13.0: hook finds us under any home
            # 0.21.5: durable event spool in the session dir (0600); a restart of this join
            # (self-update) picks up what was not acked yet, an old one is deleted
            ctl.events = vspool.SpoolQueue(sess_dir)
            ctl.events.before_add = ctl.quiet.attach  # 0.24.0
            resumed = ctl.events.resume()
            if resumed:
                _print_event(json_mode, "system", f"event spool: {resumed} unread event(s) "
                             "kept from the previous run of this join", topic="_meta")
            _print_event(json_mode, "system",
                         f"control socket ready: {server.sock_path}", topic="_meta")
    vactivity.clear(sess_dir)  # 0.10.0: no activity lines of a previous call leak in
    vrelayops.clear_dir(sess_dir)  # 0.16.0: nor helper requests/answers
    ctl.activity_path = sess_dir / vactivity.ACTIVITY_NAME
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
                   asyncio.create_task(_activity_loop(ctl, sess_dir / vactivity.ACTIVITY_NAME)),
                   asyncio.create_task(_relay_request_loop(ctl, sess_dir))]

    rc = 0
    try:
        backoff = relay.backoff_delays(base=1.0, factor=2.0, cap=30.0)
        first = True
        webrtc_blocked = False  # a WebRTC connect died without a peer connection
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
                share_hint=share_hint,
            )
            if reason == "SELF_UPDATE":  # 0.12.0: updated, main() restarts the join
                rc = RC_RESTART
                break
            if reason == "CLI_OUTDATED":  # 0.12.0: 426 and no self-update -> exit 7
                break
            if reason == "WEBRTC_BLOCKED":
                webrtc_blocked = True
            if first and vtransport.should_fallback(transport, cur_transport, reason):
                cur_transport = "bridge"
                msg = ("webrtc connect failed or timed out; retrying once via the HTTPS "
                       "bridge (transport=bridge)")
                _print_event(json_mode, "system", msg, topic="_meta")
                print(f"[warn] {msg}", file=sys.stderr, flush=True)
                continue
            if first and webrtc_blocked and reason in ("WEBRTC_BLOCKED", "CONNECT_FAILED"):
                # WebRTC cannot get through and no other transport is left (forced
                # webrtc, or the bridge failed too): retrying will not heal, point to MCP.
                _print_event(json_mode, "system",
                             f"webrtc blocked, use the remote MCP server {vtransport.MCP_URL}",
                             topic="_meta")
                print(vtransport.mcp_hint(), file=sys.stderr, flush=True)
                rc = 4
                break
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
        # 0.25.0: `rejoin` asked us to re-exec in place. Any clean quit (rc 0) while rejoining
        # becomes RC_RESTART, so main() restarts with the same --identity (restart_argv), and the
        # durable spool + the server-side room keep the queue and the status board.
        if ctl.rejoining and rc == 0:
            rc = RC_RESTART
    finally:
        ctl.quit.set()
        # On a rejoin/self-update we re-exec the SAME operator identity into the SAME room: do NOT
        # end the helper hub (POST /api/helper/pump/end), or every helper credential would answer
        # 410. The server binds helper credentials to room+identity and survives the reexec
        # (voicehook-v4 feat/helper-cred-survives-rejoin); only a real leave ends the hub.
        if (ctl.helper_pump is not None and ctl.helper_http is not None
                and rc != RC_RESTART):  # 0.17.0: real leave -> helpers 410
            await ctl.helper_pump.end(ctl.helper_http)
        if ctl.helper_http is not None:
            try:
                await ctl.helper_http.aclose()
            except Exception:  # noqa: BLE001, S110
                pass
        vmulti.remove_relay(owner_pid=os.getpid(), room=slug)  # 0.15.0: only our own file
        if rc == RC_RESTART:  # a waiting `next` learns why, instead of "ended"
            _reason = "rejoin" if ctl.rejoining else "self_update"
            _msg = ("voicehook-agent rejoins the call in place (identity, queue and board kept): "
                    "run next again" if ctl.rejoining else
                    "voicehook-agent updated itself and rejoins the call: run next again")
            ctl.events.put_front_nowait({
                "ok": True, "type": "restarting", "reason": _reason, "message": _msg})
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
            if rc != RC_RESTART:  # 0.21.5: no turn of the call stays on disk (spool.py)
                ctl.events.clear()
            vsession.remove_info(sess_dir)
            vactivity.remove_pointer(sess_dir)
            vactivity.clear(sess_dir)
            vrelayops.clear_dir(sess_dir)  # 0.16.0: helper requests/answers
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


class _SeqAction(argparse.Action):
    """0.21.4 `status`: keeps the order of --doing/--open/--done/--context/--faq, so
    --context and --faq attach to the item they follow (item_seq). --doing/--context keep
    the last value, --open/--done/--faq collect like action=append."""

    def __call__(self, parser, ns, values, option_string=None):
        ns.item_seq = list(getattr(ns, "item_seq", None) or []) + [(self.dest, values)]
        if self.dest in ("doing", "context"):
            setattr(ns, self.dest, values)
        else:
            setattr(ns, self.dest, list(getattr(ns, self.dest, None) or []) + [values])


def status_meta(seq: list, board: dict, n_open: int, n_done: int, warn=None) -> dict:
    """Board field meta {doing: {context, faq}, open: [..], done: [..]} from the flag order:
    --context/--faq belong to the --doing/--open/--done before them (none yet: the doing
    item, TEXT). open/done lists are aligned with the board's (a -f file's items first).
    ValueError when a context is too long."""
    entries: dict = {}
    cur = ("doing", 0)
    count = {"open": 0, "done": 0}
    for dest, val in seq:
        if dest == "doing":
            cur = ("doing", 0)
        elif dest in ("open", "done"):
            cur = (dest, count[dest])
            count[dest] += 1
        elif dest in ("context", "faq"):
            e = entries.setdefault(cur, {"context": None, "faq": []})
            if dest == "context":
                e["context"] = val
            else:
                e["faq"].append(val)
    meta: dict = {}
    offset = {"open": len(board.get("open") or []) - n_open,
              "done": len(board.get("done") or []) - n_done}
    for (kind, i), e in entries.items():
        m = relay.item_meta(e["context"], e["faq"], warn=warn)
        if not m:
            continue
        if kind == "doing":
            meta["doing"] = m
            continue
        lst = meta.setdefault(kind, [{} for _ in board.get(kind) or []])
        lst[offset[kind] + i] = m
    return meta


def _data_request(args) -> dict:
    """`send-data --to X (--json J | --text T | --file PATH) [--name N] [--mime M]`: the
    request for the join (a file goes by path: the join reads it, no 10 MB over the socket)."""
    req: dict = {"cmd": "data", "to": args.to}
    if args.json is not None:
        try:
            req.update(kind="json", payload=json.loads(args.json))
        except ValueError as e:
            raise ValueError(f"--json is not valid JSON: {e}") from None
    elif args.text is not None:
        req.update(kind="text", payload=sys.stdin.read() if args.text == "-" else args.text)
    else:
        path = os.path.abspath(os.path.expanduser(args.file))
        if not os.path.isfile(path):
            raise ValueError(f"--file {args.file!r} is not a file")
        req.update(kind="file", path=path)
    for k in ("name", "mime", "id"):
        if getattr(args, k, None):
            req[k] = getattr(args, k)
    return req


def _decide_faq(args) -> list[dict]:
    return relay.parse_faq(getattr(args, "faq", None) or [],
                           warn=lambda m: print(m, file=sys.stderr, flush=True),
                           limit=relay.ITEM_FAQ_MAX)


# --------------------------------------------------------------------------- #
# 0.24.0 agent-to-agent tasks: `voicehook-agent send --to <label> "<task>"` (a2a.py)
# --------------------------------------------------------------------------- #
def _session_registry() -> dict:
    """{hash: {label, at, ...}} of the sessions that reported recently (relay-state.json), {}
    on any error. Each is a session of THIS machine (helper or joined)."""
    try:
        st = json.loads((vactivity.state_root() / vmulti.STATE_NAME).read_text(encoding="utf-8"))
        sessions = st.get("sessions") if isinstance(st, dict) else None
        return {str(k): v for k, v in (sessions or {}).items() if isinstance(v, dict)}
    except (OSError, ValueError):
        return {}


def _local_session_labels() -> list[str]:
    """Labels of other sessions of this machine we can hand a task to: the recent registry
    labels plus any live join's owner-session label. Directed send targets one of these."""
    return vlocal.registry_labels()


def _live_join_for_hash(session_hash: str) -> Path | None:
    """The live join dir owned by the session with this hash (a session running its own join),
    or None. Used to wake it instantly over its control socket."""
    if not session_hash:
        return None
    from . import session as vsession
    for d in vactivity.live_join_dirs():
        try:
            info = json.loads((d / vsession.INFO_NAME).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(info, dict) and str(info.get("owner_session") or "") == session_hash:
            return d
    return None


def _wake_target_join(label: str) -> bool:
    """Instant wake: if `label`'s session owns a live join, push a front task-wake into its
    spool over the control socket. Best effort; the durable inbox copy is the real delivery."""
    reg = _session_registry()
    skey = next((k for k, v in reg.items() if va2a._clean_label(v.get("label")) == label), "")
    d = _live_join_for_hash(skey)
    if d is None:
        return False
    from . import session as vsession
    try:
        sock = d / vsession.SOCKET_NAME
        reply = vsession.request(sock, {"cmd": "task_wake", "label": label}, timeout=5.0)
        return bool(reply.get("ok"))
    except (vsession.SessionError, OSError, ValueError):
        return False


def _run_send(args) -> int:
    """`send --to <label> "<task>"`: hand a directed task to another local session (Phase 1).
    Resolves the label, appends a `task` event to that session's inbox (durable, read by its
    relay-next / listen / hook), and wakes it instantly if it owns a live join. One JSON line."""
    text = args.text
    if text == "-":
        text = sys.stdin.read()
    text = va2a.clean_text(text)
    if not text:
        _print_json({"ok": False, "type": "task", "error": "send needs a task text"})
        return 2
    try:
        me = vrelayops.session_label()
    except vrelayops.RelayError:
        me = ""
    try:
        target = va2a.resolve_target(args.to, _local_session_labels(), me=me)
    except va2a.TaskError as e:
        _print_json({"ok": False, "type": "task", "status": e.status, "error": e.detail})
        return 1
    task_id = va2a.new_id()
    ev = va2a.task_event(task_id, me or "?", target, text)
    try:
        vrelayops.append_jsonl(vrelayops.inbox_path(target), ev)
        if me:  # durable record for OUR board ("Auftrag offen bei X seit ...")
            vrelayops.append_jsonl(vrelayops.outbox_path(me), ev)
    except (OSError, vrelayops.RelayError) as e:
        _print_json({"ok": False, "type": "task", "status": 503,
                     "error": f"inbox not writable: {type(e).__name__}"})
        return 1
    woke = _wake_target_join(target)
    _print_json({"ok": True, "type": "task", "id": task_id, "to": target, "woke": woke,
                 "note": "delivered to the target's inbox; it acks via relay-next/listen"})
    return 0


def _run_tasks(args) -> int:
    """`tasks [--to-me]`: the tasks THIS session has open (sent, not yet acked), reconstructed
    from the inbox on disk. --to-me: the tasks addressed TO this session that are still in its
    inbox waiting to be consumed. One JSON line."""
    try:
        me = vrelayops.check_from(vrelayops.session_label())
    except vrelayops.RelayError as e:
        _print_json({"ok": False, "type": "tasks", "error": str(e)})
        return 1
    try:
        inbox = vrelayops.read_inbox(vrelayops.inbox_path(me))
    except (OSError, vrelayops.RelayError) as e:
        _print_json({"ok": False, "type": "tasks", "status": 503,
                     "error": f"inbox not readable: {type(e).__name__}"})
        return 1
    if getattr(args, "to_me", False):
        incoming = [va2a.valid_task(e) for e in inbox]
        incoming = [t for t in incoming if t is not None]
        _print_json({"ok": True, "type": "tasks", "incoming":
                     [{"id": t["id"], "from": t["from"], "text": t["text"]} for t in incoming]})
        return 0
    # our board: the tasks WE sent (outbox) closed by the acks/dues that came back (inbox)
    try:
        events = vrelayops.read_jsonl(vrelayops.outbox_path(me)) + inbox
    except (OSError, vrelayops.RelayError):
        events = inbox
    open_ = va2a.open_tasks(events)
    # a long-open task nags the sender: write the owed task_due into our OWN inbox so the next
    # relay-next / listen surfaces it (the inbox is the state, so a fresh process agrees)
    due = va2a.due_reminders(open_, me)
    for ev in due:
        try:
            vrelayops.append_jsonl(vrelayops.inbox_path(me), ev)
        except (OSError, vrelayops.RelayError):
            pass
    _print_json({"ok": True, "type": "tasks", "open": open_, "due": len(due),
                 "board": [va2a.board_line(t) for t in open_]})
    return 0


def _client_request(args) -> dict:
    if args.cmd == "say":
        text = " ".join(args.text)
        if text == "-":
            text = sys.stdin.read()
        req = {"cmd": "say", "text": text.strip(), "mode": args.mode or SAY_DEFAULT_MODE}
        if getattr(args, "urgent", False):
            req["urgent"] = True
        req.update(vvisual.say_fields(getattr(args, "shape_preset", None),
                                      getattr(args, "shape_json", None),
                                      getattr(args, "emotion", None)))
        return req
    if args.cmd == "show":
        if args.card is not None or args.panel is not None or args.large:  # 0.21.0 cards
            if (args.preset or args.polygon or args.path or args.json is not None or args.open
                    or args.label is not None or args.hold_ms is not None or args.emotion is not None):
                raise vcards.CardError("--card/--panel cannot be combined with shape flags "
                                       "(--preset/--polygon/--path/--json/--label/--hold-ms/--emotion)")
            return vcards.request_from_args(kind=args.card, title=args.title, text=args.text,
                                            file=args.file, panel=args.panel, large=args.large,
                                            stdin=sys.stdin)
        if args.title is not None or args.text is not None or args.file is not None:
            raise vcards.CardError("--title/--text/--file need --card text|code|link|image")
        shape = vvisual.shape_from_args(
            presets=args.preset, polygons=args.polygon, paths=args.path,
            json_text=args.json, open_=args.open, label=args.label, hold_ms=args.hold_ms)
        emo = vvisual.parse_emotion_arg(args.emotion)
        return {"cmd": "visual", **vvisual.payload(shape, emo)}
    if args.cmd == "next":
        return {"cmd": "next", "timeout": args.timeout}
    if args.cmd == "helper-credential":
        return {"cmd": "helper_credential", "label": args.label, "rotate": bool(args.rotate)}
    if args.cmd == "vocab":
        return vocab_request(args.term, args.wrong)
    if args.cmd in ("notify-me", "call-me"):
        return phone_request(args.cmd.replace("-", "_"), args.reason, args.message)
    if args.cmd == "says":
        return {"cmd": "says"}
    if args.cmd == "activity":
        text = " ".join(args.text)
        if text == "-":
            text = sys.stdin.read()
        vactivity.note_line(text)  # ValueError when empty -> exit 1 before connecting
        vwait.check(text, "activity")  # 0.21.6: waiting for the human = decide, never a line
        for w in vplain.item_warnings([text.strip()], "Aktivität"):  # 0.21.5 Klartext
            print(w, file=sys.stderr, flush=True)
        req = {"cmd": "activity", "text": text.strip()}
        meta = relay.item_meta(getattr(args, "context", None), getattr(args, "faq", None),
                               warn=lambda m: print(m, file=sys.stderr, flush=True))
        req.update(meta)  # 0.21.4: hidden {context, faq} of this line
        project = vui.project_field(getattr(args, "project", None))
        if project is not None:
            req["project"] = project
        return req
    if args.cmd == "ui":
        return {"cmd": "ui", "body": vui.command(args.action, args.args, args.preset)}
    if args.cmd == "leave":
        return {"cmd": "leave", "say": args.say} if args.say else {"cmd": "leave"}
    if args.cmd == "rejoin":  # 0.25.0: re-exec the join in place (keeps identity/queue/board)
        return {"cmd": "rejoin"}
    if args.cmd == "send-data":  # 0.21.4
        return _data_request(args)
    if args.cmd in DELTA_KINDS:  # 0.23.0
        return delta_request(args.cmd, args.action, " ".join(args.arg or []) or None,
                             bool(getattr(args, "persist", False)))
    if args.cmd == "board" or (args.cmd == "decide" and getattr(args, "list", False)):
        # 0.21.4: open + recent decisions and the boards this join has seen
        return {"cmd": "board_list", "open": bool(getattr(args, "open_only", False))}
    if args.cmd == "decide":
        # 0.21.0: background for Delta; over the limit -> ValueError, nothing sent
        context = _decide_text(getattr(args, "context", None), "--context", DECIDE_CONTEXT_MAX)
        recent = _decide_text(getattr(args, "recent", None), "--recent", DECIDE_RECENT_MAX)
        if args.delete or args.id:
            if not args.id:
                raise ValueError("decide --delete needs --id")
            req = {"cmd": "decide", "id": args.id}
            if args.delete:
                req["delete"] = True
            else:  # change an open decision ('' clears context/recent)
                if args.question is not None:
                    for w in vplain.check_decide(args.question, context, new=False):  # 0.21.5
                        print(w, file=sys.stderr, flush=True)
                    req["question"] = args.question
                if args.option:
                    req["options"] = args.option
                if args.recommend is not None:
                    req["recommend"] = args.recommend
                if context is not None:
                    req["context"] = context
                if recent is not None:
                    req["recent"] = recent
                faq = _decide_faq(args)
                if faq:
                    req["faq"] = faq
                if len(req) == 2:
                    raise ValueError("decide --id: pass --question, --option, --recommend, --context "
                                     "or --recent (or --delete)")
            return req
        if not args.question:
            raise ValueError("decide needs --question (plus optional --option/--recommend/--context)")
        # 0.21.5 Klartext: a whole sentence ending with ?, no bare numbers or abbreviations,
        # --context mandatory; PlainError -> exit 2, nothing sent (plaintext.py)
        for w in vplain.check_decide(args.question, context):
            print(w, file=sys.stderr, flush=True)
        req = {"cmd": "decide", "question": args.question}
        faq = _decide_faq(args)
        if faq:
            req["faq"] = faq
        if not context or len(faq) < relay.ITEM_FAQ_MAX:  # 0.21.4 mandatory: warn, never refuse
            print(relay.MANDATORY_HINT, file=sys.stderr, flush=True)
        if args.option:
            req["options"] = args.option
        if args.recommend is not None:
            req["recommend"] = args.recommend
        if context:
            req["context"] = context
        if recent:
            req["recent"] = recent
        return req
    faq_raw = getattr(args, "faq", None) or []
    project = vui.project_field(getattr(args, "project", None))  # 0.21.0
    if (args.text is not None or args.doing is not None or args.open or args.done or args.file
            or faq_raw):
        file_obj = None
        if args.file:
            with open(args.file, encoding="utf-8") as fh:
                file_obj = json.load(fh)
        def warn(m: str) -> None:
            print(m, file=sys.stderr, flush=True)
        # 0.22.2: every --faq belongs to the item it follows (meta, max ITEM_FAQ_MAX per item,
        # warned there); the flat board field faq keeps the first FAQ_MAX pairs silently, so
        # doing + several open/done items with 3 pairs each is never cut at 6 any more
        faq = relay.parse_faq(faq_raw, limit=len(faq_raw))[:relay.FAQ_MAX]
        board = relay.build_board(args.text, args.doing, args.open, args.done, file_obj, faq=faq)
        # 0.21.6: waiting for the human is a decision, never only a status (waitcheck.py)
        vwait.check(board.get("doing"), "status --doing")
        for w in vwait.warn_items(list(board.get("open") or [])):
            warn(w)
        meta = status_meta(getattr(args, "item_seq", None) or [], board,
                           len(args.open or []), len(args.done or []), warn)
        if meta:  # 0.21.4: per item {context, faq}, hidden, for the voicebot
            board["meta"] = meta
        items = [board.get("doing") or ""] + list(board.get("open") or []) + list(board.get("done") or [])
        for w in vplain.item_warnings([i for i in items if isinstance(i, str) and i.strip()]):
            warn(w)  # 0.21.5 Klartext, a warning: never refused while you work
        missing = relay.missing_meta(board)
        if missing:  # 0.21.4 mandatory, but never refused during a live call
            warn(f"{relay.MANDATORY_HINT} Fehlt bei: " + "; ".join(missing[:5]))
        if project is not None:
            board["project"] = project
        return {"cmd": "board", "board": board}
    if project is not None:
        raise ValueError("status --project goes with a board: TEXT, --doing, --open, --done or -f")
    return {"cmd": "status"}


def _run_bridge_session(args) -> int:
    """0.13.0 `bridge-session --save JSON [--base URL] | --clear`. Never prints the token."""
    if args.clear:
        vactivity.clear_bridge_session()
        vmulti.remove_relay(owner_session=vmulti.own_session_hash() or None,
                            owner_pid=_relay_owner_pid())
        print(json.dumps({"ok": True, "type": "bridge-session", "cleared": True}), flush=True)
        return 0
    if not args.save:
        print(json.dumps({"ok": False, "type": "error", "error": "--save JSON or --clear"}), flush=True)
        return 2
    try:
        raw = sys.stdin.read() if args.save == "-" else Path(args.save).read_text(encoding="utf-8")
        base = args.base or os.environ.get("VOICEHOOK_API_BASE") or "https://voicehook.ai"
        answer = json.loads(raw)
        path = vactivity.save_bridge_session(answer, base)
    except (OSError, ValueError) as e:
        print(json.dumps({"ok": False, "type": "error", "error": str(e)}), flush=True)
        return 1
    out = {"ok": True, "type": "bridge-session", "path": str(path)}
    # 0.15.0: relay token for the other Claude Code sessions of this machine
    if not args.no_relay and not vmulti.relay_disabled():
        sess = vactivity.load_bridge_session() or {}
        rel = vmulti.mint_relay(sess.get("base", ""), sess.get("session", ""))
        if rel is not None:
            try:
                out["relay"] = str(vmulti.write_relay(
                    rel["url"], rel["token"], str(answer.get("room") or ""), rel["expires_in"],
                    owner_pid=_relay_owner_pid() or 0))
            except (OSError, ValueError):
                pass
    print(json.dumps(out), flush=True)
    return 0


def _relay_owner_pid() -> int | None:
    """Owner pid of a relay.json written by the short-lived `bridge-session`: the Claude
    Code process ($VOICEHOOK_OWNER_PID, else $CLAUDE_PID), so readers drop it with it."""
    for v in (os.environ.get("VOICEHOOK_OWNER_PID"), os.environ.get("CLAUDE_PID")):
        try:
            pid = int(str(v or "").strip())
        except ValueError:
            continue
        if pid > 0:
            return pid
    return None


def _run_helper_import(args=None) -> int:
    """`helper-import [--as NAME]`: credential JSON on stdin (over ssh, never a chat) ->
    ~/.voicehook/helpers/<name>.json (0.22.2: one per helper session, vhelp.import_credential)."""
    try:
        got = vhelp.import_credential(sys.stdin.read(), getattr(args, "as_name", None))
    except (OSError, ValueError) as e:
        print(json.dumps({"ok": False, "type": "error", "error": str(e)}), flush=True)
        return 1
    if got.pop("legacy", False):
        print(vmulti.LEGACY_WARN, file=sys.stderr, flush=True)
    if not got["bound"]:
        print(f"[info] helper {got['name']!r} is bound to no session: start the helper session "
              f"with VOICEHOOK_SESSION_LABEL={got['name']} (or keep it the only unbound one)",
              file=sys.stderr, flush=True)
    print(json.dumps({"ok": True, "type": "helper-import", **got}), flush=True)
    return 0


def _run_client(args) -> int:
    """say / next / leave / status: one JSON line out. Exit 0 = ok (incl.
    next timeout), 1 = request failed, 3 = no running join / join ended."""
    if args.cmd == "status" and getattr(args, "waiting", None) is not None:
        return _run_waiting(args)  # 0.21.6: board "wartet auf dich" + its decision
    try:
        req = _client_request(args)
    except (vvisual.VisualError, vcards.CardError, vui.UiError, vplain.PlainError,
            vwait.WaitError) as e:  # bad shape/card/ui, not plain, 0.21.6 waits: nothing sent, exit 2
        print(json.dumps({"ok": False, "type": "invalid", "error": str(e)}, ensure_ascii=False),
              flush=True)
        print(f"[error] {e}", file=sys.stderr, flush=True)
        return 2
    except (OSError, ValueError) as e:
        print(json.dumps({"ok": False, "type": "error", "error": str(e)}, ensure_ascii=False), flush=True)
        return 1
    if req.get("cmd") in RELAY_CMDS:
        # 0.16.0: a Claude Code session without its own join, while another session of
        # this machine is in the call: decide/board go over relay.json as a helper.
        # 0.22.2: activity, next and show too, with the same rule (relayops.client_relay).
        # 0.23.1/0.23.1: show --card goes as a helper card (the card names the helper as
        # its sender); ring shapes and --panel alone stay with the join.
        rel = _helper_relay(args.session)
        if rel is not None:
            return _run_relay_client(args, req, rel)
        bad = vrelayops.helper_problem(args.session)  # 0.25.0: never another session's join
        if bad is not None:
            print(json.dumps(bad, ensure_ascii=False), flush=True)
            return 3
    wait_answer = req.get("cmd") == "decide" and getattr(args, "wait", None) == WAIT_ANSWER
    try:
        sock = vsession.resolve_socket(args.session,
                                       wait=30.0 if wait_answer else args.wait)
        sock_timeout = None if args.cmd == "next" else PHONE_SOCK_TIMEOUT_S.get(req["cmd"], 30.0)
        if req.get("cmd") == "next":
            req = {**req, "lease": True}  # 0.21.5: acked below, after the print (spool.py)
        reply = vsession.request(sock, req, timeout=sock_timeout)
    except vsession.ForeignJoin as e:  # 0.25.0: never another session's join
        print(json.dumps(_foreign_reply(e), ensure_ascii=False), flush=True)
        return 3
    except vsession.SessionError as e:
        print(json.dumps({"ok": False, "type": "no-session", "error": str(e),
                          **_no_session_hint(args, req)}, ensure_ascii=False), flush=True)
        return 3
    except OSError as e:
        print(json.dumps({"ok": False, "type": "no-session", "error": repr(e),
                          **_no_session_hint(args, req)}, ensure_ascii=False), flush=True)
        return 3
    if wait_answer and reply.get("ok"):
        reply["hint"] = "own join: the answer comes as a `next` event type decision"
    if req.get("cmd") == "ui" and reply.get("ok"):  # 0.21.0: one confirmation line
        line = vui.summary(reply.get("applied") if isinstance(reply.get("applied"), dict) else req["body"])
        print(line + (" (no human in the call, nothing shown)" if reply.get("humans") == 0 else ""),
              flush=True)
        return 0
    print(json.dumps(reply, ensure_ascii=False), flush=True)
    if req.get("cmd") == "next":
        _ack(vsession.request, sock, reply)
    if reply.get("type") == "ended":
        return 3
    return 0 if reply.get("ok") else 1


# 0.22.2: the requests a helper session (no own join) sends over the relay; one routing rule
# for all of them (relayops.client_relay). `status` without a board, `ui`, `say` & co. never.
RELAY_CMDS = ("decide", "board", "activity", "next", "visual", "card")
NO_SESSION_HINT = ("no own join and no helper credential for this session: pass --session "
                   "<slug>/<identity>, or import one per session: `voicehook-agent "
                   "helper-credential --label <name> | voicehook-agent helper-import --as <name>`")
SHOW_HELPER = ("show (ring shapes, --panel alone) is only for the session in the call: the relay "
               "has no route for it. As a helper use show --card, status/activity/decide, or ask "
               "the main agent")


def _helper_relay(explicit_session: str | None) -> dict | None:
    """The relay of this session (relayops.client_relay), with the legacy warning (0.22.2)."""
    rel = vrelayops.client_relay(explicit_session)
    if rel is not None and vmulti.is_legacy_helper(rel):
        print(vmulti.LEGACY_WARN, file=sys.stderr, flush=True)
    return rel


def _foreign_reply(e: Exception) -> dict:
    """0.25.0: the answer when only joins of OTHER agent sessions run (session.ForeignJoin)."""
    return {"ok": False, "type": "foreign-join", "error": str(e)}


def _no_session_hint(args, req: dict) -> dict:
    """{hint} for a no-session answer of a helper-capable command (0.22.2), else {}."""
    if req.get("cmd") not in RELAY_CMDS or getattr(args, "session", None):
        return {}
    return {"hint": NO_SESSION_HINT}


def _meta_faq_n(meta: object) -> int:
    if not isinstance(meta, dict):
        return 0
    items = [meta.get("doing")] + list(meta.get("open") or []) + list(meta.get("done") or [])
    return sum(len(m.get("faq") or []) for m in items if isinstance(m, dict))


def vboardpid() -> int:
    """0.22.2: board.agent_pid() of this join (0 when unknown, never raises)."""
    try:
        from . import board
        return board.agent_pid()
    except Exception:  # noqa: BLE001
        return 0


def vrelay_meta(req: dict) -> dict:
    """The valid hidden {context, faq} of a request ({} = none or invalid)."""
    m = relay.clean_meta({k: req.get(k) for k in ("context", "faq")})
    return m or {}


ACTIVITY_META_KEEP = 50  # 0.21.4: lines with a hidden {context, faq} kept for operator.activity
WAIT_ANSWER = -1.0  # `decide --wait` without SEC: wait for the answer (helper sessions)

# 0.21.4 `listen`: the gapless `next` loop. It prints and exits on the first event that needs
# the agent; everything else (timeout, human_state, voicebot_back) keeps it waiting.
LISTEN_WAKE = vspool.WAKE_TYPES
# 0.21.5: 10 s (was 45): each single `next` of the loop; the join keeps the presence itself
LISTEN_POLL_S = 10.0


def _ack(request, sock, reply: dict) -> None:
    """0.21.5: tell the join the event is out (spool cursor); best effort, never raises.
    A missing ack only means the event comes once more after spool.LEASE_S."""
    seq = reply.get("seq")
    if seq is None:
        return
    try:
        request(sock, {"cmd": "ack", "seq": seq}, timeout=5.0)
    except (vsession.SessionError, OSError, ValueError):
        pass


def _run_listen(args, request=None, resolve=None, clock=time.monotonic) -> int:
    """`listen [--timeout SEC] [--poll SEC]`: repeat `next` until an actionable event (one
    JSON line, exit 0), the join ends (exit 3) or --timeout passes ({"type":"timeout"}, exit
    0; 0 = no limit). Meant for the harness's background mode (run_in_background), so the
    agent is woken when it exits."""
    request = request or vsession.request
    resolve = resolve or vsession.resolve_socket
    poll = max(1.0, float(args.poll))
    deadline = clock() + args.timeout if args.timeout and args.timeout > 0 else None
    try:
        sock = resolve(args.session, wait=args.wait)
    except vsession.ForeignJoin as e:  # 0.25.0: never take another session's next events
        _print_json(_foreign_reply(e))
        return 3
    except (vsession.SessionError, OSError) as e:
        _print_json({"ok": False, "type": "no-session", "error": str(e)})
        return 3
    while True:
        wait = poll if deadline is None else min(poll, deadline - clock())
        if wait <= 0:
            _print_json({"ok": True, "type": "timeout", "pending": 0})
            return 0
        try:
            reply = request(sock, {"cmd": "next", "timeout": max(1.0, wait), "lease": True},
                            timeout=None)
        except (vsession.SessionError, OSError) as e:
            _print_json({"ok": False, "type": "no-session", "error": repr(e)})
            return 3
        kind = reply.get("type")
        if kind == "ended":
            _print_json(reply)
            return 3
        if not reply.get("ok"):
            _print_json(reply)
            return 1
        if kind in LISTEN_WAKE:
            _print_json(reply)
            _ack(request, sock, reply)  # after the print: a kill in between = once more, never lost
            return 0
        _ack(request, sock, reply)  # timeout / human_state / voicebot_back: consumed silently


def _print_json(obj: dict) -> None:
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def _run_relay_client(args, req: dict, rel: dict) -> int:
    """0.16.0 helper session: decide / status board over relay.json (remote: HTTP,
    local: request file of the join). One JSON line, plus "via":"relay"."""
    if req["cmd"] == "visual" or (req["cmd"] == "card" and not req.get("kind")):
        _print_json({"ok": False, "type": "helper-unsupported", "via": "relay", "error": SHOW_HELPER})
        return 1
    local = vmulti._is_local(rel)
    try:
        frm = vrelayops.check_from(vrelayops.session_label())
        if req["cmd"] == "next":
            return _relay_next_out(rel, frm, getattr(args, "timeout", 0.0))
        if req["cmd"] == "activity":
            body = {"text": req["text"], **vrelay_meta(req)}
            if local:
                ok = vmulti.append_local(rel, f"{frm}: {req['text']}")
                res = {"ok": True} if ok else {"ok": False, "status": 503,
                                               "error": "join of the relay is gone"}
            else:
                res = vrelayops.remote_activity(rel, frm, body)
            out = {**res, "type": "activity", "via": "relay"}
        elif req["cmd"] == "decide":
            body = vrelayops.decide_body(req)
            res = (vrelayops.local_request(rel, frm, "decide", body) if local
                   else vrelayops.remote_decide(rel, frm, body))
            out = {**res, "type": "decide", "via": "relay"}
            if res.get("ok") and getattr(args, "wait", None) == WAIT_ANSWER and res.get("id"):
                evs = vrelayops.next_events(rel, frm, max(0.0, float(args.timeout)),
                                            match_id=str(res["id"]))
                out["decision"] = evs[0] if evs else None
                if not evs:
                    out["timeout"] = True
        elif req["cmd"] == "card":  # 0.23.1: a helper's card (image: server relay only)
            body = vrelayops.card_body(req)
            if local and body.get("kind") == "image":
                raise vrelayops.RelayError("image cards of a helper need the server relay (the "
                                           "server of this call is too old); text, code and link work", 422)
            res = (vrelayops.local_request(rel, frm, "card", body) if local
                   else vrelayops.remote_show(rel, frm, body))
            out = {**res, "type": "card", "via": "relay"}
        else:
            board = req.get("board") or {}
            body = vrelayops.status_body(board)
            # 0.22.2: --faq goes per item in meta; warn only when a pair really stays behind
            if board.get("faq") and _meta_faq_n(body.get("meta")) < max(
                    1, _meta_faq_n(board.get("meta"))):
                print("[warn] --faq: not every pair reached the relay (meta per item, "
                      f"max {relay.ITEM_FAQ_MAX} per item)", file=sys.stderr, flush=True)
            res = (vrelayops.local_request(rel, frm, "status", body) if local
                   else vrelayops.remote_status(rel, frm, body))
            out = {**res, "type": "board", **({"board": body} if res.get("ok") else {}),
                   "via": "relay"}
    except vrelayops.RelayError as e:
        out = {"ok": False, "status": e.status or 503, "error": str(e), "via": "relay"}
    except OSError as e:
        out = {"ok": False, "status": 503, "error": f"relay: {type(e).__name__}", "via": "relay"}
    _print_json(out)
    return 0 if out.get("ok") else 1


def _waiting_out(obj: dict, rc: int) -> int:
    _print_json(obj)
    if not obj.get("ok"):
        print(f"[error] {obj.get('error')}", file=sys.stderr, flush=True)
    return rc


WAITING_NEEDS = ("status --waiting braucht --question \"<Bereich>: <was> freigeben, damit "
                 "<wozu>?\" --context \"...\" (stellt die Entscheidung) oder --decision <id|#nr> "
                 "(eine offene Entscheidung). " + vwait.RULE)


def _run_waiting(args) -> int:
    """0.21.6 `status --waiting TEXT (--question Q --context C | --decision ID)`: the board says
    "wartet auf dich" (state frage) and points at its decision; with --question the decision is
    asked first (Klartext rule, nothing on the board when it fails). Own join or relay (helper)."""
    text = str(args.waiting or "").strip()
    q = (getattr(args, "question", None) or "").strip()
    ref = getattr(args, "decision", None)
    if not text:
        return _waiting_out({"ok": False, "type": "invalid", "error": "--waiting needs a text"}, 2)
    if bool(q) == bool(ref):
        return _waiting_out({"ok": False, "type": "invalid", "error": WAITING_NEEDS}, 2)
    rel = _helper_relay(args.session)
    frm = vrelayops.check_from(vrelayops.session_label()) if rel is not None else ""
    sock = None
    if rel is None:
        bad = vrelayops.helper_problem(args.session)  # 0.25.0: never another session's join
        if bad is not None:
            return _waiting_out(bad, 3)
        try:
            sock = vsession.resolve_socket(args.session, wait=args.wait)
        except vsession.ForeignJoin as e:
            return _waiting_out(_foreign_reply(e), 3)
        except (vsession.SessionError, OSError) as e:
            return _waiting_out({"ok": False, "type": "no-session", "error": str(e)}, 3)

    def send(req: dict, kind: str) -> dict:
        if rel is None:
            return vsession.request(sock, req, timeout=30.0)
        local = vmulti._is_local(rel)
        if kind == "decide":
            body = vrelayops.decide_body(req)
            return (vrelayops.local_request(rel, frm, "decide", body) if local
                    else vrelayops.remote_decide(rel, frm, body))
        body = vrelayops.status_body(req["board"])
        return (vrelayops.local_request(rel, frm, "status", body) if local
                else vrelayops.remote_status(rel, frm, body))

    try:
        if q:
            context = _decide_text(getattr(args, "context", None), "--context", DECIDE_CONTEXT_MAX)
            for w in vplain.check_decide(q, context):
                print(w, file=sys.stderr, flush=True)
            res = send({"cmd": "decide", "question": q, "context": context}, "decide")
            if not res.get("ok"):
                return _waiting_out({**res, "ok": False, "type": "decide",
                                     "error": res.get("error") or "decide failed, board not set"}, 1)
            decision = {"id": res.get("id"), "number": res.get("number"), "question": q}
        else:
            r = vwait.decision_ref(ref)
            if r is None:
                return _waiting_out({"ok": False, "type": "invalid", "error":
                                     f"--decision {ref!r}: id d + 8 hex or the board number"}, 2)
            decision = {"id": r} if r.startswith("d") else {"number": int(r)}
            if rel is None:  # own join: it must be one of the open decisions it has seen
                lst = vsession.request(sock, {"cmd": "board_list", "open": True}, timeout=10.0)
                hit = [it for it in lst.get("open") or []
                       if it.get("id") == decision.get("id") or it.get("number") == decision.get("number")]
                if not hit:
                    return _waiting_out({"ok": False, "type": "invalid", "error":
                                         f"keine offene Entscheidung {ref}: erst `voicehook-agent decide "
                                         f"--question ... --context ...`, dann --decision <id>. "
                                         + vwait.RULE}, 2)
                decision = {k: hit[0].get(k) for k in ("id", "number", "question") if hit[0].get(k)}
        board = relay.build_board(None, text, args.open, args.done)
        board.update(state=vwait.STATE, decision=decision)
        res = send({"cmd": "board", "board": board}, "status")
        if rel is not None and res.get("ok"):
            from . import board as vboardhook
            vboardhook.post_state(rel, frm, vwait.STATE)  # the helper card: "wartet auf dich"
    except (vplain.PlainError, ValueError) as e:
        return _waiting_out({"ok": False, "type": "invalid", "error": str(e)}, 2)
    except vrelayops.RelayError as e:
        return _waiting_out({"ok": False, "status": e.status or 503, "error": str(e), "via": "relay"}, 1)
    except (vsession.SessionError, OSError) as e:
        return _waiting_out({"ok": False, "type": "no-session", "error": repr(e)}, 3)
    out = {**res, "type": "board", "board": board, "decision": decision, "state": vwait.STATE}
    if rel is not None:
        out["via"] = "relay"
    return _waiting_out(out, 0 if res.get("ok") else 1)


def _run_relay_next(args) -> int:
    """0.16.0 `relay-next [--timeout S]`: the answers to this helper session's decisions,
    one JSON line per event; {"type":"timeout"} when none came. Exit 3: no relay.
    0.22.2: the relay of this session (own helper entry first, multisession.load_relay)."""
    rel = None if vmulti.relay_disabled() else vmulti.load_relay()
    if rel is None:
        _print_json({"ok": False, "type": "no-relay", "via": "relay",
                     "error": "no helper credential for this session and no usable "
                              "~/.voicehook/relay.json (no call on this machine)"})
        return 3
    if vmulti.is_legacy_helper(rel):
        print(vmulti.LEGACY_WARN, file=sys.stderr, flush=True)
    try:
        frm = vrelayops.check_from(vrelayops.session_label())
    except vrelayops.RelayError as e:
        _print_json({"ok": False, "status": e.status or 503, "error": str(e), "via": "relay"})
        return 1
    return _relay_next_out(rel, frm, args.timeout)


def _ack_task_back(frm: str, ev: dict) -> None:
    """0.24.0: this session consumed a `task` event; write a `task_acked` back to the SENDER's
    inbox so its board shows "angenommen". Best effort, never raises, never logs the task text."""
    if ev.get("type") != va2a.TASK:
        return
    sender = va2a._clean_label(ev.get("from"))
    tid = str(ev.get("id") or "")
    if not sender or not tid or sender.lower() == va2a._clean_label(frm).lower():
        return
    try:
        vrelayops.append_jsonl(vrelayops.inbox_path(sender),
                               va2a.task_acked_event(tid, sender, frm))
    except (OSError, vrelayops.RelayError) as e:
        print(f"[warn] task ack not written ({type(e).__name__})", file=sys.stderr, flush=True)


def _relay_next_out(rel: dict, frm: str, timeout) -> int:
    """The events of one relay-next round, one JSON line each (or one timeout line)."""
    try:
        evs = vrelayops.next_events(rel, frm, max(0.0, float(timeout or 0)))
    except vrelayops.RelayError as e:
        _print_json({"ok": False, "status": e.status or 503, "error": str(e), "via": "relay"})
        return 1
    except OSError as e:
        _print_json({"ok": False, "status": 503, "error": f"relay: {type(e).__name__}", "via": "relay"})
        return 1
    if not evs:
        _print_json({"ok": True, "type": "timeout", "via": "relay"})
    for ev in evs:
        _print_json({**vrelayops.view(ev), "via": "relay"})
        _ack_task_back(frm, ev)  # 0.24.0: a consumed task is acked back to the sender
    _ack_decisions(rel, frm, evs)  # card 9: a consumed decision answer is acked to the server
    return 0


def _ack_decisions(rel: dict, frm: str, evs: list[dict]) -> None:
    """Card 9 delivery receipt: this helper session consumed decision answers -> tell the server
    (POST relay/ack) so the asking card shows "delivered to the helper". Best effort, never raises,
    never logs content; local relay is a no-op (remote_ack handles both)."""
    ids = [str(e.get("id")) for e in evs
           if isinstance(e, dict) and e.get("type") == "decision" and e.get("id")]
    if not ids:
        return
    try:
        vrelayops.remote_ack(rel, frm, ids)
    except (vrelayops.RelayError, OSError) as e:
        print(f"[warn] delivery ack not sent ({type(e).__name__})", file=sys.stderr, flush=True)


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
    p_join = sub.add_parser(
        "join", help="join a voicehook.ai call as an agent",
        epilog=__doc__[__doc__.index("Activity log (0.13.0)"):],
        formatter_class=argparse.RawDescriptionHelpFormatter)
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
        "--activity-due", type=float, default=None, metavar="SEC",
        help=f"`next` adds activity_due + activity_age_s + activity_hint when no new activity.log line came for SEC seconds while work is in progress (default {vactivity.ACTIVITY_DUE_S:g}, env {vactivity.ACTIVITY_DUE_ENV}; 0 = off). Silent while the Claude Code hook writes lines.",
    )
    p_join.add_argument(
        "--stale-error", type=float, default=None, metavar="SEC",
        help=f"`next` adds stale_error {{status_age_s, activity_age_s, message}} (null age = fresh) on EVERY output while you are active and your board or activity log is older than SEC seconds (default {relay.STALE_ERROR_S:g}, env {relay.STALE_ERROR_ENV}; 0 = off).",
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
    p_join.add_argument(
        "--quiet", action="store_true", default=False,
        help=f"0.14.0: no join hint (the one line 'joined via voicehook.ai ...' for you, never "
             f"spoken; env {QUIET_ENV}=1 works too).",
    )
    p_join.add_argument(
        "--data-channel", dest="data_channel", action="store_true", default=False,
        help="0.24.1: join as a pure DATA CHANNEL. A user turn then wakes this session (lets "
             "`listen` return / `next` wake you) only when it is addressed to you by name or "
             "mention, or Delta routes it to you (routed_by_delta); your own decisions and "
             "helper_decisions wake you regardless. Broad turns (to everyone, or a passive "
             "followup) stay readable in the durable spool as context (since_last) but do NOT "
             "wake you and do NOT trigger the Stop-hook 'nobody listening' nudge. Without the "
             "flag every addressed-to-you turn wakes as before.",
    )
    # one-shot commands against a running join
    def _add_session(p, answer_wait: bool = False):
        p.add_argument("--session", default=None, metavar="SLUG",
                       help="<slug>/<identity> of the running join (a slug alone is enough when only one join runs in that room); needed only when several joins run.")
        if answer_wait:  # decide: `--wait` alone = wait for the answer (0.16.0)
            p.add_argument("--wait", type=float, default=30.0, nargs="?", const=WAIT_ANSWER,
                           metavar="SEC",
                           help="--wait SEC: wait up to SEC seconds for the join to come up "
                                "(default 30). --wait alone (0.16.0, helper session): wait for "
                                "the answer (up to --timeout) and add it as \"decision\".")
            return
        p.add_argument("--wait", type=float, default=30.0, metavar="SEC",
                       help="wait up to SEC seconds for the join to come up (default 30).")
    p_say = sub.add_parser(
        "say", help="speak one line through voice-ai in the running join",
        description="Text wird vorgelesen: ganze, natürliche Sätze. The text is read aloud on the "
                    "phone: speak whole, natural sentences, 1-2 short ones, most important first. Local "
                    "style modes (terse, caveman, telegram style) do not apply here; no markdown, lists, "
                    "URLs, code or emoji.")
    p_say.add_argument("text", nargs="+",
                       help="text to speak, read aloud as is: ganze, natürliche Sätze ('-' reads it from stdin)")
    p_say.add_argument("--mode", choices=_SAY_MODES, default=SAY_DEFAULT_MODE,
                       help="append (default): queue at the end, starts right after the running say "
                            "(a running say is never cut). overwrite: replace your OWN not-started (queued) "
                            "says at their place, else append; never another operator's. revise: like "
                            "overwrite, plus operator.revise to you only listing the replaced texts; nothing "
                            "is held or cut.")
    p_say.add_argument("--urgent", action="store_true", default=False,
                       help="priority urgent: interrupt whoever is speaking (Delta or another operator) "
                            "and go first. Use sparingly.")
    p_say.add_argument("--shape-preset", default=None, metavar="NAME", choices=vvisual.PRESETS,
                       help="draw this preset in the ring while the say is spoken (field shape): "
                            + " ".join(vvisual.PRESETS))
    p_say.add_argument("--shape-json", default=None, metavar="JSON",
                       help="full shape for the say (preset/polygon/path/multi, see `show --help`)")
    p_say.add_argument("--emotion", default=None, metavar="LABEL",
                       help="tint of the ring for this say: " + " ".join(vvisual.EMOTIONS)
                            + " (or JSON {label, valence, arousal})")
    _add_session(p_say)
    p_next = sub.add_parser("next", help="block until the next finalized user turn (or operator.revise); prints one JSON line")
    p_next.add_argument("--timeout", type=float, default=60.0, metavar="SEC",
                        help="give up after SEC seconds and print {\"type\":\"timeout\"} (default 60; 0 = only return what is queued).")
    _add_session(p_next)
    listen_help = (
        "0.21.4: the gapless listen loop: repeats `next` and prints ONE JSON line "
        "on the first event that needs you (user, decision, helper_decision, voicebot_left, "
        "missed, status_request, revise, call_end, data), then exits 0 (3 = join ended). Run it "
        "with the harness's background mode (Claude Code: run_in_background), never detached "
        "with & or nohup: the exit is what wakes you. Start it again after handling the event. "
        "0.21.5: it reads the join's event spool with a cursor (an event is acked after the "
        "print, so a killed listen loses none); you stay present in the call while you work "
        "without it, the join holds the presence itself. 0.21.7: " + vmidturn.RULE + " New user "
        "turns and answers also reach you mid-turn (PostToolUse hook, VOICEHOOK_MIDTURN_EVENTS=0 "
        "off).")
    p_listen = sub.add_parser("listen", help=listen_help, description=listen_help)
    p_listen.add_argument("--timeout", type=float, default=0.0, metavar="SEC",
                          help="give up after SEC seconds with {\"type\":\"timeout\"} "
                               "(default 0 = wait until an event)")
    p_listen.add_argument("--poll", type=float, default=LISTEN_POLL_S, metavar="SEC",
                          help=f"timeout of each single `next` (default {LISTEN_POLL_S:g})")
    _add_session(p_listen)
    p_says = sub.add_parser(
        "says", help="last state of your own says (operator.say_status from the voicebot: "
        "sent/queued/spoken/interrupted/requeued/replaced/covered) as JSON")
    _add_session(p_says)
    p_leave = sub.add_parser("leave", help="end the running join cleanly")
    p_leave.add_argument("--say", default=None, metavar="TEXT", help="goodbye line spoken before leaving")
    _add_session(p_leave)
    p_rejoin = sub.add_parser(
        "rejoin", help="restart the running join in place (os.execve, same mechanism as "
        "self-update) so identity (slug/label/model), queue/spool and the status board are kept "
        "- instead of leaving and re-joining. A rejoin mid-call is allowed (there may be a short "
        "audio gap while the process swaps). With several local joins: --session is required.")
    _add_session(p_rejoin)
    p_status = sub.add_parser(
        "status", help="no args: show the running join's state as JSON; with text/flags: send "
        "your status board (operator.status, replaces the last one, never spoken). 0.21.5 "
        "Klartext (a warning otherwise): every item names the area and what happens, no bare "
        f"numbers or abbreviations; bad: '{vplain.BAD_ITEM}', good: '{vplain.GOOD_ITEM}'")
    p_status.add_argument("text", nargs="?", default=None,
                          help="what you are doing right now, one sentence ('' = finished, clears)")
    p_status.add_argument("--doing", default=None, action=_SeqAction, help="current task (same as TEXT)")
    p_status.add_argument("--open", action=_SeqAction, default=[], metavar="TASK", help="open task (repeatable)")
    p_status.add_argument("--done", action=_SeqAction, default=[], metavar="TASK", help="finished task (repeatable)")
    p_status.add_argument("--faq", action=_SeqAction, default=[], metavar="'FRAGE::ANTWORT'",
                          help="a question the user will likely ask next + your answer, split on "
                          "the first '::' (repeatable, 200 chars each). It belongs to the item it "
                          "follows (--doing/--open/--done, else TEXT): max "
                          f"{relay.ITEM_FAQ_MAX} per item, in board field meta (0.22.2: no cap "
                          "per call, e.g. doing + 2 open + 1 done with 3 each). Refresh it on "
                          "every board update")
    p_status.add_argument("--context", default=None, action=_SeqAction, metavar="TEXT",
                          help=f"0.21.4: hidden background of the item it follows (--doing/--open/"
                               f"--done, else TEXT), max {relay.ITEM_CONTEXT_MAX} chars: never shown, "
                               "the voicebot reads it. Board field meta {doing, open[], done[]} = "
                               "{context, faq[{q, a}]} per item")
    p_status.add_argument("-f", "--file", default=None, metavar="JSON",
                          help="board file {doing, open[], done[]}; flags add to it")
    p_status.add_argument("--waiting", default=None, metavar="TEXT",
                          help="0.21.6: you wait for the human: the board shows TEXT as \"wartet auf "
                               "dich\" (state frage) with its decision. Needs --question (asked "
                               "right away, plus --context) or --decision ID (an open one). "
                               + vwait.RULE + " A waiting text in TEXT/--doing is refused (exit 2)")
    p_status.add_argument("--question", default=None, metavar="TEXT",
                          help="0.21.6, with --waiting: the decision to ask (Klartext rule, "
                               "--context mandatory)")
    p_status.add_argument("--decision", default=None, metavar="ID",
                          help="0.21.6, with --waiting: an open decision (id d + 8 hex, or #number)")
    p_status.add_argument("--project", default=None, metavar="NAME",
                          help="0.21.0: your project (1-40 chars, e.g. voicehook, salesengine, kunden, "
                               "sonstiges), sent as board field project; overrides the project the "
                               "server derives for the focus filter. Also over the relay (helper)")
    _add_session(p_status)
    p_show = sub.add_parser(
        "show", help="draw a shape in the ring (operator.visual), or 0.21.0 --card: a text, code, "
                     "link or image card on the call page",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=SHOW_HELP)
    p_show.add_argument("--preset", action="append", default=[], metavar="NAME",
                        choices=vvisual.PRESETS, help="preset shape (repeatable): "
                        + " ".join(vvisual.PRESETS))
    p_show.add_argument("--polygon", action="append", default=[], metavar="'x,y x,y ...'",
                        help="own polygon, points 0..1 (0,0 = top left), closed (repeatable)")
    p_show.add_argument("--open", action="store_true", default=False,
                        help="--polygon is an open line, not closed")
    p_show.add_argument("--path", action="append", default=[], metavar="D",
                        help="SVG path, only M L Q C Z, numbers 0..1 (repeatable)")
    p_show.add_argument("--json", default=None, metavar="SHAPE",
                        help="full shape JSON (not combinable with --preset/--polygon/--path)")
    p_show.add_argument("--label", default=None, metavar="TEXT",
                        help=f"short caption, max {vvisual.LABEL_MAX} chars")
    p_show.add_argument("--hold-ms", type=int, default=None, metavar="N",
                        help=f"how long the shape stays, {vvisual.HOLD_MS_MIN}..{vvisual.HOLD_MS_MAX} ms "
                             "(default: server)")
    p_show.add_argument("--emotion", default=None, metavar="LABEL",
                        help="tint of the ring: " + " ".join(vvisual.EMOTIONS))
    # 0.21.0 cards on the call page (same as bridge /api/bridge/show, MCP show)
    p_show.add_argument("--card", default=None, choices=vcards.KINDS,
                        help="0.21.0: put a card on the call page instead of a ring shape: "
                             + " ".join(vcards.KINDS) + " (never spoken)")
    p_show.add_argument("--title", default=None, metavar="T",
                        help=f"card heading, max {vcards.TITLE_MAX} chars")
    p_show.add_argument("--text", default=None, metavar="T",
                        help=f"card text (code: the code, link: one http(s) URL), max {vcards.TEXT_MAX} "
                             "chars; '-' reads stdin")
    p_show.add_argument("--file", default=None, metavar="PATH",
                        help="text/code/link: UTF-8 text file; image: svg (max 300 KB, no scripts or "
                             "external refs), png, jpeg or webp (max 1 MB)")
    p_show.add_argument("--panel", default=None, choices=vcards.PANELS,
                        help="open on the human's page: cards (card view) board log decisions, "
                             "none = close all; alone or with --card")
    p_show.add_argument("--large", action="store_true", default=False,
                        help="open the card view big in the middle with this card at full size "
                             "(diagrams; implies --panel cards)")
    _add_session(p_show)
    # 0.21.0: control the humans' call page (topic delta.ui / POST /api/bridge/ui)
    p_ui = sub.add_parser(
        "ui", help="control the call page of the humans: open/close/min/medium/large/max a "
        "window, pick a layout, move a window into a slot, focus one project",
        formatter_class=argparse.RawDescriptionHelpFormatter, description=UI_HELP)
    p_ui.add_argument("action", metavar="ACTION",
                      help="open close min medium large max | layout | move | focus")
    p_ui.add_argument("args", nargs="*", metavar="ARG",
                      help="<window> | <preset> | <window> <slot> | <project|all>")
    p_ui.add_argument("--preset", default=None, metavar="P",
                      help="move: the layout preset the slot belongs to")
    _add_session(p_ui)
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
    p_hook = sub.add_parser("hook", help="activity feed: Claude Code hooks (pre-tool-use | post-tool-use | post-tool-use-failure | user-prompt-submit | stop | notification | permission-request | print | install [--settings PATH]); 0.15.0: other sessions report via ~/.voicehook/relay.json; 0.18.0: permission prompts and questions of every session as decisions")
    p_hook.add_argument("action", choices=["pre-tool-use", "post-tool-use", "post-tool-use-failure",
                                           "user-prompt-submit", "stop", "notification",
                                           "permission-request", "session-start", "doctor",
                                           "print", "install"])
    p_hook.add_argument("--settings", default=None, metavar="PATH",
                        help="install: settings.json to merge into (default ~/.claude/settings.json)")
    p_hook.add_argument("--agent", choices=["claude", "codex"], default=None,
                        help="0.20.0 print/install: claude (default, settings.json) or codex "
                             "(hooks.json + notify + MCP server in $CODEX_HOME, backup first)")
    p_hook.add_argument("--codex-home", default=None, metavar="DIR",
                        help="install --agent codex: Codex config dir (default $CODEX_HOME or ~/.codex)")
    # 0.21.0: hooks installed and runnable for sessions started without ~/.local/bin in PATH
    p_doc = sub.add_parser(
        "doctor", help="check the Claude Code hooks: installed in settings.json and runnable under "
        "/bin/sh with a minimal PATH (as a session started by a service sees it); exit 1 + fix hint")
    p_doc.add_argument("--settings", default=None, metavar="PATH",
                       help="settings.json to check (default ~/.claude/settings.json)")
    # 0.20.0 local MCP server for agents without Claude Code hooks (Codex CLI, ...)
    p_mcp = sub.add_parser(
        "mcp", help="local MCP server over stdio: status, decide, next, activity for this "
        "session (own join or helper via ~/.voicehook/relay.json); Codex: hook install --agent codex")
    p_mcp.add_argument("--agent", default="", metavar="NAME",
                       help="agent name for the default session label (e.g. codex)")
    # 0.13.0: manual activity line for agents without the hook
    p_act = sub.add_parser(
        "activity", help="append one line `HH:MM:SS note: <text>` to the activity log of the "
        "running join (for agents without the Claude Code hook); send your own short status "
        "line 1:1, 3-8 words, in plain language (0.21.5: no bare numbers or abbreviations)")
    p_act.add_argument("text", nargs="+", help="what is happening now ('-' reads it from stdin); "
                       "scrubbed, capped at 120 chars; no paths, secrets or personal data")
    p_act.add_argument("--project", default=None, metavar="NAME",
                       help="0.21.0: your project (1-40 chars), sent as field project on this and "
                            "every later operator.activity of the join (focus filter)")
    p_act.add_argument("--context", default=None, metavar="TEXT",
                       help=f"0.21.4: hidden background of this line (max {relay.ITEM_CONTEXT_MAX} "
                            "chars), never shown, the voicebot reads it")
    p_act.add_argument("--faq", action="append", default=[], metavar="'FRAGE::ANTWORT'",
                       help=f"0.21.4: a likely follow-up question + prepared answer for this line "
                            f"(repeatable, max {relay.ITEM_FAQ_MAX})")
    _add_session(p_act)
    # Decision Board: a question only the human can decide (yes/no or 2-3 options). The
    # result comes back as a `next` event type "decision". Create, change or delete.
    p_dec = sub.add_parser(
        "decide", help="ask the human a decision (Decision Board, not the status board): "
        "yes/no or 2-3 options; the answer arrives as a `next` event type decision. 0.21.5 "
        "Klartext (else exit 2, nothing sent): a whole sentence that names the product or "
        "area, what happens and why, ending with ?; no bare PR/ticket numbers, digit chains "
        "or unexplained abbreviations (SE, WF, RLS, CI ... only with the long form); "
        f"--context is mandatory. Bad: '{vplain.BAD_EXAMPLE}' Good: '{vplain.GOOD_EXAMPLE}'. "
        f"0.21.6: {vwait.RULE} ({vwait.RULE_EN})")
    p_dec.add_argument("--question", default=None, metavar="TEXT",
                       help="the question, max 160 chars (yes/no when no --option is given). "
                            "Only a real decision the human can make from the board alone, in "
                            "plain language (0.21.5, checked): a whole sentence with product or "
                            "area, what happens and why, ending with ?, never 'Weiter?'/"
                            f"'Freigabe?'/'{vplain.BAD_EXAMPLE}'; good: '{vplain.GOOD_EXAMPLE}'. "
                            "Give --context (what, why, what yes/no does)")
    p_dec.add_argument("--option", action="append", default=[], metavar="TEXT",
                       help="an answer option, max 40 chars (give 2 or 3; repeatable)")
    p_dec.add_argument("--recommend", type=int, default=None, metavar="N",
                       help="index of the recommended option, 0-based")
    p_dec.add_argument("--id", default=None, metavar="ID",
                       help="change or delete this decision (d + 8 hex), only while still open")
    p_dec.add_argument("--delete", action="store_true", default=False,
                       help="delete the decision given by --id")
    p_dec.add_argument("--context", default=None, metavar="TEXT",
                       help=f"0.21.0, mandatory since 0.21.5 for a new decision: background for Delta, never shown to the human "
                            f"(max {DECIDE_CONTEXT_MAX} chars): 2-4 plain sentences, what it is about, why, "
                            "what happens on yes, what on no. With --id: change it ('' clears)")
    p_dec.add_argument("--faq", action="append", default=[], metavar="'FRAGE::ANTWORT'",
                       help=f"0.21.4: a likely follow-up question of the human + prepared answer "
                            f"(repeatable, max {relay.ITEM_FAQ_MAX}), hidden, for the voicebot")
    p_dec.add_argument("--recent", default=None, metavar="TEXT",
                       help=f"0.21.0: the last turn of the asking session, one short line (max "
                            f"{DECIDE_RECENT_MAX} chars)")
    p_dec.add_argument("--list", action="store_true", default=False,
                       help="0.21.4: list the open and recent decisions (number, asking label, "
                            "question, options, hidden context) instead of asking; same as `board`")
    p_dec.add_argument("--timeout", type=float, default=120.0, metavar="SEC",
                       help="0.16.0, with --wait (no SEC): wait up to SEC for the answer (default 120)")
    _add_session(p_dec, answer_wait=True)
    p_sd = sub.add_parser(
        "send-data", help="0.21.4: agent-to-agent data (topic operator.data, like MCP send_data): "
        "JSON, text or a file to another operator agent of the call, never spoken, never to the "
        "humans or the voicebot. Bridge: inline up to ~15 KB, larger files via the upload "
        "(10 MB); WebRTC: inline only")
    p_sd.add_argument("--to", required=True, metavar="LABEL|IDENTITY|*",
                      help="the target agent's label or identity, '*' = every other agent")
    g_sd = p_sd.add_mutually_exclusive_group(required=True)
    g_sd.add_argument("--json", default=None, metavar="JSON", help="a JSON value")
    g_sd.add_argument("--text", default=None, metavar="TEXT", help="a text ('-' = stdin)")
    g_sd.add_argument("--file", default=None, metavar="PATH", help="a file (max 10 MB)")
    p_sd.add_argument("--name", default=None, metavar="N", help="file / item name")
    p_sd.add_argument("--mime", default=None, metavar="M", help="mime type, e.g. text/csv")
    p_sd.add_argument("--id", default=None, metavar="ID", help="own message id [A-Za-z0-9._-]")
    _add_session(p_sd)
    p_send = sub.add_parser(
        "send", help="0.24.0: hand a directed task to ANOTHER agent session of this machine "
        "(Phase 1, local only): `send --to <label> \"<Auftrag>\"`. Durable in that session's "
        "inbox (it reads it via relay-next / listen / its hook) and an instant wake if it runs "
        "a join; it acks back so your `tasks` shows it accepted. No broadcast (directed only)")
    p_send.add_argument("--to", required=True, metavar="LABEL",
                        help="the target session's label (name), case-insensitive")
    p_send.add_argument("text", metavar="TASK", help="the task ('-' = stdin)")
    p_tasks = sub.add_parser(
        "tasks", help="0.24.0: the agent-to-agent tasks of THIS session, reconstructed from the "
        "inbox on disk: the ones you sent that are still open ('Auftrag offen bei X seit MM:SS'), "
        "or --to-me the ones addressed to you still waiting to be consumed")
    p_tasks.add_argument("--to-me", action="store_true", default=False,
                         help="list the tasks addressed TO this session (incoming), not the ones it sent")
    for dcmd, what in (("delta-rule", "a standing conversation rule (how Delta talks or behaves)"),
                       ("delta-note", "a fact Delta knows and answers from")):
        p_dn = sub.add_parser(
            dcmd, help=f"0.23.0: {what}, for the rest of the call: add TEXT | list | rm ID. Silent, "
            "never spoken: it goes to the end of Delta's context and applies from his next turn "
            f"(his core rules win on conflict). Only the agent in the call, never a helper. Max "
            f"{'20 rules' if dcmd == 'delta-rule' else '30 facts'}, {DELTA_TEXT_MAX} characters each")
        p_dn.add_argument("action", choices=("add", "list", "rm"))
        p_dn.add_argument("arg", nargs="*", default=[], metavar="TEXT|ID",
                          help="add: the text ('-' = stdin); rm: the id from list")
        p_dn.add_argument("--persist", action="store_true", default=False,
                          help="mark for a later account memory (accepted, no effect yet)")
        _add_session(p_dn)
    p_board = sub.add_parser(
        "board", help="0.21.4: what is on the call's boards (read-only overview for the main "
        "agent): open and recent decisions of every agent and helper (number, asking label, "
        "question, options, hidden context, answer, age) from the server, plus your own board "
        "and every helper board of the room (name, doing/open/done, age of the last update)")
    p_board.add_argument("--open-only", action="store_true", default=False,
                         help="only the open decisions")
    _add_session(p_board)
    # 0.16.0: answers for a helper session (decide over relay.json)
    p_rn = sub.add_parser(
        "relay-next", help="0.16.0 helper session (not itself in the call): print the answers "
        "to your own decide questions as JSON lines (type decision; 0.22.0 also type user_turn: the "
        "human spoke to you by name in the call); {\"type\":\"timeout\"} if none")
    p_rn.add_argument("--timeout", type=float, default=25.0, metavar="SEC",
                      help="wait up to SEC seconds for an answer (default 25; 0 = only what is there)")
    # 0.17.0 helper access (server /api/helper/*): data only, no voice, no join
    p_hc = sub.add_parser(
        "helper-credential", help="print a helper credential of the running join as JSON for "
        "ANOTHER machine; pipe it into ssh: `voicehook-agent helper-credential --label crown | "
        "ssh crown 'voicehook-agent helper-import --as crown'` (never paste it into a chat)")
    p_hc.add_argument("--label", default=None, metavar="NAME",
                      help="the helper machine, shown as 'Helfer NAME: ...' (max 22 chars)")
    p_hc.add_argument("--rotate", action="store_true", default=False,
                      help="invalidate every earlier helper credential of this call (WebRTC join)")
    _add_session(p_hc)
    # 0.21.0: speech recognition vocabulary of the call's payer (add only)
    p_vo = sub.add_parser(
        "vocab", help="0.21.0 teach the call's speech recognition a word it mishears: "
        "`vocab add \"Trello\" --wrong \"Erzähli\"`; adds to the vocabulary of the account that "
        "pays for the call (add only, the user manages it on /konto); max 20 per request, 100 per call")
    p_vo.add_argument("action", choices=["add"], help="add (the only action: agents never delete)")
    p_vo.add_argument("term", help="the correct word or short phrase, max 50 chars")
    p_vo.add_argument("--wrong", action="append", default=[], metavar="TEXT",
                      help="a known mishearing of TERM (repeatable); corrected in every transcript")
    _add_session(p_vo)
    # notify the human outside the call (server voicehook-v4 notify_me / call_me, account rules)
    for name, kind, what in (
            ("notify-me", "notify_me", ("text the human who owns the call: SMS with a single-use "
                                        "10-min link back into the call, a phone call follows "
                                        "after 2 min if unopened")),
            ("call-me", "call_me", ("phone the human who owns the call on their confirmed "
                                    "number; blocks until the call ends"))):
        p_ph = sub.add_parser(name, help=f"{what}; the account's rules decide (refused -> why). "
                              "Prints the server's JSON")
        p_ph.add_argument("--reason", required=True, choices=PHONE_REASONS,
                          help="why: done, blocked or decision")
        p_ph.add_argument("--message", default=None, metavar="TEXT",
                          help=f"optional short text, max {PHONE_MESSAGE_MAX[kind]} chars")
        _add_session(p_ph)
    p_hi = sub.add_parser(
        "helper-import", help="read a helper credential (JSON) from stdin and write it for ONE "
        "helper session: ~/.voicehook/helpers/<name>.json (0600, 0.22.2; was the shared "
        "relay.json). Then `status`, `activity`, `decide`, `next`/`relay-next` and the hooks "
        "of that session feed the call under its own sender")
    p_hi.add_argument("--as", dest="as_name", default=None, metavar="NAME",
                      help="name of the entry, bound to no session: a session uses it when its "
                      "VOICEHOOK_SESSION_LABEL is NAME (or it is the only unbound entry). "
                      "Without --as: name = VOICEHOOK_SESSION_LABEL or the credential's label, "
                      "bound to the agent session that runs the import")
    # 0.13.0: bridge session for the hook (Quickstart A, curl bridge join)
    p_bs = sub.add_parser(
        "bridge-session", help="store {base, session} of a curl bridge join (Quickstart A) in "
        "~/.voicehook/bridge-session.json (0600) so the Claude Code hook POSTs activity lines "
        "to /api/bridge/activity; --clear on leave/ended")
    p_bs.add_argument("--save", default=None, metavar="JSON",
                      help="the /api/bridge/join answer (file, '-' = stdin); only the session is kept")
    p_bs.add_argument("--base", default=None, metavar="URL",
                      help="server base, e.g. https://voicehook.ai (default $VOICEHOOK_API_BASE or https://voicehook.ai)")
    p_bs.add_argument("--clear", action="store_true", default=False, help="delete the file")
    p_bs.add_argument("--no-relay", action="store_true", default=False,
                      help="0.15.0: do not mint ~/.voicehook/relay.json for the other sessions")
    args = ap.parse_args(argv)
    if args.cmd == "bridge-session":
        sys.exit(_run_bridge_session(args))
    if args.cmd == "self-update":
        sys.exit(_run_self_update(args.dry_run))
    if args.cmd == "doctor":
        sys.exit(vactivity.main(["doctor"] + (["--settings", args.settings] if args.settings else [])))
    if args.cmd == "hook":
        hook_argv = [args.action] + (["--settings", args.settings] if args.settings else []) \
            + (["--agent", args.agent] if args.agent else []) \
            + (["--codex-home", args.codex_home] if args.codex_home else [])
        sys.exit(vactivity.main(hook_argv))
    if args.cmd == "mcp":
        from . import mcpserver
        sys.exit(mcpserver.main(["--agent", args.agent] if args.agent else []))
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
        updater = _Updater(args.no_self_update, json_mode=args.json)
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
                activity_due=args.activity_due, stale_error=args.stale_error,
                voice=args.voice, updater=updater, quiet=args.quiet,
                data_channel=args.data_channel,
            ))
        except KeyboardInterrupt:
            rc = 130
        if rc == RC_RESTART:
            _restart_after_update(argv, updater)
            rc = 1  # only reached when the exec failed
        sys.exit(rc)
    if args.cmd == "helper-import":
        sys.exit(_run_helper_import(args))
    if args.cmd in ("say", "next", "says", "leave", "rejoin", "status", "activity", "decide",
                    "helper-credential", "vocab", "ui", "notify-me", "call-me", "board", "send-data",
                    *DELTA_KINDS):
        sys.exit(_run_client(args))
    if args.cmd == "show":
        sys.exit(_run_client(args))
    if args.cmd == "relay-next":
        sys.exit(_run_relay_next(args))
    if args.cmd == "send":
        sys.exit(_run_send(args))
    if args.cmd == "tasks":
        sys.exit(_run_tasks(args))
    if args.cmd == "listen":
        sys.exit(_run_listen(args))
    if args.cmd == "log-summary":
        try:
            rc = asyncio.run(_run_log_summary(args))
        except KeyboardInterrupt:
            rc = 130
        sys.exit(rc)


if __name__ == "__main__":
    main()
