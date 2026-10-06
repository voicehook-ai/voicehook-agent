"""Self-update (0.12.0): no agent runs an outdated CLI unnoticed.

The server names `cli_min` + `cli_latest` in every operator join answer
(`GET /api/token?invite=1`, `POST /api/bridge/join`) and answers 426 Upgrade
Required below `cli_min`. `join` then updates the CLI in place and restarts
itself with the same arguments (os.execv), at most once per join (env marker).

    uv tool install   -> uv tool upgrade voicehook-agent
    anything else     -> python -m pip install --upgrade [--user] git+...
                         (+ --break-system-packages on a PEP 668 system python)

Off: `join --no-self-update` or VOICEHOOK_NO_SELF_UPDATE=1.
stdlib only, every side effect (subprocess, exec, env) is injectable for tests.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import sysconfig
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

REPO = "git+https://github.com/voicehook-ai/voicehook-agent"
PACKAGE = "voicehook-agent"
ENV_OPT_OUT = "VOICEHOOK_NO_SELF_UPDATE"
ENV_MARKER = "VOICEHOOK_SELF_UPDATED"  # set on the restarted join: value = version before
EXIT_OUTDATED = 7                       # join: CLI below the server's cli_min, update failed
UPGRADE_TIMEOUT_S = 180.0

UPGRADE_UV = f"uv tool upgrade {PACKAGE}"
UPGRADE_UV_FORCE = f"uv tool install --force {REPO}"
UPGRADE_PIP = f"pip install --upgrade --user {REPO}"

_VER_RX = re.compile(r"^\s*v?(\d+)\.(\d+)(?:\.(\d+))?")


def parse_version(v: str | None) -> tuple[int, int, int] | None:
    """'0.11.0' -> (0, 11, 0); '0.12' -> (0, 12, 0); suffixes (.dev1) are ignored."""
    m = _VER_RX.match(v or "")
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)


def is_newer(candidate: str | None, current: str | None) -> bool:
    """True only when both parse and candidate > current (numeric, not lexical)."""
    a, b = parse_version(candidate), parse_version(current)
    return a is not None and b is not None and a > b


def opted_out(flag: bool = False, env: Mapping[str, str] | None = None) -> bool:
    env = os.environ if env is None else env
    return bool(flag) or (env.get(ENV_OPT_OUT, "").strip().lower() in ("1", "true", "yes", "on"))


def already_restarted(env: Mapping[str, str] | None = None) -> bool:
    env = os.environ if env is None else env
    return bool(env.get(ENV_MARKER))


def decide(latest: str | None, current: str, *, opt_out: bool, restarted: bool) -> str:
    """'update' or why not: 'current' | 'opt-out' | 'restarted' (the loop guard)."""
    if not is_newer(latest, current):
        return "current"
    if opt_out:
        return "opt-out"
    if restarted:
        return "restarted"
    return "update"


# --------------------------------------------------------------------------- #
# how was I installed?
# --------------------------------------------------------------------------- #
def _in_venv(prefix: str, base_prefix: str) -> bool:
    return os.path.realpath(prefix) != os.path.realpath(base_prefix)


def install_method(prefix: str | None = None) -> str:
    """'uv' for a `uv tool install` venv (uv-receipt.toml or .../uv/tools/...), else 'pip'."""
    prefix = prefix or sys.prefix
    p = Path(prefix)
    if (p / "uv-receipt.toml").exists() or "/uv/tools/" in p.as_posix() + "/":
        return "uv"
    return "pip"


def externally_managed(stdlib: str | None = None) -> bool:
    """PEP 668: a system python marks itself with EXTERNALLY-MANAGED next to the stdlib."""
    stdlib = stdlib or sysconfig.get_path("stdlib") or ""
    return bool(stdlib) and (Path(stdlib) / "EXTERNALLY-MANAGED").exists()


def upgrade_command(method: str | None = None, *, which: Callable[[str], str | None] = shutil.which,
                    executable: str | None = None, prefix: str | None = None,
                    base_prefix: str | None = None, pep668: bool | None = None) -> list[str] | None:
    """The upgrade argv for this install, or None when it cannot run (uv venv without uv)."""
    prefix = prefix or sys.prefix
    base_prefix = base_prefix or sys.base_prefix
    method = method or install_method(prefix)
    if method == "uv":
        uv = which("uv")
        return [uv, "tool", "upgrade", PACKAGE] if uv else None
    cmd = [executable or sys.executable, "-m", "pip", "install", "--upgrade", "--quiet"]
    if not _in_venv(prefix, base_prefix):
        cmd.append("--user")
        if externally_managed() if pep668 is None else pep668:
            cmd.append("--break-system-packages")
    cmd.append(REPO)
    return cmd


def manual_hint() -> str:
    return (f"update by hand: {UPGRADE_UV}  (or {UPGRADE_UV_FORCE}; without uv: {UPGRADE_PIP}, "
            "add --break-system-packages if pip refuses with PEP 668)")


def run_upgrade(cmd: Sequence[str] | None, *,
                runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
                timeout: float = UPGRADE_TIMEOUT_S,
                warn: Callable[[str], None] | None = None) -> bool:
    """Run the upgrade; True on exit 0. Never raises: a failed update only warns."""
    warn = warn or (lambda m: print(m, file=sys.stderr, flush=True))
    if not cmd:
        warn(f"[warn] self-update: no updater found (uv missing); {manual_hint()}")
        return False
    try:
        r = runner(list(cmd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                   text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as e:
        warn(f"[warn] self-update failed ({type(e).__name__}); {manual_hint()}")
        return False
    if r.returncode != 0:
        tail = " | ".join((r.stdout or "").strip().splitlines()[-3:])[:300]
        warn(f"[warn] self-update failed (exit {r.returncode}: {tail}); {manual_hint()}")
        return False
    return True


# --------------------------------------------------------------------------- #
# restart
# --------------------------------------------------------------------------- #
def restart_argv(argv: Sequence[str], *, identity: str | None = None,
                 which: Callable[[str], str | None] = shutil.which) -> list[str]:
    """argv for the restarted join: same arguments, same identity (same session dir +
    LiveKit identity), started through the freshly installed entry point."""
    args = list(argv[1:])
    if identity and "--identity" not in args and not any(a.startswith("--identity=") for a in args):
        args += ["--identity", identity]
    exe = argv[0] if argv else ""
    if exe and os.sep in exe and os.access(exe, os.X_OK) and not exe.endswith(".py"):
        return [exe, *args]
    found = which(PACKAGE)
    if found:
        return [found, *args]
    return [sys.executable, "-c", "from voicehook_agent.cli import main; main()", *args]


def restart(argv: Sequence[str], current: str, *, identity: str | None = None,
            execve: Callable[..., None] = os.execve,
            env: Mapping[str, str] | None = None) -> None:
    """Replace this process with the updated CLI. Sets the loop-guard marker."""
    new_env = dict(os.environ if env is None else env)
    new_env[ENV_MARKER] = current
    args = restart_argv(argv, identity=identity)
    sys.stdout.flush()
    sys.stderr.flush()
    execve(args[0], args, new_env)


# --------------------------------------------------------------------------- #
# last versions seen from the server (for `--version` without a network call)
# --------------------------------------------------------------------------- #
def cache_path(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    base = env.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "voicehook-agent" / "versions.json"


def remember(info: Mapping, env: Mapping[str, str] | None = None) -> None:
    """Store cli_min / cli_latest of the last server answer. Best effort."""
    data = {k: info.get(k) for k in ("cli_min", "cli_latest")
            if isinstance(info.get(k), str) and parse_version(info.get(k))}
    if not data:
        return
    try:
        p = cache_path(env)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass


def cached(env: Mapping[str, str] | None = None) -> dict:
    try:
        d = json.loads(cache_path(env).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def version_text(current: str, env: Mapping[str, str] | None = None) -> str:
    """`voicehook-agent --version`: the version, plus a hint when the last server
    answer named a newer one."""
    latest = cached(env).get("cli_latest")
    line = f"voicehook-agent {current}"
    if is_newer(latest, current):
        line += f"\nneue Version verfügbar: {latest} (voicehook-agent self-update)"
    return line
