"""0.21.0 Silent Mode: the humans' vh.mic_muted / vh.speaker_muted / vh.away attributes as an
aggregate human_state on every `next` and as an event type human_state when it changes (WebRTC
participant attributes; bridge: peer-updated over SSE carries the same attributes)."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from voicehook_agent import cli, relay
from voicehook_agent import transport as vt

OFF = {"mic_muted": False, "speaker_muted": False, "away": False}


def _h(**attrs):
    return {f"vh.{k}": v for k, v in attrs.items()}


@pytest.mark.parametrize("humans, want", [
    ([], OFF),
    ([{}], OFF),                                                     # missing = "0"
    ([_h(mic_muted="1")], {**OFF, "mic_muted": True}),
    ([_h(speaker_muted="1")], {**OFF, "speaker_muted": True}),
    ([_h(away="1")], {"mic_muted": False, "speaker_muted": True, "away": True}),
    ([_h(mic_muted="1"), _h(mic_muted="0")], OFF),                   # every human, not any
    ([_h(speaker_muted="1"), _h(away="1")], {**OFF, "speaker_muted": True}),
    ([_h(away="1"), _h(away="1", mic_muted="1")], {"mic_muted": False, "speaker_muted": True,
                                                   "away": True}),
])
def test_aggregate(humans, want):
    assert relay.human_state(humans) == want


def _peer(identity, kind, **attrs):
    return SimpleNamespace(identity=identity, kind=kind, attributes=attrs)


def _ctl(peers):
    ctl = cli._Control("blau-tiger", "claude-x", relay.SayTracker(), relay.EchoSuppressor(False), 0.0)
    room = SimpleNamespace(local_participant=None, remote_participants={p.identity: p for p in peers})
    ctl.attach(room)
    return ctl, room


def test_change_is_queued_once_and_next_carries_state():
    oliver = _peer("oliver", 0)
    ctl, room = _ctl([_peer("delta-worker", 4, **{"vh.away": "1"}),            # not a human
                      _peer("codex", 0, **{"vh.role": "agent", "vh.away": "1"}),  # not a human
                      oliver])

    async def go():
        ctl.events.arm()
        ctl.humans_changed(room)               # nothing changed: still all false
        assert len(ctl.events) == 0
        oliver.attributes = {"vh.speaker_muted": "1"}
        ctl.humans_changed(room)
        ctl.humans_changed(room)               # same state again: no second event
        ev = await cli._control_handler(ctl, {"cmd": "next", "timeout": 0})
        idle = await cli._control_handler(ctl, {"cmd": "next", "timeout": 0})
        return ev, idle
    ev, idle = asyncio.run(go())
    assert ev["type"] == "human_state" and ev["role"] == "system"
    assert {k: ev[k] for k in OFF} == {**OFF, "speaker_muted": True} and "ts" in ev
    assert ev["human_state"] == {**OFF, "speaker_muted": True}
    assert idle["type"] == "timeout" and idle["human_state"] == {**OFF, "speaker_muted": True}


def test_last_human_leaving_resets_to_false():
    ctl, room = _ctl([_peer("oliver", 0, **{"vh.away": "1"})])

    async def go():
        ctl.events.arm()
        ctl.humans_changed(room)
        room.remote_participants.clear()
        ctl.humans_changed(room)
        return [await ctl.events.get(0), await ctl.events.get(0)]
    first, second = asyncio.run(go())
    assert first["away"] is True and {k: second[k] for k in OFF} == OFF


def test_bridge_peer_updated_emits_attributes_changed():
    room = vt.BridgeRoom("http://vh.test", "a-b-c-AB12", "x", name="C", model="m")
    seen = []
    room.on("participant_attributes_changed", lambda changed, p: seen.append((changed, p.identity)))
    room._dispatch({"type": "peer-joined", "identity": "oliver", "kind": 0, "attributes": {}})
    room._dispatch({"type": "peer-updated", "identity": "oliver", "kind": 0,
                    "attributes": {"vh.mic_muted": "1"}})
    assert seen == [({"vh.mic_muted": "1"}, "oliver")]
    ctl = cli._Control("a-b-c-AB12", "x", relay.SayTracker(), relay.EchoSuppressor(False), 0.0)
    ctl.humans_changed(room)
    assert ctl.human_state == {**OFF, "mic_muted": True}
