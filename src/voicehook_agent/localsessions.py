"""Other agent sessions on this machine, right after `join` (0.21.4).

Oliver 20.10.2026: the operator should know at once which other Claude Code / Codex sessions
run on this machine and offer to bring them into the call ("Soll ich die anderen N Sessions
hier einbinden, damit sie ihren Status in den Call bringen?"). On yes it runs the existing
setup: `voicehook-agent hook install` (Claude Code, global settings.json) and
`voicehook-agent hook install --agent codex` (Codex); from then on every session reports
through the multi-session relay (relay.json, written by this join).

Detection (no network, best effort, never raises):
* running agent processes (`ps`): a `claude` binary, Claude Code's `cli.js`, a `codex`
  binary; the join's own ancestors (the joining session) are left out. Per session only the
  basename of its working directory (no paths, no arguments);
* the multi-session registry (relay-state.json): labels of sessions whose hooks reported in
  the last REGISTRY_RECENT_S (they are already in the call);
* are our hooks installed (Claude Code settings.json, Codex hooks.json)?

Claude Code reads hooks at session start: an already running session takes a newly installed
hook after `/hooks` (review) or a restart; the hint says so.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from . import activity as vact

MAX_LISTED = 12
REGISTRY_RECENT_S = 7200.0
ENV_OFF = "VOICEHOOK_SESSIONS_HINT"
CLAUDE, CODEX = "claude", "codex"
INSTALL_CLAUDE = "voicehook-agent hook install"
INSTALL_CODEX = "voicehook-agent hook install --agent codex"


def _off(env=None) -> bool:
    env = os.environ if env is None else env
    return str(env.get(ENV_OFF) or "").strip().lower() in ("0", "false", "no", "off")


def parse_ps(text: str) -> list[tuple[int, int, str]]:
    """`ps -axo pid=,ppid=,args=` lines -> [(pid, ppid, args)]."""
    out = []
    for line in str(text or "").splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        out.append((int(parts[0]), int(parts[1]), parts[2] if len(parts) > 2 else ""))
    return out


def agent_kind(args: str) -> str:
    """claude | codex | '' for one process command line."""
    a = str(args or "")
    if not a:
        return ""
    # the executable may contain spaces (".../Application Support/Claude/claude-code/.../claude"):
    # everything before the first option is the program
    exe = a.split(" -", 1)[0].strip()
    first = (exe.rsplit("/", 1)[-1] if "/" in exe else exe).split()[0] if exe else ""
    if first == "claude" or "@anthropic-ai/claude-code" in a or "/claude-code/cli.js" in a:
        return CLAUDE
    if first == "codex" or "@openai/codex" in a:
        return CODEX
    return ""


def ancestors(procs: list[tuple[int, int, str]], pid: int) -> set[int]:
    parent = {p: pp for p, pp, _ in procs}
    out, cur = set(), pid
    while cur > 1 and cur not in out:
        out.add(cur)
        cur = parent.get(cur, 0)
    return out


def agent_sessions(procs: list[tuple[int, int, str]], own_pid: int) -> list[dict]:
    """Agent session processes except the join's own ancestors and agent children of an
    agent process (one session = its top agent process)."""
    mine = ancestors(procs, own_pid)
    kinds = {p: agent_kind(a) for p, _, a in procs}
    out = []
    for pid, ppid, _args in procs:
        kind = kinds.get(pid)
        if not kind or pid in mine:
            continue
        if kinds.get(ppid):  # a helper process of an agent (e.g. claude spawning claude)
            continue
        out.append({"pid": pid, "agent": kind})
    return out


def _cwd_name(pid: int) -> str:
    try:
        return Path(os.readlink(f"/proc/{pid}/cwd")).name
    except OSError:
        pass
    try:
        r = subprocess.run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"], capture_output=True,
                           text=True, timeout=2, check=False)
        for line in r.stdout.splitlines():
            if line.startswith("n/"):
                return Path(line[1:]).name
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


def _ps() -> str:
    try:
        return subprocess.run(["ps", "-axo", "pid=,ppid=,args="], capture_output=True, text=True,
                              timeout=3, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def claude_hooks_installed(path: Path | None = None) -> bool:
    try:
        data = json.loads((path or vact.default_settings_path()).read_text(encoding="utf-8") or "{}")
        hooks = data.get("hooks") if isinstance(data, dict) else None
    except (OSError, ValueError):
        return False
    if not isinstance(hooks, dict):
        return False
    return all(isinstance(hooks.get(ev), list) and vact._installed(hooks[ev], ev)
               for ev in ("PreToolUse", "PostToolUse", "Stop"))


def codex_hooks_installed(home: Path | None = None) -> bool:
    from . import agents
    try:
        data = json.loads(((home or agents.codex_home()) / "hooks.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    hooks = data.get("hooks") if isinstance(data, dict) else None
    if not isinstance(hooks, dict):
        return False
    return any(agents._ours(h.get("command"), "post-tool-use") for e in hooks.get("PostToolUse") or []
               if isinstance(e, dict) for h in e.get("hooks") or [] if isinstance(h, dict))


def registry_labels(now: float | None = None) -> list[str]:
    """Labels of sessions that reported through the relay recently (already in the call)."""
    from . import multisession as vmulti
    t = time.time() if now is None else now
    try:
        st = json.loads((vact.state_root() / vmulti.STATE_NAME).read_text(encoding="utf-8"))
        sessions = st.get("sessions") if isinstance(st, dict) else {}
    except (OSError, ValueError):
        return []
    out = []
    for me in (sessions or {}).values():
        if isinstance(me, dict) and me.get("label") and t - float(me.get("at") or 0) < REGISTRY_RECENT_S:
            out.append(str(me["label"]))
    return sorted(set(out))


def hint(sessions: list[dict], claude_ok: bool, codex_ok: bool,
         registered: list[str] | None = None) -> dict | None:
    """The `_meta: sessions` event for the operator, or None (no other session)."""
    if not sessions:
        return None
    n = len(sessions)
    kinds = {s["agent"] for s in sessions}
    setup = []
    if CLAUDE in kinds and not claude_ok:
        setup.append(INSTALL_CLAUDE)
    if CODEX in kinds and not codex_ok:
        setup.append(INSTALL_CODEX)
    who = ", ".join(f"{s['agent']}: {s.get('name') or '?'}" for s in sessions[:MAX_LISTED])
    ask = (f"Soll ich die anderen {n} Sessions hier einbinden, damit sie ihren Status in den "
           "Call bringen?")
    if setup:
        text = (f"{n} weitere Agent-Session{'s' if n != 1 else ''} auf diesem Rechner ({who}). "
                f"Frag den Menschen: \"{ask}\" Bei Ja: {' und '.join(f'`{c}`' for c in setup)}"
                f"{' (Codex danach einmal /hooks vertrauen)' if INSTALL_CODEX in setup else ''}; "
                "laufende Claude-Code-Sessions übernehmen den Hook nach /hooks oder einem "
                "Neustart, dann melden sie Ziel, PR, Deploy, Fehler und Fragen in den Call.")
    else:
        text = (f"{n} weitere Agent-Session{'s' if n != 1 else ''} auf diesem Rechner ({who}); "
                "der Hook ist installiert, sie melden sich über den Relay selbst in den Call "
                "(eine erst nach Neustart oder /hooks, falls sie vor der Installation lief).")
    out = {"role": "system", "topic": "_meta", "_meta": "sessions", "count": n,
           "sessions": [{"agent": s["agent"], "name": s.get("name") or ""} for s in sessions[:MAX_LISTED]],
           "hooks": {"claude": claude_ok, "codex": codex_ok}, "text": text}
    if setup:
        out["ask"] = ask
        out["setup"] = setup
    if registered:
        out["reporting"] = registered[:MAX_LISTED]
    return out


def detect(own_pid: int | None = None, ps=_ps, cwd=_cwd_name) -> dict | None:
    """The hint for this machine right now (None: off, no other session). Never raises."""
    try:
        if _off():
            return None
        procs = parse_ps(ps())
        sessions = agent_sessions(procs, os.getpid() if own_pid is None else own_pid)
        for s in sessions[:MAX_LISTED]:
            name = vact._clean(cwd(s["pid"]))[:40].strip()
            if name:
                s["name"] = name
        return hint(sessions, claude_hooks_installed(), codex_hooks_installed(), registry_labels())
    except Exception:  # noqa: BLE001
        return None
