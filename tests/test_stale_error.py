"""0.13.0: `next` carries `stale_error` on EVERY output while the operator is active and
its status board or activity log is older than 60 s (Oliver 08.10.2026). No rate limit;
status_due / activity_due stay as they are."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from voicehook_agent import activity, cli, relay

BOARD = {"doing": "baut den Fix, ETA 5 min", "open": ["Tests"], "done": []}
DONE_ONLY = {"doing": "", "open": [], "done": ["Fix live"]}


class _Local:
    async def publish_data(self, data, reliable=True, topic=""):
        pass


@pytest.fixture
def t(monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr(relay.time, "monotonic", lambda: clock["now"])
    monkeypatch.delenv(relay.STALE_ERROR_ENV, raising=False)
    return clock


def _ctl(t, log=None) -> cli._Control:
    ctl = cli._Control("blau-tiger", "claude-x", relay.SayTracker(), relay.EchoSuppressor(False), 0.0)
    ctl.attach(SimpleNamespace(local_participant=_Local(), remote_participants={}))
    ctl.activity_clock = lambda: t["now"]
    ctl.activity_path = log
    return ctl


async def _next(ctl):
    return await cli._control_handler(ctl, {"cmd": "next", "timeout": 0})


# ----- text format ------------------------------------------------------------------
@pytest.mark.parametrize("sec,text", [
    (0, "0 s"), (45, "45 s"), (59.9, "59 s"), (60, "1 Min"), (95, "1 Min 35 s"),
    (179, "2 Min 59 s"), (187, "3 Min"), (3599, "59 Min"), (3600, "1 Std"), (3900, "1 Std 5 Min")])
def test_human_age(sec, text):
    assert relay.human_age(sec) == text


def test_message_both_parts_german_with_commands():
    e = relay.stale_error_payload(187.0, 95.0)
    assert e["status_age_s"] == 187 and e["activity_age_s"] == 95
    assert e["message"] == (
        "FEHLER: Statusboard seit 3 Min nicht aktualisiert, Aktivitätslog seit 1 Min 35 s. "
        'Jetzt: voicehook-agent status --doing "…" und voicehook-agent activity "…"')


def test_message_single_parts():
    s = relay.stale_error_payload(70.0, None)
    assert set(s) == {"status_age_s", "message"}
    assert s["message"] == ('FEHLER: Statusboard seit 1 Min 10 s nicht aktualisiert. '
                            'Jetzt: voicehook-agent status --doing "…"')
    a = relay.stale_error_payload(None, 61.0)
    assert set(a) == {"activity_age_s", "message"}
    assert a["message"] == ('FEHLER: Aktivitätslog seit 1 Min 1 s nicht aktualisiert. '
                            'Jetzt: voicehook-agent activity "…"')
    assert relay.stale_error_payload(None, None) == {}


def test_activity_hint_is_german():
    assert activity.ACTIVITY_HINT.startswith("Aktivitätslog")
    assert 'voicehook-agent activity "<3-8 Wörter>"' in activity.ACTIVITY_HINT


# ----- `next` end to end ------------------------------------------------------------
def test_every_next_while_stale_no_rate_limit_and_age_rises(t):
    async def go():
        ctl = _ctl(t)
        await cli._control_handler(ctl, {"cmd": "board", "board": BOARD})
        fresh = await _next(ctl)
        out = []
        for _ in range(3):
            t["now"] += 61.0
            out.append(await _next(ctl))
        out.append(await _next(ctl))          # same second: still there, no rate limit
        return fresh, out
    fresh, out = asyncio.run(go())
    assert "stale_error" not in fresh
    ages = [o["stale_error"]["status_age_s"] for o in out]
    assert ages == [61, 122, 183, 183]
    assert [o["stale_error"]["activity_age_s"] for o in out] == [61, 122, 183, 183]
    assert all(o["stale_error"]["message"].startswith("FEHLER: Statusboard seit ") for o in out)
    assert out[2]["stale_error"]["message"].startswith("FEHLER: Statusboard seit 3 Min nicht")
    # compatible: the old keys are still there
    assert out[0]["status_due"] is True and out[0]["activity_due"] is True


def test_disappears_after_updates(t, tmp_path):
    log = tmp_path / activity.ACTIVITY_NAME

    async def go():
        ctl = _ctl(t, log)
        await cli._control_handler(ctl, {"cmd": "board", "board": BOARD})
        t["now"] += 90.0
        both = await _next(ctl)
        await cli._control_handler(ctl, {"cmd": "board", "board": BOARD})
        only_act = await _next(ctl)
        await cli._control_handler(ctl, {"cmd": "activity", "text": "Running migrations"})
        clean = await _next(ctl)
        return both, only_act, clean
    both, only_act, clean = asyncio.run(go())
    assert set(both["stale_error"]) == {"status_age_s", "activity_age_s", "message"}
    assert set(only_act["stale_error"]) == {"activity_age_s", "message"}
    assert "stale_error" not in clean


def test_inactive_operator_is_silent(t):
    async def go():
        ctl = _ctl(t)
        await cli._control_handler(ctl, {"cmd": "board", "board": DONE_ONLY})
        t["now"] += 10_000.0                    # finished board, nothing said for long
        return await _next(ctl)
    assert "stale_error" not in asyncio.run(go())


def test_say_in_last_5_min_counts_as_active(t):
    async def go():
        ctl = _ctl(t)
        await cli._control_handler(ctl, {"cmd": "board", "board": DONE_ONLY})
        t["now"] += 1000.0
        await cli._control_handler(ctl, {"cmd": "say", "text": "Moment."})
        t["now"] += 10.0
        active = await _next(ctl)
        t["now"] += relay.WORK_WINDOW_S
        idle = await _next(ctl)
        return active, idle
    active, idle = asyncio.run(go())
    assert active["stale_error"]["status_age_s"] == 1010
    assert "stale_error" not in idle


def test_no_board_since_join_counts_from_join(t):
    async def go():
        ctl = _ctl(t)
        t["now"] += 30.0
        early = await _next(ctl)
        t["now"] += 40.0
        late = await _next(ctl)
        return early, late
    early, late = asyncio.run(go())
    assert "stale_error" not in early
    assert late["stale_error"]["status_age_s"] == 70 and late["stale_error"]["activity_age_s"] == 70


def test_hook_lines_make_only_status_part_relevant(t, tmp_path):
    log = tmp_path / activity.ACTIVITY_NAME

    async def go():
        ctl = _ctl(t, log)
        await cli._control_handler(ctl, {"cmd": "board", "board": BOARD})
        activity.append_line(tmp_path, "10:00:00 Bash: Wait for next turn")
        await _next(ctl)                        # observes the hook line
        t["now"] += 120.0
        return await _next(ctl)
    out = asyncio.run(go())
    assert set(out["stale_error"]) == {"status_age_s", "message"}


def test_threshold_env_flag_and_zero(t, monkeypatch):
    assert relay.stale_error_seconds() == 60.0
    monkeypatch.setenv(relay.STALE_ERROR_ENV, "20")
    assert relay.stale_error_seconds() == 20.0
    assert relay.stale_error_seconds(90) == 90.0

    async def go(stale_s):
        ctl = _ctl(t)
        ctl.stale_s = stale_s
        await cli._control_handler(ctl, {"cmd": "board", "board": BOARD})
        t["now"] += 30.0
        return await _next(ctl)
    assert asyncio.run(go(20.0))["stale_error"]["status_age_s"] == 30
    assert "stale_error" not in asyncio.run(go(0.0))
    monkeypatch.setenv(relay.STALE_ERROR_ENV, "0")
    ctl = _ctl(t)
    assert ctl.stale_s == 0.0


def test_join_flag_parses(monkeypatch):
    seen = {}

    async def fake_join(*a, **kw):
        seen.update(kw)
        return 0
    monkeypatch.setattr(cli, "_join", fake_join)
    with pytest.raises(SystemExit):
        cli.main(["join", "https://voicehook.ai/r/blau-tiger-hase-AB12", "--name", "Claude",
                  "--model", "opus", "--stale-error", "45"])
    assert seen["stale_error"] == 45.0


def test_join_help_mentions_stale_error(capsys):
    with pytest.raises(SystemExit):
        cli.main(["join", "--help"])
    out = capsys.readouterr().out
    assert "--stale-error" in out and "VOICEHOOK_STALE_ERROR_S" in out and "stale_error" in out


# ----- interactive (plain) output -----------------------------------------------------
def test_plain_line(t, capsys):
    ctl = _ctl(t)
    t["now"] += 70.0
    cli._print_stale(ctl, json_mode=False)
    out = capsys.readouterr().out
    assert out.startswith("!! stale: FEHLER: Statusboard seit 1 Min 10 s nicht aktualisiert")
    cli._print_stale(ctl, json_mode=True)       # json stream: `next` carries it instead
    assert capsys.readouterr().out == ""
