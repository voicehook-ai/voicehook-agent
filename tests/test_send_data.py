"""0.21.4: `voicehook-agent send-data` (agent-to-agent data like MCP send_data) and incoming
`type: data` events in `next` / `listen`."""
from __future__ import annotations

import asyncio
import base64
import json
import types
from types import SimpleNamespace

import pytest

from voicehook_agent import cli, datachan, relay


def _op(identity, name=None, kind=0, role="agent"):
    return SimpleNamespace(identity=identity, kind=kind, name="",
                           attributes={"vh.role": role, **({"vh.name": name} if name else {})})


PEERS = {"claude-a": _op("claude-a", "Scout"), "codex-b": _op("codex-b", "Codex"),
         "delta": _op("delta", "Delta", kind=4), "host-1": _op("host-1", "Oliver", role="")}


def test_resolve_targets():
    ps = PEERS.values()
    assert [p.identity for p in datachan.resolve_targets(ps, "*")] == ["claude-a", "codex-b"]
    assert [p.identity for p in datachan.resolve_targets(ps, "scout")] == ["claude-a"]
    assert [p.identity for p in datachan.resolve_targets(ps, "codex-b")] == ["codex-b"]
    for to, status in (("Delta", 404), ("Oliver", 404), ("", 422)):
        with pytest.raises(datachan.DataError) as e:
            datachan.resolve_targets(ps, to)
        assert e.value.status == status


def test_build_packet_and_inline_limit():
    pkt, size = datachan.build_packet(sender_identity="me", sender_label="me", to="*",
                                      targets=["claude-a"], kind="json", payload={"a": 1})
    assert pkt["kind"] == "json" and pkt["payload"] == {"a": 1} and pkt["to_identities"] == ["claude-a"]
    raw = b"x" * 1000
    pkt, size = datachan.build_packet(sender_identity="me", sender_label="me", to="Scout",
                                      targets=["claude-a"], kind="file", payload=raw, name="/tmp/a.csv")
    assert size == 1000 and pkt["encoding"] == "base64" and pkt["name"] == "a.csv"
    assert base64.b64decode(pkt["payload"]) == raw
    with pytest.raises(datachan.DataError) as e:
        datachan.build_packet(sender_identity="me", sender_label="me", to="*", targets=["x"],
                              kind="file", payload=b"x" * 20_000)
    assert e.value.status == 413
    assert datachan.fits_inline(b"x" * 1000) and not datachan.fits_inline(b"x" * 20_000)


def test_receive_event_webrtc_and_bridge():
    raw = {"id": "m1", "from": "x", "kind": "text", "payload": "hallo", "to_identities": ["me"]}
    ev = datachan.receive_event("me", PEERS["claude-a"], raw)
    assert ev["type"] == "data" and ev["from"] == "Scout" and ev["payload"] == "hallo"
    assert ev["summary"].startswith("Daten von Scout: Text (5 B)")
    assert datachan.receive_event("me", PEERS["claude-a"], dict(raw, to_identities=["other"])) is None
    assert datachan.receive_event("me", PEERS["host-1"], raw) is None         # a human: never
    assert datachan.receive_event("me", PEERS["delta"], raw) is None          # the voicebot: never
    bridge_ev = {"type": "data", "from": "Codex", "kind": "file", "url": "/api/bridge/file/" + "a" * 32,
                 "file_id": "a" * 32, "size": 2048, "name": "r.pdf"}
    ev = datachan.receive_event("me", None, bridge_ev)
    assert ev["url"].endswith("a" * 32) and "r.pdf (2.0 KB)" in ev["summary"]


class _Local:
    def __init__(self):
        self.sent = []

    async def publish_data(self, data, reliable=True, topic="", destination_identities=None):
        self.sent.append((topic, json.loads(data), destination_identities))


def _ctl(room):
    ctl = cli._Control("room", "claude-me", relay.SayTracker(), relay.EchoSuppressor(False), 0.0)
    ctl.attach(room)
    return ctl


def test_join_webrtc_publishes_to_target_identities(tmp_path):
    room = SimpleNamespace(local_participant=_Local(), remote_participants=PEERS)
    ctl = _ctl(room)
    out = asyncio.run(cli._control_handler(ctl, {"cmd": "data", "to": "Scout", "kind": "json",
                                                 "payload": {"rows": 3}}))
    assert out["ok"] and out["to"] == ["Scout"]
    topic, pkt, dest = room.local_participant.sent[-1]
    assert topic == "operator.data" and dest == ["claude-a"] and pkt["payload"] == {"rows": 3}
    big = tmp_path / "big.bin"
    big.write_bytes(b"x" * 20_000)
    out = asyncio.run(cli._control_handler(ctl, {"cmd": "data", "to": "*", "kind": "file",
                                                 "path": str(big)}))
    assert out["ok"] is False and out["status"] == 413 and "--transport bridge" in out["error"]


def test_join_bridge_inline_and_upload(tmp_path):
    calls = []

    class BridgeRoom:
        local_participant = _Local()
        remote_participants = PEERS

        async def send_data(self, body):
            calls.append(("data", body))
            return {"ok": True, "id": "m9", "to": ["Scout"], "size": 1}

        async def upload_file(self, data, name="", mime=""):
            calls.append(("upload", len(data), name))
            return {"ok": True, "file_id": "b" * 32, "url": "/api/bridge/file/" + "b" * 32}
    ctl = _ctl(BridgeRoom())
    small, big = tmp_path / "s.txt", tmp_path / "big.csv"
    small.write_bytes(b"abc")
    big.write_bytes(b"y" * 50_000)
    asyncio.run(cli._control_handler(ctl, {"cmd": "data", "to": "Scout", "kind": "file", "path": str(small)}))
    assert calls[-1][0] == "data" and base64.b64decode(calls[-1][1]["payload"]) == b"abc"
    assert calls[-1][1]["name"] == "s.txt"
    out = asyncio.run(cli._control_handler(ctl, {"cmd": "data", "to": "Scout", "kind": "file",
                                                 "path": str(big), "mime": "text/csv"}))
    assert calls[-2] == ("upload", 50_000, "big.csv")
    assert calls[-1][1]["file_id"] == "b" * 32 and "payload" not in calls[-1][1]
    assert out["ok"] and out["type"] == "data"


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


def test_cli_send_data_requests(tmp_path):
    assert _parse(["send-data", "--to", "*", "--json", '{"a": [1, 2]}']) == \
        {"cmd": "data", "to": "*", "kind": "json", "payload": {"a": [1, 2]}}
    assert _parse(["send-data", "--to", "Scout", "--text", "hi", "--name", "n"]) == \
        {"cmd": "data", "to": "Scout", "kind": "text", "payload": "hi", "name": "n"}
    f = tmp_path / "r.pdf"
    f.write_bytes(b"%PDF")
    req = _parse(["send-data", "--to", "Codex", "--file", str(f), "--mime", "application/pdf"])
    assert req == {"cmd": "data", "to": "Codex", "kind": "file", "path": str(f), "mime": "application/pdf"}
    with pytest.raises(ValueError):
        _parse(["send-data", "--to", "*", "--json", "{nope"])


def test_listen_wakes_on_data(capsys):
    replies = [{"ok": True, "type": "timeout"},
               {"ok": True, "type": "data", "from": "Scout", "kind": "text", "payload": "x",
                "summary": "Daten von Scout: Text (1 B)"}]
    out = cli._run_listen(types.SimpleNamespace(session=None, wait=0.0, timeout=0.0, poll=45.0),
                          request=lambda s, r, timeout=None: replies.pop(0),
                          resolve=lambda s, wait: "sock")
    assert out == 0 and json.loads(capsys.readouterr().out)["type"] == "data"
