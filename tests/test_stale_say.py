"""0.21.9 (Oliver 21.10.2026, rule VH-R1): the server drops our queued, not yet spoken says
older than VOICEHOOK_SAY_STALE_S (default 60 s) and sends them back as operator.revise
{stale: true, items: [{seq, text}], hint}. The CLI shows a clear line, wakes next/listen,
and the mid-turn hook tells the agent: outdated, never repeat, ONE short summary if relevant.
Stdout of the join carries metadata only (count, age), never the texts."""
from __future__ import annotations

import asyncio
import json

import pytest
from test_midturn import _ctx, _post, owned  # noqa: F401  (fixture)
from test_session import _call, _FakeRoom, _join, fake_env  # noqa: F401  (fixture)

from voicehook_agent import midturn, relay, spool

STALE = {"stale": True, "owner": None, "threshold_s": 60, "max_age_s": 74,
         "items": [{"seq": 3, "text": "Der Build läuft noch", "age_s": 74},
                   {"seq": 5, "text": "Tests sind grün", "age_s": 61}]}


def _stale(**kw):
    return {k: v for k, v in {**STALE, **kw}.items() if v is not None}


# ----- pure ------------------------------------------------------------------------------
def test_revise_event_carries_stale_items_and_hint():
    ev = relay.revise_event(_stale(), now=1.0)
    assert ev["type"] == "revise" and ev["stale"] is True
    assert ev["items"] == [{"seq": 3, "text": "Der Build läuft noch", "age_s": 74},
                           {"seq": 5, "text": "Tests sind grün", "age_s": 61}]
    assert ev["hint"] == relay.STALE_HINT and "nicht wiederholen" in ev["hint"]
    assert "EINE kurze aktuelle Zusammenfassung" in ev["hint"]
    assert ev["text"].startswith("VERALTET: 2 eingereihte Aussage(n) älter als 60 s")
    assert '[3] "Der Build läuft noch"' in ev["text"] and ev["text"].endswith(relay.STALE_HINT)


def test_stale_english_and_server_hint_and_plain_strings():
    ev = relay.revise_event(_stale(lang="en", items=["old one"]), now=1.0)
    assert ev["hint"] == relay.STALE_HINT_EN and ev["text"].startswith("STALE: 1 queued")
    assert ev["items"] == [{"seq": None, "text": "old one"}]
    ev = relay.revise_event(_stale(hint="Server sagt: weg damit"), now=1.0)
    assert ev["hint"] == "Server sagt: weg damit"


def test_normal_revise_unchanged():
    """Positive control: a revise without stale keeps its old shape (no stale fields)."""
    ev = relay.revise_event({"text": "REVISE: ...", "unspoken": ["a"], "new": "b"}, now=1.0)
    assert ev == {"type": "revise", "role": "system", "text": "REVISE: ...", "ts": 1.0,
                  "unspoken": ["a"], "new": "b"}
    assert not relay.is_stale({"stale": "yes"}) and not relay.is_stale(None)


def test_stale_meta_has_no_texts():
    meta = relay.stale_meta(relay.revise_event(_stale(), now=1.0))
    assert meta == "stale-dropped n=2 max_age=74s"
    assert "Build" not in meta and "grün" not in meta


def test_midturn_shows_stale_but_not_normal_revise(tmp_path):
    import time

    async def run():
        q = spool.SpoolQueue(tmp_path, armed=True)
        q.put_nowait(relay.revise_event({"text": "R", "unspoken": ["x"], "new": "y"}))
        q.put_nowait(relay.revise_event(_stale()))
        shown = q.take(midturn.TYPES)
        left = await q.get(0)   # the normal revise stays for listen
        return shown, left

    shown, left = asyncio.run(run())
    assert [e.get("stale") for e in shown] == [True]
    assert left["type"] == "revise" and "stale" not in left
    t = midturn.text_for({"now": time.time(), "events": shown})
    assert 'Veraltet, nicht gesprochen (2 Aussage(n)): "Der Build läuft noch" "Tests sind grün"' in t
    assert "Nicht wiederholen" in t and "EINE kurze aktuelle Zusammenfassung" in t
    assert "Bestätige kurz" not in t   # nothing to confirm, only the hint


# ----- end to end: real join -------------------------------------------------------------
@pytest.mark.usefixtures("fake_env")
def test_join_queues_stale_wakes_next_and_logs_metadata_only(capsys):
    async def run():
        join = asyncio.create_task(_join(idle_timeout=0))
        await _call({"cmd": "say", "text": "Hallo"})
        room = _FakeRoom.instances[0]
        pending = asyncio.create_task(_call({"cmd": "next", "timeout": 5}))
        await asyncio.sleep(0.2)
        room.emit("operator.revise", _stale())
        got = await pending
        await _call({"cmd": "leave"})
        await asyncio.wait_for(join, 10)
        return got

    got = asyncio.run(run())
    assert got["type"] == "revise" and got["stale"] is True
    assert [i["text"] for i in got["items"]] == ["Der Build läuft noch", "Tests sind grün"]
    assert got["hint"] == relay.STALE_HINT and "status_hint" in got   # board hint kept apart
    out = capsys.readouterr().out
    assert "stale-dropped n=2 max_age=74s" in out
    assert "Der Build läuft noch" not in out and "Tests sind grün" not in out


@pytest.mark.usefixtures("fake_env")
def test_join_ignores_foreign_stale():
    async def run():
        join = asyncio.create_task(_join(idle_timeout=0))
        await _call({"cmd": "say", "text": "Hallo"})
        room = _FakeRoom.instances[0]
        room.emit("operator.revise", _stale(owner="another-operator"))
        await asyncio.sleep(0.1)
        got = await _call({"cmd": "next", "timeout": 0})
        await _call({"cmd": "leave"})
        await asyncio.wait_for(join, 10)
        return got

    assert asyncio.run(run())["type"] == "timeout"


@pytest.mark.usefixtures("owned")
def test_hook_shows_stale_mid_turn():
    async def run():
        join = asyncio.create_task(_join(idle_timeout=0))
        await _call({"cmd": "say", "text": "Hallo"})
        room = _FakeRoom.instances[0]
        room.emit("operator.revise", _stale())
        await asyncio.sleep(0.05)
        out = await asyncio.to_thread(midturn.handle, _post())
        nxt = await _call({"cmd": "next", "timeout": 0})
        await _call({"cmd": "leave"})
        await asyncio.wait_for(join, 10)
        return out, nxt

    out, nxt = asyncio.run(run())
    t = _ctx(out)
    assert "Veraltet, nicht gesprochen (2 Aussage(n))" in t and "Nicht wiederholen" in t
    assert nxt["type"] == "timeout"   # shown once, listen never brings it twice


def test_listen_wakes_on_stale():
    from voicehook_agent import cli
    assert "revise" in cli.LISTEN_WAKE
    assert json.loads(json.dumps(relay.revise_event(_stale())))["stale"] is True


def test_say_status_dropped_stale_note():
    st = relay.SayStatus()
    st.sent(4, "alt", now=0.0)
    st.sent(5, "kein audio", now=0.0)
    a = st.update({"seq": 4, "state": "dropped", "reason": "stale"}, now=1.0)
    b = st.update({"seq": 5, "state": "dropped", "reason": "no_audio"}, now=1.0)
    assert a == {"seq": 4, "state": "dropped", "reason": "stale", "note": relay.SAY_NOTE_STALE}
    assert b["note"] == relay.SAY_NOTES["dropped"]   # positive control: other drops unchanged
