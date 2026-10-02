"""0.9.0: `next` nudges the agent to keep its status board fresh (status_due + hint),
status_request first in line, progress `say` without a fresh board (Oliver 02.10.2026)."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from voicehook_agent import cli, relay

BOARD = {"doing": "baut den Fix, ETA 5 min", "open": ["Tests"], "done": ["Analyse"]}
DONE_ONLY = {"doing": "", "open": [], "done": ["Fix live"]}


class _Local:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []

    async def publish_data(self, data, reliable=True, topic=""):
        self.published.append((topic, json.loads(data)))


def _ctl() -> cli._Control:
    ctl = cli._Control("blau-tiger", "claude-x", relay.SayTracker(), relay.EchoSuppressor(False), 0.0)
    ctl.attach(SimpleNamespace(local_participant=_Local(), remote_participants={}))
    return ctl


# ----- TurnClock.status_due ---------------------------------------------------------
def test_empty_board_is_due_with_exact_command():
    c = relay.TurnClock(now=0.0)
    h = c.hints(now=1.0)
    assert h["status_due"] is True and h["status_reason"] == "empty"
    assert "voicehook-agent status --doing" in h["hint"] and "--open" in h["hint"] and "--done" in h["hint"]
    c.board(now=2.0, board={"doing": "", "open": [], "done": []})   # cleared = still empty
    assert c.hints(now=3.0)["status_reason"] == "empty"


def test_fresh_board_not_due_then_due_after_n_seconds():
    c = relay.TurnClock(now=0.0, due_s=45.0)
    c.board(now=10.0, board=BOARD)
    assert "status_due" not in c.hints(now=54.0)        # 44 s old
    h = c.hints(now=56.0)                               # 46 s old, doing set
    assert h["status_due"] is True and h["status_reason"] == "stale" and h["board_age_s"] == 46.0
    c.board(now=57.0, board=BOARD)
    assert "status_due" not in c.hints(now=58.0)


def test_finished_board_nags_only_after_user_spoke():
    c = relay.TurnClock(now=0.0, due_s=45.0)
    c.board(now=0.0, board=DONE_ONLY)
    assert "status_due" not in c.hints(now=500.0)       # idle, finished: no noise
    c.user(now=501.0)
    assert c.hints(now=502.0)["status_reason"] == "stale"


def test_due_seconds_flag_env_and_off(monkeypatch):
    monkeypatch.delenv(relay.STATUS_DUE_ENV, raising=False)
    assert relay.status_due_seconds() == 45.0
    monkeypatch.setenv(relay.STATUS_DUE_ENV, "20")
    assert relay.status_due_seconds() == 20.0
    assert relay.status_due_seconds(90) == 90.0         # flag wins
    c = relay.TurnClock(now=0.0, due_s=0)
    c.board(now=0.0, board=BOARD)
    assert "status_due" not in c.hints(now=10_000.0)    # 0 = age rule off


def test_status_request_due_until_next_board():
    c = relay.TurnClock(now=0.0)
    c.board(now=0.0, board=BOARD)
    c.requested(now=5.0)
    h = c.hints(now=6.0)
    assert h["status_reason"] == "status_request"
    assert h["hint"].startswith("Nutzer fragt nach Stand: Board jetzt aktualisieren")
    c.board(now=7.0, board=BOARD)
    assert "status_due" not in c.hints(now=8.0)


# ----- `next` end to end ------------------------------------------------------------
def test_status_request_comes_first_in_next():
    async def go():
        ctl = _ctl()
        ctl.events.arm()
        ctl.events.put_nowait({"type": "user", "role": "user", "text": "eins", "ts": 1.0})
        ctl.events.put_nowait({"type": "user", "role": "user", "text": "zwei", "ts": 2.0})
        ctl.events.put_front_nowait(relay.status_request_event({"text": "wie weit bist du?"}))
        ctl.events.put_front_nowait(relay.status_request_event({"text": "und, Stand?"}))
        ctl.clock.requested()
        first = await cli._control_handler(ctl, {"cmd": "next", "timeout": 0})
        await cli._control_handler(ctl, {"cmd": "board", "board": BOARD})
        second = await cli._control_handler(ctl, {"cmd": "next", "timeout": 0})
        return first, second
    first, second = asyncio.run(go())
    assert first["type"] == "status_request" and first["text"] == "und, Stand?"   # newest, deduped
    assert first["hint"].startswith("Nutzer fragt nach Stand: Board jetzt aktualisieren")
    assert first["status_due"] is True and first["pending"] == 2
    assert second["type"] == "user" and second["text"] == "eins" and "status_due" not in second


def test_next_timeout_carries_status_due_when_board_old(monkeypatch):
    t = {"now": 100.0}
    monkeypatch.setattr(relay.time, "monotonic", lambda: t["now"])

    async def go():
        ctl = _ctl()
        ctl.clock.due_s = 45.0
        await cli._control_handler(ctl, {"cmd": "board", "board": BOARD})
        fresh = await cli._control_handler(ctl, {"cmd": "next", "timeout": 0})
        t["now"] += 60.0
        old = await cli._control_handler(ctl, {"cmd": "next", "timeout": 0})
        return fresh, old
    fresh, old = asyncio.run(go())
    assert fresh["type"] == "timeout" and "status_due" not in fresh
    assert old["status_due"] is True and old["status_reason"] == "stale" and old["board_age_s"] == 60.0
    assert "voicehook-agent status --doing" in old["hint"]


def test_join_flag_parses(monkeypatch):
    seen = {}

    async def fake_join(*a, **kw):
        seen.update(kw)
        return 0
    monkeypatch.setattr(cli, "_join", fake_join)
    try:
        cli.main(["join", "https://voicehook.ai/r/blau-tiger-hase-AB12", "--name", "Claude",
                  "--model", "opus", "--status-due", "30"])
    except SystemExit:
        pass
    assert seen["status_due"] == 30.0


# ----- `say` progress without fresh board ------------------------------------------
def test_say_progress_without_board_gets_hint():
    async def go():
        ctl = _ctl()
        a = await cli._control_handler(ctl, {"cmd": "say", "text": "Moment, ich schaue nach."})
        b = await cli._control_handler(ctl, {"cmd": "say", "text": "Der Fix ist fertig und live."})
        await cli._control_handler(ctl, {"cmd": "board", "board": DONE_ONLY})
        c = await cli._control_handler(ctl, {"cmd": "say", "text": "Ist jetzt live."})
        d = await cli._control_handler(ctl, {"cmd": "say", "text": "Deploye gleich nochmal."})
        return a, b, c, d
    a, b, c, d = asyncio.run(go())
    assert a["ok"] and "hint" not in a                         # no progress word
    assert b["ok"] and b["status_reason"] == "say_progress" and "voicehook-agent status" in b["hint"]
    assert c["ok"] and "hint" not in c                         # board pushed since last say
    assert d["ok"] and d["status_reason"] == "say_progress"    # again no board since last say


def test_say_progress_with_old_board_gets_hint():
    c = relay.TurnClock(now=0.0, due_s=45.0)
    c.board(now=0.0, board=BOARD)
    assert c.said(now=10.0, text="ist live") == {}
    assert c.said(now=100.0, text="deploye jetzt")["status_reason"] == "say_progress"


def test_mentions_progress():
    for t in ("fertig", "Ist live", "ich deploye", "deployed", "gemerged", "Done!"):
        assert relay.mentions_progress(t), t
    for t in ("Hallo", "Moment bitte", "Lieferung", "fertigen"):
        assert not relay.mentions_progress(t), t
