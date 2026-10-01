"""Local control channel between a running `join` and one-shot commands.

`voicehook-agent join` opens a Unix socket in a per-process session directory
(`sessions/<slug>/<identity>/`), so several joins into the same room from the
same machine do not collide.
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
import re
import socket
import stat
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


def _safe_name(name: str) -> str:
    """One path segment: keep [A-Za-z0-9._-], replace the rest."""
    out = re.sub(r"[^A-Za-z0-9._-]", "_", name).strip(".") or "_"
    return out[:80]


def room_dir(slug: str) -> Path:
    """Directory holding all joins of one room on this machine."""
    return sessions_root() / _safe_name(slug)


def session_dir(slug: str, identity: str) -> Path:
    """Per-process session dir: one join = one (slug, identity) pair."""
    return room_dir(slug) / _safe_name(identity)


# AF_UNIX paths are limited to ~104-108 bytes. Deep home dirs (or a long
# VOICEHOOK_AGENT_HOME) fall back to a short, deterministic path under /tmp, so
# both `join` and the one-shot commands compute the same socket path. That
# directory is shared with other local users, so it is only used when it is a
# real directory (no symlink) owned by us with mode 0700 (check_private_dir).
_MAX_SOCK_PATH = 100
FALLBACK_ROOT = Path("/tmp")


def _uid() -> int:
    return os.getuid() if hasattr(os, "getuid") else 0


def fallback_dir() -> Path:
    return FALLBACK_ROOT / f"voicehook-agent-{_uid()}"


def check_private_dir(dir_: Path) -> None:
    """Raise SessionError unless `dir_` is a real dir, owned by us, mode 0700
    (no group/other bits). lstat, so a symlink is never followed."""
    try:
        st = os.lstat(dir_)
    except OSError as e:
        raise SessionError(f"socket dir {dir_} not usable: {e}") from e
    if stat.S_ISLNK(st.st_mode):
        raise SessionError(f"socket dir {dir_} is a symlink; refusing to use it")
    if not stat.S_ISDIR(st.st_mode):
        raise SessionError(f"socket dir {dir_} is not a directory; refusing to use it")
    if hasattr(os, "getuid") and st.st_uid != os.getuid():
        raise SessionError(
            f"socket dir {dir_} belongs to uid {st.st_uid}, not to you "
            f"(uid {os.getuid()}); refusing to use it. Remove it or set a "
            "shorter VOICEHOOK_AGENT_HOME")
    if st.st_mode & 0o077:
        raise SessionError(
            f"socket dir {dir_} has mode {stat.S_IMODE(st.st_mode):o}, expected 700; "
            f"refusing to use it (chmod 700 {dir_})")


def is_fallback(sock_path: Path) -> bool:
    return sock_path.parent == fallback_dir()


def socket_path(dir_: Path) -> Path:
    """Control socket for a session dir (deterministic, length-safe)."""
    cand = dir_ / SOCKET_NAME
    if len(str(cand).encode("utf-8")) <= _MAX_SOCK_PATH:
        return cand
    digest = hashlib.sha256(str(dir_.resolve()).encode("utf-8")).hexdigest()[:16]
    return fallback_dir() / f"{digest}.sock"


def prepare_socket_dir(sock_path: Path) -> None:
    """Create the socket's directory for the server. The /tmp fallback must
    pass check_private_dir (else SessionError); a dir under our own home is
    created/tightened to 0700."""
    parent = sock_path.parent
    if is_fallback(sock_path):
        try:
            parent.mkdir(mode=0o700)
        except FileExistsError:
            pass
        check_private_dir(parent)
        return
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(parent, 0o700)
    except OSError:
        pass


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
    """Session dirs (sessions/<slug>/<identity>) whose control socket answers."""
    root = sessions_root()
    if not root.is_dir():
        return []
    out = []
    for room in sorted(root.iterdir()):
        if not room.is_dir():
            continue
        for d in sorted(room.iterdir()):
            if d.is_dir() and socket_alive(socket_path(d)):
                out.append(d)
    return out


class SessionError(RuntimeError):
    pass


class SessionBusy(SessionError):
    """A live join already owns this (slug, identity) socket."""


def _label(d: Path) -> str:
    return f"{d.parent.name}/{d.name}"


def _pick(live: list[Path], scope: str) -> Path:
    if len(live) == 1:
        return live[0]
    names = ", ".join(_label(d) for d in live)
    raise SessionError(f"several joins running {scope}({names}); "
                       "pass --session <slug>/<identity>")


def _checked(sock: Path) -> Path:
    """Client side of the /tmp fallback check: never talk to a socket in a
    directory another user could have planted."""
    if is_fallback(sock):
        check_private_dir(sock.parent)
    return sock


def resolve_socket(session: str | None, wait: float = 0.0,
                   poll: float = 0.2) -> Path:
    """Find the control socket of a running join.

    `session` may be `<slug>/<identity>`, a slug, a session dir or a socket
    path. A slug alone works when only one join runs in that room. Without
    `session`, the single live join is used; more than one is an error that
    lists them (pass --session). Waits up to `wait` seconds for the socket to
    appear, so `say` right after starting `join` in the background just works."""
    deadline = time.monotonic() + max(0.0, wait)
    while True:
        if session:
            p = Path(session)
            live: list[Path] = []
            if p.name.endswith(".sock") or p.is_socket():
                cand: Path | None = p
            elif p.is_dir() and (p / SOCKET_NAME).exists():
                cand = socket_path(p)
            elif p.is_dir():
                cand = None
                live = [d for d in sorted(p.iterdir())
                        if d.is_dir() and socket_alive(socket_path(d))]
            elif "/" in session.strip("/") and not p.is_absolute():
                slug, ident = session.strip("/").split("/", 1)
                cand = socket_path(session_dir(slug, ident))
            else:
                cand = None
                rd = room_dir(session)
                if rd.is_dir():
                    live = [d for d in sorted(rd.iterdir())
                            if d.is_dir() and socket_alive(socket_path(d))]
            if cand is not None:
                if socket_alive(cand):
                    return _checked(cand)
                msg = f"no running join for session {session!r} ({cand})"
            elif live:
                return _checked(socket_path(_pick(live, f"in room {session!r} ")))
            else:
                msg = f"no running join for session {session!r}"
        else:
            live = live_sessions()
            if live:
                return _checked(socket_path(_pick(live, "")))
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
    turn the user spoke between `say` and `next` is never lost.

    The queue only starts collecting once it is `arm()`ed (the join arms it on
    the first `say` or `next` over the control socket). A FIFO/stdout-only
    agent therefore never piles up turns, and an agent switching to `next`
    late does not get minutes-old turns as if they were fresh. It is also
    bounded (`maxlen`, oldest dropped, counted in `dropped`)."""

    MAXLEN = 200

    def __init__(self, maxlen: int | None = None, armed: bool = False) -> None:
        self._q: deque[dict] = deque(maxlen=maxlen or self.MAXLEN)
        self._cond = asyncio.Condition()
        self.closed = False
        self.armed = armed
        self.dropped = 0

    def __len__(self) -> int:
        return len(self._q)

    def arm(self) -> None:
        self.armed = True

    def _append(self, event: dict) -> bool:
        if not self.armed:
            return False
        if len(self._q) == self._q.maxlen:
            self.dropped += 1
        self._q.append(event)
        return True

    async def put(self, event: dict) -> None:
        async with self._cond:
            if self._append(event):
                self._cond.notify_all()

    def put_nowait(self, event: dict) -> None:
        """Sync push from LiveKit callbacks (same loop)."""
        if self._append(event):
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
        returns {"type": "ended"} immediately. Arms the queue."""
        self.arm()
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
    """asyncio Unix-socket server; one JSON request, one JSON reply.

    A client must send its request line within `read_timeout` seconds.
    close() drops open client connections and returns within
    `close_timeout` seconds even if a handler hangs."""

    def __init__(self, sock_path: Path, handler: Handler,
                 read_timeout: float = 5.0, close_timeout: float = 2.0) -> None:
        self.sock_path = sock_path
        self.handler = handler
        self.read_timeout = read_timeout
        self.close_timeout = close_timeout
        self._server: asyncio.AbstractServer | None = None
        self._clients: set[asyncio.StreamWriter] = set()
        self._tasks: set[asyncio.Task] = set()

    async def start(self) -> None:
        prepare_socket_dir(self.sock_path)
        if self.sock_path.exists():
            if socket_alive(self.sock_path):
                raise SessionBusy(
                    f"a join with this identity is already running in this room "
                    f"({self.sock_path})")
            self.sock_path.unlink()
        self._server = await asyncio.start_unix_server(self._serve, path=str(self.sock_path))
        try:
            os.chmod(self.sock_path, 0o600)
        except OSError:
            pass

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        self._clients.add(writer)
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=self.read_timeout)
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
        except (ConnectionError, asyncio.TimeoutError, asyncio.CancelledError):
            pass
        finally:
            self._clients.discard(writer)
            if task is not None:
                self._tasks.discard(task)
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            # Give in-flight replies a moment, then drop whatever is left
            # (a client that connected but never sent, a say stuck waiting).
            pending = [t for t in self._tasks if not t.done()]
            if pending:
                await asyncio.wait(pending, timeout=self.close_timeout / 2)
            for w in list(self._clients):
                try:
                    w.transport.abort()
                except Exception:  # noqa: BLE001
                    pass
            for t in list(self._tasks):
                t.cancel()
            try:
                await asyncio.wait_for(self._server.wait_closed(), timeout=self.close_timeout)
            except (asyncio.TimeoutError, Exception):  # noqa: BLE001
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
