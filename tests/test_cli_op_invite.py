"""Operator invite: the HMAC `?invite=` of the join link rides along on
GET /api/token as `op_invite`; a 403 "operator invite required" gives a clear
hint; the invite value is never printed."""
from __future__ import annotations

import asyncio
import io
import sys

import pytest
from urllib.parse import quote

cli = pytest.importorskip(
    "voicehook_agent.cli",
    reason="livekit/httpx not installed in this environment",
)

HMAC = "v1.ab+c/d=.sig"  # contains chars that must be URL-encoded
ENC = quote(HMAC, safe="")  # as it appears in a well-formed invite link


class _Resp:
    def __init__(self, status=200, text="", body=None):
        self.status_code, self.text = status, text
        self._body = body or {"token": "t", "url": "wss://x"}

    def json(self):
        return self._body


def _client(sent: dict, resp: _Resp):
    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, params=None):
            sent["url"], sent["params"] = url, params
            return resp
    return _Client


def test_operator_invite_extraction():
    assert cli._operator_invite(f"https://voicehook.ai/r/a-b-c-AB12?invite={ENC}") == HMAC
    assert cli._operator_invite("https://voicehook.ai/r/a-b-c-AB12?invite=1") is None
    assert cli._operator_invite("https://voicehook.ai/r/a-b-c-AB12?go=1") is None
    assert cli._operator_invite("https://voicehook.ai/r/a-b-c-AB12") is None
    assert cli._operator_invite("a-b-c-AB12") is None


def test_mint_sends_op_invite_url_encoded(monkeypatch):
    import httpx
    sent: dict = {}
    monkeypatch.setattr(cli.httpx, "AsyncClient", _client(sent, _Resp()))
    asyncio.run(cli._mint_token("https://voicehook.ai", "demo-room", "claude-box-1",
                                name="Claude", model="opus-5.5", op_invite=HMAC))
    assert sent["params"]["invite"] == "1"
    assert sent["params"]["op_invite"] == HMAC
    # httpx encodes the params; the raw value never appears unescaped in the query
    q = str(httpx.URL(sent["url"], params=sent["params"]).query, "ascii")
    assert "op_invite=v1.ab%2Bc%2Fd%3D.sig" in q


def test_mint_without_invite_sends_no_op_invite(monkeypatch):
    sent: dict = {}
    monkeypatch.setattr(cli.httpx, "AsyncClient", _client(sent, _Resp()))
    asyncio.run(cli._mint_token("https://voicehook.ai", "demo-room", "claude-box-1",
                                name="Claude", model="opus-5.5"))
    assert "op_invite" not in sent["params"]


def _join(monkeypatch, capsys, url: str, resp: _Resp):
    sent: dict = {}
    monkeypatch.setattr(cli.httpx, "AsyncClient", _client(sent, resp))
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    rc = asyncio.run(cli._join(url, None, "Claude", True, model="opus-5.5", no_greet=True,
                               idle_timeout=0, control=False, transport="webrtc"))
    out, err = capsys.readouterr()
    return rc, sent, out, err


def test_join_with_invite_link_sends_op_invite(monkeypatch, capsys):
    rc, sent, out, err = _join(
        monkeypatch, capsys, f"https://voicehook.ai/r/demo-room-live-ABC5?invite={ENC}",
        _Resp(403, "invalid invite: bad signature"))
    assert sent["params"]["op_invite"] == HMAC
    assert rc == 3
    assert "invalid invite" in err
    assert HMAC not in out and HMAC not in err


def test_join_bare_slug_sends_no_op_invite(monkeypatch, capsys):
    rc, sent, out, err = _join(monkeypatch, capsys, "demo-room-live-ABC5",
                               _Resp(403, "operator invite required"))
    assert "op_invite" not in sent["params"]
    assert rc == 3


def test_403_operator_invite_required_prints_full_link_hint(monkeypatch, capsys):
    rc, sent, out, err = _join(monkeypatch, capsys,
                               "https://voicehook.ai/r/demo-room-live-ABC5?go=1",
                               _Resp(403, '{"error":"operator invite required"}'))
    assert rc == 3
    assert "operator invite required" in err
    assert "?invite=" in err and "full invite link" in err
    assert "reconnecting" not in out  # a 403 is terminal, no retry loop
