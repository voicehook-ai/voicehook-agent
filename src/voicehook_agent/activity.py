"""Activity feed (0.10.0): what the background coding agent is doing right now.

Claude Code hooks (`voicehook-agent-hook pre-tool-use` / `post-tool-use`, also
reachable as `voicehook-agent hook ...`) append ONE short line per tool call to
`<session_dir>/activity.log` of the running join:

    17:12:03 Bash: Tests laufen lassen
    17:12:09 Edit: relay.py
    17:12:15 note: Running migrations

0.13.0: the PreToolUse hook writes the line when the tool STARTS (a long Bash or
Agent run shows up while it runs); PostToolUse writes only for a tool_use_id that
Pre did not log (an old Post-only install keeps working). Tools without a
`description` (Read/Edit/Write/...) log the file's basename, never a path; Grep/Glob
log the tool name only. Agents without hooks add a line with
`voicehook-agent activity "<text>"` (tool name `note`), and `next` reminds them
(activity_due, see ActivityDue) when the log stays silent while they work.

The running `join` publishes the newest lines as `operator.activity` (see
ActivityPublisher + cli._activity_loop), so Delta can answer "was machst du
gerade?" without asking the agent.

Privacy: a line holds only the local time, the sanitized tool name and the
tool's own `description` (Bash/Agent/Task carry one) or a file's basename, passed
through a secret scrubber. Never command text, arguments, full paths, search
patterns, file contents or tool output. With zero or several live joins the hook writes nothing, so one Claude
session never leaks into another call.

Automatic flow (0.13.0):
* Pointer files `~/.voicehook/joins/<pid>.json` (written by `join`) let the hook find
  a join that runs with its own VOICEHOOK_AGENT_HOME (Quickstart B wrapper).
* Bridge: with `~/.voicehook/bridge-session.json` ({base, session} of a curl bridge
  join, Quickstart A) the hook also POSTs the text to `<base>/api/bridge/activity`
  (Bearer session; 2 s timeout, at most one POST per 5 s, the latest line wins via a
  detached flusher, never blocking). 401/404/410 = session gone: the file is removed.

This module stays stdlib-only (no livekit/httpx) so the hook starts fast.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import unicodedata
from pathlib import Path

from . import session as vsession

ACTIVITY_NAME = "activity.log"
IDS_NAME = "activity.ids"   # 0.13.0: tool_use_ids logged by PreToolUse (dedupe, never published)
IDS_KEEP = 200
TRIM_AT = 200          # lines; above this the file is rewritten ...
TRIM_KEEP = 50         # ... keeping the newest 50
DESC_MAX = 120
TOOL_MAX = 40
NAME_MAX = 60          # basename of a file tool's path
NOTE_TOOL = "note"     # tool name of a manual `voicehook-agent activity` line
# Tools without `description` whose file basename is logged (never the path).
FILE_TOOLS = {"Read": "file_path", "Edit": "file_path", "Write": "file_path",
              "MultiEdit": "file_path", "NotebookEdit": "notebook_path",
              "NotebookRead": "notebook_path"}
PUBLISH_LINES = 15     # newest lines per operator.activity packet
PUBLISH_WINDOW = 5.0   # s, at most one packet per window (last one wins)
POLL_INTERVAL = 1.0    # s, how often the join looks at activity.log
TOPIC = "operator.activity"

HOOK_COMMAND = "voicehook-agent-hook post-tool-use"
# 0.13.0. `|| true`: a PreToolUse hook exiting 2 BLOCKS the tool in Claude Code, and an
# older voicehook-agent-hook on PATH (<0.13) answers an unknown subcommand with exit 2.
HOOK_COMMAND_PRE = "voicehook-agent-hook pre-tool-use || true"
HOOK_COMMANDS = {"PreToolUse": HOOK_COMMAND_PRE, "PostToolUse": HOOK_COMMAND}
# Commands that count as "our hook is already installed" (idempotent install).
_OUR_COMMANDS = (HOOK_COMMAND, "voicehook-agent hook post-tool-use")
_OUR_COMMANDS_BY_EVENT = {"PreToolUse": (HOOK_COMMAND_PRE, "voicehook-agent-hook pre-tool-use",
                                         "voicehook-agent hook pre-tool-use",
                                         "voicehook-agent hook pre-tool-use || true"),
                          "PostToolUse": _OUR_COMMANDS}
HOOK_TIMEOUT = 5

REDACTED = "[redacted]"

# Token-start boundary: not preceded by a word char, so "rest"/"pre_x" stay.
_B = r"(?<![A-Za-z0-9_])"
_TAIL = r"[A-Za-z0-9_\-]{8,}\S*"
_SECRET_PATTERNS = [
    re.compile(r"(?i)\bBearer\s+\S+"),
    re.compile(r"(?i)" + _B + r"(api[_-]?key|access[_-]?key|key|token|password|passwd|pwd|secret|auth)"
               r"\s*=\s*(\"[^\"]*\"|'[^']*'|\S+)"),
    re.compile(_B + r"github_pat_" + _TAIL),
    re.compile(_B + r"(sk|pk)[_-]" + _TAIL),
    re.compile(_B + r"(rk|re|whsec|vhw)_" + _TAIL),
    re.compile(_B + r"gh[pousr]_" + _TAIL),
    re.compile(_B + r"xox[abpr]-" + _TAIL),
    re.compile(_B + r"AKIA[0-9A-Z]{16}"),
    re.compile(_B + r"AIza[0-9A-Za-z_\-]{20,}"),
    re.compile(_B + r"eyJ[A-Za-z0-9_\-]{8,}(?:\.[A-Za-z0-9_\-]+)*"),
    re.compile(r"[A-Za-z0-9+/=_\-]{32,}"),
]
_KV = _SECRET_PATTERNS[1]
_LINE_RX = re.compile(r"^(\d\d:\d\d:\d\d) ([A-Za-z0-9_.:\-]{1,40})(?:: (.*))?$")


def _clean(text: str) -> str:
    """Control/format chars -> space, whitespace collapsed."""
    out = "".join(" " if unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp") else c
                  for c in text)
    return " ".join(out.split())


def scrub(text: str) -> str:
    """Remove secrets from free text, strip control chars, collapse whitespace."""
    s = _clean(str(text))
    for rx in _SECRET_PATTERNS:
        if rx is _KV:
            s = rx.sub(lambda m: f"{m.group(1)}={REDACTED}", s)
        else:
            s = rx.sub(REDACTED, s)
    return s


def sanitize_tool(name: object) -> str:
    out = re.sub(r"[^A-Za-z0-9_.:\-]", "", str(name or ""))[:TOOL_MAX]
    return out or "Tool"


def basename(path: object) -> str:
    """Last path component (POSIX or Windows separators), scrubbed and capped; else ''."""
    if not isinstance(path, str):
        return ""
    name = re.split(r"[/\\]", path.strip())[-1]
    return scrub(name)[:NAME_MAX].strip()


def describe(tool_input: object, tool_name: object = None) -> str:
    """The tool's own description (Bash/Agent/Task), scrubbed and capped; for a file
    tool without one (Read/Edit/Write/...) the file's basename; else ''."""
    if not isinstance(tool_input, dict):
        return ""
    desc = tool_input.get("description")
    if isinstance(desc, str) and desc.strip():
        return scrub(desc)[:DESC_MAX].strip()
    key = FILE_TOOLS.get(str(tool_name or ""))
    return basename(tool_input.get(key)) if key else ""


def _ts(now: float | None) -> str:
    return time.strftime("%H:%M:%S", time.localtime(time.time() if now is None else now))


def format_line(event: dict, now: float | None = None) -> str:
    """`HH:MM:SS Tool: description` (local time) or `HH:MM:SS Tool`."""
    tool = sanitize_tool(event.get("tool_name"))
    desc = describe(event.get("tool_input"), event.get("tool_name"))
    return f"{_ts(now)} {tool}: {desc}" if desc else f"{_ts(now)} {tool}"


def bridge_text(event: dict) -> str:
    """0.13.0: text for the bridge: the tool's own description 1:1; a file tool
    `Read relay.py`; else the tool name."""
    tool = sanitize_tool(event.get("tool_name"))
    inp = event.get("tool_input")
    if isinstance(inp, dict) and isinstance(inp.get("description"), str) \
            and inp["description"].strip():
        return describe(inp, tool)
    desc = describe(inp, event.get("tool_name"))
    return f"{tool} {desc}" if desc else tool


def note_line(text: str, now: float | None = None) -> str:
    """0.13.0: manual line `HH:MM:SS note: <text>` (`voicehook-agent activity`),
    scrubbed and capped like a hook description. ValueError when empty."""
    desc = scrub(str(text or ""))[:DESC_MAX].strip()
    if not desc:
        raise ValueError("empty activity text")
    return f"{_ts(now)} {NOTE_TOOL}: {desc}"


def scrub_line(line: str) -> str:
    """Defense in depth for a stored line: keep the time + tool prefix, scrub
    the description; a line in any other shape is scrubbed as a whole."""
    line = _clean(line)
    m = _LINE_RX.match(line)
    if not m:
        return scrub(line)[:DESC_MAX + TOOL_MAX + 12]
    ts, tool, desc = m.group(1), m.group(2), m.group(3)
    desc = scrub(desc or "")[:DESC_MAX].strip()
    return f"{ts} {tool}: {desc}" if desc else f"{ts} {tool}"


# --------------------------------------------------------------------------- #
# target session dir
# --------------------------------------------------------------------------- #
def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def live_join_dirs() -> list[Path]:
    """Session dirs whose session.json names a live pid."""
    root = vsession.sessions_root()
    out: list[Path] = []
    try:
        infos = sorted(root.glob(f"*/*/{vsession.INFO_NAME}"))
    except OSError:
        return []
    for info in infos:
        try:
            pid = int(json.loads(info.read_text(encoding="utf-8")).get("pid"))
        except (OSError, ValueError, TypeError, AttributeError):
            continue
        if pid > 0 and _pid_alive(pid):
            out.append(info.parent)
    return out


# --------------------------------------------------------------------------- #
# 0.13.0: fixed state dir (independent of VOICEHOOK_AGENT_HOME): join pointers +
# bridge session
# --------------------------------------------------------------------------- #
STATE_ENV = "VOICEHOOK_STATE_DIR"
BRIDGE_SESSION_NAME = "bridge-session.json"
BRIDGE_STATE_NAME = "bridge-activity.json"
BRIDGE_IDS_NAME = "bridge-activity.ids"
BRIDGE_TIMEOUT_S = 2.0
BRIDGE_WINDOW_S = 5.0


def state_root() -> Path:
    env = (os.environ.get(STATE_ENV) or "").strip()
    return Path(env) if env else Path.home() / ".voicehook"


def _private_dir(d: Path) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    return d


def _write_private(path: Path, text: str) -> None:
    """Write `text` atomically, mode 0600, never following a symlink."""
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)
    os.replace(tmp, path)


def pointer_path(pid: int | None = None) -> Path:
    return state_root() / "joins" / f"{os.getpid() if pid is None else pid}.json"


def write_pointer(dir_: Path) -> None:
    """join: `~/.voicehook/joins/<pid>.json` = {dir, pid}, so the hook finds this join
    whatever VOICEHOOK_AGENT_HOME the hook sees. Best effort."""
    try:
        _private_dir(state_root())
        _private_dir(state_root() / "joins")
        _write_private(pointer_path(), json.dumps({"dir": str(Path(dir_).resolve()),
                                                   "pid": os.getpid()}))
    except OSError:
        pass


def remove_pointer(dir_: Path | None = None) -> None:
    try:
        pointer_path().unlink()
    except OSError:
        pass


def pointer_dirs() -> list[Path]:
    """Session dirs named by pointer files whose pid is alive and whose session.json
    names the same pid."""
    out: list[Path] = []
    try:
        files = sorted((state_root() / "joins").glob("*.json"))
    except OSError:
        return []
    for f in files:
        try:
            info = json.loads(f.read_text(encoding="utf-8"))
            pid, d = int(info.get("pid")), Path(str(info.get("dir")))
            own = int(json.loads((d / vsession.INFO_NAME).read_text(encoding="utf-8")).get("pid"))
        except (OSError, ValueError, TypeError, AttributeError):
            continue
        if pid > 0 and pid == own and _pid_alive(pid):
            out.append(d)
    return out


def target_dir() -> Path | None:
    """VOICEHOOK_SESSION=<slug>/<identity> wins; else the ONE live join (own home or a
    pointer file, 0.13.0); else None (zero or several: nothing leaks into another call)."""
    env = (os.environ.get("VOICEHOOK_SESSION") or "").strip()
    if env:
        slug, _, ident = env.partition("/")
        if not slug or not ident:
            return None
        d = vsession.session_dir(slug, ident)
        return d if d.is_dir() else None
    live: dict[str, Path] = {}
    for d in live_join_dirs() + pointer_dirs():
        try:
            live.setdefault(str(d.resolve()), d)
        except OSError:
            continue
    return next(iter(live.values())) if len(live) == 1 else None


# ----- bridge session (Quickstart A: curl bridge join) ------------------------------
def save_bridge_session(answer: dict, base: str) -> Path:
    """Store ONLY {base, session} of a /api/bridge/join answer (0600). ValueError when
    the answer has no session or base is not http(s)."""
    session = answer.get("session") if isinstance(answer, dict) else None
    base = str(base or "").strip().rstrip("/")
    if not isinstance(session, str) or not session.strip():
        raise ValueError("join answer has no session")
    if not re.match(r"^https?://[^\s/]+", base):
        raise ValueError(f"base must be an http(s) URL, got {base!r}")
    _private_dir(state_root())
    path = state_root() / BRIDGE_SESSION_NAME
    _write_private(path, json.dumps({"base": base, "session": session.strip()}))
    return path


def clear_bridge_session() -> None:
    for name in (BRIDGE_SESSION_NAME, BRIDGE_STATE_NAME, BRIDGE_IDS_NAME):
        try:
            (state_root() / name).unlink()
        except OSError:
            pass


def load_bridge_session() -> dict | None:
    try:
        d = json.loads((state_root() / BRIDGE_SESSION_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict) or not d.get("base") or not d.get("session"):
        return None
    return {"base": str(d["base"]), "session": str(d["session"])}


def _bridge_post(base: str, session: str, text: str,
                 timeout: float = BRIDGE_TIMEOUT_S) -> int | None:
    """POST <base>/api/bridge/activity {"text"}; HTTP status, None on network error."""
    import urllib.error
    import urllib.request
    req = urllib.request.Request(
        f"{base}/api/bridge/activity", method="POST",
        data=json.dumps({"text": text}, ensure_ascii=False).encode("utf-8"),
        headers={"authorization": f"Bearer {session}", "content-type": "application/json",
                 "user-agent": "voicehook-agent-hook"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # base comes from our own 0600 file
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:  # noqa: BLE001 - never block the hook
        return None


def _spawn_flusher(delay: float) -> None:
    """Detached `voicehook-agent-hook bridge-flush DELAY`: sends the pending line when
    the rate window ends. Best effort."""
    import subprocess
    try:
        subprocess.Popen([sys.executable, "-m", "voicehook_agent.activity", "bridge-flush",
                          f"{max(0.0, delay):.3f}"],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except Exception:  # noqa: BLE001
        return


def _bridge_state(fn) -> object:
    """Run fn(state) -> result under an exclusive lock on the bridge state file."""
    _private_dir(state_root())
    path = state_root() / BRIDGE_STATE_NAME
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        _lock(fd)
        try:
            st = json.loads(os.read(fd, 1 << 16).decode("utf-8") or "{}")
        except ValueError:
            st = {}
        if not isinstance(st, dict):
            st = {}
        res = fn(st)
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, json.dumps(st).encode("utf-8"))
        return res
    finally:
        os.close(fd)


def _deliver(sess: dict, text: str) -> None:
    status = _bridge_post(sess["base"], sess["session"], text)
    if status in (401, 404, 410):  # bridge session unknown/expired or call ended
        clear_bridge_session()


def bridge_send(text: str, now: float | None = None) -> bool:
    """Hook -> bridge: POST at once when the last POST is >= 5 s ago, else keep it as
    pending (latest wins) and make sure one flusher is scheduled. True if sent or
    queued. Never raises."""
    try:
        sess = load_bridge_session()
        if sess is None or not text:
            return False
        t = time.time() if now is None else now

        def step(st: dict):
            last = float(st.get("last_post") or 0.0)
            if t - last >= BRIDGE_WINDOW_S:
                st.update(last_post=t, pending=None)
                return ("post", 0.0)
            st["pending"] = text
            due = last + BRIDGE_WINDOW_S
            if float(st.get("flush_at") or 0.0) < t:   # no flusher scheduled yet
                st["flush_at"] = due
                return ("spawn", due - t)
            return ("queued", 0.0)
        action, delay = _bridge_state(step)
        if action == "post":
            _deliver(sess, text)
        elif action == "spawn":
            _spawn_flusher(delay)
        return True
    except Exception:  # noqa: BLE001
        return False


def bridge_flush(now: float | None = None, sleep=time.sleep, delay: float = 0.0) -> None:
    """The flusher: after `delay` s send the pending line (if any). Never raises."""
    try:
        if delay > 0:
            sleep(delay)
        sess = load_bridge_session()
        t = time.time() if now is None else now

        def step(st: dict):
            pending = st.get("pending")
            st.update(pending=None, flush_at=0.0)
            if pending:
                st["last_post"] = t
            return pending
        pending = _bridge_state(step)
        if pending and sess is not None:
            _deliver(sess, str(pending))
    except Exception:  # noqa: BLE001
        return


def append_line(dir_: Path, line: str) -> None:
    """Append one line (file mode 0600, no symlink follow); trim when long."""
    path = dir_ / ACTIVITY_NAME
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        try:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX)
        except (ImportError, OSError):
            pass
        try:
            os.fchmod(fd, 0o600)
        except (AttributeError, OSError):
            pass
        os.write(fd, (line + "\n").encode("utf-8"))
        _trim(path)
    finally:
        os.close(fd)


def _trim(path: Path) -> None:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return
    if len(lines) <= TRIM_AT:
        return
    tmp = path.with_name(ACTIVITY_NAME + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.write(fd, ("\n".join(lines[-TRIM_KEEP:]) + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    os.replace(tmp, path)


def _tool_use_id(event: dict) -> str:
    tid = event.get("tool_use_id")
    return re.sub(r"[^A-Za-z0-9_\-]", "", tid)[:100] if isinstance(tid, str) else ""


def _remember_id(path: Path, tid: str) -> None:
    """Pre: note tid as logged (file 0600, newest IDS_KEEP kept)."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        _lock(fd)
        ids = os.read(fd, 1 << 20).decode("utf-8", "replace").split()
        ids = (ids + [tid])[-IDS_KEEP:]
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, ("\n".join(ids) + "\n").encode("utf-8"))
    finally:
        os.close(fd)


def _take_id(path: Path, tid: str) -> bool:
    """Post: True (and forget it) if Pre already logged tid."""
    try:
        fd = os.open(path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return False
    try:
        _lock(fd)
        ids = os.read(fd, 1 << 20).decode("utf-8", "replace").split()
        if tid not in ids:
            return False
        ids.remove(tid)
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, ("\n".join(ids) + "\n").encode("utf-8") if ids else b"")
        return True
    finally:
        os.close(fd)


def _lock(fd: int) -> None:
    try:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX)
    except (ImportError, OSError):
        pass


def _handle(raw: str, phase: str, now: float | None) -> bool:
    try:
        event = json.loads(raw) if raw and raw.strip() else {}
        if not isinstance(event, dict):
            return False
        tid = _tool_use_id(event)
        done = False
        dir_ = target_dir()
        if dir_ is not None and not (phase == "post" and tid and _take_id(dir_ / IDS_NAME, tid)):
            append_line(dir_, format_line(event, now))
            if phase == "pre" and tid:
                _remember_id(dir_ / IDS_NAME, tid)
            done = True
        if load_bridge_session() is not None:   # 0.13.0: curl bridge join (Quickstart A)
            ids = state_root() / BRIDGE_IDS_NAME
            if not (phase == "post" and tid and _take_id(ids, tid)):
                done = bridge_send(bridge_text(event), now) or done
                if phase == "pre" and tid:
                    _remember_id(ids, tid)
        return done
    except Exception:  # noqa: BLE001 - a hook must never disturb Claude Code
        return False


def pre_tool_use(raw: str, now: float | None = None) -> bool:
    """0.13.0: handle one PreToolUse event (JSON text): the line is in the log while
    the tool runs. True if a line was written. Never raises."""
    return _handle(raw, "pre", now)


def post_tool_use(raw: str, now: float | None = None) -> bool:
    """Handle one PostToolUse event (JSON text). Skips a tool_use_id that the
    PreToolUse hook already logged. True if a line was written. Never raises."""
    return _handle(raw, "post", now)


def read_tail(path: Path, n: int = PUBLISH_LINES) -> list[str]:
    """Newest `n` non-empty lines (oldest first), scrubbed again; [] if missing."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 64 * 1024))
            data = f.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    lines = [ln for ln in data.splitlines() if ln.strip()]
    return [scrub_line(ln) for ln in lines[-n:]]


def clear(dir_: Path) -> None:
    for name in (ACTIVITY_NAME, IDS_NAME):
        try:
            (dir_ / name).unlink()
        except OSError:
            pass


class ActivityPublisher:
    """Rate limit for operator.activity: publish only on change, at most once
    per `window` seconds; a change inside the window goes out when the window
    ends (last one wins)."""

    def __init__(self, window: float = PUBLISH_WINDOW) -> None:
        self.window = window
        self.last: list[str] = []
        self.last_ts: float | None = None

    def due(self, lines: list[str], now: float) -> bool:
        if lines == self.last:
            return False
        return self.last_ts is None or now - self.last_ts >= self.window

    def sent(self, lines: list[str], now: float) -> None:
        self.last = list(lines)
        self.last_ts = now


# --------------------------------------------------------------------------- #
# 0.13.0 activity_due: remind agents without the hook to fill the log
# --------------------------------------------------------------------------- #
ACTIVITY_DUE_S = 60.0          # no new line for this long while working -> activity_due
ACTIVITY_DUE_ENV = "VOICEHOOK_ACTIVITY_DUE"
HOOK_QUIET_S = 600.0           # a hook line within this window = hook active, stay silent
ACTIVITY_CMD = 'voicehook-agent activity "<3-8 Wörter>"'
ACTIVITY_HINT = ("Aktivitätslog still, während du arbeitest: Delta liest nur Zeilen jünger als "
                 "60 s vor und weiss sonst nicht, was gerade passiert. Deine eigene kurze "
                 "Statuszeile 1:1 senden, nicht umformulieren: " + ACTIVITY_CMD + " (was gerade "
                 "passiert, aktuelle Phase; keine Pfade, Secrets oder personenbezogenen Daten). "
                 "Claude Code: `voicehook-agent hook install` erledigt das automatisch.")


def activity_due_seconds(value: float | str | None = None) -> float:
    """N for activity_due: explicit value (--activity-due), else
    $VOICEHOOK_ACTIVITY_DUE, else ACTIVITY_DUE_S. 0 or less switches it off."""
    for v in (value, os.environ.get(ACTIVITY_DUE_ENV)):
        if v is None or v == "":
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return ACTIVITY_DUE_S


def _is_note(line: str) -> bool:
    m = _LINE_RX.match(line)
    return bool(m) and m.group(2) == NOTE_TOOL


class ActivityDue:
    """Is the activity log silent while the agent works?

    `observe(lines, now)` with the newest lines of activity.log (the join's
    activity loop and `next` call it); `check(now, working)` returns
    {"activity_due", "activity_age_s", "activity_hint"} or {}. Due when work is in
    progress and no new line came for `due_s` s (counted from the join without any
    line). Silent while hook lines (any tool but `note`) arrived within HOOK_QUIET_S:
    with the hook the log fills itself. At most one hint per `due_s` s."""

    def __init__(self, due_s: float = ACTIVITY_DUE_S, now: float | None = None) -> None:
        self.due_s = due_s
        self.started = time.monotonic() if now is None else now
        self.line_at: float | None = None
        self.hook_at: float | None = None
        self.hinted_at: float | None = None
        self._last: list[str] = []

    def observe(self, lines: list[str], now: float) -> None:
        if lines == self._last:
            return
        old = set(self._last)
        new = [ln for ln in lines if ln not in old] or lines[-1:]
        self._last = list(lines)
        if not new:
            return
        self.line_at = now
        if any(not _is_note(ln) for ln in new):
            self.hook_at = now

    def age(self, now: float) -> float:
        """Seconds since the newest line (since the join when none came)."""
        return now - (self.line_at if self.line_at is not None else self.started)

    def hook_active(self, now: float) -> bool:
        return self.hook_at is not None and now - self.hook_at < HOOK_QUIET_S

    def check(self, now: float, working: bool) -> dict:
        if self.due_s <= 0 or not working:
            return {}
        if self.hook_at is not None and now - self.hook_at < HOOK_QUIET_S:
            return {}
        age = now - (self.line_at if self.line_at is not None else self.started)
        if age <= self.due_s:
            return {}
        if self.hinted_at is not None and now - self.hinted_at < self.due_s:
            return {}
        self.hinted_at = now
        return {"activity_due": True, "activity_age_s": round(age, 1),
                "activity_hint": ACTIVITY_HINT}


# --------------------------------------------------------------------------- #
# settings.json snippet + installer
# --------------------------------------------------------------------------- #
def hook_entry(event: str = "PostToolUse") -> dict:
    return {"matcher": "*",
            "hooks": [{"type": "command", "command": HOOK_COMMANDS[event], "timeout": HOOK_TIMEOUT}]}


def snippet() -> dict:
    return {"hooks": {ev: [hook_entry(ev)] for ev in HOOK_COMMANDS}}


def default_settings_path() -> Path:
    return Path.home() / ".claude" / "settings.json"


def _installed(entries: list, event: str = "PostToolUse") -> bool:
    ours = _OUR_COMMANDS_BY_EVENT[event]
    for e in entries:
        if not isinstance(e, dict):
            continue
        for h in e.get("hooks") or []:
            if isinstance(h, dict) and str(h.get("command", "")).strip() in ours:
                return True
    return False


def install(path: Path) -> tuple[int, str]:
    """Merge the PreToolUse + PostToolUse hooks into settings.json. Idempotent (an
    older Post-only install gets the Pre hook added); refuses to touch a file that
    is not a JSON object."""
    data: dict = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8") or "{}")
        except (OSError, ValueError) as e:
            return 1, f"[error] {path} is not valid JSON ({e}); not changed. Fix it or add the snippet by hand (voicehook-agent hook print)."
        if not isinstance(data, dict):
            return 1, f"[error] {path} is not a JSON object; not changed."
    hooks = data.get("hooks")
    if hooks is None:
        hooks = data["hooks"] = {}
    if not isinstance(hooks, dict):
        return 1, f"[error] {path}: 'hooks' is not an object; not changed."
    for ev in HOOK_COMMANDS:
        if hooks.get(ev) is not None and not isinstance(hooks.get(ev), list):
            return 1, f"[error] {path}: 'hooks.{ev}' is not a list; not changed."
    added = []
    for ev in HOOK_COMMANDS:
        entries = hooks.setdefault(ev, [])
        if not _installed(entries, ev):
            entries.append(hook_entry(ev))
            added.append(ev)
    if not added:
        return 0, f"already installed in {path}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return 0, f"installed {' + '.join(added)} hook{'s' if len(added) > 1 else ''} in {path}"


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    cmd = args[0] if args else ""
    if cmd in ("pre-tool-use", "post-tool-use"):
        try:
            raw = sys.stdin.read()
        except Exception:  # noqa: BLE001
            raw = ""
        (pre_tool_use if cmd == "pre-tool-use" else post_tool_use)(raw)
        return 0  # always 0, nothing on stdout (PreToolUse: no output = allow)
    if cmd == "bridge-flush":
        try:
            delay = float(args[1]) if len(args) > 1 else 0.0
        except ValueError:
            delay = 0.0
        bridge_flush(delay=delay)
        return 0
    if cmd == "print":
        print(json.dumps(snippet(), indent=2))
        return 0
    if cmd == "install":
        path = default_settings_path()
        rest = args[1:]
        if rest[:1] == ["--settings"] and len(rest) >= 2:
            path = Path(rest[1]).expanduser()
        elif rest and rest[0].startswith("--settings="):
            path = Path(rest[0].split("=", 1)[1]).expanduser()
        elif rest:
            print(f"[error] unknown argument {rest[0]!r}", file=sys.stderr)
            return 2
        rc, msg = install(path)
        print(msg, file=sys.stderr if rc else sys.stdout)
        return rc
    print("usage: voicehook-agent hook {pre-tool-use|post-tool-use|print|install [--settings PATH]}",
          file=sys.stderr)
    return 2


def console_main() -> None:  # `voicehook-agent-hook`
    sys.exit(main())


if __name__ == "__main__":
    console_main()
