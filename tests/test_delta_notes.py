"""0.23.0: `voicehook-agent delta-rule|delta-note add|list|rm`: standing rules and facts for
Delta. WebRTC: vh.delta_notes RPC to the voicebot; bridge: POST /api/bridge/delta. Never over
the helper relay."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from voicehook_agent import cli, relay, transport


def _op(identity, kind=0, role="agent"):
    return SimpleNamespace(identity=identity, kind=kind, name="", attributes={"vh.role": role})


PEERS = {"delta": _op("delta", kind=4), "host-1": _op("host-1", role="")}


class _Local:
    def __init__(self, answers):
        self.calls, self.answers = [], list(answers)

    async def perform_rpc(self, *, destination_identity, method, payload, response_timeout=10.0):
        self.calls.append((destination_identity, method, json.loads(payload)))
        return json.dumps(self.answers.pop(0))


def _ctl(room):
    ctl = cli._Control("room", "claude-me", relay.SayTracker(), relay.EchoSuppressor(False), 0.0)
    ctl.attach(room)
    return ctl


def _parse(argv):
    seen = {}
    orig = cli._run_client
    cli._run_client = lambda a: seen.setdefault("a", a) and 0
    try:
        try:
            cli.main(argv)
        except SystemExit:
            pass
    finally:
        cli._run_client = orig
    return cli._client_request(seen["a"])


def test_cli_builds_the_right_requests():
    assert _parse(["delta-rule", "add", "Sprich", "den", "Kunden  mit", "Sie", "an"]) == \
        {"cmd": "delta", "kind": "rule", "op": "add", "text": "Sprich den Kunden mit Sie an"}
    assert _parse(["delta-note", "add", "Release am Freitag", "--persist"]) == \
        {"cmd": "delta", "kind": "note", "op": "add", "text": "Release am Freitag", "persist": True}
    assert _parse(["delta-note", "list"]) == {"cmd": "delta", "kind": "note", "op": "list"}
    assert _parse(["delta-rule", "rm", "r1a2b3c"]) == {"cmd": "delta", "kind": "rule", "op": "rm", "id": "r1a2b3c"}
    with pytest.raises(ValueError, match="max 300 characters"):
        _parse(["delta-note", "add", "x" * 301])
    assert _parse(["delta-note", "add", "x" * 300])["text"] == "x" * 300       # Grenze genau
    for argv in (["delta-rule", "add"], ["delta-rule", "rm"], ["delta-note", "list", "x"]):
        with pytest.raises(ValueError):
            _parse(argv)


def test_not_a_helper_relay_command():
    assert "delta" not in cli.RELAY_CMDS


def test_webrtc_rpc_to_the_voicebot_and_retry_while_not_ready():
    ok = {"ok": True, "kind": "rule", "op": "add", "id": "r1a2b3c", "items": [], "count": 1, "max": 20}
    room = SimpleNamespace(local_participant=_Local([{"ok": False, "status": 425}, ok]),
                           remote_participants=PEERS)
    out = asyncio.run(cli._control_handler(_ctl(room), {"cmd": "delta", "kind": "rule", "op": "add",
                                                        "text": "Sprich langsam"}))
    assert out["ok"] and out["type"] == "delta" and out["id"] == "r1a2b3c"
    dest, method, body = room.local_participant.calls[-1]
    assert (dest, method) == ("delta", "vh.delta_notes")
    assert body == {"kind": "rule", "op": "add", "text": "Sprich langsam"}       # nie ein sender
    room2 = SimpleNamespace(local_participant=_Local([{"ok": False, "status": 422, "detail": "too many rules: max 20"}]),
                            remote_participants=PEERS)
    out = asyncio.run(cli._control_handler(_ctl(room2), {"cmd": "delta", "kind": "rule", "op": "add", "text": "x"}))
    assert out["ok"] is False and out["status"] == 422 and "max 20" in out["error"]


def test_bridge_posts_api_bridge_delta():
    calls = []

    class BridgeRoom:
        local_participant = _Local([])
        remote_participants = PEERS

        async def send_delta(self, body):
            calls.append(body)
            return {"ok": True, "kind": "note", "op": "list", "items": [], "count": 0, "max": 30}
    out = asyncio.run(cli._control_handler(_ctl(BridgeRoom()), {"cmd": "delta", "kind": "note", "op": "list"}))
    assert calls == [{"kind": "note", "op": "list"}] and out["type"] == "delta" and out["max"] == 30


def test_transport_send_delta_path():
    seen = {}

    async def fake(self, path, body, topic):
        seen.update(path=path, body=body, topic=topic)
        return SimpleNamespace(status_code=200, json=lambda: {"ok": True}, text="{}")
    orig = transport.BridgeRoom._post_retry
    transport.BridgeRoom._post_retry = fake
    try:
        room = transport.BridgeRoom.__new__(transport.BridgeRoom)
        asyncio.run(room.send_delta({"kind": "rule", "op": "list"}))
    finally:
        transport.BridgeRoom._post_retry = orig
    assert seen == {"path": "/api/bridge/delta", "body": {"kind": "rule", "op": "list"}, "topic": "delta"}
