"""`notify-me` / `call-me`: the server's notify_me (SMS with a single-use link, phone call
as follow-up) and call_me (phone call) for a CLI join (server voicehook-v4). WebRTC join:
POST /api/cli/<kind>, Bearer = its LiveKit token (expired -> pump token); bridge join:
/api/bridge/<kind> with its session. One JSON answer, never the token in the output."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import ClassVar

import pytest

from voicehook_agent import cli

JWT = "fake.lk.jwt"  # fake, low entropy (gitleaks)
PUMP = "fake-pump"


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
    answers: ClassVar[list] = []
    timeout = None

    def __init__(self, *a, **kw) -> None:
        self.headers = kw.get("headers") or {}
        FakeClient.timeout = kw.get("timeout")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, headers=None, timeout=None):
        FakeClient.calls.append((url, json, headers, self.headers))
        return FakeClient.answers.pop(0) if len(FakeClient.answers) > 1 else FakeClient.answers[0]


SMS_SENT = {"status": "sms_sent", "phone_call_id": "pc1", "billed_eur": 0.08, "link_valid_s": 600,
            "fallback_call_in_s": 120}


@pytest.fixture(autouse=True)
def _fake_http(monkeypatch):
    FakeClient.calls = []
    FakeClient.answers = [Resp(200, SMS_SENT)]
    monkeypatch.setattr(cli.httpx, "AsyncClient", FakeClient)


def _ctl():
    ctl = cli._Control("room-a", "claude-1", None, None, 0, 0)
    ctl.api_base, ctl.lk_jwt = "https://voicehook.ai", JWT
    return ctl


def test_request_shapes():
    assert cli.phone_request("notify_me", "done", None) == {"cmd": "notify_me", "reason": "done"}
    assert cli.phone_request("call_me", "blocked", "  CI  rot ") == {
        "cmd": "call_me", "reason": "blocked", "message": "CI rot"}
    for kind, reason, msg in (("notify_me", "scheduled", None), ("notify_me", "done", " "),
                              ("notify_me", "done", "x" * 121), ("call_me", "done", "x" * 201)):
        with pytest.raises(ValueError):
            cli.phone_request(kind, reason, msg)
    assert cli.phone_request("call_me", "done", "x" * 200)["message"] == "x" * 200


@pytest.mark.parametrize("argv,want", [
    (["notify-me", "--reason", "decision", "--message", "Merge ok?"],
     {"cmd": "notify_me", "reason": "decision", "message": "Merge ok?"}),
    (["call-me", "--reason", "done"], {"cmd": "call_me", "reason": "done"}),
])
def test_parser_builds_request(monkeypatch, argv, want):
    seen = {}
    monkeypatch.setattr(cli, "_run_client", lambda args: seen.setdefault("req", cli._client_request(args)) and 0)
    with pytest.raises(SystemExit):
        cli.main(argv)
    assert seen["req"] == want


def test_parser_rejects_bad_reason(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["notify-me", "--reason", "spam"])
    assert e.value.code == 2


def test_webrtc_join_posts_cli_route_with_its_livekit_token():
    out = asyncio.run(cli._control_handler(_ctl(), {"cmd": "notify_me", "reason": "done", "message": "Fertig"}))
    assert out == {"ok": True, "type": "notify_me", **SMS_SENT}
    url, body, headers, client_headers = FakeClient.calls[0]
    assert url == "https://voicehook.ai/api/cli/notify_me"
    assert body == {"reason": "done", "message": "Fertig"}
    assert headers == {"authorization": f"Bearer {JWT}"} and client_headers["x-vh-cli"] == cli._VERSION
    assert JWT not in json.dumps(out)


def test_call_me_waits_for_the_call():
    FakeClient.answers = [Resp(200, {"status": "answered", "phone_call_id": "pc2", "billed_eur": 0.2})]
    out = asyncio.run(cli._control_handler(_ctl(), {"cmd": "call_me", "reason": "blocked"}))
    assert out["ok"] and out["status"] == "answered"
    assert FakeClient.calls[0][0] == "https://voicehook.ai/api/cli/call_me"
    assert FakeClient.timeout == cli.PHONE_HTTP_TIMEOUT_S["call_me"] > 690


def test_refused_is_passed_on():
    FakeClient.answers = [Resp(200, {"status": "refused", "why": "no_account"})]
    out = asyncio.run(cli._phone_post(_ctl(), "notify_me", {"reason": "done"}))
    assert out == {"ok": True, "type": "notify_me", "status": "refused", "why": "no_account"}


def test_expired_livekit_token_falls_back_to_pump_token():
    ctl = _ctl()
    ctl.helper_pump = SimpleNamespace(token=PUMP)
    FakeClient.answers = [Resp(404, {"detail": "unknown or expired session"}), Resp(200, SMS_SENT)]
    out = asyncio.run(cli._phone_post(ctl, "notify_me", {"reason": "done"}))
    assert out["ok"] and [c[2]["authorization"] for c in FakeClient.calls] == [f"Bearer {JWT}", f"Bearer {PUMP}"]
    assert PUMP not in json.dumps(out)


def test_server_errors_are_passed_on():
    ctl = _ctl()
    FakeClient.answers = [Resp(422, {"detail": "message max 120 chars"})]
    assert asyncio.run(cli._phone_post(ctl, "notify_me", {"reason": "done"})) == {
        "ok": False, "type": "notify_me", "status": 422, "error": "message max 120 chars"}
    FakeClient.answers = [Resp(429, {"detail": "too many notify_me/call_me requests"})]
    assert asyncio.run(cli._phone_post(ctl, "call_me", {"reason": "done"}))["status"] == 429
    FakeClient.answers = [Resp(404, ValueError("html"))]
    assert "too old" in asyncio.run(cli._phone_post(ctl, "notify_me", {"reason": "done"}))["error"]


def test_bad_request_on_socket_path_sends_nothing():
    out = asyncio.run(cli._control_handler(_ctl(), {"cmd": "call_me", "reason": "scheduled"}))
    assert out["ok"] is False and out["type"] == "invalid" and FakeClient.calls == []


def test_bridge_join_uses_its_session():
    ctl = _ctl()
    sent = []

    class Bridge:
        async def post_phone(self, kind, body, timeout):
            sent.append((kind, body, timeout))
            return 200, {"status": "sms_sent"}

    ctl.room = Bridge()
    out = asyncio.run(cli._phone_post(ctl, "notify_me", {"reason": "done"}))
    assert out == {"ok": True, "type": "notify_me", "status": "sms_sent"}
    assert sent == [("notify_me", {"reason": "done"}, cli.PHONE_HTTP_TIMEOUT_S["notify_me"])]
    assert FakeClient.calls == []


def test_not_connected():
    ctl = cli._Control("room-a", "claude-1", None, None, 0, 0)
    out = asyncio.run(cli._phone_post(ctl, "call_me", {"reason": "done"}))
    assert out["ok"] is False and "not connected" in out["error"]
