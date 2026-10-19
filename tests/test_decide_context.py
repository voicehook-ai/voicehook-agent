"""0.21.0: background for Delta on decisions (`decide --context/--recent`, length limits, both
transports, the helper relay), verdict "unclear" in `next` and for helpers, and `--project`
on status/activity (board field, operator.activity field, relay body)."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from voicehook_agent import cli, relay
from voicehook_agent import relayops as ro
from voicehook_agent import transport as vt


def _args(**kw):
    base = {"cmd": "decide", "question": None, "option": [], "recommend": None,
            "id": None, "delete": False, "context": None, "recent": None}
    base.update(kw)
    return SimpleNamespace(**base)


CTX = "Fix für den Login. Ja: geht jetzt live. Nein: wartet bis morgen."


# ----- request building + limits ----------------------------------------------------------
def test_create_carries_context_and_recent():
    req = cli._client_request(_args(question="Deploy?", context=CTX, recent="  Oliver: mach\n weiter "))
    assert req == {"cmd": "decide", "question": "Deploy?", "context": CTX, "recent": "Oliver: mach weiter"}


def test_empty_context_is_left_out_on_create():
    assert cli._client_request(_args(question="Deploy?", context="  ")) == {
        "cmd": "decide", "question": "Deploy?"}


def test_update_with_context_only_and_clear():
    assert cli._client_request(_args(id="d0a1b2c3d", context=CTX)) == {
        "cmd": "decide", "id": "d0a1b2c3d", "context": CTX}
    assert cli._client_request(_args(id="d0a1b2c3d", context="", recent="neu")) == {
        "cmd": "decide", "id": "d0a1b2c3d", "context": "", "recent": "neu"}


@pytest.mark.parametrize("field, limit", [("context", 1500), ("recent", 600)])
def test_length_limits(field, limit):
    ok = cli._client_request(_args(question="q?", **{field: "a" * limit}))
    assert len(ok[field]) == limit
    with pytest.raises(ValueError, match=f"--{field}: max {limit} characters"):
        cli._client_request(_args(question="q?", **{field: "a" * (limit + 1)}))
    with pytest.raises(ValueError, match=f"--{field}"):
        cli._client_request(_args(id="d0a1b2c3d", **{field: "a" * (limit + 1)}))


def test_too_long_context_exits_before_sending(monkeypatch, capsys):
    def boom(*a, **k):
        raise AssertionError("must not contact the join")
    monkeypatch.setattr(cli.vsession, "resolve_socket", boom)
    with pytest.raises(SystemExit) as e:
        cli.main(["decide", "--question", "q?", "--context", "x" * 1501])
    assert e.value.code == 1
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False and "max 1500" in out["error"]


def test_cli_flags_reach_the_join(monkeypatch, capsys):
    sent = {}
    monkeypatch.setattr(cli.vsession, "resolve_socket", lambda s, wait: "/sock")
    monkeypatch.setattr(cli.vsession, "request",
                        lambda sock, r, timeout=None: sent.update(r) or {"ok": True, "id": "d1", "number": 1})
    with pytest.raises(SystemExit) as e:
        cli.main(["decide", "--question", "Deploy?", "--context", CTX, "--recent", "letzter Turn"])
    assert e.value.code == 0
    assert sent == {"cmd": "decide", "question": "Deploy?", "context": CTX, "recent": "letzter Turn"}


# ----- WebRTC RPC payload + bridge POST -----------------------------------------------------
class _Worker:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls: list[dict] = []

    async def perform_rpc(self, *, destination_identity, method, payload, response_timeout):
        self.calls.append(json.loads(payload))
        return self.replies.pop(0)

    async def publish_data(self, data, reliable=True, topic="", destination_identities=None):
        self.published = getattr(self, "published", []) + [(topic, json.loads(data))]


def _ctl(room):
    ctl = cli._Control("blau-tiger", "claude-x", relay.SayTracker(), relay.EchoSuppressor(False), 0.0)
    ctl.attach(room)
    return ctl


def test_rpc_payload_carries_context_and_recent():
    w = _Worker(['{"ok": true, "id": "d0a1b2c3d", "number": 1}'])
    room = SimpleNamespace(local_participant=w, remote_participants={
        "delta": SimpleNamespace(identity="delta-worker", kind=4)})
    asyncio.run(cli._control_handler(_ctl(room), {"cmd": "decide", "question": "Deploy?",
                                                  "context": CTX, "recent": "r"}))
    assert w.calls == [{"question": "Deploy?", "context": CTX, "recent": "r"}]


def test_rpc_update_and_helper_sender_keep_context():
    w = _Worker(['{"ok": true, "id": "d0a1b2c3d", "number": 1}'])
    room = SimpleNamespace(local_participant=w, remote_participants={
        "delta": SimpleNamespace(identity="delta-worker", kind=4)})
    asyncio.run(cli._decide_rpc(_ctl(room), {"id": "d0a1b2c3d", "context": "", "sender": "crown: SE"}))
    assert w.calls == [{"id": "d0a1b2c3d", "context": "", "sender": "crown: SE"}]


def test_bridge_decide_posts_api_bridge_decide():
    calls = []

    class _BridgeLike(SimpleNamespace):
        async def send_decide(self, body):
            calls.append(body)
            return {"ok": True, "id": "d0a1b2c3d", "number": 4}
    room = _BridgeLike(local_participant=_Worker([]), remote_participants={})
    out = asyncio.run(cli._control_handler(_ctl(room), {"cmd": "decide", "question": "Deploy?",
                                                        "context": CTX}))
    assert out == {"ok": True, "id": "d0a1b2c3d", "number": 4, "type": "decide"}
    assert calls == [{"question": "Deploy?", "context": CTX}]


def test_bridge_decide_error_status():
    class _Failing(SimpleNamespace):
        async def send_decide(self, body):
            raise vt.BridgeError("bridge send decide failed: HTTP 422 x", status=422,
                                 body={"detail": "context: max 1500 characters"})
    out = asyncio.run(cli._control_handler(_ctl(_Failing(remote_participants={})),
                                           {"cmd": "decide", "question": "q?"}))
    assert out == {"ok": False, "status": 422, "error": "context: max 1500 characters"}


def test_bridge_room_send_decide_http():
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append((req.url.path, req.headers.get("authorization"), json.loads(req.content)))
        return httpx.Response(200, json={"ok": True, "id": "d0a1b2c3d", "number": 2})

    async def go():
        room = vt.BridgeRoom("http://vh.test", "a-b-c-AB12", "x", name="C", model="m")
        room._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        room._session = "S3SSION"
        out = await room.send_decide({"question": "q?", "context": CTX, "recent": "r"})
        await room._http.aclose()
        return out
    assert asyncio.run(go()) == {"ok": True, "id": "d0a1b2c3d", "number": 2}
    assert seen == [("/api/bridge/decide", "Bearer S3SSION", {"question": "q?", "context": CTX,
                                                              "recent": "r"})]


# ----- helper relay ---------------------------------------------------------------------------
def test_relay_decide_body_and_local_validation():
    body = ro.decide_body({"cmd": "decide", "question": "q?", "context": CTX, "recent": "r"})
    assert body == {"question": "q?", "context": CTX, "recent": "r"}
    assert ro._valid_decide(body) == body
    assert ro._valid_decide({"question": "q?", "context": 3}) is None
    assert ro._valid_decide({"id": "d0a1b2c3d", "context": ""}) == {"id": "d0a1b2c3d", "context": ""}


def test_remote_relay_decide_sends_context(monkeypatch):
    seen = {}

    def fake_http(rel, method, url, body, timeout):
        seen.update(url=url, body=body)
        return 200, {"ok": True, "id": "d0a1b2c3d", "number": 3}
    monkeypatch.setattr(ro, "_http", fake_http)
    rel = {"url": "https://vh.test/api/activity/relay", "token": "fake.relay.test.only"}
    out = ro.remote_decide(rel, "SE Refill", ro.decide_body({"question": "q?", "context": CTX}))
    assert out == {"ok": True, "id": "d0a1b2c3d", "number": 3}
    assert seen["url"].endswith("/api/activity/relay/decide")
    assert seen["body"] == {"from": "SE Refill", "question": "q?", "context": CTX}


# ----- verdict "unclear" -----------------------------------------------------------------------
UNCLEAR = {"type": "decision", "id": "d0a1b2c3d", "number": 2, "verdict": "unclear", "by": "Oliver",
           "answer": "Oliver versteht #2 nicht, bitte einfacher neu stellen.",
           "text": "decision #2 unclear: Oliver versteht #2 nicht, bitte einfacher neu stellen."}


def test_unclear_reaches_next_unchanged():
    ctl = _ctl(SimpleNamespace(local_participant=_Worker([]), remote_participants={}))

    async def go():
        ctl.events.arm()
        ctl.events.put_nowait(dict(UNCLEAR))
        return await cli._control_handler(ctl, {"cmd": "next", "timeout": 0})
    out = asyncio.run(go())
    assert out["type"] == "decision" and out["verdict"] == "unclear"
    assert out["text"] == UNCLEAR["text"] and out["number"] == 2


def test_unclear_helper_copy_carries_text():
    ev = relay.helper_decision_event({**UNCLEAR, "sender": "crown: SE Refill"}, question="Merge?")
    assert ev["type"] == "helper_decision" and ev["verdict"] == "unclear"
    assert ev["text"] == UNCLEAR["answer"] and ev["from"] == "SE Refill" and ev["label"] == "crown"


def test_unclear_for_a_local_helper_goes_to_its_inbox(tmp_path):
    ctl = _ctl(SimpleNamespace(local_participant=_Worker([]), remote_participants={}))
    ctl.activity_path = tmp_path / "activity.log"
    ctl.relay_senders["crown: SE Refill"] = "SE Refill"
    ev = {**UNCLEAR, "sender": "crown: SE Refill"}

    async def go():
        ctl.events.arm()  # in a real call the agent already called say/next
        assert cli._route_helper_decision(ctl, ev) is True
        return await ctl.events.get(0)
    copy = asyncio.run(go())
    got = ro.take_jsonl(ro.answer_path(tmp_path, "SE Refill"), lambda o: True)
    assert got == [ev]
    assert copy["verdict"] == "unclear" and copy["text"] == UNCLEAR["answer"]


# ----- --project on status / activity ---------------------------------------------------------
def _status_args(**kw):
    base = {"cmd": "status", "text": None, "doing": None, "open": [], "done": [], "file": None,
            "faq": [], "project": None}
    base.update(kw)
    return SimpleNamespace(**base)


def test_status_project_goes_into_the_board():
    req = cli._client_request(_status_args(doing="Tests", project=" Sales  Engine "))
    assert req == {"cmd": "board", "board": {"doing": "Tests", "open": [], "done": [],
                                             "project": "Sales Engine"}}
    assert "project" not in cli._client_request(_status_args(doing="Tests"))["board"]


@pytest.mark.parametrize("bad", ["", "   ", "x" * 41])
def test_project_length(bad):
    with pytest.raises(ValueError, match="1 to 40"):
        cli._client_request(_status_args(doing="Tests", project=bad))


def test_status_project_alone_is_an_error():
    with pytest.raises(ValueError, match="goes with a board"):
        cli._client_request(_status_args(project="voicehook"))


def test_board_with_project_is_published_as_is():
    w = _Worker([])
    board = {"doing": "Tests", "open": [], "done": [], "project": "voicehook"}
    asyncio.run(cli._control_handler(_ctl(SimpleNamespace(local_participant=w, remote_participants={})),
                                     {"cmd": "board", "board": board}))
    assert w.published == [("operator.status", board)]


def test_relay_status_body_and_validation():
    body = ro.status_body({"doing": "x", "open": [], "done": [], "project": "kunden", "faq": [1]})
    assert body == {"doing": "x", "open": [], "done": [], "project": "kunden"}
    assert ro._valid_status(body) == body
    assert ro._valid_status({"doing": "x", "project": "y" * 41}) is None
    assert ro._valid_status({"doing": "x", "project": 5}) is None


def test_activity_project_request_and_join(tmp_path):
    args = SimpleNamespace(cmd="activity", text=["Tests", "laufen"], project="salesengine")
    assert cli._client_request(args) == {"cmd": "activity", "text": "Tests laufen",
                                         "project": "salesengine"}
    with pytest.raises(ValueError):
        cli._client_request(SimpleNamespace(cmd="activity", text=["x"], project="y" * 41))

    w = _Worker([])
    ctl = _ctl(SimpleNamespace(local_participant=w, remote_participants={}))
    ctl.activity_path = tmp_path / "activity.log"
    out = asyncio.run(cli._control_handler(ctl, {"cmd": "activity", "text": "Tests laufen",
                                                 "project": "salesengine"}))
    assert out["ok"] is True and out["project"] == "salesengine"
    assert asyncio.run(cli._publish_activity(ctl, ["17:00:00 note: Tests laufen"])) is True
    topic, payload = w.published[-1]
    assert topic == "operator.activity" and payload["project"] == "salesengine"
    # kept for the later packets of this join (hook lines), until another --project
    asyncio.run(cli._publish_activity(ctl, ["17:00:05 Bash: tests"]))
    assert w.published[-1][1]["project"] == "salesengine"


def test_activity_without_project_has_no_field(tmp_path):
    w = _Worker([])
    ctl = _ctl(SimpleNamespace(local_participant=w, remote_participants={}))
    asyncio.run(cli._publish_activity(ctl, ["17:00:00 note: x"]))
    assert "project" not in w.published[-1][1]
