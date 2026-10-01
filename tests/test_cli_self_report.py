"""Mandatory self-report: `join` without --name AND --model must fail fast
(exit != 0, clear message) — before any network call."""
from __future__ import annotations

import asyncio

import pytest

cli = pytest.importorskip(
    "voicehook_agent.cli",
    reason="livekit/httpx not installed in this environment",
)

URL = "https://voicehook.ai/r/demo-room-live-ABC5"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    async def _boom(*a, **k):  # pragma: no cover - must never be reached
        raise AssertionError("token mint must not run without self-report")
    monkeypatch.setattr(cli, "_mint_token", _boom)


@pytest.mark.parametrize("argv, missing", [
    (["join", URL], "--name, --model"),
    (["join", URL, "--model", "opus-5.5"], "--name"),
    (["join", URL, "--name", "Claude"], "--model"),
    (["join", URL, "--name", "  ", "--model", "opus-5.5"], "--name"),
    (["join", URL, "--identity", "claude-box-1", "--model", "opus-5.5"], "--name"),
    (["join", URL, "--no-greet"], "--name, --model"),
])
def test_join_without_self_report_exits_nonzero(argv, missing, capsys):
    with pytest.raises(SystemExit) as ei:
        cli.main(argv)
    assert ei.value.code == 2
    err = capsys.readouterr().err
    assert "--name" in err and "--model" in err
    assert f"(fehlt: {missing})" in err
    assert "Beispiel" in err and "--name Claude --model opus-5.5" in err


def test_join_coroutine_guard_also_rejects():
    rc = asyncio.run(cli._join(URL, None, "Claude", False, None, model=None))
    assert rc == 2


def test_missing_self_report_helper():
    assert cli._missing_self_report("Claude", "opus-5.5") == []
    assert cli._missing_self_report(None, None) == ["--name", "--model"]


def test_mint_token_sends_name_and_model(monkeypatch):
    sent = {}

    class _Resp:
        def raise_for_status(self): pass
        def json(self): return {"token": "t", "url": "wss://x"}

    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, params=None):
            sent["url"], sent["params"] = url, params
            return _Resp()

    monkeypatch.undo()  # restore the real _mint_token
    monkeypatch.setattr(cli.httpx, "AsyncClient", _Client)
    asyncio.run(cli._mint_token("https://voicehook.ai", "demo-room", "claude-box-1",
                                name="Claude", model="opus-5.5"))
    assert sent["url"] == "https://voicehook.ai/api/token"
    assert sent["params"] == {"room": "demo-room", "identity": "claude-box-1", "invite": "1",
                              "name": "Claude", "model": "opus-5.5"}
