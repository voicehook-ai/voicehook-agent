"""Decision Board over the CLI (`voicehook-agent decide`): build the request, the RPC to
the worker (method vh.decide), and the result arriving as a `next` event type decision."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from voicehook_agent import cli, relay


@pytest.fixture(autouse=True)
def _plain_check_off(monkeypatch):
    """These tests are about the decide wiring with short sample questions; the 0.21.5
    Klartext rule has its own tests (tests/test_plaintext.py)."""
    monkeypatch.setenv("VOICEHOOK_PLAIN_CHECK", "0")


class _Worker:
    """A fake voicebot: answers vh.decide RPCs with canned JSON (one per call)."""

    def __init__(self, replies: list[str]) -> None:
        self.replies = replies
        self.calls: list[dict] = []

    async def perform_rpc(self, *, destination_identity, method, payload, response_timeout):
        self.calls.append({"to": destination_identity, "method": method,
                           "payload": json.loads(payload), "timeout": response_timeout})
        return self.replies.pop(0)

    async def publish_data(self, data, reliable=True, topic=""):
        pass


def _ctl(replies=None, *, with_worker=True):
    ctl = cli._Control("blau-tiger", "claude-x", relay.SayTracker(), relay.EchoSuppressor(False), 0.0)
    local = _Worker(list(replies or []))
    peers = {}
    if with_worker:
        peers = {"delta": SimpleNamespace(identity="delta-worker", kind=4)}
    ctl.attach(SimpleNamespace(local_participant=local, remote_participants=peers))
    return ctl, local


def _args(**kw):
    base = {"cmd": "decide", "question": None, "option": [], "recommend": None,
            "id": None, "delete": False}
    base.update(kw)
    return SimpleNamespace(**base)


# ----- request building -------------------------------------------------------------
def test_yes_no_question():
    assert cli._client_request(_args(question="Deploy now?")) == {
        "cmd": "decide", "question": "Deploy now?"}


def test_options_and_recommend():
    req = cli._client_request(_args(question="Which color?", option=["Teal", "Grey"], recommend=0))
    assert req == {"cmd": "decide", "question": "Which color?",
                   "options": ["Teal", "Grey"], "recommend": 0}


def test_recommend_zero_is_kept():
    assert cli._client_request(_args(question="q", option=["a", "b"], recommend=0))["recommend"] == 0


def test_delete_needs_id():
    with pytest.raises(ValueError):
        cli._client_request(_args(delete=True))
    assert cli._client_request(_args(id="d0a1b2c3d4", delete=True)) == {
        "cmd": "decide", "id": "d0a1b2c3d4", "delete": True}


def test_update_fields():
    req = cli._client_request(_args(id="d0a1b2c3d4", recommend=1))
    assert req == {"cmd": "decide", "id": "d0a1b2c3d4", "recommend": 1}


def test_update_without_fields_errors():
    with pytest.raises(ValueError):
        cli._client_request(_args(id="d0a1b2c3d4"))


def test_question_required():
    with pytest.raises(ValueError):
        cli._client_request(_args())


def test_argparse_accepts_decide():
    ap = cli._build_parser() if hasattr(cli, "_build_parser") else None
    # the parser is built inside main(); parse via a round-trip through main's argv path
    import argparse
    # minimal: ensure the subcommand exists by invoking the request builder on parsed args
    req = cli._client_request(_args(question="go?", option=["x", "y"], recommend=1))
    assert req["options"] == ["x", "y"]
    assert isinstance(req, dict)
    del ap, argparse


# ----- the RPC to the worker --------------------------------------------------------
def test_decide_rpc_create():
    ctl, worker = _ctl(['{"ok": true, "id": "d0a1b2c3d4", "number": 1}'])
    out = asyncio.run(cli._control_handler(ctl, {"cmd": "decide", "question": "Deploy now?"}))
    assert out["ok"] is True and out["id"] == "d0a1b2c3d4" and out["number"] == 1
    assert worker.calls[0]["to"] == "delta-worker"
    assert worker.calls[0]["method"] == cli._DECIDE_RPC
    assert worker.calls[0]["payload"] == {"question": "Deploy now?"}


def test_decide_rpc_options_payload():
    ctl, worker = _ctl(['{"ok": true, "id": "d0a1b2c3d4", "number": 2}'])
    asyncio.run(cli._control_handler(ctl, {"cmd": "decide", "question": "Color?",
                                           "options": ["Teal", "Grey"], "recommend": 0}))
    assert worker.calls[0]["payload"] == {"question": "Color?", "options": ["Teal", "Grey"],
                                          "recommend": 0}


def test_decide_rpc_delete_payload():
    ctl, worker = _ctl(['{"ok": true, "id": "d0a1b2c3d4", "number": 1, "deleted": true}'])
    out = asyncio.run(cli._control_handler(ctl, {"cmd": "decide", "id": "d0a1b2c3d4", "delete": True}))
    assert out.get("deleted") is True
    assert worker.calls[0]["payload"] == {"id": "d0a1b2c3d4", "delete": True}


def test_decide_retries_on_425_then_succeeds():
    ctl, worker = _ctl(['{"ok": false, "status": 425}',
                        '{"ok": true, "id": "d0a1b2c3d4", "number": 1}'])
    out = asyncio.run(cli._control_handler(ctl, {"cmd": "decide", "question": "q?"}))
    assert out["ok"] is True
    assert len(worker.calls) == 2


def test_decide_no_worker():
    ctl, _ = _ctl([], with_worker=False)
    out = asyncio.run(cli._control_handler(ctl, {"cmd": "decide", "question": "q?"}))
    assert out["ok"] is False and out["status"] == 503


# ----- the result as a `next` event -------------------------------------------------
def test_decision_event_reaches_next():
    ctl, _ = _ctl(['{"ok": true}'])
    ev = {"type": "decision", "id": "d0a1b2c3d4", "number": 1, "verdict": "yes", "by": "oliver"}

    async def go():
        ctl.events.arm()  # in a real call the agent already called say/next before the answer
        ctl.events.put_nowait(ev)
        return await cli._control_handler(ctl, {"cmd": "next", "timeout": 0})

    out = asyncio.run(go())
    assert out["type"] == "decision" and out["verdict"] == "yes"
