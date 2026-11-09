"""0.35.8 (09.11., calls ember-nova-ivory-DQXJ / ember-orbit): a reply is mirrored to the
Decision Board only when it really asks the human: a complete question ("?" or _ASK_START),
never the answer to another agent (<cross-session-message>), never a question to an agent
of the call, never a duplicate of a decide question of the room, never a fragment."""
from __future__ import annotations

import json
import os

import pytest

from voicehook_agent import activity, asks, board
from voicehook_agent import multisession as ms

URL = "https://voicehook.ai/api/activity/relay"
TOKEN = "fake.relay.test.only"  # fake, low entropy (gitleaks)


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    for k in ("CLAUDE_CODE_SESSION_ID", ms.LABEL_ENV, ms.MACHINE_LABEL_ENV, ms.NO_RELAY_ENV,
              "VOICEHOOK_SESSION", asks.ENV_OFF, asks.ASK_WAIT_ENV):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("VOICEHOOK_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv(ms.LABEL_ENV, "Ines")


@pytest.fixture
def relay(monkeypatch):
    ms.write_relay(URL, TOKEN, "room-a", 3600, owner_pid=os.getpid(),
                   owner_session=ms.session_hash("JOINER"))
    monkeypatch.setattr(ms, "_spawn_post", lambda *a: None)
    return ms.load_relay()


def _transcript(tmp_path, prompt, reply):
    p = tmp_path / "t.jsonl"
    recs = [{"type": "user", "message": {"role": "user", "content": prompt}},
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "x", "name": "Bash"}]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "x", "content": "ok"}]}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": reply}]}}]
    p.write_text("\n".join(json.dumps(r) for r in recs) + "\n")
    return str(p)


def _ev(sid="other-1", **kw) -> dict:
    ev = {"session_id": sid, "hook_event_name": "Stop", "cwd": "/Users/x/proj", "transcript_path": ""}
    ev.update(kw)
    return ev


class Board:
    def __init__(self):
        self.created: list[dict] = []

    def create(self, job):
        from voicehook_agent import notify
        self.created.append(job)
        dec_id = f"d{len(self.created):08d}"
        notify.set_id(job["skey"], job.get("qk") or notify._qkey(job["question"]), dec_id)
        return [dec_id]


def _stop(ev: dict, fb: Board, now: float = 1000.0):
    return asks.stop_ask(json.dumps(ev), wait=True, now=now, create=fb.create,
                         next_events=lambda *a, **k: [], clock=iter([0.0] + [1e9] * 9).__next__)


# ----- negative cases (real ones of 09.11.) -------------------------------------------------
CROSS = ('<cross-session-message from="uds:/tmp/n.sock" from-name="Theo">\n'
         'Theo an Nora: Wer übernimmt die Spiegelung?\n</cross-session-message>')


@pytest.mark.parametrize("reply", [
    "Die Spiegelung liegt bei Nora. Soll ich ihr den Befund schicken?",          # Nora Nr. 13
    "Erledigt, liegt bei Nora.\n\nWelche Variante nimmst du?\nA) Server\nB) CLI",  # Nora Nr. 26
    "Bruno hier: Board ist sauber. Passt das so?",                                 # Bruno Nr. 23
])
def test_reply_to_another_agent_is_never_mirrored(relay, tmp_path, reply):
    fb = Board()
    ev = _ev(transcript_path=_transcript(tmp_path, CROSS, reply), last_assistant_message=reply)
    assert asks.agent_turn(ev)
    assert _stop(ev, fb) == (0, "") and fb.created == []
    assert asks.stop_question(reply) is not None          # it would have been a question


def test_cross_session_prompt_as_content_list(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text(json.dumps({"type": "user", "message": {"content": [{"type": "text", "text": CROSS}]}}) + "\n")
    assert asks.agent_turn({"transcript_path": str(p)})
    p.write_text(json.dumps({"type": "user", "message": {"content": "Mach den Fix."}}) + "\n")
    assert not asks.agent_turn({"transcript_path": str(p)})


def test_wahl_header_with_list_is_no_question():                                   # Ines Nr. 27
    text = ("Deine Wahl (Nora und Theo legen sie dir vor):\n\n"
            "A) Server zuerst\nB) CLI zuerst")
    assert asks.stop_question(text) is None


def test_decide_duplicate_in_the_room_is_never_mirrored(relay, tmp_path, monkeypatch):  # Ines Nr. 27
    fb = Board()
    sk = ms.session_hash("other-1")
    theo = ms.session_hash("theo-1")
    monkeypatch.setattr(board.time, "time", lambda: 900.0)
    st = {"sessions": {
        sk: {"room": "room-a", "from": "Ines"},
        theo: {"room": "room-a", "from": "Theo",
               "qs": {"dTheo": {"question": "Welche Variante nehmen wir zuerst, Server oder CLI?",
                                "src": "decide", "at": 900.0}}}}}
    activity._private_dir(activity.state_root())
    (activity.state_root() / board.STATE_NAME).write_text(json.dumps(st))
    reply = "Welche Variante nehmen wir zuerst?\nA) Server\nB) CLI"
    assert asks.stop_question(reply) is not None
    assert _stop(_ev(last_assistant_message=reply), fb, now=1000.0) == (0, "") and fb.created == []
    # much later (no fresh decide question any more): mirrored again
    assert _stop(_ev(last_assistant_message=reply), fb, now=900.0 + asks.DECIDE_FRESH_S + 60) == (0, "")
    assert len(fb.created) == 1


def test_own_decide_question_recorded_with_time_blocks_its_mirror(relay):
    sk = ms.session_hash("other-1")
    board.record_question(sk, "d1", "Soll ich #889 mergen?", 4, src="decide")
    _names, asked = board.room_questions(sk, 0.0)
    assert asked == ["Soll ich #889 mergen?"]
    assert asks.mirror_question(_ev(last_assistant_message="Soll ich PR #889 jetzt mergen?"), sk) is None


@pytest.mark.parametrize("text", [
    "Wenn du dich für B entscheidest (Frage Nr. 27):",                              # Ines Nr. 30
    "Wenn du dich für B entscheidest (Frage Nr. 27):\n\n1. Server\n2. CLI",
    "27):", "Nr. 27)?", "(3)?",
])
def test_fragments_are_never_mirrored(text):
    assert asks.stop_question(text) is None


def test_options_need_a_question_line():                                           # Ravi QWvCQHOj
    text = ("Fertig formuliert.\n\nDie drei Sätze für Oliver:\n"
            "1. Der Server spiegelt nur echte Fragen.\n2. Dubletten fallen weg.\n3. Fragmente nie.")
    assert asks.stop_question(text) is None
    assert asks.stop_question("Die drei Sätze für Oliver:\n\nA) eins\nB) zwei") is None


def test_question_to_an_agent_of_the_call_is_never_mirrored(relay):
    sk = ms.session_hash("other-1")
    st = {"sessions": {sk: {"room": "room-a", "from": "Ines"},
                       ms.session_hash("n"): {"room": "room-a", "from": "Nora"}}}
    activity._private_dir(activity.state_root())
    (activity.state_root() / board.STATE_NAME).write_text(json.dumps(st))
    for reply in ("Nora, soll ich den Befund an Theo geben?", "@Nora passt das so?",
                  "Frage an Nora: übernimmst du den Server-Teil?"):
        assert asks.mirror_question(_ev(last_assistant_message=reply), sk) is None, reply
    assert asks.mirror_question(_ev(last_assistant_message="Soll ich Nora den Befund geben?"), sk) is not None


def test_question_check_hint_untouched():
    """Problem 5 (server side, not here): the hint still names the server's 90 s auto question."""
    txt = board.question_check_text({"type": "question_check", "quote": "x", "id": "q1"})
    assert "90 s" in txt


# ----- positive controls ----------------------------------------------------------------------
def test_positive_controls(relay, tmp_path):
    q = asks.stop_question("Welche nehmen wir? A) Server zuerst B) CLI zuerst")
    assert q is not None and q["question"] == "Welche nehmen wir?" and q["options"] == ["Server zuerst", "CLI zuerst"]
    q = asks.stop_question("Tests grün.\n\nSoll ich #889 mergen?")
    assert q is not None and q["question"] == "Soll ich #889 mergen?" and q["options"] == []
    fb = Board()
    reply = "Tests grün.\n\nSoll ich #889 mergen?"
    ev = _ev(transcript_path=_transcript(tmp_path, "Mach den Fix für #889.", reply), last_assistant_message=reply)
    assert not asks.agent_turn(ev)
    _stop(ev, fb)
    assert len(fb.created) == 1 and fb.created[0]["question"] == "Soll ich #889 mergen?"


def test_similar():
    assert asks.similar("Soll ich #889 mergen?", "Soll ich #889 jetzt mergen?")
    assert not asks.similar("Soll ich #889 mergen?", "Welche Farbe soll der Button haben?")
