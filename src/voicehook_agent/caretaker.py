"""Caretaker daemon (0.34.0): the watcher ("Hausmeister") active in EVERY call of the account
owner, no per-call start.

Prerequisite: `voicehook-agent connect` (once, approved in the browser, see device.py). Then
`voicehook-agent caretaker` (run by systemd --user / launchd after `caretaker install`):

* long-polls GET /api/device/calls?wait=25 and detects the owner's live calls itself;
* for the NEWEST call with the owner present it asks for a caretaker credential and runs the
  existing Watcher (watch.py) bound to that credential: board sentences, activity lines,
  Kanban, the summary card "N Sessions arbeiten gerade parallel ... alle abonnieren?";
* on Ja it asks for a helper credential per subscribed session and writes it as
  helpers/<label>.json (multisession.write_helper, owner_session = the session), so the
  session's own CLI commands (VOICEHOOK_SESSION_LABEL=<label>) land in this call;
* when the call is no longer listed it stops the watcher (boards cleared) and removes the
  helper entries it wrote for that call.

Design choice: ONE watcher at a time, for the newest call (not one per call). The watcher's
machine state (watch-subscribed.json, watch-boards.json, watch-self.json) is keyed by a
single call and a local session reports into one call only; parallel watchers would
overwrite each other's state. A newer call replaces the older one (like 0.33.3 did for the
classic watcher). Single thread: the long poll doubles as the loop clock.

Invisible: it never posts its own status; the only senders are session labels (and the
existing `Wächter` proposal card). One daemon per machine (flock caretaker.lock); the watcher
also holds watch.lock while a call is served, so a classic watcher is never started twice.
Log ~/.voicehook/caretaker.log: metadata only (no token, no label text beyond counts), rotated.

stdlib only.
"""
from __future__ import annotations

import json
import os
import plistlib
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import activity as vact
from . import device as vdev
from . import multisession as vmulti
from . import relayops as vrelayops
from . import watch as W

LOG_NAME = "caretaker.log"
LOG_MAX_BYTES = 256 * 1024
IDLE_S = 30.0               # no device.json: look again every 30 s (connect after install works)
ERROR_BACKOFF_MAX_S = 300.0
RETRY_START_S = 60.0
SOON_S = 20.0               # 409: nobody in the call yet
BIND_BACKOFF_S = 60.0
RETRY_MAX_S = 600.0
RENEW_BEFORE_S = 600.0      # renew the caretaker / helper credential this long before it ends
HELPER_BATCH = 12

UNIT_NAME = "voicehook-caretaker.service"
PLIST_LABEL = "ai.voicehook.caretaker"
KEEP_ENV = ("VOICEHOOK_API", "VOICEHOOK_STATE_DIR", "CLAUDE_CONFIG_DIR", "CODEX_HOME",
            "VOICEHOOK_WATCH_MODEL_CMD", "VOICEHOOK_WATCH_NEVER", "VOICEHOOK_WATCH_ALL")


def log(event: str, **kv: object) -> None:
    """One line `HH:MM:SS event k=v` in caretaker.log (size-rotated). Metadata only."""
    try:
        path = vact.state_root() / LOG_NAME
        vact._private_dir(vact.state_root())
        try:
            if path.stat().st_size > LOG_MAX_BYTES:
                os.replace(path, path.with_name(LOG_NAME + ".1"))
        except OSError:
            pass
        parts = [time.strftime("%H:%M:%S"), event]
        parts += [f"{k}={str(v).replace(chr(10), ' ')[:60]}" for k, v in kv.items()]
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            os.write(fd, (" ".join(parts) + "\n").encode("utf-8"))
        finally:
            os.close(fd)
    except Exception:  # noqa: BLE001, S110
        pass


# --------------------------------------------------------------------------- #
# the daemon
# --------------------------------------------------------------------------- #
class Caretaker:
    def __init__(self, *, clock=time.time, http=vdev.http_request, watcher_factory=None,
                 lock_fn=W.acquire_lock, wait_s: int = vdev.CALLS_WAIT_S) -> None:
        self.clock = clock
        self.http = http
        self.wait_s = wait_s
        self.lock_fn = lock_fn
        self.watcher_factory = watcher_factory or (
            lambda **kw: W.Watcher(**kw))
        self.room = ""
        self.rel: dict | None = None
        self.watcher = None
        self.lock_fd = None
        self.next_tick = 0.0
        self.fails = 0
        self.retry_at: dict[str, float] = {}
        self.retry_gap: dict[str, float] = {}
        self.bind_off_until = 0.0

    # ----- credential of the current call -----
    def _rel_from(self, room: str, got: dict, now: float) -> dict:
        exp = vdev.ts(got.get("expires_at")) or now + 3600.0
        return {"url": str(got["url"]), "token": str(got["token"]), "room": room, "call_id": room,
                "expires_at": exp, "written_at": now, "owner_pid": 0, "owner_session": ""}

    def _cred(self, now: float | None = None) -> dict | None:
        return self.rel

    # ----- start / stop one call -----
    def start_call(self, dev: dict, call: dict, now: float) -> bool:
        room = str(call["room"])
        fd = self.lock_fn()
        if fd is None:
            log("start", room_set=1, why="watch-lock-busy")
            self._retry(room, now)
            return False
        try:
            got = vdev.caretaker_credential(dev, room, self.http)
        except vdev.DeviceError as e:
            os.close(fd)
            if e.status == 401:
                self.revoked()
            log("start", why="credential-failed", status=e.status)
            # 409: no human in the call yet, 429: join limits. The call stays listed: later again
            self._retry(room, now, SOON_S if e.status == 409 else RETRY_START_S)
            return False
        self.lock_fd = fd
        self.room = room
        self.rel = self._rel_from(room, got, now)
        self.watcher = self.watcher_factory(cred_fn=lambda t=None: self.rel, bind_fn=self.bind,
                                            clock=self.clock)
        self.next_tick = 0.0
        self.retry_gap.pop(room, None)
        log("start", why="call", expires_in=int(self.rel["expires_at"] - now))
        return True

    def end_call(self, why: str) -> None:
        w, room = self.watcher, self.room
        self.watcher = None
        if w is not None:
            try:
                self.rel = None            # shutdown() then sees "no call": opt-ins end too
                w.shutdown()
            except Exception:  # noqa: BLE001, S110
                pass
        self.rel = None
        self.room = ""
        self.remove_helpers(room)
        if self.lock_fd is not None:
            try:
                os.close(self.lock_fd)
            except OSError:
                pass
            self.lock_fd = None
        log("end", why=why)

    def _retry(self, room: str, now: float, start: float = RETRY_START_S) -> None:
        gap = min(RETRY_MAX_S, self.retry_gap.get(room, start / 2) * 2)
        self.retry_gap[room] = gap
        self.retry_at[room] = now + gap

    def revoked(self) -> None:
        """401 on the device token: the connection was revoked. Forget it, idle."""
        vdev.clear_device()
        log("revoked", note="device.json removed, run connect again")

    # ----- credential renewal -----
    def renew(self, dev: dict, now: float, force: bool = False) -> bool:
        """New caretaker credential for the current call (before it ends, or `force`: the
        server forgot it, e.g. 410 after a restart while the call is still listed)."""
        if self.rel is None or (not force and self.rel["expires_at"] - now > RENEW_BEFORE_S):
            return False
        try:
            got = vdev.caretaker_credential(dev, self.room, self.http)
        except vdev.DeviceError as e:
            if e.status == 401:
                self.revoked()
            log("renew", ok=0, status=e.status)
            return False
        self.rel = self._rel_from(self.room, got, now)
        log("renew", ok=1)
        return True

    # ----- helper credentials for subscribed sessions -----
    def _registry_label(self, tp: str) -> str:
        try:
            st = json.loads((vact.state_root() / vmulti.STATE_NAME).read_text(encoding="utf-8"))
            for e in (st.get("sessions") or {}).values():
                if isinstance(e, dict) and tp and e.get("tp") == tp and e.get("label"):
                    return str(e["label"])
        except (OSError, ValueError, AttributeError):
            pass
        return ""

    def _fresh_helper(self, label: str, room: str, now: float) -> bool:
        path = vmulti.helpers_dir() / f"{vmulti.helper_key(label)}.json"
        e = vmulti._read_raw(path)
        return bool(e and e.get("room") == room and float(e.get("expires_at") or 0) - now > RENEW_BEFORE_S)

    def bind(self, rel: dict, items: list[dict]) -> None:
        """Helper credential per subscribed session -> helpers/<label>.json. Idempotent: only
        sessions without a fresh entry for this call cost a request."""
        dev = vdev.load_device()
        now = self.clock()
        if dev is None or self.room == "" or now < self.bind_off_until:
            return
        want: dict[str, dict] = {}
        for it in items:
            label = self._registry_label(str(it.get("tp") or "")) or str(it.get("label") or "")
            try:
                label = vrelayops.check_from(label)
            except vrelayops.RelayError:
                continue
            if label and not self._fresh_helper(label, self.room, now):
                want[label] = it
        labels = list(want)
        for i in range(0, len(labels), HELPER_BATCH):
            chunk = labels[i:i + HELPER_BATCH]
            try:
                got = vdev.helper_credentials(dev, self.room, chunk, self.http)
            except vdev.DeviceError as e:
                if e.status == 401:
                    self.revoked()
                log("bind", ok=0, count=len(chunk), status=e.status)
                if e.status in (409, 429, 410):    # nobody yet / limits / credential forgotten
                    self.bind_off_until = now + BIND_BACKOFF_S
                    break
                continue
            n = 0
            for h in got:
                label = str(h["label"])
                if label not in want:
                    continue
                ttl = (vdev.ts(h.get("expires_at")) or now + 3600.0) - now
                try:
                    vmulti.write_helper(label, str(h["url"]), str(h["token"]), self.room, ttl,
                                        owner_session=str(want[label].get("key") or ""),
                                        label=label, now=now)
                    n += 1
                except (ValueError, OSError):
                    continue
            log("bind", ok=1, count=n)

    def remove_helpers(self, room: str) -> None:
        """Helper entries written for this call end with it."""
        if not room:
            return
        for p, e in vmulti._helper_entries():
            if e.get("room") == room:
                try:
                    p.unlink()
                except OSError:
                    pass

    # ----- one loop pass -----
    def step(self) -> float:
        """One pass: poll the calls, start / switch / end the watcher, tick it when due.
        Returns the seconds to sleep before the next pass (0 = the long poll already waited)."""
        dev = vdev.load_device()
        now = self.clock()
        if dev is None:
            if self.watcher is not None:
                self.end_call("disconnected")
            return IDLE_S
        calls: list[dict] | None = None
        try:
            calls = vdev.list_calls(dev, self.wait_s, self.http)
            self.fails = 0
        except vdev.DeviceError as e:
            if e.status == 401:
                self.revoked()
                if self.watcher is not None:
                    self.end_call("revoked")
                return IDLE_S
            self.fails += 1
            log("calls", ok=0, status=e.status, fails=self.fails)
        now = self.clock()
        if calls is not None:
            live = {str(c["room"]) for c in calls}
            if self.room and self.room not in live:
                self.end_call("call-over")
            mine = sorted((c for c in calls if c.get("owner_present")),
                          key=lambda c: -vdev.ts(c.get("started_at")))
            newest = mine[0] if mine else None
            if newest is not None and str(newest["room"]) != self.room \
                    and now >= self.retry_at.get(str(newest["room"]), 0.0):
                if self.room:
                    self.end_call("newer-call")
                self.start_call(dev, newest, now)
        if self.watcher is not None:
            self.renew(dev, now)
            if now >= self.next_tick:
                self.tick(now)
        if calls is None:
            return min(ERROR_BACKOFF_MAX_S, 5.0 * (2 ** min(self.fails, 6)))
        return 0.0 if self.wait_s > 0 else 1.0

    def tick(self, now: float) -> None:
        try:
            res = self.watcher.tick()
        except Exception as e:  # noqa: BLE001  never die on a watcher bug
            self.watcher.fails += 1
            log("error", where="tick", type=type(e).__name__)
            res = W.OK
        if res == W.END:
            # 410 after a server restart: the credential is gone but the call may still be
            # listed (step() ends the call when /calls no longer lists it): ask for a new one
            dev = vdev.load_device()
            if dev is not None and self.renew(dev, now, force=True):
                self.watcher.over = []
                self.next_tick = now + W.TICK_S
                return
            room = self.room
            self.end_call("watcher-end")
            self._retry(room, now)     # still listed? try again later with a fresh credential
            return
        self.next_tick = now + self.watcher.delay()

    def run(self, sleep=time.sleep, should_stop=lambda: False) -> int:
        log("daemon", pid=os.getpid())
        try:
            while not should_stop():
                try:
                    d = self.step()
                except Exception as e:  # noqa: BLE001
                    self.fails += 1
                    log("error", where="step", type=type(e).__name__)
                    d = min(ERROR_BACKOFF_MAX_S, 5.0 * (2 ** min(self.fails, 6)))
                if d > 0:
                    sleep(d)
        finally:
            if self.watcher is not None:
                self.end_call("stop")
        return 0


def run_daemon() -> int:
    """`caretaker run` (what the unit runs): one per machine."""
    fd = vdev.acquire_daemon_lock()
    if fd is None:
        print("caretaker already running", file=sys.stderr, flush=True)
        return 0
    try:
        import signal
        signal.signal(signal.SIGTERM, lambda *_a: sys.exit(0))   # finally: boards cleared
    except (ImportError, ValueError, OSError):
        pass
    try:
        return Caretaker().run()
    finally:
        os.close(fd)


# --------------------------------------------------------------------------- #
# autostart: systemd --user (Linux), launchd (macOS)
# --------------------------------------------------------------------------- #
def exec_argv() -> list[str]:
    """The command line the unit runs: the installed `voicehook-agent`, else this interpreter
    with the source tree (like watch.child_argv)."""
    exe = shutil.which("voicehook-agent")
    if exe:
        return [exe, "caretaker", "run"]
    return [sys.executable, "-I", "-c", W._BOOT, str(Path(__file__).resolve().parents[1]),
            "caretaker", "run"]


def unit_env(env=None) -> dict[str, str]:
    """Plain, non-secret settings the daemon needs under a minimal service environment
    (PATH finds `claude`). Never a token."""
    env = os.environ if env is None else env
    out = {"PATH": str(env.get("PATH") or "/usr/local/bin:/usr/bin:/bin")}
    for k in KEEP_ENV:
        if env.get(k):
            out[k] = str(env[k])
    return out


def unit_path(home: Path | None = None) -> Path:
    return (home or Path.home()) / ".config" / "systemd" / "user" / UNIT_NAME


def plist_path(home: Path | None = None) -> Path:
    return (home or Path.home()) / "Library" / "LaunchAgents" / f"{PLIST_LABEL}.plist"


def unit_text(argv: list[str], env: dict[str, str]) -> str:
    def q(v: str) -> str:
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'
    lines = ["[Unit]", "Description=voicehook caretaker (Hausmeister): active in every call",
             "After=network-online.target", "Wants=network-online.target", "",
             "[Service]", "Type=simple",
             "ExecStart=" + " ".join(q(a) for a in argv),
             "Restart=always", "RestartSec=10"]
    lines += [f"Environment={q(f'{k}={v}')}" for k, v in env.items()]
    lines += ["", "[Install]", "WantedBy=default.target", ""]
    return "\n".join(lines)


def plist_bytes(argv: list[str], env: dict[str, str]) -> bytes:
    return plistlib.dumps({"Label": PLIST_LABEL, "ProgramArguments": argv, "RunAtLoad": True,
                           "KeepAlive": True, "EnvironmentVariables": env,
                           "StandardOutPath": os.devnull, "StandardErrorPath": os.devnull})


def _run(cmd: list[str], runner=subprocess.run) -> tuple[int, str]:
    try:
        r = runner(cmd, capture_output=True, text=True, timeout=30)
        return r.returncode, (r.stdout or "").strip()
    except (OSError, subprocess.SubprocessError):
        return 127, ""


def _write_text(path: Path, text: bytes | str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = text if isinstance(text, bytes) else text.encode("utf-8")
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_bytes(data)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def install(*, home: Path | None = None, platform: str | None = None, runner=subprocess.run,
            argv: list[str] | None = None, env: dict[str, str] | None = None, out=None,
            uid: int | None = None) -> int:
    out = out or (lambda s: print(s, flush=True))
    plat = sys.platform if platform is None else platform
    argv = argv or exec_argv()
    env = env if env is not None else unit_env()
    if plat.startswith("linux"):
        path = unit_path(home)
        _write_text(path, unit_text(argv, env))
        _run(["systemctl", "--user", "daemon-reload"], runner)
        rc, _ = _run(["systemctl", "--user", "enable", "--now", UNIT_NAME], runner)
        if rc != 0:
            out(f"[warn] unit written ({path}) but `systemctl --user enable --now {UNIT_NAME}` failed; "
                f"start it by hand")
            return 1
        user = os.environ.get("USER") or ""
        _rc, lingering = _run(["loginctl", "show-user", user, "-p", "Linger"], runner)
        if "Linger=no" in lingering:
            out(f"[hint] runs only while you are logged in; to keep it after logout: "
                f"loginctl enable-linger {user}")
        out(f"caretaker installed (systemd --user, {UNIT_NAME}) and started")
        return 0
    if plat == "darwin":
        path = plist_path(home)
        _write_text(path, plist_bytes(argv, env))
        domain = f"gui/{os.getuid() if uid is None else uid}"
        _run(["launchctl", "bootout", domain, str(path)], runner)
        rc, _ = _run(["launchctl", "bootstrap", domain, str(path)], runner)
        if rc != 0:
            rc, _ = _run(["launchctl", "load", "-w", str(path)], runner)
        if rc != 0:
            out(f"[warn] launch agent written ({path}) but launchctl failed; log out and in again")
            return 1
        out(f"caretaker installed (launchd, {PLIST_LABEL}) and started")
        return 0
    out("[error] autostart is available on Linux (systemd --user) and macOS (launchd); "
        "run `voicehook-agent caretaker run` yourself")
    return 1


def uninstall(*, home: Path | None = None, platform: str | None = None, runner=subprocess.run,
              out=None, uid: int | None = None) -> int:
    out = out or (lambda s: print(s, flush=True))
    plat = sys.platform if platform is None else platform
    if plat.startswith("linux"):
        path = unit_path(home)
        _run(["systemctl", "--user", "disable", "--now", UNIT_NAME], runner)
        existed = path.exists()
        path.unlink(missing_ok=True)
        _run(["systemctl", "--user", "daemon-reload"], runner)
    elif plat == "darwin":
        path = plist_path(home)
        domain = f"gui/{os.getuid() if uid is None else uid}"
        rc, _ = _run(["launchctl", "bootout", domain, str(path)], runner)
        if rc != 0:
            _run(["launchctl", "unload", "-w", str(path)], runner)
        existed = path.exists()
        path.unlink(missing_ok=True)
    else:
        out("[error] no autostart on this platform")
        return 1
    out("caretaker autostart removed" if existed else "caretaker autostart was not installed")
    return 0


def installed(home: Path | None = None, platform: str | None = None) -> bool:
    plat = sys.platform if platform is None else platform
    if plat.startswith("linux"):
        return unit_path(home).exists()
    if plat == "darwin":
        return plist_path(home).exists()
    return False


def status(*, home: Path | None = None, platform: str | None = None, out=None) -> dict:
    """{connected, account, daemon, autostart}; never the token."""
    view = vdev.public_view(vdev.load_device())
    view["daemon"] = vdev.daemon_running()
    view["autostart"] = installed(home, platform)
    if out is not None:
        out(f"connected: {'yes (' + str(view['account']) + ')' if view['connected'] else 'no (run `voicehook-agent connect`)'}"
            f" | daemon: {'running' if view['daemon'] else 'stopped'}"
            f" | autostart: {'installed' if view['autostart'] else 'not installed'}")
    return view

