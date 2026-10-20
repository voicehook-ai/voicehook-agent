"""Listen guard (0.21.4, 0.21.5): the joining Claude Code session never ends its turn deaf.

Observed in live calls: the operator agent is joined (`status` says connected) but ends its
turn while no `voicehook-agent next` waits for this join, or starts the listener detached so
it is never woken. The user's sentences pile up as `pending` and to the user the agent has
left the call.

The Stop hook (`voicehook-agent-hook stop`) asks the agent's OWN live join over its control
socket (`status`, 1 s): the join counts the `next` requests that wait right now
(`listening`) and the seconds since the last one ended (`listen_idle_s`). When this session
owns a connected join, nobody listens and the last `next` ended more than LISTEN_GAP_S ago
(the gap between two `next` of a loop is not "deaf"), the hook answers
`{"decision": "block", "reason": ...}` with the pending count and the command to start.

0.21.5: the join holds the presence itself and spools every event (spool.py), so the agent
is no longer "absent" while it works. `status` now says `listening: true` while join + spool
hold the call, `consumers` = the waiting `next`/`listen` requests, `unread` / `unread_wake` =
spool events not acked yet. The Stop hook blocks only when
* wake-worthy events wait unread and no consumer is there to take them (`unread`),
* this session's join process is gone without leaving the call (a spool is left behind,
  `dead`), or
* nothing would wake the agent: no consumer waits and the last one ended more than
  LISTEN_GAP_S ago (`nolistener`; the join keeps the presence, but a user turn would only
  pile up in the spool).
The reason always names `voicehook-agent listen` with run_in_background. A join older than
0.21.5 (no `consumers` field) is judged as before.

Loop safety: on `stop_hook_active` (Claude Code already continues because of a Stop hook) it
blocks again only when MORE sentences are pending than at its last block (state file
`listen-guard.json` in the join dir, LAST_BLOCK_KEEP_S). Never blocks: outside a call, for
another session's join (owner_session), a subagent, a join that is leaving, a join too old
to report `listening`, or with VOICEHOOK_LISTEN_GUARD=0.

Stdlib only (runs inside the hook).
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from . import session as vsession
from . import spool as vspool

ENV_OFF = "VOICEHOOK_LISTEN_GUARD"
LISTEN_GAP_S = 5.0          # a `next` ended this recently: a loop between two calls
STATUS_TIMEOUT_S = 1.0      # control socket answer for the hook (Stop hook alarm is 3 s)
STATE_NAME = "listen-guard.json"
LAST_BLOCK_KEEP_S = 600.0   # an older block state does not count for stop_hook_active
LISTEN_CMD = "voicehook-agent listen"
NEXT_LOOP = "voicehook-agent next --timeout 10"
DEAD_RECENT_S = 3600.0      # a spool left by a dead join counts this long (then: stale)


def _off() -> bool:
    return (os.environ.get(ENV_OFF) or "").strip().lower() in ("0", "false", "no", "off")


def _int(v: object) -> int:
    try:
        return max(0, int(v))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def kind(status: dict | None) -> str | None:
    """Why this join's session must not end its turn: "unread", "nolistener" or None."""
    if not isinstance(status, dict) or not status.get("ok"):
        return None
    if not status.get("connected") or status.get("leaving") or "listening" not in status:
        return None
    from . import waitcheck
    if status.get("board_waiting") and _int(status.get("open_decisions")) == 0 \
            and not waitcheck.off():
        return "nodecision"  # 0.21.6: the board waits for the human, the board has no question
    if "consumers" in status:  # 0.21.5 join: listening = join + spool hold the call
        if _int(status.get("consumers")) > 0:
            return None  # a `listen`/`next` waits: it takes every event at once
        if _int(status.get("unread_wake")) > 0:
            return "unread"
    elif _int(status.get("listening")) > 0:  # 0.21.4 join: listening = waiting `next`
        return None
    idle = status.get("listen_idle_s")
    just_ended = isinstance(idle, (int, float)) and not isinstance(idle, bool) \
        and idle < LISTEN_GAP_S
    return None if just_ended else "nolistener"


def deaf(status: dict | None) -> bool:
    """Is this join connected while nobody would take its events (0.21.4 name)?"""
    return kind(status) is not None


def _pending(status: dict) -> int:
    return _int(status.get("unread_wake" if "unread_wake" in status else "pending"))


def reason(status: dict, session: str = "", why: str | None = None) -> str:
    where = f" --session {session}" if session else ""
    start = (f"Start the listen loop now with run_in_background (so the harness wakes you), e.g. "
             f"`{LISTEN_CMD}{where}` (or `{NEXT_LOOP}{where}` in a loop), before ending the turn. "
             f"Not detached with & or nohup: nobody would wake you. Leaving the call instead: "
             f"`voicehook-agent leave`.")
    why = why or kind(status) or "nolistener"
    if why == "nodecision":
        from . import board as vboard
        return vboard.wait_stop_text(str(status.get("board_waiting") or ""))
    if why == "unread":
        n = _pending(status)
        return (f"You are in a live voicehook call and {n} event{'s' if n != 1 else ''} for you "
                f"wait{'' if n != 1 else 's'} unread (the user or a decision). `{LISTEN_CMD}` prints "
                f"the first one and exits; handle it, then start it again. {start}")
    if why == "dead":
        n = _int(status.get("unread"))
        return (f"Your voicehook join process ended without leaving the call ({n} unread "
                f"event{'s' if n != 1 else ''} left in its spool): nobody takes the user's turns "
                f"now. Join again in the background (`voicehook-agent join <invite-url> --name "
                f"... --model ... --json`), then {start[0].lower()}{start[1:]}")
    pending = _int(status.get("pending"))
    return (f"You are in a live voicehook call and nobody is listening ({pending} pending). "
            f"The call keeps you present, but nothing wakes you when the user speaks: the "
            f"sentences pile up and to the user you do not answer. {start}")


def decide(event: dict, status: dict | None, last: dict | None, now: float,
           session: str = "", why: str | None = None) -> dict | None:
    """The Stop hook's answer for this session's own join (None = let the turn end).
    `status`: the join's status reply (None = no own join); `last`: the state of the last
    block ({pending, at}) or None. `why`="dead": the join process is gone (status = the
    spool counts read from disk)."""
    if not isinstance(event, dict) or str(event.get("agent_id") or "").strip():
        return None  # a subagent's Stop is not the joining agent's turn end
    why = why or kind(status)
    if why is None:
        return None
    pending = _int(status.get("unread")) if why == "dead" else _pending(status)  # type: ignore[union-attr]
    if why == "nodecision":  # once per board text, never a loop
        if isinstance(last, dict) and last.get("waiting") == status.get("board_waiting"):  # type: ignore[union-attr]
            why = kind({**status, "board_waiting": None})  # type: ignore[dict-item]
            if why is None:
                return None
        else:
            return {"decision": "block", "reason": reason(status, session, why)}  # type: ignore[arg-type]
    if event.get("stop_hook_active") and isinstance(last, dict):
        fresh = now - float(last.get("at") or 0.0) < LAST_BLOCK_KEEP_S
        if fresh and pending <= _int(last.get("pending")):
            return None  # already told once for this pending state: no loop
    return {"decision": "block", "reason": reason(status, session, why)}  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# I/O: the session's own live join, its status, the block state
# --------------------------------------------------------------------------- #
def own_join_dirs(event: dict) -> list[Path]:
    """Live join dirs (this home or a pointer file) whose owner_session is the hook
    event's session. Unknown owner or session: none (never block a foreign session)."""
    from . import activity as vact
    from . import multisession as vmulti
    me = vmulti.session_hash(event.get("session_id")) or vmulti.own_session_hash()
    if not me:
        return []
    seen: dict[str, Path] = {}
    for d in vact.live_join_dirs() + vact.pointer_dirs():
        try:
            seen.setdefault(str(d.resolve()), d)
        except OSError:
            continue
    return [d for d in seen.values() if vact.dir_owner(d) == me]


def dead_join_dirs(event: dict, now: float | None = None) -> list[Path]:
    """0.21.5: join dirs of THIS session whose join process is gone without leaving the call:
    session.json names a dead pid and the event spool is still there (a clean leave deletes
    it), written within DEAD_RECENT_S."""
    from . import activity as vact
    from . import multisession as vmulti
    me = vmulti.session_hash(event.get("session_id")) or vmulti.own_session_hash()
    if not me:
        return []
    t = time.time() if now is None else now
    out: list[Path] = []
    try:
        infos = sorted(vsession.sessions_root().glob(f"*/*/{vsession.INFO_NAME}"))
    except OSError:
        return []
    for info in infos:
        d = info.parent
        try:
            pid = int(json.loads(info.read_text(encoding="utf-8")).get("pid"))
            age = t - os.lstat(d / vspool.EVENTS_NAME).st_mtime
        except (OSError, ValueError, TypeError, AttributeError):
            continue
        if pid <= 0 or vact._pid_alive(pid) or age > DEAD_RECENT_S or vact.dir_owner(d) != me:
            continue
        out.append(d)
    return out


def join_status(dir_: Path) -> dict | None:
    sock = vsession.socket_path(dir_)
    try:
        if vsession.is_fallback(sock):
            vsession.check_private_dir(sock.parent)
        if not vsession.socket_alive(sock, timeout=STATUS_TIMEOUT_S):
            return None
        return vsession.request(sock, {"cmd": "status"}, timeout=STATUS_TIMEOUT_S)
    except (OSError, ValueError, vsession.SessionError):
        return None


def _label(dir_: Path) -> str:
    """`--session` for the hint: slug/identity when the join lives in this home and is not
    the only one there; '' otherwise (the command finds the single join by itself)."""
    try:
        root = vsession.sessions_root().resolve()
        if dir_.resolve().parent.parent != root or len(vsession.live_sessions()) < 2:
            return ""
    except OSError:
        return ""
    return f"{dir_.parent.name}/{dir_.name}"


def _read_state(dir_: Path) -> dict | None:
    try:
        d = json.loads((dir_ / STATE_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def _write_state(dir_: Path, pending: int, now: float, dead: bool = False,
                 waiting: str | None = None) -> None:
    state: dict = {"pending": pending, "at": now}
    if dead:
        state["dead"] = True
    if waiting:
        state["waiting"] = waiting  # 0.21.6: told once for this board text
    try:
        (dir_ / STATE_NAME).write_text(json.dumps(state), encoding="utf-8")
    except OSError:
        pass


def handle(raw: str, now: float | None = None) -> dict | None:
    """Stop hook: block + reason when this session's own join is deaf. Never raises."""
    try:
        if _off():
            return None
        event = json.loads(raw) if raw and raw.strip() else {}
        if not isinstance(event, dict) or str(event.get("agent_id") or "").strip():
            return None
        t = time.time() if now is None else now
        live = own_join_dirs(event)
        for d in live:
            status = join_status(d)
            last = _read_state(d)
            out = decide(event, status, last, t, _label(d))
            if out is not None:
                st = status or {}
                told = (last or {}).get("waiting") if isinstance(last, dict) else None
                _write_state(d, _pending(st), t, waiting=st.get("board_waiting")
                             if kind(st) == "nodecision" and told != st.get("board_waiting") else told)
                return out
        if live:
            return None
        for d in dead_join_dirs(event, t):  # 0.21.5: the join died without leaving
            last = _read_state(d)
            if isinstance(last, dict) and last.get("dead"):
                continue  # told once for this dead join: never again
            unread, wake = vspool.unread_on_disk(d)
            out = decide(event, {"ok": True, "unread": unread, "unread_wake": wake},
                         None, t, "", why="dead")
            if out is not None:
                _write_state(d, unread, t, dead=True)
                return out
        return None
    except Exception:  # noqa: BLE001
        return None


def merge(first: dict | None, second: dict | None) -> dict | None:
    """Two Stop answers into one: block wins, reasons joined."""
    if not first or not second:
        return first or second
    if first.get("decision") == "block" and second.get("decision") == "block":
        return {"decision": "block", "reason": f"{first.get('reason', '')}\n\n{second.get('reason', '')}".strip()}
    return first if first.get("decision") == "block" else second
