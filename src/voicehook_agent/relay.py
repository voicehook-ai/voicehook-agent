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
})


def is_terminal_disconnect(reason_name: str) -> bool:
    return (reason_name or "").upper() in TERMINAL_DISCONNECT_REASONS


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
    return {"type": "user", "role": "user", "text": text,
            "ts": now if now is not None else time.time()}


def revise_event(payload: dict, now: float | None = None) -> dict:
    """Queue event for an incoming operator.revise (merge + resend with
    mode=overwrite within 8 s)."""
    ev = {"type": "revise", "role": "system", "text": payload.get("text", ""),
          "ts": now if now is not None else time.time()}
    for k in ("unspoken", "new"):
        if k in payload:
            ev[k] = payload[k]
    return ev


# ----- 0.7.0: status board, status_request, latency hint ---------------------------
STATUS_STALE_S = 300.0     # board older than this and the user spoke since -> status_stale
LATENCY_WARN_S = 8.0       # user turn delivered by `next` -> next `say` slower -> latency_warning
LATENCY_HINT = "delegate slow work, keep main loop free"


def status_request_event(payload: dict, now: float | None = None) -> dict:
    """Queue event for operator.status_request: the user asked what you are doing;
    answer at once with `voicehook-agent status --doing ...`."""
    return {"type": "status_request", "role": "system",
            "text": str((payload or {}).get("text", ""))[:200],
            "ts": now if now is not None else time.time()}


def build_board(text: str | None = None, doing: str | None = None,
                open_: list[str] | None = None, done: list[str] | None = None,
                file_obj: dict | None = None) -> dict:
    """operator.status payload {doing, open[], done[]}. The server caps it (600 chars)."""
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
    return base


class TurnClock:
    """Measures how fast the agent answers and whether its board is stale.

    `delivered()` when `next` hands out a user turn, `said()` on every `say`,
    `board()` on every status push, `user()` on every queued user turn.
    `hints()` (called by `next`) returns the extra keys for that reply."""

    def __init__(self, now: float | None = None) -> None:
        t = time.monotonic() if now is None else now
        self.delivered_at: float | None = None
        self.board_at = t          # join counts as the start
        self.user_at: float | None = None
        self._warn: float | None = None

    @staticmethod
    def _t(now: float | None) -> float:
        return time.monotonic() if now is None else now

    def delivered(self, now: float | None = None) -> None:
        self.delivered_at = self._t(now)

    def said(self, now: float | None = None) -> None:
        if self.delivered_at is not None:
            took = self._t(now) - self.delivered_at
            if took > LATENCY_WARN_S:
                self._warn = took
            self.delivered_at = None

    def board(self, now: float | None = None) -> None:
        self.board_at = self._t(now)

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
        return out
