"""Owner agent key (0.33.1): VOICEHOOK_AGENT_KEY rides as `Authorization: Bearer` on both joins
(GET /api/token, POST /api/bridge/join); env only; wrong prefix ignored; never in output."""
from __future__ import annotations

import asyncio
import base64
import io
import json
import sys

import pytest

cli = pytest.importorskip("voicehook_agent.cli", reason="livekit/httpx not installed")
from test_transport import FakeBridge

from voicehook_agent import agentkey
from voicehook_agent import transport as vt

FAKE = "vhak_" + "x" * 8   # built at runtime, obviously fake


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv(agentkey.ENV, raising=False)
    monkeypatch.setattr(agentkey, "_warned", False)


class _Resp:
    def __init__(self, status=200, text="", body=None):
        self.status_code, self.text = status, text
        self._body = body or {"token": "t", "url": "wss://x"}

    def json(self):
        return self._body


def _client(sent: dict, resp: _Resp):
    class _Client:
        def __init__(self, *a, headers=None, **k):
            sent["client_headers"] = dict(headers or {})
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, params=None):
            sent["url"], sent["params"] = url, params
            return resp
    return _Client


def _mint(monkeypatch, key, resp=None):
    sent: dict = {}
    monkeypatch.setattr(cli.httpx, "AsyncClient", _client(sent, resp or _Resp()))
    asyncio.run(cli._mint_token("https://voicehook.ai", "demo-room", "claude-box-1",
                                name="C", model="m", agent_key=key))
    return sent


def test_webrtc_mint_sends_bearer_header_not_url(monkeypatch):
    sent = _mint(monkeypatch, FAKE)
    assert sent["client_headers"]["authorization"] == f"Bearer {FAKE}"
    assert FAKE not in json.dumps(sent["params"]) and FAKE not in sent["url"]


def test_webrtc_mint_without_key_sends_no_authorization(monkeypatch):
    sent = _mint(monkeypatch, None)
    assert "authorization" not in sent["client_headers"]


def test_webrtc_mint_error_text_redacted(monkeypatch):
    resp = _Resp(403, text=f"nope {FAKE}")
    with pytest.raises(cli.TokenMintError) as ei:
        _mint(monkeypatch, FAKE, resp)
    assert FAKE not in str(ei.value) and FAKE not in ei.value.detail


def test_bridge_join_sends_bearer_header():
    async def go():
        fb = FakeBridge()
        base = await fb.start()
        room = vt.BridgeRoom(base, "a-b-c-AB12", "x", name="C", model="m", agent_key=FAKE)
        await room.connect()
        await room.disconnect()
        await fb.stop()
        return fb
    fb = asyncio.run(go())
    joins = [r for r in fb.requests if r[1].endswith("/api/bridge/join")]
    assert joins and joins[0][2].get("authorization") == f"Bearer {FAKE}"
    assert FAKE not in json.dumps(fb.joins)   # not in the body either


def test_bridge_join_without_key_has_no_authorization():
    async def go():
        fb = FakeBridge()
        base = await fb.start()
        room = vt.BridgeRoom(base, "a-b-c-AB12", "x", name="C", model="m")
        await room.connect()
        await room.disconnect()
        await fb.stop()
        return fb
    fb = asyncio.run(go())
    joins = [r for r in fb.requests if r[1].endswith("/api/bridge/join")]
    assert joins and "authorization" not in joins[0][2]


def test_env_states_and_wrong_prefix_ignored(monkeypatch, capsys):
    assert agentkey.state() == "unset" and agentkey.get() is None
    monkeypatch.setenv(agentkey.ENV, "sk_" + "y" * 8)
    assert agentkey.state() == "invalid" and agentkey.get() is None
    assert agentkey.get() is None   # warns once only
    err = capsys.readouterr().err
    assert err.count("[warn]") == 1 and "y" * 8 not in err
    monkeypatch.setenv(agentkey.ENV, FAKE)
    assert agentkey.state() == "set" and agentkey.get() == FAKE
    assert FAKE not in agentkey.doctor_line()
    monkeypatch.setenv(agentkey.ENV, "bad")
    assert "rejected" in agentkey.doctor_line() and "bad" not in agentkey.doctor_line().replace("bad)", "")


def test_owner_granted_reads_signed_attribute():
    def jwt(attrs):
        b = base64.urlsafe_b64encode(json.dumps({"attributes": attrs}).encode()).decode().rstrip("=")
        return f"h.{b}.s"
    assert agentkey.owner_granted(jwt({"vh.owner_agent": "1"})) is True
    assert agentkey.owner_granted(jwt({})) is False
    assert agentkey.owner_granted("garbage") is None


def _join(monkeypatch, capsys, tmp_path, transport):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    monkeypatch.setattr(cli.httpx, "AsyncClient",
                        _client({}, _Resp(403, text=f"forbidden {FAKE}")))
    asyncio.run(cli._join("https://voicehook.ai/r/a-b-c-AB12", None, "Claude", True,
                          model="opus-5.5", no_greet=True, idle_timeout=0, control=False,
                          transport=transport))
    return capsys.readouterr()


def test_join_never_prints_or_stores_key(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv(agentkey.ENV, FAKE)
    out, err = _join(monkeypatch, capsys, tmp_path, "webrtc")
    assert FAKE not in out and FAKE not in err
    for f in tmp_path.rglob("*"):
        if f.is_file():
            assert FAKE not in f.read_text(errors="ignore"), f
