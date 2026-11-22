"""`voicehook-agent roster set` (XQ14jFc1): own roster row via PUT /api/account/roster/me."""
from __future__ import annotations

import json

import pytest

cli = pytest.importorskip("voicehook_agent.cli", reason="livekit/httpx not installed in this environment")

KEY = "vhak_" + "t" * 3 + "-roster"   # zerlegt, kein echter Schlüssel (gitleaks)


class _Resp:
    def __init__(self, code, body):
        self.status_code, self._body = code, body
        self.text = json.dumps(body)

    def json(self):
        return self._body


@pytest.fixture
def calls(monkeypatch):
    seen = []

    def _put(url, json=None, headers=None, timeout=None):
        seen.append({"url": url, "json": json, "headers": headers})
        return seen.resp if hasattr(seen, "resp") else _Resp(200, {"ok": True, "entry": {"name": "Bruno"}})

    seen = type("L", (list,), {})()
    monkeypatch.setattr(cli.httpx, "put", _put)
    monkeypatch.delenv("VOICEHOOK_API_BASE", raising=False)
    monkeypatch.delenv("VOICEHOOK_SESSION_LABEL", raising=False)
    monkeypatch.delenv("VOICEHOOK_AGENT_KEY_HOST", raising=False)
    return seen


def _run(argv, capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(argv)
    return e.value.code, json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_set_sends_areas_label_and_key(calls, capsys, monkeypatch):
    monkeypatch.setenv("VOICEHOOK_AGENT_KEY", KEY)
    rc, out = _run(["roster", "set", "  crown,  Secrets ", "--name", "[R] Bruno · Security"], capsys)
    assert rc == 0 and out["ok"] and out["entry"] == {"name": "Bruno"}
    c = calls[0]
    assert c["url"] == "https://voicehook.ai/api/account/roster/me"
    assert c["json"] == {"areas": "crown, Secrets"}
    assert c["headers"]["authorization"] == f"Bearer {KEY}"
    assert c["headers"]["x-vh-agent"] == "[R] Bruno · Security"


def test_label_from_session_env_else_none(calls, capsys, monkeypatch):
    monkeypatch.setenv("VOICEHOOK_AGENT_KEY", KEY)
    monkeypatch.setenv("VOICEHOOK_SESSION_LABEL", "Ravi")
    _run(["roster", "set", "Voice"], capsys)
    monkeypatch.delenv("VOICEHOOK_SESSION_LABEL")
    _run(["roster", "set", "Voice"], capsys)
    assert calls[0]["headers"]["x-vh-agent"] == "Ravi"
    assert "x-vh-agent" not in calls[1]["headers"]


def test_no_key_no_request(calls, capsys, monkeypatch):
    monkeypatch.delenv("VOICEHOOK_AGENT_KEY", raising=False)
    rc, out = _run(["roster", "set", "Voice"], capsys)
    assert rc == 2 and out["error"] == "agent_key_missing" and not calls


def test_untrusted_host_no_request(calls, capsys, monkeypatch):
    monkeypatch.setenv("VOICEHOOK_AGENT_KEY", KEY)
    rc, out = _run(["roster", "set", "Voice", "--base", "https://evil.example"], capsys)
    assert rc == 2 and out["error"] == "agent_key_missing" and not calls


def test_empty_areas_no_request(calls, capsys, monkeypatch):
    monkeypatch.setenv("VOICEHOOK_AGENT_KEY", KEY)
    rc, out = _run(["roster", "set", "   "], capsys)
    assert rc == 2 and out["error"] == "invalid_value" and not calls


def test_server_error_passed_through_and_key_redacted(calls, capsys, monkeypatch):
    monkeypatch.setenv("VOICEHOOK_AGENT_KEY", KEY)
    calls.resp = _Resp(403, {"ok": False, "code": 403, "error": "not_owner", "detail": f"bad {KEY}"})
    rc, out = _run(["roster", "set", "Voice"], capsys)
    assert rc == 1 and out["status"] == 403 and out["error"] == "not_owner"
    assert KEY not in json.dumps(out)


def test_network_error(calls, capsys, monkeypatch):
    monkeypatch.setenv("VOICEHOOK_AGENT_KEY", KEY)

    def _boom(*a, **k):
        raise cli.httpx.ConnectError(f"down {KEY}")
    monkeypatch.setattr(cli.httpx, "put", _boom)
    rc, out = _run(["roster", "set", "Voice"], capsys)
    assert rc == 1 and out["error"] == "network" and KEY not in json.dumps(out)
