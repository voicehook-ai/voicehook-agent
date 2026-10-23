"""Other coding agents (0.20.0): the Claude Code automation for OpenAI Codex CLI.

Codex CLI (rust-v0.16x, hooks stable and on by default) has Claude-Code-compatible hooks
(`$CODEX_HOME/hooks.json`, stdin JSON with session_id, cwd, hook_event_name, tool_name,
tool_input, tool_response, prompt, turn_id), plus the older `notify` program
(config.toml, JSON as the last argv argument, only `agent-turn-complete`) and MCP servers
(`[mcp_servers.<name>]`). `voicehook-agent hook install --agent codex` writes all three:

* hooks.json: PreToolUse, PostToolUse, UserPromptSubmit, Stop, PermissionRequest,
  SessionStart -> `voicehook-agent-hook codex <event>`. The adapter normalizes the Codex event to the
  Claude Code shape and runs the same handlers (activity.py, multisession.py,
  notify.py, and board.py when present):
  - Bash (`exec_command`, `shell`, `local_shell` too): Codex sends no `description`, so
    the activity line names a category from a FIXED list (Tests, Git, Build, ...) and
    never the command; PR/deploy detection reads the command locally as for Claude Code.
  - apply_patch: `Edit: <basename of the first file of the patch>`.
  - A failed Bash (`Exit code: N`, N != 0) counts as an error (`Fehler bei Bash`).
  - Stop (0.21.3, asks.py): a turn that ends with an open question to the human
    (`last_assistant_message`) posts it once to the call's Decision Board.
  - stdout: only what board.py (0.21.0, when present) answers on PostToolUse,
    UserPromptSubmit, SessionStart and Stop (additionalContext / Stop decision block with
    an answer of the human), the same JSON Codex reads from Claude Code hooks; Stop
    always JSON (`{}`), Codex wants JSON there. PermissionRequest prints nothing: no
    decision, the human decides in Codex as before.
* notify: `voicehook-agent-hook codex-notify` (only when config.toml has no `notify` yet):
  turn complete -> `Turn fertig` also before the hooks are trusted. One line per turn:
  the Stop hook and notify share a turn id list (codex-turns.json).
* `[mcp_servers.voicehook-local]` -> `voicehook-agent-hook mcp --agent codex` (mcpserver.py):
  status, decide, next, activity as tools.

Own join (Codex itself ran `voicehook-agent join`): turn start/end also land in the
join's activity log (`Codex: neuer Auftrag`, `Codex: Turn fertig`). Every other Codex
session is a helper while ~/.voicehook/relay.json is usable, exactly like a Claude Code
helper. Session identity: hook `session_id` = $CODEX_THREAD_ID of the Codex shell
(multisession.own_session_id), never stored, only hashed.

Codex runs a new or changed user hook only after it was trusted once (`/hooks` in Codex).
The installer cannot do that for the user and says so.

Every file the installer changes is copied to `<file>.voicehook-backup-<UTC time>` first.
stdlib only (the hook starts fast).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

from . import activity as vact

AGENTS = ("claude", "codex", "opencode")
CODEX = "codex"
OPENCODE = "opencode"
CODEX_EVENTS = {"pre-tool-use": "PreToolUse", "post-tool-use": "PostToolUse",
                "user-prompt-submit": "UserPromptSubmit", "stop": "Stop",
                "permission-request": "PermissionRequest", "session-start": "SessionStart"}
PRINT_EVENTS = ("PostToolUse", "UserPromptSubmit", "SessionStart", "Stop")
CODEX_HOOK_PREFIX = "voicehook-agent-hook codex "
CODEX_HOOK_TIMEOUT = 5
CODEX_NOTIFY = ["voicehook-agent-hook", "codex-notify"]
MCP_NAME = "voicehook-local"
MCP_ENV_VARS = ["VOICEHOOK_SESSION_LABEL", "VOICEHOOK_STATE_DIR", "VOICEHOOK_AGENT_HOME",
                "VOICEHOOK_NO_RELAY", "VOICEHOOK_RELAY_LABEL"]
AGENT_TOOL = "Codex"            # tool field of the own-join turn lines
TURNS_NAME = "codex-turns.json"
TURNS_KEEP = 100
LABELS_NAME = "agent-labels.json"
LABELS_KEEP = 50
LABEL_TTL_S = 12 * 3600.0
BASH_NAMES = {"Bash", "bash", "shell", "exec_command", "local_shell", "unified_exec",
              "container.exec"}
PATCH_NAMES = {"apply_patch", "applyPatch"}
TRUST_HINT = ("Codex runs new hooks only after you trust them once: start `codex`, type "
              "/hooks and trust the voicehook entries (PreToolUse, PostToolUse, "
              "UserPromptSubmit, Stop, PermissionRequest, SessionStart). notify and the MCP "
              "tools work without that.")


# --------------------------------------------------------------------------- #
# Codex event -> Claude Code event
# --------------------------------------------------------------------------- #
_CATEGORIES = [
    (re.compile(r"(?:^|\s)(?:pytest|jest|vitest|mocha|tox|nox|playwright)\b|\b(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?test\b|\b(?:go|cargo|deno|dotnet|mix)\s+test\b|\bunittest\b"), "Tests"),
    (re.compile(r"(?:^|\s)(?:ruff|eslint|flake8|mypy|pyright|prettier|black|biome|clippy|tsc)\b|\b(?:npm|pnpm|yarn)\s+(?:run\s+)?lint\b"), "Lint"),
    (re.compile(r"(?:^|\s)gh\s+pr\b"), "PR"),
    (re.compile(r"(?:^|\s)git\s+(?:commit|push|merge|rebase|cherry-pick)\b"), "Git"),
    (re.compile(r"(?:^|\s)(?:pip|uv|npm|pnpm|yarn|bun|cargo|brew|poetry)\s+(?:install|add|sync)\b"), "Install"),
    (re.compile(r"\b(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?build\b|(?:^|\s)(?:make|cargo\s+build|go\s+build|uv\s+build|vite\s+build)\b"), "Build"),
]


def _command_text(cmd: object) -> str:
    """Codex Bash command: a string, or an argv list (`["bash", "-lc", "..."]`)."""
    if isinstance(cmd, str):
        return cmd
    if isinstance(cmd, list) and all(isinstance(x, str) for x in cmd):
        if len(cmd) >= 3 and cmd[0].rsplit("/", 1)[-1] in ("bash", "sh", "zsh") \
                and cmd[1] in ("-lc", "-c"):
            return cmd[2]
        return " ".join(cmd)
    return ""


def command_category(command: str) -> str:
    """One word of a FIXED list for a shell command ('' when none fits). The command
    itself never leaves the machine."""
    from . import multisession as vmulti
    text = vmulti._unquote(command)
    if vmulti._deploy_kind(command):
        return "Deploy"
    for rx, word in _CATEGORIES:
        if rx.search(text):
            return word
    return ""


_PATCH_FILE = re.compile(r"^\*\*\* (?:Update|Add|Delete) File: (.+)$", re.MULTILINE)


def patch_file(patch: object) -> str:
    m = _PATCH_FILE.search(patch) if isinstance(patch, str) else None
    return m.group(1).strip() if m else ""


_EXIT = re.compile(r"(?i)^\s*(?:exit code|process exited with code)[:\s]+(-?\d+)")


def _bash_failed(resp: object) -> bool:
    if isinstance(resp, str):
        m = _EXIT.search(resp)
        return bool(m) and m.group(1) != "0"
    if isinstance(resp, dict):
        for k in ("exit_code", "exitCode", "code"):
            v = resp.get(k)
            if isinstance(v, int) and not isinstance(v, bool):
                return v != 0
        for k in ("output", "stdout", "formatted_output", "aggregated_output"):
            if isinstance(resp.get(k), str) and _bash_failed(resp[k]):
                return True
    return False


def normalize_codex(event: dict) -> dict:
    """A Codex hook event in the Claude Code shape the handlers know. Never raises."""
    ev = dict(event)
    name = str(ev.get("hook_event_name") or "")
    tool = str(ev.get("tool_name") or "")
    inp = ev.get("tool_input") if isinstance(ev.get("tool_input"), dict) else {}
    if tool in BASH_NAMES:
        cmd = _command_text(inp.get("command") if "command" in inp else inp.get("cmd"))
        new = {"command": cmd}
        desc = inp.get("description")
        if name == "PermissionRequest" and isinstance(desc, str) and desc.strip():
            new["description"] = desc  # Codex's own reason for the approval
        elif name in ("PreToolUse", "PostToolUse"):
            cat = command_category(cmd)
            if cat:
                new["description"] = cat
        ev["tool_name"], ev["tool_input"] = "Bash", new
        if name == "PostToolUse" and _bash_failed(ev.get("tool_response")):
            resp = ev.get("tool_response")
            out = resp if isinstance(resp, str) else json.dumps(resp, ensure_ascii=False)
            ev["tool_response"] = {"is_error": True, "output": out[:20000]}
    elif tool in PATCH_NAMES:
        new = {}
        path = patch_file(inp.get("command") or inp.get("input") or inp.get("patch"))
        if path:
            new["file_path"] = path
        if name == "PermissionRequest" and isinstance(inp.get("description"), str):
            new["description"] = inp["description"]
        ev["tool_name"], ev["tool_input"] = "Edit", new
    elif tool == "spawn_agent":
        ev["tool_name"] = "Agent"
    return ev


# --------------------------------------------------------------------------- #
# turn bookkeeping (Stop hook + notify: one line per turn)
# --------------------------------------------------------------------------- #
def _locked_json(name: str, fn):
    vact._private_dir(vact.state_root())
    fd = os.open(vact.state_root() / name, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                 0o600)
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


def first_turn_end(session_id: object, turn_id: object) -> bool:
    """True the first time a (session, turn) ends; without a turn id always True."""
    from . import multisession as vmulti
    tid = str(turn_id or "").strip()
    if not tid:
        return True
    key = vmulti._h(f"{session_id}|{tid}", "vh-turn")

    def step(st: dict):
        seen = [x for x in (st.get("turns") or []) if isinstance(x, str)]
        if key in seen:
            return False
        st["turns"] = (seen + [key])[-TURNS_KEEP:]
        return True
    try:
        return bool(_locked_json(TURNS_NAME, step))
    except OSError:
        return True


# --------------------------------------------------------------------------- #
# session label shared by hooks and the MCP server
# --------------------------------------------------------------------------- #
def _cwd_key(cwd: str) -> str:
    from . import multisession as vmulti
    return vmulti._h(os.path.realpath(cwd) if cwd else "", "vh-label-cwd")


def remember_label(event: dict, now: float | None = None) -> str:
    """Hook: the label the relay uses for this session, kept per cwd (hash) so the MCP
    server of the same agent (which does not know the session id) uses the same name.
    '' on any error, never raises."""
    try:
        return _remember_label(event, now)
    except Exception:  # noqa: BLE001
        return ""


def _remember_label(event: dict, now: float | None) -> str:
    from . import multisession as vmulti
    from . import relayops as vrelayops
    skey = vmulti.session_hash(event.get("session_id"))
    label = vmulti.session_label(event, vrelayops._cached_label(skey) if skey else "")
    cwd = str(event.get("cwd") or "")
    if not cwd or not label:
        return label
    t = time.time() if now is None else now

    def step(st: dict):
        rows = st.get("cwd") if isinstance(st.get("cwd"), dict) else {}
        rows[_cwd_key(cwd)] = {"label": label, "at": t}
        st["cwd"] = dict(sorted(rows.items(), key=lambda kv: float(
            kv[1].get("at") or 0) if isinstance(kv[1], dict) else 0)[-LABELS_KEEP:])
    try:
        _locked_json(LABELS_NAME, step)
    except OSError:
        pass
    return label


def session_label(agent: str = "", cwd: str | None = None, now: float | None = None) -> str:
    """`from` of the MCP server: $VOICEHOOK_SESSION_LABEL > the label the agent's hook used
    last in this cwd (12 h) > `<cwd basename> <agent>`."""
    from . import multisession as vmulti
    env = vmulti._clean_label(os.environ.get(vmulti.LABEL_ENV))
    if env:
        return env
    if cwd is None:
        try:
            cwd = os.getcwd()
        except OSError:
            cwd = ""
    t = time.time() if now is None else now
    try:
        st = json.loads((vact.state_root() / LABELS_NAME).read_text(encoding="utf-8"))
        row = st.get("cwd", {}).get(_cwd_key(cwd))
        if isinstance(row, dict) and t - float(row.get("at") or 0) < LABEL_TTL_S \
                and vmulti._clean_label(row.get("label")):
            return vmulti._clean_label(row["label"])
    except (OSError, ValueError, AttributeError, TypeError):
        pass
    base = vmulti._clean_label(vact.basename(cwd))[:vmulti.SESSION_LABEL_MAX - 7].strip()
    return f"{base} {agent or 'agent'}".strip() if base else (agent or "Session").capitalize()


def session_key(agent: str = "") -> str:
    """State key of the MCP server's session (same idea as the hook's session hash)."""
    from . import multisession as vmulti
    return vmulti.own_session_hash() or "label-" + vmulti._h(session_label(agent), "vh-from")


def relay_note(agent: str, text: str, rel: dict) -> str | None:
    """MCP `activity` of a helper: `<label>: <text>` through the relay throttle."""
    from . import multisession as vmulti
    label = session_label(agent)
    old = os.environ.get(vmulti.LABEL_ENV)
    os.environ[vmulti.LABEL_ENV] = label  # _admit names the line with the session label
    try:
        line = vmulti._admit(session_key(agent), "note", vact.scrub(text)[:vact.DESC_MAX],
                             {}, time.time())
    finally:
        if old is None:
            os.environ.pop(vmulti.LABEL_ENV, None)
        else:
            os.environ[vmulti.LABEL_ENV] = old
    if not line:
        return None
    if vmulti._is_local(rel):
        vmulti.append_local(rel, line)
    else:
        vmulti.post_line(line)
    return line


# --------------------------------------------------------------------------- #
# hook entry points
# --------------------------------------------------------------------------- #
def _own_join_line(event: dict, text: str, tool: str = AGENT_TOOL) -> None:
    """Codex/opencode itself is in the call: its turn start/end in its own activity log (the
    same target rule as the tool lines, activity._handle); `tool` names the agent."""
    try:
        d = vact.target_dir()
        if d is None:
            return
        if not (os.environ.get("VOICEHOOK_SESSION") or "").strip() \
                and vact._foreign(vact.dir_owner(d), vact._event_session(event)):
            return
        vact.append_line(d, f"{vact._ts(None)} {tool}: {vact.scrub(text)[:vact.DESC_MAX]}")
    except Exception:  # noqa: BLE001
        return


def _board(norm: str) -> str:
    """board.py (0.21.0 session state, board reminder, answers to the session's own
    questions) when this CLI has it: its JSON answer (additionalContext, or on Stop a
    decision block with the answer as reason; Codex reads both like Claude Code)."""
    hook = getattr(vact, "board_hook", None)
    if hook is None:
        return ""
    out = hook(norm)
    if isinstance(out, dict):
        return json.dumps(out, ensure_ascii=False) if out else ""
    return out if isinstance(out, str) else ""


def _stop_question(norm: str) -> None:
    """0.21.3 (asks.py): an open question at the end of the turn (last_assistant_message)
    goes to the Decision Board; Codex has no rewake, the answer comes with the session's
    next hook event (board.py). Never raises."""
    try:
        from . import asks
        asks.stop_ask(norm, wait=False)
    except Exception:  # noqa: BLE001
        return


def codex_hook(action: str, raw: str) -> str:
    """`voicehook-agent-hook codex <event>`: what to print (Stop: JSON, at least `{}`;
    PermissionRequest and PreToolUse: nothing). Never raises."""
    name = CODEX_EVENTS.get(action, "")
    stop = "{}" if name == "Stop" else ""
    try:
        event = json.loads(raw) if raw and raw.strip() else {}
        if not isinstance(event, dict) or not name:
            return stop
        event.setdefault("hook_event_name", name)
        ev = normalize_codex(event)
        norm = json.dumps(ev, ensure_ascii=False)
        remember_label(ev)
        if name == "PermissionRequest":
            vact.notify_hook(norm)  # a decision in the call, never an answer to Codex
            return ""
        if name == "PreToolUse":
            vact.pre_tool_use(norm)
        elif name != "SessionStart":
            if name == "PostToolUse":
                vact.post_tool_use(norm)
            elif name == "UserPromptSubmit":
                from . import multisession as vmulti
                _own_join_line(ev, vmulti.prompt_goal(ev.get("prompt")))
            elif name == "Stop":
                if first_turn_end(ev.get("session_id"), ev.get("turn_id")):
                    _own_join_line(ev, "Turn fertig")
                    vact.relay_hook(norm)
                    vact.notify_progress(norm)
                    _stop_question(norm)
                return _board(norm) or stop
            vact.relay_hook(norm)
            vact.notify_progress(norm)
        out = _board(norm)
        return out if name in PRINT_EVENTS else ""
    except Exception:  # noqa: BLE001 - a hook must never disturb Codex
        return stop


def notify_event(payload: object) -> dict | None:
    """Codex notify JSON (kebab-case) -> a Stop event, or None."""
    if not isinstance(payload, dict) or payload.get("type") != "agent-turn-complete":
        return None
    return {"hook_event_name": "Stop", "session_id": payload.get("thread-id") or "",
            "turn_id": payload.get("turn-id") or "", "cwd": payload.get("cwd") or ""}


def codex_notify(arg: str) -> None:
    """`voicehook-agent-hook codex-notify '<json>'` (Codex notify program). Never raises."""
    try:
        ev = notify_event(json.loads(arg) if arg else None)
        if ev is None or not first_turn_end(ev["session_id"], ev["turn_id"]):
            return
        remember_label(ev)
        _own_join_line(ev, "Turn fertig")
        norm = json.dumps(ev)
        vact.relay_hook(norm)
        vact.notify_progress(norm)
    except Exception:  # noqa: BLE001
        return


# --------------------------------------------------------------------------- #
# installer
# --------------------------------------------------------------------------- #
def codex_home() -> Path:
    env = (os.environ.get("CODEX_HOME") or "").strip()
    return Path(env).expanduser() if env else Path.home() / ".codex"


def codex_hook_command(action: str) -> str:
    # `|| true`: an older voicehook-agent-hook without `codex` exits 2, which blocks a tool
    # (PreToolUse) or the prompt in Codex
    return f"{CODEX_HOOK_PREFIX}{action} || true"


def codex_hooks_snippet() -> dict:
    hooks: dict = {}
    for action, ev in CODEX_EVENTS.items():
        entry: dict = {"hooks": [{"type": "command", "command": codex_hook_command(action),
                                  "timeout": CODEX_HOOK_TIMEOUT}]}
        if ev in ("PreToolUse", "PostToolUse", "PermissionRequest"):  # regex on tool name
            entry = {"matcher": "*", **entry}
        hooks[ev] = [entry]
    return {"hooks": hooks}


def codex_toml_snippet(with_notify: bool = True) -> str:
    out = []
    if with_notify:
        out.append("notify = " + json.dumps(CODEX_NOTIFY))
    out.append(f"\n[mcp_servers.{MCP_NAME}]\ncommand = \"voicehook-agent-hook\"\n"
               f"args = [\"mcp\", \"--agent\", \"codex\"]\n"
               f"env_vars = {json.dumps(MCP_ENV_VARS)}\nstartup_timeout_sec = 10\n"
               f"tool_timeout_sec = 120\n")
    return "\n".join(out)


def _backup(path: Path, stamp: str) -> Path | None:
    if not path.exists():
        return None
    dst = path.with_name(f"{path.name}.voicehook-backup-{stamp}")
    shutil.copy2(path, dst)
    os.chmod(dst, 0o600)
    return dst


def _ours(cmd: object, action: str) -> bool:
    c = str(cmd or "").strip()
    return c in (codex_hook_command(action), f"{CODEX_HOOK_PREFIX}{action}",
                 f"voicehook-agent hook codex {action}")


_TABLE = re.compile(r"^\s*\[")
_NOTIFY = re.compile(r"^\s*notify\s*=")
_MCP_TABLE = re.compile(r"^\s*\[\s*mcp_servers\s*\.\s*(?:\"voicehook-local\"|'voicehook-local'|voicehook-local)\s*\]")


def _toml_state(text: str) -> tuple[bool, bool]:
    """(top-level notify set, our mcp server present) of a config.toml."""
    has_notify = has_mcp = False
    top = True
    for line in text.splitlines():
        if _TABLE.match(line):
            top = False
            if _MCP_TABLE.match(line):
                has_mcp = True
        elif top and _NOTIFY.match(line):
            has_notify = True
    return has_notify, has_mcp


def _toml_ok(text: str) -> bool:
    try:
        import tomllib  # 3.11+
    except ImportError:
        return True
    try:
        tomllib.loads(text)
        return True
    except Exception:  # noqa: BLE001
        return False


def install_codex(home: Path | None = None, now: float | None = None) -> tuple[int, str]:
    """Merge our hooks into <CODEX_HOME>/hooks.json, add notify (only when none is set) and
    the voicehook-local MCP server to config.toml. Idempotent; a backup of every changed
    file; refuses invalid JSON/TOML."""
    home = Path(home) if home is not None else codex_home()
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(time.time() if now is None else now))
    hooks_path, cfg_path = home / "hooks.json", home / "config.toml"
    msgs: list[str] = []
    # hooks.json
    data: dict = {}
    if hooks_path.exists():
        try:
            data = json.loads(hooks_path.read_text(encoding="utf-8") or "{}")
        except (OSError, ValueError) as e:
            return 1, f"[error] {hooks_path} is not valid JSON ({e}); nothing changed."
        if not isinstance(data, dict) or not isinstance(data.get("hooks", {}), dict):
            return 1, f"[error] {hooks_path}: not a JSON object with 'hooks' {{}}; nothing changed."
    cfg_text = ""
    if cfg_path.exists():
        try:
            cfg_text = cfg_path.read_text(encoding="utf-8")
        except OSError as e:
            return 1, f"[error] {cfg_path} not readable ({e}); nothing changed."
        if not _toml_ok(cfg_text):
            return 1, f"[error] {cfg_path} is not valid TOML; nothing changed."
    hooks = data.setdefault("hooks", {})
    for ev in CODEX_EVENTS.values():
        if hooks.get(ev) is not None and not isinstance(hooks[ev], list):
            return 1, f"[error] {hooks_path}: 'hooks.{ev}' is not a list; nothing changed."
    added = []
    snippet = codex_hooks_snippet()["hooks"]
    for action, ev in CODEX_EVENTS.items():
        entries = hooks.setdefault(ev, [])
        if not any(isinstance(e, dict) and any(isinstance(h, dict) and _ours(h.get("command"), action)
                                               for h in e.get("hooks") or []) for e in entries):
            entries.append(snippet[ev][0])
            added.append(ev)
    home.mkdir(parents=True, exist_ok=True)
    if added:
        b = _backup(hooks_path, stamp)
        hooks_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        msgs.append(f"installed {' + '.join(added)} hook{'s' if len(added) > 1 else ''} in "
                    f"{hooks_path}" + (f" (backup {b.name})" if b else ""))
    else:
        msgs.append(f"hooks already installed in {hooks_path}")
    # config.toml
    has_notify, has_mcp = _toml_state(cfg_text)
    new = cfg_text
    if not has_notify:
        line = "notify = " + json.dumps(CODEX_NOTIFY) + "\n"
        lines = new.splitlines(keepends=True)
        idx = next((i for i, ln in enumerate(lines) if _TABLE.match(ln)), len(lines))
        if idx == len(lines) and lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.insert(idx, line + ("\n" if idx < len(lines) else ""))
        new = "".join(lines)
    elif not any(json.dumps(CODEX_NOTIFY)[1:-1] in ln for ln in cfg_text.splitlines()
                 if _NOTIFY.match(ln)):
        msgs.append("config.toml already has its own `notify` program: left as is (turn end "
                    "comes from the Stop hook once trusted)")
    if not has_mcp:
        if new and not new.endswith("\n"):
            new += "\n"
        new += codex_toml_snippet(with_notify=False)
    if new != cfg_text:
        if not _toml_ok(new):
            return 1, f"[error] {cfg_path}: the merge would not be valid TOML; config.toml not changed ({'; '.join(msgs)})."
        b = _backup(cfg_path, stamp)
        cfg_path.write_text(new, encoding="utf-8")
        what = [w for w, on in (("notify", not has_notify), (f"mcp_servers.{MCP_NAME}", not has_mcp)) if on]
        msgs.append(f"added {' + '.join(what)} to {cfg_path}" + (f" (backup {b.name})" if b else ""))
    else:
        msgs.append(f"config.toml already set up ({cfg_path})")
    if added:
        msgs.append(TRUST_HINT)
    return 0, "\n".join(msgs)


# --------------------------------------------------------------------------- #
# opencode (0.25.0): a plugin that shells out to this CLI
# --------------------------------------------------------------------------- #
# opencode (sst/opencode) has no JSON-stdin-per-event hook protocol like Claude Code or
# Codex. Instead it auto-loads JS/TS plugins from `~/.config/opencode/plugin/` and
# `.opencode/plugin/` (an async export that returns an object of hooks, package
# `@opencode-ai/plugin`), running them in its own Bun runtime. A plugin can shell out with
# the `$` on its context. So the installer drops ONE self-contained plugin that builds a
# Claude-Code-shaped event and pipes it to `voicehook-agent-hook opencode <event>` on stdin;
# the Python side (opencode_hook) reuses the Codex adapter and the same handlers. The plugin
# uses only the two hooks that the stable docs pin down verbatim:
#   tool.execute.after:  async (input, output) => {}   input.tool, output.args/title
#   event:               async ({ event }) => {}       event.type === "session.idle"
# -> PostToolUse (one activity line per tool) and Stop (turn end) + notify_progress. There is
# no reliable "turn start"/prompt hook across versions, and the v2 `dev` branch (package
# `@opencode/plugin`, `Plugin.define`) differs; the plugin degrades gracefully (try/catch,
# `|| true`) and the installer prints the version caveat. No board/decide answer path:
# opencode plugins cannot inject context back into the turn, so the hook only WRITES
# (activity, relay, notify), it never prints a decision block (that stays MCP's job).
OPENCODE_EVENTS = {"post-tool-use": "PostToolUse", "stop": "Stop"}
OPENCODE_HOOK_PREFIX = "voicehook-agent-hook opencode "
OPENCODE_PLUGIN_NAME = "voicehook.js"
OPENCODE_AGENT_TOOL = "opencode"   # tool field of the own-join turn lines
OPENCODE_SESSION_ENV = "OPENCODE_SESSION_ID"  # the plugin passes session.id through, hashed only
OPENCODE_HINT = ("opencode auto-loads the plugin on its next start; no trust step. It needs the "
                 "`voicehook-agent-hook` binary on PATH (or set OPENCODE_VOICEHOOK_BIN). For the "
                 "status board / decisions in opencode use the voicehook MCP server (opencode.json "
                 "`mcp`), the plugin only feeds the activity log and turn ends.")


def opencode_config_home() -> Path:
    """`~/.config/opencode` (XDG_CONFIG_HOME honoured), where opencode auto-loads plugins."""
    xdg = (os.environ.get("XDG_CONFIG_HOME") or "").strip()
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "opencode"


# The plugin is stdlib-of-JS only (Bun), fire-and-forget, never throws into opencode. It is
# written as a data string so the installer owns the exact bytes (idempotency, backup).
OPENCODE_PLUGIN_JS = r'''// voicehook.js - generated by `voicehook-agent hook install --agent opencode`.
// Feeds opencode's activity into a running voicehook.ai call via the voicehook-agent-hook
// binary (the same adapter Claude Code and Codex use). Safe to delete; re-create with the
// installer. It only READS opencode events and WRITES activity/turn-end, never blocks a tool.
import { spawn } from "node:child_process";
const BIN = process.env.OPENCODE_VOICEHOOK_BIN || "voicehook-agent-hook";

function feed(event, payload) {
  return new Promise((resolve) => {
    let done = false;
    const end = () => { if (!done) { done = true; resolve(); } };
    try {
      const p = spawn(BIN, ["opencode", event], { stdio: ["pipe", "ignore", "ignore"] });
      p.on("error", end);        // binary not on PATH: ignore, never crash opencode
      p.on("close", end);
      setTimeout(() => { try { p.kill(); } catch (e) {} end(); }, 5000);
      p.stdin.on("error", end);
      p.stdin.end(JSON.stringify(payload));
    } catch (e) { end(); }
  });
}

export const VoicehookPlugin = async ({ project, directory, worktree }) => {
  const cwd = (worktree && worktree.path) || directory || (project && project.path) || process.cwd();
  return {
    // one activity line per finished tool call (opencode built-in tools only;
    // MCP tool calls do not fire this hook, a documented opencode limitation)
    "tool.execute.after": async (input, output) => {
      await feed("post-tool-use", {
        hook_event_name: "PostToolUse",
        session_id: (input && input.sessionID) || "",
        cwd,
        tool_name: (input && input.tool) || "",
        tool_input: (output && output.args) || {},
        tool_response: (output && (output.output != null ? output.output : output.title)) || "",
      });
    },
    // turn finished -> turn-end line + progress (opencode's analog of the Stop hook)
    event: async ({ event }) => {
      if (event && event.type === "session.idle") {
        const sid = (event.properties && event.properties.sessionID) || "";
        await feed("stop", { hook_event_name: "Stop", session_id: sid, cwd });
      }
    },
  };
};
export default VoicehookPlugin;
'''


def opencode_hook(action: str, raw: str) -> str:
    """`voicehook-agent-hook opencode <event>`: the plugin already sends a Claude-Code-shaped
    event (PostToolUse, Stop) on stdin; normalize it (Bash -> category, apply_patch -> file)
    and run the same handlers as Codex. Writes only; never prints a decision block. Never
    raises (a plugin must not disturb opencode)."""
    name = OPENCODE_EVENTS.get(action, "")
    try:
        event = json.loads(raw) if raw and raw.strip() else {}
        if not isinstance(event, dict) or not name:
            return ""
        event.setdefault("hook_event_name", name)
        ev = normalize_codex(event)
        norm = json.dumps(ev, ensure_ascii=False)
        remember_label(ev)
        if name == "PostToolUse":
            vact.post_tool_use(norm)
            vact.relay_hook(norm)
            vact.notify_progress(norm)
        elif name == "Stop":
            if first_turn_end(ev.get("session_id"), ev.get("turn_id")):
                _own_join_line(ev, "Turn fertig", tool=OPENCODE_AGENT_TOOL)
                vact.relay_hook(norm)
                vact.notify_progress(norm)
        return ""
    except Exception:  # noqa: BLE001 - a hook must never disturb opencode
        return ""


def install_opencode(home: Path | None = None, now: float | None = None) -> tuple[int, str]:
    """Write the voicehook plugin to <config>/opencode/plugin/voicehook.js. Idempotent (only
    rewrites when the bytes differ), backs up a different existing file first, makes the
    directory. `home` overrides the opencode config dir (tests, --codex-home reuse)."""
    base = Path(home) if home is not None else opencode_config_home()
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(time.time() if now is None else now))
    plugin_dir = base / "plugin"
    path = plugin_dir / OPENCODE_PLUGIN_NAME
    want = OPENCODE_PLUGIN_JS
    if path.exists():
        try:
            have = path.read_text(encoding="utf-8")
        except OSError as e:
            return 1, f"[error] {path} not readable ({e}); nothing changed."
        if have == want:
            return 0, f"plugin already installed in {path}"
        b = _backup(path, stamp)
        plugin_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(want, encoding="utf-8")
        return 0, (f"updated the voicehook plugin in {path}"
                   + (f" (backup {b.name})" if b else "") + "\n" + OPENCODE_HINT)
    plugin_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(want, encoding="utf-8")
    return 0, f"installed the voicehook plugin in {path}\n{OPENCODE_HINT}"


def print_snippet(agent: str) -> str:
    if agent == CODEX:
        return (f"# {codex_home() / 'hooks.json'}\n" + json.dumps(codex_hooks_snippet(), indent=2)
                + f"\n\n# {codex_home() / 'config.toml'} (notify only if you have none yet)\n"
                + codex_toml_snippet())
    if agent == OPENCODE:
        return f"# {opencode_config_home() / 'plugin' / OPENCODE_PLUGIN_NAME}\n{OPENCODE_PLUGIN_JS}"
    return json.dumps(vact.snippet(vact.hook_bin()), indent=2)


def main(args: list[str]) -> int:
    """`voicehook-agent-hook codex <event>` / `codex-notify <json>` / `opencode <event>`."""
    cmd = args[0] if args else ""
    if cmd == "codex-notify":
        with vact._deadline(vact.RELAY_HOOK_TIMEOUT):
            codex_notify(args[-1] if len(args) > 1 else "")
        return 0
    action = args[1] if len(args) > 1 else ""
    out = ""
    with vact._deadline(vact.RELAY_HOOK_TIMEOUT):
        try:
            raw = sys.stdin.read()
        except Exception:  # noqa: BLE001
            raw = ""
        out = opencode_hook(action, raw) if cmd == OPENCODE else codex_hook(action, raw)
    if not out and cmd != OPENCODE and action == "stop":
        out = "{}"
    if out:
        print(out, flush=True)
    return 0
