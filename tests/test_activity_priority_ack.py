"""0.35.6 (coordinated with the server): `activity --priority high` / `--decision <id>` set
the fields `priority` / `decision` on the operator.activity packet; the main agent (own join)
acknowledges the answer to its OWN decision once with operator.decision_ack {ids, sender}."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from voicehook_agent import cli, relay, relayops, spool


class _Local:
    def __init__(self):
        self.published = []

    async def publish_data(self, data, reliable=True, topic=""):
        self.published.append((topic, json.loads(data)))


def _ctl(tmp_path, events=None):
    ctl = cli._Control("room", "claude-x", relay.SayTracker(), relay.EchoSuppressor(False), 0.0)
    ctl.attach(SimpleNamespace(local_participant=_Local(), remote_participants={}))
    ctl.activity_path = tmp_path / "activity.log"
    if events is not None:
        ctl.events = events
    return ctl


def _parse(argv):
    seen = {}

    def grab(a):
        seen["args"] = a
        return 0
    orig = cli._run_client
    cli._run_client = grab
    try:
        cli.main(argv)
    except SystemExit:
        pass
    finally:
        cli._run_client = orig
    return cli._client_request(seen["args"])


def _acts(ctl):
    return [p for t, p in ctl.room.local_participant.published if t == "operator.activity"]


def _acks(ctl):
    return [p for t, p in ctl.room.local_participant.published if t == cli.TOPIC_DECISION_ACK]


# ----- activity --priority / --decision --------------------------------------------------
def test_cli_flags_go_into_the_request():
    req = _parse(["activity", "--priority", "high", "--decision", "d1a2b3c4d", "angekommen: Deploy"])
    assert req["priority"] == "high" and req["decision"] == "d1a2b3c4d"
    req = _parse(["activity", "nur eine Zeile"])
    assert "priority" not in req and "decision" not in req


def test_cli_rejects_a_bad_decision_id():
    with pytest.raises(ValueError):
        _parse(["activity", "--decision", "xyz", "angekommen"])


def test_priority_and_decision_on_the_packet_once(tmp_path):
    ctl = _ctl(tmp_path)

    async def run():
        r1 = await cli._control_handler(ctl, {"cmd": "activity", "text": "eins"})
        r2 = await cli._control_handler(ctl, {"cmd": "activity", "text": "angekommen: Deploy",
                                              "priority": "high", "decision": "d1a2b3c4d"})
        assert r2["priority"] == "high" and r2["decision"] == "d1a2b3c4d"
        await cli._publish_activity(ctl, [r1["line"], r2["line"]])
        await cli._publish_activity(ctl, [r1["line"], r2["line"]])   # same lines again
        r3 = await cli._control_handler(ctl, {"cmd": "activity", "text": "drei"})
        await cli._publish_activity(ctl, [r2["line"], r3["line"]])
    asyncio.run(run())
    first, second, third = _acts(ctl)
    assert first["priority"] == "high" and first["decision"] == "d1a2b3c4d"
    assert "priority" not in second and "decision" not in second       # only once
    assert "priority" not in third and "decision" not in third


def test_invalid_flags_are_dropped(tmp_path):
    ctl = _ctl(tmp_path)

    async def run():
        r = await cli._control_handler(ctl, {"cmd": "activity", "text": "x", "priority": "urgent",
                                             "decision": "nope"})
        await cli._publish_activity(ctl, [r["line"]])
    asyncio.run(run())
    assert "priority" not in _acts(ctl)[0] and "decision" not in _acts(ctl)[0]


def test_helper_relay_body_carries_the_flags(monkeypatch):
    sent = {}
    monkeypatch.setattr(relayops, "_http", lambda rel, m, url, body, to: sent.update(body) or (200, {}))
    req = {"cmd": "activity", "text": "angekommen", "priority": "high", "decision": "d1a2b3c4d"}
    body = {"text": req["text"], **cli.vrelay_meta(req), **cli.activity_flags(req)}
    relayops.remote_activity({"url": "https://vh.test/api/activity/relay", "token": "t"}, "Ravi", body)
    assert sent["priority"] == "high" and sent["decision"] == "d1a2b3c4d"


def test_help_names_the_flags(capsys):
    with pytest.raises(SystemExit):
        cli.main(["activity", "--help"])
    out = capsys.readouterr().out
    assert "--priority" in out and "--decision" in out and "--reply" in out


# ----- operator.decision_ack for the own decisions ------------------------------------------
def test_own_decision_acked_once_via_next(tmp_path):
    q = spool.SpoolQueue(tmp_path, armed=True)
    ctl = _ctl(tmp_path, events=q)
    ev = {"type": "decision", "id": "d1a2b3c4d", "question": "PR mergen?", "option": 0}

    async def run():
        q.put_nowait(dict(ev))
        r = await cli._control_handler(ctl, {"cmd": "next", "timeout": 1})
        assert r["type"] == "decision"
        q.put_nowait(dict(ev))                                 # the same answer again
        await cli._control_handler(ctl, {"cmd": "next", "timeout": 1})
    asyncio.run(run())
    assert _acks(ctl) == [{"ids": ["d1a2b3c4d"], "sender": "claude-x"}]
    # a restarted join (same session dir) never acks it again
    q2 = spool.SpoolQueue(tmp_path, armed=True)
    assert q2.first_decision_ack("d1a2b3c4d") is False
    assert q2.first_decision_ack("d00000001") is True       # other ids still free
    q2.clear()
    assert not (tmp_path / spool.ACKS_NAME).exists()


def test_helper_decision_and_events_without_id_are_not_acked(tmp_path):
    ctl = _ctl(tmp_path, events=spool.SpoolQueue(tmp_path, armed=True))

    async def run():
        await cli._ack_own_decisions(ctl, [
            {"type": "decision", "id": "d1a2b3c4d", "sender": "crown: Ravi"},   # a helper's card
            {"type": "helper_decision", "id": "d22222222"},
            {"type": "decision", "id": "bad"},
            {"type": "user", "text": "hallo"}])
    asyncio.run(run())
    assert _acks(ctl) == []


def test_mid_turn_take_acks_too(tmp_path):
    q = spool.SpoolQueue(tmp_path, armed=True)
    ctl = _ctl(tmp_path, events=q)

    async def run():
        q.put_nowait({"type": "decision", "id": "d33333333", "question": "q?", "option": 1})
        r = await cli._control_handler(ctl, {"cmd": "take_wake", "types": ["decision"]})
        assert r["events"]
    asyncio.run(run())
    assert _acks(ctl) == [{"ids": ["d33333333"], "sender": "claude-x"}]


# ----- delta.msg -> next event delta_msg, activity --reply ----------------------------------
def test_delta_msg_for_us_or_all_only():
    own = {"claude-x"}
    ev = cli.delta_msg_event({"id": "m1", "to": "all", "text": "Wie weit  ist der Deploy?"}, own)
    assert ev["type"] == "delta_msg" and ev["id"] == "m1" and ev["text"] == "Wie weit ist der Deploy?"
    assert ev["reply_hint"] == ('Delta fragt: Wie weit ist der Deploy? — antworte per '
                                'voicehook-agent activity --reply m1 "<Antwort>"')
    assert cli.delta_msg_event({"id": "m2", "to": ["claude-x", "codex-y"], "text": "x"}, own)
    assert cli.delta_msg_event({"id": "m3", "to": ["codex-y"], "text": "x"}, own) is None
    assert cli.delta_msg_event({"id": "m4", "to": "claude-x", "text": "x"}, own) is None
    assert cli.delta_msg_event({"id": "bad id!", "to": "all", "text": "x"}, own) is None
    assert cli.delta_msg_event({"id": "m5", "to": "all", "text": "  "}, own) is None


def test_delta_msg_wakes_listen_and_mid_turn():
    from voicehook_agent import midturn
    assert "delta_msg" in spool.WAKE_TYPES and "delta_msg" in cli.LISTEN_WAKE
    assert "delta_msg" in midturn.TYPES
    t = midturn.text_for({"events": [{"type": "delta_msg", "id": "m1", "text": "Status?"}], "now": 0})
    assert 'Delta fragt: Status? — antworte per voicehook-agent activity --reply m1 "<Antwort>"' in t
    assert "Bestätige kurz" not in t


def test_delta_msg_next_event_from_the_spool(tmp_path):
    q = spool.SpoolQueue(tmp_path, armed=True)
    ctl = _ctl(tmp_path, events=q)

    async def run():
        q.put_nowait(cli.delta_msg_event({"id": "m1", "to": "all", "text": "Status?"}, {"claude-x"}))
        return await cli._control_handler(ctl, {"cmd": "next", "timeout": 1})
    r = asyncio.run(run())
    assert r["type"] == "delta_msg" and r["id"] == "m1" and "--reply m1" in r["reply_hint"]


def test_activity_reply_field(tmp_path):
    req = _parse(["activity", "--reply", "m1", "Deploy läuft, fertig in fünf Minuten"])
    assert req["reply"] == "m1"
    with pytest.raises(ValueError):
        _parse(["activity", "--reply", "bad id!", "x"])
    ctl = _ctl(tmp_path)

    async def run():
        r = await cli._control_handler(ctl, {"cmd": "activity", "text": "fertig gleich", "reply": "m1"})
        await cli._publish_activity(ctl, [r["line"]])
        await cli._publish_activity(ctl, [r["line"]])
    asyncio.run(run())
    a, b = _acts(ctl)
    assert a["reply"] == "m1" and "reply" not in b
