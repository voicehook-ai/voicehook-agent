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
* POST <url>/decide  {from, question?, options?, recommend?, id?, delete?, context?, recent?}
  -> {ok, id, number} (0.21.0: context/recent = background for Delta)
* POST <url>/status  {from, doing?, open?, done?, project?} -> {ok}; all empty removes the card
* POST <url>/show    {from, kind, title?, text?, image?} -> {ok, id, kind, from, shown?} (0.23.1:
  `show --card` of a helper; an image only ok once a call page loaded it, else 409 + why)
* GET  <url>/next?from=<label>&timeout=<0..25> -> {events: [{type: "decision", ...}]}; 0.22.0
  also {type: "user_turn", text, to, to_kind: "helper", speaker?, ts}: the human spoke to
  this helper by name in the call (a request to it)
Bearer relay token, never in the URL. 401 marks the token dead (relay.dead) like the hook.
Events fetched while `decide --wait` waits for another id are kept in a local inbox
(state dir, `relay-inbox-<hash of from>.jsonl`, 0600) and handed out by the next
`relay-next` first, so nothing is lost.

Local relay (relay.json kind local, WebRTC join; file protocol, stdlib only):
* Request file `<join dir>/relay-req.jsonl` (0600, O_APPEND, flock, never a symlink), one
  JSON object per line: {"v": 1, "rid": "<16 hex>", "from": "<label>", "op":
  "decide"|"status"|"state" (0.21.4: {state}, the helper's session state)|"card" (0.23.1: text,
  code or link card; images need the server relay), "body": {...},
  "ts": <unix>}. Written only when the join dir is
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
SHOW_TIMEOUT_S = 50.0   # 0.23.1: server stores the image, the call pages confirm it (15 s)
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
    sid = vmulti.own_session_id()
    try:
        cwd = os.getcwd()
    except OSError:
        cwd = ""
    event = {"session_id": sid, "cwd": cwd, "transcript_path": find_transcript(sid)}
    return vmulti.session_label(event, _cached_label(vmulti.session_hash(sid)))[:FROM_MAX]


def _own_join_running(own: str) -> bool:
    """A live join (session.json with a live pid) started by this agent session: its
    owner_session, or 0.25.0 the same agent process after /clear or /resume (a new session
    id; session.owned_by)."""
    from . import session as vsession
    for d in vact.live_join_dirs():
        if vsession.owned_by(vsession.read_info(d), own):
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
    """The relay a command of this session uses, or None (talk to the own join). ONE rule
    for every helper-capable command (decide, status board, activity, next, show; 0.22.2):
    1. `--session` given, or VOICEHOOK_NO_RELAY=1: never the relay (own join).
    2. This agent session runs its own join (session.json names its session hash, or 0.25.0
       the same agent process after /clear): own join.
    3. Its own helper credential (helpers/<name>.json, multisession.load_helper:
       $VOICEHOOK_SESSION_LABEL names it, or it was imported in this session, or the only
       unbound one): the relay.
    4. relay.json of a join on this machine (or a legacy pre-0.22.2 helper-import): the
       relay, except for the session that wrote it; outside an agent session only when no
       join control socket answers.
    5. Nothing: None (the caller then reports no-session with a hint). 0.25.0: when this
       session has its own helper entry but it is dead/expired, the caller reports
       helper_problem() instead and never falls back to another session's join."""
    if explicit_session or vmulti.relay_disabled():
        return None
    own = vmulti.own_session_hash()
    if own and _own_join_running(own):
        return None
    rel = vmulti.load_relay()
    if rel is None:
        return None
    if rel.get("helper"):
        return rel
    if own:
        return None if own == str(rel.get("owner_session") or "") else rel
    return None if _socket_answers() else rel


HELPER_PROBLEM = {
    "dead": "the relay of helper {name!r} is dead (the main agent rejoined or revoked it)",
    "expired": "the helper credential of {name!r} has expired",
    "unusable": "the helper credential of {name!r} is not usable",
}
HELPER_RENEW = ("nothing was sent (a helper never posts through the join of another session). "
                "Ask the main agent of the call for a fresh credential: `voicehook-agent "
                "helper-credential --label {name} | voicehook-agent helper-import --as {name}` "
                "(MCP: helper_credential), then repeat the command")


def helper_problem(explicit_session: str | None) -> dict | None:
    """0.25.0 (live 22.10.2026, crown): this session is a helper (its own helper entry,
    multisession.own_helper_entry) without a join of its own, and that entry is dead, expired
    or unusable. Then the error the caller prints INSTEAD of falling back to
    resolve_socket(None): that finds the join of ANOTHER session of the machine, which posted
    the helper's decisions as its own (no sender) and got the answers. None when the session
    may use a join (`--session`, VOICEHOOK_NO_RELAY=1, its own join, no helper entry) or its
    entry is live."""
    if explicit_session or vmulti.relay_disabled():
        return None
    own = vmulti.own_session_hash()
    if own and _own_join_running(own):
        return None
    found = vmulti.own_helper_entry()
    if found is None:
        return None
    state = vmulti.helper_state(found[1], found[0])
    if state == "live":
        return None
    name = str(found[1].get("helper") or "helper")
    return {"ok": False, "type": f"relay-{state}",
            "error": HELPER_PROBLEM.get(state, HELPER_PROBLEM["unusable"]).format(name=name),
            "hint": HELPER_RENEW.format(name=name)}


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
    vact._private_dir(Path(path).parent)  # the state/join dir may not exist yet (first inbox line)
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


def read_jsonl(path: Path) -> list[dict]:
    """All JSON objects in `path`, oldest first, WITHOUT removing them (flock). [] when the file
    is missing or not a private regular file. For a read-only view (board reconstruction)."""
    try:
        fd = _open_locked(path, create=False)
    except RelayError:
        return []
    if fd is None:
        return []
    try:
        out: list[dict] = []
        for ln in _read_all(fd).decode("utf-8", "replace").splitlines():
            if not ln.strip():
                continue
            try:
                obj = json.loads(ln)
            except ValueError:
                continue
            if isinstance(obj, dict):
                out.append(obj)
        return out
    finally:
        os.close(fd)


# board reconstruction reads the inbox without consuming it (a2a.py, cli `tasks`)
read_inbox = read_jsonl


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


OUTBOX_PREFIX = "a2a-outbox-"


def outbox_path(frm: str) -> Path:
    """0.24.0 a2a.py: a session's durable record of the tasks IT sent (board reconstruction).
    Separate from the inbox so `relay-next` never re-delivers a session its own sent tasks."""
    return vact.state_root() / f"{OUTBOX_PREFIX}{_from_hash(frm)}.jsonl"


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
DECIDE_KEYS = ("question", "options", "recommend", "id", "delete", "context", "recent",  # 0.21.0
               "faq")  # 0.21.4: hidden follow-up questions for the voicebot
STATUS_KEYS = ("doing", "open", "done", "project")


def decide_body(req: dict) -> dict:
    return {k: req[k] for k in DECIDE_KEYS if req.get(k) is not None}


def status_body(board: dict) -> dict:
    """{doing, open, done, project?} of a CLI board (faq is not part of the relay contract;
    0.21.0: project = `status --project`)."""
    out: dict = {}
    if isinstance(board.get("doing"), str):
        out["doing"] = board["doing"]
    for k in ("open", "done"):
        v = board.get(k)
        if isinstance(v, list):
            out[k] = [str(x) for x in v if isinstance(x, str)]
    if isinstance(board.get("project"), str) and board["project"]:
        out["project"] = board["project"]
    if board.get("state") in ("arbeitet", "wartet", "frage", "leerlauf", "problem"):
        out["state"] = board["state"]  # 0.21.6 `status --waiting`: frage (server field state)
    att = clean_attach(board.get("attach"))  # 0.25.0 status --attach
    if att:
        out["attach"] = att
    if board.get("meta"):  # 0.21.4: per item {context, faq}; a server without it ignores it
        from . import relay as vrelay
        meta = vrelay.clean_board_meta(board["meta"], out)
        if meta:
            out["meta"] = meta
    return out


FILE_KINDS = ("pdf", "csv", "md")
ATTACH_KINDS = (*FILE_KINDS, "image")
ATTACH_MAX = 8


def clean_attach(v: object) -> list[dict]:
    """0.25.0 board field attach [{id: 16 hex, name, kind}] (max ATTACH_MAX); bad items dropped."""
    out: list[dict] = []
    for a in v if isinstance(v, list) else []:
        if (isinstance(a, dict) and isinstance(a.get("id"), str) and re.fullmatch(r"[0-9a-f]{16}", a["id"])
                and a.get("kind") in ATTACH_KINDS and isinstance(a.get("name"), str)):
            out.append({"id": a["id"], "name": a["name"][:120], "kind": a["kind"]})
    return out[:ATTACH_MAX]


def _valid_decide(body: object) -> dict | None:
    if not isinstance(body, dict):
        return None
    out: dict = {}
    for k, typ in (("question", str), ("id", str), ("delete", bool),
                   ("context", str), ("recent", str)):  # 0.21.0 context/recent
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
    if body.get("faq") is not None:  # 0.21.4
        from . import relay as vrelay
        m = vrelay.clean_meta({"faq": body["faq"]})
        if m is None:
            return None
        if m.get("faq"):
            out["faq"] = m["faq"]
    return out if out.get("question") or out.get("id") else None


HELPER_STATES = ("arbeitet", "wartet", "frage", "leerlauf", "problem")  # server board.HELPER_STATES


def _valid_state(body: object) -> dict | None:
    s = body.get("state") if isinstance(body, dict) else None
    return {"state": s} if isinstance(s, str) and s in HELPER_STATES else None


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
    if body.get("project") is not None:  # 0.21.0 `status --project` of a helper
        p = body["project"]
        if not isinstance(p, str) or not 1 <= len(p) <= 40:
            return None
        out["project"] = p
    if body.get("meta") is not None:  # 0.21.4 per item {context, faq}
        from . import relay as vrelay
        meta = vrelay.clean_board_meta(body["meta"], out)
        if meta is None:
            return None
        if meta:
            out["meta"] = meta
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


RETRY_503_S = (0.5, 1.0, 2.0)  # 0.22.2: backoff while the server answers 503 (v4 #586)
_sleep = time.sleep


def _http(rel: dict, method: str, url: str, body: dict | None, timeout: float) -> tuple[int, dict]:
    """One relay request. 503 (v4 #586: the main agent is briefly away) = wait and retry
    (RETRY_503_S), never a dead credential; only 401 marks the token dead here (the hook's
    post_line also 403/410)."""
    for wait in (*RETRY_503_S, None):
        status, out = _http_once(rel, method, url, body, timeout)
        if status != 503 or wait is None:
            return status, out
        _sleep(wait)
    return status, out  # pragma: no cover


def _http_once(rel: dict, method: str, url: str, body: dict | None,
               timeout: float) -> tuple[int, dict]:
    import urllib.error
    import urllib.request
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, method=method, data=data, headers={
        "authorization": f"Bearer {rel['token']}", "content-type": "application/json",
        "user-agent": "voicehook-agent"})
    card_id = None
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # url from our own 0600 file
            status, raw = r.status, r.read()
    except urllib.error.HTTPError as e:
        status = e.code
        card_id = (e.headers or {}).get("X-Card-Id") if e.headers is not None else None
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
    out = out if isinstance(out, dict) else {}
    # 0.25.0: 409 of relay/show = card is in the call, no page loaded it yet; the server names it
    if card_id and re.fullmatch(r"[0-9a-f]{16}", str(card_id)):
        out["_card_id"] = str(card_id)
    return status, out


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


ACK_IDS_MAX = 50


def remote_ack(rel: dict, frm: str, ids: list[str], timeout: float = HTTP_TIMEOUT_S) -> dict:
    """0.25.x delivery receipt (voicehook-v4 card 9): tell the server this helper session (`frm`)
    consumed the answers to these decisions (`ids`) via relay-next, so the asking card shows
    "delivered to the helper". Best effort, a display signal only, never an approval. Never logs
    content. On a local relay nothing is sent (the main agent sees the board itself)."""
    ids = [str(i) for i in (ids or []) if isinstance(i, str) and i][:ACK_IDS_MAX]
    if not ids or vmulti._is_local(rel):
        return {"ok": True, "acked": 0}
    status, out = _http(rel, "POST", endpoint(rel, "ack"), {"from": frm, "ids": ids}, timeout)
    if status >= 300 or out.get("ok") is False:
        return _err(status, out)
    return {"ok": True, "acked": int(out.get("acked") or 0)}


def card_body(req: dict) -> dict:
    """0.23.1 `show --card` of a helper -> relay body {kind, title?, text?, image?}. Panels belong
    to the main agent: --panel/--large -> RelayError 422."""
    if req.get("panel") is not None or req.get("large"):
        raise RelayError("a helper session shows cards only: --panel/--large belong to the main agent", 422)
    if not req.get("kind"):
        raise RelayError("a helper session needs --card text|code|link|image|pdf|csv|md", 422)
    return {k: req[k] for k in ("kind", "title", "text", "image", "name", "data")
            if req.get(k) is not None}


def remote_show(rel: dict, frm: str, body: dict, timeout: float = SHOW_TIMEOUT_S) -> dict:
    status, out = _http(rel, "POST", endpoint(rel, "show"), {"from": frm, **body}, timeout)
    if status >= 300 or out.get("ok") is False:
        e = _err(status, out)
        if status == 409 and out.get("_card_id"):   # angelegt, nur noch nicht gezeigt
            e.update({"id": out["_card_id"], "type": "not_shown"})
        if body.get("kind") in FILE_KINDS and (status in (404, 405) or (
                status == 422 and "must be one of" in e["error"])):
            e["error"] = ("pdf, csv and md cards of a helper need a newer server (relay/show "
                          "without data/name); text, code, link and image cards work")
        return e
    return {"ok": True, **{k: out[k] for k in ("id", "kind", "from", "shown", "not_loaded") if k in out}}


def _valid_card(body: object) -> dict | None:
    """Local relay card: text, code or link (an image does not fit the request file)."""
    if not isinstance(body, dict) or body.get("kind") not in ("text", "code", "link"):
        return None
    text, title = body.get("text"), body.get("title", "")
    if not isinstance(text, str) or not text.strip() or len(text) > 4000:
        return None
    if not isinstance(title, str) or len(title) > 80:
        return None
    if body["kind"] == "link" and not re.fullmatch(r"https?://\S+", text.strip()):
        return None
    return {"kind": body["kind"], "text": text, "title": title}


def base_url(rel: dict) -> str:
    """<base>/api/activity/relay (the activity line endpoint) from the relay url."""
    return endpoint(rel, "x")[:-2]


def remote_activity(rel: dict, frm: str, body: dict, timeout: float = HTTP_TIMEOUT_S) -> dict:
    """0.22.2: one activity line of a helper {text, context?, faq?} under `from`."""
    status, out = _http(rel, "POST", base_url(rel), {"from": frm, **body}, timeout)
    if status >= 300 or out.get("ok") is False:
        return _err(status, out)
    return {"ok": True, **({"line": out["line"]} if isinstance(out.get("line"), str) else {})}


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


def _is_stale(o: dict) -> bool:
    """0.21.9: the server dropped this helper's queued updates as stale (operator.revise
    stale=true); `relay-next` / MCP `next` bring it like a decision answer."""
    from . import relay as vrelay
    return vrelay.is_stale(o)


def _is_user_turn(o: dict) -> bool:
    """0.22.0: the human spoke to this helper by name in the call (server: to_kind helper)."""
    return o.get("type") == "user_turn"


def _is_question_check(o: dict) -> bool:
    """0.22.1: the server asks whether a board/feed line of this helper is a question."""
    return o.get("type") == "question_check"


def _is_task(o: dict) -> bool:
    """0.24.0 a2a.py: an agent-to-agent task event (task / task_acked / task_due) in the inbox."""
    from . import a2a
    return o.get("type") in a2a.WAKE_TYPES


def _wanted(o: dict) -> bool:
    return (_is_decision(o) or _is_stale(o) or _is_user_turn(o) or _is_question_check(o)
            or _is_task(o))


def view(ev: dict) -> dict:
    """An inbox event as the helper sees it: a stale drop gets its clear text and hint."""
    if not _is_stale(ev):
        return ev
    from . import relay as vrelay
    return {**ev, "type": "revise", **vrelay.stale_fields(ev)}


def next_events(rel: dict, frm: str, timeout: float, *, match_id: str | None = None,
                match_ids: set | None = None, sleep=time.sleep) -> list[dict]:
    """Decision events for this helper (0.21.9: and stale drops of its updates); with
    match_id (or match_ids, 0.21.3) only those decisions (others stay queued). Waits up to `timeout` seconds; [] on timeout."""
    ids = set(match_ids or ()) | ({match_id} if match_id else set())
    want = (lambda o: _is_decision(o) and o.get("id") in ids) if ids else _wanted
    match_id = match_id or (next(iter(ids)) if ids else None)
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
    elif op == "state":   # 0.21.4: session state of a local helper (board.py)
        body = _valid_state(obj.get("body"))
    elif op == "card":    # 0.23.1: text/code/link card of a local helper
        body = _valid_card(obj.get("body"))
    else:
        return rid, frm, str(op), None
    return rid, frm, op, body
