"""`voicehook-agent task assign <nr> <head>` (0.35.8, ownerops.py): the running join POSTs
/api/cli/tasks/{nr}/assign {assignee} with ITS LiveKit token (server voicehook-v4
kanban_routes._owner_actor: owner ops only for a join with the vhak_ key). The token never
shows up in an answer, stdout, stderr or the log."""
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

JWT = "fake.lk.jwt-assign"  # fake, low entropy (gitleaks)
PUMP = "fake-pump-assign"


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


CARD = {"nr": 7, "assignee": {"key": "kai", "label": "Kai", "kind": "agent"}}


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


def test_request_shape():
    assert ownerops.request(7, "  Kai   Zwei ") == {"cmd": "task_op", "op": "assign", "nr": 7,
                                                   "head": "Kai Zwei"}
    for nr, head in ((0, "Kai"), (-1, "Kai"), (True, "Kai"), ("7", "Kai"), (7, "  "), (7, "x" * 81)):
        with pytest.raises(ownerops.AssignError):
            ownerops.request(nr, head)


def test_join_posts_assign_with_its_livekit_token():
    out = _handle(_ctl(), {"cmd": "task_assign", "nr": 7, "head": "Kai"})
    assert out == {"ok": True, "op": "assign", "nr": 7, "status": 200, "assignee": "Kai"}
    url, body, headers, client_headers = FakeClient.calls[0]
    assert url == "https://voicehook.ai/api/cli/tasks/7/assign"
    assert body == {"assignee": "Kai"}
    assert headers == {"authorization": f"Bearer {JWT}"}
    assert client_headers["x-vh-cli"] == cli._VERSION
    assert len(FakeClient.calls) == 1   # never retried with the pump token


def test_task_op_form_on_socket():
    out = _handle(_ctl(), {"cmd": "task_op", "op": "assign", "nr": 7, "head": "Kai"})
    assert out["ok"] and FakeClient.calls[0][1] == {"assignee": "Kai"}


def test_no_livekit_token_sends_nothing():
    out = _handle(_ctl(jwt=""), {"cmd": "task_assign", "nr": 7, "head": "Kai"})
    assert out["ok"] is False and out["error"] == "no_livekit_join"
    assert "VOICEHOOK_AGENT_KEY" in out["hint"] and FakeClient.calls == []


def test_bad_input_on_socket_sends_nothing():
    out = _handle(_ctl(), {"cmd": "task_assign", "nr": 0, "head": "Kai"})
    assert out["ok"] is False and FakeClient.calls == []


@pytest.mark.parametrize("status,body,error,hint", [
    (403, {"ok": False, "code": 403, "error": "owner_only"}, "owner_only", ownerops.HINT_OWNER_ONLY),
    (403, {"ok": False, "code": 403, "error": "not_owner_agent"}, "not_owner_agent",
     ownerops.HINT_OWNER_ONLY),
    (403, {"ok": False, "code": 403, "error": "guest_present"}, "guest_present", ownerops.HINT_GUEST),
    (429, {"ok": False, "code": 429, "error": "rate_limited"}, "rate_limited", ownerops.HINT_RATE),
    (404, {"ok": False, "code": 404, "error": "auth_invalid", "detail": "unknown or expired session"},
     "auth_invalid", ownerops.HINT_EXPIRED),
    (404, {"ok": False, "code": 404, "error": "not_found"}, "not_found", None),
    (502, ValueError("html"), "HTTP 502", None),
])
def test_server_errors_are_passed_on(status, body, error, hint):
    FakeClient.answer = Resp(status, body)
    out = _handle(_ctl(), {"cmd": "task_assign", "nr": 7, "head": "Kai"})
    assert out["ok"] is False and out["nr"] == 7 and out["status"] == status
    assert out["error"] == error and out.get("hint") == hint


def test_owner_only_hint_names_key_and_alone():
    assert "ohne Schlüssel" in ownerops.HINT_OWNER_ONLY and "allein" in ownerops.HINT_OWNER_ONLY


def test_network_error_names_type_only():
    FakeClient.answer = httpx.ConnectError(f"boom Bearer {JWT}")
    out = _handle(_ctl(), {"cmd": "task_assign", "nr": 7, "head": "Kai"})
    assert out == {"ok": False, "op": "assign", "nr": 7, "status": 0,
                   "error": "request failed (ConnectError)"}


def test_token_never_in_answer_stdout_or_log(capsys, caplog):
    caplog.set_level(logging.DEBUG)
    answers = [Resp(200, {"ok": True, "card": CARD}), Resp(403, {"error": "owner_only"}),
               Resp(429, {"error": "rate_limited"}), httpx.ReadTimeout(JWT)]
    outs = []
    for a in answers:
        FakeClient.answer = a
        outs.append(_handle(_ctl(), {"cmd": "task_assign", "nr": 7, "head": "Kai"}))
    seen = capsys.readouterr()
    blob = json.dumps(outs) + seen.out + seen.err + caplog.text
    assert JWT not in blob and PUMP not in blob


class _Sock:
    """Fake control socket: the CLI side of `task assign` against a join handler."""

    def __init__(self, monkeypatch, ctl=None, missing=False):
        self.reqs = []

        def resolve(session, wait=0):
            if missing:
                raise vsession.SessionError("no running join")
            return "/fake/ctl.sock"

        def request(sock, req, timeout=None):
            self.reqs.append(req)
            return asyncio.run(cli._control_handler(ctl, req))

        monkeypatch.setattr(vsession, "resolve_socket", resolve)
        monkeypatch.setattr(vsession, "request", request)


def _main(argv) -> int:
    with pytest.raises(SystemExit) as e:
        cli.main(argv)
    return e.value.code


def test_cli_one_json_line_ok(monkeypatch, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    sock = _Sock(monkeypatch, _ctl())
    assert _main(["task", "assign", "7", "Kai"]) == 0
    out = capsys.readouterr()
    lines = out.out.strip().splitlines()
    assert len(lines) == 1 and json.loads(lines[0]) == {
        "ok": True, "op": "assign", "nr": 7, "status": 200, "assignee": "Kai"}
    assert sock.reqs == [{"cmd": "task_op", "op": "assign", "nr": 7, "head": "Kai"}]
    assert JWT not in out.out + out.err + caplog.text


def test_cli_owner_only_exit_1(monkeypatch, capsys):
    FakeClient.answer = Resp(403, {"ok": False, "code": 403, "error": "owner_only"})
    _Sock(monkeypatch, _ctl())
    assert _main(["task", "assign", "7", "Kai", "--session", "room-a"]) == 1
    got = json.loads(capsys.readouterr().out)
    assert got["error"] == "owner_only" and got["hint"] == ownerops.HINT_OWNER_ONLY


def test_cli_rate_limited_passed_on(monkeypatch, capsys):
    FakeClient.answer = Resp(429, {"ok": False, "code": 429, "error": "rate_limited"})
    _Sock(monkeypatch, _ctl())
    assert _main(["task", "assign", "7", "Kai"]) == 1
    got = json.loads(capsys.readouterr().out)
    assert got["status"] == 429 and got["error"] == "rate_limited"


def test_cli_no_join_hint(monkeypatch, capsys):
    _Sock(monkeypatch, missing=True)
    assert _main(["task", "assign", "7", "Kai", "--wait", "0"]) == 3
    got = json.loads(capsys.readouterr().out)
    assert got["ok"] is False and got["error"] == "no_join" and got["nr"] == 7
    assert "VOICEHOOK_AGENT_KEY" in got["hint"]
    assert FakeClient.calls == []


def test_cli_bad_input_exit_2(monkeypatch, capsys):
    sock = _Sock(monkeypatch, _ctl())
    assert _main(["task", "assign", "0", "Kai"]) == 2
    assert json.loads(capsys.readouterr().out)["ok"] is False and sock.reqs == []


def test_cli_old_join(monkeypatch, capsys):
    monkeypatch.setattr(vsession, "resolve_socket", lambda s, wait=0: "/fake/ctl.sock")
    monkeypatch.setattr(vsession, "request", lambda s, r, timeout=None: {"ok": False,
                                                                         "error": "unknown cmd"})
    assert _main(["task", "assign", "7", "Kai"]) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "join_too_old"
