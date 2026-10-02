"""0.7.0: status board (operator.status), status_request in `next`, status_stale and
latency_warning hints (Oliver 02.10.2026)."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from voicehook_agent import cli, relay


class _Local:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []

    async def publish_data(self, data, reliable=True, topic=""):
        self.published.append((topic, json.loads(data)))


def _ctl() -> cli._Control:
    ctl = cli._Control("blau-tiger", "claude-x", relay.SayTracker(), relay.EchoSuppressor(False), 0.0)
    ctl.attach(SimpleNamespace(local_participant=_Local(), remote_participants={}))
    return ctl


def _args(**kw):
    base = {"cmd": "status", "text": None, "doing": None, "open": [], "done": [], "file": None}
    base.update(kw)
    return SimpleNamespace(**base)


# ----- `status` command -------------------------------------------------------------
def test_status_without_args_still_shows_state():
    assert cli._client_request(_args()) == {"cmd": "status"}


def test_status_flags_build_the_board():
    req = cli._client_request(_args(doing="baut gerade den Fix", open=["Tests", "Doku"], done=["Analyse"]))
    assert req == {"cmd": "board", "board": {"doing": "baut gerade den Fix",
                                             "open": ["Tests", "Doku"], "done": ["Analyse"]}}


def test_status_text_and_empty_clears():
    assert cli._client_request(_args(text="testet"))["board"]["doing"] == "testet"
    assert cli._client_request(_args(text=""))["board"] == {"doing": "", "open": [], "done": []}


def test_status_board_file(tmp_path):
    f = tmp_path / "board.json"
    f.write_text(json.dumps({"doing": "a", "open": ["b"], "done": ["c"]}))
    req = cli._client_request(_args(file=str(f), open=["d"]))
    assert req["board"] == {"doing": "a", "open": ["b", "d"], "done": ["c"]}
    f.write_text("[1, 2]")
    with pytest.raises(ValueError):
        cli._client_request(_args(file=str(f)))


def test_argparse_accepts_status_flags(monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "_run_client", lambda a: seen.setdefault("req", cli._client_request(a)) and 0)
    with pytest.raises(SystemExit):
        cli.main(["status", "--doing", "baut", "--open", "x", "--open", "y", "--done", "z"])
    assert seen["req"]["board"] == {"doing": "baut", "open": ["x", "y"], "done": ["z"]}


def test_control_board_publishes_operator_status():
    ctl = _ctl()
    board = {"doing": "baut", "open": ["x"], "done": []}
    r = asyncio.run(cli._control_handler(ctl, {"cmd": "board", "board": board}))
    assert r == {"ok": True, "type": "board", "board": board}
    assert ctl.room.local_participant.published == [("operator.status", board)]
    bad = asyncio.run(cli._control_handler(ctl, {"cmd": "board", "board": "x"}))
    assert bad["ok"] is False


# ----- status_request round trip ----------------------------------------------------
def test_status_request_event_shape():
    ev = relay.status_request_event({"text": "was macht Claude gerade?"}, now=1.0)
    assert ev == {"type": "status_request", "role": "system", "text": "was macht Claude gerade?", "ts": 1.0}


def test_status_request_reaches_next_and_answer_goes_out():
    async def go():
        ctl = _ctl()
        ctl.events.arm()
        ctl.events.put_nowait(relay.status_request_event({"text": "wie weit bist du?"}))
        nxt = await cli._control_handler(ctl, {"cmd": "next", "timeout": 1})
        ans = await cli._control_handler(ctl, {"cmd": "board", "board": {"doing": "testet", "open": [], "done": []}})
        return ctl, nxt, ans
    ctl, nxt, ans = asyncio.run(go())
    assert nxt["type"] == "status_request" and nxt["text"] == "wie weit bist du?"
    assert ans["ok"] and ctl.room.local_participant.published[-1][0] == "operator.status"


# ----- hints: latency_warning + status_stale ----------------------------------------
def test_turn_clock_latency_warning_after_slow_say():
    c = relay.TurnClock(now=0.0)
    c.delivered(now=10.0)
    c.said(now=21.5)                       # 11.5 s between next and say
    h = c.hints(now=22.0)
    assert h["latency_warning"] == {"seconds": 11.5, "hint": "delegate slow work, keep main loop free"}
    assert "latency_warning" not in c.hints(now=23.0)   # reported once


def test_turn_clock_fast_say_no_warning():
    c = relay.TurnClock(now=0.0)
    c.delivered(now=10.0)
    c.said(now=12.0)
    assert c.hints(now=12.5) == {}


def test_turn_clock_unanswered_turn_warns():
    c = relay.TurnClock(now=0.0)
    c.delivered(now=10.0)
    assert c.hints(now=19.0)["latency_warning"]["seconds"] == 9.0


def test_turn_clock_status_stale_only_after_user_spoke():
    c = relay.TurnClock(now=0.0)
    assert c.hints(now=1000.0) == {}               # nobody spoke
    c.user(now=500.0)
    assert c.hints(now=1000.0) == {"status_stale": True}
    c.board(now=1001.0)
    assert c.hints(now=1002.0) == {}               # fresh board
    c.user(now=1100.0)
    assert c.hints(now=1200.0) == {}               # board younger than 5 min


def test_next_carries_latency_warning(monkeypatch):
    t = {"now": 100.0}
    monkeypatch.setattr(relay.time, "monotonic", lambda: t["now"])

    async def go():
        ctl = _ctl()
        ctl.events.arm()
        ctl.events.put_nowait({"type": "user", "role": "user", "text": "Hallo?", "ts": 1.0})
        first = await cli._control_handler(ctl, {"cmd": "next", "timeout": 0})
        t["now"] += 12.0                           # slow tool work before answering
        await cli._control_handler(ctl, {"cmd": "say", "text": "Hallo!"})
        second = await cli._control_handler(ctl, {"cmd": "next", "timeout": 0})
        return first, second
    first, second = asyncio.run(go())
    assert first["type"] == "user" and "latency_warning" not in first
    assert second["type"] == "timeout" and second["latency_warning"]["seconds"] == 12.0
