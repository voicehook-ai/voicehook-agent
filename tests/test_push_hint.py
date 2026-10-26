"""Hook-Hinweis: PushNotification-Tool im laufenden Call -> Warnung auf stderr, kein Block."""
from __future__ import annotations

import json
import sys

import pytest

from voicehook_agent import activity, relayops


# --------------------------------------------------------------------------- #
# _push_hint: Warnung bei PushNotification + aktivem Relay
# --------------------------------------------------------------------------- #
def _push_event(tool_name: str) -> str:
    return json.dumps({
        "tool_name": tool_name,
        "tool_input": {"description": "test"},
        "tool_use_id": "test-id-001",
        "session_id": "ses-0000",
    })


def test_push_hint_with_relay_writes_stderr(monkeypatch, capsys):
    """PushNotification + aktiver Relay -> Hinweis auf stderr."""
    monkeypatch.setattr("voicehook_agent.multisession.load_relay", lambda *a, **kw: {"token": "t"})
    activity._push_hint({"tool_name": "PushNotification"})
    err = capsys.readouterr().err
    assert "PushNotification" in err
    assert "activity" in err


def test_push_hint_case_insensitive(monkeypatch, capsys):
    """toolname 'mcp__foo__pushnotification' wird erkannt."""
    monkeypatch.setattr("voicehook_agent.multisession.load_relay", lambda *a, **kw: {"token": "t"})
    activity._push_hint({"tool_name": "mcp__foo__PushNotification"})
    err = capsys.readouterr().err
    assert "PushNotification" in err


def test_push_hint_without_relay_silent(monkeypatch, capsys):
    """PushNotification OHNE Relay -> kein Hinweis."""
    monkeypatch.setattr("voicehook_agent.multisession.load_relay", lambda *a, **kw: None)
    activity._push_hint({"tool_name": "PushNotification"})
    assert capsys.readouterr().err == ""


def test_push_hint_other_tool_silent(monkeypatch, capsys):
    """Anderes Tool -> kein Hinweis, auch wenn Relay aktiv."""
    monkeypatch.setattr("voicehook_agent.multisession.load_relay", lambda *a, **kw: {"token": "t"})
    activity._push_hint({"tool_name": "Bash"})
    activity._push_hint({"tool_name": "Edit"})
    activity._push_hint({"tool_name": "mcp__voicehook__say"})
    assert capsys.readouterr().err == ""


def test_push_hint_never_raises(monkeypatch, capsys):
    """Auch wenn load_relay wirft -> kein Absturz."""
    def _boom(*a, **kw):
        raise RuntimeError("boom")
    monkeypatch.setattr("voicehook_agent.multisession.load_relay", _boom)
    activity._push_hint({"tool_name": "PushNotification"})  # must not raise


def test_handle_pre_push_tool_still_returns(monkeypatch, tmp_path):
    """pre_tool_use mit PushNotification + Relay: Tool wird NICHT blockiert (keine Exception)."""
    monkeypatch.setattr("voicehook_agent.multisession.load_relay", lambda *a, **kw: {"token": "t"})
    monkeypatch.setenv("VOICEHOOK_AGENT_HOME", str(tmp_path / "vh"))
    monkeypatch.delenv("VOICEHOOK_SESSION", raising=False)
    raw = _push_event("PushNotification")
    # Must not raise; return value is bool (does not matter if True or False)
    result = activity.pre_tool_use(raw)
    assert isinstance(result, bool)


# --------------------------------------------------------------------------- #
# relayops.status_body: done-Cap auf die letzten DONE_CAP Eintraege
# --------------------------------------------------------------------------- #
def test_done_cap_trims_to_last_three():
    board = {"doing": "x", "open": ["A"], "done": ["d1", "d2", "d3", "d4", "d5"]}
    body = relayops.status_body(board)
    assert body["done"] == ["d3", "d4", "d5"]


def test_done_cap_fewer_than_cap_unchanged():
    board = {"doing": "x", "open": [], "done": ["d1", "d2"]}
    body = relayops.status_body(board)
    assert body["done"] == ["d1", "d2"]


def test_done_cap_empty_unchanged():
    body = relayops.status_body({"doing": "", "open": [], "done": []})
    assert body["done"] == []


def test_open_not_capped():
    """open-Liste bleibt unbegrenzt."""
    board = {"done": [], "open": [f"task-{i}" for i in range(10)]}
    body = relayops.status_body(board)
    assert len(body["open"]) == 10
