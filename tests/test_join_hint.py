"""0.14.0: join hint (E8). Once per join, after the connect, one discreet line for the
agent's own user: plain mode on stderr, --json as one `_meta` event on stdout. Never
spoken, never in persona/say. Off with --quiet or VOICEHOOK_QUIET=1."""
from __future__ import annotations

import asyncio
import json
import re

import pytest
from test_session import (
    URL,
    _call,
    _FakeRoom,
    _peer,
    fake_env,  # noqa: F401  (fixture, used via usefixtures)
)

from voicehook_agent import cli

pytestmark = pytest.mark.usefixtures("fake_env")

SIGNAL_CLOSE = 9  # transient LiveKit disconnect -> reconnect cycle
HUMAN = {"host-ut46y7": _peer("host-ut46y7", kind=0)}
HINT_LINE = ("joined via voicehook.ai · Talk with your agents · "
             "https://voicehook.ai/?utm_source=agent-join&utm_campaign=viral")


def _run_join(json_mode: bool, *, reconnect: bool = False, **kw):
    _FakeRoom.peers = HUMAN

    async def run():
        join = asyncio.create_task(cli._join(URL, None, "Claude", json_mode, None,
                                             model="opus-5.5", idle_timeout=0,
                                             keep_alive=True, **kw))
        await asyncio.sleep(0.3)
        if reconnect:
            _FakeRoom.instances[0].handlers["disconnected"](SIGNAL_CLOSE)
            await asyncio.sleep(1.8)  # backoff 1 s, then the second cycle connects
        await _call({"cmd": "leave"})
        return await asyncio.wait_for(join, 5)

    return asyncio.run(run())


def _hints(out: str) -> list[dict]:
    return [o for o in (json.loads(line) for line in out.splitlines() if line.strip())
            if o.get("_meta") == "hint"]


def _published(room) -> str:
    return json.dumps(room.local_participant.sent, ensure_ascii=False)


def test_plain_mode_one_line_on_stderr_not_stdout(capsys):
    assert _run_join(False) == 0
    cap = capsys.readouterr()
    assert cap.err.count(HINT_LINE) == 1
    assert "utm_source=agent-join" not in cap.out          # stdout stays the transcript


def test_json_mode_one_meta_event_and_every_line_parses(capsys):
    assert _run_join(True) == 0
    cap = capsys.readouterr()
    lines = [ln for ln in cap.out.splitlines() if ln.strip()]
    objs = [json.loads(ln) for ln in lines]                 # raises on any free text
    hints = [o for o in objs if o.get("_meta") == "hint"]
    assert hints == [{"role": "system", "topic": "_meta", "_meta": "hint",
                      "text": "joined via voicehook.ai · Talk with your agents",
                      "url": "https://voicehook.ai/?utm_source=agent-join&utm_campaign=viral"}]
    assert "utm_source" not in cap.err                     # no free-text copy on stderr
    # comes right after the connect event
    idx = objs.index(hints[0])
    assert objs[idx - 1]["text"].startswith("connected")


def test_once_per_join_also_across_reconnect(capsys):
    assert _run_join(True, reconnect=True) == 0
    assert len(_FakeRoom.instances) == 2                    # positive control: it did reconnect
    out = capsys.readouterr().out
    assert sum(json.loads(ln)["text"].startswith("connected")
               for ln in out.splitlines() if ln.strip()) == 2
    assert len(_hints(out)) == 1


def test_never_published_into_the_call():
    _run_join(True)
    for room in _FakeRoom.instances:
        sent = _published(room)
        assert "voicehook.ai/?utm" not in sent and "Talk with your agents" not in sent


def test_quiet_flag_turns_it_off(capsys):
    assert _run_join(True, quiet=True) == 0
    cap = capsys.readouterr()
    assert _hints(cap.out) == [] and "utm_source" not in cap.err
    assert '"text": "connected' in cap.out                  # positive control: it did join


def test_quiet_flag_plain_mode(capsys):
    assert _run_join(False, quiet=True) == 0
    cap = capsys.readouterr()
    assert "utm_source" not in cap.err + cap.out
    assert "[system] connected" in cap.out


def test_env_quiet_turns_it_off(capsys, monkeypatch):
    monkeypatch.setenv("VOICEHOOK_QUIET", "1")
    assert _run_join(True) == 0
    assert _hints(capsys.readouterr().out) == []


def test_env_quiet_zero_keeps_it(capsys, monkeypatch):
    monkeypatch.setenv("VOICEHOOK_QUIET", "0")
    assert _run_join(True) == 0
    assert len(_hints(capsys.readouterr().out)) == 1


def test_quiet_from_env_values():
    assert cli._quiet_from_env({"VOICEHOOK_QUIET": "1"})
    assert cli._quiet_from_env({"VOICEHOOK_QUIET": "true"})
    assert cli._quiet_from_env({"VOICEHOOK_QUIET": " YES "})
    assert not cli._quiet_from_env({"VOICEHOOK_QUIET": "0"})
    assert not cli._quiet_from_env({"VOICEHOOK_QUIET": ""})
    assert not cli._quiet_from_env({})


def test_cli_flag_reaches_join(monkeypatch):
    seen = {}

    async def fake_join(*a, **kw):
        seen.update(kw)
        return 0

    monkeypatch.setattr(cli, "_join", fake_join)
    for argv, want in ((["--quiet"], True), ([], False)):
        try:
            cli.main(["join", URL, "--name", "Claude", "--model", "opus-5.5", *argv])
        except SystemExit as e:
            assert e.code == 0
        assert seen["quiet"] is want


def test_text_has_claim_no_emoji_no_dash():
    line = f"{cli.SHARE_TEXT} {cli.SHARE_URL}"
    assert "Talk with your agents" in cli.SHARE_TEXT
    assert not re.search("[–—]", line)                     # no en/em dash
    assert not re.search("[\U0001F000-\U0001FAFF☀-➿]", line)  # no emoji
    assert cli.SHARE_URL.startswith("https://voicehook.ai/?")
    assert "utm_source=agent-join" in cli.SHARE_URL and "utm_campaign=viral" in cli.SHARE_URL
