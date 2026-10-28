"""Device connection of the caretaker (0.34.0): `voicehook-agent connect`.

ONE one-time approval in the browser by the logged-in account owner (OAuth-style device
flow) gives this machine a device token. The caretaker daemon (caretaker.py) uses it to find
the owner's calls itself and to ask for a relay credential per call; nobody creates keys or
tokens by hand.

Server contract (voicehook-v4), base `$VOICEHOOK_API` (default https://voicehook.ai):
* POST /api/device/start {name} -> {device_code, user_code, verify_url, interval, expires_in}
* POST /api/device/poll {device_code} -> 428 pending | 200 {device_token, account_hint} |
  403 denied | 410 expired
* GET  /api/device/calls?wait=25 (Bearer device token) -> {calls: [{room, started_at, owner_present}]}
* POST /api/device/calls/{room}/caretaker -> {url, token, expires_at} (relay credential, owner rights)
* POST /api/device/calls/{room}/helpers {labels} -> {helpers: [{label, url, token, expires_at}]}
* 401 = revoked / unknown device token.

~/.voicehook/device.json (0600, dir 0700) holds the device token: a credential the USER
approved, comparable to a login session. It is never printed, never logged, never in a
unit file or argv, never in an exception text (errors carry the class and the HTTP status).

stdlib only.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from . import activity as vact

DEFAULT_API = "https://voicehook.ai"
ENV_API = "VOICEHOOK_API"
DEVICE_NAME = "device.json"
LOCK_NAME = "caretaker.lock"
HTTP_TIMEOUT_S = 15.0
CALLS_WAIT_S = 25
POLL_DEFAULT_S = 5.0
POLL_MAX_S = 30.0
SLOW_DOWN_S = 5.0          # 429 slow_down: back off by this much
FLOW_MAX_S = 900.0
LOCALHOSTS = ("localhost", "127.0.0.1", "::1")


class DeviceError(RuntimeError):
    """A failed device request. Carries the HTTP status (0 = network), never a URL or token."""

    def __init__(self, msg: str, status: int = 0) -> None:
        super().__init__(msg)
        self.status = status


def reason_text(status: int) -> str:
    """Why a device / relay request failed, as the end of the German sentence "Konnte X nicht
    in den Call holen, weil ..." (shown to the owner). Status only, never a URL or token."""
    if status == 0:
        return "der Server nicht erreichbar war"
    if status == 401:
        return "dieser Rechner nicht mehr verbunden ist (voicehook-agent connect neu ausführen)"
    if status == 403:
        return "der Server den Zugang abgelehnt hat (Call eines anderen Kontos)"
    if status == 404:
        return "der Server den Call nicht kennt"
    if status == 409:
        return "noch kein Mensch im Call ist"
    if status == 410:
        return "der Call vorbei oder der Zugang abgelaufen ist"
    if status == 422:
        return "der Server den Stand der Session abgelehnt hat (Formatfehler 422)"
    if status == 429:
        return "der Server zu viele Anfragen dieses Rechners abgelehnt hat (Ratenlimit), neuer Versuch in einer Minute"
    if status >= 500:
        return f"der Server einen Fehler hatte ({status})"
    return f"der Server mit {status} geantwortet hat"


# --------------------------------------------------------------------------- #
# base url + http
# --------------------------------------------------------------------------- #
def api_base(env=None) -> str:
    env = os.environ if env is None else env
    base = str(env.get(ENV_API) or "").strip().rstrip("/") or DEFAULT_API
    host = base.split("://", 1)[-1].split("/", 1)[0].rsplit(":", 1)[0].strip("[]")
    if not (base.startswith("https://") or (base.startswith("http://") and host in LOCALHOSTS)):
        raise DeviceError("VOICEHOOK_API must be https (http only for localhost)")
    return base


def http_request(method: str, url: str, body: dict | None = None, token: str = "",
                 timeout: float = HTTP_TIMEOUT_S) -> tuple[int, dict]:
    """One request -> (status, json dict). Network failure: DeviceError(status 0) with the
    exception class only."""
    import urllib.error
    import urllib.request
    headers = {"content-type": "application/json", "user-agent": "voicehook-agent"}
    if token:
        headers["authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, method=method, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            status, raw = r.status, r.read()
    except urllib.error.HTTPError as e:
        status = e.code
        try:
            raw = e.read()
        except Exception:  # noqa: BLE001
            raw = b""
    except Exception as e:  # noqa: BLE001
        raise DeviceError(f"request failed: {type(e).__name__}", 0) from None
    try:
        out = json.loads(raw.decode("utf-8") or "{}")
    except ValueError:
        out = {}
    return status, out if isinstance(out, dict) else {}


# --------------------------------------------------------------------------- #
# device.json
# --------------------------------------------------------------------------- #
def device_path() -> Path:
    return vact.state_root() / DEVICE_NAME


def load_device() -> dict | None:
    """The stored device connection, or None (missing, not ours, group/world readable, broken)."""
    path = device_path()
    try:
        st = os.lstat(path)
        if st.st_mode & 0o077 or (hasattr(os, "getuid") and st.st_uid != os.getuid()):
            return None
        d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict) or not isinstance(d.get("device_token"), str) or not d["device_token"]:
        return None
    return d


def save_device(token: str, account_hint: str, base: str, name: str,
                now: float | None = None) -> Path:
    vact._private_dir(vact.state_root())
    d = {"device_token": token, "account_hint": str(account_hint or "")[:80], "base": base,
         "name": name[:60], "connected_at": int(time.time() if now is None else now)}
    vact._write_private(device_path(), json.dumps(d))
    return device_path()


def clear_device() -> bool:
    try:
        device_path().unlink()
        return True
    except OSError:
        return False


def public_view(dev: dict | None) -> dict:
    """What may be shown: never the token."""
    if not dev:
        return {"connected": False}
    return {"connected": True, "account": dev.get("account_hint") or "", "base": dev.get("base") or "",
            "name": dev.get("name") or "", "connected_at": dev.get("connected_at") or 0}


# --------------------------------------------------------------------------- #
# device flow
# --------------------------------------------------------------------------- #
def start_flow(name: str, base: str, http=http_request) -> dict:
    status, out = http("POST", f"{base}/api/device/start", {"name": name}, "", HTTP_TIMEOUT_S)
    if status != 200 or not out.get("device_code") or not out.get("user_code") \
            or not out.get("verify_url"):
        raise DeviceError(f"device start failed (HTTP {status})", status)
    return out


def poll_flow(device_code: str, base: str, http=http_request) -> tuple[str, dict]:
    """('pending'|'slow_down'|'ok'|'denied'|'expired', body). Anything else raises DeviceError."""
    status, out = http("POST", f"{base}/api/device/poll", {"device_code": device_code}, "", HTTP_TIMEOUT_S)
    if status == 428:
        return "pending", {}
    if status == 200 and isinstance(out.get("device_token"), str) and out["device_token"]:
        return "ok", out
    if status == 429:           # polled faster than the server allows
        return "slow_down", {}
    if status == 403:
        return "denied", {}
    if status == 410:
        return "expired", {}
    raise DeviceError(f"device poll failed (HTTP {status})", status)


def browser_available(env=None, platform: str | None = None) -> bool:
    env = os.environ if env is None else env
    plat = sys.platform if platform is None else platform
    return plat == "darwin" or bool(env.get("DISPLAY") or env.get("WAYLAND_DISPLAY"))


def connect(name: str | None = None, *, http=http_request, sleep=time.sleep, clock=time.time,
            out=None, open_url=None, env=None) -> int:
    """Run the device flow: print the verify URL + code, poll until approved, store the token.
    0 = connected, 1 = denied / expired / failed."""
    out = out or (lambda s: print(s, flush=True))
    name = (name or socket.gethostname() or "machine")[:60]
    try:
        base = api_base(env)
        flow = start_flow(name, base, http)
    except DeviceError as e:
        out(f"[error] {e}")
        return 1
    url, code = str(flow["verify_url"]), str(flow["user_code"])
    out(f"Zum Verbinden dieser Maschine oeffne / To connect this machine open: {url}  "
        f"Code: {code}")
    if open_url is None and browser_available(env):
        import webbrowser
        open_url = webbrowser.open
    if open_url is not None:
        try:
            open_url(url)
        except Exception:  # noqa: BLE001, S110
            pass
    try:
        interval = max(1.0, min(POLL_MAX_S, float(flow.get("interval") or POLL_DEFAULT_S)))
    except (TypeError, ValueError):
        interval = POLL_DEFAULT_S
    try:
        life = float(flow.get("expires_in") or FLOW_MAX_S)
    except (TypeError, ValueError):
        life = FLOW_MAX_S
    deadline = clock() + min(life, FLOW_MAX_S)
    errors = 0
    while clock() < deadline:
        sleep(interval)
        try:
            verdict, body = poll_flow(str(flow["device_code"]), base, http)
        except DeviceError as e:
            errors += 1
            if errors >= 5:
                out(f"[error] {e}")
                return 1
            continue
        errors = 0
        if verdict == "slow_down":
            interval += SLOW_DOWN_S
            continue
        if verdict == "pending":
            continue
        if verdict == "ok":
            save_device(body["device_token"], str(body.get("account_hint") or ""), base, name,
                        clock())
            out(f"Verbunden / connected: {body.get('account_hint') or 'account'}")
            return 0
        out("[error] Verbindung abgelehnt / connection denied" if verdict == "denied"
            else "[error] Code abgelaufen / code expired, run `voicehook-agent connect` again")
        return 1
    out("[error] Code abgelaufen / code expired, run `voicehook-agent connect` again")
    return 1


# --------------------------------------------------------------------------- #
# authenticated calls (device token)
# --------------------------------------------------------------------------- #
def _auth(dev: dict, method: str, path: str, body: dict | None, http, timeout: float) -> dict:
    base = str(dev.get("base") or "").rstrip("/") or api_base()
    status, out = http(method, f"{base}{path}", body, str(dev.get("device_token") or ""), timeout)
    if status == 200:
        return out
    raise DeviceError(f"{path.split('?')[0].rsplit('/', 1)[-1]} failed (HTTP {status})", status)


def list_calls(dev: dict, wait: int = CALLS_WAIT_S, http=http_request) -> list[dict]:
    out = _auth(dev, "GET", f"/api/device/calls?wait={int(wait)}", None, http, wait + 15.0)
    calls = out.get("calls")
    return [c for c in calls if isinstance(c, dict) and c.get("room")] if isinstance(calls, list) else []


def caretaker_credential(dev: dict, room: str, http=http_request) -> dict:
    out = _auth(dev, "POST", f"/api/device/calls/{quote(str(room), safe='')}/caretaker", {}, http,
                HTTP_TIMEOUT_S)
    if not out.get("url") or not out.get("token"):
        raise DeviceError("caretaker credential incomplete", 502)
    return out


def helper_credentials(dev: dict, room: str, labels: list[str], http=http_request) -> list[dict]:
    out = _auth(dev, "POST", f"/api/device/calls/{quote(str(room), safe='')}/helpers",
                {"labels": list(labels)}, http, HTTP_TIMEOUT_S)
    hs = out.get("helpers")
    return [h for h in hs if isinstance(h, dict) and h.get("label") and h.get("url") and h.get("token")] \
        if isinstance(hs, list) else []


def ts(v: object) -> float:
    """Epoch seconds from a number or an ISO 8601 string, else 0."""
    if isinstance(v, bool):
        return 0.0
    if isinstance(v, (int, float)):
        return float(v) / 1000.0 if v > 1e11 else float(v)
    try:
        return datetime.fromisoformat(str(v).strip().replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


# --------------------------------------------------------------------------- #
# the daemon lock
# --------------------------------------------------------------------------- #
def acquire_daemon_lock():
    """flock ~/.voicehook/caretaker.lock (one daemon per machine). The fd, or None when held."""
    try:
        import fcntl
    except ImportError:  # pragma: no cover  (Windows)
        return None
    vact._private_dir(vact.state_root())
    fd = os.open(vact.state_root() / LOCK_NAME, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    try:
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
    except OSError:
        pass
    return fd


def daemon_running() -> bool:
    fd = acquire_daemon_lock()
    if fd is None:
        return True
    os.close(fd)
    return False


def caretaker_active() -> bool:
    """A connected machine whose caretaker daemon runs: it covers the calls, the classic
    detached watcher of join / helper-import stays out of its way."""
    return load_device() is not None and daemon_running()
