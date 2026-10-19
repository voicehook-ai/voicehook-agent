"""Claude Code asks the human (0.18.0): permission prompts and questions in the call.

A Claude Code session that waits for the human (permission dialog, AskUserQuestion, MCP
elicitation, idle "waiting for your input") used to show that only as a desktop push
notification. Now the hook brings it into the call:

* Notification (`voicehook-agent-hook notification`, every `notification_type`):
  - `permission_prompt`, `elicitation_dialog`, `elicitation_url_dialog`,
    `agent_needs_input` (or, without a type, a message that names a permission): a
    DECISION (yes/no) on the call's Decision Board, question = the notification message
    (paths cut to the basename, secrets scrubbed, max 160 chars), plus an activity line.
  - `idle_prompt` (or a message about waiting for input): only an activity line
    `wartet auf Eingabe`.
  - `elicitation_complete`, `elicitation_response`: the session goes on, its open
    decision is removed. Other types (auth_success, agent_completed, quota_*): nothing.
* PermissionRequest (`voicehook-agent-hook permission-request`): the same as a
  permission_prompt, but the question names the tool and its own description
  (`Freigabe für Bash: Remove node_modules directory?`). The hook prints NOTHING, so the
  permission flow stays exactly as it is: the human still decides in the session.
* PreToolUse of AskUserQuestion: the question text itself, its 2-3 option labels as
  options (more than 3: yes/no on the question).

Routing (who asks):
* The session that owns the running join (owner_session): decide over its own control
  socket; the answer arrives in its `next` as a `decision` event. Activity: its own
  activity.log (`HH:MM:SS Notification: wartet auf Eingabe`), or the curl bridge.
* Every other session while ~/.voicehook/relay.json exists: a helper decision over the
  relay (`from` = the session label of the multi-session relay: /rename title, ai-title,
  or `<cwd basename> <4 hex>`), activity through multisession.handle_event. The human's
  answer goes to that helper's inbox (`voicehook-agent relay-next`) and, server side, as
  a copy to the main agent (helper_decision).
* Nothing is ever approved or denied automatically.

Hygiene:
* One open notification decision per session. The same question again while it is
  open: nothing (repeated pushes). A more specific question (PermissionRequest,
  AskUserQuestion) replaces a generic one (Notification text); a generic one never
  replaces a specific one, and a generic push within LATE_GENERIC_S after the session
  went on is stale.
* At most one new decision per ASK_GAP_S per session, all sessions together at most
  GLOBAL_ASK_MAX per GLOBAL_ASK_WINDOW_S.
* The decision is removed once the session goes on: PostToolUse / PostToolUseFailure of
  the tool that asked (any tool when unknown), UserPromptSubmit, Stop.
* The network work runs in a detached child (vact.child_argv, isolated from the cwd);
  the hook itself never waits, prints nothing and always exits 0.

State: ~/.voicehook/notify-state.json (0600, flock): per session hash the open decision
(id, question hash, tool, route), never the question text, never a session id.

stdlib only (the hook starts fast).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path

from . import activity as vact

STATE_NAME = "notify-state.json"
QUESTION_MAX = 160            # server decisions.QUESTION_MAX
OPTION_MAX = 40
ASK_GAP_S = 10.0              # new decisions of one session: at most one per 10 s
GLOBAL_ASK_WINDOW_S = 60.0
GLOBAL_ASK_MAX = 6            # all sessions together
OPEN_KEEP_S = 3600.0          # an open decision older than this is forgotten
SESSIONS_KEEP = 50
LATE_GENERIC_S = 15.0        # a generic push right after the session went on: stale
ACT_DEDUPE_S = 120.0          # own-session activity line: same text at most once per 2 min
CHILD_WAIT_S = 20.0
ACT_TOOL = "Notification"

ASK_TYPES = {"permission_prompt", "elicitation_dialog", "elicitation_url_dialog",
             "agent_needs_input"}
WAIT_TYPES = {"idle_prompt"}
DONE_TYPES = {"elicitation_complete", "elicitation_response"}
ASK_TOOL = "AskUserQuestion"

PRIO_GENERIC = 1   # Notification text
PRIO_SPECIFIC = 2  # PermissionRequest / AskUserQuestion

WAIT_TEXT = "wartet auf Eingabe"
PERM_TEXT = "wartet auf Freigabe"
ASK_TEXT = "hat eine Frage"

_URL = re.compile(r"(?i)\b(?:https?|wss?|ftp)://([^\s/?#'\"<>]+)[^\s'\"<>]*")
_PATH = re.compile(r"(?<![\w.:/\-~])(?:~|\.{1,2})?(?:/[^\s/'\"`<>()\[\]{},;]+)+/?")
_PERM_TOOL = re.compile(r"(?i)permission to use ([A-Za-z0-9_.:\-]{1,40})")


# --------------------------------------------------------------------------- #
# text
# --------------------------------------------------------------------------- #
def _cut(text: str, n: int) -> str:
    text = text.strip()
    return text if len(text) <= n else text[:n - 1].rstrip() + "…"


def clean_text(text: object, n: int = QUESTION_MAX) -> str:
    """Free text for the call: URLs -> host, paths -> basename, secrets scrubbed, one line,
    max n chars."""
    s = vact._clean(str(text or ""))
    s = _URL.sub(lambda m: m.group(1), s)
    s = _PATH.sub(lambda m: m.group(0).rstrip("/").rsplit("/", 1)[-1] or "/", s)
    return _cut(vact.scrub(s), n)


def _qkey(question: str) -> str:
    return hashlib.sha256(f"vh-notify|{question}".encode()).hexdigest()[:16]


def _tool_desc(event: dict) -> str:
    return vact.describe(event.get("tool_input"), event.get("tool_name"))


def _ask_from_tool_input(inp: object) -> tuple[str, list[str]] | None:
    """(question, options) of an AskUserQuestion tool_input (its first question)."""
    if not isinstance(inp, dict):
        return None
    qs = inp.get("questions")
    first = qs[0] if isinstance(qs, list) and qs and isinstance(qs[0], dict) else None
    if first is None:
        return None
    q = clean_text(first.get("question"))
    if not q:
        return None
    labels = []
    for o in first.get("options") or []:
        lbl = o.get("label") if isinstance(o, dict) else o
        lbl = clean_text(lbl, OPTION_MAX) if isinstance(lbl, str) else ""
        if lbl and lbl not in labels:
            labels.append(lbl)
    more = len(qs) - 1
    if more > 0:
        q = _cut(f"{q} (+{more} weitere Frage{'n' if more > 1 else ''} in der Session)",
                 QUESTION_MAX)
    return q, labels if 2 <= len(labels) <= 3 else []


def classify(event: dict) -> dict | None:
    """What one hook event means for the call, or None.
    {kind: ask|wait|done, text (activity), question?, options?, tool?, prio?}"""
    name = str(event.get("hook_event_name") or "")
    if name == "Notification":
        ntype = str(event.get("notification_type") or "").strip()
        msg = str(event.get("message") or "")
        low = msg.lower()
        if not ntype:  # older Claude Code without notification_type: by message
            if "permission" in low:
                ntype = "permission_prompt"
            elif "waiting for your input" in low or "waiting for input" in low:
                ntype = "idle_prompt"
        if ntype in DONE_TYPES:
            return {"kind": "done"}
        if ntype in WAIT_TYPES:
            return {"kind": "wait", "text": WAIT_TEXT}
        if ntype in ASK_TYPES:
            q = clean_text(msg) or ("Claude braucht eine Freigabe" if ntype == "permission_prompt"
                                    else "Claude hat eine Frage")
            m = _PERM_TOOL.search(msg)
            tool = vact.sanitize_tool(m.group(1)) if m else ""
            text = PERM_TEXT if ntype == "permission_prompt" else ASK_TEXT
            return {"kind": "ask", "text": f"{text}: {tool}" if tool else text,
                    "question": q, "options": [], "tool": tool, "prio": PRIO_GENERIC}
        return None
    if name == "PermissionRequest":
        tool = vact.sanitize_tool(event.get("tool_name"))
        if tool == ASK_TOOL:
            got = _ask_from_tool_input(event.get("tool_input"))
            if got:
                return {"kind": "ask", "text": ASK_TEXT, "question": got[0], "options": got[1],
                        "tool": tool, "prio": PRIO_SPECIFIC}
        desc = clean_text(_tool_desc(event), 120).rstrip(" .?!:")
        q = _cut(f"Freigabe für {tool}: {desc}", QUESTION_MAX - 1) + "?" if desc \
            else f"Freigabe für {tool}?"
        return {"kind": "ask", "text": f"{PERM_TEXT}: {tool}", "question": q, "options": [],
                "tool": tool, "prio": PRIO_SPECIFIC}
    if name == "PreToolUse" and str(event.get("tool_name") or "") == ASK_TOOL:
        got = _ask_from_tool_input(event.get("tool_input"))
        if got:
            return {"kind": "ask", "text": ASK_TEXT, "question": got[0], "options": got[1],
                    "tool": ASK_TOOL, "prio": PRIO_SPECIFIC}
    return None


# --------------------------------------------------------------------------- #
# routing
# --------------------------------------------------------------------------- #
def _skey(event: dict) -> str:
    from . import multisession as vmulti
    return vmulti.session_hash(event.get("session_id")) or vmulti.own_session_hash() or \
        "cwd-" + vmulti._h(str(event.get("cwd") or ""), "vh-cwd")


def route(skey: str) -> tuple[str, Path | None, dict | None]:
    """('own', join dir, None) | ('relay', None, relay) | ('bridge', None, None) | ('', ...)."""
    from . import multisession as vmulti
    rel = None if vmulti.relay_disabled() else vmulti.load_relay()
    owner = str(rel.get("owner_session") or "") if rel else ""
    if rel is not None and owner and owner != skey:
        return "relay", None, rel
    env_session = bool((os.environ.get("VOICEHOOK_SESSION") or "").strip())
    d = vact.target_dir()
    if d is not None and (env_session or not vact._foreign(vact.dir_owner(d), skey)):
        return "own", d, None
    if rel is not None and not owner:
        return "relay", None, rel   # helper-import on another machine: every session helps
    bs = vact.load_bridge_session()
    if bs is not None and not vact._foreign(bs.get("owner_session", ""), skey):
        return "bridge", None, None
    return "", None, None


# --------------------------------------------------------------------------- #
# state
# --------------------------------------------------------------------------- #
def _with_state(fn):
    vact._private_dir(vact.state_root())
    path = vact.state_root() / STATE_NAME
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        vact._lock(fd)
        try:
            st = json.loads(os.read(fd, 1 << 18).decode("utf-8") or "{}")
        except ValueError:
            st = {}
        if not isinstance(st, dict):
            st = {}
        sessions = st.get("sessions")
        st["sessions"] = sessions if isinstance(sessions, dict) else {}
        res = fn(st)
        keep = sorted(((k, v) for k, v in st["sessions"].items() if isinstance(v, dict)),
                      key=lambda kv: float(kv[1].get("seen") or 0))[-SESSIONS_KEEP:]
        st["sessions"] = dict(keep)
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, json.dumps(st).encode("utf-8"))
        return res
    finally:
        os.close(fd)


def _state_exists() -> bool:
    try:
        return (vact.state_root() / STATE_NAME).is_file()
    except OSError:
        return False


def admit_ask(skey: str, info: dict, rt: str, t: float, extra: dict | None = None) -> dict | None:
    """Throttle + dedupe under the lock. {"replace": old id or None} when a new decision
    goes out (the slot is then marked open, id still None), else None."""
    qk = _qkey(info["question"])
    prio = int(info.get("prio") or 0)

    def step(st: dict):
        me = st["sessions"].get(skey) if isinstance(st["sessions"].get(skey), dict) else {}
        me["seen"] = t
        st["sessions"][skey] = me
        asks = [a for a in (st.get("asks") or []) if isinstance(a, (int, float))
                and t - a < GLOBAL_ASK_WINDOW_S]
        st["asks"] = asks
        open_ = me.get("open") if isinstance(me.get("open"), dict) else None
        if open_ and (open_.get("resolved") or t - float(open_.get("at") or 0) >= OPEN_KEEP_S):
            open_ = None
        upgrade = False
        if open_:
            if open_.get("q") == qk or int(open_.get("prio") or 0) > prio:
                return None          # the same question is open / specific beats generic
            upgrade = prio > int(open_.get("prio") or 0)
        elif prio == PRIO_GENERIC and t - float(me.get("done_at") or 0) < LATE_GENERIC_S:
            return None              # a late push notification for a prompt already answered
        if not upgrade and t - float(me.get("ask_at") or 0) < ASK_GAP_S:
            return None
        if len(asks) >= GLOBAL_ASK_MAX:
            return None
        me["open"] = {"q": qk, "at": t, "prio": prio, "route": rt, "id": None, "resolved": False,
                      # generic text: the tool is a guess, any tool going on resolves it
                      "tool": info.get("tool") or "" if prio == PRIO_SPECIFIC else "",
                      **(extra or {})}
        me["ask_at"] = t
        asks.append(t)
        return {"replace": open_.get("id") if open_ else None}
    return _with_state(step)


def set_id(skey: str, qk: str, dec_id: str) -> bool:
    """Child: store the id of the decision it created. True when the session went on in the
    meantime (or another question replaced it): then delete it right away."""
    def step(st: dict):
        me = st["sessions"].get(skey)
        open_ = me.get("open") if isinstance(me, dict) and isinstance(me.get("open"), dict) else None
        if open_ is None or open_.get("q") != qk:
            return True
        if open_.get("resolved"):
            me.pop("open", None)
            return True
        open_["id"] = dec_id
        return False
    return bool(_with_state(step))


def resolve(skey: str, tool: str | None, t: float) -> dict | None:
    """The session goes on: the open decision slot ({id, route, from?, dir?}) to delete, or
    None. Without an id yet (the child is still asking) the slot is only marked resolved;
    the child deletes the decision as soon as it has the id."""
    def step(st: dict):
        me = st["sessions"].get(skey)
        open_ = me.get("open") if isinstance(me, dict) and isinstance(me.get("open"), dict) else None
        if open_ is None or open_.get("resolved"):
            return None
        if tool is not None and open_.get("tool") and open_.get("tool") != tool:
            return None
        me.update(seen=t, done_at=t)
        if not open_.get("id"):
            open_["resolved"] = True
            return None
        me.pop("open", None)
        return dict(open_)
    return _with_state(step)


def _admit_activity(skey: str, text: str, t: float) -> bool:
    def step(st: dict):
        me = st["sessions"].get(skey) if isinstance(st["sessions"].get(skey), dict) else {}
        me["seen"] = t
        st["sessions"][skey] = me
        if me.get("act") == text and t - float(me.get("act_at") or 0) < ACT_DEDUPE_S:
            return False
        me.update(act=text, act_at=t)
        return True
    return bool(_with_state(step))


# --------------------------------------------------------------------------- #
# detached child: the network part
# --------------------------------------------------------------------------- #
def spawn(job: dict) -> None:
    """Detached `voicehook-agent-hook notify-post` (job on stdin, never argv)."""
    import subprocess
    try:
        p = subprocess.Popen(vact.child_argv("notify-post"), stdin=subprocess.PIPE,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True, cwd=vact.child_cwd())
        assert p.stdin is not None
        p.stdin.write(json.dumps(job).encode("utf-8"))
        p.stdin.close()
    except Exception:  # noqa: BLE001
        return


def _own_socket(dir_: str) -> Path | None:
    """The control socket of a live join dir (session.json names a live pid)."""
    from . import session as vsession
    d = Path(dir_)
    try:
        pid = int(json.loads((d / vsession.INFO_NAME).read_text(encoding="utf-8")).get("pid"))
    except (OSError, ValueError, TypeError, AttributeError):
        return None
    if pid <= 0 or not vact._pid_alive(pid):
        return None
    sock = vsession.socket_path(d)
    return sock if vsession.socket_alive(sock) else None


def _decide(job: dict, body: dict) -> dict:
    """One decide request (create or delete) on the job's route. Never raises."""
    try:
        if job.get("route") == "own":
            from . import session as vsession
            sock = _own_socket(str(job.get("dir") or ""))
            if sock is None:
                return {"ok": False}
            return vsession.request(sock, {"cmd": "decide", **body}, timeout=CHILD_WAIT_S)
        if job.get("route") == "relay":
            from . import multisession as vmulti
            from . import relayops as vrelayops
            rel = vmulti.load_relay()
            if rel is None:
                return {"ok": False}
            frm = vrelayops.check_from(str(job.get("from") or ""))
            if vmulti._is_local(rel):
                return vrelayops.local_request(rel, frm, "decide", body, wait=CHILD_WAIT_S)
            return vrelayops.remote_decide(rel, frm, body)
    except Exception:  # noqa: BLE001, S110 - detached child, nobody to tell
        pass
    return {"ok": False}


_ID = re.compile(r"^[A-Za-z0-9_\-]{1,40}$")


def post_main(raw: str) -> int:
    """The detached child: create (and maybe replace) or delete one decision. Always 0."""
    try:
        job = json.loads(raw or "{}")
        if not isinstance(job, dict):
            return 0
        old = job.get("replace") or job.get("delete")
        if isinstance(old, str) and _ID.match(old):
            _decide(job, {"id": old, "delete": True})
        q = job.get("question")
        if job.get("op") != "create" or not isinstance(q, str) or not q:
            return 0
        body: dict = {"question": q}
        opts = [o for o in job.get("options") or [] if isinstance(o, str)]
        if 2 <= len(opts) <= 3:
            body["options"] = opts
        res = _decide(job, body)
        dec_id = res.get("id") if isinstance(res, dict) and res.get("ok") else None
        if isinstance(dec_id, str) and _ID.match(dec_id) and \
                set_id(str(job.get("skey") or ""), _qkey(q), dec_id):
            _decide(job, {"id": dec_id, "delete": True})
    except Exception:  # noqa: BLE001, S110
        pass
    return 0


# --------------------------------------------------------------------------- #
# hook entries
# --------------------------------------------------------------------------- #
def _activity(rt: str, dir_: Path | None, skey: str, raw: str, text: str, event: dict,
              t: float) -> None:
    if rt == "relay":
        from . import multisession as vmulti
        vmulti.handle_event(raw)          # labeled `<session>: <text>`, its own throttle
        return
    if event.get("hook_event_name") == "PreToolUse":
        return                            # the own PreToolUse line is already in the log
    if rt == "own" and dir_ is not None and _admit_activity(skey, text, t):
        vact.append_line(dir_, f"{vact._ts(t)} {ACT_TOOL}: {text}")
    elif rt == "bridge" and _admit_activity(skey, text, t):
        vact.bridge_send(text, t)


def handle(raw: str, now: float | None = None, post=None) -> dict | None:
    """Notification / PermissionRequest / PreToolUse(AskUserQuestion): activity line and,
    for a question, a decision in the call. The job handed to `post` (default: detached
    child) or None. Never raises, never prints."""
    try:
        event = json.loads(raw) if raw and raw.strip() else {}
        if not isinstance(event, dict):
            return None
        info = classify(event)
        if info is None:
            return None
        t = time.time() if now is None else now
        skey = _skey(event)
        if info["kind"] == "done":
            return _resolve_job(skey, None, t, post)
        rt, dir_, _rel = route(skey)
        if not rt:
            return None
        _activity(rt, dir_, skey, raw, info["text"], event, t)
        if info["kind"] != "ask" or rt == "bridge":
            return None
        if rt == "own":
            extra = {"dir": str(dir_)}
        else:
            from . import multisession as vmulti
            extra = {"from": vmulti.session_label(event, _cached_label(skey))}
        adm = admit_ask(skey, info, rt, t, extra)
        if adm is None:
            return None
        job = {"op": "create", "route": rt, "skey": skey, "question": info["question"],
               "options": info.get("options") or [], "replace": adm.get("replace"), **extra}
        (post or spawn)(job)
        return job
    except Exception:  # noqa: BLE001 - a hook must never disturb Claude Code
        return None


def _cached_label(skey: str) -> str:
    from . import multisession as vmulti
    try:
        st = json.loads((vact.state_root() / vmulti.STATE_NAME).read_text(encoding="utf-8"))
        me = st.get("sessions", {}).get(skey) if isinstance(st, dict) else None
        return str(me.get("label") or "") if isinstance(me, dict) else ""
    except (OSError, ValueError, AttributeError):
        return ""


def _resolve_job(skey: str, tool: str | None, t: float, post) -> dict | None:
    got = resolve(skey, tool, t)
    if got is None:
        return None
    job = {"op": "delete", "route": got.get("route") or "", "skey": skey, "delete": got["id"]}
    for k in ("dir", "from"):
        if isinstance(got.get(k), str):
            job[k] = got[k]
    (post or spawn)(job)
    return job


def on_progress(raw: str, now: float | None = None, post=None) -> dict | None:
    """PostToolUse / PostToolUseFailure / UserPromptSubmit / Stop: the session goes on, so
    its open notification decision leaves the board. Cheap without notify-state.json."""
    try:
        if not _state_exists():
            return None
        event = json.loads(raw) if raw and raw.strip() else {}
        if not isinstance(event, dict):
            return None
        name = str(event.get("hook_event_name") or "")
        if name in ("PostToolUse", "PostToolUseFailure"):
            tool: str | None = vact.sanitize_tool(event.get("tool_name"))
        elif name in ("UserPromptSubmit", "Stop"):
            tool = None
        else:
            return None
        return _resolve_job(_skey(event), tool, time.time() if now is None else now, post)
    except Exception:  # noqa: BLE001
        return None
