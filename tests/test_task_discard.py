"""`voicehook-agent task discard <nr>` (0.35.8, ownerops.py): the running join POSTs
/api/cli/tasks/{nr}/discard {} with ITS LiveKit token (server voicehook-v4
kanban_routes._owner_op_event: discard marks the card as discarded, never a deletion). The
token never shows up in an answer, stdout, stderr or the log; no DELETE route, ever."""
from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace
from typing import ClassVar

import httpx
import pytest

from voicehook_agent import cli, ownerops
from voicehook_agent import session as vsession

JWT = "fake.lk.jwt-discard"  # fake, low entropy (gitleaks)
PUMP = "fake-pump-discard"


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
    answer: ClassVar[object] = None

    def __init__(self, *a, **kw) -> None:
        self.headers = kw.get("headers") or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, headers=None):
        FakeClient.calls.append((url, json, headers, self.headers))
        if isinstance(FakeClient.answer, Exception):
            raise FakeClient.answer
        return FakeClient.answer

    async def delete(self, *a, **kw):   # must never be used
        raise AssertionError("DELETE is never sent")


CARD = {"nr": 9, "marks": ["discarded"], "assignee": {"key": "kai", "label": "Kai"}}


@pytest.fixture(autouse=True)
def _fake_http(monkeypatch):
    FakeClient.calls = []
    FakeClient.answer = Resp(200, {"ok": True, "card": CARD})
    monkeypatch.setattr(ownerops.httpx, "AsyncClient", FakeClient)


def _ctl(jwt: str = JWT):
    ctl = cli._Control("room-a", "claude-1", None, None, 0, 0)
    ctl.api_base, ctl.lk_jwt = "https://voicehook.ai", jwt
    ctl.helper_pump = SimpleNamespace(token=PUMP)
    return ctl


def _handle(ctl, req):
    return asyncio.run(cli._control_handler(ctl, req))


REQ = {"cmd": "task_op", "op": "discard", "nr": 9}


def test_request_shape():
    assert ownerops.op_request("discard", 9) == REQ
    assert ownerops.op_request("discard", 9, "ignored") == REQ
    for op, nr in (("discard", 0), ("discard", True), ("discard", "9"), ("delete", 9), (None, 9)):
        with pytest.raises(ownerops.AssignError):
            ownerops.op_request(op, nr)


def test_join_posts_discard_with_its_livekit_token():
    out = _handle(_ctl(), REQ)
    assert out == {"ok": True, "op": "discard", "nr": 9, "status": 200}
    url, body, headers, client_headers = FakeClient.calls[0]
    assert url == "https://voicehook.ai/api/cli/tasks/9/discard"
    assert body == {}
    assert headers == {"authorization": f"Bearer {JWT}"}
    assert client_headers["x-vh-cli"] == cli._VERSION
    assert len(FakeClient.calls) == 1   # never retried with the pump token


def test_unknown_op_on_socket_sends_nothing():
    out = _handle(_ctl(), {"cmd": "task_op", "op": "delete", "nr": 9})
    assert out["ok"] is False and FakeClient.calls == []


def test_no_livekit_token_sends_nothing():
    out = _handle(_ctl(jwt=""), REQ)
    assert out["ok"] is False and out["op"] == "discard" and out["error"] == "no_livekit_join"
    assert "VOICEHOOK_AGENT_KEY" in out["hint"] and FakeClient.calls == []


@pytest.mark.parametrize("status,body,error,hint", [
    (403, {"ok": False, "code": 403, "error": "owner_only"}, "owner_only", ownerops.HINT_OWNER_ONLY),
    (403, {"ok": False, "code": 403, "error": "guest_present"}, "guest_present", ownerops.HINT_GUEST),
    (429, {"ok": False, "code": 429, "error": "rate_limited"}, "rate_limited", ownerops.HINT_RATE),
    (409, {"ok": False, "code": 409, "error": "invalid_transition"}, "invalid_transition", None),
])
def test_server_errors_are_passed_on(status, body, error, hint):
    FakeClient.answer = Resp(status, body)
    out = _handle(_ctl(), REQ)
    assert out["ok"] is False and out["op"] == "discard" and out["nr"] == 9
    assert out["status"] == status and out["error"] == error and out.get("hint") == hint


def test_token_never_in_answer_stdout_or_log(capsys, caplog):
    caplog.set_level(logging.DEBUG)
    outs = []
    for a in (Resp(200, {"ok": True, "card": CARD}), Resp(403, {"error": "owner_only"}),
              httpx.ReadTimeout(JWT)):
        FakeClient.answer = a
        outs.append(_handle(_ctl(), REQ))
    seen = capsys.readouterr()
    blob = json.dumps(outs) + seen.out + seen.err + caplog.text
    assert JWT not in blob and PUMP not in blob


def _sock(monkeypatch, ctl=None, missing=False):
    reqs = []

    def resolve(session, wait=0):
        if missing:
            raise vsession.SessionError("no running join")
        return "/fake/ctl.sock"

    def request(sock, req, timeout=None):
        reqs.append(req)
        return asyncio.run(cli._control_handler(ctl, req))

    monkeypatch.setattr(vsession, "resolve_socket", resolve)
    monkeypatch.setattr(vsession, "request", request)
    return reqs


def _main(argv) -> int:
    with pytest.raises(SystemExit) as e:
        cli.main(argv)
    return e.value.code


def test_cli_one_json_line_ok(monkeypatch, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    reqs = _sock(monkeypatch, _ctl())
    assert _main(["task", "discard", "9", "--session", "room-a"]) == 0
    out = capsys.readouterr()
    lines = out.out.strip().splitlines()
    assert len(lines) == 1 and json.loads(lines[0]) == {
        "ok": True, "op": "discard", "nr": 9, "status": 200}
    assert reqs == [REQ]
    assert JWT not in out.out + out.err + caplog.text


def test_cli_owner_only_exit_1(monkeypatch, capsys):
    FakeClient.answer = Resp(403, {"ok": False, "code": 403, "error": "owner_only"})
    _sock(monkeypatch, _ctl())
    assert _main(["task", "discard", "9"]) == 1
    got = json.loads(capsys.readouterr().out)
    assert got["op"] == "discard" and got["hint"] == ownerops.HINT_OWNER_ONLY


def test_cli_no_join_hint(monkeypatch, capsys):
    _sock(monkeypatch, missing=True)
    assert _main(["task", "discard", "9", "--wait", "0"]) == 3
    got = json.loads(capsys.readouterr().out)
    assert got["op"] == "discard" and got["error"] == "no_join"
    assert "VOICEHOOK_AGENT_KEY" in got["hint"] and FakeClient.calls == []


def test_cli_old_join_knows_only_assign(monkeypatch, capsys):
    monkeypatch.setattr(vsession, "resolve_socket", lambda s, wait=0: "/fake/ctl.sock")
    monkeypatch.setattr(vsession, "request", lambda s, r, timeout=None: {
        "ok": False, "op": "assign", "nr": 9, "status": 0, "error": "head is empty"})
    assert _main(["task", "discard", "9"]) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "join_too_old"


def test_assign_alias_still_works():
    out = _handle(_ctl(), {"cmd": "task_assign", "nr": 9, "head": "Kai"})
    assert out["ok"] and out["op"] == "assign"
    assert FakeClient.calls[0][0].endswith("/api/cli/tasks/9/assign")
    assert FakeClient.calls[0][1] == {"assignee": "Kai"}
