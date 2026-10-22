"""Agent-to-agent tasks from the CLI (0.24.0): `voicehook-agent send --to <label> "<task>"`.

Phase 1, local sessions only (no server line): one Claude Code / Codex session of this
machine hands a directed task to another session of the SAME machine. The channel is the
flock-guarded relay inbox already used for decisions and user turns (relayops.py,
`relay-inbox-<hash of label>.jsonl`, 0600): durable on disk, read by the target's
`relay-next` / `listen` / the hook, exactly like a user turn the human spoke to it by name.

Directed only: there is no `--to *` broadcast in Phase 1 (delegation is one agent to one).
Remote targets (a helper on another machine, the server relay) are Phase 2.

Flow:
 1. `send` resolves `--to <label>` to one local session (resolve_target: 404 unknown,
    409 ambiguous, 404 "target not in room" when nobody matches), then appends a `task`
    event to THAT label's inbox. A session that owns a live join also gets an instant wake
    over its control socket (cli._deliver_task); an idle `relay-next`/`listen` picks it up
    from the inbox regardless.
 2. The target consumes the `task` event (relay-next / listen) and acks it: it appends a
    `task_acked` event to the SENDER's inbox, so the sender's board shows "angenommen".
 3. TaskReminder: an open, unacked task older than DUE_S seconds yields a `task_due` event
    back to the sender, once per REMIND_EVERY_S, capped at REMIND_MAX.

Board: the "offen seit" view is reconstructed from the inbox on disk (open_tasks), never from
a volatile in-memory ledger, so it survives a restart of the sending session.

Events (same shape both directions, additive WAKE_TYPES task / task_acked / task_due; an old
CLI treats them as inert unknown events, so this is fully backward compatible):
  task        {type, id, from, to, text, ts}
  task_acked  {type, id, from, to, ts}
  task_due    {type, id, from, to, text, ts, reminder_count}

Stdlib only; never logs the task text.
"""
from __future__ import annotations

import re
import secrets
import time

# additive wake types (also added to spool.WAKE_TYPES and relayops._wanted at import time of
# those modules via A2A_WAKE_TYPES); an older server/CLI never sends them, so nothing breaks.
TASK = "task"
TASK_ACKED = "task_acked"
TASK_DUE = "task_due"
WAKE_TYPES = (TASK, TASK_ACKED, TASK_DUE)

TEXT_MAX = 4000
LABEL_MAX = 40
DUE_S = 60.0           # an unacked task older than this nags the sender
REMIND_EVERY_S = 30.0  # at most one task_due per task per this window
REMIND_MAX = 3         # then the sender is on its own (no auto-cancel: the task stays open)

_ID_RE = re.compile(r"^t[0-9a-f]{12}$")


class TaskError(Exception):
    """A send that cannot be routed (status mirrors datachan.DataError: 404 / 409 / 422)."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def new_id() -> str:
    return "t" + secrets.token_hex(6)


def clean_text(text: object) -> str:
    return " ".join(str(text or "").split())[:TEXT_MAX]


def _clean_label(label: object) -> str:
    return " ".join(str(label or "").split())[:LABEL_MAX].strip()


# --------------------------------------------------------------------------- #
# events (both directions)
# --------------------------------------------------------------------------- #
def task_event(task_id: str, frm: str, to: str, text: str, now: float | None = None) -> dict:
    return {"type": TASK, "id": task_id, "from": _clean_label(frm), "to": _clean_label(to),
            "text": clean_text(text), "ts": now if now is not None else time.time()}


def task_acked_event(task_id: str, frm: str, to: str, now: float | None = None) -> dict:
    """The receiver (`to`) acks task `task_id` of sender `frm`: written back to `frm`'s inbox."""
    return {"type": TASK_ACKED, "id": task_id, "from": _clean_label(frm), "to": _clean_label(to),
            "ts": now if now is not None else time.time()}


def task_due_event(task: dict, reminder_count: int, now: float | None = None) -> dict:
    return {"type": TASK_DUE, "id": str(task.get("id") or ""), "from": _clean_label(task.get("from")),
            "to": _clean_label(task.get("to")), "text": clean_text(task.get("text")),
            "reminder_count": int(reminder_count), "ts": now if now is not None else time.time()}


def is_task(ev: object) -> bool:
    return isinstance(ev, dict) and ev.get("type") in WAKE_TYPES


def valid_task(ev: object) -> dict | None:
    """A task event as read from an inbox, or None (ignore it). The id must look minted here."""
    if not isinstance(ev, dict) or ev.get("type") != TASK:
        return None
    tid = str(ev.get("id") or "")
    if not _ID_RE.match(tid):
        return None
    text = clean_text(ev.get("text"))
    if not text:
        return None
    return {"type": TASK, "id": tid, "from": _clean_label(ev.get("from")),
            "to": _clean_label(ev.get("to")), "text": text,
            "ts": float(ev.get("ts") or 0.0)}


# --------------------------------------------------------------------------- #
# routing: resolve a label to ONE local session
# --------------------------------------------------------------------------- #
def resolve_target(to: str, candidates, *, me: str = "") -> str:
    """Resolve `--to <label>` against the known local session labels `candidates` (an iterable
    of label strings). Returns the matched label. Case-insensitive; an exact (case) match wins.
    404 when nothing matches ("target not in room" with the list), 409 when ambiguous, 422 when
    `to` is empty or '*' (no broadcast in Phase 1)."""
    want = _clean_label(to)
    if not want:
        raise TaskError(422, "send needs --to <label> (the target session's name)")
    if want == "*":
        raise TaskError(422, "no broadcast in Phase 1: send --to one session by name "
                             "(directed delegation)")
    mine = _clean_label(me).lower()
    pool = {lbl for lbl in (_clean_label(c) for c in candidates) if lbl and lbl.lower() != mine}
    if want in pool:
        return want                                   # exact (case) match: unambiguous
    hits = sorted(p for p in pool if p.lower() == want.lower())
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        raise TaskError(409, f"label {want!r} is ambiguous: {hits}")
    avail = sorted(pool)
    raise TaskError(404, f"target not in room: no session {want!r} on this machine; "
                         f"known sessions: {avail}" if avail
                    else f"target not in room: no other session on this machine (can't reach {want!r})")


# --------------------------------------------------------------------------- #
# board: reconstruct open tasks from the sender's inbox (durable, not RAM)
# --------------------------------------------------------------------------- #
def open_tasks(events, now: float | None = None) -> list[dict]:
    """Given the raw events in the SENDER's inbox (a list of dicts, oldest first), return the
    tasks it sent that are still open (no task_acked seen), newest first:
    [{id, to, text, queued_s, reminders}]. A task_acked cancels its task. Phase-1 default:
    no auto-cancel, an unacked task stays open until the operator intervenes."""
    t = time.time() if now is None else now
    tasks: dict[str, dict] = {}
    acked: set[str] = set()
    reminders: dict[str, int] = {}
    remind_ts: dict[str, float] = {}
    for ev in events or []:
        if not isinstance(ev, dict):
            continue
        tid = str(ev.get("id") or "")
        typ = ev.get("type")
        if typ == TASK and _ID_RE.match(tid):
            tasks.setdefault(tid, {"id": tid, "to": _clean_label(ev.get("to")),
                                   "text": clean_text(ev.get("text")),
                                   "ts": float(ev.get("ts") or t)})
        elif typ == TASK_ACKED and tid:
            acked.add(tid)
        elif typ == TASK_DUE and tid:
            reminders[tid] = max(reminders.get(tid, 0), int(ev.get("reminder_count") or 0))
            remind_ts[tid] = max(remind_ts.get(tid, 0.0), float(ev.get("ts") or 0.0))
    out = []
    for tid, task in tasks.items():
        if tid in acked:
            continue
        out.append({"id": tid, "to": task["to"], "text": task["text"], "ts": task["ts"],
                    "queued_s": round(max(0.0, t - task["ts"]), 1),
                    "reminders": reminders.get(tid, 0),
                    "last_reminder_ts": remind_ts.get(tid, 0.0)})
    out.sort(key=lambda it: -it["queued_s"])  # longest open first
    return out


def due_reminders(open_tasks_now: list[dict], frm: str, *, now: float | None = None,
                  due_s: float = DUE_S, every_s: float = REMIND_EVERY_S,
                  cap: int = REMIND_MAX) -> list[dict]:
    """Stateless reminder decision for a short-lived `tasks` call: given the open tasks
    (open_tasks, which carry `reminders` and `last_reminder_ts` straight from the inbox), the
    task_due events owed now. No RAM state: the inbox IS the state, so each process agrees.
    An open task older than due_s nags once per every_s (wall clock) up to cap times; then it
    stays open (no auto-cancel, Phase-1 default: operator intervenes)."""
    t = time.time() if now is None else now
    out: list[dict] = []
    for it in open_tasks_now:
        tid = str(it.get("id") or "")
        if not tid or float(it.get("queued_s") or 0.0) < due_s:
            continue
        n = int(it.get("reminders") or 0)
        if n >= cap:
            continue
        last = float(it.get("last_reminder_ts") or 0.0)
        if last and t - last < every_s:
            continue
        out.append(task_due_event({"id": tid, "from": frm, "to": it.get("to"),
                                   "text": it.get("text")}, n + 1, now=t))
    return out


def board_line(task: dict) -> str:
    """'Auftrag offen bei <X> seit <MM:SS>' for the status board (no task text: no content leak)."""
    s = int(task.get("queued_s") or 0)
    return f"Auftrag offen bei {task.get('to') or '?'} seit {s // 60:02d}:{s % 60:02d}"


# --------------------------------------------------------------------------- #
# reminders: the sending side, a pure clock over its open tasks
# --------------------------------------------------------------------------- #
class TaskReminder:
    """Tracks the tasks THIS session sent and decides when a task_due is owed. Pure: feed it the
    open tasks (open_tasks of the inbox) and a clock; it keeps per-task reminder state in RAM.

    due(now): the task_due events owed right now (an open task older than DUE_S, at most one per
    REMIND_EVERY_S per task, capped at REMIND_MAX). The caller writes each back to the sender's
    OWN inbox (so its next relay-next shows it) and/or prints it."""

    def __init__(self, due_s: float = DUE_S, every_s: float = REMIND_EVERY_S,
                 cap: int = REMIND_MAX, clock=time.monotonic) -> None:
        self.due_s = due_s
        self.every_s = every_s
        self.cap = cap
        self.clock = clock
        self._count: dict[str, int] = {}
        self._last: dict[str, float] = {}
        self._first_seen: dict[str, float] = {}

    def due(self, open_tasks_now: list[dict]) -> list[dict]:
        t = self.clock()
        live = {str(it.get("id")) for it in open_tasks_now}
        for tid in list(self._first_seen):  # a now-acked/gone task frees its reminder state
            if tid not in live:
                self._count.pop(tid, None)
                self._last.pop(tid, None)
                self._first_seen.pop(tid, None)
        out: list[dict] = []
        for it in open_tasks_now:
            tid = str(it.get("id") or "")
            if not tid:
                continue
            self._first_seen.setdefault(tid, t)
            try:
                queued = float(it.get("queued_s") or 0.0)
            except (TypeError, ValueError):
                queued = 0.0
            if queued < self.due_s:
                continue
            n = self._count.get(tid, 0)
            if n >= self.cap:
                continue
            last = self._last.get(tid)
            if last is not None and t - last < self.every_s:
                continue
            n += 1
            self._count[tid] = n
            self._last[tid] = t
            out.append(task_due_event({"id": tid, "from": it.get("from"), "to": it.get("to"),
                                       "text": it.get("text")}, n))
        return out
