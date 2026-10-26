"""board-hygiene CLI half: ttl_s, operator.leave, hidden/closed/unresolved,
waiting-Pflichtfelder, verdict expired, pump kind leave (PR #703)."""
from __future__ import annotations

import asyncio
import http.server
import json
import os
import threading
from types import SimpleNamespace

import pytest

from voicehook_agent import boardlist, cli, relay
from voicehook_agent import helperaccess as vhelp
from voicehook_agent import multisession as ms
from voicehook_agent import relayops as ro


# --------------------------------------------------------------------------- #
# shared fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _plain_check_off(monkeypatch):
    monkeypatch.setenv("VOICEHOOK_PLAIN_CHECK", "0")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    for k in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_PID", "VOICEHOOK_OWNER_PID",
              ms.LABEL_ENV, ms.MACHINE_LABEL_ENV, ms.NO_RELAY_ENV, "VOICEHOOK_SESSION"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("VOICEHOOK_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))


TOKEN = "fake.relay.hygiene.only"  # fake, low entropy (gitleaks)
PUMP_URL = "https://voicehook.ai/api/helper/pump"
PUMP = "fake.pump.hygiene.only"


# --------------------------------------------------------------------------- #
# fake relay server (reused from test_relay_decide.py pattern)
# --------------------------------------------------------------------------- #
class _Fake:
    def __init__(self):
        self.calls: list[dict] = []
        self.replies: dict[str, list[tuple[int, dict]]] = {}

    def reply(self, name, status, body):
        self.replies.setdefault(name, []).append((status, body))


@pytest.fixture
def server():
    from urllib.parse import parse_qs, urlsplit
    fake = _Fake()

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _serve(self):
            u = urlsplit(self.path)
            n = int(self.headers.get("content-length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}") if n else None
            name = u.path.rsplit("/", 1)[-1]
            fake.calls.append({"method": self.command, "path": u.path, "query": parse_qs(u.query),
                               "body": body})
            q = fake.replies.get(name) or [(200, {"ok": True} if name != "next" else {"events": []})]
            status, out = q.pop(0) if len(q) > 1 else q[0]
            data = json.dumps(out).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = _serve

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    fake.url = f"http://127.0.0.1:{srv.server_address[1]}/api/activity/relay"
    try:
        yield fake
    finally:
        srv.shutdown()
        srv.server_close()


def _remote(fake, owner="JOINER"):
    return ms.write_relay(fake.url, TOKEN, "room-a", 3600, owner_pid=os.getpid(),
                          owner_session=ms.session_hash(owner))


def _helper(monkeypatch, sid="other-1", label="SE Refill"):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", sid)
    if label:
        monkeypatch.setenv(ms.LABEL_ENV, label)


def _cli(argv, capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(argv)
    out, err = capsys.readouterr()
    lines = [json.loads(x) for x in out.splitlines() if x.strip()]
    return e.value.code, lines, err


# --------------------------------------------------------------------------- #
# 1. decide --ttl-s sends ttl_s in body
# --------------------------------------------------------------------------- #
def test_decide_ttl_s_in_body(monkeypatch, server, capsys):
    _remote(server)
    _helper(monkeypatch)
    server.reply("decide", 200, {"ok": True, "id": "d0a1b2c3d", "number": 3})
    rc, _lines, _ = _cli(["decide", "--question", "Deploy jetzt?", "--ttl-s", "600"], capsys)
    assert rc == 0
    body = server.calls[-1]["body"]
    assert body["ttl_s"] == 600


def test_decide_ttl_s_in_valid_decide_body():
    body = {"question": "Q?", "ttl_s": 300}
    out = ro._valid_decide(body)
    assert out is not None and out["ttl_s"] == 300


def test_decide_ttl_s_invalid_rejected():
    assert ro._valid_decide({"question": "Q?", "ttl_s": 0}) is None  # < 1
    assert ro._valid_decide({"question": "Q?", "ttl_s": "600"}) is None  # wrong type
    assert ro._valid_decide({"question": "Q?", "ttl_s": True}) is None  # bool is int, reject


# --------------------------------------------------------------------------- #
# 2. delete responds with {deleted, hidden} - CLI reads and outputs them
# --------------------------------------------------------------------------- #
def test_delete_response_deleted_hidden(monkeypatch, server, capsys):
    _remote(server)
    _helper(monkeypatch)
    server.reply("decide", 200, {"ok": True, "deleted": True, "hidden": False})
    rc, lines, _ = _cli(["decide", "--id", "d0a1b2c3d", "--delete"], capsys)
    assert rc == 0
    assert lines[0]["ok"] is True
    assert lines[0]["deleted"] is True
    assert lines[0]["hidden"] is False


def test_delete_response_hidden_true(monkeypatch, server, capsys):
    _remote(server)
    _helper(monkeypatch)
    server.reply("decide", 200, {"ok": True, "hidden": True, "deleted": False})
    rc, lines, _ = _cli(["decide", "--id", "d0a1b2c3d", "--delete"], capsys)
    assert rc == 0 and lines[0]["hidden"] is True and lines[0]["deleted"] is False


def test_remote_decide_passes_deleted_hidden():
    """Unit: remote_decide includes deleted/hidden from server response."""
    from unittest import mock

    class _FakeResp:
        status = 200
        def read(self): return json.dumps({"ok": True, "id": "d1", "deleted": True, "hidden": False}).encode()
        def __enter__(self): return self
        def __exit__(self, *a): pass

    rel = {"url": "http://x/api/activity/relay", "token": "t"}
    with mock.patch("urllib.request.urlopen", return_value=_FakeResp()):
        out = ro.remote_decide(rel, "me", {"id": "d1", "delete": True})
    assert out == {"ok": True, "id": "d1", "deleted": True, "hidden": False}


# --------------------------------------------------------------------------- #
# 3. status meta ttl_s per item + expired items filtered
# --------------------------------------------------------------------------- #
def test_status_meta_ttl_s_in_body(monkeypatch, server, capsys):
    _remote(server)
    _helper(monkeypatch)
    rc, _lines, _ = _cli(["status", "--doing", "Tests fixen", "--ttl-s", "300"], capsys)
    assert rc == 0
    body = server.calls[-1]["body"]
    # meta should carry ttl_s for doing
    meta = body.get("meta") or {}
    doing_meta = meta.get("doing") or {}
    assert doing_meta.get("ttl_s") == 300


def test_status_body_filters_expired_items():
    import time
    now = time.time()
    board = {
        "doing": "Aktiv",
        "open": ["Task A", "Task B"],
        "done": [],
        "meta": {
            "doing": {"context": "ctx", "ttl_s": 60, "_expires_at": now - 10},  # expired
            "open": [
                {"context": "ctx A", "ttl_s": 60, "_expires_at": now + 100},  # valid
                {"context": "ctx B", "ttl_s": 60, "_expires_at": now - 5},   # expired
            ],
        }
    }
    body = ro.status_body(board, now=now)
    assert "meta" in body
    # doing expired -> filtered out
    assert "doing" not in body["meta"]
    # open: first valid, second expired
    open_meta = body["meta"].get("open") or []
    assert len(open_meta) == 1
    assert open_meta[0].get("context") == "ctx A"
    # _expires_at should not be in output
    assert "_expires_at" not in open_meta[0]


def test_status_body_no_filter_without_now():
    """Without now= argument, no filtering happens (backward compat)."""
    import time
    now = time.time()
    board = {
        "doing": "Aktiv",
        "open": ["Task A"],
        "meta": {
            "doing": {"context": "ctx", "_expires_at": now - 100},  # expired but no now passed
        }
    }
    body = ro.status_body(board)  # no now argument
    # meta is passed through including expired (no filtering)
    assert "doing" in (body.get("meta") or {})


# --------------------------------------------------------------------------- #
# 4. operator.leave published on leave command
# --------------------------------------------------------------------------- #
def test_leave_publishes_operator_leave():
    published = []

    class _FakeLocal:
        async def publish_data(self, data, reliable=True, topic=""):
            published.append((topic, json.loads(data)))

    ctl = cli._Control("room", "claude-x", relay.SayTracker(), relay.EchoSuppressor(False), 0.0)
    ctl.attach(SimpleNamespace(local_participant=_FakeLocal(), remote_participants={}))

    async def run():
        return await cli._control_handler(ctl, {"cmd": "leave"})

    asyncio.run(run())
    topics = [t for t, _ in published]
    assert "operator.leave" in topics
    leave_payloads = [p for t, p in published if t == "operator.leave"]
    assert len(leave_payloads) == 1 and leave_payloads[0] == {}


# --------------------------------------------------------------------------- #
# 5. pump kind "leave" -> operator.leave {sub}
# --------------------------------------------------------------------------- #
def test_pump_leave_kind_calls_publish_leave():
    items = [{"kind": "leave", "payload": {"sub": "Mac: Scout"}}]
    http_fake = _FakeHttp([_Resp(200, {"items": items, "ended": True})])
    published, left = [], []

    async def publish_status(p):
        published.append(p)

    async def publish_leave(p):
        left.append(p)

    pump = vhelp.Pump("https://voicehook.ai", PUMP_URL, PUMP)
    asyncio.run(pump.run(http_fake, publish_status=publish_status,
                         decide=_nop_decide, append_activity=lambda _l: None,
                         stop=asyncio.Event(), publish_leave=publish_leave))
    assert published == []  # no status published
    assert left == [{"sub": "Mac: Scout"}]


def test_pump_leave_without_callback_is_ignored():
    """When no publish_leave callback is given, the leave item is silently skipped."""
    items = [{"kind": "leave", "payload": {"sub": "Mac: Scout"}}]
    http_fake = _FakeHttp([_Resp(200, {"items": items, "ended": True})])
    published = []

    async def publish_status(p):
        published.append(p)

    pump = vhelp.Pump("https://voicehook.ai", PUMP_URL, PUMP)
    asyncio.run(pump.run(http_fake, publish_status=publish_status,
                         decide=_nop_decide, append_activity=lambda _l: None,
                         stop=asyncio.Event()))  # no publish_leave
    assert published == []  # nothing published, no crash


# --------------------------------------------------------------------------- #
# 6. board/recent passthrough: hidden, closed, unresolved, ttl_s, expires_in_s
# --------------------------------------------------------------------------- #
def test_board_merged_passes_through_hygiene_fields():
    """Server items with hidden/closed/unresolved/ttl_s/expires_in_s pass through."""
    server_listing = {
        "ok": True,
        "open": [{"id": "d1", "number": 1, "question": "Q?", "hidden": True,
                  "ttl_s": 300, "expires_in_s": 250}],
        "recent": [{"id": "d2", "number": 2, "question": "R?", "closed": True,
                    "unresolved": False, "expires_in_s": 0}],
        "boards": [],
    }
    lg = boardlist.Ledger()
    out = lg.merged(server_listing)
    assert out["open"][0]["hidden"] is True
    assert out["open"][0]["ttl_s"] == 300
    assert out["open"][0]["expires_in_s"] == 250
    assert out["recent"][0]["closed"] is True
    assert out["recent"][0]["unresolved"] is False


# --------------------------------------------------------------------------- #
# 7. verdict "expired" accepted (not an error)
# --------------------------------------------------------------------------- #
def test_verdict_expired_accepted():
    ev = relay.helper_decision_event(
        {"type": "decision", "id": "d1", "number": 1, "verdict": "expired",
         "by": "server", "sender": "Mac: Scout"},
        question="Q?")
    assert ev["verdict"] == "expired"
    # No extra "text" field (expired doesn't produce text like "unclear")
    # (server may or may not send text, just verify no crash)


def test_verdict_expired_in_clean_meta():
    """clean_meta still accepts items that may carry ttl_s in the meta."""
    from voicehook_agent import relay as vrelay
    m = vrelay.clean_meta({"context": "ctx", "ttl_s": 120})
    assert m is not None and m["ttl_s"] == 120


# --------------------------------------------------------------------------- #
# 8. status --waiting without --question AND --context -> CLI error (not sent)
# --------------------------------------------------------------------------- #
def test_waiting_without_question_and_context_gives_error(monkeypatch, server, capsys):
    """--waiting without --question AND --context (and without --decision) must error."""
    _remote(server)
    _helper(monkeypatch)
    rc, lines, _err = _cli(["status", "--waiting", "Freigabe für Deploy?"], capsys)
    assert rc == 2
    assert lines[0]["ok"] is False
    assert server.calls == []  # nothing sent


def test_waiting_with_question_but_no_context_gives_error(monkeypatch, server, capsys):
    """--waiting --question without --context should error clearly."""
    _remote(server)
    _helper(monkeypatch)
    rc, lines, err = _cli(["status", "--waiting", "Freigabe?",
                           "--question", "Deploy jetzt?"], capsys)
    assert rc == 2
    assert lines[0]["ok"] is False
    assert "context" in lines[0]["error"].lower() or "context" in err.lower()
    assert server.calls == []


# --------------------------------------------------------------------------- #
# helpers for pump tests
# --------------------------------------------------------------------------- #
class _Resp:
    def __init__(self, status: int, body: dict) -> None:
        self.status_code = status
        self._body = body

    def json(self) -> dict:
        return self._body


class _FakeHttp:
    def __init__(self, gets: list[_Resp], post_status: int = 200,
                 post_body: dict | None = None) -> None:
        self.gets = list(gets)
        self.posts: list = []
        self.post_status = post_status
        self.post_body = post_body or {"ok": True}

    async def get(self, url, params=None, headers=None, timeout=None):
        if not self.gets:
            return _Resp(200, {"items": [], "ended": True})
        return self.gets.pop(0)

    async def post(self, url, json=None, headers=None, timeout=None):
        self.posts.append((url, json or {}, headers or {}))
        return _Resp(self.post_status, self.post_body)


async def _nop_decide(body):
    return {"ok": True, "id": "d1", "number": 1}
