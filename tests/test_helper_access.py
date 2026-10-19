"""0.17.0 helper access (server voicehook-v4 /api/helper/*): a WebRTC join gets the helper
credential + pump token from the server, writes relay.json and publishes the helpers'
packets as itself; helpers use it like a bridge relay (relayops, 0.16.0); another machine
gets the credential over ssh with helper-import."""
from __future__ import annotations

import asyncio
import io
import json
import os
import stat
from pathlib import Path

import pytest

from voicehook_agent import activity, cli
from voicehook_agent import helperaccess as vhelp
from voicehook_agent import multisession as ms

URL = "https://voicehook.ai/api/activity/relay"
PUMP_URL = "https://voicehook.ai/api/helper/pump"
TOKEN = "fake.relay.test.only"  # fake, low entropy (gitleaks)
PUMP = "fake.pump.test.only"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("CLAUDE_PID", "VOICEHOOK_OWNER_PID", ms.LABEL_ENV, ms.MACHINE_LABEL_ENV, ms.NO_RELAY_ENV):
        monkeypatch.delenv(k, raising=False)


class Resp:
    def __init__(self, status: int, body: dict) -> None:
        self.status_code = status
        self._body = body

    def json(self) -> dict:
        return self._body


class FakeHttp:
    """httpx.AsyncClient stand-in: GET answers from a list, POSTs are recorded."""

    def __init__(self, gets: list[Resp], post_status: int = 200, post_body: dict | None = None) -> None:
        self.gets = gets
        self.posts: list[tuple[str, dict, dict]] = []
        self.get_calls: list[tuple[str, dict]] = []
        self.post_status = post_status
        self.post_body = post_body or {"ok": True}

    async def get(self, url, params=None, headers=None, timeout=None):
        self.get_calls.append((url, headers or {}))
        if not self.gets:
            return Resp(200, {"items": [], "ended": True})
        return self.gets.pop(0)

    async def post(self, url, json=None, headers=None, timeout=None):
        self.posts.append((url, json or {}, headers or {}))
        return Resp(self.post_status, self.post_body)


# ---- credential ------------------------------------------------------------------------ #
def test_mint_parses_credential_and_pump_never_logs_token():
    http = FakeHttp([], post_body={"ok": True, "token": TOKEN, "url": URL, "expires_in": 3600,
                                   "label": "Mac", "transport": "cli", "pump": {"token": PUMP, "url": PUMP_URL}})
    rel = asyncio.run(vhelp.mint(http, "https://voicehook.ai/", "lk.jwt.x", "Mac"))
    assert rel == {"token": TOKEN, "url": URL, "expires_in": 3600, "pump": {"token": PUMP, "url": PUMP_URL}}
    url, body, headers = http.posts[0]
    assert url == "https://voicehook.ai/api/helper/credential" and body == {"label": "Mac", "rotate": False}
    assert headers["authorization"] == "Bearer lk.jwt.x"
    old = FakeHttp([], post_status=404, post_body={"detail": "Not Found"})
    assert asyncio.run(vhelp.mint(old, "https://voicehook.ai", "x", "Mac")) is None   # old server


def test_join_mints_once_and_writes_relay(monkeypatch):
    ctl = cli._Control("room-a", "claude-1", None, None, 0, 0)
    http = FakeHttp([], post_body={"ok": True, "token": TOKEN, "url": URL, "expires_in": 3600,
                                   "pump": {"token": PUMP, "url": PUMP_URL}})
    ctl.helper_http = http
    rel = asyncio.run(cli._helper_hub(ctl, "https://voicehook.ai", "lk.jwt.x"))
    assert rel == {"token": TOKEN, "url": URL, "expires_in": 3600}
    assert ctl.helper_pump.token == PUMP and ctl.helper_pump.url == PUMP_URL
    assert asyncio.run(cli._helper_hub(ctl, "https://voicehook.ai", "other.jwt")) is rel   # reconnect: reuse
    assert len(http.posts) == 1
    path = ms.write_relay(rel["url"], rel["token"], "room-a", rel["expires_in"])
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert ms.load_relay()["token"] == TOKEN


def test_old_server_keeps_local_relay():
    ctl = cli._Control("room-a", "claude-1", None, None, 0, 0)
    ctl.helper_http = FakeHttp([], post_status=404)
    assert asyncio.run(cli._helper_hub(ctl, "https://voicehook.ai", "lk.jwt.x")) is None
    assert ctl.helper_pump is None


# ---- pump ------------------------------------------------------------------------------ #
def test_pump_publishes_helper_packets_as_this_join():
    items = [{"kind": "status", "payload": {"sub": "Mac: A", "doing": "Ring", "open": [], "done": []}},
             {"kind": "activity", "line": "03:04:05 relay: Helfer Mac: A: Tests grün"},
             {"kind": "decide", "id": "r1", "body": {"question": "Deploy?", "sender": "Mac: A"}},
             {"kind": "status", "payload": {"doing": "kein sub"}},          # never the main board
             {"kind": "decide", "id": "r2", "body": {"question": "ohne sender"}}]   # never own question
    http = FakeHttp([Resp(200, {"items": items, "ended": False}), Resp(200, {"items": [], "ended": True})])
    published, lines, decided = [], [], []

    async def publish(p):
        published.append(p)

    async def decide(body):
        decided.append(body)
        return {"ok": True, "id": "d00000001", "number": 1}
    pump = vhelp.Pump("https://voicehook.ai", PUMP_URL, PUMP)
    asyncio.run(pump.run(http, publish_status=publish, decide=decide, append_activity=lines.append,
                         stop=asyncio.Event()))
    assert published == [items[0]["payload"]]
    assert len(lines) == 1 and lines[0].endswith(" relay: Helfer Mac: A: Tests grün")
    assert activity._LINE_RX.match(lines[0])
    assert decided == [items[2]["body"]]
    assert http.posts == [(PUMP_URL + "/result", {"id": "r1", "result": {"ok": True, "id": "d00000001", "number": 1}},
                           {"authorization": f"Bearer {PUMP}"})]
    assert pump.ended and all(h["authorization"] == f"Bearer {PUMP}" for _u, h in http.get_calls)


def test_pump_stops_on_unknown_token_and_end_posts_once():
    http = FakeHttp([Resp(404, {"detail": "unknown pump token"})])
    pump = vhelp.Pump("https://voicehook.ai", PUMP_URL, PUMP)

    async def nop(*_a):
        return {}
    asyncio.run(pump.run(http, publish_status=nop, decide=nop, append_activity=lambda _l: None,
                         stop=asyncio.Event()))
    assert pump.ended
    p2 = vhelp.Pump("https://voicehook.ai", PUMP_URL, PUMP)
    http2 = FakeHttp([])
    asyncio.run(p2.end(http2))
    asyncio.run(p2.end(http2))
    assert [u for u, _b, _h in http2.posts] == [PUMP_URL + "/end"]


def test_answer_goes_to_helper_inbox():
    http = FakeHttp([])
    ev = {"type": "decision", "id": "d1", "verdict": "yes", "sender": "Mac: A", "by": "Oliver"}
    asyncio.run(vhelp.Pump("https://voicehook.ai", PUMP_URL, PUMP).answer(http, "Mac: A", ev))
    assert http.posts == [(PUMP_URL + "/answer", {"sender": "Mac: A", "event": ev}, {"authorization": f"Bearer {PUMP}"})]


def test_answer_returns_server_copy_into_own_next():
    """0.18.1: the server answers /answer with the copy helper_decision; it lands in our next."""
    copy = {"type": "helper_decision", "from": "SE To-dos", "label": "Mac", "id": "d1", "number": 2,
            "question": "Deploy?", "verdict": "yes", "by": "Oliver", "ts": 1.0}
    ctl = cli._Control("room-a", "claude-1", None, None, 0, 0)
    ctl.helper_pump = vhelp.Pump("https://voicehook.ai", PUMP_URL, PUMP)
    ctl.helper_http = FakeHttp([], post_body={"ok": True, "helper_decision": copy})
    ev = {"type": "decision", "id": "d1", "number": 2, "verdict": "yes", "sender": "Mac: SE To-dos", "by": "Oliver"}

    async def go():
        ctl.events.arm()
        await cli._answer_helper(ctl, "Mac: SE To-dos", ev)
        return await ctl.events.get(0)
    got = asyncio.run(go())
    assert {k: v for k, v in got.items() if k != "hint"} == copy and "SendMessage" in got["hint"]
    # older server (no copy): built from the worker event, sender split at ": "
    ctl.helper_http = FakeHttp([], post_body={"ok": True})
    got = asyncio.run(go())
    assert (got["type"], got["from"], got["label"], got["question"], got["verdict"]) == (
        "helper_decision", "SE To-dos", "Mac", "", "yes")
    assert asyncio.run(vhelp.Pump("https://voicehook.ai", PUMP_URL, PUMP).answer(
        FakeHttp([], post_status=503), "Mac: A", ev)) is None


def test_helper_decision_event_other_and_changed():
    from voicehook_agent import relay
    ev = relay.helper_decision_event({"type": "decision", "id": "d2", "number": 1, "verdict": "other",
                                      "answer": "lieber Blau", "changed": True, "by": "O", "sender": "solo"})
    assert (ev["from"], ev["label"], ev["text"], ev["changed"]) == ("solo", "", "lieber Blau", True)
    assert "option" not in ev


def test_rotate_adopts_new_pump_token():
    http = FakeHttp([], post_body={"ok": True, "token": "new.tok", "url": URL, "expires_in": 60,
                                   "pump": {"token": "new.pump", "url": PUMP_URL}})
    pump = vhelp.Pump("https://voicehook.ai", PUMP_URL, PUMP)
    out = asyncio.run(pump.credential(http, "crown", rotate=True))
    assert out["token"] == "new.tok" and pump.token == "new.pump"
    assert http.posts[0][1] == {"label": "crown", "rotate": True} and http.posts[0][2]["authorization"] == f"Bearer {PUMP}"


# ---- another machine: helper-import ---------------------------------------------------- #
def test_helper_import_writes_relay_without_foreign_owner(monkeypatch, capsys):
    raw = json.dumps({"ok": True, "type": "helper_credential", "token": TOKEN, "url": URL,
                      "expires_in": 3600, "label": "crown"})
    monkeypatch.setattr("sys.stdin", io.StringIO(raw))
    with pytest.raises(SystemExit) as e:
        cli.main(["helper-import"])
    assert e.value.code == 0
    out = json.loads(capsys.readouterr().out)
    assert TOKEN not in json.dumps(out)
    d = json.loads(Path(out["path"]).read_text())
    assert d["owner_pid"] == 0 and d["owner_session"] == "" and d["token"] == TOKEN
    assert stat.S_IMODE(os.stat(out["path"]).st_mode) == 0o600
    assert ms.load_relay()["token"] == TOKEN


def test_helper_import_accepts_relay_json_copy_and_rejects_garbage(monkeypatch):
    src = {"url": URL, "token": TOKEN, "room": "room-a", "expires_at": 9_999_999_999,
           "owner_pid": 424242, "owner_session": "abc"}
    path = vhelp.import_credential(json.dumps(src))
    d = json.loads(Path(path).read_text())
    assert d["owner_pid"] == 0 and d["room"] == "room-a"
    for bad in ("", "nope", json.dumps({"url": URL})):
        with pytest.raises(ValueError):
            vhelp.import_credential(bad)
