"""0.9.0: operator.say_status receipts (v4 #141) in `next` / `says`, stuck-say hint,
and --username forwarded to the server as vh.user (v4 #139)."""
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


# ----- SayStatus ----------------------------------------------------------------------
def test_say_status_changes_only_own_seqs():
    s = relay.SayStatus()
    s.sent(1, "Hallo", now=0.0)
    assert s.update({"seq": 1, "state": "queued", "spoken_chars": 0}, now=1.0) == {"seq": 1, "state": "queued"}
    assert s.update({"seq": 99, "state": "spoken"}) is None            # not ours
    assert s.update({"seq": "vh-3", "state": "spoken"}) is None        # foreign/old client id
    assert s.update({"seq": 1, "state": "bogus"}) is None
    s.update({"seq": "1", "state": "interrupted", "spoken_chars": 4}, now=2.0)
    assert s.take(now=2.0) == {"say_status": [{"seq": 1, "state": "queued"},
                                              {"seq": 1, "state": "interrupted", "spoken_chars": 4}]}
    assert s.take(now=2.0) == {}


def test_stuck_say_hint_after_20s_once():
    s = relay.SayStatus()
    s.sent(3, "x", now=0.0)
    s.update({"seq": 3, "state": "queued"}, now=1.0)
    assert "say_hint" not in s.take(now=20.0)
    h = s.take(now=22.0)["say_hint"]
    assert h.startswith("say seq 3 haengt seit 21 s in queued") and "--mode overwrite" in h
    assert s.take(now=40.0) == {}                                      # once per state
    s.update({"seq": 3, "state": "requeued", "spoken_chars": 5}, now=41.0)
    s.take(now=41.0)
    assert "requeued" in s.take(now=62.0)["say_hint"]
    s.update({"seq": 3, "state": "spoken"}, now=63.0)
    assert "say_hint" not in s.take(now=200.0)


def test_says_table_keeps_last_state():
    s = relay.SayStatus()
    s.sent(1, "eins", now=0.0)
    s.sent(2, "zwei", now=1.0)
    s.update({"seq": 1, "state": "spoken", "spoken_chars": 4}, now=2.0)
    assert s.table(now=3.0) == [
        {"seq": 1, "state": "spoken", "spoken_chars": 4, "age_s": 3.0, "text": "eins"},
        {"seq": 2, "state": "sent", "spoken_chars": 0, "age_s": 2.0, "text": "zwei"}]


# ----- through the control socket ------------------------------------------------------
def test_say_next_says_round_trip():
    async def go():
        ctl = _ctl()
        r = await cli._control_handler(ctl, {"cmd": "say", "text": "Hallo Oliver"})
        ctl.say_status.update({"seq": r["seq"], "state": "queued", "spoken_chars": 0})
        ctl.say_status.update({"seq": r["seq"], "state": "spoken", "spoken_chars": 12})
        nxt = await cli._control_handler(ctl, {"cmd": "next", "timeout": 0})
        nxt2 = await cli._control_handler(ctl, {"cmd": "next", "timeout": 0})
        says = await cli._control_handler(ctl, {"cmd": "says"})
        return r, nxt, nxt2, says
    r, nxt, nxt2, says = asyncio.run(go())
    assert r["ok"] and r["seq"] == 1
    assert nxt["say_status"] == [{"seq": 1, "state": "queued"}, {"seq": 1, "state": "spoken"}]
    assert "say_status" not in nxt2
    assert says["type"] == "says" and says["says"][0]["state"] == "spoken"
    assert says["says"][0]["spoken_chars"] == 12


def test_on_data_routes_say_status():
    import inspect
    src = inspect.getsource(cli)
    assert 'topic == "operator.say_status"' in src and "ctl.say_status.update(payload)" in src


def test_says_cli_command(monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "_run_client", lambda a: seen.setdefault("req", cli._client_request(a)) and 0)
    with pytest.raises(SystemExit):
        cli.main(["says"])
    assert seen["req"] == {"cmd": "says"}


# ----- --username -> server -------------------------------------------------------------
def test_mint_token_sends_username(monkeypatch):
    sent = {}

    class _Resp:
        status_code = 200
        def json(self): return {"token": "t", "url": "wss://x"}

    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, params=None):
            sent["params"] = params
            return _Resp()

    monkeypatch.setattr(cli.httpx, "AsyncClient", _Client)
    asyncio.run(cli._mint_token("https://voicehook.ai", "r", "i", name="Claude", model="m",
                                username="Oliver"))
    assert sent["params"]["username"] == "Oliver"
    asyncio.run(cli._mint_token("https://voicehook.ai", "r", "i", name="Claude", model="m"))
    assert "username" not in sent["params"]


def test_join_threads_username_to_connect(monkeypatch):
    seen = {}

    async def fake_connect(*a, **kw):
        seen.update(kw)
        return 0, None
    monkeypatch.setattr(cli, "_connect_and_listen", fake_connect)
    rc = asyncio.run(cli._join("https://voicehook.example/r/blau-tiger-wald-AB12", None, "Claude",
                               True, model="opus-5.5", username="Oliver", no_greet=True,
                               idle_timeout=0, control=False, keep_alive=False))
    assert rc == 0 and seen["username"] == "Oliver"


# ----- 0.12.0: covered (v4 #162, live mode) ----------------------------------------------
def test_say_status_covered_in_next_and_says():
    s = relay.SayStatus()
    s.sent(7, "Der Deploy ist fertig", now=0.0)
    s.update({"seq": 7, "state": "queued"}, now=0.5)
    change = s.update({"seq": 7, "state": "covered", "spoken_chars": 0}, now=3.0)
    note = "Info steckt schon in Deltas Antwort, nicht nochmal senden"
    assert change == {"seq": 7, "state": "covered", "note": note}
    assert s.take(now=3.0) == {"say_status": [{"seq": 7, "state": "queued"},
                                              {"seq": 7, "state": "covered", "note": note}]}
    row = s.table(now=4.0)[0]
    assert row["state"] == "covered" and row["note"] == note
    assert s.stuck(now=60.0) is None                       # final, never "stuck"


def test_say_status_note_only_for_covered():
    s = relay.SayStatus()
    s.sent(1, "Hallo", now=0.0)
    assert "note" not in s.update({"seq": 1, "state": "spoken"}, now=1.0)
    assert "note" not in s.table(now=1.0)[0]


def test_says_help_lists_covered():
    assert '"covered"' in cli.__doc__


def test_covered_round_trip_next_and_says():
    async def go():
        ctl = _ctl()
        r = await cli._control_handler(ctl, {"cmd": "say", "text": "Der Deploy ist fertig"})
        ctl.say_status.update({"seq": r["seq"], "state": "queued", "spoken_chars": 0})
        ctl.say_status.update({"seq": r["seq"], "state": "covered", "spoken_chars": 0})
        nxt = await cli._control_handler(ctl, {"cmd": "next", "timeout": 0})
        says = await cli._control_handler(ctl, {"cmd": "says"})
        return nxt, says
    nxt, says = asyncio.run(go())
    assert nxt["say_status"][-1]["state"] == "covered"
    assert "Deltas Antwort" in nxt["say_status"][-1]["note"]
    assert says["says"][0]["state"] == "covered" and "Deltas Antwort" in says["says"][0]["note"]
