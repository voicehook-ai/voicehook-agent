"""Helper sessions in the call, fed by their own hooks (0.21.0): board reminder, session
state, answers to their questions, idle clear.

A helper session (a Claude Code session of this machine that is not itself in the call,
while ~/.voicehook/relay.json is usable, see multisession.py / relayops.py) has its own
status board in the call, keyed by its session name (`from`). Until 0.18 nothing reminded
it to keep that board fresh, so the call showed boards that were 3 to 37 minutes old.

The hook (`voicehook-agent-hook post-tool-use|user-prompt-submit|stop|...`) now:
* records when this session last set or cleared its board: a Bash call of
  `vh-helper status "<from>" "<doing>" [open ...]` or `voicehook-agent status [TEXT]
  [--doing/--open/--done/-f]` that did not fail (the command is parsed locally, nothing of
  it is ever sent or stored except the `from` name);
* while the session works (PostToolUse, UserPromptSubmit) and its board is older than
  REMIND_AFTER_S or was never set in this call, answers Claude Code with
  `hookSpecificOutput.additionalContext` (one short German + English line: age, the exact
  command with the session's name). At most one reminder per REMIND_GAP_S per session.
  Never a `decision`, so the tool call and the prompt are never blocked;
* on Stop starts one detached sleeper per session: when the session stays idle for
  IDLE_CLEAR_S after a Stop, the sleeper clears the session's board over the relay
  (POST <relay>/status {from} / local relay request), so an idle session leaves no stale
  board behind. Any new work before that cancels it;
* reports the session STATE read from the hook events alone (arbeitet | wartet | frage |
  leerlauf | problem, see next_state / tool_kind) on change: detached POST <relay>/state
  {from, state}; a tool call that only waits (`vh-helper next`, `relay-next`, `sleep`, an
  until-loop, Monitor) is `wartet` (0.21.2), not `arbeitet`;
* delivers the human's answers to the session's own questions (fetch_answers / deliver):
  additionalContext, or on Stop `decision: block` + reason, exactly once per answer.

Subagents (Agent tool, 0.21.1): their hook events carry the parent's session_id plus
agent_id; each subagent is its own helper (key session + agent_id hash), also inside the
joining session: own board name (the `from` it used, else `<agent_type> <4 hex>`), reminder
only after 3 min of work without a board, state only once it has a board.

Name: the `from` this session already used for its board, else the hook's session label
(multisession.session_label: $VOICEHOOK_SESSION_LABEL > /rename title > ai-title > cwd
basename + 4 hex), the same `from` the activity relay and `voicehook-agent status` use.

Silent for the joining session itself (owner_session of relay.json: its main board has its
own status_due reminder in `next`), without a usable relay.json, with VOICEHOOK_NO_RELAY=1
or VOICEHOOK_BOARD_REMIND=0. State: `board-state.json` in the state dir (0600, flock): only
session hashes, names, room and timestamps. stdlib only (the hook starts fast).
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import time

from . import activity as vact
from . import multisession as vmulti

STATE_NAME = "board-state.json"
REMIND_AFTER_S = 180.0     # board older than this while working -> reminder
REMIND_GAP_S = 180.0       # at most one reminder per session per this window
IDLE_CLEAR_S = 900.0       # idle this long after Stop -> the sleeper clears the board
ENV_OFF = "VOICEHOOK_BOARD_REMIND"
IDLE_ENV_OFF = "VOICEHOOK_BOARD_IDLE_CLEAR"
SESSIONS_KEEP = 50
WORK_EVENTS = ("PostToolUse", "PostToolUseFailure", "UserPromptSubmit")
REMIND_EVENTS = ("PostToolUse", "UserPromptSubmit")
_PUNCT = {"&&", "||", ";", "|", "&", "\n", "(", ")", ";;", "|&"}


def _off(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in ("0", "false", "no", "off")


# --------------------------------------------------------------------------- #
# which Bash command set the board
# --------------------------------------------------------------------------- #
def _tokens(command: str) -> list[str]:
    lex = shlex.shlex(vmulti._strip_heredocs(str(command or "")), posix=True,
                      punctuation_chars=True)
    lex.whitespace_split = True
    lex.commenters = ""
    return list(lex)


def _segments(command: str) -> list[list[str]]:
    try:
        toks = _tokens(command)
    except ValueError:  # unbalanced quotes: not a command we can read
        return []
    out: list[list[str]] = [[]]
    for t in toks:
        if t in _PUNCT or (t and set(t) <= set("&|;()<>")):
            out.append([])
        else:
            out[-1].append(t)
    return [s for s in out if s]


def _cmd_start(seg: list[str]) -> list[str]:
    """Drop env assignments and a leading `bash`/`sh`/`exec`/`command`/`uvx` wrapper."""
    i = 0
    while i < len(seg) and (vmulti._ENV_ASSIGN.match(seg[i]) or seg[i] in ("exec", "command")):
        i += 1
    if i < len(seg) and seg[i].rsplit("/", 1)[-1] in vmulti._SHELLS:
        i += 1
    return seg[i:]


def _vh_helper(args: list[str]) -> tuple[str, bool] | None:
    """vh-helper status "<from>" "<doing>" [open ...] -> (from, cleared)."""
    if len(args) < 2 or args[0] != "status" or not args[1].strip():
        return None
    doing = args[2] if len(args) > 2 else ""
    rest = [a for a in args[3:] if a.strip()]
    return args[1].strip(), not doing.strip() and not rest


def _cli_status(args: list[str]) -> tuple[str, bool] | None:
    """voicehook-agent status [TEXT] [--doing X] [--open X] [--done X] [-f F] -> ('', cleared).
    No text and no flag = the read-only status query: None."""
    if not args or args[0] != "status":
        return None
    vals: list[str] = []
    seen = False
    file_ = False
    i = 1
    while i < len(args):
        a = args[i]
        key, eq, val = a.partition("=")
        if key in ("--doing", "--open", "--done", "--faq", "-f", "--file"):
            if not eq:
                i += 1
                val = args[i] if i < len(args) else ""
            seen = True
            file_ = file_ or key in ("-f", "--file")
            if key != "--faq":
                vals.append(val)
        elif key in ("--session", "--json"):
            if key == "--session" and not eq:
                i += 1
        elif not a.startswith("-"):
            seen = True
            vals.append(a)
        i += 1
    if not seen:
        return None
    return "", not file_ and not any(v.strip() for v in vals)


def decide_command(command: object) -> str | None:
    """The `from` of a question this Bash command asked ('' = the CLI's session label):
    `vh-helper decide "<from>" "<question>"` or `voicehook-agent decide ...`. Else None."""
    try:
        for seg in _segments(str(command or "")):
            words = _cmd_start(seg)
            if not words:
                continue
            exe = words[0].rsplit("/", 1)[-1]
            if exe == "vh-helper" and len(words) > 3 and words[1] == "decide" and words[2].strip():
                return words[2].strip()
            if exe in ("voicehook-agent", "voicehook_agent") and len(words) > 1 \
                    and words[1] == "decide" and "--session" not in words:
                return ""
    except Exception:  # noqa: BLE001
        return None
    return None


def status_command(command: object) -> tuple[str, bool] | None:
    """(from, cleared) when the Bash command sets this session's board ('' = the CLI's own
    session label); None otherwise. Read-only, never raises."""
    try:
        for seg in _segments(str(command or "")):
            words = _cmd_start(seg)
            if not words:
                continue
            exe = words[0].rsplit("/", 1)[-1]
            if exe == "vh-helper":
                got = _vh_helper(words[1:])
            elif exe in ("voicehook-agent", "voicehook_agent"):
                got = _cli_status(words[1:])
            elif exe in ("uvx", "pipx") and len(words) > 2 and \
                    words[1].split("@")[0] == "voicehook-agent":
                got = _cli_status(words[2:])
            else:
                got = None
            if got is not None:
                return got
    except Exception:  # noqa: BLE001
        return None
    return None


# --------------------------------------------------------------------------- #
# which tool call only waits (0.21.2): wartet instead of arbeitet
# --------------------------------------------------------------------------- #
KIND_WAIT, KIND_META, KIND_WORK = "wait", "meta", "work"
# tools that only wait for something else (a background job, a condition)
WAIT_TOOLS = {"Monitor", "TaskOutput", "BashOutput"}
# shell keywords in front of a command (`until X; do sleep 5; done`)
_KEYWORDS = {"until", "while", "do", "done", "then", "else", "elif", "if", "fi", "!", "{", "}",
             "time"}
# commands that only look (the condition of a wait loop) or do nothing
_PROBES = {"echo", "printf", "true", "false", ":", "date", "test", "[", "[[", "]]", "curl",
           "grep", "egrep", "jq", "cat", "tail", "head", "ls", "pgrep", "ps", "wc", "break",
           "continue", "exit", "stat", "lsof", "nc"}
_GH_LOOK = {("run", "view"), ("run", "list"), ("pr", "checks"), ("pr", "view"), ("pr", "status")}
_WRAPPERS = {"timeout", "gtimeout", "nice", "nohup"}


def _wait_segments(command: str) -> list[list[str]]:
    """Segments like _segments, but a redirection drops its target (`sleep 5 >/dev/null 2>&1`)
    and shell keywords / `for x in ...` headers are no command of their own."""
    try:
        toks = _tokens(command)
    except ValueError:
        return []
    out: list[list[str]] = [[]]
    skip = False
    for t in toks:
        if skip:
            skip = False
            continue
        if t and set(t) <= set("&|<>") and ("<" in t or ">" in t):
            skip = True          # `>`, `>>`, `2>&`, `&>`: the next token is the target
            continue
        if t in _PUNCT or (t and set(t) <= set("&|;()<>")):
            out.append([])
        else:
            out[-1].append(t)
    segs = []
    for seg in out:
        while seg and seg[0] in _KEYWORDS:
            seg = seg[1:]
        if seg and seg[0] in ("for", "select", "case", "esac"):
            continue
        if seg:
            segs.append(seg)
    return segs


def _unwrap(words: list[str]) -> list[str]:
    words = _cmd_start(words)
    while words and words[0].rsplit("/", 1)[-1] in _WRAPPERS:
        words = words[1:]
        while words and (words[0].startswith("-") or words[0][:1].isdigit()):
            words = words[1:]    # timeout -k 5 60 <cmd> / nice -n 5 <cmd>
    if words and words[0] == "pipx" and words[1:2] == ["run"]:
        words = words[1:]        # pipx run voicehook-agent ...
    if len(words) > 1 and words[0] in ("uvx", "run") and words[1].split("@")[0] == "voicehook-agent":
        words = ["voicehook-agent"] + words[2:]
    return words


def _seg_kind(words: list[str]) -> str | None:
    """wait | meta | probe | None (= real work) for one simple command."""
    words = _unwrap(words)
    if not words:
        return "probe"
    exe = words[0].rsplit("/", 1)[-1]
    sub = words[1] if len(words) > 1 else ""
    if exe in ("sleep", "wait"):
        return KIND_WAIT
    if exe == "vh-helper":
        if sub == "next":
            return KIND_WAIT
        return KIND_META if sub in ("status", "activity") else None
    if exe in ("voicehook-agent", "voicehook_agent"):
        if sub in ("next", "relay-next"):
            return KIND_WAIT
        return KIND_META if sub in ("status", "activity") else None
    if exe == "gh":
        if words[1:3] == ["run", "watch"] or (words[1:3] == ["pr", "checks"] and "--watch" in words):
            return KIND_WAIT
        return "probe" if tuple(words[1:3]) in _GH_LOOK else None
    if exe == "curl" and any("/relay/next" in w for w in words[1:]):
        return KIND_WAIT         # the helper long poll by hand
    if exe in _PROBES:
        return "probe"
    return None


def command_kind(command: object) -> str:
    """wait: the Bash command only waits (at least one `sleep`/`wait`/`vh-helper next`/
    `voicehook-agent next|relay-next`/`gh run watch`/relay long poll, everything else only
    looks: an until-loop on curl/grep, echo, `vh-helper status`); meta: only board/activity
    bookkeeping; else work. Read-only, never raises."""
    try:
        kinds = [_seg_kind(s) for s in _wait_segments(str(command or ""))]
    except Exception:  # noqa: BLE001
        return KIND_WORK
    if not kinds or None in kinds:
        return KIND_WORK
    if KIND_WAIT in kinds:
        return KIND_WAIT
    return KIND_META if KIND_META in kinds else KIND_WORK


def tool_kind(event: dict) -> str:
    """wait | meta | work for the tool of a Pre/PostToolUse event (work when unsure)."""
    tool = str(event.get("tool_name") or "")
    if tool in WAIT_TOOLS or (tool.startswith("mcp__") and tool.rsplit("__", 1)[-1] == "next"):
        return KIND_WAIT
    if tool == "Bash":
        ti = event.get("tool_input") if isinstance(event.get("tool_input"), dict) else {}
        return command_kind(ti.get("command"))
    return KIND_WORK


# --------------------------------------------------------------------------- #
# state
# --------------------------------------------------------------------------- #
def _with_state(fn) -> object:
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
        sessions = st.get("sessions") if isinstance(st.get("sessions"), dict) else {}
        st["sessions"] = {k: v for k, v in sessions.items() if isinstance(v, dict)}
        res = fn(st)
        if len(st["sessions"]) > SESSIONS_KEEP:
            keep = sorted(st["sessions"].items(), key=lambda kv: float(kv[1].get("seen") or 0))
            st["sessions"] = dict(keep[-SESSIONS_KEEP:])
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, json.dumps(st).encode("utf-8"))
        return res
    finally:
        os.close(fd)


def _room(rel: dict) -> str:
    return str(rel.get("room") or "")


def _me(st: dict, skey: str, room: str, t: float) -> dict:
    """This session's entry; a new call (other room) starts without a board."""
    me = st["sessions"].setdefault(skey, {})
    if me.get("room") != room:
        name = me.get("from")
        me.clear()
        me.update(room=room)
        if isinstance(name, str) and name:
            me["from"] = name
    me["seen"] = t
    return me


def _float(v: object) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


# --------------------------------------------------------------------------- #
# reminder text
# --------------------------------------------------------------------------- #
def _q(s: str) -> str:
    return '"' + s.replace("\\", "").replace('"', "'").replace("`", "'").replace("$", "") + '"'


def command_hint(name: str, local: bool = False) -> tuple[str, str]:
    """(set, clear) command for this session: vh-helper when on PATH (it speaks the remote
    relay only), else the CLI (both relay kinds, same `from`)."""
    if not local and shutil.which("vh-helper"):
        return (f'vh-helper status {_q(name)} "<Schritt + ETA>"',
                f'vh-helper status {_q(name)} ""')
    return ('voicehook-agent status "<Schritt + ETA>"', 'voicehook-agent status ""')


def reminder_text(name: str, age_s: float | None, local: bool = False) -> str:
    set_cmd, clear_cmd = command_hint(name, local)
    if age_s is None:
        de = f"voicehook-Call: Du hast kein Board (Name {_q(name)})."
        en = "voicehook call: you have no status board yet."
    else:
        mins = max(1, int(age_s // 60))
        de = f"voicehook-Call: Dein Board ist {mins} Min alt."
        en = f"voicehook call: your status board is {mins} min old."
    return (f"{de} Jetzt `{set_cmd}` setzen (oder leeren mit `{clear_cmd}`, wenn du nichts "
            f"tust). / {en} Set it now with your current step + ETA (or clear it when idle). "
            "Keine Pfade, Secrets oder Kundendaten.")


def output(event_name: str, text: str) -> dict:
    """The hook's stdout object: additionalContext only, never a decision (nothing blocked)."""
    return {"hookSpecificOutput": {"hookEventName": event_name, "additionalContext": text}}


# --------------------------------------------------------------------------- #
# hook entry
# --------------------------------------------------------------------------- #
def _session_key(event: dict) -> str:
    return vmulti.session_hash(event.get("session_id")) or vmulti.own_session_hash()


def _agent_label(event: dict, skey: str) -> str:
    """Name of a subagent without its own `from` yet: `<agent_type> <4 hex>` (max 40)."""
    kind = vmulti._clean_label(event.get("agent_type") or "Subagent")[:30] or "Subagent"
    return f"{kind} {vmulti._h(str(event.get('agent_id') or ''), 'vh-agent-label')[:4]}"


def _label(event: dict, skey: str) -> str:
    from . import relayops
    return vmulti.session_label(event, relayops._cached_label(skey))[:vmulti.SESSION_LABEL_MAX]


STATE_WORK, STATE_ASK, STATE_IDLE, STATE_PROBLEM = "arbeitet", "frage", "leerlauf", "problem"
STATE_WAIT = "wartet"      # 0.21.2: only a wait/poll loop runs (tool_kind == wait)
STATES = (STATE_WORK, STATE_WAIT, STATE_ASK, STATE_IDLE, STATE_PROBLEM)
FAIL_STREAK = 3            # failed tool calls in a row -> problem
STATE_RESEND_S = 600.0     # same state again at most this often (server restart heals)
ASK_EVENTS = ("Notification", "PermissionRequest")
NAME_EVENTS = ("UserPromptSubmit", "Stop", "SessionStart")


def _failed(event: dict) -> bool:
    name = str(event.get("hook_event_name") or "")
    if name == "PostToolUseFailure":
        return event.get("is_interrupt") is not True
    return name == "PostToolUse" and vmulti._resp_failed(event.get("tool_response"))


def next_state(event: dict, me: dict) -> str | None:
    """Session state from the hook event alone (never from what the agent writes):
    PreToolUse/PostToolUse/UserPromptSubmit -> arbeitet; a tool call that only waits
    (tool_kind == wait: `vh-helper next`, `relay-next`, `sleep`, until-loop, Monitor) ->
    wartet, board bookkeeping (`vh-helper status`) keeps wartet; Notification,
    PermissionRequest, AskUserQuestion -> frage; Stop -> leerlauf; FAIL_STREAK failed tools in
    a row -> problem. Updates the failure streak in `me`."""
    name = str(event.get("hook_event_name") or "")
    tool = str(event.get("tool_name") or "")
    if name in ASK_EVENTS or (name == "PreToolUse" and tool == "AskUserQuestion"):
        return STATE_ASK
    if name == "Stop":
        me["fails"] = 0
        return STATE_IDLE
    if name == "UserPromptSubmit":
        me["fails"] = 0
        return STATE_WORK
    if name in ("PostToolUse", "PostToolUseFailure"):
        if name == "PostToolUseFailure" and event.get("is_interrupt") is True:
            return None
        me["fails"] = int(me.get("fails") or 0) + 1 if _failed(event) else 0
        return STATE_PROBLEM if me["fails"] >= FAIL_STREAK else _tool_state(event, me)
    if name == "PreToolUse":
        return STATE_PROBLEM if int(me.get("fails") or 0) >= FAIL_STREAK else _tool_state(event, me)
    return None


def _tool_state(event: dict, me: dict) -> str:
    kind = tool_kind(event)
    if kind == KIND_WAIT or (kind == KIND_META and me.get("state") == STATE_WAIT):
        return STATE_WAIT
    return STATE_WORK


def _known_name(skey: str) -> bool:
    try:
        st = json.loads((vact.state_root() / STATE_NAME).read_text(encoding="utf-8"))
        me = st.get("sessions", {}).get(skey)
        return isinstance(me, dict) and bool(me.get("from") or me.get("name"))
    except (OSError, ValueError, AttributeError):
        return False


def handle(raw: str, now: float | None = None, spawn=None, post=None, fetch=None) -> dict | None:
    """One hook event of a helper session: board age, session state, answers to its questions.
    Returns the hook's stdout object or None:
    * PostToolUse / UserPromptSubmit / SessionStart: {hookSpecificOutput: {additionalContext}}
      (answers first, then the board reminder);
    * Stop: {decision: block, reason} ONLY when a new, undelivered answer arrived (each answer
      is delivered exactly once, so this never loops).
    Never raises; the only network in the hook itself is one short answer poll."""
    try:
        if _off(ENV_OFF) or vmulti.relay_disabled():
            return None
        event = json.loads(raw) if raw and raw.strip() else {}
        if not isinstance(event, dict):
            return None
        name_ev = str(event.get("hook_event_name") or "")
        rel = vmulti.load_relay(now)
        if rel is None:
            return None
        skey = _session_key(event)
        agent = str(event.get("agent_id") or "").strip()
        if not skey or (skey == str(rel.get("owner_session") or "") and not agent):
            return None  # the joining session: own board (status_due), own `next`
        if agent:  # 0.21.1: a subagent (Agent tool) has its own board, name, state, answers
            skey = f"{skey}-a{vmulti._h(agent, 'vh-agent')[:8]}"
        t = time.time() if now is None else now
        room = _room(rel)
        local = vmulti._is_local(rel)
        got = asked = None
        if name_ev == "PostToolUse" and str(event.get("tool_name") or "") == "Bash" \
                and not _failed(event):
            cmd = (event.get("tool_input") or {}).get("command") \
                if isinstance(event.get("tool_input"), dict) else ""
            got = status_command(cmd)
            asked = decide_command(cmd)
        is_ask = name_ev in ASK_EVENTS or (name_ev == "PreToolUse"
                                           and str(event.get("tool_name") or "") == "AskUserQuestion")
        need = name_ev in NAME_EVENTS or got is not None or asked is not None or is_ask \
            or not _known_name(skey)
        label = (_agent_label(event, skey) if agent else _label(event, skey)) if need else ""

        def step(st: dict):
            me = _me(st, skey, room, t)
            if label:
                me["name"] = label
            if got is not None:
                frm, cleared = got
                me["from"] = frm or label or me.get("from") or ""
                me.update(at=t, cleared=cleared)
            asks = me.get("asks") if isinstance(me.get("asks"), dict) else {}
            asks = {k: v for k, v in asks.items() if t - _float(v) < ASK_KEEP_S}
            if asked is not None:
                asks[asked or label or str(me.get("from") or "")] = t
                me["poll_at"] = t        # just asked: the answer takes a while
            if is_ask and label:
                asks[label] = t          # notify.py asks under the session label
            asks.pop("", None)
            me["asks"] = asks
            name = str(me.get("from") or me.get("name") or label or "Session")
            out: dict = {"name": name}
            if name_ev in WORK_EVENTS:
                me["work_at"] = t
            state = next_state(event, me)
            if agent and not me.get("from"):
                state = None     # a subagent's state goes out once it has its own board
            if state and not local and not _off(STATE_ENV_OFF) and (
                    state != me.get("state") or t - _float(me.get("state_at")) >= STATE_RESEND_S):
                me.update(state=state, state_at=t)
                out["state"] = state
            if asks and (name_ev in NAME_EVENTS or
                         (name_ev == "PostToolUse" and t - _float(me.get("poll_at")) >= POLL_GAP_S)):
                me["poll_at"] = t
                out["poll"] = sorted(asks, key=lambda k: -_float(asks[k]))[:POLL_MAX]
            if name_ev == "Stop":
                me["stop_at"] = t
                if _float(me.get("sleeper_until")) < t and me.get("at") and not me.get("cleared"):
                    me["sleeper_until"] = t + IDLE_CLEAR_S
                    out["spawn"] = True
            me.setdefault("first_at", t)
            if got is None and name_ev in REMIND_EVENTS:
                at = _float(me.get("at"))
                age = None if not at else t - at
                # a subagent without a board is reminded only once it works for 3 min (a
                # short Explore/Read subagent never gets the line)
                young = agent and at == 0 and t - _float(me.get("first_at")) < REMIND_AFTER_S
                if not young and (age is None or age >= REMIND_AFTER_S) and \
                        t - _float(me.get("reminded_at")) >= REMIND_GAP_S:
                    me["reminded_at"] = t
                    out["remind"] = reminder_text(name, None if me.get("cleared") else age, local)
            return out
        res = _with_state(step)
        if res.get("state"):
            (post or _spawn_post)({"op": "state", "from": res["name"], "state": res["state"]})
        if res.get("spawn") and not _off(IDLE_ENV_OFF):
            (spawn or _spawn_sleeper)(skey)
        answers = deliver(skey, rel, res.get("poll") or [], fetch=fetch) if res.get("poll") else []
        texts = [answer_text(a) for a in answers]
        if name_ev == "Stop":
            if not texts:
                return None
            return {"decision": "block", "reason": " ".join(texts)}
        if name_ev in REMIND_EVENTS or name_ev == "SessionStart":
            ctx = texts + ([res["remind"]] if res.get("remind") else [])
            return output(name_ev, " ".join(ctx)) if ctx else None
        return None
    except Exception:  # noqa: BLE001 - a hook must never disturb Claude Code
        return None


# --------------------------------------------------------------------------- #
# answers to this session's own questions (delivered by the hook, exactly once)
# --------------------------------------------------------------------------- #
ASK_KEEP_S = 6 * 3600.0    # poll for answers this long after the session's last question
POLL_GAP_S = 20.0          # PostToolUse polls at most this often (Prompt/Stop/Start always)
POLL_MAX = 3               # names polled per hook run
FETCH_TIMEOUT_S = 1.2
DELIVERED_KEEP = 100
STATE_ENV_OFF = "VOICEHOOK_SESSION_STATE"


def fetch_answers(rel: dict, frm: str) -> list[dict]:
    """Decision events for `frm`: the local inbox (left over by `decide --wait`) first, then
    the relay (remote: GET <relay>/next?from=..&timeout=0; local: the join's answer file).
    Taken = consumed. Never raises."""
    from urllib.parse import quote

    from . import relayops
    out: list[dict] = []
    try:
        out += relayops.take_jsonl(relayops.inbox_path(frm), relayops._is_decision)
        if vmulti._is_local(rel):
            out += relayops.take_jsonl(relayops.answer_path(relayops.local_dir(rel), frm),
                                       relayops._is_decision)
        else:
            url = f"{relayops.endpoint(rel, 'next')}?from={quote(frm, safe='')}&timeout=0"
            status, data = relayops._http(rel, "GET", url, None, FETCH_TIMEOUT_S)
            evs = data.get("events") if status < 300 else None
            if isinstance(evs, list):
                out += [e for e in evs if isinstance(e, dict) and e.get("type", "decision") == "decision"]
    except Exception:  # noqa: BLE001, S110 - best effort, the next hook run polls again
        pass
    return out


def _answer_key(ev: dict) -> str:
    return vmulti._h("|".join(str(ev.get(k) or "") for k in ("id", "verdict", "option", "text")),
                     "vh-answer")


def deliver(skey: str, rel: dict, names: list[str], fetch=None) -> list[dict]:
    """New answers for this session (deduped across hook runs by a hash of id + verdict)."""
    evs: list[dict] = []
    for frm in names:
        evs += (fetch or fetch_answers)(rel, frm)
    if not evs:
        return []

    def step(st: dict):
        me = st["sessions"].setdefault(skey, {})
        seen = [x for x in (me.get("delivered") or []) if isinstance(x, str)]
        new = []
        for ev in evs:
            k = _answer_key(ev)
            if k not in seen:
                seen.append(k)
                new.append(ev)
        me["delivered"] = seen[-DELIVERED_KEEP:]
        return new
    return _with_state(step)


_VERDICT = {"yes": "Ja", "no": "Nein"}


def answer_text(ev: dict) -> str:
    """`voicehook-Call: Antwort von Oliver auf deine Frage #3 ("Mergen?"): Ja. Handle jetzt
    danach.` + English. Text fields scrubbed and capped."""
    num = ev.get("number")
    nr = f" #{num}" if isinstance(num, int) and not isinstance(num, bool) else ""
    by = vact.scrub(str(ev.get("by") or "")).strip()[:40] or "dem Menschen"
    q = vact.scrub(str(ev.get("question") or "")).strip()[:160]
    verdict = str(ev.get("verdict") or "")
    text = vact.scrub(str(ev.get("text") or "")).strip()[:300]
    if verdict == "option":
        opt = ev.get("option")
        ans = text or (f"Option {opt + 1}" if isinstance(opt, int) and not isinstance(opt, bool) else "Option")
    else:
        ans = text or _VERDICT.get(verdict, verdict or "?")
        if text and verdict in _VERDICT:
            ans = f"{_VERDICT[verdict]} ({text})"
    qpart = f" ({_q(q)})" if q else ""
    return (f"voicehook-Call: Antwort von {by} auf deine Frage{nr}{qpart}: {ans}. Handle jetzt "
            f"danach. / Answer from {by} to your question{nr}: {ans}. Act on it now.")


def _spawn_post(job: dict) -> None:
    """Detached `voicehook-agent-hook board-post` (job on stdin): state / clear over the relay."""
    import subprocess
    try:
        p = subprocess.Popen(vact.child_argv("board-post"), stdin=subprocess.PIPE,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True, cwd=vact.child_cwd())
        assert p.stdin is not None
        p.stdin.write(json.dumps(job).encode("utf-8"))
        p.stdin.close()
    except Exception:  # noqa: BLE001
        return


def post_state(rel: dict, frm: str, state: str) -> bool:
    """POST <relay>/state {from, state} (server merges it into the helper's board card).
    An older server answers 404/405: ignored; one without `wartet` (422) gets `leerlauf`
    instead. Remote relay only."""
    from . import relayops
    if vmulti._is_local(rel) or state not in STATES:
        return False
    try:
        url = relayops.endpoint(rel, "state")
        status, _ = relayops._http(rel, "POST", url, {"from": frm, "state": state},
                                   relayops.HTTP_TIMEOUT_S)
        if status == 422 and state == STATE_WAIT:
            status, _ = relayops._http(rel, "POST", url, {"from": frm, "state": STATE_IDLE},
                                       relayops.HTTP_TIMEOUT_S)
        return status < 300
    except Exception:  # noqa: BLE001
        return False


def post_main(raw: str) -> int:
    """The detached child of the hook: one state update. Always 0."""
    try:
        job = json.loads(raw or "{}")
        rel = vmulti.load_relay()
        if isinstance(job, dict) and rel is not None and job.get("op") == "state":
            from . import relayops
            frm = relayops.check_from(str(job.get("from") or ""))
            post_state(rel, frm, str(job.get("state") or ""))
    except Exception:  # noqa: BLE001, S110 - detached child, nobody to tell
        pass
    return 0


# --------------------------------------------------------------------------- #
# idle sleeper (detached child of the Stop hook)
# --------------------------------------------------------------------------- #
def _spawn_sleeper(skey: str) -> None:
    import subprocess
    try:
        subprocess.Popen(vact.child_argv("board-idle", skey), stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True, cwd=vact.child_cwd())
    except Exception:  # noqa: BLE001
        return


def clear_board(rel: dict, frm: str) -> bool:
    """Remove this helper's board over the relay (empty status). True on success."""
    from . import relayops
    try:
        if vmulti._is_local(rel):
            res = relayops.local_request(rel, frm, "status", {})
        else:
            res = relayops.remote_status(rel, frm, {})
        return bool(res.get("ok"))
    except Exception:  # noqa: BLE001
        return False


def idle_step(skey: str, now: float | None = None, clear=clear_board) -> float | None:
    """One sleeper check: seconds to sleep on, or None (done: cleared, resumed or gone)."""
    t = time.time() if now is None else now
    rel = vmulti.load_relay(t)

    def step(st: dict):
        me = st["sessions"].get(skey)
        if not isinstance(me, dict):
            return ("done", None)
        stop_at, work_at = _float(me.get("stop_at")), _float(me.get("work_at"))
        if rel is None or me.get("room") != _room(rel) or not me.get("at") or me.get("cleared") \
                or work_at > stop_at:
            me["sleeper_until"] = 0
            return ("done", None)
        due = stop_at + IDLE_CLEAR_S
        if t < due:
            me["sleeper_until"] = due
            return ("sleep", due - t)
        me["sleeper_until"] = 0
        return ("clear", str(me.get("from") or me.get("name") or ""))
    kind, val = _with_state(step)
    if kind == "sleep":
        return val
    if kind == "clear" and val and rel is not None and clear(rel, val):
        def mark(st: dict):
            me = st["sessions"].get(skey)
            if isinstance(me, dict):
                me.update(cleared=True, at=t, auto_cleared=True)
        _with_state(mark)
    return None


def idle_main(skey: str, sleep=time.sleep) -> int:
    """The detached sleeper: wait until the session is idle IDLE_CLEAR_S after its Stop,
    then clear its board. Always 0."""
    try:
        for _ in range(100):  # each round sleeps until the newest Stop + IDLE_CLEAR_S
            wait = idle_step(skey)
            if wait is None:
                return 0
            sleep(max(1.0, wait))
    except Exception:  # noqa: BLE001, S110 - detached child, nobody to tell
        pass
    return 0
