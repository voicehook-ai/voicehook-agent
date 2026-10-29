"""0.21.4: answers to a helper session's own `decide` reach it automatically.

Live (20.10.2026): a helper asked with `vh-helper decide`, Oliver answered on the board, the
operator saw `helper_decision ... verdict=yes`, but the helper session, idle after its turn,
never got it unless someone pasted it in. The sync hooks only polled while the session
worked; now the async `stop-wait` hook long-polls the open questions after Stop and wakes the
idle session (asyncRewake) with the answer. Remote and local relay."""
from __future__ import annotations

import json
import os
import time

import pytest

from voicehook_agent import activity, asks, board, relayops
from voicehook_agent import multisession as ms

URL = "https://voicehook.ai/api/activity/relay"
TOKEN = "fake.relay.test.only"  # fake, low entropy (gitleaks)
SID = "helper-1"


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    for k in ("CLAUDE_CODE_SESSION_ID", ms.NO_RELAY_ENV, "VOICEHOOK_SESSION", asks.ENV_OFF,
              board.ENV_OFF, board.STATE_ENV_OFF):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("VOICEHOOK_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv(ms.LABEL_ENV, "Hooks")
    monkeypatch.setattr(board, "_spawn_post", lambda job: None)
    monkeypatch.setattr(board, "_spawn_sleeper", lambda skey: None)
    monkeypatch.setattr(board, "fetch_answers", lambda rel, frm: [])
    monkeypatch.setattr(ms, "_spawn_post", lambda *a: None)


@pytest.fixture
def relay():
    ms.write_relay(URL, TOKEN, "room-a", 3600, owner_pid=os.getpid(),
                   owner_session=ms.session_hash("JOINER"))
    return ms.load_relay()


def _ev(name, **kw):
    d = {"session_id": SID, "hook_event_name": name, "cwd": "/w/proj", "transcript_path": ""}
    d.update(kw)
    return json.dumps(d)


def _bash(cmd, stdout=""):
    return _ev("PostToolUse", tool_name="Bash", tool_input={"command": cmd, "description": "x"},
               tool_response={"stdout": stdout, "stderr": ""})


def _ans(dec_id="d1", verdict="yes", number=4):
    return {"type": "decision", "id": dec_id, "verdict": verdict, "number": number, "by": "Oliver",
            "question": "PR mergen?"}


SK = ms.session_hash(SID)


def _clock():
    t = [0.0]

    def tick():
        t[0] += 5.0
        return t[0]
    return tick


def test_decide_is_open_until_answered(relay):
    board.handle(_bash('vh-helper decide "Hooks" "PR mergen?"'), now=1000.0)
    assert board.open_names(SK, 1001.0) == ["Hooks"]
    # the sync Stop poll finds nothing yet: no block
    assert board.handle(_ev("Stop", last_assistant_message="Fertig."), now=1002.0) is None
    assert board.open_names(SK, 1003.0) == ["Hooks"]


def test_stop_wait_wakes_the_idle_session_with_a_later_answer(relay):
    board.handle(_bash('vh-helper decide "Hooks" "PR mergen?"'), now=1000.0)
    polls: list[str] = []

    def nxt(rel, frm, timeout, match_ids=None):
        polls.append(frm)
        return [_ans()] if len(polls) == 3 else []   # Oliver answers a minute later
    rc, text = asks.stop_ask(_ev("Stop", last_assistant_message="Fertig, warte auf Oliver."),
                             now=1001.0, next_events=nxt, clock=_clock())
    assert rc == asks.WAKE_EXIT
    assert text.startswith("voicehook-Board #4 (deine Frage)") and "Oliver hat im Call-Board geantwortet: Ja." in text
    assert polls == ["Hooks"] * 3
    assert board.open_names(SK, 1100.0) == []            # closed: no further waiting
    # delivered once: the next hook poll does not repeat it
    assert board.take_new(SK, [("Hooks", _ans())]) == []


def test_stop_wait_without_open_question_returns_at_once(relay):
    calls = []
    rc = asks.stop_ask(_ev("Stop", last_assistant_message="Fertig."), now=1000.0,
                       next_events=lambda *a, **k: calls.append(1) or [], clock=_clock())
    assert rc == (0, "") and calls == []


def test_session_goes_on_answer_lands_in_inbox_and_comes_as_context(relay, monkeypatch):
    board.handle(_bash('vh-helper decide "Hooks" "PR mergen?"'), now=1000.0)

    def nxt(rel, frm, timeout, match_ids=None):
        board.handle(_ev("UserPromptSubmit", prompt="weiter"), now=1005.0)  # Oliver types
        return [_ans()]
    assert asks.stop_ask(_ev("Stop", last_assistant_message="Ok."), now=1001.0,
                         next_events=nxt, clock=_clock()) == (0, "")
    # not lost: the session's next hook event delivers it from the local inbox
    monkeypatch.setattr(board, "fetch_answers", REAL_FETCH)
    monkeypatch.setattr(relayops, "_http", lambda *a, **k: (200, {"events": []}))
    out = board.handle(_bash("ls"), now=1030.0)
    assert "Oliver hat im Call-Board geantwortet" in out["hookSpecificOutput"]["additionalContext"]


def test_reading_answers_yourself_closes_the_question(relay):
    board.handle(_bash('vh-helper decide "Hooks" "PR mergen?"'), now=1000.0)
    board.handle(_bash('vh-helper next "Hooks"', stdout=json.dumps(_ans())), now=1010.0)
    assert board.open_names(SK, 1011.0) == []


def test_answer_older_than_keep_window_is_not_waited_for(relay):
    board.handle(_bash('vh-helper decide "Hooks" "PR mergen?"'), now=1000.0)
    assert board.open_names(SK, 1000.0 + board.ASK_KEEP_S + 1) == []


def test_local_relay_answer_file(tmp_path):
    d = tmp_path / "join"
    d.mkdir()
    (d / "session.json").write_text(json.dumps({"pid": os.getpid()}))
    rel = ms.write_local_relay(d, "room-l", owner_pid=os.getpid(), owner_session=ms.session_hash("JOINER"))
    rel = ms.load_relay() or rel
    board.handle(_bash('voicehook-agent decide --question "PR mergen?"'), now=1000.0)
    names = board.open_names(SK, 1001.0)
    assert names
    # the join writes the board answer into the helper's answer file
    relayops.append_jsonl(relayops.answer_path(relayops.local_dir(rel), names[0]), _ans("d9"))
    rc, text = asks.stop_ask(_ev("Stop", last_assistant_message="Ok."), now=1001.0,
                             clock=_clock())
    assert rc == asks.WAKE_EXIT and "Oliver hat im Call-Board geantwortet" in text


def test_stop_wait_hook_command_wakes(relay, monkeypatch, capsys):
    board.handle(_bash('vh-helper decide "Hooks" "PR mergen?"'), now=time.time())
    monkeypatch.setattr(relayops, "next_events", lambda rel, frm, timeout, **k: [_ans()])
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(_ev("Stop", last_assistant_message="Ok.")))
    assert activity.main(["stop-wait"]) == asks.WAKE_EXIT
    assert "Oliver hat im Call-Board geantwortet" in capsys.readouterr().err


REAL_FETCH = board.fetch_answers


def test_helper_decision_carries_option_label():
    """0.35.5: the chosen option text travels with the index (router guessed before, 29.10.)."""
    from voicehook_agent import relay as vrelay

    ev = vrelay.helper_decision_event({"type": "decision", "sender": "voicehook-dev: dev-1", "id": "d1",
                                       "number": 24, "verdict": "option", "option": 1,
                                       "option_label": "Neue Karte anlegen"}, question="Q?")
    assert ev["option"] == 1 and ev["option_label"] == "Neue Karte anlegen"
    ev2 = vrelay.helper_decision_event({"type": "decision", "sender": "a: b", "id": "d2", "verdict": "yes"},
                                       question="Q?")
    assert "option_label" not in ev2   # control: yes/no has no label
