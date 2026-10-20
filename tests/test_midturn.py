"""0.21.7: new call events reach the operator inside its running turn (PostToolUse hook),
instead of only when the turn ends; the user's turn is shown once, consumed from the spool."""
from __future__ import annotations

import asyncio
import io
import json
import time

import pytest
from test_session import _call, _FakeRoom, _join, fake_env  # noqa: F401  (fixture)

from voicehook_agent import activity, cli, mcpserver, midturn, spool

SID = "sess-midturn"


def _post(sid=SID, **kw) -> str:
    ev = {"hook_event_name": "PostToolUse", "session_id": sid, "tool_name": "Bash",
          "tool_input": {"command": "make build", "description": "Build the app"},
          "tool_response": {}}
    ev.update(kw)
    return json.dumps(ev)


def _ctx(out):
    return (out or {}).get("hookSpecificOutput", {}).get("additionalContext", "")


@pytest.fixture
def owned(request, monkeypatch):
    home = request.getfixturevalue("fake_env")
    monkeypatch.delenv(midturn.ENV_OFF, raising=False)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)  # the join records this owner
    return home


# ----- pure ------------------------------------------------------------------------------
def test_text_names_speaker_age_and_waiting():
    now = 1000.0
    reply = {"now": now, "last_say_at": None, "events": [
        {"type": "user", "text": "Wie weit ist das Deploy?", "ts": now - 35, "speaker": "Oliver"}]}
    t = midturn.text_for(reply)
    assert 'Oliver hat im Call gesagt (vor 35 s): "Wie weit ist das Deploy?"' in t
    assert "Oliver wartet seit 35 s" in t and "ZUERST per `voicehook-agent say" in t
    assert midturn.RULE in t
    reply["last_say_at"] = now - 5            # answered after it was spoken: no "wartet"
    assert "wartet seit" not in midturn.text_for(reply)
    reply["last_say_at"] = None
    reply["events"][0]["ts"] = now - 5        # younger than 20 s: no "wartet"
    assert "wartet seit" not in midturn.text_for(reply)
    assert midturn.text_for({"events": []}) is None


def test_decision_lines():
    t = midturn.text_for({"now": 10.0, "events": [
        {"type": "decision", "number": 3, "question": "Deploy?", "verdict": "yes", "ts": 9.0},
        {"type": "helper_decision", "number": 4, "from": "Promo", "question": "Musik?",
         "text": "Ja, die zweite", "ts": 9.0, "delivered": True}]})
    assert "Entscheidung im Call zu #3" in t and "yes" in t
    assert "Antwort im Call an den Helfer Promo zu #4" in t and "kam auch über listen" in t


def test_spool_take_shows_once_and_consumes(tmp_path):
    async def run():
        q = spool.SpoolQueue(tmp_path, armed=True)
        q.put_nowait({"type": "user", "text": "eins", "ts": time.time()})
        q.put_nowait({"type": "human_state", "ts": time.time()})
        first = q.take(midturn.TYPES)
        second = q.take(midturn.TYPES)
        left = await q.get(0)                # listen gets only what was not shown
        q.put_nowait({"type": "user", "text": "zwei", "ts": time.time()})
        got = await q.get(0)                 # listen took it first ...
        late = q.take(midturn.TYPES)         # ... the hook still shows it, marked delivered
        return first, second, left, got, late

    first, second, left, got, late = asyncio.run(run())
    assert [e["text"] for e in first] == ["eins"] and first[0]["delivered"] is False
    assert second == [] and left["type"] == "human_state"
    assert got["text"] == "zwei" and late[0]["text"] == "zwei" and late[0]["delivered"] is True


# ----- end to end: real join, real hook -----------------------------------------------------
def test_hook_injects_user_turn_mid_turn_and_listen_skips_it(owned):
    async def run():
        join = asyncio.create_task(_join(idle_timeout=0))
        await _call({"cmd": "say", "text": "Hallo"})             # arms the spool
        room = _FakeRoom.instances[0]
        room.emit("transcript", {"role": "user", "text": "Stopp, erst die Tests!",
                                 "speaker": "Oliver"})
        await asyncio.sleep(0.05)
        out = await asyncio.to_thread(midturn.handle, _post())
        again = await asyncio.to_thread(midturn.handle, _post())
        nxt = await _call({"cmd": "next", "timeout": 0})
        foreign = await asyncio.to_thread(midturn.handle, _post(sid="another"))
        room.emit("transcript", {"role": "user", "text": "Noch was"})
        await asyncio.sleep(0.05)
        sub = await asyncio.to_thread(midturn.handle, _post(agent_id="a1"))
        await _call({"cmd": "leave"})
        await asyncio.wait_for(join, 10)
        return out, again, nxt, foreign, sub

    out, again, nxt, foreign, sub = asyncio.run(run())
    assert out["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    assert 'Oliver hat im Call gesagt (vor 0 s): "Stopp, erst die Tests!"' in _ctx(out)
    assert again is None                    # shown once
    assert nxt["type"] == "timeout"         # consumed: listen/next never bring it twice
    assert foreign is None and sub is None  # other session, subagent: never


def test_waiting_and_off_switch(owned, monkeypatch):
    async def run():
        join = asyncio.create_task(_join(idle_timeout=0))
        await _call({"cmd": "say", "text": "Hallo"})
        room = _FakeRoom.instances[0]
        room.emit("transcript", {"role": "user", "text": "Hörst du mich?", "speaker": "Oliver"})
        await asyncio.sleep(0.05)
        monkeypatch.setenv(midturn.ENV_OFF, "0")
        off = await asyncio.to_thread(midturn.handle, _post())
        monkeypatch.delenv(midturn.ENV_OFF)
        real = midturn.take

        def aged(d):  # the user turn is 42 s old and no say came since
            r = real(d)
            r["now"] = r["events"][0]["ts"] + 42
            r["last_say_at"] = r["events"][0]["ts"] - 1
            return r
        monkeypatch.setattr(midturn, "take", aged)
        out = await asyncio.to_thread(midturn.handle, _post())
        await _call({"cmd": "leave"})
        await asyncio.wait_for(join, 10)
        return off, out

    off, out = asyncio.run(run())
    assert off is None and "Oliver wartet seit 42 s" in _ctx(out)


def test_hook_main_merges_into_post_tool_use_output(monkeypatch, capsys):
    monkeypatch.setattr(activity, "post_tool_use", lambda raw: True)
    monkeypatch.setattr(activity, "relay_hook", lambda raw: None)
    monkeypatch.setattr(activity, "notify_progress", lambda raw: None)
    monkeypatch.setattr(activity, "board_hook", lambda raw: {"hookSpecificOutput": {
        "hookEventName": "PostToolUse", "additionalContext": "Board-Hinweis"}})
    monkeypatch.setattr(midturn, "handle", lambda raw: {"hookSpecificOutput": {
        "hookEventName": "PostToolUse", "additionalContext": "Oliver hat im Call gesagt"}})
    monkeypatch.setattr("sys.stdin", io.StringIO(_post()))
    assert activity.main(["post-tool-use"]) == 0
    ctx = _ctx(json.loads(capsys.readouterr().out))
    assert ctx.startswith("Oliver hat im Call gesagt") and "Board-Hinweis" in ctx


def test_rule_in_help_onboarding_and_mcp(capsys):
    assert midturn.RULE in cli.onboarding_hint({"LANG": "de_DE"})["text"]
    assert midturn.RULE_EN in cli.onboarding_hint({"LANG": "en_US"})["text"]
    assert "answer every utterance first" in mcpserver.INSTRUCTIONS
    with pytest.raises(SystemExit):
        cli.main(["listen", "--help"])
    assert "Arbeit über 2 Tool-Calls" in " ".join(capsys.readouterr().out.split())
