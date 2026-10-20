"""0.21.5: right after `join` the agent is offered the multi-agent feature: one `_meta` event
`onboarding` (--json) / one `[onboarding]` line on stderr (plain) with ready commands to
(1) invite more own sessions (hook install, own join with own voice, helper-credential |
helper-import) and (2) show and share their boards, activity and open decisions. German or
English by locale, never spoken, never a credential, once per join."""
from __future__ import annotations

import asyncio
import json

import pytest
from test_join_hint import HUMAN, _run_join  # noqa: F401
from test_session import URL, _call, _FakeRoom, fake_env  # noqa: F401  (fixture)

from voicehook_agent import cli, midturn, spool

pytestmark = pytest.mark.usefixtures("fake_env")


@pytest.fixture(autouse=True)
def _onboarding_on(monkeypatch):
    monkeypatch.delenv(cli.ONBOARDING_ENV, raising=False)  # conftest turns it off for the rest


def _onboarding(out: str) -> list[dict]:
    return [o for o in (json.loads(ln) for ln in out.splitlines() if ln.strip())
            if o.get("_meta") == "onboarding"]


def test_json_join_offers_multi_agent_once_also_across_reconnect(capsys, monkeypatch):
    monkeypatch.setenv("LANG", "de_DE.UTF-8")
    monkeypatch.delenv("LC_ALL", raising=False)
    monkeypatch.delenv("LC_MESSAGES", raising=False)
    assert _run_join(True, reconnect=True) == 0
    assert len(_FakeRoom.instances) == 2
    cap = capsys.readouterr()
    hints = _onboarding(cap.out)
    assert len(hints) == 1
    h = hints[0]
    assert h["lang"] == "de" and h["topic"] == "_meta" and h["role"] == "system"
    assert "Multi-Agent" in h["text"] and "Frag den Menschen" in h["text"]
    for c in ("voicehook-agent hook install", "helper-credential", "helper-import",
              "voicehook-agent join <invite-url>", "voicehook-agent status --doing",
              "voicehook-agent decide", "voicehook-agent board"):
        assert c in h["text"], c
    assert h["commands"]["invite"] == cli.ONBOARDING_INVITE
    assert h["commands"]["share"] == cli.ONBOARDING_SHARE
    # 0.21.5 Klartext rule with a bad and a good example
    assert "Klartext-Regel" in h["text"] and "DB quick wins 620?" in h["text"]
    assert "Sales Engine: vier Datenbank-Indizes anlegen" in h["text"]
    assert h["rule"]["bad"] == "DB quick wins 620?" and "--context" in h["rule"]["text"]
    assert "[onboarding]" not in cap.err
    room = _FakeRoom.instances[0]
    assert "Multi-Agent" not in json.dumps(room.local_participant.sent)   # never spoken


def test_plain_join_english_line_on_stderr(capsys, monkeypatch):
    monkeypatch.setenv("LC_ALL", "en_US.UTF-8")
    assert _run_join(False) == 0
    cap = capsys.readouterr()
    lines = [ln for ln in cap.err.splitlines() if ln.startswith("[onboarding] ")]
    assert len(lines) == 1 and "Multi-agent: you can bring more agents" in lines[0]
    assert "Ask the human" in lines[0] and "own voice" in lines[0]
    assert "Plain-language rule" in lines[0] and "DB quick wins 620?" in lines[0]
    assert "[onboarding]" not in cap.out


def test_no_credential_in_the_hint(capsys, monkeypatch):
    monkeypatch.setenv("LANG", "de_DE.UTF-8")
    assert _run_join(True) == 0
    text = json.dumps(_onboarding(capsys.readouterr().out))
    assert "wss://fake" not in text and '"t"' not in text and "token" not in text.lower()
    assert "voicehook.example" not in text           # not even the invite URL itself


def test_off_switch_and_lang():
    assert cli.onboarding_hint({cli.ONBOARDING_ENV: "0"}) is None
    assert cli.onboarding_lang({"LANG": "de_AT.UTF-8"}) == "de"
    assert cli.onboarding_lang({"LC_ALL": "C", "LANG": "de_DE"}) == "en"
    assert cli.onboarding_lang({}) == "en"


# ----- 0.21.8: also the first next/listen event (an agent may never read the join's stdout) --
def test_json_quiet_join_still_prints_onboarding(capsys):
    """Bug report: `join --json --keep-alive --quiet` with stdout into a file. --quiet only
    suppresses the share hint, never the onboarding."""
    assert _run_join(True, quiet=True) == 0
    assert len(_onboarding(capsys.readouterr().out)) == 1


def _first_events(username=None):
    async def run():
        join = asyncio.create_task(cli._join(URL, None, "Claude", True, None, model="opus-5.5",
                                             idle_timeout=0, username=username))
        await _call({"cmd": "say", "text": "Hallo"})        # the first say arms + queues it
        _FakeRoom.instances[0].emit("transcript", {"role": "user", "text": "Hallo zurück"})
        await asyncio.sleep(0.05)
        out = [await _call({"cmd": "next", "timeout": 0}) for _ in range(3)]
        await _call({"cmd": "leave"})
        await asyncio.wait_for(join, 10)
        return out
    return asyncio.run(run())


def test_onboarding_is_the_first_next_event_once(monkeypatch):
    monkeypatch.setenv("LC_ALL", "de_DE.UTF-8")
    evs = _first_events(username="Oliver")
    assert [e["type"] for e in evs] == ["onboarding", "user", "timeout"]
    ob = evs[0]
    assert ob["text"] == ("Biete Oliver an, weitere Sessions einzuladen (helper-credential | "
                          "helper-import oder eigener Join) und deren Boards zu teilen.")
    assert "helper-import" in " ".join(ob["commands"]["invite"]) and "Multi-Agent" in ob["details"]


def test_onboarding_event_wakes_listen_and_midturn(monkeypatch):
    monkeypatch.setenv("LC_ALL", "de_DE.UTF-8")
    assert "onboarding" in spool.WAKE_TYPES and "onboarding" in cli.LISTEN_WAKE
    assert "onboarding" in midturn.TYPES
    ev = cli.onboarding_event("Oliver")
    t = midturn.text_for({"now": ev["ts"], "events": [ev]})
    assert "Biete Oliver an, weitere Sessions einzuladen" in t and "helper-credential" in t


def test_onboarding_event_off_switch(monkeypatch):
    monkeypatch.setenv(cli.ONBOARDING_ENV, "0")
    assert cli.onboarding_event("Oliver") is None
    assert cli.onboarding_event(None, env={"LANG": "de_DE"})["text"].startswith("Biete dem Menschen an")
    assert cli.onboarding_event(None, env={"LANG": "en_US"})["text"].startswith("Offer the human to invite")
