"""0.21.3: the session's own questions (AskUserQuestion, a reply ending with a question)
show up on the call's Decision Board as asked, and the board answer goes back to the
session (AskUserQuestion: the tool's answers; Stop: an asyncRewake). Fixtures only, nothing
reaches a real board."""
from __future__ import annotations

import json
import os
import subprocess

import pytest

from voicehook_agent import activity, agents, asks, board, notify
from voicehook_agent import multisession as ms

URL = "https://voicehook.ai/api/activity/relay"
TOKEN = "fake.relay.test.only"  # fake, low entropy (gitleaks)


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    for k in ("CLAUDE_CODE_SESSION_ID", ms.LABEL_ENV, ms.MACHINE_LABEL_ENV, ms.NO_RELAY_ENV,
              "VOICEHOOK_SESSION", asks.ENV_OFF, asks.ASK_WAIT_ENV):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("VOICEHOOK_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv(ms.LABEL_ENV, "Hooks")


@pytest.fixture
def relay(monkeypatch):
    """Remote relay of the joining session JOINER; this test session is a helper."""
    ms.write_relay(URL, TOKEN, "room-a", 3600, owner_pid=os.getpid(),
                   owner_session=ms.session_hash("JOINER"))
    monkeypatch.setattr(ms, "_spawn_post", lambda *a: None)
    return ms.load_relay()


def _ev(name, sid="other-1", **kw) -> str:
    ev = {"session_id": sid, "hook_event_name": name, "cwd": "/Users/x/proj", "transcript_path": ""}
    ev.update(kw)
    return json.dumps(ev)


AUQ_INPUT = {"questions": [
    {"question": "Which auth method should we use?", "header": "Auth", "multiSelect": False,
     "options": [{"label": "OAuth", "description": "Standard, needs a callback URL"},
                 {"label": "API key", "description": "Simplest"},
                 {"label": "Magic link", "description": "Mail based"}]},
    {"question": "Soll ich die Migration gleich mitliefern?", "header": "Migration", "multiSelect": False,
     "options": [{"label": "Ja", "description": ""}, {"label": "Nein, später", "description": ""}]},
]}


def _auq(sid="other-1", inp=None):
    return _ev("PreToolUse", sid=sid, tool_name="AskUserQuestion", tool_use_id="toolu_9",
               tool_input=inp or AUQ_INPUT)


def _dec(dec_id, opt=None, text="", verdict=None):
    ev = {"type": "decision", "id": dec_id, "verdict": verdict or ("option" if opt is not None else "yes"),
          "by": "Human", "number": 7, "question": "q"}
    if opt is not None:
        ev["option"] = opt
    if text:
        ev["text"] = text
    return ev


class FakeBoard:
    """create() hands out ids; next_events() returns queued answers once."""

    def __init__(self):
        self.created: list[dict] = []
        self.queue: list[dict] = []
        self.calls = 0

    def create(self, job):
        items = job.get("items") or [{"question": job["question"], "options": job["options"]}]
        ids = [f"d{len(self.created) + i:08d}" for i in range(len(items))]
        self.created += [dict(i, id=x) for i, x in zip(items, ids)]
        qk = job.get("qk") or notify._qkey(job["question"])
        if len(ids) > 1:
            notify.set_ids(job["skey"], qk, ids)
        else:
            notify.set_id(job["skey"], qk, ids[0])
        return ids

    def next_events(self, rel, frm, timeout, match_ids=None):
        self.calls += 1
        out = [e for e in self.queue if not match_ids or e["id"] in match_ids]
        self.queue = [e for e in self.queue if e not in out]
        return out


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        self.t += 10.0
        return self.t


# ----- the reply ends with a question -----------------------------------------------------
@pytest.mark.parametrize("text,question,options", [
    ("Tests sind grün, PR #92 ist offen.\n\nSoll ich mergen?", "Soll ich mergen?", []),
    ("Done. Shall I also bump the version", "Shall I also bump the version", []),
    ("Zwei Wege:\n\nWelche Variante nehmen wir?\nA) Raster 3 Spalten\nB) 2 Spalten\nC) so lassen",
     "Welche Variante nehmen wir?", ["Raster 3 Spalten", "2 Spalten", "so lassen"]),
    ("Welcher Fix zuerst?\n\n1. Pill-Farbe\n2. Abstand", "Welcher Fix zuerst?", ["Pill-Farbe", "Abstand"]),
    ("Optionen: A) **Server zuerst** / B) CLI zuerst", "Optionen", ["Server zuerst", "CLI zuerst"]),
    ("- A: deploy now\n- B: wait for review\n\nWhich one?", "Which one?", []),
])
def test_stop_question_detects(text, question, options):
    q = asks.stop_question(text)
    assert q is not None and q["question"] == question and q["options"] == options


@pytest.mark.parametrize("text", [
    "", "Fertig. PR #92 ist gemerged.", "Warum schlug das fehl? Weil der Token abgelaufen war. Behoben.",
    "Hier der Code:\n\n```python\nx = input('ok?')\n```", None,
    "Soll ich den Key token=hunter2hunter2 eintragen?",
])
def test_stop_question_ignores(text):
    assert asks.stop_question(text) is None


def test_stop_question_cuts_paths_and_keeps_context():
    q = asks.stop_question("Ich habe /Users/x/kunden/acme/app.py angepasst und getestet.\n\n"
                           "Soll ich /Users/x/kunden/acme/app.py auch deployen?")
    assert q["question"] == "Soll ich app.py auch deployen?" and "kunden" not in json.dumps(q)
    assert "angepasst" in q["context"]


def test_last_message_from_event_or_transcript(tmp_path):
    assert asks.last_message({"last_assistant_message": "Soll ich?"}) == "Soll ich?"
    tr = tmp_path / "t.jsonl"
    tr.write_text("\n".join(json.dumps(x) for x in [
        {"type": "user", "message": {"content": "hi"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Mergen?"}]}},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash"}]}}]))
    assert asks.last_message({"transcript_path": str(tr)}) == "Mergen?"


# ----- AskUserQuestion: every question, labels, descriptions ------------------------------
def test_ask_items_all_questions_with_context():
    items = notify.ask_items(AUQ_INPUT)
    assert [i["question"] for i in items] == ["Which auth method should we use?",
                                              "Soll ich die Migration gleich mitliefern?"]
    assert items[0]["options"] == ["OAuth", "API key", "Magic link"]
    assert items[0]["context"].startswith("Topic: Auth; OAuth: Standard, needs a callback URL")
    assert items[1]["options"] == ["Ja", "Nein, später"] and items[1]["raw"] == AUQ_INPUT["questions"][1]["question"]


def test_handle_posts_one_decision_per_question(relay):
    jobs: list[dict] = []
    job = notify.handle(_auq(), now=1000.0, post=jobs.append)
    assert job["from"] == "Hooks" and len(job["items"]) == 2 and job["qk"]
    calls = []
    import voicehook_agent.notify as n
    orig = n._decide
    try:
        n._decide = lambda j, body: calls.append(body) or {"ok": True, "id": f"d{len(calls):08d}"}
        assert notify.create(job) == ["d00000001", "d00000002"]
    finally:
        n._decide = orig
    assert calls[0]["options"] == ["OAuth", "API key", "Magic link"] and "context" in calls[0]
    assert calls[1]["question"] == "Soll ich die Migration gleich mitliefern?"
    slot = notify.open_slot(ms.session_hash("other-1"))
    assert slot["ids"] == ["d00000001", "d00000002"]
    # the human answers in the terminal: both leave the board
    out = notify.on_progress(_ev("PostToolUse", tool_name="AskUserQuestion"), now=1001.0, post=jobs.append)
    assert out["delete"] == "d00000001" and out["delete_ids"] == ["d00000002"]


def test_ask_wait_answers_the_tool_from_the_board(relay):
    fb, clock = FakeBoard(), Clock()
    fb.queue = [_dec("d00000000", opt=1), _dec("d00000001", opt=0)]
    out = asks.ask_wait(_auq(), now=1000.0, next_events=fb.next_events, clock=clock,
                        create=fb.create)
    hs = out["hookSpecificOutput"]
    assert hs["permissionDecision"] == "allow" and hs["hookEventName"] == "PreToolUse"
    assert hs["updatedInput"]["questions"] == AUQ_INPUT["questions"]
    assert hs["updatedInput"]["answers"] == {"Which auth method should we use?": "API key",
                                             "Soll ich die Migration gleich mitliefern?": "Ja"}
    assert notify.open_slot(ms.session_hash("other-1")) is None   # answered: nothing to delete


def test_ask_wait_free_text_and_timeout(relay):
    fb, clock = FakeBoard(), Clock()
    one = {"questions": [AUQ_INPUT["questions"][0]]}
    fb.queue = [_dec("d00000000", text="Passkeys", verdict="option")]
    out = asks.ask_wait(_auq(inp=one), next_events=fb.next_events, clock=clock, create=fb.create)
    assert out["hookSpecificOutput"]["updatedInput"]["answers"] == {"Which auth method should we use?": "Passkeys"}
    # only one of two answered in time: the dialog shows, the decisions stay
    fb2, clock2 = FakeBoard(), Clock()
    fb2.queue = [_dec("d00000000", opt=0)]
    assert asks.ask_wait(_auq(sid="other-2"), next_events=fb2.next_events, clock=clock2,
                         create=fb2.create) is None
    assert notify.open_slot(ms.session_hash("other-2"))["ids"] == ["d00000000", "d00000001"]


def test_ask_wait_only_for_helpers_of_a_call(relay, monkeypatch):
    fb = FakeBoard()
    assert asks.ask_wait(_auq(sid="JOINER"), next_events=fb.next_events, create=fb.create) is None
    monkeypatch.setenv(asks.ASK_WAIT_ENV, "0")
    assert asks.ask_wait(_auq(), next_events=fb.next_events, create=fb.create) is None
    monkeypatch.delenv(asks.ASK_WAIT_ENV)
    monkeypatch.setenv(asks.ENV_OFF, "0")
    assert asks.ask_wait(_auq(), next_events=fb.next_events, create=fb.create) is None
    assert fb.created == []


def test_ask_wait_without_call_does_nothing():
    fb = FakeBoard()
    assert asks.ask_wait(_auq(), next_events=fb.next_events, create=fb.create) is None
    assert asks.stop_ask(_ev("Stop", last_assistant_message="Soll ich?"), create=fb.create) == (0, "")
    assert fb.created == []


# ----- Stop: post once, the answer wakes the session --------------------------------------
STOP_TEXT = "PR ist grün.\n\nWas machen wir?\nA) Mergen\nB) Warten auf Review"


def test_stop_ask_posts_once_and_wakes_with_the_answer(relay):
    fb, clock = FakeBoard(), Clock()
    fb.queue = [_dec("d00000000", opt=0)]
    rc, text = asks.stop_ask(_ev("Stop", last_assistant_message=STOP_TEXT), now=1000.0,
                             next_events=fb.next_events, clock=clock, create=fb.create)
    assert rc == asks.WAKE_EXIT and "(aus deiner Antwort gespiegelt)" in text
    assert '"Was machen wir?" -> Human hat im Call-Board geantwortet: Option 1 "Mergen".' in text
    assert fb.created[0]["question"] == "Was machen wir?" and fb.created[0]["options"] == ["Mergen", "Warten auf Review"]
    sk = ms.session_hash("other-1")
    st = json.loads((activity.state_root() / board.STATE_NAME).read_text())
    assert "Hooks" in st["sessions"][sk]["asks"]                 # a late answer is polled too
    # the same question again (next Stop): never a second decision
    assert asks.stop_ask(_ev("Stop", last_assistant_message=STOP_TEXT), now=1100.0,
                         next_events=fb.next_events, clock=clock, create=fb.create) == (0, "")
    assert len(fb.created) == 1


def test_stop_ask_option_label_in_wake_text(relay):
    fb, clock = FakeBoard(), Clock()
    ev = _dec("d00000000", opt=1)
    ev["text"] = ""
    fb.queue = [dict(ev, text="")]
    rc, text = asks.stop_ask(_ev("Stop", last_assistant_message="Soll ich mergen?"),
                             next_events=fb.next_events, clock=clock, create=fb.create)
    assert rc == asks.WAKE_EXIT and text.startswith("voicehook-Board")
    assert "Human hat im Call-Board geantwortet" in text


def test_stop_ask_ends_quietly_when_the_session_goes_on(relay):
    fb, clock = FakeBoard(), Clock()
    sk = ms.session_hash("other-1")

    def nxt(rel, frm, timeout, match_ids=None):
        # the human types in the terminal meanwhile: the decision leaves the board
        notify.on_progress(_ev("UserPromptSubmit", prompt="nein"), now=1001.0, post=lambda j: None)
        return []
    assert asks.stop_ask(_ev("Stop", last_assistant_message="Soll ich mergen?"), now=1000.0,
                         next_events=nxt, clock=clock, create=fb.create) == (0, "")
    assert notify.open_slot(sk) is None


def test_stop_resolve_keeps_the_question_asked_at_this_stop(relay):
    fb = FakeBoard()
    sk = ms.session_hash("other-1")
    asks.stop_ask(_ev("Stop", last_assistant_message="Soll ich mergen?"), now=1000.0,
                  next_events=lambda *a, **k: [], clock=iter([0.0] + [1e9] * 5).__next__, create=fb.create)
    assert notify.open_slot(sk)["kind"] == "stop"
    assert notify.on_progress(_ev("Stop"), now=1001.0, post=lambda j: None) is None   # parallel sync Stop hook
    assert notify.open_slot(sk) is not None
    job = notify.on_progress(_ev("PostToolUse", tool_name="Read"), now=1002.0, post=lambda j: None)
    assert job["delete"] == "d00000000"


def test_stop_ask_skips_continuations_joiner_subagents(relay):
    fb = FakeBoard()
    for raw in (_ev("Stop", last_assistant_message="Soll ich?", stop_hook_active=True),
                _ev("Stop", sid="JOINER", last_assistant_message="Soll ich mergen?"),
                _ev("Stop", agent_id="a1", last_assistant_message="Soll ich mergen?"),
                _ev("Stop", last_assistant_message="Fertig.")):
        assert asks.stop_ask(raw, create=fb.create, next_events=fb.next_events) == (0, "")
    assert fb.created == []


def test_codex_stop_posts_the_question(relay, monkeypatch):
    jobs: list[dict] = []
    monkeypatch.setattr(notify, "spawn", jobs.append)
    monkeypatch.setattr(agents, "first_turn_end", lambda *a: True)
    out = agents.codex_hook("stop", json.dumps({"session_id": "codex-1", "turn_id": "t1", "cwd": "/x",
                                                 "last_assistant_message": STOP_TEXT}))
    assert json.loads(out) is not None
    assert len(jobs) == 1 and jobs[0]["question"] == "Was machen wir?" and jobs[0]["from"]


# ----- the settings command: only our own exit 3 wakes the session ------------------------
@pytest.mark.parametrize("code,want", [(3, 2), (2, 0), (0, 0), (1, 0)])
def test_stop_wait_command_maps_exit_codes(tmp_path, code, want):
    fake = tmp_path / "voicehook-agent-hook"
    fake.write_text(f"#!/bin/sh\necho usage >&2\nexit {code}\n")
    fake.chmod(0o755)
    cmd = activity._with_bin(activity.HOOK_COMMAND_STOP_WAIT, str(fake))
    assert subprocess.run(["/bin/sh", "-c", cmd], capture_output=True, check=False).returncode == want


def test_hook_main_stop_wait_and_ask_wait(relay, monkeypatch, capsys):
    import io
    monkeypatch.setattr(asks, "stop_ask", lambda raw: (asks.WAKE_EXIT, "answer: Mergen"))
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    assert activity.main(["stop-wait"]) == asks.WAKE_EXIT
    assert capsys.readouterr().err.strip() == "answer: Mergen"
    monkeypatch.setattr(asks, "ask_wait", lambda raw: {"hookSpecificOutput": {"x": 1}})
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    assert activity.main(["ask-wait"]) == 0
    assert json.loads(capsys.readouterr().out) == {"hookSpecificOutput": {"x": 1}}
