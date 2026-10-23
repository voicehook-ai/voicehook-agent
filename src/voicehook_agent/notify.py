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
* PermissionRequest (`voicehook-agent-hook permission-request`): 0.21.4 a real question
  that names the session and the concrete action (permission_question): `Kunde C will
  ausführen: git worktree remove wt-a – erlauben?` (Bash: the command, first line, paths
  cut to the basename, secrets scrubbed; Read/Edit/Write: the file; WebFetch: the host;
  ...); the tool's own description as hidden context. Without a concrete detail (no
  command, no file, ...) NO decision, only the activity line. A generic permission_prompt
  Notification ("Claude needs your permission to use Bash") is no question either: only the
  activity line (the PermissionRequest of the same prompt carries the details). The hook
  prints NOTHING, so the permission flow stays exactly as it is: the human still decides
  in the session.
* 0.21.4 unattended sessions (`claude -p`, SDK runs, timers: unattended()) never put a
  question on the board, only the activity line: nobody could answer it. The same question
  of the same session label within RECENT_DEDUPE_S goes out once (repeated runs).
* PreToolUse of AskUserQuestion: the question text itself, its 2-3 option labels as
  options (more than 3: yes/no on the question). 0.21.3 (asks.py): EVERY question of the
  call gets its own decision, the option descriptions go along as hidden `context`, a 4th
  option is named in the question (or the context); the `ask-wait` hook can answer the
  tool directly from the board.

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
RECENT_DEDUPE_S = 600.0       # 0.21.4: the same question of the same label at most once
DETAIL_MAX = 90               # the action in a permission question (command, file, ...)
ATTENDED_ENV = "VOICEHOOK_ATTENDED"   # 0.21.4: 1 = always attended, 0 = never ask

PRIO_GENERIC = 1   # Notification text
PRIO_SPECIFIC = 2  # PermissionRequest / AskUserQuestion

WAIT_TEXT = "wartet auf Eingabe"
PERM_TEXT = "wartet auf Freigabe"
ASK_TEXT = "hat eine Frage"

# A notification message that is ONLY a generic "waiting/needs input" phrase (no concrete
# question) must never become a decision card: contentless, no options, no context, nobody
# could answer it. The Klartext rule applies to automatic cards too -> such a message stays a
# wait (activity line only). A real elicitation question (a concrete ask) does not match this.
_CONTENTLESS = re.compile(
    r"(?i)^(?:claude\s+|the\s+agent\s+|der\s+agent\s+)?(?:is\s+|ist\s+)?"
    r"(?:waiting\s+for\s+(?:your\s+)?(?:input|response|reply|answer)"
    r"|needs?\s+(?:your\s+)?input"
    r"|input\s+needed"
    r"|wartet\s+auf\s+(?:deine\s+)?(?:eingabe|antwort|freigabe)"
    r"|eingabe\s+ben(?:ö|oe)tigt"
    r"|ready(?:\s+for\s+input)?"
    r"|bereit(?:\s+f(?:ü|ue)r\s+eingabe)?)\.?$")


def _contentless(q: str) -> bool:
    """True when the question is only a generic waiting/needs-input phrase (no concrete ask)."""
    return bool(_CONTENTLESS.match(str(q or "").strip()))


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


CONTEXT_MAX = 1500            # server decisions.CONTEXT_MAX (hidden background for Delta)
ITEMS_MAX = 4                 # AskUserQuestion: 1-4 questions


def _redacted(text: object) -> bool:
    """A secret was scrubbed out of this text: it never goes to the call (0.21.3)."""
    s = str(text or "")
    return vact.REDACTED in vact.scrub(s) and vact.REDACTED not in vact._clean(s)


def ask_items(inp: object) -> list[dict]:
    """0.21.3: every question of an AskUserQuestion tool_input as one decision:
    {question, options (2-3 labels, else []), labels (the original labels), raw (the
    original question text, the key of the tool's `answers`), context (descriptions)}.
    A question with a secret in it (scrubbed) is dropped. Language as written."""
    if not isinstance(inp, dict) or not isinstance(inp.get("questions"), list):
        return []
    out: list[dict] = []
    for qd in inp["questions"][:ITEMS_MAX]:
        if not isinstance(qd, dict) or not isinstance(qd.get("question"), str):
            continue
        raw = qd["question"]
        q = clean_text(raw)
        if not q or _redacted(raw):
            continue
        labels: list[str] = []
        shown: list[str] = []
        ctx: list[str] = []
        for o in qd.get("options") or []:
            lbl = o.get("label") if isinstance(o, dict) else o
            if not isinstance(lbl, str) or not lbl.strip() or _redacted(lbl):
                continue
            short = clean_text(lbl, OPTION_MAX)
            if short in shown:
                continue
            labels.append(lbl)
            shown.append(short)
            desc = o.get("description") if isinstance(o, dict) else ""
            if isinstance(desc, str) and desc.strip() and not _redacted(desc):
                ctx.append(f"{short}: {clean_text(desc, 200)}")
        opts = shown[:3] if len(shown) >= 2 else []
        if len(shown) > 3:          # the board has 2-3 options: name the rest in the question
            rest = " / ".join(shown[3:])
            if len(q) + len(rest) + 3 <= QUESTION_MAX:
                q = f"{q} ({rest})"
            ctx.append("More options (answer as free text): " + rest)
        if qd.get("multiSelect") is True:
            ctx.append("Several options may be chosen (answer as free text).")
        header = qd.get("header")
        if isinstance(header, str) and header.strip() and not _redacted(header):
            ctx.insert(0, "Topic: " + clean_text(header, 40))
        out.append({"question": q, "options": opts, "labels": labels[:len(shown)], "raw": raw,
                    "context": _cut("; ".join(ctx), CONTEXT_MAX) if ctx else ""})
    return out


def _ask_from_tool_input(inp: object) -> tuple[str, list[str], list[dict]] | None:
    """(question, options, items) of an AskUserQuestion tool_input; question/options are
    those of its first question (dedupe key), items all of them (ask_items)."""
    items = ask_items(inp)
    if not items:
        return None
    return items[0]["question"], items[0]["options"], items


# 0.21.4: what a tool will do, as the verb of a permission question
_VERBS = {"Bash": "ausführen", "Read": "lesen", "NotebookRead": "lesen", "Edit": "ändern",
          "MultiEdit": "ändern", "NotebookEdit": "ändern", "Write": "schreiben",
          "WebFetch": "abrufen", "WebSearch": "suchen", "Glob": "durchsuchen",
          "Grep": "durchsuchen"}


def _detail(tool: str, inp: dict) -> str:
    """The concrete action of a tool call, safe for the call ('' = nothing concrete)."""
    if tool == "Bash":
        cmd = next((ln for ln in str(inp.get("command") or "").splitlines() if ln.strip()), "")
        return clean_text(cmd, DETAIL_MAX) if cmd else ""
    if tool in vact.FILE_TOOLS:
        return vact.basename(inp.get(vact.FILE_TOOLS[tool]))
    if tool == "WebFetch":
        m = _URL.search(str(inp.get("url") or ""))
        return m.group(1)[:DETAIL_MAX] if m else ""
    if tool in ("WebSearch", "Glob", "Grep"):
        return clean_text(inp.get("query") or inp.get("pattern") or "", DETAIL_MAX)
    return ""


def permission_question(event: dict, label: str = "") -> tuple[str, str] | None:
    """0.21.4 (question, context) of a PermissionRequest: the session and the concrete
    action, `<label> will ausführen: <command> – erlauben?`; the tool's own description as
    context. None when nothing concrete can be named (then no decision)."""
    tool = vact.sanitize_tool(event.get("tool_name"))
    inp = event.get("tool_input") if isinstance(event.get("tool_input"), dict) else {}
    who = vact._clean(label)[:40].strip() or "Eine Session"
    desc = clean_text(_tool_desc(event), 200).rstrip(" .?!:")
    detail = _detail(tool, inp)
    if tool.startswith("mcp__"):
        parts = [p for p in tool.split("__")[1:] if p]
        action = f"{' '.join(parts)} nutzen" if parts else ""
        detail = desc
        verb = action
    else:
        verb = _VERBS.get(tool, "")
        if not verb and desc:
            verb, detail = f"{tool} nutzen", desc
    if not verb or not (detail or tool.startswith("mcp__")):
        return None
    tail = " – erlauben?"
    head = f"{who} will {verb}"
    if detail:
        room = QUESTION_MAX - len(head) - len(tail) - 2
        if room < 8:
            return None
        q = f"{head}: {_cut(detail, room)}{tail}"
    else:
        q = f"{head}{tail}"
    ctx = f"Beschreibung: {desc}" if desc and desc != detail else ""
    return q, ctx


def unattended(env=None) -> bool:
    """0.21.4: nobody sits at this session (`claude -p`, Agent SDK runs, timers): its
    questions never go to the board. VOICEHOOK_ATTENDED=1/0 overrides; Claude Code's
    CLAUDE_CODE_SESSION_ATTENDED wins over the entrypoint (sdk-*: headless)."""
    env = os.environ if env is None else env

    def flag(name: str) -> bool | None:
        v = str(env.get(name) or "").strip().lower()
        if v in ("1", "true", "yes", "on"):
            return True
        if v in ("0", "false", "no", "off"):
            return False
        return None
    for name in (ATTENDED_ENV, "CLAUDE_CODE_SESSION_ATTENDED"):
        f = flag(name)
        if f is not None:
            return not f
    ep = str(env.get("CLAUDE_CODE_ENTRYPOINT") or "").strip().lower()
    return ep.startswith("sdk") or ep in ("print", "headless")


def classify(event: dict, label: str = "") -> dict | None:
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
            m = _PERM_TOOL.search(msg)
            tool = vact.sanitize_tool(m.group(1)) if m else ""
            if ntype == "permission_prompt":
                # 0.21.4: "Claude needs your permission to use Bash" is no question; the
                # PermissionRequest of the same prompt asks with the concrete action
                return {"kind": "wait", "text": f"{PERM_TEXT}: {tool}" if tool else PERM_TEXT}
            q = clean_text(msg)
            if not q or _contentless(q):
                # generic "waiting/needs input" with no concrete question: activity only, no card
                return {"kind": "wait", "text": ASK_TEXT}
            return {"kind": "ask", "text": f"{ASK_TEXT}: {tool}" if tool else ASK_TEXT,
                    "question": q, "options": [], "tool": tool, "prio": PRIO_GENERIC}
        return None
    if name == "PermissionRequest":
        tool = vact.sanitize_tool(event.get("tool_name"))
        if tool == ASK_TOOL:
            got = _ask_from_tool_input(event.get("tool_input"))
            if got:
                return {"kind": "ask", "text": ASK_TEXT, "question": got[0], "options": got[1],
                        "items": got[2], "tool": tool, "prio": PRIO_SPECIFIC}
        got = permission_question(event, label)
        if got is None:  # 0.21.4: nothing concrete to ask about: only the activity line
            return {"kind": "wait", "text": f"{PERM_TEXT}: {tool}"}
        q, ctx = got
        info = {"kind": "ask", "text": f"{PERM_TEXT}: {tool}", "question": q, "options": [],
                "tool": tool, "prio": PRIO_SPECIFIC}
        if ctx:
            info["items"] = [{"question": q, "options": [], "context": ctx}]
        return info
    if name == "PreToolUse" and str(event.get("tool_name") or "") == ASK_TOOL:
        got = _ask_from_tool_input(event.get("tool_input"))
        if got:
            return {"kind": "ask", "text": ASK_TEXT, "question": got[0], "options": got[1],
                    "items": got[2], "tool": ASK_TOOL, "prio": PRIO_SPECIFIC}
    return None


# --------------------------------------------------------------------------- #
# routing
# --------------------------------------------------------------------------- #
def _skey(event: dict) -> str:
    from . import multisession as vmulti
    return vmulti.session_hash(event.get("session_id")) or vmulti.own_session_hash() or \
        "cwd-" + vmulti._h(str(event.get("cwd") or ""), "vh-cwd")


def _base(skey: str) -> str:
    from . import board
    return board.base_key(skey)


def route(skey: str) -> tuple[str, Path | None, dict | None]:
    """('own', join dir, None) | ('relay', None, relay) | ('bridge', None, None) | ('', ...)."""
    from . import multisession as vmulti
    rel = None if vmulti.relay_disabled() else vmulti.load_relay(session=_base(skey))
    owner = str(rel.get("owner_session") or "") if rel else ""
    if rel is not None and (rel.get("helper") or (owner and owner != skey)):
        return "relay", None, rel  # 0.22.2: an own helper entry is always the relay
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


def group_key(info: dict) -> str:
    """Dedupe key of one ask: its question, or all questions of an AskUserQuestion."""
    items = info.get("items") or []
    if len(items) > 1:
        return _qkey("\n".join(str(i.get("question") or "") for i in items))
    return _qkey(str(info.get("question") or ""))


def admit_ask(skey: str, info: dict, rt: str, t: float, extra: dict | None = None) -> dict | None:
    """Throttle + dedupe under the lock. {"replace": old id or None} when a new decision
    goes out (the slot is then marked open, id still None), else None."""
    qk = group_key(info)
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
        return {"replace": open_.get("id") if open_ else None,
                "replace_ids": list(open_.get("ids") or []) if open_ else []}
    return _with_state(step)


def recent(label: str, info: dict, t: float, mark: bool = False) -> bool:
    """0.21.4: was the same question of the same session label (repeated runs, new
    sessions) on the board within RECENT_DEDUPE_S? `mark`: it goes out now."""
    key = _qkey(f"{label}|{group_key(info)}")

    def step(st: dict):
        rec = st.get("recent") if isinstance(st.get("recent"), dict) else {}
        rec = {k: v for k, v in rec.items() if isinstance(v, (int, float)) and t - v < RECENT_DEDUPE_S}
        st["recent"] = rec
        seen = key in rec
        if mark:
            rec[key] = t
        return seen
    return bool(_with_state(step))


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


def set_ids(skey: str, qk: str, ids: list[str]) -> bool:
    """set_id for the decisions of a multi-question ask (0.21.3): True = delete them now."""
    def step(st: dict):
        me = st["sessions"].get(skey)
        open_ = me.get("open") if isinstance(me, dict) and isinstance(me.get("open"), dict) else None
        if open_ is None or open_.get("q") != qk:
            return True
        if open_.get("resolved"):
            me.pop("open", None)
            return True
        open_["ids"] = list(ids)
        open_["id"] = ids[0] if ids else None
        return False
    return bool(_with_state(step))


def open_slot(skey: str) -> dict | None:
    """The open decision slot of this session (a copy), or None."""
    def step(st: dict):
        me = st["sessions"].get(skey)
        open_ = me.get("open") if isinstance(me, dict) and isinstance(me.get("open"), dict) else None
        return dict(open_) if open_ and not open_.get("resolved") else None
    return _with_state(step)


def forget(skey: str, qk: str) -> bool:
    """The slot was answered in the call (asks.py): drop it without deleting the decision."""
    def step(st: dict):
        me = st["sessions"].get(skey)
        open_ = me.get("open") if isinstance(me, dict) and isinstance(me.get("open"), dict) else None
        if open_ is None or open_.get("q") != qk:
            return False
        me.pop("open", None)
        return True
    return bool(_with_state(step))


def resolve(skey: str, tool: str | None, t: float, stop: bool = False) -> dict | None:
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
        if stop and open_.get("kind") == "stop":
            return None   # 0.21.3: asked at this Stop (asks.py); the next turn resolves it
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
            rel = vmulti.load_relay(session=_base(str(job.get("skey") or "")))
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
        create(job)
    except Exception:  # noqa: BLE001, S110
        pass
    return 0


def _body(item: dict) -> dict:
    body: dict = {"question": item["question"]}
    opts = [o for o in item.get("options") or [] if isinstance(o, str)]
    if 2 <= len(opts) <= 3:
        body["options"] = opts
    ctx = item.get("context")
    if isinstance(ctx, str) and ctx.strip():
        body["context"] = ctx[:CONTEXT_MAX]
    return body


def _record(job: dict, dec_id: str, question: str, res: dict, item: dict) -> None:
    """0.21.4: a helper's mirrored question is on the board: the session gets a note at its
    next hook event, and the answer quotes the question (board.record_question)."""
    if job.get("route") != "relay":
        return  # the joining session gets its answers in `next`
    try:
        from . import board
        num = res.get("number") if isinstance(res, dict) else None
        board.record_question(str(job.get("skey") or ""), dec_id, question,
                              num if isinstance(num, int) else None,
                              item.get("options") or job.get("options") or [],
                              str(job.get("src") or "mirror-perm"))
    except Exception:  # noqa: BLE001, S110
        pass


def create(job: dict) -> list[str]:
    """Delete what the job replaces/deletes, create its decision(s) (one per item of a
    multi-question ask) and store their ids. The ids created (and kept), [] otherwise."""
    olds = [job.get("replace"), job.get("delete"), *(job.get("replace_ids") or []),
            *(job.get("delete_ids") or [])]
    done: set = set()
    for old in olds:
        if isinstance(old, str) and _ID.match(old) and old not in done:
            done.add(old)
            _decide(job, {"id": old, "delete": True})
    q = job.get("question")
    if job.get("op") != "create" or not isinstance(q, str) or not q:
        return []
    items = [i for i in job.get("items") or [] if isinstance(i, dict)
             and isinstance(i.get("question"), str) and i["question"]]
    if len(items) <= 1:
        item = items[0] if items else {"question": q, "options": job.get("options") or []}
        res = _decide(job, _body({**item, "question": q}))
        dec_id = res.get("id") if isinstance(res, dict) and res.get("ok") else None
        if isinstance(dec_id, str) and _ID.match(dec_id):
            if set_id(str(job.get("skey") or ""), str(job.get("qk") or _qkey(q)), dec_id):
                _decide(job, {"id": dec_id, "delete": True})
                return []
            _record(job, dec_id, q, res, item)
            return [dec_id]
        return []
    ids: list[str] = []
    for item in items[:ITEMS_MAX]:
        res = _decide(job, _body(item))
        dec_id = res.get("id") if isinstance(res, dict) and res.get("ok") else None
        if isinstance(dec_id, str) and _ID.match(dec_id):
            ids.append(dec_id)
            _record(job, dec_id, item["question"], res, item)
    qk = str(job.get("qk") or _qkey("\n".join(i["question"] for i in items)))
    if ids and set_ids(str(job.get("skey") or ""), qk, ids):
        for dec_id in ids:
            _decide(job, {"id": dec_id, "delete": True})
        return []
    return ids


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
        t = time.time() if now is None else now
        skey = _skey(event)
        rt, dir_, _rel = route(skey)
        label = ""
        if rt:
            from . import multisession as vmulti
            label = vmulti.session_label(event, _cached_label(skey))
        info = classify(event, label)
        if info is None:
            return None
        if info["kind"] == "done":
            return _resolve_job(skey, None, t, post)
        if not rt:
            return None
        _activity(rt, dir_, skey, raw, info["text"], event, t)
        if info["kind"] != "ask" or rt == "bridge" or unattended():
            return None  # 0.21.4: nobody at an unattended session could answer
        extra = {"dir": str(dir_)} if rt == "own" else {"from": label}
        if recent(label, info, t):
            return None
        adm = admit_ask(skey, info, rt, t, extra)
        if adm is None:
            return None
        recent(label, info, t, mark=True)
        job = ask_job(rt, skey, info, adm, extra)
        (post or spawn)(job)
        return job
    except Exception:  # noqa: BLE001 - a hook must never disturb Claude Code
        return None


def ask_job(rt: str, skey: str, info: dict, adm: dict, extra: dict) -> dict:
    """The create job of an admitted ask (handed to the detached child or to create())."""
    job = {"op": "create", "route": rt, "skey": skey, "question": info["question"],
           "options": info.get("options") or [], "replace": adm.get("replace"),
           **{k: v for k, v in extra.items() if k in ("dir", "from")}}
    # 0.21.4: where the question came from (the answer and the mirror note say it)
    job["src"] = ("mirror-stop" if extra.get("kind") == "stop" else
                  "mirror-ask" if info.get("tool") == ASK_TOOL else "mirror-perm")
    items = info.get("items") or []
    if adm.get("replace_ids"):
        job["replace_ids"] = adm["replace_ids"]
    if len(items) > 1 or (items and items[0].get("context")):
        job["items"] = [{k: i.get(k) or ("" if k != "options" else []) for k in
                         ("question", "options", "context")} for i in items]
        job["qk"] = group_key(info)
    return job


def _cached_label(skey: str) -> str:
    from . import multisession as vmulti
    try:
        st = json.loads((vact.state_root() / vmulti.STATE_NAME).read_text(encoding="utf-8"))
        me = st.get("sessions", {}).get(skey) if isinstance(st, dict) else None
        return str(me.get("label") or "") if isinstance(me, dict) else ""
    except (OSError, ValueError, AttributeError):
        return ""


def _resolve_job(skey: str, tool: str | None, t: float, post, stop: bool = False) -> dict | None:
    got = resolve(skey, tool, t, stop)
    if got is None:
        return None
    job = {"op": "delete", "route": got.get("route") or "", "skey": skey, "delete": got["id"]}
    if len(got.get("ids") or []) > 1:
        job["delete_ids"] = [i for i in got["ids"] if i != got["id"]]
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
        return _resolve_job(_skey(event), tool, time.time() if now is None else now, post,
                            stop=name == "Stop")
    except Exception:  # noqa: BLE001
        return None
