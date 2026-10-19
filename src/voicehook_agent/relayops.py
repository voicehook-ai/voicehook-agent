"""Helper sessions on the Decision Board and their own status board (0.16.0).

A helper session is a Claude Code session of this machine that is NOT itself in the call,
while another session joined and wrote ~/.voicehook/relay.json (multisession.py). Until
0.15.0 a helper only sent activity lines. Now `decide` and `status --doing/--open/--done`
of a helper go over that relay, under the helper's session label (`from`, the same label
the hook uses), and `relay-next` returns the answers to the helper's own questions.

Routing (`client_relay`): `--session` given or VOICEHOOK_NO_RELAY=1: never the relay.
No usable relay.json: never. This Claude Code session owns a running join (a live
session.json or relay.json names its session hash): its own join. A Claude Code session
without its own join: the relay. Outside Claude Code (no session id): the relay only when
no join control socket answers.

Remote relay (relay.json kind remote {url, token}, url = <base>/api/activity/relay):
* POST <url>/decide  {from, question?, options?, recommend?, id?, delete?} -> {ok, id, number}
* POST <url>/status  {from, doing?, open?, done?} -> {ok}; all empty removes the card
* GET  <url>/next?from=<label>&timeout=<0..25> -> {events: [{type: "decision", ...}]}
Bearer relay token, never in the URL. 401 marks the token dead (relay.dead) like the hook.
Events fetched while `decide --wait` waits for another id are kept in a local inbox
(state dir, `relay-inbox-<hash of from>.jsonl`, 0600) and handed out by the next
`relay-next` first, so nothing is lost.

Local relay (relay.json kind local, WebRTC join; file protocol, stdlib only):
* Request file `<join dir>/relay-req.jsonl` (0600, O_APPEND, flock, never a symlink), one
  JSON object per line: {"v": 1, "rid": "<16 hex>", "from": "<label>", "op":
  "decide"|"status", "body": {...}, "ts": <unix>}. Written only when the join dir is
  ours and its session.json names the relay's owner pid.
* The join drains it (flock, read, truncate) every POLL_S and runs decide (LiveKit RPC
  vh.decide with `sender`) or operator.status (with `sub`); sender = sub =
  "<machine label>: <from>".
* Answer file `<join dir>/relay-ans-<hash of from>.jsonl` (0600, written by the join):
  {"type": "result", "rid": ..., "ok": ..., "id"?, "number"?, "status"?, "error"?} for each
  request, and every later Decision Board event {"type": "decision", ...} whose `sender`
  is that helper's sender. The helper takes the lines it consumes (flock, rewrite rest).
Files are capped (REQ_LINE_MAX per request, ANS_MAX_BYTES per answer file: oldest dropped).

Never prints or logs the token or any question/board text to stderr.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import stat
import time
from pathlib import Path
from urllib.parse import quote, urlsplit

from . import activity as vact
from . import multisession as vmulti

REQ_NAME = "relay-req.jsonl"
ANS_PREFIX = "relay-ans-"
INBOX_PREFIX = "relay-inbox-"
RELAY_PATH = "/api/activity/relay"
HTTP_TIMEOUT_S = 10.0
NEXT_MAX_S = 25
REQ_LINE_MAX = 8192
REQ_BATCH_MAX = 50
ANS_MAX_BYTES = 256 * 1024
RESULT_WAIT_S = 20.0
POLL_S = 0.3
FROM_MAX = vmulti.SESSION_LABEL_MAX


class RelayError(RuntimeError):
    def __init__(self, msg: str, status: int | None = None) -> None:
        super().__init__(msg)
        self.status = status


# --------------------------------------------------------------------------- #
# who am I, where does it go
# --------------------------------------------------------------------------- #
def _config_dir() -> Path:
    env = (os.environ.get("CLAUDE_CONFIG_DIR") or "").strip()
    return Path(env) if env else Path.home() / ".claude"


def find_transcript(session_id: str) -> str:
    """Transcript of this Claude Code session (projects/*/<id>.jsonl), '' when not found."""
    sid = str(session_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", sid):
        return ""
    try:
        for p in sorted((_config_dir() / "projects").glob(f"*/{sid}.jsonl")):
            if p.is_file():
                return str(p)
    except OSError:
        pass
    return ""


def _cached_label(skey: str) -> str:
    try:
        st = json.loads((vact.state_root() / vmulti.STATE_NAME).read_text(encoding="utf-8"))
        me = st.get("sessions", {}).get(skey) if isinstance(st, dict) else None
        return str(me.get("label") or "") if isinstance(me, dict) else ""
    except (OSError, ValueError, AttributeError):
        return ""


def session_label() -> str:
    """`from` of this session, the hook's logic: $VOICEHOOK_SESSION_LABEL > custom-title >
    ai-title of the transcript > the label the hook saw before > `<cwd> <4 hex>`."""
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID") or ""
    try:
        cwd = os.getcwd()
    except OSError:
        cwd = ""
    event = {"session_id": sid, "cwd": cwd, "transcript_path": find_transcript(sid)}
    return vmulti.session_label(event, _cached_label(vmulti.session_hash(sid)))[:FROM_MAX]


def _own_join_running(own: str) -> bool:
    """A live join (session.json with a live pid) started by this Claude Code session."""
    from . import session as vsession
    for d in vact.live_join_dirs():
        try:
            info = json.loads((d / vsession.INFO_NAME).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(info, dict) and info.get("owner_session") == own:
            return True
    return False


def _socket_answers() -> bool:
    from . import session as vsession
    try:
        vsession.resolve_socket(None, wait=0.0)
        return True
    except (vsession.SessionError, OSError):
        return False


def client_relay(explicit_session: str | None) -> dict | None:
    """The relay.json a helper command should use, or None (talk to the own join)."""
    if explicit_session or vmulti.relay_disabled():
        return None
    rel = vmulti.load_relay()
    if rel is None:
        return None
    own = vmulti.own_session_hash()
    if own:
        if own == str(rel.get("owner_session") or "") or _own_join_running(own):
            return None
        return rel
    return None if _socket_answers() else rel


def _from_hash(frm: str) -> str:
    return vmulti._h(frm, "vh-from")


def check_from(frm: str) -> str:
    f = str(frm or "").strip()
    if not 1 <= len(f) <= FROM_MAX:
        raise RelayError("session label must be 1 to 40 chars")
    return f


# --------------------------------------------------------------------------- #
# private jsonl files
# --------------------------------------------------------------------------- #
def _private_regular(path: Path) -> bool:
    """Missing, or a regular file of ours that nobody else can read or write."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o077:
        return False
    return not (hasattr(os, "getuid") and st.st_uid != os.getuid())


def _open_locked(path: Path, create: bool) -> int | None:
    if not _private_regular(path):
        raise RelayError("relay file is not a private regular file")
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | (os.O_CREAT if create else 0)
    try:
        fd = os.open(path, flags, 0o600)
    except FileNotFoundError:
        return None
    try:
        os.fchmod(fd, 0o600)
    except (AttributeError, OSError):
        pass
    vact._lock(fd)
    return fd


def _read_all(fd: int) -> bytes:
    os.lseek(fd, 0, os.SEEK_SET)
    chunks = []
    while True:
        b = os.read(fd, 1 << 16)
        if not b:
            return b"".join(chunks)
        chunks.append(b)


def _rewrite(fd: int, lines: list[str]) -> None:
    data = "".join(ln + "\n" for ln in lines).encode("utf-8")
    os.lseek(fd, 0, os.SEEK_SET)
    os.ftruncate(fd, 0)
    while data:
        data = data[os.write(fd, data):]


def append_jsonl(path: Path, obj: dict, cap: int = ANS_MAX_BYTES) -> None:
    """Append one JSON line under flock (0600, no symlink); drop the oldest when > cap."""
    line = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    fd = _open_locked(path, create=True)
    assert fd is not None
    try:
        old = _read_all(fd).decode("utf-8", "replace").splitlines()
        lines = [ln for ln in old if ln.strip()] + [line]
        while len(lines) > 1 and sum(len(x) + 1 for x in lines) > cap:
            lines.pop(0)
        _rewrite(fd, lines)
    finally:
        os.close(fd)


def take_jsonl(path: Path, want) -> list[dict]:
    """Remove and return the lines `want(obj)` accepts, keep the rest (flock)."""
    try:
        fd = _open_locked(path, create=False)
    except RelayError:
        return []
    if fd is None:
        return []
    try:
        taken: list[dict] = []
        keep: list[str] = []
        for ln in _read_all(fd).decode("utf-8", "replace").splitlines():
            if not ln.strip():
                continue
            try:
                obj = json.loads(ln)
            except ValueError:
                continue
            if isinstance(obj, dict) and want(obj):
                taken.append(obj)
            else:
                keep.append(ln)
        if taken:
            _rewrite(fd, keep)
        return taken
    finally:
        os.close(fd)


def drain_requests(dir_: Path) -> list[dict]:
    """Join side: take all queued helper requests (max REQ_BATCH_MAX per call)."""
    path = Path(dir_) / REQ_NAME
    try:
        fd = _open_locked(path, create=False)
    except RelayError:
        return []
    if fd is None:
        return []
    try:
        lines = [ln for ln in _read_all(fd).decode("utf-8", "replace").splitlines() if ln.strip()]
        now, rest = lines[:REQ_BATCH_MAX], lines[REQ_BATCH_MAX:]
        _rewrite(fd, rest)
    finally:
        os.close(fd)
    out = []
    for ln in now:
        if len(ln) > REQ_LINE_MAX:
            continue
        try:
            obj = json.loads(ln)
        except ValueError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def answer_path(dir_: Path, frm: str) -> Path:
    return Path(dir_) / f"{ANS_PREFIX}{_from_hash(frm)}.jsonl"


def inbox_path(frm: str) -> Path:
    return vact.state_root() / f"{INBOX_PREFIX}{_from_hash(frm)}.jsonl"


def clear_dir(dir_: Path) -> None:
    """Join start/end: no request or answer of an earlier call stays."""
    try:
        for p in Path(dir_).iterdir():
            if p.name == REQ_NAME or (p.name.startswith(ANS_PREFIX) and p.name.endswith(".jsonl")):
                try:
                    p.unlink()
                except OSError:
                    pass
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# request bodies (both relay kinds)
# --------------------------------------------------------------------------- #
DECIDE_KEYS = ("question", "options", "recommend", "id", "delete")
STATUS_KEYS = ("doing", "open", "done")


def decide_body(req: dict) -> dict:
    return {k: req[k] for k in DECIDE_KEYS if req.get(k) is not None}


def status_body(board: dict) -> dict:
    """{doing, open, done} of a CLI board (faq is not part of the relay contract)."""
    out: dict = {}
    if isinstance(board.get("doing"), str):
        out["doing"] = board["doing"]
    for k in ("open", "done"):
        v = board.get(k)
        if isinstance(v, list):
            out[k] = [str(x) for x in v if isinstance(x, str)]
    return out


def _valid_decide(body: object) -> dict | None:
    if not isinstance(body, dict):
        return None
    out: dict = {}
    for k, typ in (("question", str), ("id", str), ("delete", bool)):
        if body.get(k) is not None:
            if not isinstance(body[k], typ):
                return None
            out[k] = body[k]
    if body.get("recommend") is not None:
        if not isinstance(body["recommend"], int) or isinstance(body["recommend"], bool):
            return None
        out["recommend"] = body["recommend"]
    if body.get("options") is not None:
        opts = body["options"]
        if not isinstance(opts, list) or len(opts) > 3 or not all(isinstance(o, str) for o in opts):
            return None
        out["options"] = opts
    return out if out.get("question") or out.get("id") else None


def _valid_status(body: object) -> dict | None:
    if not isinstance(body, dict):
        return None
    out: dict = {}
    if body.get("doing") is not None:
        if not isinstance(body["doing"], str):
            return None
        out["doing"] = body["doing"]
    for k in ("open", "done"):
        v = body.get(k)
        if v is not None:
            if not isinstance(v, list) or len(v) > 10 or not all(isinstance(x, str) for x in v):
                return None
            out[k] = v
    return out


# --------------------------------------------------------------------------- #
# helper side: remote (HTTP)
# --------------------------------------------------------------------------- #
def endpoint(rel: dict, name: str) -> str:
    """<base>/api/activity/relay/<name>; the base comes from the relay url."""
    url = str(rel.get("url") or "")
    parts = urlsplit(url)
    path = parts.path.rstrip("/")
    i = path.find(RELAY_PATH)
    prefix = path[:i] if i >= 0 else ""
    return f"{parts.scheme}://{parts.netloc}{prefix}{RELAY_PATH}/{name}"


def _http(rel: dict, method: str, url: str, body: dict | None, timeout: float) -> tuple[int, dict]:
    import urllib.error
    import urllib.request
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, method=method, data=data, headers={
        "authorization": f"Bearer {rel['token']}", "content-type": "application/json",
        "user-agent": "voicehook-agent"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # url from our own 0600 file
            status, raw = r.status, r.read()
    except urllib.error.HTTPError as e:
        status = e.code
        try:
            raw = e.read()
        except Exception:  # noqa: BLE001
            raw = b""
    except Exception as e:  # noqa: BLE001  network: only the class, never the URL
        raise RelayError(f"relay request failed: {type(e).__name__}", 503) from None
    if status == 401:
        vmulti.mark_dead(rel["token"])
    try:
        out = json.loads(raw.decode("utf-8") or "{}")
    except ValueError:
        out = {}
    return status, out if isinstance(out, dict) else {}


def _err(status: int, out: dict) -> dict:
    detail = out.get("detail") or out.get("error") or f"HTTP {status}"
    return {"ok": False, "status": status, "error": str(detail)[:300]}


def remote_decide(rel: dict, frm: str, body: dict, timeout: float = HTTP_TIMEOUT_S) -> dict:
    status, out = _http(rel, "POST", endpoint(rel, "decide"), {"from": frm, **body}, timeout)
    if status >= 300 or out.get("ok") is False:
        return _err(status, out)
    return {"ok": True, **{k: out[k] for k in ("id", "number") if k in out}}


def remote_status(rel: dict, frm: str, body: dict, timeout: float = HTTP_TIMEOUT_S) -> dict:
    status, out = _http(rel, "POST", endpoint(rel, "status"), {"from": frm, **body}, timeout)
    if status >= 300 or out.get("ok") is False:
        return _err(status, out)
    return {"ok": True}


def remote_next(rel: dict, frm: str, timeout: float) -> list[dict]:
    t = max(0, min(NEXT_MAX_S, int(timeout)))
    url = f"{endpoint(rel, 'next')}?from={quote(frm, safe='')}&timeout={t}"
    status, out = _http(rel, "GET", url, None, t + HTTP_TIMEOUT_S)
    if status >= 300:
        e = _err(status, out)
        raise RelayError(e["error"], status)
    evs = out.get("events")
    return [e for e in evs if isinstance(e, dict)] if isinstance(evs, list) else []


# --------------------------------------------------------------------------- #
# helper side: local (files in the join dir)
# --------------------------------------------------------------------------- #
def local_dir(rel: dict) -> Path:
    """The join dir of a local relay: ours and run by the relay's owner pid."""
    from . import session as vsession
    d = Path(str(rel.get("dir") or ""))
    try:
        st = os.lstat(d)
        if not stat.S_ISDIR(st.st_mode) or (hasattr(os, "getuid") and st.st_uid != os.getuid()):
            raise RelayError("join dir not usable", 503)
        info = json.loads((d / vsession.INFO_NAME).read_text(encoding="utf-8"))
        if int(info.get("pid") or 0) != int(rel.get("owner_pid") or -1):
            raise RelayError("join of the relay is gone", 503)
    except RelayError:
        raise
    except (OSError, ValueError, TypeError, AttributeError):
        raise RelayError("join dir not usable", 503) from None
    return d


def local_request(rel: dict, frm: str, op: str, body: dict,
                  wait: float = RESULT_WAIT_S, sleep=time.sleep) -> dict:
    """Queue one request for the join and wait for its result line."""
    d = local_dir(rel)
    rid = secrets.token_hex(8)
    line = {"v": 1, "rid": rid, "from": frm, "op": op, "body": body, "ts": int(time.time())}
    if len(json.dumps(line, ensure_ascii=False)) > REQ_LINE_MAX:
        return {"ok": False, "status": 422, "error": "request too long"}
    append_jsonl(d / REQ_NAME, line, cap=REQ_LINE_MAX * REQ_BATCH_MAX * 2)
    deadline = time.monotonic() + wait
    ans = answer_path(d, frm)
    while True:
        got = take_jsonl(ans, lambda o: o.get("type") == "result" and o.get("rid") == rid)
        if got:
            res = {k: v for k, v in got[0].items() if k not in ("type", "rid")}
            return res
        if time.monotonic() >= deadline:
            return {"ok": False, "status": 504, "error": "no answer from the join (is it still running?)"}
        sleep(POLL_S)


def _is_decision(o: dict) -> bool:
    return o.get("type") == "decision"


def next_events(rel: dict, frm: str, timeout: float, *, match_id: str | None = None,
                sleep=time.sleep) -> list[dict]:
    """Decision events for this helper; with match_id only that one (others stay queued).
    Waits up to `timeout` seconds; [] on timeout."""
    want = (lambda o: _is_decision(o) and o.get("id") == match_id) if match_id else _is_decision
    deadline = time.monotonic() + max(0.0, timeout)
    inbox = inbox_path(frm)
    while True:
        got = take_jsonl(inbox, want)
        if got:
            return got
        if vmulti._is_local(rel):
            got = take_jsonl(answer_path(local_dir(rel), frm), want)
            if got:
                return got
            if time.monotonic() >= deadline:
                return []
            sleep(POLL_S)
            continue
        left = deadline - time.monotonic()
        evs = remote_next(rel, frm, max(0.0, left))
        if match_id:
            mine = [e for e in evs if want(e)]
            for e in evs:
                if not want(e):
                    vact._private_dir(vact.state_root())
                    append_jsonl(inbox, e)
            if mine:
                return mine
        elif evs:
            return evs
        if time.monotonic() >= deadline:
            return []


# --------------------------------------------------------------------------- #
# join side
# --------------------------------------------------------------------------- #
def sender_for(frm: str, machine: str | None = None) -> str:
    return f"{machine or vmulti.machine_label()}: {frm}"


def parse_request(obj: dict) -> tuple[str, str, str, dict | None] | None:
    """(rid, from, op, body) of a request line; body None = invalid (answer 422).
    None when not even rid/from are usable (nobody to answer)."""
    rid, frm, op = obj.get("rid"), obj.get("from"), obj.get("op")
    if not isinstance(rid, str) or not re.fullmatch(r"[0-9a-f]{16}", rid):
        return None
    if not isinstance(frm, str) or vmulti._clean_label(frm) != frm or not 1 <= len(frm) <= FROM_MAX:
        return None
    if op == "decide":
        body = _valid_decide(obj.get("body"))
    elif op == "status":
        body = _valid_status(obj.get("body"))
    else:
        return rid, frm, str(op), None
    return rid, frm, op, body
