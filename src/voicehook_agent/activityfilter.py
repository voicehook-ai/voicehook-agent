"""Call bookkeeping never goes into the activity log (0.21.5).

Live 20.10.2026: the Pre/PostToolUse hook logged the agent's own call management as activity,
"Read call event", "Wait for next call event", "Restart listening loop"; Delta read it out
("Ich lese das Anrufereignis und überlege mir den nächsten Schritt"). The hook now drops a tool
call (`skip(event)`) when it is only call bookkeeping:

* a Bash command whose every part is a `voicehook-agent` / `vh-helper` call of ADMIN_SUBS
  (next, listen, status, say, board, decide, activity, send-data, ...), a listen/wait wrapper
  (basename vh-* / voicehook-* or with "listen"/"wait" in it, also run as `bash wrapper.sh`),
  or a look at a background task's output file (cat/tail/... of `*.output`
  or a `/tasks/` path), with `sleep` and read-only probes (echo, grep, ...) around them (a
  plain `sleep` alone is not bookkeeping);
* the harness's task output tools (TaskOutput, BashOutput), a voicehook MCP tool, a
  Read of a task output file;
* a description that only talks about the call: every word is a call word (call, event,
  listen, loop, Ereignis, Anruf, ...) or a filler (read, wait, next, restart, ...);
* anything the ignore list matches: VOICEHOOK_ACTIVITY_IGNORE (regexes, separated by newline
  or `;;`) and the file `~/.voicehook/activity-ignore.txt` (one regex per line, `#` comments),
  case-insensitive, searched in the description and the command.

Subagents (0.21.5, live: Delta read a background agent's test steps in first person as the
operator's own work): a tool call inside a subagent carries `agent_id` / `agent_type` in the
hook payload (Claude Code; the same field board.py and listenguard.py use), its transcript
lives under `.../subagents/`. Default: dropped. VOICEHOOK_SUBAGENT_ACTIVITY=bundle logs one
line per subagent instead, "Hintergrund-Agent für <agent_type> arbeitet" (subagent_line).

VOICEHOOK_ACTIVITY_FILTER=0 turns the built-in rules off (the ignore list still applies).
Read-only, stdlib only, never raises (unsure: the line is kept).
"""
from __future__ import annotations

import os
import re
from pathlib import Path

ENV_IGNORE = "VOICEHOOK_ACTIVITY_IGNORE"
ENV_OFF = "VOICEHOOK_ACTIVITY_FILTER"
ENV_SUBAGENT = "VOICEHOOK_SUBAGENT_ACTIVITY"   # drop (default) | bundle
IGNORE_FILE = "activity-ignore.txt"

ADMIN_SUBS = frozenset({"next", "listen", "status", "say", "says", "board", "decide", "activity",
                        "send-data", "relay-next", "show", "ui", "leave"})
CLI_EXES = frozenset({"voicehook-agent", "voicehook_agent", "vh-helper", "voicehook-agent-hook"})
WAIT_TOOLS = frozenset({"TaskOutput", "BashOutput"})  # reading a background task's output
_SHELLS = frozenset({"bash", "sh", "zsh", "python", "python3", "uv", "node"})
_READERS = frozenset({"cat", "tail", "head", "less", "grep", "wc", "jq", "sed", "awk", "stat"})
_WRAPPER_RX = re.compile(r"(?i)^(vh|voicehook)[-_]|listen|wait")
_TASK_FILE_RX = re.compile(r"(?i)(\.output$|/tasks/)")
_CALL_WORDS = ("call", "event", "listen", "loop", "ereignis", "anruf", "voicehook", "waiter",
               "spool", "presence", "präsenz")
_FILLER = frozenset({
    "a", "an", "the", "for", "to", "of", "on", "in", "at", "and", "or", "from", "my", "our",
    "its", "new", "next", "again", "latest", "last", "first", "one", "read", "reads", "reading",
    "get", "gets", "fetch", "check", "checks", "checking", "poll", "polls", "polling", "wait",
    "waits", "waiting", "watch", "watching", "await", "restart", "restarts", "start", "starts",
    "starting", "stop", "resume", "run", "runs", "running", "keep", "show", "print", "handle",
    "handling", "see", "look", "peek", "open", "close", "status", "state", "current", "pending",
    "tail", "drain", "der", "die", "das", "den", "dem", "des", "ein", "eine", "einen", "einem",
    "nächste", "nächsten", "nächstes", "neu", "neue", "neuen", "erneut", "wieder", "auf", "für",
    "vom", "von", "im", "am", "zum", "zur", "und", "oder", "mein", "meine", "lesen", "lese",
    "liest", "abholen", "hole", "holen", "warten", "warte", "wartet", "prüfen", "prüfe",
    "starten", "starte", "neustarten", "weiter", "aktuell", "aktuelle", "offen", "offene",
})


def _off() -> bool:
    return (os.environ.get(ENV_OFF) or "").strip().lower() in ("0", "false", "no", "off")


def ignore_patterns(env=None, state_root: Path | None = None) -> list[re.Pattern]:
    """The configured ignore regexes (env + file); a broken regex is skipped."""
    env = os.environ if env is None else env
    raw = [p for chunk in re.split(r"\n|;;", env.get(ENV_IGNORE) or "") if (p := chunk.strip())]
    if state_root is None:
        from . import activity as vact
        state_root = vact.state_root()
    try:
        for line in (Path(state_root) / IGNORE_FILE).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                raw.append(line)
    except OSError:
        pass
    out = []
    for p in raw:
        try:
            out.append(re.compile(p, re.IGNORECASE))
        except re.error:
            continue
    return out


def call_only_description(desc: object) -> bool:
    """Every word is a call word or a filler, and at least one is a call word."""
    words = re.findall(r"[a-zäöüß]+", str(desc or "").lower())
    if not words:
        return False
    call = False
    for w in words:
        if w.startswith(_CALL_WORDS):  # calls, events, listening, anrufereignis ...
            call = True
        elif w not in _FILLER:
            return False
    return call


def _task_read(words: list[str]) -> bool:
    exe = words[0].rsplit("/", 1)[-1]
    return exe in _READERS and any(_TASK_FILE_RX.search(w) for w in words[1:]
                                   if not w.startswith("-"))


def _segment(words: list[str]) -> str:
    """admin | probe | work for one simple command."""
    from . import board as vboard
    words = vboard._unwrap(words)
    if not words:
        return "probe"
    exe = words[0].rsplit("/", 1)[-1]
    sub = words[1] if len(words) > 1 else ""
    if exe in CLI_EXES:
        return "admin" if exe == "voicehook-agent-hook" or sub in ADMIN_SUBS else "work"
    if exe in ("sleep", "wait"):
        return "probe"  # neutral: a plain `sleep 300` with its own description stays work
    if _task_read(words):
        return "admin"
    if exe in _SHELLS and sub and not sub.startswith("-") \
            and _WRAPPER_RX.search(sub.rsplit("/", 1)[-1]):
        return "admin"  # bash ~/bin/vh-wait.sh
    if _WRAPPER_RX.search(exe):
        return "admin"  # vh-wait, voicehook-listen, call-waiter ...
    if exe in vboard._PROBES:
        return "probe"
    return "work"


def admin_command(command: object) -> bool:
    """Is this Bash command only call bookkeeping (see the module doc)?"""
    from . import board as vboard
    try:
        kinds = [_segment(s) for s in vboard._wait_segments(str(command or ""))]
    except Exception:  # noqa: BLE001
        return False
    return bool(kinds) and "work" not in kinds and "admin" in kinds


def subagent(event: dict) -> str:
    """The subagent id of a hook event ('' = the session's main agent)."""
    aid = str(event.get("agent_id") or "").strip()
    if aid:
        return aid
    tp = str(event.get("transcript_path") or "")
    if "/subagents/" in tp.replace("\\", "/"):
        return tp.rsplit("/", 1)[-1].rsplit(".", 1)[0] or "subagent"
    return ""


def subagent_mode(env=None) -> str:
    env = os.environ if env is None else env
    return "bundle" if (env.get(ENV_SUBAGENT) or "").strip().lower() == "bundle" else "drop"


def subagent_line(event: dict) -> str:
    """The one bundled line of a subagent (VOICEHOOK_SUBAGENT_ACTIVITY=bundle)."""
    from . import activity as vact
    kind = vact.scrub(str(event.get("agent_type") or "")).strip()[:40] or "eine Teilaufgabe"
    return f"Hintergrund-Agent für {kind} arbeitet"


def skip(event: dict) -> bool:
    """True: do not log this tool call as activity. Never raises (unsure: False)."""
    try:
        tool = str(event.get("tool_name") or "")
        inp = event.get("tool_input") if isinstance(event.get("tool_input"), dict) else {}
        desc = inp.get("description") if isinstance(inp.get("description"), str) else ""
        cmd = inp.get("command") if isinstance(inp.get("command"), str) else ""
        pats = ignore_patterns()
        if pats and any(p.search(desc) or p.search(cmd) for p in pats):
            return True
        if _off():
            return False
        if tool in WAIT_TOOLS:
            return True
        if tool.startswith("mcp__") and "voicehook" in tool.lower():
            return True
        if tool == "Read" and _TASK_FILE_RX.search(str(inp.get("file_path") or "")):
            return True
        if tool == "Bash" and cmd and admin_command(cmd):
            return True
        return call_only_description(desc)
    except Exception:  # noqa: BLE001
        return False
