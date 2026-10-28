"""Owner agent key (0.33.1): `VOICEHOOK_AGENT_KEY` (`vhak_...`, created on voicehook.ai/account).

The server treats an agent as the account owner's own agent (`owner_agent`, needed for the
Kanban relay routes and for helper credentials the agent mints) only when the join presents
such a key as `Authorization: Bearer vhak_...`:

* WebRTC join (default): `GET /api/token?invite=1` (server.py `owner_agent_account`), the
  signed LiveKit token then carries `vh.owner_agent=1`.
* HTTPS bridge join: `POST /api/bridge/join` (bridge_routes.py `bridge_join` -> `_bearer`).

The key is read from the environment only, never written to disk, never printed or logged,
never put into a child process argv. Any error text goes through `redact`.
"""
from __future__ import annotations

import base64
import json
import os
import sys

ENV = "VOICEHOOK_AGENT_KEY"
HOST_ENV = "VOICEHOOK_AGENT_KEY_HOST"   # extra trusted host (staging/local), exact match
DEFAULT_HOST = "voicehook.ai"
LOCAL_HOSTS = ("localhost", "127.0.0.1")
PREFIX = "vhak_"

_warned = False
_warned_hosts: set[str] = set()


def trusted(api_base: str) -> tuple[bool, str]:
    """(ok, host): the key may go only to https://voicehook.ai or exactly the host of
    VOICEHOOK_AGENT_KEY_HOST (https, except localhost / 127.0.0.1)."""
    from urllib.parse import urlsplit

    try:
        u = urlsplit(str(api_base).strip())
        host = (u.hostname or "").lower()
        scheme = u.scheme.lower()
    except ValueError:
        return False, ""
    extra = (os.environ.get(HOST_ENV) or "").strip().lower()
    if host and scheme == "https" and (host == DEFAULT_HOST or (extra and host == extra)):
        return True, host
    if host and extra and host == extra and host in LOCAL_HOSTS and scheme in ("http", "https"):
        return True, host
    return False, host


def for_base(key: str | None, api_base: str) -> str | None:
    """`key` if the join origin is trusted, else None with one warning (no value)."""
    if not key:
        return None
    ok, host = trusted(api_base)
    if ok:
        return key
    if host not in _warned_hosts:
        _warned_hosts.add(host)
        # no host or key in the text (CodeQL: clear-text logging of a value next to the key)
        print("[warn] agent key not sent: the join host is not trusted (only https "
              f"voicehook.ai or {HOST_ENV})", file=sys.stderr, flush=True)
    return None


def state() -> str:
    """'unset' | 'invalid' (wrong prefix) | 'set'. Never returns the value."""
    raw = (os.environ.get(ENV) or "").strip()
    if not raw:
        return "unset"
    return "set" if raw.startswith(PREFIX) and len(raw) > len(PREFIX) else "invalid"


def doctor_line() -> str:
    """`voicehook-agent doctor` text: set / not set / rejected. Whether the server granted
    owner_agent is only known at join time (`owner-agent:` meta line of a WebRTC join)."""
    return {"set": f"set ({ENV}; the server grants owner agent at join, see the "
                   "'owner-agent:' line of join)",
            "unset": f"not set (export {ENV}; see docs/CLI.md, Owner agent key)",
            "invalid": f"rejected (no '{PREFIX}' prefix, ignored)"}[state()]


def get(warn: bool = True) -> str | None:
    """The key when set and well-formed, else None. A malformed value is ignored with a
    one-line warning (once per process, without the value)."""
    global _warned
    st = state()
    if st == "set":
        return os.environ[ENV].strip()
    if st == "invalid" and warn and not _warned:
        _warned = True
        print(f"[warn] {ENV} ignored: it must start with '{PREFIX}' (create one on "
              "voicehook.ai/account)", file=sys.stderr, flush=True)
    return None


def headers(key: str | None) -> dict:
    return {"authorization": f"Bearer {key}"} if key else {}


def redact(text: str, key: str | None = None) -> str:
    """Remove the key (and anything shaped like one) from error text."""
    import re

    key = key or os.environ.get(ENV) or ""
    if key.strip():
        text = text.replace(key.strip(), "***")
    return re.sub(r"vhak_[A-Za-z0-9_\-]+", "vhak_***", text)


def owner_granted(jwt: object) -> bool | None:
    """Did the server put `vh.owner_agent=1` into the (signed) LiveKit token? Unverified
    payload read, only to report; None when the token cannot be read."""
    try:
        part = str(jwt).split(".")[1]
        payload = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        return (payload.get("attributes") or {}).get("vh.owner_agent") == "1"
    except Exception:  # noqa: BLE001
        return None
