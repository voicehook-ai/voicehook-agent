"""Multi-session activity relay (0.15.0): every Claude Code session of a machine in the
activity feed of the running call.

Token distribution:
* The joining session (`join` over the HTTPS bridge, or `bridge-session --save` of a
  curl/MCP bridge join) mints ONE activity relay token (POST
  /api/bridge/activity/relay-token, label = this machine, default the short host name)
  and writes `~/.voicehook/relay.json` atomically, mode 0600, dir 0700:
  {url, token, room, expires_at, owner_pid, owner_session, written_at}.
  `owner_session` is a hash of the joining Claude Code session id (never the id itself),
  so that session does not report twice (its own hook already feeds the call).
  A WebRTC join (the default transport, no bridge session to mint with) writes a LOCAL
  relay instead: {kind: "local", dir, label, room, expires_at, owner_pid, owner_session,
  written_at}. The hooks then append `HH:MM:SS relay: Helfer <machine>: <line>` to the
  join's own activity.log, which the join publishes as operator.activity (the same line
  the server builds for a relay POST). No token, no network. A join answer that carries
  `relay` {token, url, expires_in} still wins.
* On leave / call end / SIGTERM (try/finally) the owner removes the file, but only if it
  still names the owner (owner_pid or owner_session, plus room).
* Readers ignore a file that is a symlink, not ours (uid), group/world readable,
  expired, whose owner pid is gone, or that a 401/403/410 marked dead
  (`relay.dead` = hash of the token; a reader never deletes a file it does not own).

Hook (`voicehook-agent-hook user-prompt-submit|post-tool-use|post-tool-use-failure|stop`):
one short line per event, posted to the relay as `<session label>: <text>`; the server
shows it as `relay: Helfer <machine>: <session label>: <text>`:
* UserPromptSubmit: `neuer Auftrag` plus at most 3 category words from a fixed list
  (PR, Deploy, Tests, Bugfix, ...). Never words of the prompt itself.
* PostToolUse Bash: `gh pr create` -> `PR #N geöffnet`, `gh pr merge` -> `PR #N gemergt`,
  a deploy command -> `Deploy gelaufen` (number from the command or the tool output;
  neither command nor output is ever sent). GitHub MCP create/merge tools likewise.
* PostToolUseFailure / a tool_response with is_error: `Fehler bei <Tool>` (+ the tool's
  own description; never the error text); a failed deploy or merge says so.
* Stop: `Turn fertig`.
Session label: $VOICEHOOK_SESSION_LABEL, else the Claude Code session name
(`session_title` of the hook event, else the last `custom-title` of the transcript),
else the basename of cwd. Scrubbed, max 30 chars.
Throttle: the same line of one session at most once per 120 s; turn/stop at most one
per 5 s per session; all sessions together at most 8 lines per 10 s (PR/deploy/error up
to 16; the server allows 20 per token).
The POST runs in a detached child (2 s timeout), the hook itself never waits on the
network, prints nothing and always exits 0.

stdlib only (the hook starts fast).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import stat
import time
from pathlib import Path

from . import activity as vact

RELAY_NAME = "relay.json"
DEAD_NAME = "relay.dead"
STATE_NAME = "relay-state.json"
MINT_PATH = "/api/bridge/activity/relay-token"
POST_TIMEOUT_S = 2.0
MINT_TIMEOUT_S = 5.0
LABEL_ENV = "VOICEHOOK_SESSION_LABEL"     # label of THIS Claude Code session (hook side)
MACHINE_LABEL_ENV = "VOICEHOOK_RELAY_LABEL"  # token label (join side), default host name
NO_RELAY_ENV = "VOICEHOOK_NO_RELAY"
MACHINE_LABEL_MAX = 22                    # server chip.LABEL_MAX
SESSION_LABEL_MAX = 30
TEXT_MAX = 120                            # server NOTE_MAX
DEDUPE_S = 120.0
LOW_GAP_S = 5.0
GLOBAL_WINDOW_S = 10.0
GLOBAL_MAX = 8
GLOBAL_MAX_IMPORTANT = 16
SESSIONS_KEEP = 50
TRANSCRIPT_TAIL = 512 * 1024
IMPORTANT = {"pr", "deploy", "error"}


# --------------------------------------------------------------------------- #
# relay.json
# --------------------------------------------------------------------------- #
def relay_path() -> Path:
    return vact.state_root() / RELAY_NAME


def _h(value: str, salt: str) -> str:
    return hashlib.sha256(f"{salt}|{value}".encode()).hexdigest()[:16]


def session_hash(session_id: object) -> str:
    """Hash of a Claude Code session id ('' when missing): relay.json never holds the id."""
    sid = str(session_id or "").strip()
    return _h(sid, "vh-session") if sid else ""


def own_session_hash() -> str:
    """The calling Claude Code session (Bash tool env CLAUDE_CODE_SESSION_ID)."""
    return session_hash(os.environ.get("CLAUDE_CODE_SESSION_ID"))


def _token_hash(token: str) -> str:
    return _h(token, "vh-relay-dead")


def machine_label() -> str:
    """Token label: $VOICEHOOK_RELAY_LABEL, else the short host name (max 22, no '|')."""
    raw = (os.environ.get(MACHINE_LABEL_ENV) or "").strip() or socket.gethostname().split(".")[0]
    lbl = re.sub(r"[|:]", " ", vact.scrub(raw))
    lbl = " ".join(lbl.split())[:MACHINE_LABEL_MAX].strip()
    return lbl or "Rechner"


def relay_disabled() -> bool:
    return (os.environ.get(NO_RELAY_ENV) or "").strip().lower() in ("1", "true", "yes", "on")


def write_relay(url: str, token: str, room: str, expires_in: float | None, *,
                owner_pid: int | None = None, owner_session: str | None = None,
                now: float | None = None) -> Path:
    """Write relay.json atomically (tmp + rename, 0600, dir 0700). ValueError on bad input."""
    url = str(url or "").strip()
    if not re.match(r"^https?://[^\s/]+/\S*$", url):
        raise ValueError("relay url must be an http(s) URL")
    if not isinstance(token, str) or not token.strip() or any(c.isspace() for c in token.strip()):
        raise ValueError("relay token missing")
    t = time.time() if now is None else now
    try:
        ttl = float(expires_in) if expires_in is not None else 12 * 3600.0
    except (TypeError, ValueError):
        ttl = 12 * 3600.0
    data = {"url": url, "token": token.strip(), "room": str(room or ""),
            "expires_at": int(t + max(0.0, ttl)),
            "owner_pid": int(os.getpid() if owner_pid is None else owner_pid),
            "owner_session": own_session_hash() if owner_session is None else owner_session,
            "written_at": int(t)}
    vact._private_dir(vact.state_root())
    path = relay_path()
    vact._write_private(path, json.dumps(data))
    try:
        (vact.state_root() / DEAD_NAME).unlink()
    except OSError:
        pass
    return path


LOCAL = "local"
LOCAL_TTL_S = 12 * 3600.0
LOCAL_TAG = vact.RELAY_TOOL  # tool field of a local relay line (server: activity.RELAY_TAG)


def write_local_relay(dir_: Path, room: str, *, label: str | None = None,
                      owner_pid: int | None = None, owner_session: str | None = None,
                      now: float | None = None) -> Path:
    """WebRTC join: relay.json pointing at the join's own session dir (0600, atomic).
    ValueError when dir_ is not a directory."""
    d = Path(dir_).resolve()
    if not d.is_dir():
        raise ValueError("join dir missing")
    t = time.time() if now is None else now
    data = {"kind": LOCAL, "dir": str(d), "label": label or machine_label(),
            "room": str(room or ""), "expires_at": int(t + LOCAL_TTL_S),
            "owner_pid": int(os.getpid() if owner_pid is None else owner_pid),
            "owner_session": own_session_hash() if owner_session is None else owner_session,
            "written_at": int(t)}
    vact._private_dir(vact.state_root())
    path = relay_path()
    vact._write_private(path, json.dumps(data))
    return path


def _is_local(d: dict) -> bool:
    return d.get("kind") == LOCAL


def _read_raw() -> dict | None:
    """relay.json as dict, or None when missing, a symlink, not ours, readable by others
    or not valid."""
    path = relay_path()
    try:
        st = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o077:
        return None
    if hasattr(os, "getuid") and st.st_uid != os.getuid():
        return None
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict):
        return None
    if _is_local(d):
        return d if isinstance(d.get("dir"), str) and os.path.isabs(d["dir"]) else None
    if not isinstance(d.get("token"), str) or not isinstance(d.get("url"), str):
        return None
    return d


def _is_dead(token: str) -> bool:
    try:
        return (vact.state_root() / DEAD_NAME).read_text(encoding="utf-8").strip() == _token_hash(token)
    except OSError:
        return False


def load_relay(now: float | None = None) -> dict | None:
    """A usable relay.json (see _read_raw), not expired, owner alive, not marked dead."""
    d = _read_raw()
    if d is None:
        return None
    t = time.time() if now is None else now
    try:
        if float(d.get("expires_at") or 0) <= t:
            return None
        pid = int(d.get("owner_pid") or 0)
    except (TypeError, ValueError):
        return None
    if pid > 0 and not vact._pid_alive(pid):
        return None
    if not _is_local(d) and _is_dead(d["token"]):
        return None
    return d


def mark_dead(token: str) -> None:
    """401/403/410 from the relay: readers skip this token. The file stays (not ours)."""
    try:
        vact._private_dir(vact.state_root())
        vact._write_private(vact.state_root() / DEAD_NAME, _token_hash(token))
    except OSError:
        pass


def remove_relay(*, owner_pid: int | None = None, owner_session: str | None = None,
                 room: str | None = None) -> bool:
    """Owner cleanup: remove relay.json only if it still names this owner (pid or session
    hash) and, when given, this room. True if removed. Never raises."""
    try:
        d = _read_raw()
        if d is None:
            return False
        mine = (owner_pid is not None and int(d.get("owner_pid") or 0) == int(owner_pid)) or \
               (bool(owner_session) and d.get("owner_session") == owner_session)
        if not mine or (room is not None and str(d.get("room") or "") != str(room)):
            return False
        relay_path().unlink()
        for name in (DEAD_NAME, STATE_NAME):
            try:
                (vact.state_root() / name).unlink()
            except OSError:
                pass
        return True
    except Exception:  # noqa: BLE001
        return False


def relay_from_answer(answer: object) -> dict | None:
    """A join answer that carries a relay token: {relay: {token, url, expires_in}} or the
    mint answer itself {token, url, expires_in}."""
    if not isinstance(answer, dict):
        return None
    r = answer.get("relay") if isinstance(answer.get("relay"), dict) else answer
    if isinstance(r.get("token"), str) and isinstance(r.get("url"), str) and r["token"] and r["url"]:
        return {"token": r["token"], "url": r["url"], "expires_in": r.get("expires_in")}
    return None


def mint_relay(base: str, session: str, label: str | None = None,
               timeout: float = MINT_TIMEOUT_S) -> dict | None:
    """POST <base>/api/bridge/activity/relay-token {label} with the bridge session.
    The answer {token, url, expires_in} or None. Never raises."""
    import urllib.error
    import urllib.request
    base = str(base or "").rstrip("/")
    if not re.match(r"^https?://[^\s/]+", base) or not session:
        return None
    req = urllib.request.Request(
        base + MINT_PATH, method="POST",
        data=json.dumps({"label": label or machine_label()}).encode("utf-8"),
        headers={"authorization": f"Bearer {session}", "content-type": "application/json",
                 "user-agent": "voicehook-agent"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # base from our own 0600 file / CLI arg
            return relay_from_answer(json.loads(r.read().decode("utf-8")))
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------- #
# event -> line
# --------------------------------------------------------------------------- #
_KEYWORDS = [
    (re.compile(r"(?i)\b(pr|prs|pull[ -]?requests?)\b"), "PR"),
    (re.compile(r"(?i)\bdeploy"), "Deploy"),
    (re.compile(r"(?i)\btest"), "Tests"),
    (re.compile(r"(?i)\b(bug|fix|fehler|kaputt|broken)"), "Bugfix"),
    (re.compile(r"(?i)\breview"), "Review"),
    (re.compile(r"(?i)\b(doku|docs?|readme|dokumentation)\b"), "Doku"),
    (re.compile(r"(?i)\brefactor"), "Refactoring"),
    (re.compile(r"(?i)\b(merge|mergen|gemergt)\b"), "Merge"),
    (re.compile(r"(?i)\b(release|version)"), "Release"),
    (re.compile(r"(?i)\b(ui|design|frontend|css|layout)\b"), "UI"),
]


def prompt_goal(prompt: object) -> str:
    """`neuer Auftrag` plus up to 3 category words of a FIXED list. The prompt text itself
    never leaves the machine."""
    text = str(prompt or "")
    words: list[str] = []
    for rx, word in _KEYWORDS:
        if word not in words and rx.search(text):
            words.append(word)
        if len(words) == 3:
            break
    return "neuer Auftrag: " + ", ".join(words) if words else "neuer Auftrag"


_SEG_SPLIT = re.compile(r"&&|\|\||[;|\n]")
_READONLY = {"grep", "rg", "ag", "cat", "less", "more", "head", "tail", "echo", "printf", "git",
             "ls", "find", "sed", "awk", "man", "which", "type", "vi", "vim", "nano", "code",
             "wc", "diff", "jq", "test", "["}
_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_DEPLOY_RX = re.compile(
    r"(?:^|[\s/])(?:"
    r"(?:wrangler|vercel|netlify|firebase|fly|flyctl|railway|serverless|sls|cdk|amplify)\b[^\n]*\bdeploy\b"
    r"|supabase\s+(?:functions|db)\s+(?:deploy|push)\b"
    r"|(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?deploy\b"
    r"|make\s+deploy\b"
    r"|[\w.\-]*deploy[\w.\-]*\.sh\b"
    r"|vercel\s+--prod\b"
    r"|kubectl\s+(?:apply|rollout)\b"
    r"|helm\s+(?:upgrade|install)\b"
    r"|gh\s+workflow\s+run\s+[\w.\-]*deploy"
    r")")
_PR_CREATE = re.compile(r"\bgh\s+pr\s+create\b")
_PR_MERGE = re.compile(r"\bgh\s+pr\s+merge\b")
_PR_URL = re.compile(r"/pull/(\d{1,7})\b")
_PR_HASH = re.compile(r"#(\d{1,7})\b")


def _segments(command: str) -> list[str]:
    out = []
    for seg in _SEG_SPLIT.split(command):
        words = [w for w in seg.strip().split() if not _ENV_ASSIGN.match(w)]
        if words and words[0].rsplit("/", 1)[-1] not in _READONLY:
            out.append(" ".join(words))
    return out


def _is_deploy(command: str) -> bool:
    return any(_DEPLOY_RX.search(" " + s) for s in _segments(command))


def _has(rx: re.Pattern, command: str) -> bool:
    return any(rx.search(s) for s in _segments(command))


def _output(resp: object) -> str:
    if isinstance(resp, str):
        return resp[:20000]
    if isinstance(resp, dict):
        return " ".join(str(resp.get(k) or "") for k in ("stdout", "stderr", "output", "result",
                                                          "url", "html_url"))[:20000]
    if isinstance(resp, list):
        return " ".join(str(x.get("text") or "") if isinstance(x, dict) else str(x)
                        for x in resp)[:20000]
    return ""


def _resp_failed(resp: object) -> bool:
    if not isinstance(resp, dict):
        return False
    if resp.get("is_error") is True or resp.get("isError") is True:
        return True
    for k in ("exit_code", "exitCode", "returncode", "code"):
        v = resp.get(k)
        if isinstance(v, int) and not isinstance(v, bool) and v != 0:
            return True
    return False


def _merge_arg(command: str) -> str:
    """The PR number argument of `gh pr merge` (word by word, no backtracking regex)."""
    for seg in _segments(command):
        words = seg.split()
        for i in range(len(words) - 2):
            if words[i:i + 3] == ["gh", "pr", "merge"]:
                for w in words[i + 3:]:
                    if re.fullmatch(r"#?\d{1,7}", w):
                        return w.lstrip("#")
    return ""


def _pr_number(command: str, out: str, merge: bool) -> str:
    num = _merge_arg(command) if merge else ""
    if num:
        return f"PR #{num}"
    m = _PR_URL.search(command if merge else "") or _PR_URL.search(out) \
        or (_PR_HASH.search(out) if merge else None)
    return f"PR #{m.group(1)}" if m else "PR"


def classify(event: dict) -> tuple[str, str] | None:
    """(kind, text) for one hook event, or None (nothing to report). kind in
    turn/stop/pr/deploy/error."""
    name = str(event.get("hook_event_name") or "")
    if name == "UserPromptSubmit":
        return "turn", prompt_goal(event.get("prompt"))
    if name == "Stop":
        return "stop", "Turn fertig"
    if name not in ("PostToolUse", "PostToolUseFailure"):
        return None
    tool = vact.sanitize_tool(event.get("tool_name"))
    inp = event.get("tool_input") if isinstance(event.get("tool_input"), dict) else {}
    resp = event.get("tool_response")
    failed = name == "PostToolUseFailure" or _resp_failed(resp)
    if name == "PostToolUseFailure" and event.get("is_interrupt") is True:
        return None  # the user pressed Esc: no error worth a line
    low = tool.lower()
    if tool == "Bash":
        cmd = str(inp.get("command") or "")
        out = _output(resp)
        if _has(_PR_MERGE, cmd):
            pr = _pr_number(cmd, out, merge=True)
            return ("error", f"{pr} Merge fehlgeschlagen") if failed else ("pr", f"{pr} gemergt")
        if _has(_PR_CREATE, cmd):
            if failed:
                return "error", "PR erstellen fehlgeschlagen"
            return "pr", f"{_pr_number(cmd, out, merge=False)} geöffnet"
        if _is_deploy(cmd):
            return ("error", "Deploy fehlgeschlagen") if failed else ("deploy", "Deploy gelaufen")
    elif "pull_request" in low and ("create" in low or "merge" in low):
        out = _output(resp) if not isinstance(resp, dict) else json.dumps(resp)[:20000]
        m = _PR_URL.search(out) or re.search(r'"number":\s*(\d{1,7})', out)
        if m is None and "merge" in low:
            m = re.search(r"^(\d{1,7})$", str(inp.get("pullNumber") or inp.get("pull_number") or ""))
        pr = f"PR #{m.group(1)}" if m else "PR"
        verb = "gemergt" if "merge" in low else "geöffnet"
        return ("error", f"{pr} {'Merge' if 'merge' in low else 'erstellen'} fehlgeschlagen") \
            if failed else ("pr", f"{pr} {verb}")
    if failed:
        # only the tool's own description (3-8 words by Claude Code), never the error text
        desc = vact.describe(inp, tool) if isinstance(inp.get("description"), str) else ""
        return "error", f"Fehler bei {tool}: {desc}" if desc else f"Fehler bei {tool}"
    return None


# --------------------------------------------------------------------------- #
# session label
# --------------------------------------------------------------------------- #
def _clean_label(raw: object) -> str:
    s = re.sub(r"[|:]", " ", vact.scrub(str(raw or "")))
    return " ".join(s.split())[:SESSION_LABEL_MAX].strip()


def _transcript_title(path: object) -> str:
    """Last `custom-title` (the name set with /rename) in the transcript's tail."""
    if not isinstance(path, str) or not path:
        return ""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - TRANSCRIPT_TAIL))
            data = f.read().decode("utf-8", "replace")
    except OSError:
        return ""
    found = ""
    for line in data.splitlines():
        if '"custom-title"' not in line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if isinstance(d, dict) and d.get("type") == "custom-title" and d.get("customTitle"):
            found = str(d["customTitle"])
    return found


def session_label(event: dict, cached: str = "") -> str:
    """$VOICEHOOK_SESSION_LABEL > session_title of the event > custom-title of the
    transcript > the label seen before > basename of cwd > `Session`."""
    for raw in (os.environ.get(LABEL_ENV), event.get("session_title"),
                _transcript_title(event.get("transcript_path"))):
        lbl = _clean_label(raw)
        if lbl:
            return lbl
    if cached:
        return cached
    lbl = _clean_label(vact.basename(event.get("cwd")))
    return lbl or "Session"


# --------------------------------------------------------------------------- #
# throttle + post
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
        res = fn(st)
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, json.dumps(st).encode("utf-8"))
        return res
    finally:
        os.close(fd)


def _admit(skey: str, kind: str, text: str, event: dict, t: float) -> str | None:
    """Throttle under the state lock. The final line or None (dropped)."""
    def step(st: dict):
        sessions = st.get("sessions") if isinstance(st.get("sessions"), dict) else {}
        posts = [p for p in (st.get("posts") or []) if isinstance(p, (int, float))
                 and t - p < GLOBAL_WINDOW_S]
        me = sessions.get(skey) if isinstance(sessions.get(skey), dict) else {}
        label = session_label(event, str(me.get("label") or ""))
        line = vact.scrub(f"{label}: {text}")[:TEXT_MAX].strip()
        me["label"] = label
        sessions.pop(skey, None)
        keep = sorted(((k, v) for k, v in sessions.items() if isinstance(v, dict)),
                      key=lambda kv: float(kv[1].get("at") or 0))[-(SESSIONS_KEEP - 1):]
        st["sessions"] = {**dict(keep), skey: me}
        st["posts"] = posts
        if me.get("last") == line and t - float(me.get("at") or 0) < DEDUPE_S:
            return None
        if kind not in IMPORTANT and t - float(me.get("low_at") or 0) < LOW_GAP_S:
            return None
        if len(posts) >= (GLOBAL_MAX_IMPORTANT if kind in IMPORTANT else GLOBAL_MAX):
            return None
        me.update(last=line, at=t)
        if kind not in IMPORTANT:
            me["low_at"] = t
        posts.append(t)
        return line
    return _with_state(step)


def post_line(text: str, timeout: float = POST_TIMEOUT_S) -> int | None:
    """POST {text} to the relay of the current relay.json (Bearer, never in the URL).
    HTTP status (None: no file / network error). 401/403/410 mark the token dead."""
    import urllib.error
    import urllib.request
    rel = load_relay()
    if rel is None or _is_local(rel):
        return None
    req = urllib.request.Request(
        rel["url"], method="POST",
        data=json.dumps({"text": text}, ensure_ascii=False).encode("utf-8"),
        headers={"authorization": f"Bearer {rel['token']}", "content-type": "application/json",
                 "user-agent": "voicehook-agent-hook"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # url from our own 0600 file
            status = r.status
    except urllib.error.HTTPError as e:
        status = e.code
    except Exception:  # noqa: BLE001
        return None
    if status in (401, 403, 410):
        mark_dead(rel["token"])
    return status


def _spawn_post(text: str) -> None:
    """Detached `voicehook-agent-hook relay-post` (text on stdin, never argv), isolated
    from the cwd of the reporting session (vact.child_argv: -I, cwd = state dir)."""
    import subprocess
    try:
        p = subprocess.Popen(vact.child_argv("relay-post"),
                             stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True,
                             cwd=vact.child_cwd())
        assert p.stdin is not None
        p.stdin.write(json.dumps({"text": text}).encode("utf-8"))
        p.stdin.close()
    except Exception:  # noqa: BLE001
        return


def append_local(rel: dict, line: str, now: float | None = None) -> bool:
    """Local relay: append `HH:MM:SS relay: Helfer <machine>: <line>` to the activity.log of
    the join named by rel (only when its session.json names the owner pid). Never raises."""
    try:
        d = Path(str(rel.get("dir") or ""))
        st = os.lstat(d)
        if not stat.S_ISDIR(st.st_mode) or (hasattr(os, "getuid") and st.st_uid != os.getuid()):
            return False
        info = json.loads((d / vact.vsession.INFO_NAME).read_text(encoding="utf-8"))
        if int(info.get("pid") or 0) != int(rel.get("owner_pid") or -1):
            return False
        label = re.sub(r"[|:]", " ", vact.scrub(str(rel.get("label") or ""))).strip() or "Rechner"
        body = vact.scrub(f"Helfer {label}: {line}")[:vact.DESC_MAX].strip()
        vact.append_line(d, f"{vact._ts(now)} {LOCAL_TAG}: {body}")
        return True
    except Exception:  # noqa: BLE001
        return False


def relay_post_main(raw: str) -> int:
    """The detached child: post one line. Always 0."""
    try:
        d = json.loads(raw or "{}")
        text = str(d.get("text") or "") if isinstance(d, dict) else ""
        if text:
            post_line(text)
    except Exception:  # noqa: BLE001, S110 - detached child, nobody to tell
        pass
    return 0


def handle_event(raw: str, now: float | None = None, post=None) -> str | None:
    """Hook entry: the line handed to `post` (default: detached POST) or None. Silent
    without relay.json, for the joining session itself, and on anything unexpected."""
    try:
        rel = load_relay(now)
        if rel is None:
            return None
        event = json.loads(raw) if raw and raw.strip() else {}
        if not isinstance(event, dict):
            return None
        skey = session_hash(event.get("session_id")) or session_hash(
            os.environ.get("CLAUDE_CODE_SESSION_ID"))
        if not skey:
            skey = "cwd-" + _h(str(event.get("cwd") or ""), "vh-cwd")
        own = str(rel.get("owner_session") or "")
        if own and own == skey:
            return None  # the joining session: its own hook already feeds the call
        got = classify(event)
        if got is None:
            return None
        kind, text = got
        line = _admit(skey, kind, text, event, time.time() if now is None else now)
        if not line:
            return None
        if post is not None:
            post(line)
        elif _is_local(rel):
            append_local(rel, line)
        else:
            _spawn_post(line)
        return line
    except Exception:  # noqa: BLE001 - a hook must never disturb Claude Code
        return None
