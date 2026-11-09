"""0.35.9: a growing utterance (hold mode) spools only its new part (call ember-orbit 09.11.)."""
from __future__ import annotations

import asyncio

import test_session as TS

from voicehook_agent import relay

fake_env = TS.fake_env     # the join harness of test_session (fake LiveKit room)

FINAL = {"final": True}


def _turn(c: relay.UserContinuation, text: str, speaker: str = "Malik", **extra) -> dict | None:
    payload = {**FINAL, "speaker": speaker, **extra}
    ev = relay.user_turn_event("user", text, payload, now=1.0)
    assert ev is not None
    return c.apply(ev, payload)


V1 = "Ich habe mir die Pipeline angesehen und die Ladezeiten sind zu hoch."
V2 = V1 + " Außerdem fehlen die Logs vom Wochenende."
V3 = V2 + " Bitte prüf das bis morgen."


def test_growing_texts_spool_only_the_new_part():
    c = relay.UserContinuation()
    e1 = _turn(c, V1)
    assert e1["text"] == V1 and "continued" not in e1
    e2 = _turn(c, V2)
    assert e2["text"] == "Außerdem fehlen die Logs vom Wochenende."
    assert e2["continued"] is True and e2["full_len"] == len(V2)
    assert e2["speaker"] == "Malik"
    e3 = _turn(c, V3)
    assert e3["text"] == "Bitte prüf das bis morgen."
    assert e3["continued"] is True and e3["full_len"] == len(V3)


def test_same_text_again_spools_nothing():
    c = relay.UserContinuation()
    assert _turn(c, V1) is not None
    assert _turn(c, V1) is None
    assert _turn(c, "  " + V1 + "  ") is None   # stripped by user_turn_event
    e = _turn(c, V2)                            # the last full text is still the reference
    assert e["text"] == "Außerdem fehlen die Logs vom Wochenende."


def test_rewritten_sentence_stays_a_normal_event_and_becomes_the_reference():
    c = relay.UserContinuation()
    _turn(c, V1)
    rew = "Ich habe mir die Pipeline angesehen, die Ladezeiten passen doch."
    e = _turn(c, rew)
    assert e["text"] == rew and "continued" not in e
    e2 = _turn(c, rew + " Danke.")
    assert e2["text"] == "Danke." and e2["continued"] is True


def test_no_continuation_inside_a_word():
    c = relay.UserContinuation()
    _turn(c, "Ja")
    e = _turn(c, "Jawohl, mach das.")
    assert e["text"] == "Jawohl, mach das." and "continued" not in e


def test_two_speakers_are_tracked_apart():
    c = relay.UserContinuation()
    _turn(c, V1, speaker="Malik")
    o = _turn(c, V2, speaker="Oliver")          # Oliver never said V1: a normal event
    assert o["text"] == V2 and "continued" not in o
    m = _turn(c, V2, speaker="Malik")
    assert m["continued"] is True and m["text"] == "Außerdem fehlen die Logs vom Wochenende."
    assert _turn(c, V2, speaker="Oliver") is None


def test_server_marking_seg_and_fresh_wins():
    c = relay.UserContinuation()
    e1 = _turn(c, V1, seg="s1", fresh=V1)
    assert e1["text"] == V1 and e1["seg"] == "s1" and "continued" not in e1
    e2 = _turn(c, V2, seg="s1", fresh="Außerdem fehlen die Logs vom Wochenende.", continued=True)
    assert e2 == {**e2, "text": "Außerdem fehlen die Logs vom Wochenende.", "seg": "s1",
                  "continued": True, "full_len": len(V2)}
    assert _turn(c, V2, seg="s1", fresh="", continued=True) is None


def test_server_marking_keeps_the_reference_for_an_old_server_turn():
    c = relay.UserContinuation()
    _turn(c, V2, seg="s1", fresh="Außerdem fehlen die Logs vom Wochenende.", continued=True)
    e = _turn(c, V3)                            # next turn without seg/fresh: prefix path
    assert e["text"] == "Bitte prüf das bis morgen." and e["continued"] is True


def test_server_fresh_trusted_over_prefix():
    c = relay.UserContinuation()
    _turn(c, V1)
    e = _turn(c, V2, seg="s9", fresh=V2)        # server says: a new line, all of it fresh
    assert e["text"] == V2 and e["seg"] == "s9" and "continued" not in e


def test_field_names_are_constants():
    assert (relay.SEG_FIELD, relay.FRESH_FIELD, relay.CONTINUED_FIELD, relay.FULL_LEN_FIELD) == \
        ("seg", "fresh", "continued", "full_len")


def test_join_spools_only_the_new_part(fake_env):
    """End to end through the join's data handler: 3 growing versions + 1 repeat -> 3 events."""
    async def run():
        join = asyncio.create_task(TS._join(idle_timeout=0))
        await TS._call({"cmd": "next", "timeout": 0})        # arms the queue
        room = TS._FakeRoom.instances[0]
        for t in (V1, V2, V2, V3):
            room.emit("transcript", {"role": "user", "text": t, "speaker": "Malik"})
        got = [await TS._call({"cmd": "next", "timeout": 2}) for _ in range(4)]
        await TS._call({"cmd": "leave"})
        await asyncio.wait_for(join, 5)
        return got

    got = asyncio.run(run())
    assert [g["type"] for g in got] == ["user", "user", "user", "timeout"]
    assert got[0]["text"] == V1 and "continued" not in got[0]
    assert got[1]["text"] == "Außerdem fehlen die Logs vom Wochenende." and got[1]["full_len"] == len(V2)
    assert got[2]["text"] == "Bitte prüf das bis morgen." and got[2]["continued"] is True
