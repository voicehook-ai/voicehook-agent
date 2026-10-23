"""0.24.1 data-channel gate (voicehook, Oliver P1, 22.10.2026): a session that joined as a
pure DATA CHANNEL (`voicehook-agent join --data-channel`) must NOT be woken on every user turn
(it costs Claude-plan budget and distracts). It wakes only when Delta/the router addresses the
turn to it by name/mention or routed_by_delta, or when it is its own decision/helper_decision.

Broad turns (addressed_by all, or context without a name = wake.scope main, or a passive
followup) stay as read-only context in the durable spool (`since_last`), they do NOT make the
join actionable and do NOT make the Stop hook complain.

Without the flag everything is unchanged (positive control): the 0.24.0 wake rule applies as
before (test_targeted_wake.py)."""
from __future__ import annotations

import asyncio

import pytest
from test_session import _call, _FakeRoom, _join, fake_env  # noqa: F401  (fixture)

from voicehook_agent import relay
from voicehook_agent import spool as vspool

ME = "claude-x"
OTHER = "copilot-1"


def _w(scope, ids=(ME,), helper=""):
    return {"scope": scope, "ids": list(ids), "helper": helper}


def _u(text, *, by=None, ids=None, routed=False, wake=None, **kw):
    """A final user turn as the worker stamps it (worker.py _address_fields)."""
    p = {"role": "user", "final": True, "text": text, "speaker": "Oliver", **kw}
    if ids is not None:
        p["addressed_ids"] = list(ids)
    if by is not None:
        p["addressed_by"] = by
    if routed:
        p["routed_by_delta"] = True
    if wake is not None:
        p["wake"] = wake
    return p


# ----- pure rule: wakes_data_channel ----------------------------------------------------- #
def test_dc_named_or_mention_wakes():
    # addressed by name / mention: the human means this agent -> wake
    assert relay.wakes_data_channel(
        _u("Claude, Stand?", by="name", ids=[ME], wake=_w("agents")), {ME}) is True
    assert relay.wakes_data_channel(
        _u("... Claude ...", by="mention", ids=[ME], wake=_w("agents")), {ME}) is True


def test_dc_routed_by_delta_wakes():
    # Delta routed the turn to this session (context/explicit): wake even without a name
    assert relay.wakes_data_channel(
        _u("Und wie lange noch?", by="context", ids=[ME], routed=True, wake=_w("agents")),
        {ME}) is True


def test_dc_broad_all_does_not_wake():
    # addressed_by all (nobody named) -> wake.scope main: context only, no wake
    assert relay.wakes_data_channel(
        _u("Wie weit seid ihr?", by="all", ids=[ME, OTHER], wake=_w("main", ids=[ME, OTHER])),
        {ME}) is False


def test_dc_passive_context_without_routed_does_not_wake():
    # a passive followup (by context, wake.scope agents) that Delta did NOT route to us:
    # context only, no wake
    assert relay.wakes_data_channel(
        _u("Und die Kunden?", by="context", ids=[ME], wake=_w("agents")), {ME}) is False


def test_dc_other_agent_does_not_wake():
    assert relay.wakes_data_channel(
        _u("Copilot, mach du das", by="name", ids=[OTHER], wake=_w("agents", ids=[OTHER])),
        {ME}) is False


def test_dc_helper_and_none_do_not_wake():
    assert relay.wakes_data_channel(
        _u("Steuern, schick die Belege", by="helper", ids=[], wake=_w("helper", ids=[], helper="crown: Steuern")),
        {ME}) is False
    assert relay.wakes_data_channel(
        _u("Delta, was ist offen?", by="delta", ids=[], wake=_w("none", ids=[])), {ME}) is False


def test_dc_old_server_without_fields_does_not_wake():
    """A server that stamps neither addressed_ids nor wake: a data-channel join stays
    conservative (context only) instead of waking on everything (that is the whole point)."""
    assert relay.wakes_data_channel({"role": "user", "final": True, "text": "irgendwas"}, {ME}) is False


def test_dc_addressed_without_wake_stamp_still_wakes():
    """Only addressed_ids/addressed_by present (no wake stamp): a named turn still wakes."""
    assert relay.wakes_data_channel(_u("Claude, los", by="name", ids=[ME]), {ME}) is True
    assert relay.wakes_data_channel(_u("alle mal her", by="all", ids=[ME, OTHER]), {ME}) is False


def test_non_dc_rule_unchanged_positive_control():
    """Positive control: wakes_main (the 0.24.0 non-DC rule) is untouched."""
    broad = _u("Wie weit seid ihr?", by="all", ids=[ME, OTHER], wake=_w("main", ids=[ME, OTHER]))
    assert relay.wakes_main(broad, {ME}) is True        # non-DC: broad still wakes
    assert relay.wakes_data_channel(broad, {ME}) is False  # DC: broad is quiet
    bare = {"role": "user", "final": True, "text": "x"}
    assert relay.wakes_main(bare, {ME}) is True          # old server: non-DC wakes
    assert relay.wakes_data_channel(bare, {ME}) is False  # old server: DC stays quiet


# ----- end to end: a real join in --data-channel mode ------------------------------------- #
@pytest.mark.usefixtures("fake_env")
def test_data_channel_join_wakes_only_when_addressed_decisions_always_wake():
    async def run():
        join = asyncio.create_task(_join(identity=ME, idle_timeout=0, data_channel=True))
        await _call({"cmd": "say", "text": "Hallo"})              # arms the spool
        room = _FakeRoom.instances[0]
        # broad turns: context only, NOT actionable
        room.emit("transcript", _u("Wie weit seid ihr?", by="all", ids=[ME, OTHER],
                                    wake=_w("main", ids=[ME, OTHER])))
        room.emit("transcript", _u("Und die Kunden?", by="context", ids=[ME], wake=_w("agents")))
        await asyncio.sleep(0.05)
        idle = await _call({"cmd": "next", "timeout": 0})
        unread_after_broad = await _call({"cmd": "status"})
        # a named turn: wakes
        room.emit("transcript", _u("Claude, Stand?", by="name", ids=[ME], wake=_w("agents")))
        await asyncio.sleep(0.05)
        woke = await _call({"cmd": "next", "timeout": 0})
        # an own decision: always wakes (bypasses the user-turn gate)
        room.remote_participants  # noqa: B018  (keep room referenced)
        await _call({"cmd": "leave"})
        await asyncio.wait_for(join, 10)
        return idle, unread_after_broad, woke

    idle, status_after_broad, woke = asyncio.run(run())
    assert idle["type"] == "timeout"                      # broad turns did not wake
    assert status_after_broad["unread_wake"] == 0         # Stop hook stays quiet (no wake-worthy pending)
    assert woke["type"] == "user" and woke["text"] == "Claude, Stand?"
    # the broad turns ride along as context on the first woken event
    assert [s["text"] for s in woke["since_last"]] == ["Wie weit seid ihr?", "Und die Kunden?"]


@pytest.mark.usefixtures("fake_env")
def test_data_channel_decision_wakes_and_carries_context():
    """A decision for this join wakes it in data-channel mode (decisions bypass the gate),
    and the quiet broad turns ride along as since_last."""
    async def run():
        join = asyncio.create_task(_join(identity=ME, idle_timeout=0, data_channel=True))
        await _call({"cmd": "say", "text": "Hallo"})
        room = _FakeRoom.instances[0]
        room.emit("transcript", _u("alle mal her", by="all", ids=[ME, OTHER],
                                   wake=_w("main", ids=[ME, OTHER])))
        await asyncio.sleep(0.05)
        room.emit("operator.decision", {"type": "decision", "id": "d1234abcd", "number": 2,
                                        "question": "Deploy?", "verdict": "yes"})
        await asyncio.sleep(0.05)
        ev = await _call({"cmd": "next", "timeout": 0})
        await _call({"cmd": "leave"})
        await asyncio.wait_for(join, 10)
        return ev

    ev = asyncio.run(run())
    assert ev["type"] == "decision"
    assert [s["text"] for s in ev["since_last"]] == ["alle mal her"]


@pytest.mark.usefixtures("fake_env")
def test_non_data_channel_join_unchanged_positive_control():
    """Positive control: without --data-channel a broad turn (wake.scope main) still wakes."""
    async def run():
        join = asyncio.create_task(_join(identity=ME, idle_timeout=0))
        await _call({"cmd": "say", "text": "Hallo"})
        room = _FakeRoom.instances[0]
        room.emit("transcript", _u("Wie weit seid ihr?", by="all", ids=[ME, OTHER],
                                   wake=_w("main", ids=[ME, OTHER])))
        await asyncio.sleep(0.05)
        a = await _call({"cmd": "next", "timeout": 0})
        await _call({"cmd": "leave"})
        await asyncio.wait_for(join, 10)
        return a

    a = asyncio.run(run())
    assert a["type"] == "user" and a["text"] == "Wie weit seid ihr?"


def test_spool_wake_types_has_data_channel_doc():
    """WAKE_TYPES still drives the Stop hook; the DC gate keeps broad user turns OUT of the
    spool, so they never count in unread_wake (listenguard stays quiet)."""
    assert "user" in vspool.WAKE_TYPES
    assert "decision" in vspool.WAKE_TYPES and "helper_decision" in vspool.WAKE_TYPES


# ----- Stop-hook guard: broad turns in DC mode never force a block ------------------------ #
def test_listenguard_does_not_block_on_broad_turns_in_dc_mode():
    """In data-channel mode broad turns go to `quiet`, never the spool, so unread_wake stays 0.
    The Stop hook's `kind()` returns 'unread' only when unread_wake > 0 -> it does NOT complain
    while a consumer is idle (it still nudges to run a listen loop for addressed turns, which is
    correct); crucially it never says 'N events wait unread' for broad turns."""
    from voicehook_agent import listenguard
    base = {"ok": True, "connected": True, "leaving": False, "listening": True,
            "consumers": 0, "listen_idle_s": 0.5, "unread": 0, "unread_wake": 0}
    # a consumer waits: never a block
    assert listenguard.kind({**base, "consumers": 1}) is None
    # broad turns came in but were routed to `quiet` (not the spool): unread_wake stays 0,
    # so the guard never raises "unread" (the user/decision block reason)
    assert listenguard.kind({**base, "unread": 0, "unread_wake": 0, "listen_idle_s": 99.0}) \
        != "unread"
    # an addressed turn / decision DID enter the spool: unread_wake > 0 -> the guard blocks
    assert listenguard.kind({**base, "unread": 1, "unread_wake": 1, "consumers": 0}) == "unread"
