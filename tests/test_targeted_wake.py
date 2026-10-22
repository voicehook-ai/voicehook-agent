"""0.24.0 targeted wake (server voicehook-v4 #606): every final user turn carries
`wake: {scope, ids, helper}`. A main session wakes its LLM only when its identity is in
`wake.ids` and scope != helper; on scope helper it only forwards to the helper whose relay key
is `wake.helper`; scope none wakes nobody. Not-woken turns come with the next woken event as
`since_last`. Without `wake` (old server) everything as before."""
from __future__ import annotations

import asyncio

import pytest
from test_helper_user_turn import PAYLOAD
from test_relay_decide import _clean_env  # noqa: F401  (fixture)
from test_relay_decide import _join as _rjoin
from test_session import _call, _FakeRoom, _join, fake_env  # noqa: F401  (fixture)

from voicehook_agent import cli, midturn, relay
from voicehook_agent import relayops as ro

ME = "claude-x"


def _w(scope, ids=(ME,), helper=""):
    return {"scope": scope, "ids": list(ids), "helper": helper}


# ----- pure rule ------------------------------------------------------------------------
def test_rule_main_session():
    p = {"role": "user", "final": True, "text": "x"}
    assert relay.wakes_main(p, {ME}) is True                                  # old server
    assert relay.wakes_main({**p, "wake": _w("agents")}, {ME}) is True
    assert relay.wakes_main({**p, "wake": _w("main")}, {ME}) is True
    assert relay.wakes_main({**p, "wake": _w("agents", ids=["other"])}, {ME}) is False
    assert relay.wakes_main({**p, "wake": _w("helper")}, {ME}) is False      # only forwards
    assert relay.wakes_main({**p, "wake": _w("none", ids=[])}, {ME}) is False
    assert relay.wakes_main({**p, "wake": {"scope": "bogus"}}, {ME}) is True  # unusable = absent


def test_rule_helper_session():
    k = "crown: Steuern"
    assert relay.wakes_helper({"text": "x"}, k) is True                      # old server
    assert relay.wakes_helper({"wake": _w("helper", helper=k)}, k) is True
    assert relay.wakes_helper({"wake": _w("helper", helper="crown: Promo")}, k) is False
    assert relay.wakes_helper({"wake": _w("agents", helper=k)}, k) is False
    assert relay.wakes_helper({"wake": _w("none", helper=k)}, k) is False


def test_quiet_turns_compact_and_bounded():
    q = relay.QuietTurns(maxlen=2)
    for i in range(3):
        q.add({"text": f"satz {i} " + "x" * 400, "ts": i, "speaker": "Oliver"}, "none")
    ev = q.attach({"type": "user", "text": "jetzt du"})
    assert [s["text"][:6] for s in ev["since_last"]] == ["satz 1", "satz 2"]
    assert len(ev["since_last"][0]["text"]) == relay.QUIET_TEXT_MAX
    assert ev["since_last_dropped"] == 1 and ev["since_last"][0]["scope"] == "none"
    assert len(q) == 0 and "since_last" not in q.attach({"type": "user"})


# ----- helper forwarding ------------------------------------------------------------------
def test_scope_helper_forwards_only_to_wake_helper(monkeypatch):
    d, ctl, _local = _rjoin(monkeypatch)
    p = {**PAYLOAD, "wake": _w("helper", helper="crown: Steuern")}
    assert cli._route_helper_user_turn(ctl, p, {ME}) is True
    got = ro.take_jsonl(ro.answer_path(d, "Steuern"), ro._wanted)
    assert [e["type"] for e in got] == ["user_turn"]
    # wake.helper names another helper than to_key: nobody gets it
    p2 = {**PAYLOAD, "wake": _w("helper", helper="crown: Promo")}
    assert cli._route_helper_user_turn(ctl, p2, {ME}) is False
    # not scope helper (agents / none): never forwarded, even with to_kind helper
    for sc in ("agents", "none"):
        assert cli._route_helper_user_turn(ctl, {**PAYLOAD, "wake": _w(sc)}, {ME}) is False
    # wake alone (no to/to_kind) is enough: name from the relay key
    bare = {"role": "user", "final": True, "text": "Steuern, los", "speaker": "Oliver",
            "wake": _w("helper", helper="crown: Steuern")}
    assert cli._route_helper_user_turn(ctl, bare, {ME}) is True
    got = ro.take_jsonl(ro.answer_path(d, "Steuern"), ro._wanted)
    assert got[0]["to"] == ["Steuern"] and got[0]["text"] == "Steuern, los"
    # we are not the forwarding main session
    assert cli._route_helper_user_turn(
        ctl, {**bare, "wake": _w("helper", ids=["other"], helper="crown: Steuern")}, {ME}) is False


# ----- end to end: real join, fake room -------------------------------------------------------
def _u(text, wake=None, **kw):
    p = {"role": "user", "final": True, "text": text, "speaker": "Oliver", **kw}
    if wake is not None:
        p["wake"] = wake
    return p


@pytest.mark.usefixtures("fake_env")
def test_join_wakes_only_when_meant_and_carries_since_last():
    async def run():
        join = asyncio.create_task(_join(identity=ME, idle_timeout=0))
        await _call({"cmd": "say", "text": "Hallo"})              # arms the spool
        room = _FakeRoom.instances[0]
        room.emit("transcript", _u("Delta, wie spät ist es?", _w("none", ids=[])))
        room.emit("transcript", _u("Copilot, mach du das", _w("agents", ids=["copilot-1"])))
        room.emit("transcript", _u("Steuern, schick die Belege",
                                   _w("helper", helper="crown: Steuern"),
                                   to=["Steuern"], to_kind="helper", to_key="crown: Steuern",
                                   to_op=ME))
        await asyncio.sleep(0.05)
        idle = await _call({"cmd": "next", "timeout": 0})
        room.emit("transcript", _u("Claude, Stand?", _w("agents")))
        await asyncio.sleep(0.05)
        woke = await _call({"cmd": "next", "timeout": 0})
        room.emit("transcript", _u("Und alle?", _w("main", ids=[ME, "copilot-1"])))
        await asyncio.sleep(0.05)
        again = await _call({"cmd": "next", "timeout": 0})
        await _call({"cmd": "leave"})
        await asyncio.wait_for(join, 10)
        return idle, woke, again

    idle, woke, again = asyncio.run(run())
    assert idle["type"] == "timeout"                     # none / other agent / helper: no wake
    assert woke["type"] == "user" and woke["text"] == "Claude, Stand?"
    sl = woke["since_last"]
    assert [s["text"] for s in sl] == ["Delta, wie spät ist es?", "Copilot, mach du das",
                                       "Steuern, schick die Belege"]
    assert [s["scope"] for s in sl] == ["none", "agents", "helper"]
    assert sl[2]["to"] == ["Steuern"] and sl[0]["speaker"] == "Oliver"
    assert again["text"] == "Und alle?" and "since_last" not in again   # delivered once


@pytest.mark.usefixtures("fake_env")
def test_join_without_wake_unchanged():
    """Positive control for the old server: every final user turn wakes, no since_last."""
    async def run():
        join = asyncio.create_task(_join(identity=ME, idle_timeout=0))
        await _call({"cmd": "say", "text": "Hallo"})
        room = _FakeRoom.instances[0]
        room.emit("transcript", _u("erste"))
        room.emit("transcript", _u("zweite"))
        await asyncio.sleep(0.05)
        a = await _call({"cmd": "next", "timeout": 0})
        b = await _call({"cmd": "next", "timeout": 0})
        await _call({"cmd": "leave"})
        await asyncio.wait_for(join, 10)
        return a, b

    a, b = asyncio.run(run())
    assert (a["text"], b["text"]) == ("erste", "zweite")
    assert "since_last" not in a and "since_last" not in b


def test_since_last_rides_on_a_decision_and_shows_mid_turn():
    ev = relay.QuietTurns()
    ev.add({"text": "Delta, danke", "ts": 1.0, "speaker": "Oliver"}, "none")
    dec = ev.attach({"type": "decision", "number": 2, "question": "Deploy?", "verdict": "yes", "ts": 9.0})
    t = midturn.text_for({"now": 10.0, "events": [dec]})
    assert 'Seit dem letzten Mal im Call (nicht an dich, nur Kontext): Oliver: "Delta, danke".' in t
    assert "Entscheidung im Call zu #2" in t
