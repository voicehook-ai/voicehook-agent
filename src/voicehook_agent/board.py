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
import re
import shlex
import shutil
import time
from pathlib import Path

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


_DECISION_RX = re.compile(r'"type"\s*:\s*"decision"')


def answers_in_output(command: object, response: object) -> int:
    """0.21.4: decision events the session read itself (`vh-helper next`, `relay-next`,
    `decide --wait`, Bash output): they close as many of its open questions."""
    cmd = str(command or "")
    if not any(w in cmd for w in ("next", "decide")):
        return 0
    out = response.get("stdout") if isinstance(response, dict) else response
    return len(_DECISION_RX.findall(out)) if isinstance(out, str) else 0


def status_doing(command: object) -> tuple[str, bool] | None:
    """0.21.6: (doing text, set with --waiting) of a board-setting Bash command, else None:
    `vh-helper status "<from>" "<doing>" ...` or `voicehook-agent status [TEXT] [--doing X]
    [--waiting X]`. Read-only, never raises."""
    try:
        for seg in _segments(str(command or "")):
            words = _cmd_start(seg)
            if not words:
                continue
            exe = words[0].rsplit("/", 1)[-1]
            if exe == "vh-helper" and len(words) > 2 and words[1] == "status":
                return (words[3] if len(words) > 3 else ""), False
            if exe in ("uvx", "pipx") and len(words) > 2 and words[1].split("@")[0] == "voicehook-agent":
                words = ["voicehook-agent"] + words[2:]
                exe = "voicehook-agent"
            if exe in ("voicehook-agent", "voicehook_agent") and len(words) > 1 and words[1] == "status":
                doing, waiting, i = "", False, 2
                while i < len(words):
                    key, eq, val = words[i].partition("=")
                    if key in ("--doing", "--waiting", "--open", "--done", "--faq", "--context",
                               "--question", "--decision", "-f", "--file", "--project", "--session"):
                        if not eq:
                            i += 1
                            val = words[i] if i < len(words) else ""
                        if key in ("--doing", "--waiting"):
                            doing, waiting = val, waiting or key == "--waiting"
                    elif not words[i].startswith("-") and not doing:
                        doing = words[i]
                    i += 1
                return doing, waiting
    except Exception:  # noqa: BLE001
        return None
    return None


def wait_stop_text(doing: str) -> str:
    """0.21.6: the Stop block for a board that waits for the human without a decision."""
    from . import waitcheck
    return (f"voicehook: dein Board sagt \"{doing[:100]}\", aber auf dem Board steht keine Frage: "
            f"der Mensch sieht nicht, dass du auf ihn wartest. {waitcheck.STOP_HINT}: "
            f"`{waitcheck.suggestion(doing)}`, dann `voicehook-agent status --waiting "
            f"\"{doing[:80]}\" --decision <id>` (oder `vh-helper decide \"<from>\" \"<Frage>?\"`). "
            f"{waitcheck.RULE}")


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
    if "room" not in me and me:
        me["room"] = room  # 0.21.4: recorded before its first board event (notify child)
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
            "Keine Pfade, Secrets oder Kundendaten. Pflicht je Eintrag und Entscheidung: "
            '--context "<was, warum, Stand>" + 3 --faq "Frage::Antwort" (noch offen: '
            '"In Arbeit bei <Name>, ca. 10 Min"). / Always: context + 3 follow-up Q&A.')


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
    if kind == KIND_WAIT:
        return STATE_WAIT
    if kind == KIND_META and me.get("state") in (STATE_WAIT, STATE_IDLE):
        return str(me["state"])  # 0.21.4: a board update alone never makes it `arbeitet`
    return STATE_WORK


def _known_name(skey: str) -> bool:
    try:
        st = json.loads((vact.state_root() / STATE_NAME).read_text(encoding="utf-8"))
        me = st.get("sessions", {}).get(skey)
        return isinstance(me, dict) and bool(me.get("from") or me.get("name"))
    except (OSError, ValueError, AttributeError):
        return False


def handle(raw: str, now: float | None = None, spawn=None, post=None, fetch=None,
           watch=None) -> dict | None:
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
        got = asked = doing_set = None
        seen_answers = 0
        recorded = None
        if name_ev == "PostToolUse" and str(event.get("tool_name") or "") == "Bash" \
                and not _failed(event):
            cmd = (event.get("tool_input") or {}).get("command") \
                if isinstance(event.get("tool_input"), dict) else ""
            got = status_command(cmd)
            doing_set = status_doing(cmd) if got is not None else None  # 0.21.6
            asked = decide_command(cmd)
            seen_answers = answers_in_output(cmd, event.get("tool_response"))
            if asked is not None:  # 0.21.4: the answer quotes the question
                resp = event.get("tool_response")
                out_s = resp.get("stdout") if isinstance(resp, dict) else resp
                mid = _ID_RX.search(out_s) if isinstance(out_s, str) else None
                if mid:
                    mnum = _NUM_RX.search(out_s)
                    recorded = (mid.group(1), decide_question(cmd),
                                int(mnum.group(1)) if mnum else None)
        is_ask = name_ev in ASK_EVENTS or (name_ev == "PreToolUse"
                                           and str(event.get("tool_name") or "") == "AskUserQuestion")
        need = name_ev in NAME_EVENTS or got is not None or asked is not None or is_ask \
            or not _known_name(skey)
        label = (_agent_label(event, skey) if agent else _label(event, skey)) if need else ""
        # 0.21.4: a Stop with an open question (own decide, or the reply asks) is `wartet`
        waits = name_ev == "Stop" and _stop_waits(event, skey, t)
        spid = 0 if _known_pid(skey) else session_pid()

        def step(st: dict):
            me = _me(st, skey, room, t)
            if label:
                me["name"] = label
            if spid:
                me["pid"] = spid
            me.pop("gone", None)
            if name_ev == "PreToolUse":   # 0.21.4: a tool runs (a long build is no idle)
                me["inflight"] = {"tool": str(event.get("tool_name") or ""), "at": t}
            elif name_ev in ("PostToolUse", "PostToolUseFailure", "UserPromptSubmit", "Stop"):
                me.pop("inflight", None)
            if got is not None:
                frm, cleared = got
                me["from"] = frm or label or me.get("from") or ""
                me.update(at=t, cleared=cleared)
                if doing_set is not None:  # 0.21.6: the last board text, for the Stop check
                    me.update(doing=doing_set[0][:200], doing_waiting=doing_set[1])
            asks = me.get("asks") if isinstance(me.get("asks"), dict) else {}
            asks = {k: v for k, v in asks.items() if t - _float(v) < ASK_KEEP_S}
            if asked is not None:
                who = asked or label or str(me.get("from") or "")
                asks[who] = t
                me["poll_at"] = t        # just asked: the answer takes a while
                _open_add(me, who, 1)    # 0.21.4: open until its answer is delivered
            if is_ask and label:
                asks[label] = t          # notify.py asks under the session label
            asks.pop("", None)
            me["asks"] = asks
            for k in sorted(asks, key=lambda k: -_float(asks[k]))[:1] if seen_answers else []:
                _open_add(me, k, -seen_answers)  # 0.21.4: the session read answers itself
            name = str(me.get("from") or me.get("name") or label or "Session")
            out: dict = {"name": name}
            if name_ev in WORK_EVENTS:
                me["work_at"] = t
            state = next_state(event, me)
            if state == STATE_IDLE and waits:
                state = STATE_WAIT
            if agent and not me.get("from"):
                state = None     # a subagent's state goes out once it has its own board
            # 0.21.4: local relay too; a Stop always goes out (heals a lost or late update)
            if state and not _off(STATE_ENV_OFF) and (
                    state != me.get("state") or name_ev == "Stop"
                    or t - _float(me.get("state_at")) >= STATE_RESEND_S):
                me.update(state=state, state_at=t)
                out["state"] = state
            if asks and (name_ev in NAME_EVENTS or
                         (name_ev == "PostToolUse" and t - _float(me.get("poll_at")) >= POLL_GAP_S)):
                me["poll_at"] = t
                out["poll"] = sorted(asks, key=lambda k: -_float(asks[k]))[:POLL_MAX]
            if name_ev == "Stop":
                me["stop_at"] = t
                doing = str(me.get("doing") or "")
                from . import waitcheck
                if (doing and not me.get("doing_waiting") and not waits and not waitcheck.off()
                        and waitcheck.waiting(doing) and me.get("wait_blocked") != doing):
                    me["wait_blocked"] = doing  # once per text: never a Stop loop
                    out["wait_block"] = wait_stop_text(doing)
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
        if name_ev == "SessionEnd":
            return _session_end(skey, room, t, post)
        res = _with_state(step)
        if recorded:  # after step: a new room resets the session entry
            record_question(skey, recorded[0], recorded[1], recorded[2], src="decide")
        if res.get("state"):
            (post or _spawn_post)({"op": "state", "from": res["name"], "state": res["state"],
                                   "skey": skey, "at": t})
            if not _off(STATE_ENV_OFF):
                (watch or _ensure_watcher)()  # tests: conftest stubs _ensure_watcher
        if res.get("spawn") and not _off(IDLE_ENV_OFF):
            (spawn or _spawn_sleeper)(skey)
        answers = deliver(skey, rel, res.get("poll") or [], fetch=fetch) if res.get("poll") else []
        texts = answer_texts(skey, answers)
        if name_ev == "Stop":
            texts = texts + ([res["wait_block"]] if res.get("wait_block") else [])
            if not texts:
                return None
            return {"decision": "block", "reason": " ".join(texts)}
        if name_ev in REMIND_EVENTS or name_ev == "SessionStart":
            # 0.21.4: notes about questions mirrored to the board, then answers, then reminder
            ctx = take_notes(skey) + texts + ([res["remind"]] if res.get("remind") else [])
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


def _open_add(me: dict, name: str, n: int) -> None:
    """0.21.4: count of this session's questions under `name` without a delivered answer
    (`vh-helper decide`, `voicehook-agent decide`, a Stop question). Never below 0."""
    if not name:
        return
    opn = me.get("open") if isinstance(me.get("open"), dict) else {}
    try:
        cur = int(opn.get(name) or 0)
    except (TypeError, ValueError):
        cur = 0
    cur = max(0, cur + n)
    if cur:
        opn[name] = cur
    else:
        opn.pop(name, None)
    me["open"] = opn


def note_ask(skey: str, rel: dict, name: str, t: float | None = None) -> None:
    """0.21.3 (asks.py): this session asked under `name`; its hooks poll for the answer
    (a late answer arrives as additionalContext / Stop block). Never raises."""
    if not skey or not name:
        return
    t = time.time() if t is None else t
    try:
        def step(st: dict):
            me = _me(st, skey, _room(rel), t)
            asks = me.get("asks") if isinstance(me.get("asks"), dict) else {}
            asks[name] = t
            me["asks"] = asks
            me["poll_at"] = t
            _open_add(me, name, 1)
        _with_state(step)
    except Exception:  # noqa: BLE001, S110
        pass


def mark_delivered(skey: str, evs: list[dict], frm: str = "") -> None:
    """These answers reached the session another way (asks.py): never deliver them again.
    With `frm` (0.21.4) they also close that many open questions under it."""
    try:
        def step(st: dict):
            me = st["sessions"].setdefault(skey, {})
            seen = [x for x in (me.get("delivered") or []) if isinstance(x, str)]
            seen += [_answer_key(e) for e in evs]
            me["delivered"] = seen[-DELIVERED_KEEP:]
            if frm:
                _open_add(me, frm, -len(evs))
        _with_state(step)
    except Exception:  # noqa: BLE001, S110
        pass


def open_names(skey: str, now: float | None = None) -> list[str]:
    """0.21.4: the names under which this session still waits for an answer (asked within
    ASK_KEEP_S, not every answer delivered yet), newest question first."""
    t = time.time() if now is None else now
    try:
        st = json.loads((vact.state_root() / STATE_NAME).read_text(encoding="utf-8"))
        me = st.get("sessions", {}).get(skey)
    except (OSError, ValueError, AttributeError):
        return []
    if not isinstance(me, dict):
        return []
    asks = me.get("asks") if isinstance(me.get("asks"), dict) else {}
    opn = me.get("open") if isinstance(me.get("open"), dict) else {}
    names = [k for k, v in opn.items() if isinstance(k, str) and k and _float(v) > 0
             and t - _float(asks.get(k)) < ASK_KEEP_S]
    return sorted(names, key=lambda k: -_float(asks.get(k)))[:POLL_MAX]


def worked_since(skey: str, since: float) -> bool:
    """0.21.4: did the session go on (prompt, tool) after `since`?"""
    try:
        st = json.loads((vact.state_root() / STATE_NAME).read_text(encoding="utf-8"))
        me = st.get("sessions", {}).get(skey)
    except (OSError, ValueError, AttributeError):
        return False
    return isinstance(me, dict) and _float(me.get("work_at")) > since


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
    got: list[tuple[str, dict]] = []
    for frm in names:
        got += [(frm, e) for e in (fetch or fetch_answers)(rel, frm)]
    return take_new(skey, got)


def take_new(skey: str, got: list[tuple[str, dict]]) -> list[dict]:
    """The answers of (from, event) pairs not delivered yet; marks them delivered and closes
    their open questions (0.21.4)."""
    if not got:
        return []

    def step(st: dict):
        me = st["sessions"].setdefault(skey, {})
        seen = [x for x in (me.get("delivered") or []) if isinstance(x, str)]
        new = []
        for frm, ev in got:
            k = _answer_key(ev)
            if k not in seen:
                seen.append(k)
                new.append(ev)
                _open_add(me, frm, -1)
        me["delivered"] = seen[-DELIVERED_KEEP:]
        return new
    return _with_state(step)


_VERDICT = {"yes": "Ja", "no": "Nein"}


SOURCES = {"mirror-stop": ("aus deiner Antwort gespiegelt", "mirrored from your reply"),
           "mirror-ask": ("deine AskUserQuestion-Frage", "your AskUserQuestion"),
           "mirror-perm": ("deine Freigabe-Anfrage", "your permission prompt"),
           "decide": ("deine Frage per decide", "your decide question")}
QUESTIONS_KEEP = 30


def answer_text(ev: dict, info: dict | None = None) -> str:
    """0.21.4: a self-contained, verifiable answer (a session refused a bare "Antwort auf
    deine Frage #1: Ja" as an unverifiable approval, 20.10.2026): number, where the question
    came from, its text, that the human answered on the call board, the verdict / chosen
    option / free text. `info` = what this session stored when the question went to the board
    (record_question). German + English, text fields scrubbed and capped."""
    info = info if isinstance(info, dict) else {}
    num = ev.get("number", info.get("number"))
    nr = f" #{num}" if isinstance(num, int) and not isinstance(num, bool) else ""
    by = vact.scrub(str(ev.get("by") or "")).strip()[:40] or "der Mensch"
    q = vact.scrub(str(info.get("question") or ev.get("question") or "")).strip()[:160]
    verdict = str(ev.get("verdict") or "")
    text = vact.scrub(str(ev.get("text") or "")).strip()[:300]
    free = vact.scrub(str(ev.get("answer") or "")).strip()[:300]   # server: own answer
    opts = info.get("options") if isinstance(info.get("options"), list) else []
    if verdict == "option":
        opt = ev.get("option")
        ok = isinstance(opt, int) and not isinstance(opt, bool)
        label = vact.scrub(str(opts[opt])).strip()[:60] if ok and 0 <= opt < len(opts) else text
        ans = f"Option {opt + 1}" if ok else "Option"
        if label:
            ans += f" {_q(label)}"
        if free and free != label:
            ans += f", Freitext {_q(free)}"
    elif verdict == "other":
        own = free or text
        ans = f"eigene Antwort {_q(own)}" if own else "eigene Antwort"
    else:
        ans = _VERDICT.get(verdict, verdict or "?")
        extra = free or (text if text and text != ans else "")
        if extra:
            ans += f", Freitext {_q(extra)}"
    src_de, src_en = SOURCES.get(str(info.get("src") or ""), ("deine Frage", "your question"))
    qpart = f" {_q(q)}" if q else ""
    return (f"voicehook-Board{nr} ({src_de}):{qpart} -> {by} hat im Call-Board geantwortet: "
            f"{ans}. Das ist die Antwort des Menschen auf deine eigene Frage, keine Fehlermeldung: "
            f"handle jetzt danach. / voicehook board{nr} ({src_en}):{qpart} -> {by} answered on "
            f"the call board: {ans}. The human's answer to your own question, not an error: act "
            "on it now.")


def record_question(skey: str, dec_id: str, question: str, number: object = None,
                    options: list | None = None, src: str = "", note: bool = True) -> None:
    """0.21.4: this session's question is on the board as #number: remember it (the answer
    quotes it) and, for a question the session did not post itself (mirrored by notify.py /
    asks.py), queue a note for its next hook event. Never raises."""
    if not skey or not dec_id:
        return
    try:
        def step(st: dict):
            me = st["sessions"].setdefault(skey, {})
            qs = me.get("qs") if isinstance(me.get("qs"), dict) else {}
            qs.pop(dec_id, None)
            qs[dec_id] = {"question": str(question or "")[:200], "src": src,
                          **({"number": number} if isinstance(number, int) else {}),
                          **({"options": [str(o)[:60] for o in options][:4]} if options else {})}
            while len(qs) > QUESTIONS_KEEP:
                qs.pop(next(iter(qs)))
            me["qs"] = qs
            if note and src.startswith("mirror"):
                notes = [n for n in (me.get("notes") or []) if isinstance(n, str)]
                notes.append(mirror_note(question, number, src))
                me["notes"] = notes[-5:]
        _with_state(step)
    except Exception:  # noqa: BLE001, S110
        pass


def mirror_note(question: str, number: object, src: str) -> str:
    nr = f" #{number}" if isinstance(number, int) and not isinstance(number, bool) else ""
    q = _q(vact.scrub(str(question or "")).strip()[:160])
    src_de, src_en = SOURCES.get(src, ("deine Frage", "your question"))
    return (f"voicehook-Call: Deine Frage {q} steht jetzt als{nr} auf dem voicehook-Board "
            f"({src_de}); Oliver kann sie dort beantworten, die Antwort kommt hierher und "
            f"zitiert sie. / Your question {q} is now on the voicehook board as{nr} ({src_en}); "
            "the answer comes here and quotes it.")


def question_info(skey: str, dec_id: object) -> dict | None:
    me = _session(skey) if skey else None
    qs = me.get("qs") if isinstance(me, dict) and isinstance(me.get("qs"), dict) else {}
    got = qs.get(str(dec_id or ""))
    return got if isinstance(got, dict) else None


def answer_texts(skey: str, evs: list[dict]) -> list[str]:
    return [answer_text(e, question_info(skey, e.get("id"))) for e in evs]


def take_notes(skey: str) -> list[str]:
    """The pending mirror notes of this session (each delivered once)."""
    def step(st: dict):
        me = st["sessions"].get(skey)
        if not isinstance(me, dict):
            return []
        notes = [n for n in (me.pop("notes", None) or []) if isinstance(n, str)]
        return notes
    try:
        return _with_state(step)
    except Exception:  # noqa: BLE001
        return []


_ID_RX = re.compile(r'"id"\s*:\s*"(d[0-9a-f]{6,12})"')
_NUM_RX = re.compile(r'"number"\s*:\s*(\d{1,5})')


def decide_question(command: object) -> str:
    """The question text of a `vh-helper decide "<from>" "<question>"` or `voicehook-agent
    decide --question "<q>"` Bash command ('' when not readable)."""
    try:
        for seg in _segments(str(command or "")):
            words = _cmd_start(seg)
            exe = words[0].rsplit("/", 1)[-1] if words else ""
            if exe == "vh-helper" and len(words) > 3 and words[1] == "decide":
                return words[3]
            if exe in ("voicehook-agent", "voicehook_agent") and words[1:2] == ["decide"]:
                for i, w in enumerate(words):
                    if w == "--question" and i + 1 < len(words):
                        return words[i + 1]
                    if w.startswith("--question="):
                        return w.split("=", 1)[1]
    except Exception:  # noqa: BLE001
        return ""
    return ""


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
    instead. 0.21.4: local relay too (request op `state`, the join merges it into the
    helper's operator.status like the server)."""
    from . import relayops
    if state not in STATES:
        return False
    if vmulti._is_local(rel):
        try:
            return bool(relayops.local_request(rel, frm, "state", {"state": state}).get("ok"))
        except Exception:  # noqa: BLE001
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


def run_job(job: dict, rel: dict | None = None) -> bool:
    """One state job, in order (0.21.4): under POST_LOCK_NAME, and only while it is still
    the session's newest state (a detached child of an older hook run that starts late
    never overwrites a newer state: no sticky `arbeitet`). `gone`: clear the board and
    post `leerlauf`. True when something was sent."""
    from . import relayops
    rel = rel if rel is not None else vmulti.load_relay()
    if not isinstance(job, dict) or rel is None:
        return False
    frm = relayops.check_from(str(job.get("from") or ""))
    vact._private_dir(vact.state_root())
    fd = os.open(vact.state_root() / POST_LOCK_NAME,
                 os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        vact._lock(fd)
        if job.get("op") == "gone":
            clear_board(rel, frm)
            return post_state(rel, frm, STATE_IDLE)
        if job.get("op") != "state":
            return False
        skey = str(job.get("skey") or "")
        if skey and job.get("at") is not None:
            me = _session(skey)
            if me is not None and (me.get("state") != job.get("state")
                                   or _float(me.get("state_at")) != _float(job.get("at"))):
                return False     # a newer state exists: this one is stale
        return post_state(rel, frm, str(job.get("state") or ""))
    finally:
        os.close(fd)


def post_main(raw: str) -> int:
    """The detached child of the hook: one state update. Always 0."""
    try:
        run_job(json.loads(raw or "{}"))
    except Exception:  # noqa: BLE001, S110 - detached child, nobody to tell
        pass
    return 0


# --------------------------------------------------------------------------- #
# 0.21.4: the real state, also without hook events (Esc, closed window, crash)
# --------------------------------------------------------------------------- #
POST_LOCK_NAME = "board-post.lock"
WATCH_PID_NAME = "board-watch.pid"
WATCH_POLL_S = 30.0
WATCH_MAX_S = 24 * 3600.0
IDLE_GUESS_S = 180.0       # `arbeitet` without any hook event this long (and no tool running)
INFLIGHT_MAX_S = 900.0     # a tool call longer than this without an event counts as idle
GONE_QUIET_S = 60.0        # a dead session pid only counts after this long without events
LONG_TOOLS = ("Agent", "Task")  # a subagent runs as long as it needs (its own events)
_SHELLS = ("sh", "bash", "dash", "zsh", "ksh", "fish")


def _session(skey: str) -> dict | None:
    try:
        st = json.loads((vact.state_root() / STATE_NAME).read_text(encoding="utf-8"))
        me = st.get("sessions", {}).get(skey)
    except (OSError, ValueError, AttributeError):
        return None
    return me if isinstance(me, dict) else None


def _known_pid(skey: str) -> bool:
    me = _session(skey)
    return bool(me and int(_float(me.get("pid"))) > 1)


def _proc(pid: int) -> tuple[str, int]:
    """(command name, parent pid) of a process; ('', 0) when unknown."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
        comm = raw[raw.index("(") + 1:raw.rindex(")")]
        return comm, int(raw[raw.rindex(")") + 2:].split()[1])
    except (OSError, ValueError, IndexError):
        pass
    import subprocess
    try:
        out = subprocess.run(["ps", "-o", "ppid=,comm=", "-p", str(pid)], capture_output=True,
                             text=True, timeout=2, check=False).stdout.strip()
        ppid, _, comm = out.partition(" ")
        return comm.strip(), int(ppid)
    except (OSError, ValueError, subprocess.SubprocessError):
        return "", 0


def session_pid(env=None, proc=_proc) -> int:
    """The pid of the agent process that runs this hook: $CLAUDE_PID, else the first
    ancestor that is no shell (Claude Code runs hook commands through /bin/sh). 0 = unknown."""
    env = os.environ if env is None else env
    v = str(env.get("CLAUDE_PID") or "").strip()
    if v.isdigit() and int(v) > 1:
        return int(v)
    pid = os.getppid()
    for _ in range(3):
        comm, ppid = proc(pid)
        if not comm:
            return 0
        if comm.rsplit("/", 1)[-1].lstrip("-") not in _SHELLS:
            return pid if pid > 1 else 0
        pid = ppid
        if pid <= 1:
            return 0
    return 0


def _stop_waits(event: dict, skey: str, t: float) -> bool:
    """Stop of a session that waits for the human: an own question is still open, or the
    reply ends with a question (asks.stop_question)."""
    try:
        if open_names(skey, t):
            return True
        from . import asks
        return asks.stop_question(asks.last_message(event)) is not None
    except Exception:  # noqa: BLE001
        return False


def _session_end(skey: str, room: str, t: float, post=None) -> None:
    """SessionEnd: the session is gone: its board leaves the call, state leerlauf."""
    def step(st: dict):
        me = st["sessions"].get(skey)
        if not isinstance(me, dict) or me.get("room") != room:
            return ""
        me.update(gone=t, state=STATE_IDLE, state_at=t, sleeper_until=0)
        me.pop("inflight", None)
        return str(me.get("from") or me.get("name") or "")
    name = _with_state(step)
    if name and not _off(STATE_ENV_OFF):
        (post or _spawn_post)({"op": "gone", "from": name, "skey": skey})


def _inflight(me: dict, t: float) -> bool:
    inf = me.get("inflight") if isinstance(me.get("inflight"), dict) else None
    if not inf:
        return False
    return str(inf.get("tool") or "") in LONG_TOOLS or t - _float(inf.get("at")) < INFLIGHT_MAX_S


def watch_step(now: float | None = None, alive=None) -> tuple[list[dict], int] | None:
    """One watcher round over the helper sessions of the current call: (jobs, live
    sessions) or None (no call). A session whose process is gone -> `gone`; `arbeitet` /
    `problem` without any hook event for IDLE_GUESS_S and no tool running -> `leerlauf`
    (Claude Code sends no Stop after Esc)."""
    t = time.time() if now is None else now
    alive = alive or vact._pid_alive
    rel = vmulti.load_relay(t)
    if rel is None:
        return None
    room = _room(rel)

    def step(st: dict):
        jobs: list[dict] = []
        live = 0
        for skey, me in st["sessions"].items():
            name = str(me.get("from") or me.get("name") or "")
            if me.get("room") != room or me.get("gone") or not name:
                continue
            quiet = t - _float(me.get("seen"))
            pid = int(_float(me.get("pid")))
            if pid > 1 and not alive(pid) and quiet >= GONE_QUIET_S:
                me.update(gone=t, state=STATE_IDLE, state_at=t)
                me.pop("inflight", None)
                jobs.append({"op": "gone", "from": name, "skey": skey})
                continue
            live += 1
            if me.get("state") in (STATE_WORK, STATE_PROBLEM) and quiet >= IDLE_GUESS_S \
                    and not _inflight(me, t):
                me.update(state=STATE_IDLE, state_at=t)
                jobs.append({"op": "state", "from": name, "state": STATE_IDLE, "skey": skey,
                             "at": t})
        return jobs, live
    return _with_state(step)


def _watch_pid_path() -> Path:
    return vact.state_root() / WATCH_PID_NAME


def _watcher_alive() -> bool:
    try:
        pid = int(_watch_pid_path().read_text().strip() or 0)
    except (OSError, ValueError):
        return False
    return pid > 1 and vact._pid_alive(pid)


def _ensure_watcher() -> None:
    """One detached `voicehook-agent-hook board-watch` per machine."""
    if _watcher_alive():
        return
    import subprocess
    try:
        subprocess.Popen(vact.child_argv("board-watch"), stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True, cwd=vact.child_cwd())
    except Exception:  # noqa: BLE001
        return


def watch_main(sleep=time.sleep, clock=time.monotonic) -> int:
    """The machine's state watcher: every WATCH_POLL_S one watch_step, its jobs in order.
    Ends when the call ends or no helper session lives any more. Always 0."""
    try:
        vact._private_dir(vact.state_root())
        if _watcher_alive():
            return 0
        vact._write_private(_watch_pid_path(), str(os.getpid()))
        start = clock()
        while clock() - start < WATCH_MAX_S:
            got = watch_step()
            if got is None:
                return 0
            jobs, live = got
            for job in jobs:
                run_job(job)
            if not live:
                return 0
            sleep(WATCH_POLL_S)
    except Exception:  # noqa: BLE001, S110 - detached child, nobody to tell
        pass
    finally:
        try:
            if _watch_pid_path().read_text().strip() == str(os.getpid()):
                _watch_pid_path().unlink()
        except (OSError, ValueError):
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
