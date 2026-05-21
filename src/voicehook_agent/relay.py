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

import time
from dataclasses import dataclass, field
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
    """Suppresses the operator-stream echo of text we just pushed via senior.say.

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
    topic: str = "senior.say"
    extra: dict = field(default_factory=dict)


class SayTracker:
    """Tags each outgoing senior.say with a monotonically increasing ``seq`` and
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

    def tag(self, text: str, topic: str = "senior.say", extra: dict | None = None,
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
