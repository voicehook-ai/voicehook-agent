"""Local control channel between a running `join` and one-shot commands.

`voicehook-agent join` opens a Unix socket in a per-room session directory.
The one-shot commands `say`, `next`, `leave` and `status` talk to it, so an
agent loop can run "say -> next -> say -> next" without a FIFO, without tmux
and without `sleep; tail` polling.

Wire format: the client sends ONE JSON line, the server answers with ONE JSON
line and closes. Requests:

    {"cmd": "say", "text": "...", "mode": "revise|overwrite|append"?}
    {"cmd": "next", "timeout": 60}
    {"cmd": "leave", "say": "..."?}
    {"cmd": "status"}

This module has no LiveKit dependency so it can be unit-tested in isolation.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import socket
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

SOCKET_NAME = "ctl.sock"
INFO_NAME = "session.json"


# --------------------------------------------------------------------------- #
# session directory
# --------------------------------------------------------------------------- #
def home_dir() -> Path:
    """Root for session dirs. Override with VOICEHOOK_AGENT_HOME."""
    env = os.environ.get("VOICEHOOK_AGENT_HOME")
    if env:
        return Path(env)
    return Path.home() / ".voicehook-agent"


def sessions_root() -> Path:
    return home_dir() / "sessions"


def session_dir(slug: str) -> Path:
    return sessions_root() / slug


# AF_UNIX paths are limited to ~104-108 bytes. Deep home dirs (or a long
# VOICEHOOK_AGENT_HOME) fall back to a short, deterministic path under /tmp, so
# both `join` and the one-shot commands compute the same socket path.
_MAX_SOCK_PATH = 100


def socket_path(dir_: Path) -> Path:
    """Control socket for a session dir (deterministic, length-safe)."""
    cand = dir_ / SOCKET_NAME
    if len(str(cand).encode("utf-8")) <= _MAX_SOCK_PATH:
        return cand
    digest = hashlib.sha256(str(dir_.resolve()).encode("utf-8")).hexdigest()[:16]
    uid = os.getuid() if hasattr(os, "getuid") else 0
    return Path("/tmp") / f"voicehook-agent-{uid}" / f"{digest}.sock"


def socket_alive(sock_path: Path, timeout: float = 1.0) -> bool:
    """True if something accepts connections on `sock_path`."""
    if not sock_path.exists():
        return False
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(str(sock_path))
        return True
    except OSError:
        return False
    finally:
        s.close()


def live_sessions() -> list[Path]:
    """Session dirs whose control socket answers."""
    root = sessions_root()
    if not root.is_dir():
        return []
    return sorted(d for d in root.iterdir() if d.is_dir() and socket_alive(socket_path(d)))


class SessionError(RuntimeError):
    pass


def resolve_socket(session: str | None, wait: float = 0.0,
                   poll: float = 0.2) -> Path:
    """Find the control socket of a running join.

    `session` may be a slug, a session dir or a socket path. Without it, the
    single live session is used; more than one is an error (pass --session).
    Waits up to `wait` seconds for the socket to appear, so `say` right after
    starting `join` in the background just works."""
    deadline = time.monotonic() + max(0.0, wait)
    while True:
        if session:
            p = Path(session)
            if p.name.endswith(".sock") or p.is_socket():
                cand = p
            elif p.is_dir():
                cand = socket_path(p)
            else:
                cand = socket_path(session_dir(session))
            if socket_alive(cand):
                return cand
            msg = f"no running join for session {session!r} ({cand})"
        else:
            live = live_sessions()
            if len(live) == 1:
                return socket_path(live[0])
            if len(live) > 1:
                names = ", ".join(d.name for d in live)
                raise SessionError(f"several joins running ({names}); pass --session <slug>")
            msg = f"no running join found under {sessions_root()}"
        if time.monotonic() >= deadline:
            raise SessionError(msg)
        time.sleep(poll)


def request(sock_path: Path, obj: dict, timeout: float | None = None) -> dict:
    """Send one JSON request, return the one JSON reply."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(str(sock_path))
        s.sendall((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    finally:
        s.close()
    line = buf.split(b"\n", 1)[0].decode("utf-8").strip()
    if not line:
        raise SessionError("join closed the connection without an answer (session ended?)")
    return json.loads(line)


# --------------------------------------------------------------------------- #
# event queue for `next`
# --------------------------------------------------------------------------- #
class EventQueue:
    """FIFO of events for `next`: finalized user turns, operator.revise and the
    final `ended` marker. Events arriving while nobody waits are kept, so the
    turn the user spoke between `say` and `next` is never lost."""

    def __init__(self) -> None:
        self._q: deque[dict] = deque()
        self._cond = asyncio.Condition()
        self.closed = False

    def __len__(self) -> int:
        return len(self._q)

    async def put(self, event: dict) -> None:
        async with self._cond:
            self._q.append(event)
            self._cond.notify_all()

    def put_nowait(self, event: dict) -> None:
        """Sync push from LiveKit callbacks (same loop)."""
        self._q.append(event)
        asyncio.get_running_loop().create_task(self._notify())

    async def _notify(self) -> None:
        async with self._cond:
            self._cond.notify_all()

    async def close(self) -> None:
        async with self._cond:
            self.closed = True
            self._cond.notify_all()

    async def get(self, timeout: float | None) -> dict | None:
        """Oldest event, or None on timeout. After close and once drained,
        returns {"type": "ended"} immediately."""
        async def _wait() -> dict:
            async with self._cond:
                while not self._q and not self.closed:
                    await self._cond.wait()
                if self._q:
                    return self._q.popleft()
                return {"type": "ended"}
        if timeout is not None and timeout <= 0:
            if self._q:
                return self._q.popleft()
            return {"type": "ended"} if self.closed else None
        try:
            return await asyncio.wait_for(_wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return None


# --------------------------------------------------------------------------- #
# idle watchdog (orphaned joins)
# --------------------------------------------------------------------------- #
@dataclass
class IdleWatchdog:
    """Tracks the last sign of life of the brain (say / next / stdin line).

    A `next` that is currently blocked counts as alive for its whole duration
    (`busy`). `timeout` <= 0 disables the watchdog."""

    timeout: float
    last: float = field(default_factory=time.monotonic)
    busy: int = 0

    def touch(self, now: float | None = None) -> None:
        self.last = now if now is not None else time.monotonic()

    def enter(self) -> None:
        self.busy += 1

    def leave(self, now: float | None = None) -> None:
        self.busy = max(0, self.busy - 1)
        self.touch(now)

    def idle_for(self, now: float | None = None) -> float:
        if self.busy:
            return 0.0
        now = now if now is not None else time.monotonic()
        return max(0.0, now - self.last)

    def expired(self, now: float | None = None) -> bool:
        return self.timeout > 0 and self.idle_for(now) >= self.timeout


# --------------------------------------------------------------------------- #
# server
# --------------------------------------------------------------------------- #
Handler = Callable[[dict], Awaitable[dict]]


class ControlServer:
    """asyncio Unix-socket server; one JSON request, one JSON reply."""

    def __init__(self, sock_path: Path, handler: Handler) -> None:
        self.sock_path = sock_path
        self.handler = handler
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        self.sock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.sock_path.parent, 0o700)
        except OSError:
            pass
        if self.sock_path.exists():
            if socket_alive(self.sock_path):
                raise SessionError(
                    f"a join is already running for this room ({self.sock_path}); "
                    "use say/next/leave against it")
            self.sock_path.unlink()
        self._server = await asyncio.start_unix_server(self._serve, path=str(self.sock_path))
        try:
            os.chmod(self.sock_path, 0o600)
        except OSError:
            pass

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            raw = await reader.readline()
            try:
                req = json.loads(raw.decode("utf-8") or "{}")
                if not isinstance(req, dict):
                    raise ValueError("request must be a JSON object")
            except ValueError as e:
                reply = {"ok": False, "error": f"bad request: {e}"}
            else:
                try:
                    reply = await self.handler(req)
                except Exception as e:  # noqa: BLE001
                    reply = {"ok": False, "error": repr(e)}
            writer.write((json.dumps(reply, ensure_ascii=False) + "\n").encode("utf-8"))
            await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:  # noqa: BLE001
                pass
            self._server = None
        try:
            self.sock_path.unlink()
        except OSError:
            pass


def write_info(dir_: Path, info: dict) -> None:
    try:
        (dir_ / INFO_NAME).write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def remove_info(dir_: Path) -> None:
    try:
        (dir_ / INFO_NAME).unlink()
    except OSError:
        pass
