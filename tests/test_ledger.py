"""operator.ledger (ledger.py): the worker's full list of this agent's open gaps -> ledger.json,
a `next` event on a change of the id set, the hook's todo line, `status` counts."""
from __future__ import annotations

import asyncio
import io
import json
from types import SimpleNamespace
from typing import ClassVar

import pytest

from voicehook_agent import ledger, midturn
from voicehook_agent import session as vs

cli = pytest.importorskip("voicehook_agent.cli", reason="livekit/httpx not installed")

TS = 1794400000.0
BOARD = {"id": "board_stale:me", "kind": "board_stale", "since": TS - 700, "ref": "me",
         "hint": "Board seit 11 min nicht aktualisiert, bitte Status setzen."}
QUESTION = {"id": "question_not_decide:a1", "kind": "question_not_decide", "since": TS - 90,
            "ref": "a1", "hint": "Frage als Karte stellen (decide).", "sub": "helper-x"}


def _pkt(items, v=1, ts=TS, **extra):
    return {"v": v, "ts": ts, "agent": "me", "thresholds": {"board_stale_s": 600}, "items": items,
            **extra}


def test_for_us():
    assert ledger.for_us({"agent": "me"}, {"me", "x"})
    assert not ledger.for_us({"agent": "other"}, {"me"})
    assert not ledger.for_us({"agent": 5}, {"me"})
    assert ledger.for_us({}, {"me"}) and ledger.for_us({"agent": None}, {"me"})


# ---- ledger.py -------------------------------------------------------------------------- #
def test_parse_keeps_unknown_fields():
    st = ledger.parse(_pkt([QUESTION], future={"x": 1}))
    assert st["future"] == {"x": 1} and st["items"][0]["sub"] == "helper-x"


@pytest.mark.parametrize("bad", [
    _pkt([], v=2), _pkt([], v="1"), _pkt([], v=True), {"items": []}, _pkt("x"),
    _pkt([{"id": "a"}]), _pkt([{"id": 1, "kind": "k"}]), _pkt(["a"]), [1]])
def test_parse_rejects(bad):
    with pytest.raises(ledger.LedgerError):
        ledger.parse(bad)


def test_unknown_version_text():
    with pytest.raises(ledger.LedgerError, match=r"unbekannte Version v=2 ignoriert"):
        ledger.parse(_pkt([], v=2))


def test_summary():
    assert ledger.summary(_pkt([BOARD, QUESTION])) == \
        "2 offene Punkte: Board seit 11 min alt; Frage nur als Aktivität gemeldet."
    assert ledger.summary(_pkt([])) == "Alles erledigt."
    odd = {"id": "x:1", "kind": "new_kind", "hint": "Etwas Neues."}
    assert ledger.summary(_pkt([odd])) == "1 offener Punkt: Etwas Neues."


def test_write_read_roundtrip(tmp_path):
    ledger.write(tmp_path, ledger.parse(_pkt([QUESTION])))
    assert (tmp_path / "ledger.json").stat().st_mode & 0o777 == 0o600
    assert ledger.read(tmp_path)["items"][0]["sub"] == "helper-x"
    assert not list(tmp_path.glob("*.tmp"))


def test_hook_line_change_gap_stale(tmp_path):
    ledger.write(tmp_path, ledger.parse(_pkt([BOARD, QUESTION])))
    line = ledger.hook_line(tmp_path, now=TS + 5)
    assert line.startswith("voicehook offene Punkte (vom Call): 2 offene Punkte: Board seit 11 min")
    assert "Frage als Karte stellen (decide)" in line
    assert ledger.hook_line(tmp_path, now=TS + 60) is None                  # same set, in the gap
    assert ledger.hook_line(tmp_path, now=TS + 5 + ledger.REMIND_GAP_S) is not None   # again
    ledger.write(tmp_path, ledger.parse(_pkt([], ts=TS + 300)))
    assert ledger.hook_line(tmp_path, now=TS + 301) == "voicehook offene Punkte: alles erledigt."
    assert ledger.hook_line(tmp_path, now=TS + 302) is None
    ledger.write(tmp_path, ledger.parse(_pkt([BOARD], ts=TS)))
    stale = ledger.hook_line(tmp_path, now=TS + 900)                        # > 10 min old
    assert "veraltet (vor 15 min)" in stale and "Board" not in stale
    assert ledger.hook_line(tmp_path, now=TS + 960) is None                 # one note per packet


def test_hook_line_empty_never_shown_is_silent(tmp_path):
    ledger.write(tmp_path, ledger.parse(_pkt([])))
    assert ledger.hook_line(tmp_path, now=TS) is None
    assert ledger.hook_line(tmp_path / "missing", now=TS) is None


def test_midturn_hook_shows_items(tmp_path, monkeypatch):
    from voicehook_agent import listenguard
    ledger.write(tmp_path, ledger.parse(_pkt([BOARD], ts=__import__("time").time())))
    monkeypatch.setattr(listenguard, "own_join_dirs", lambda ev: [tmp_path])
    monkeypatch.setattr(midturn, "take", lambda d: None)
    out = midturn.handle(json.dumps({"hook_event_name": "PostToolUse", "session_id": "s"}))
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "voicehook offene Punkte (vom Call): 1 offener Punkt: Board seit" in ctx
    assert midturn.handle(json.dumps({"hook_event_name": "PostToolUse"})) is None   # in the gap


def test_midturn_hook_stale_note(tmp_path, monkeypatch):
    from voicehook_agent import listenguard
    ledger.write(tmp_path, ledger.parse(_pkt([BOARD], ts=__import__("time").time() - 3600)))
    monkeypatch.setattr(listenguard, "own_join_dirs", lambda ev: [tmp_path])
    monkeypatch.setattr(midturn, "take", lambda d: None)
    ctx = midturn.handle("{}")["hookSpecificOutput"]["additionalContext"]
    assert "Stand veraltet" in ctx and "Board seit" not in ctx


def test_listen_carries_ledger_along(capsys):
    replies = iter([{"ok": True, "type": "ledger", "items": [BOARD], "thresholds": {},
                     "text": "1 offener Punkt: Board seit 11 min alt.", "ts": TS, "seq": 1},
                    {"ok": True, "type": "user", "text": "hallo", "seq": 2}])
    acks = []

    def request(sock, req, timeout=None):
        if req["cmd"] == "ack":
            acks.append(req["seq"])
            return {"ok": True}
        return next(replies)

    args = SimpleNamespace(session=None, wait=0, poll=1.0, timeout=0)
    assert cli._run_listen(args, request=request, resolve=lambda s, wait=0: "sock") == 0
    out = json.loads(capsys.readouterr().out.strip())
    assert out["type"] == "user" and out["ledger"]["text"].startswith("1 offener Punkt")
    assert acks == [1, 2]


# ---- end-to-end through the join (fake LiveKit room) ----------------------------------- #
SLUG = "abc-def-ghi-XYZ4"
WORKER = SimpleNamespace(identity="agent-AJ_worker", kind=4, name="", attributes={},
                         track_publications={})
GUEST = SimpleNamespace(identity="gast-1", kind=0, name="", attributes={"vh.name": "Gast"},
                        track_publications={})


class _LP:
    async def publish_data(self, data, reliable=True, topic="", **_):
        return None


class _Room:
    instances: ClassVar[list] = []

    def __init__(self):
        self.handlers = {}
        self.remote_participants = {p.identity: p for p in (WORKER, GUEST)}
        self.local_participant = _LP()
        _Room.instances.append(self)

    def on(self, event, fn=None):
        self.handlers[event] = fn

    async def connect(self, url, token):
        return None

    async def disconnect(self):
        return None

    def emit(self, topic, obj, sender):
        self.handlers["data_received"](SimpleNamespace(
            topic=topic, data=json.dumps(obj).encode("utf-8"), participant=sender))


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("VOICEHOOK_AGENT_HOME", str(tmp_path / "vh"))
    monkeypatch.setattr(cli.rtc, "Room", _Room)

    async def _mint(*a, **k):
        return {"url": "wss://fake", "token": "x.e30.sig"}

    monkeypatch.setattr(cli, "_mint_token", _mint)
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    _Room.instances = []


async def _call(req):
    sock = await asyncio.to_thread(vs.resolve_socket, None, 5.0)
    return await asyncio.to_thread(vs.request, sock, req, 10.0)


def test_join_end_to_end(env, capsys):
    async def run():
        join = asyncio.create_task(cli._join(f"https://voicehook.example/r/{SLUG}", "me", "Claude",
                                             True, None, model="opus-5.5", idle_timeout=0))
        st0 = await _call({"cmd": "status"})
        await _call({"cmd": "next", "timeout": 0.1})   # arms the queue
        room = _Room.instances[0]
        path = vs.session_dir(SLUG, st0["identity"]) / ledger.FILE_NAME
        res = {"st0": st0}
        me = st0["identity"]
        # from a guest: dropped and counted, no file, no event
        room.emit(ledger.TOPIC, _pkt([BOARD]), GUEST)
        res["guest_next"] = await _call({"cmd": "next", "timeout": 0.3})
        res["guest_file"] = path.exists()
        # from the worker: file + event
        room.emit(ledger.TOPIC, _pkt([BOARD, QUESTION], extra_field=7, agent=me), WORKER)
        res["ev1"] = await _call({"cmd": "next", "timeout": 2})
        res["file1"] = json.loads(path.read_text())
        # the same id set again: file updated, no new event
        room.emit(ledger.TOPIC, _pkt([QUESTION, BOARD], ts=TS + 60, agent=me), WORKER)
        res["same_next"] = await _call({"cmd": "next", "timeout": 0.3})
        # unknown version: _meta line, previous state kept
        room.emit(ledger.TOPIC, _pkt([], v=2), WORKER)
        res["v2_next"] = await _call({"cmd": "next", "timeout": 0.3})
        res["file_v2"] = json.loads(path.read_text())
        res["st1"] = await _call({"cmd": "status"})
        # agent names someone else: _meta, previous state kept
        room.emit(ledger.TOPIC, _pkt([], ts=TS + 90, agent="ravi-crown-b394"), WORKER)
        res["other_next"] = await _call({"cmd": "next", "timeout": 0.3})
        res["file_other"] = json.loads(path.read_text())
        # empty, without `agent` (taken: the routing is enough): "Alles erledigt"
        done = _pkt([], ts=TS + 120)
        del done["agent"]
        room.emit(ledger.TOPIC, done, WORKER)
        res["ev_done"] = await _call({"cmd": "next", "timeout": 2})
        res["st2"] = await _call({"cmd": "status"})
        await _call({"cmd": "leave"})
        await asyncio.wait_for(join, 5)
        return res

    r = asyncio.run(run())
    out = capsys.readouterr().out
    assert r["st0"]["ledger"] == {"items": 0, "ts": None}
    assert r["guest_next"]["type"] == "timeout" and r["guest_file"] is False
    assert "(operator.ledger) verworfen: Absender ist nicht der Worker" in out
    ev = r["ev1"]
    assert ev["type"] == "ledger" and [i["id"] for i in ev["items"]] == [BOARD["id"], QUESTION["id"]]
    assert ev["text"] == "2 offene Punkte: Board seit 11 min alt; Frage nur als Aktivität gemeldet."
    assert ev["thresholds"] == {"board_stale_s": 600} and ev["items"][1]["sub"] == "helper-x"
    assert r["file1"]["extra_field"] == 7 and r["file1"]["items"][1]["sub"] == "helper-x"
    assert r["same_next"]["type"] == "timeout"
    assert r["v2_next"]["type"] == "timeout"
    assert "(operator.ledger) unbekannte Version v=2 ignoriert" in out
    assert r["file_v2"]["ts"] == TS + 60 and len(r["file_v2"]["items"]) == 2
    assert r["st1"]["ledger"] == {"items": 2, "ts": TS + 60}
    assert r["st1"]["forged_decisions"] == 1
    assert r["other_next"]["type"] == "timeout" and r["file_other"]["ts"] == TS + 60
    assert "(operator.ledger) für andere Identität, ignoriert" in out
    assert r["ev_done"]["type"] == "ledger" and r["ev_done"]["items"] == [] \
        and r["ev_done"]["text"] == "Alles erledigt."
    assert r["st2"]["ledger"] == {"items": 0, "ts": TS + 120}
