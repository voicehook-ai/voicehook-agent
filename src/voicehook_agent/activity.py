"""Activity feed (0.10.0): what the background coding agent is doing right now.

A Claude Code PostToolUse hook (`voicehook-agent-hook post-tool-use`, also
reachable as `voicehook-agent hook post-tool-use`) appends ONE short line per
tool call to `<session_dir>/activity.log` of the running join:

    17:12:03 Bash: Tests laufen lassen
    17:12:09 Edit

The running `join` publishes the newest lines as `operator.activity` (see
ActivityPublisher + cli._activity_loop), so Delta can answer "was machst du
gerade?" without asking the agent.

Privacy: a line holds only the local time, the sanitized tool name and the
tool's own `description` (Bash/Agent/Task carry one), passed through a secret
scrubber. Never command text, arguments, file paths, file contents or tool
output. With zero or several live joins the hook writes nothing, so one Claude
session never leaks into another call.

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
TRIM_AT = 200          # lines; above this the file is rewritten ...
TRIM_KEEP = 50         # ... keeping the newest 50
DESC_MAX = 120
TOOL_MAX = 40
PUBLISH_LINES = 15     # newest lines per operator.activity packet
PUBLISH_WINDOW = 5.0   # s, at most one packet per window (last one wins)
POLL_INTERVAL = 1.0    # s, how often the join looks at activity.log
TOPIC = "operator.activity"

HOOK_COMMAND = "voicehook-agent-hook post-tool-use"
# Commands that count as "our hook is already installed" (idempotent install).
_OUR_COMMANDS = (HOOK_COMMAND, "voicehook-agent hook post-tool-use")
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


def describe(tool_input: object) -> str:
    """The tool's own description (Bash/Agent/Task), scrubbed and capped; else ''."""
    if not isinstance(tool_input, dict):
        return ""
    desc = tool_input.get("description")
    if not isinstance(desc, str):
        return ""
    return scrub(desc)[:DESC_MAX].strip()


def format_line(event: dict, now: float | None = None) -> str:
    """`HH:MM:SS Tool: description` (local time) or `HH:MM:SS Tool`."""
    ts = time.strftime("%H:%M:%S", time.localtime(time.time() if now is None else now))
    tool = sanitize_tool(event.get("tool_name"))
    desc = describe(event.get("tool_input"))
    return f"{ts} {tool}: {desc}" if desc else f"{ts} {tool}"


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


def target_dir() -> Path | None:
    """VOICEHOOK_SESSION=<slug>/<identity> wins; else the ONE live join; else None."""
    env = (os.environ.get("VOICEHOOK_SESSION") or "").strip()
    if env:
        slug, _, ident = env.partition("/")
        if not slug or not ident:
            return None
        d = vsession.session_dir(slug, ident)
        return d if d.is_dir() else None
    live = live_join_dirs()
    return live[0] if len(live) == 1 else None


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


def post_tool_use(raw: str, now: float | None = None) -> bool:
    """Handle one PostToolUse event (JSON text). True if a line was written.
    Never raises."""
    try:
        event = json.loads(raw) if raw and raw.strip() else {}
        if not isinstance(event, dict):
            return False
        dir_ = target_dir()
        if dir_ is None:
            return False
        append_line(dir_, format_line(event, now))
        return True
    except Exception:  # noqa: BLE001 - a hook must never disturb Claude Code
        return False


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
    try:
        (dir_ / ACTIVITY_NAME).unlink()
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
# settings.json snippet + installer
# --------------------------------------------------------------------------- #
def hook_entry() -> dict:
    return {"matcher": "*",
            "hooks": [{"type": "command", "command": HOOK_COMMAND, "timeout": HOOK_TIMEOUT}]}


def snippet() -> dict:
    return {"hooks": {"PostToolUse": [hook_entry()]}}


def default_settings_path() -> Path:
    return Path.home() / ".claude" / "settings.json"


def _installed(entries: list) -> bool:
    for e in entries:
        if not isinstance(e, dict):
            continue
        for h in e.get("hooks") or []:
            if isinstance(h, dict) and str(h.get("command", "")).strip() in _OUR_COMMANDS:
                return True
    return False


def install(path: Path) -> tuple[int, str]:
    """Merge the PostToolUse hook into settings.json. Idempotent; refuses to
    touch a file that is not a JSON object."""
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
    post = hooks.get("PostToolUse")
    if post is None:
        post = hooks["PostToolUse"] = []
    if not isinstance(post, list):
        return 1, f"[error] {path}: 'hooks.PostToolUse' is not a list; not changed."
    if _installed(post):
        return 0, f"already installed in {path}"
    post.append(hook_entry())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return 0, f"installed PostToolUse hook in {path}"


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    cmd = args[0] if args else ""
    if cmd == "post-tool-use":
        try:
            raw = sys.stdin.read()
        except Exception:  # noqa: BLE001
            raw = ""
        post_tool_use(raw)
        return 0  # always 0, nothing on stdout
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
    print("usage: voicehook-agent hook {post-tool-use|print|install [--settings PATH]}",
          file=sys.stderr)
    return 2


def console_main() -> None:  # `voicehook-agent-hook`
    sys.exit(main())


if __name__ == "__main__":
    console_main()
