"""0.19.0 `vocab add`: teach the call's speech recognition (server voicehook-v4
POST /api/bridge/vocab). WebRTC join: Bearer = its LiveKit token; bridge join: its session.
Add only, one JSON answer, never the token in the output."""
from __future__ import annotations

import asyncio
import json
from typing import ClassVar

import pytest

from voicehook_agent import cli

JWT = "fake.lk.jwt"  # fake, low entropy (gitleaks)


class Resp:
    def __init__(self, status: int, body) -> None:
        self.status_code = status
        self._body = body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class FakeClient:
    calls: ClassVar[list] = []
    answer = Resp(200, {"ok": True, "added": {"terms": 1, "fixes": 1}, "total": {"terms": 3, "fixes": 1},
                        "call_left": 98, "reloaded": True})

    def __init__(self, *a, **kw) -> None:
        self.headers = kw.get("headers") or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, headers=None, timeout=None):
        FakeClient.calls.append((url, json, headers, self.headers))
        return FakeClient.answer


@pytest.fixture(autouse=True)
def _fake_http(monkeypatch):
    FakeClient.calls = []
    FakeClient.answer = Resp(200, {"ok": True, "added": {"terms": 1, "fixes": 1}, "total": {"terms": 3, "fixes": 1},
                                   "call_left": 98, "reloaded": True})
    monkeypatch.setattr(cli.httpx, "AsyncClient", FakeClient)


def _ctl():
    ctl = cli._Control("room-a", "claude-1", None, None, 0, 0)
    ctl.api_base, ctl.op_jwt = "https://voicehook.ai", JWT
    return ctl


def test_request_shapes():
    assert cli.vocab_request("Trello", []) == {"cmd": "vocab", "terms": ["Trello"]}
    assert cli.vocab_request(" Trello ", ["Erzähli", "Erzähle"]) == {
        "cmd": "vocab", "fixes": [{"wrong": "Erzähli", "right": "Trello"}, {"wrong": "Erzähle", "right": "Trello"}]}
    for term, wrong in (("", []), ("x" * 51, []), ("Trello", ["trello"]), ("Trello", [" "])):
        with pytest.raises(ValueError):
            cli.vocab_request(term, wrong)


def test_parser_builds_request(monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "_run_client", lambda args: seen.setdefault("req", cli._client_request(args)) and 0)
    with pytest.raises(SystemExit):
        cli.main(["vocab", "add", "Trello", "--wrong", "Erzähli"])
    assert seen["req"] == {"cmd": "vocab", "fixes": [{"wrong": "Erzähli", "right": "Trello"}]}


def test_webrtc_join_posts_with_its_livekit_token():
    ctl = _ctl()
    out = asyncio.run(cli._control_handler(ctl, {"cmd": "vocab", "terms": ["Trello"]}))
    assert out["ok"] is True and out["type"] == "vocab" and out["added"] == {"terms": 1, "fixes": 1}
    url, body, headers, client_headers = FakeClient.calls[0]
    assert url == "https://voicehook.ai/api/bridge/vocab"
    assert body == {"terms": ["Trello"], "fixes": []}
    assert headers == {"authorization": f"Bearer {JWT}"} and client_headers["x-vh-cli"] == cli._VERSION
    assert JWT not in json.dumps(out)


def test_server_errors_are_passed_on():
    ctl = _ctl()
    FakeClient.answer = Resp(409, {"detail": "this call has no account (guest call)"})
    out = asyncio.run(cli._vocab_add(ctl, {"terms": ["Trello"], "fixes": []}))
    assert out == {"ok": False, "type": "vocab", "status": 409, "error": "this call has no account (guest call)"}
    FakeClient.answer = Resp(404, ValueError("html"))
    assert "too old" in asyncio.run(cli._vocab_add(ctl, {"terms": ["T"], "fixes": []}))["error"]


def test_bridge_join_uses_its_session():
    ctl = _ctl()
    sent = []

    class Bridge:
        async def post_vocab(self, body):
            sent.append(body)
            return 200, {"ok": True, "added": {"terms": 1, "fixes": 0}}

    ctl.room = Bridge()
    out = asyncio.run(cli._vocab_add(ctl, {"terms": ["Trello"], "fixes": []}))
    assert out["ok"] and sent == [{"terms": ["Trello"], "fixes": []}] and FakeClient.calls == []


def test_not_connected():
    ctl = cli._Control("room-a", "claude-1", None, None, 0, 0)
    out = asyncio.run(cli._vocab_add(ctl, {"terms": ["Trello"], "fixes": []}))
    assert out["ok"] is False and "not connected" in out["error"]
