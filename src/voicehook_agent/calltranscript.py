"""The call's transcript for this agent: `voicehook-agent transcript` and Delta's
`send_transcript` (server: the worker keeps the call's final lines in memory).

Owner 06.11.2026: "Agent liest den ganzen Call, Delta schickt den Verlauf." An agent that
joined late saw only the lines after its join. Server protocol (data channel, JSON):

  Request  agent -> worker  `operator.transcript_request` {id, since_s?, max_lines?}
  Answer   worker -> ONLY the requester  `operator.transcript`
           {id, part, parts, truncated, total, lines:[{id, t, speaker, role, text,
           interrupted?, cut?}]}, parts of at most ~12 kB, the same id in every part.
           Not an own agent of the owner (vh.owner_agent): {id, error: "not_allowed"}.
  Delta    (tool send_transcript): the same topic WITHOUT id, with by: "delta" and minutes?.

Here: the parts are put together IN MEMORY only (Assembler). A requested transcript goes back
to the waiting `transcript` command (stdout of the calling agent), a Delta one becomes ONE
`next`/`listen` event type "transcript". Privacy: the lines never go into a log, the join's
stdout or a file; the event spool persists only its metadata (spool.VOLATILE).

Stdlib only.
"""
from __future__ import annotations

import secrets
import time

TOPIC_REQUEST = "operator.transcript_request"
TOPIC = "operator.transcript"
TYPE = "transcript"
TIMEOUT_S = 10.0       # `transcript` waits this long for all parts
STALE_S = 30.0         # a half-assembled answer older than this is dropped
MAX_LINES = 2000       # server calltranscript.MAX_LINES
SINCE_MAX_S = 24 * 3600
DELTA_KEY = "\x00delta"  # Delta's parts carry no id
NOT_ALLOWED = ("not_allowed: only the owner's own agents (joined with the owner's agent key) "
               "may read the call transcript")
NOT_ALLOWED_DE = ("Kein Zugriff: den Verlauf lesen nur eigene Agenten des Inhabers mit "
                  "Schlüssel (Join mit dem Agent-Key des Inhabers).")
LINE_KEYS = ("id", "t", "speaker", "role", "text", "interrupted", "cut")


def new_id() -> str:
    return "tr-" + secrets.token_hex(6)


def request_payload(rid: str, since_s: float | None = None, max_lines: int | None = None) -> dict:
    """The `operator.transcript_request` body; out-of-range values are left out (the server
    ignores them as well)."""
    body: dict = {"id": rid}
    if since_s is not None and 0 < since_s <= SINCE_MAX_S:
        body["since_s"] = since_s
    if max_lines is not None and 1 <= max_lines <= MAX_LINES:
        body["max_lines"] = int(max_lines)
    return body


def _clean_line(ln: object) -> dict | None:
    if not isinstance(ln, dict) or not isinstance(ln.get("text"), str):
        return None
    return {k: ln[k] for k in LINE_KEYS if k in ln}


def _int(v: object, default: int) -> int:
    try:
        return int(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


class Assembler:
    """Parts of `operator.transcript` -> complete answers. Keyed by id (requests) or by
    DELTA_KEY (Delta's send_transcript; a new part 1 starts a new one)."""

    def __init__(self, clock=time.monotonic) -> None:
        self.clock = clock
        self._open: dict[str, dict] = {}  # key -> {"at", "parts", "got": {part: lines}, "head"}

    def __len__(self) -> int:
        return len(self._open)

    def _expire(self) -> None:
        now = self.clock()
        for k in [k for k, v in self._open.items() if now - v["at"] > STALE_S]:
            self._open.pop(k, None)

    def progress(self, key: str) -> tuple[int, int]:
        """(parts received, parts expected) of an open answer; (0, 0) if none."""
        o = self._open.get(key)
        return (len(o["got"]), o["parts"]) if o else (0, 0)

    def drop(self, key: str) -> None:
        self._open.pop(key, None)

    def add(self, payload: dict) -> tuple[str, dict] | None:
        """One packet. -> (key, result) once the answer is complete, else None. result:
        {id?, by?, minutes?, lines, total, truncated, parts} or {id, error}."""
        if not isinstance(payload, dict):
            return None
        self._expire()
        rid = str(payload.get("id") or "")[:64]
        if payload.get("error"):
            return (rid or DELTA_KEY), {"id": rid, "error": str(payload.get("error"))[:64]}
        key = rid if rid else (DELTA_KEY if payload.get("by") == "delta" else "")
        if not key:
            return None
        parts = max(1, _int(payload.get("parts"), 1))
        part = _int(payload.get("part"), 1)
        if not 1 <= part <= parts:
            return None
        lines = [x for x in (_clean_line(ln) for ln in (payload.get("lines") or [])) if x]
        o = self._open.get(key)
        if o is None or o["parts"] != parts or (key == DELTA_KEY and part == 1):
            o = self._open[key] = {"at": self.clock(), "parts": parts, "got": {}, "head": {}}
        o["got"][part] = lines
        o["at"] = self.clock()
        head = o["head"]
        for k in ("by", "minutes"):
            if k in payload:
                head[k] = payload[k]
        head["truncated"] = bool(head.get("truncated")) or bool(payload.get("truncated"))
        head["total"] = _int(payload.get("total"), head.get("total") or 0)
        if len(o["got"]) < parts:
            return None
        self._open.pop(key, None)
        out_lines = [ln for i in range(1, parts + 1) for ln in o["got"].get(i, [])]
        res = {**({"id": rid} if rid else {}), **head, "parts": parts, "lines": out_lines}
        res["total"] = res.get("total") or len(out_lines)
        return key, res


def _hhmm(t: object) -> str:
    try:
        return time.strftime("%H:%M", time.localtime(float(t)))  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError, OSError):
        return "--:--"


def render(lines: list[dict]) -> str:
    """'HH:MM Speaker: Text' per line (local time); interrupted / cut marked."""
    out = []
    for ln in lines:
        who = str(ln.get("speaker") or ln.get("role") or "?").strip()
        text = str(ln.get("text") or "").strip()
        tail = (" [unterbrochen]" if ln.get("interrupted") else "") + (" [gekürzt]" if ln.get("cut") else "")
        out.append(f"{_hhmm(ln.get('t'))} {who}: {text}{tail}")
    return "\n".join(out)


def _minutes(res: dict) -> float | None:
    m = res.get("minutes")
    if isinstance(m, bool) or not isinstance(m, (int, float)) or m <= 0:
        return None
    return m


def summary(res: dict) -> str:
    """One line WITHOUT content (counts only): the join's stdout, the mid-turn hook."""
    n = len(res.get("lines") or []) if "lines" in res else _int(res.get("n"), 0)
    m = _minutes(res)
    span = f"der letzten {m:g} Minuten" if m else "des Calls"
    more = " (gekürzt, älteres fehlt)" if res.get("truncated") else ""
    return f"Delta hat dir den Verlauf {span} geschickt ({n} Zeilen){more}."


def delta_event(res: dict) -> dict:
    """Delta's send_transcript -> the `next`/`listen` event (lines included: only the
    agent's own `next` prints them)."""
    lines = list(res.get("lines") or [])
    ev = {"type": TYPE, "by": "delta", "lines": lines, "n": len(lines),
          "total": res.get("total") or len(lines), "truncated": bool(res.get("truncated")),
          "ts": time.time()}
    m = _minutes(res)
    if m:
        ev["minutes"] = m
    ev["summary"] = summary(ev)
    ev["text"] = render(lines)
    return ev


def meta_only(ev: dict) -> dict:
    """The event without its content (lines/text): for the spool file and the mid-turn hook."""
    return {k: v for k, v in ev.items() if k not in ("lines", "text")}
