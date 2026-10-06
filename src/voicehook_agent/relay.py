"""Pure, side-effect-free relay logic for voicehook-agent.

This module holds the parts of the CLI that have no LiveKit / network / asyncio
dependency so they can be unit-tested in isolation:

  * LineBuffer       — newline-tolerant stdin splitting (#11)
  * TurnNotifier      — finalized-user-turn dedup + role filtering (#12)
  * EchoSuppressor    — drop our own relayed TTS on the operator stream (#10)
  * SayTracker        — seq/timestamp tagging + TTL / supersede drop (#9)
  * backoff_delays    — reconnect backoff schedule (#6)

Keeping these here means a test suite can exercise the tricky edge cases
(partial lines, dedup, TTL expiry) without standing up a room.
"""
from __future__ import annotations

import hashlib
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator


# --------------------------------------------------------------------------- #
# #11 — FIFO / control-message newline tolerance
# --------------------------------------------------------------------------- #
class LineBuffer:
    """Splits an incoming byte/str stream into complete lines while NOT silently
    swallowing a trailing chunk that lacks a final newline.

    Usage::

        buf = LineBuffer()
        for line in buf.feed("a\\nb\\nc"):   # yields "a", "b"  ("c" is held)
            handle(line)
        # at EOF:
        for line, complete in buf.flush():    # yields ("c", False)
            if not complete:
                warn(...)
            handle(line)

    The held tail is the bug behind #11: a control JSON written without a
    trailing newline used to be invisible forever. ``flush()`` surfaces it
    (with ``complete=False`` so the caller can warn) instead of dropping it.
    """

    def __init__(self) -> None:
        self._buf = ""

    def feed(self, chunk: str) -> Iterator[str]:
        """Add ``chunk`` and yield every COMPLETE (newline-terminated) line.
        A trailing partial line is retained for the next feed/flush."""
        if not chunk:
            return
        self._buf += chunk
        while True:
            nl = self._buf.find("\n")
            if nl < 0:
                break
            line, self._buf = self._buf[:nl], self._buf[nl + 1 :]
            yield line.rstrip("\r")

    def flush(self) -> Iterator[tuple[str, bool]]:
        """Yield any retained tail at EOF as ``(line, complete=False)``.

        ``complete`` is always ``False`` here because by definition a flushed
        tail never had its terminating newline. Empty / whitespace-only tails
        are not yielded (nothing to warn about)."""
        tail = self._buf
        self._buf = ""
        tail = tail.rstrip("\r")
        if tail.strip():
            yield tail, False

    @property
    def pending(self) -> str:
        return self._buf


# --------------------------------------------------------------------------- #
# #12 — NOTIFY + WAKE on finalized user turns
# --------------------------------------------------------------------------- #
@dataclass
class WakeDecision:
    wake: bool
    reason: str = ""
    payload: dict | None = None


class TurnNotifier:
    """Decides whether an incoming transcript event should fire a wake marker.

    Rules (#12):
      * Only FINALIZED turns wake (a ``final``/``is_final`` flag that is present
        and falsey suppresses the wake; absence is treated as final, since the
        existing transcript topic only emits finals today).
      * ``wake_only_user=True`` → only role=user wakes (role=agent is the
        agent's own echoed TTS → must not wake, else infinite loop, see #10).
      * Dedup: identical (role, normalized-text) back-to-back is fired once.
    """

    def __init__(self, wake_only_user: bool = True) -> None:
        self.wake_only_user = wake_only_user
        self._last_sig: tuple[str, str] | None = None

    @staticmethod
    def _is_final(payload: dict) -> bool:
        for k in ("final", "is_final", "isFinal"):
            if k in payload:
                return bool(payload[k])
        return True  # transcript topic emits finals today

    def consider(
        self,
        role: str,
        text: str,
        payload: dict | None = None,
        room: str | None = None,
        now: float | None = None,
    ) -> WakeDecision:
        payload = payload or {}
        if not self._is_final(payload):
            return WakeDecision(False, "not-final")
        if self.wake_only_user and role != "user":
            return WakeDecision(False, f"role={role}-filtered")
        norm = (text or "").strip()
        if not norm:
            return WakeDecision(False, "empty")
        sig = (role, norm)
        if sig == self._last_sig:
            return WakeDecision(False, "dup")
        self._last_sig = sig
        payload_out = {
            "role": role,
            "text": norm,
            "room": room,
            "timestamp": now if now is not None else time.time(),
        }
        return WakeDecision(True, "final-turn", payload_out)


# --------------------------------------------------------------------------- #
# #10 — echo suppression
# --------------------------------------------------------------------------- #
class EchoSuppressor:
    """Suppresses the operator-stream echo of text we just pushed via operator.say.

    When ``--suppress-echo`` is on, an incoming ``role=agent`` transcript whose
    text matches something we recently sent is dropped (it's our own relayed
    TTS coming back). A small ring of recent sends is kept; matches are
    consumed so a genuine repeat later still shows.
    """

    def __init__(self, enabled: bool, window: int = 16) -> None:
        self.enabled = enabled
        self._window = window
        self._recent: list[str] = []

    @staticmethod
    def _norm(text: str) -> str:
        return " ".join((text or "").split()).lower()

    def record_sent(self, text: str) -> None:
        if not self.enabled:
            return
        n = self._norm(text)
        if not n:
            return
        self._recent.append(n)
        if len(self._recent) > self._window:
            self._recent.pop(0)

    def should_suppress(self, role: str, text: str) -> bool:
        if not self.enabled or role != "agent":
            return False
        n = self._norm(text)
        if n in self._recent:
            self._recent.remove(n)  # consume one match
            return True
        return False


# --------------------------------------------------------------------------- #
# #9 — say-pending lock + TTL
# --------------------------------------------------------------------------- #
@dataclass
class PendingSay:
    seq: int
    text: str
    created: float
    topic: str = "operator.say"
    extra: dict = field(default_factory=dict)


class SayTracker:
    """Tags each outgoing operator.say with a monotonically increasing ``seq`` and
    a ``ts``; enforces a TTL and supersede-on-newer-user-turn rule (#9).

    The server today does not ack a say, so "spoken" cannot be observed from
    the CLI. We approximate: a say is dropped (not sent) if, at flush time, it
    is older than ``ttl`` seconds, OR a newer user-turn arrived after it was
    queued (it is now stale relative to the conversation). This is documented
    as a best-effort client-side guard in the README/PR.
    """

    def __init__(self, ttl: float | None = None) -> None:
        self.ttl = ttl
        self._seq = 0
        self._last_user_turn_ts: float = 0.0

    def note_user_turn(self, now: float | None = None) -> None:
        self._last_user_turn_ts = now if now is not None else time.time()

    def tag(self, text: str, topic: str = "operator.say", extra: dict | None = None,
            now: float | None = None) -> PendingSay:
        self._seq += 1
        return PendingSay(
            seq=self._seq,
            text=text,
            created=now if now is not None else time.time(),
            topic=topic,
            extra=dict(extra or {}),
        )

    def is_stale(self, say: PendingSay, now: float | None = None) -> tuple[bool, str]:
        """Returns (stale, reason). A stale say must NOT be published."""
        now = now if now is not None else time.time()
        if self.ttl is not None and (now - say.created) > self.ttl:
            return True, f"ttl-expired(>{self.ttl}s)"
        if self._last_user_turn_ts > say.created:
            return True, "superseded-by-newer-user-turn"
        return False, ""

    def envelope(self, say: PendingSay) -> dict:
        """The wire payload for a say, carrying seq + ts so a future
        server-side ack can correlate (forward-compatible)."""
        env = {"text": say.text, "_seq": say.seq, "_ts": say.created}
        env.update(say.extra)
        return env


# --------------------------------------------------------------------------- #
# #6 — reconnect backoff
# --------------------------------------------------------------------------- #
def backoff_delays(base: float = 1.0, factor: float = 2.0, cap: float = 30.0,
                   attempts: int | None = None) -> Iterator[float]:
    """Yields an exponential backoff schedule capped at ``cap`` seconds.
    If ``attempts`` is None the schedule is infinite (caller decides when to
    stop, i.e. when the host truly leaves / room closes)."""
    d = base
    i = 0
    while attempts is None or i < attempts:
        yield min(d, cap)
        d *= factor
        i += 1


# These disconnect reasons are considered TERMINAL (host left / room closed /
# we initiated) → do NOT reconnect. Anything else is treated as transient. The
# names match livekit.rtc.DisconnectReason.Name(...) output.
TERMINAL_DISCONNECT_REASONS: frozenset[str] = frozenset({
    "CLIENT_INITIATED",
    "ROOM_DELETED",
    "ROOM_CLOSED",
    "PARTICIPANT_REMOVED",
    "USER_REJECTED",
    "USER_UNAVAILABLE",
    # 0.10.1: the server ended the call (topic `call_end`, or /api/token or the
    # bridge answered 410 "call has ended"). Never rejoin, keep-alive or not.
    "CALL_ENDED",
})


def is_terminal_disconnect(reason_name: str) -> bool:
    return (reason_name or "").upper() in TERMINAL_DISCONNECT_REASONS


def is_human_peer(kind: str, attrs: dict | None) -> bool:
    """A real person in the room: a browser/phone participant (kind user or sip)
    that is not an operator agent (``vh.role == "agent"``). The voice-ai worker
    is kind ``agent`` and never counts."""
    if kind not in ("user", "sip"):
        return False
    return (attrs or {}).get("vh.role") != "agent"


# --------------------------------------------------------------------------- #
# #graph — live-context sync (status loop + graph-per-turn)
# --------------------------------------------------------------------------- #
@dataclass
class GraphSnapshot:
    """A read of the operator's live-context file (the "graph").

    ``digest`` is a sha256 of the raw bytes so the CLI can skip unchanged
    pushes; ``text`` is the decoded content pushed verbatim as `operator.persona`
    so the voice-ai always knows "was gerade Phase ist".
    """

    digest: str | None
    text: str | None


def read_graph(path: str) -> GraphSnapshot:
    """Read the graph/status file. Returns (None, None) when missing/unreadable —
    the caller treats that as "no update available" and never crashes the loop."""
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return GraphSnapshot(digest=None, text=None)
    return GraphSnapshot(
        digest=hashlib.sha256(raw).hexdigest(),
        text=raw.decode("utf-8").rstrip(),
    )


@dataclass
class GraphHolder:
    """In-memory slot for the operator's live-context (the "graph").

    Fed by `operator.graph` stdin lines (and optionally seeded from `--graph` at
    connect). The CLI's cadence loop pushes `latest` as `operator.persona` every
    `--graph-interval` seconds — the CLI enforces the cadence, the operator only
    writes its current state whenever it changes.
    """

    latest: str | None = None

    def set(self, text: str | None) -> None:
        self.latest = text


# --------------------------------------------------------------------------- #
# #log-summary — the "2nd micro agent" (rolling call-log digest)
# --------------------------------------------------------------------------- #
@dataclass
class RollingSummary:
    """Rolling digest of the last `max_turns` finalized transcript turns.

    The micro-agent feeds this from the CLI's `--json` transcript stream and
    emits the digest as a `operator.graph` update — so the voice-ai (and the
    operator, when it's back in the loop) knows "was bisher passiert ist" without
    the operator having to do it live.
    """

    max_turns: int = 8
    _turns: list[tuple[str, str]] = field(default_factory=list)
    _last: tuple[str, str] | None = None

    def add(self, role: str, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        role = role if role in ("user", "agent") else "other"
        sig = (role, text)
        if sig == self._last:
            return  # consecutive dup → skip
        self._last = sig
        self._turns.append(sig)
        if len(self._turns) > self.max_turns:
            self._turns = self._turns[-self.max_turns:]

    @property
    def turns(self) -> list[tuple[str, str]]:
        return list(self._turns)

    def deterministic(self) -> str:
        return "\n".join(f"- {r}: {t}" for r, t in self._turns)


def build_summary_prompt(turns: list[tuple[str, str]]) -> str:
    """Prompt for the local LLM: compress the raw turns into a short digest."""
    lines = [f"{role}: {text}" for role, text in turns]
    return (
        "Fasse in maximal 3 kurzen Saetzen zusammen, was im Call besprochen, "
        "entschieden oder als offen markiert wurde. Deutsch, kein Markdown.\n"
        + "\n".join(lines)
    )


# --------------------------------------------------------------------------- #
# persona guard — never overwrite another operator's persona
# --------------------------------------------------------------------------- #
def other_operators(peers: Iterable[tuple[str, str, dict | None]]) -> list[str]:
    """Identities of OTHER operator agents in the room.

    `peers` = (identity, kind_label, attributes) of the REMOTE participants.
    The voicehook server stamps every operator token (`/api/token?invite=1`)
    with the LiveKit attribute ``vh.role=agent``; the built-in voice-ai worker
    is a LiveKit agent participant (kind ``agent``) and the human host has no
    ``vh.role``. So an operator is: ``vh.role == "agent"`` and not the worker.
    """
    out = []
    for identity, kind, attrs in peers:
        if kind == "agent":
            continue
        if (attrs or {}).get("vh.role") == "agent":
            out.append(identity)
    return sorted(out)


def user_turn_event(role: str, text: str, payload: dict | None = None,
                    now: float | None = None) -> dict | None:
    """Queue event for `voicehook-agent next`, or None if this transcript line
    is not a finalized, non-empty user turn."""
    if role != "user":
        return None
    if not TurnNotifier._is_final(payload or {}):
        return None
    text = (text or "").strip()
    if not text:
        return None
    ev = {"type": "user", "role": "user", "text": text,
          "ts": now if now is not None else time.time()}
    speaker = (payload or {}).get("speaker")
    if speaker:  # 0.11.0: v4 names the speaker (user name); old servers: no field
        ev["speaker"] = speaker
    return ev


def foreign_owner(payload: dict, own: set[str] | str | None, key: str = "owner") -> bool:
    """0.11.0 (multi-operator): True if `payload[key]` names another operator.

    The server routes operator.revise / operator.say_status only to the owner of the
    say (destination_identities) and stamps `owner`; operator lines in `transcript`
    carry `op`. Defensive second filter: without the field (old server) nothing is
    foreign."""
    who = payload.get(key) if isinstance(payload, dict) else None
    if not who:
        return False
    mine = {own} if isinstance(own, str) else set(own or ())
    return bool(mine) and who not in mine


def revise_event(payload: dict, now: float | None = None) -> dict:
    """Queue event for an incoming operator.revise: our `--mode revise` replaced own
    not-started says (`unspoken`); if any still matters, send one merged say with
    mode=overwrite."""
    ev = {"type": "revise", "role": "system", "text": payload.get("text", ""),
          "ts": now if now is not None else time.time()}
    for k in ("unspoken", "new"):
        if k in payload:
            ev[k] = payload[k]
    return ev


# ----- 0.7.0: status board, status_request, latency hint ---------------------------
STATUS_STALE_S = 300.0     # board older than this and the user spoke since -> status_stale
STATUS_DUE_S = 45.0        # 0.9.0: board older than this (work in progress) -> status_due
STATUS_DUE_ENV = "VOICEHOOK_STATUS_DUE"
STATUS_CMD = 'voicehook-agent status --doing "<Zwischenstand, ETA>" --open "<offen>" --done "<erledigt>"'
# 0.10.0: every status_due hint also asks for the FAQ (predicted next questions).
FAQ_HINT = ("Welche 3 Fragen stellt der Nutzer wahrscheinlich als Nächstes? "
            'Beantworte sie vorab per --faq "Frage::Antwort"')
STATUS_HINTS = {
    "status_request": "Nutzer fragt nach Stand: Board jetzt aktualisieren: " + STATUS_CMD + ". " + FAQ_HINT,
    "empty": "Board leer, Delta weiss nichts: jetzt setzen: " + STATUS_CMD + ". " + FAQ_HINT,
    "stale": "Board veraltet, Delta antwortet sonst falsch: jetzt aktualisieren: " + STATUS_CMD + ". " + FAQ_HINT,
}
SAY_PROGRESS_HINT = ("Fortschritt angesagt, Board ist aelter: Delta kennt ihn sonst nicht. "
                     "Jetzt: " + STATUS_CMD + ". " + FAQ_HINT)
FAQ_MAX = 6          # pairs per board
FAQ_CHARS = 200      # per question and per answer
_PROGRESS_RX = re.compile(
    r"\b(fertig|erledigt|abgeschlossen|live|deploy\w*|gemerged|merged|done|finished|shipped)\b",
    re.IGNORECASE)


def status_due_seconds(value: float | str | None = None) -> float:
    """N for status_due: explicit value (--status-due), else $VOICEHOOK_STATUS_DUE,
    else STATUS_DUE_S. 0 or less switches the age rule off."""
    for v in (value, os.environ.get(STATUS_DUE_ENV)):
        if v is None or v == "":
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return STATUS_DUE_S


def mentions_progress(text: str) -> bool:
    """`say` text that reports progress (fertig, live, deploye ...)."""
    return bool(_PROGRESS_RX.search(text or ""))


def board_is_empty(board: dict | None) -> bool:
    b = board or {}
    return not (str(b.get("doing") or "").strip() or b.get("open") or b.get("done"))
LATENCY_WARN_S = 8.0       # user turn delivered by `next` -> next `say` slower -> latency_warning
LATENCY_HINT = "delegate slow work, keep main loop free"


def status_request_event(payload: dict, now: float | None = None) -> dict:
    """Queue event for operator.status_request: the user asked what you are doing;
    answer at once with `voicehook-agent status --doing ...`."""
    return {"type": "status_request", "role": "system",
            "text": str((payload or {}).get("text", ""))[:200],
            "ts": now if now is not None else time.time(),
            "hint": STATUS_HINTS["status_request"]}


def parse_faq(items: list[str] | None, warn=None) -> list[dict]:
    """`--faq "Frage::Antwort"` values -> [{"q", "a"}]: split on the FIRST "::",
    both halves stripped, an item with an empty half (or no "::") is skipped with
    a warning, at most FAQ_MAX pairs, q and a capped to FAQ_CHARS each."""
    out: list[dict] = []
    for raw in items or []:
        q, sep, a = str(raw).partition("::")
        q, a = q.strip(), a.strip()
        if not sep or not q or not a:
            if warn is not None:
                warn(f"[warn] --faq {raw!r} skipped: expected \"Frage::Antwort\" with both halves")
            continue
        if len(out) >= FAQ_MAX:
            if warn is not None:
                warn(f"[warn] --faq: more than {FAQ_MAX} pairs, the rest is dropped")
            break
        out.append({"q": q[:FAQ_CHARS], "a": a[:FAQ_CHARS]})
    return out


def build_board(text: str | None = None, doing: str | None = None,
                open_: list[str] | None = None, done: list[str] | None = None,
                file_obj: dict | None = None, faq: list[dict] | None = None) -> dict:
    """operator.status payload {doing, open[], done[]} (+ faq[{q, a}] when given,
    0.10.0). The server caps it (600 chars)."""
    if file_obj is not None:
        if not isinstance(file_obj, dict):
            raise ValueError("board file must hold a JSON object {doing, open, done}")
        base = {"doing": str(file_obj.get("doing", "") or ""),
                "open": [str(x) for x in file_obj.get("open") or []],
                "done": [str(x) for x in file_obj.get("done") or []]}
    else:
        base = {"doing": "", "open": [], "done": []}
    if text is not None:
        base["doing"] = text
    if doing is not None:
        base["doing"] = doing
    base["open"] += list(open_ or [])
    base["done"] += list(done or [])
    base["doing"] = base["doing"].strip()
    if faq:
        base["faq"] = list(faq)
    return base


class TurnClock:
    """Measures how fast the agent answers and whether its board is stale.

    `delivered()` when `next` hands out a user turn, `said()` on every `say`,
    `board()` on every status push, `user()` on every queued user turn.
    `hints()` (called by `next`) returns the extra keys for that reply."""

    def __init__(self, now: float | None = None, due_s: float | None = None) -> None:
        t = time.monotonic() if now is None else now
        self.delivered_at: float | None = None
        self.board_at = t          # join counts as the start
        self.user_at: float | None = None
        self._warn: float | None = None
        # 0.9.0 status_due
        self.due_s = STATUS_DUE_S if due_s is None else due_s
        self.board_set_at: float | None = None   # last real `status` push (None = never)
        self.board_empty = True
        self.board_live = False                  # doing or open set = work in progress
        self.request_at: float | None = None     # open status_request
        self.said_at: float | None = None

    @staticmethod
    def _t(now: float | None) -> float:
        return time.monotonic() if now is None else now

    def delivered(self, now: float | None = None) -> None:
        self.delivered_at = self._t(now)

    def said(self, now: float | None = None, text: str = "") -> dict:
        """Records a `say`; returns {"hint": ...} when the line reports progress
        (fertig, live, deploye ...) but the board was not pushed since the last say
        or is older than due_s."""
        t = self._t(now)
        out: dict = {}
        prev, self.said_at = self.said_at, t
        if mentions_progress(text) and (
                self.board_set_at is None
                or (prev is not None and self.board_set_at < prev)
                or (self.due_s > 0 and t - self.board_set_at > self.due_s)):
            out = {"status_due": True, "status_reason": "say_progress", "hint": SAY_PROGRESS_HINT}
        self._said(t)
        return out

    def _said(self, now: float) -> None:
        if self.delivered_at is not None:
            took = self._t(now) - self.delivered_at
            if took > LATENCY_WARN_S:
                self._warn = took
            self.delivered_at = None

    def board(self, now: float | None = None, board: dict | None = None) -> None:
        t = self._t(now)
        self.board_at = t
        self.board_set_at = t
        self.board_empty = board_is_empty(board)
        b = board or {}
        self.board_live = bool(str(b.get("doing") or "").strip() or b.get("open"))
        self.request_at = None

    def requested(self, now: float | None = None) -> None:
        """operator.status_request arrived: status_due until the next board."""
        self.request_at = self._t(now)

    def status_due(self, now: float | None = None) -> dict:
        """{"status_due": True, "status_reason", "board_age_s", "hint"} or {}.

        Due when a status_request is open, when the board is empty/never set, or
        when it is older than due_s while work is in progress (doing/open set) or
        the user spoke after it. A finished board (only done) does not nag by age."""
        t = self._t(now)
        age = None if self.board_set_at is None else round(t - self.board_set_at, 1)
        if self.request_at is not None:
            reason = "status_request"
        elif self.board_empty:
            reason = "empty"
        elif self.due_s > 0 and age is not None and age > self.due_s and (
                self.board_live or (self.user_at is not None and self.user_at > self.board_set_at)):
            reason = "stale"
        else:
            return {}
        out: dict = {"status_due": True, "status_reason": reason, "hint": STATUS_HINTS[reason]}
        if age is not None:
            out["board_age_s"] = age
        return out

    def user(self, now: float | None = None) -> None:
        self.user_at = self._t(now)

    def hints(self, now: float | None = None) -> dict:
        t = self._t(now)
        out: dict = {}
        if self.delivered_at is not None and t - self.delivered_at > LATENCY_WARN_S:
            self._warn = t - self.delivered_at   # turn never answered with a say
            self.delivered_at = None
        if self._warn is not None:
            out["latency_warning"] = {"seconds": round(self._warn, 1), "hint": LATENCY_HINT}
            self._warn = None
        if self.user_at is not None and self.user_at > self.board_at \
                and t - self.board_at > STATUS_STALE_S:
            out["status_stale"] = True
        out.update(self.status_due(t))
        return out


# ----- 0.9.0: operator.say_status (worker -> operator) ------------------------------
SAY_STATES = ("queued", "spoken", "interrupted", "requeued", "replaced", "covered")
# 0.12.0 (v4 #162, live mode): the say reached the model as context while the user had the
# floor, and Delta already said its content in his answer, so it is not spoken (final).
SAY_NOTES = {"covered": "Info steckt schon in Deltas Antwort, nicht nochmal senden"}
SAY_STUCK_S = 20.0          # queued/requeued longer than this -> hint
SAYS_KEEP = 50              # says remembered for `voicehook-agent says`


class SayStatus:
    """Tracks what the voicebot did with each own `say` (operator.say_status
    {seq, state, spoken_chars}; seq = the CLI's `_seq`). `next` hands out the state
    changes since the last `next` as `say_status`, `says` shows the last state of
    every say, and a say stuck in queued/requeued for SAY_STUCK_S yields a hint."""

    def __init__(self, stuck_s: float = SAY_STUCK_S) -> None:
        self.stuck_s = stuck_s
        self._says: dict[int, dict] = {}
        self._changes: list[dict] = []
        self._hinted: set[tuple[int, str, float]] = set()

    @staticmethod
    def _t(now: float | None) -> float:
        return time.monotonic() if now is None else now

    def sent(self, seq: int, text: str = "", now: float | None = None) -> None:
        t = self._t(now)
        self._says[seq] = {"seq": seq, "state": "sent", "spoken_chars": 0,
                           "text": " ".join(str(text).split())[:80], "at": t, "since": t}
        while len(self._says) > SAYS_KEEP:
            self._says.pop(next(iter(self._says)))

    def update(self, payload: dict, now: float | None = None) -> dict | None:
        """Apply one operator.say_status packet. Only own seqs count (the packet
        goes to the whole room). Returns the compact change or None."""
        if not isinstance(payload, dict):
            return None
        try:
            seq = int(payload.get("seq"))
        except (TypeError, ValueError):
            return None
        state = payload.get("state")
        rec = self._says.get(seq)
        if rec is None or state not in SAY_STATES:
            return None
        try:
            chars = int(payload.get("spoken_chars") or 0)
        except (TypeError, ValueError):
            chars = 0
        if rec["state"] != state:
            rec["since"] = self._t(now)
        rec["state"], rec["spoken_chars"] = state, chars
        change = {"seq": seq, "state": state}
        if state in ("interrupted", "requeued") and chars:
            change["spoken_chars"] = chars
        if state in SAY_NOTES:
            change["note"] = SAY_NOTES[state]
        self._changes.append(change)
        return change

    def take(self, now: float | None = None) -> dict:
        """{"say_status": [...]} plus {"say_hint": ...} for a stuck say, or {}."""
        out: dict = {}
        if self._changes:
            out["say_status"], self._changes = self._changes, []
        stuck = self.stuck(now)
        if stuck:
            out["say_hint"] = stuck
        return out

    def stuck(self, now: float | None = None) -> str | None:
        t = self._t(now)
        for rec in self._says.values():
            key = (rec["seq"], rec["state"], rec["since"])
            if rec["state"] in ("queued", "requeued") and t - rec["since"] > self.stuck_s \
                    and key not in self._hinted:
                self._hinted.add(key)
                return (f"say seq {rec['seq']} haengt seit {round(t - rec['since'])} s in "
                        f"{rec['state']} (Delta spricht oder der Nutzer redet): nicht "
                        "nachschieben; veraltet? `voicehook-agent say --mode overwrite "
                        "\"<Kurzfassung>\"`")
        return None

    def table(self, now: float | None = None) -> list[dict]:
        t = self._t(now)
        rows = []
        for r in self._says.values():
            row = {"seq": r["seq"], "state": r["state"], "spoken_chars": r["spoken_chars"],
                   "age_s": round(t - r["at"], 1), "text": r["text"]}
            if r["state"] in SAY_NOTES:
                row["note"] = SAY_NOTES[r["state"]]
            rows.append(row)
        return rows


AGENT_SAID_MAX = 3          # entries in `agent_said`
AGENT_SAID_CHARS = 400      # total chars in `agent_said`


class AgentSaid:
    """What the voicebot (Delta) said on its own since the last `next` (transcript
    role=agent, never echoes of your own say). `next` hands it out as `agent_said`
    so you do not repeat it and can correct it. Oldest entries drop first."""

    def __init__(self) -> None:
        self._items: list[str] = []

    def add(self, text: str) -> None:
        text = " ".join(str(text or "").split())
        if not text:
            return
        self._items.append(text[:AGENT_SAID_CHARS])
        del self._items[:-AGENT_SAID_MAX]
        while sum(map(len, self._items)) > AGENT_SAID_CHARS and len(self._items) > 1:
            self._items.pop(0)

    def take(self) -> dict:
        """{"agent_said": [...]} (chronological) and reset, or {} when nothing new."""
        if not self._items:
            return {}
        out, self._items = self._items, []
        return {"agent_said": out}
