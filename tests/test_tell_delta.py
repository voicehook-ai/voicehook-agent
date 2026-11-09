"""`voicehook-agent tell-delta` (0.35.9, telldelta.py): the owner's main agent gives Delta an
instruction without speaking. Transport (Ravi): `delta.tell` {id, text, verbatim, priority} to
the worker only, answer `delta.tell_status` {id, state, reason?}. Helpers, joins without key and
guests get a clear error and nothing is sent."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from voicehook_agent import cli, telldelta
from voicehook_agent import relayops as vrelayops
from voicehook_agent import session as vsession


class FakeLP:
    def __init__(self) -> None:
        self.sent: list[tuple[dict, str, list | None]] = []

    async def publish_data(self, data, reliable=True, topic="", destination_identities=None):
        self.sent.append((json.loads(data), topic, destination_identities))


def _peer(ident: str, kind: int):
    return SimpleNamespace(identity=ident, kind=kind, attributes={}, track_publications={})


class FakeRoom:
    def __init__(self, worker: bool = True) -> None:
        self.local_participant = FakeLP()
        self.remote_participants = {"oliver": _peer("oliver", 0)}
        if worker:
            self.remote_participants["delta-worker"] = _peer("delta-worker", 4)


def _ctl(key: bool = True, granted: bool | None = True, room: FakeRoom | None = None):
    ctl = cli._Control("room-a", "claude-1", None, None, 0, 0)
    ctl.agent_key_set, ctl.owner_agent = key, granted
    ctl.room = FakeRoom() if room is None else room

    async def wait_room(timeout):
        return ctl.room

    ctl.wait_room = wait_room
    return ctl


def _handle(ctl, req):
    return asyncio.run(cli._control_handler(ctl, req))


def _req(text="Sag Oliver, dass der Build grün ist", **kw):
    return telldelta.request(text, kw.get("verbatim", False), kw.get("vorrang", False),
                             kw.get("wait", 0))


# ----- request / payload -----
def test_request_shape():
    assert _req() == {"cmd": "delta_tell", "text": "Sag Oliver, dass der Build grün ist",
                      "verbatim": False, "priority": "normal", "wait": 0.0}
    r = _req("  a\n  b ", verbatim=True, vorrang=True, wait=5)
    assert r["text"] == "a b" and r["verbatim"] is True and r["priority"] == "vorrang"
    assert r["wait"] == 5.0


@pytest.mark.parametrize("text,wait", [("", 0), ("   ", 0), ("x" * (telldelta.TEXT_MAX + 1), 0),
                                       ("ok", -1), ("ok", "bald")])
def test_request_bad_input(text, wait):
    with pytest.raises(telldelta.TellError):
        telldelta.request(text, wait=wait)


def test_payload_is_exactly_ravis_format():
    p = telldelta.payload("dt-1", _req(vorrang=True, verbatim=True))
    assert p == {"id": "dt-1", "text": "Sag Oliver, dass der Build grün ist", "verbatim": True,
                 "priority": "vorrang"}


# ----- join side -----
def test_join_sends_delta_tell_to_the_worker_only():
    ctl = _ctl()
    out = _handle(ctl, _req(verbatim=True, vorrang=True))
    assert out["ok"] is True and out["type"] == "delta_tell" and out["state"] == "sent"
    assert telldelta.ID_RX.fullmatch(out["id"])
    [(data, topic, dest)] = ctl.room.local_participant.sent
    assert topic == "delta.tell" and dest == ["delta-worker"]
    assert data == {"id": out["id"], "text": "Sag Oliver, dass der Build grün ist",
                    "verbatim": True, "priority": "vorrang"}


@pytest.mark.parametrize("key,granted,hint", [
    (False, None, telldelta.HINT_NO_KEY),     # join without VOICEHOOK_AGENT_KEY (guest agent)
    (False, False, telldelta.HINT_NO_KEY),
    (True, False, telldelta.HINT_NOT_OWNER),  # key not of this room's account
])
def test_not_owner_agent_sends_nothing(key, granted, hint):
    ctl = _ctl(key, granted)
    out = _handle(ctl, _req())
    assert out == {"ok": False, "type": "delta_tell", "error": "not_owner_agent", "hint": hint}
    assert ctl.room.local_participant.sent == []


def test_granted_unknown_is_sent_worker_decides():
    ctl = _ctl(True, None)   # bridge answer: the token cannot tell
    assert _handle(ctl, _req())["ok"] is True
    assert len(ctl.room.local_participant.sent) == 1


def test_no_worker_in_room():
    ctl = _ctl(room=FakeRoom(worker=False))
    out = _handle(ctl, _req())
    assert out["ok"] is False and out["error"] == "no_delta"
    assert ctl.room.local_participant.sent == []


def test_invalid_on_socket():
    out = _handle(_ctl(), {"cmd": "delta_tell", "text": "  "})
    assert out["ok"] is False and out["type"] == "invalid"


# ----- delta.tell_status -----
def test_tracker_only_own_ids_and_final_states():
    t = telldelta.Tracker()
    t.sent("dt-a", "x")
    assert t.update({"id": "dt-other", "state": "spoken"}) is None
    assert t.update({"id": "dt-a", "state": "bogus"}) is None
    assert t.update({"id": "dt-a", "state": "queued"}) == {"id": "dt-a", "state": "queued"}
    assert t.update({"id": "dt-a", "state": "spoken"}) == {"id": "dt-a", "state": "spoken"}
    assert t.update({"id": "dt-a", "state": "queued"}) is None   # late queued never undoes spoken
    assert t.get("dt-a")["state"] == "spoken"


def _wait_with_status(ctl, req, status_after: dict, delay=0.01):
    async def run():
        async def feed():
            await asyncio.sleep(delay)
            # the first sent id gets the status
            tid = next(iter(ctl.tell._recs))
            ctl.tell.update({**status_after, "id": tid})
        task = asyncio.ensure_future(feed())
        out = await cli._control_handler(ctl, req)
        await task
        return out
    return asyncio.run(run())


@pytest.mark.parametrize("state", ["spoken", "covered"])
def test_wait_returns_spoken_or_covered(state):
    ctl = _ctl()
    out = _wait_with_status(ctl, _req(wait=2), {"state": state})
    assert out["ok"] is True and out["state"] == state and "timeout" not in out
    assert out["note"] == telldelta.NOTES[state]


def test_wait_rejected_not_owner_agent_has_hint():
    ctl = _ctl(True, None)
    out = _wait_with_status(ctl, _req(wait=2), {"state": "rejected", "reason": "not_owner_agent"})
    assert out["ok"] is False and out["state"] == "rejected"
    assert out["error"] == "not_owner_agent" and out["reason"] == "not_owner_agent"
    assert out["hint"] == telldelta.HINT_NOT_OWNER


def test_wait_timeout_keeps_last_state():
    ctl = _ctl()
    out = _wait_with_status(ctl, _req(wait=0.1), {"state": "queued"})
    assert out["ok"] is True and out["state"] == "queued" and out["timeout"] is True


def test_late_rejected_becomes_next_event():
    t = telldelta.Tracker()
    t.sent("dt-b", "x")
    change = t.update({"id": "dt-b", "state": "rejected", "reason": "not_owner_agent"})
    ev = telldelta.status_event(change)
    assert ev["type"] == "delta_tell_status" and ev["id"] == "dt-b" and ev["state"] == "rejected"
    assert ev["hint"] == telldelta.HINT_NOT_OWNER


# ----- CLI -----
def _sock(monkeypatch, ctl=None, missing=False):
    reqs = []

    def resolve(session, wait=0):
        if missing:
            raise vsession.SessionError("no running join")
        return "/fake/ctl.sock"

    def request(sock, req, timeout=None):
        reqs.append((req, timeout))
        return asyncio.run(cli._control_handler(ctl, req))

    monkeypatch.setattr(vsession, "resolve_socket", resolve)
    monkeypatch.setattr(vsession, "request", request)
    return reqs


def _main(argv) -> int:
    with pytest.raises(SystemExit) as e:
        cli.main(argv)
    return e.value.code


def test_cli_one_json_line_with_id(monkeypatch, capsys):
    ctl = _ctl()
    reqs = _sock(monkeypatch, ctl)
    assert _main(["tell-delta", "Frag", "nach,", "ob", "das", "Angebot", "raus", "darf",
                  "--wörtlich", "--vorrang"]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 1
    got = json.loads(lines[0])
    assert got["ok"] is True and got["id"].startswith("dt-")
    (req, timeout), = reqs
    assert req["verbatim"] is True and req["priority"] == "vorrang" and req["wait"] == 0
    assert timeout == telldelta.SOCK_EXTRA_S


def test_cli_ascii_aliases(monkeypatch, capsys):
    reqs = _sock(monkeypatch, _ctl())
    assert _main(["tell-delta", "x", "--woertlich", "--priority", "--wait", "0"]) == 0
    assert reqs[0][0]["verbatim"] is True and reqs[0][0]["priority"] == "vorrang"
    reqs.clear()
    assert _main(["tell-delta", "x", "--verbatim"]) == 0
    assert reqs[0][0]["verbatim"] is True and reqs[0][0]["priority"] == "normal"


def test_cli_not_owner_exit_1(monkeypatch, capsys):
    _sock(monkeypatch, _ctl(False, None))
    assert _main(["tell-delta", "x"]) == 1
    got = json.loads(capsys.readouterr().out)
    assert got["error"] == "not_owner_agent" and "VOICEHOOK_AGENT_KEY" in got["hint"]


def test_cli_helper_session_clear_error(monkeypatch, capsys):
    _sock(monkeypatch, missing=True)
    monkeypatch.setattr(vrelayops, "client_relay", lambda s: {"url": "u", "token": "t"})
    assert _main(["tell-delta", "x"]) == 1
    got = json.loads(capsys.readouterr().out)
    assert got["error"] == "helper_session" and got["hint"] == telldelta.HINT_HELPER


def test_cli_no_join_exit_3(monkeypatch, capsys):
    _sock(monkeypatch, missing=True)
    monkeypatch.setattr(vrelayops, "client_relay", lambda s: None)
    assert _main(["tell-delta", "x"]) == 3
    assert json.loads(capsys.readouterr().out)["error"] == "no_join"


def test_cli_empty_exit_2(monkeypatch, capsys):
    reqs = _sock(monkeypatch, _ctl())
    assert _main(["tell-delta", " "]) == 2
    assert reqs == []


def test_cli_old_join(monkeypatch, capsys):
    monkeypatch.setattr(vsession, "resolve_socket", lambda s, wait=0: "/fake/ctl.sock")
    monkeypatch.setattr(vsession, "request", lambda s, r, timeout=None: {
        "ok": False, "error": "unknown cmd 'delta_tell'"})
    assert _main(["tell-delta", "x"]) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "join_too_old"


def test_cli_wait_passes_socket_timeout(monkeypatch, capsys):
    ctl = _ctl()
    reqs = _sock(monkeypatch, ctl)
    assert _main(["tell-delta", "x", "--wait", "0.05"]) == 0
    assert reqs[0][1] == pytest.approx(0.05 + telldelta.SOCK_EXTRA_S)
    assert json.loads(capsys.readouterr().out)["timeout"] is True


def test_help_explains_purpose_and_flags(capsys):
    assert _main(["tell-delta", "--help"]) == 0
    out = capsys.readouterr().out
    assert "ohne selbst zu sprechen" in out and "--wörtlich" in out and "--vorrang" in out
    assert "unverändert" in out and "vor die übrigen Ansagen" in out
