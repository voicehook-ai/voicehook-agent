"""Listen guard (0.21.4): the joining Claude Code session never ends its turn deaf.

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

ENV_OFF = "VOICEHOOK_LISTEN_GUARD"
LISTEN_GAP_S = 5.0          # a `next` ended this recently: a loop between two calls
STATUS_TIMEOUT_S = 1.0      # control socket answer for the hook (Stop hook alarm is 3 s)
STATE_NAME = "listen-guard.json"
LAST_BLOCK_KEEP_S = 600.0   # an older block state does not count for stop_hook_active
LISTEN_CMD = "voicehook-agent listen"
NEXT_LOOP = "voicehook-agent next --timeout 45"


def _off() -> bool:
    return (os.environ.get(ENV_OFF) or "").strip().lower() in ("0", "false", "no", "off")


def _int(v: object) -> int:
    try:
        return max(0, int(v))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def deaf(status: dict | None) -> bool:
    """Is this join connected while no `next` waits for it (and none just ended)?
    A join without the `listening` field (older than 0.21.4) never counts as deaf."""
    if not isinstance(status, dict) or not status.get("ok"):
        return False
    if not status.get("connected") or status.get("leaving") or "listening" not in status:
        return False
    if _int(status.get("listening")) > 0:
        return False
    idle = status.get("listen_idle_s")
    just_ended = isinstance(idle, (int, float)) and not isinstance(idle, bool) \
        and idle < LISTEN_GAP_S
    return not just_ended


def reason(status: dict, session: str = "") -> str:
    pending = _int(status.get("pending"))
    where = f" --session {session}" if session else ""
    return (f"You are in a live voicehook call and nobody is listening ({pending} pending). "
            f"The user's sentences pile up and to them you have left the call. Start the listen "
            f"loop now with run_in_background (so the harness wakes you), e.g. "
            f"`{LISTEN_CMD}{where}` (or `{NEXT_LOOP}{where}` in a loop), before ending the "
            f"turn. Not detached with & or nohup: nobody would wake you. Leaving the call "
            f"instead: `voicehook-agent leave`.")


def decide(event: dict, status: dict | None, last: dict | None, now: float,
           session: str = "") -> dict | None:
    """The Stop hook's answer for this session's own join (None = let the turn end).
    `status`: the join's status reply (None = no own join); `last`: the state of the last
    block ({pending, at}) or None."""
    if not isinstance(event, dict) or str(event.get("agent_id") or "").strip():
        return None  # a subagent's Stop is not the joining agent's turn end
    if not deaf(status):
        return None
    pending = _int(status.get("pending"))  # type: ignore[union-attr]
    if event.get("stop_hook_active") and isinstance(last, dict):
        fresh = now - float(last.get("at") or 0.0) < LAST_BLOCK_KEEP_S
        if fresh and pending <= _int(last.get("pending")):
            return None  # already told once for this pending state: no loop
    return {"decision": "block", "reason": reason(status, session)}


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


def _write_state(dir_: Path, pending: int, now: float) -> None:
    try:
        (dir_ / STATE_NAME).write_text(json.dumps({"pending": pending, "at": now}),
                                       encoding="utf-8")
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
        for d in own_join_dirs(event):
            status = join_status(d)
            out = decide(event, status, _read_state(d), t, _label(d))
            if out is not None:
                _write_state(d, _int((status or {}).get("pending")), t)
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
