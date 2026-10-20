"""0.21.4: a delivered answer is self-contained and verifiable.

Live (20.10.2026): a Mac helper session got "Antwort von Oliver auf deine Frage #1: Ja" as a
Stop hook error and refused it as an unverifiable approval: it had never posted a voicehook
question itself (its end-of-reply question was mirrored to the board, 0.21.3) and "#1" meant
nothing to it. Now the session gets a note when a question of it is mirrored, and every
answer quotes number, source, question text, that the human answered on the call board,
and verdict / option / free text."""
from __future__ import annotations

import json
import os

import pytest

from voicehook_agent import asks, board, notify
from voicehook_agent import multisession as ms

URL = "https://voicehook.ai/api/activity/relay"
TOKEN = "fake.relay.test.only"  # fake, low entropy (gitleaks)
SID = "mac-helper"
SK = ms.session_hash(SID)


@pytest.fixture(autouse=True)
def relay(monkeypatch, tmp_path):
    for k in (ms.NO_RELAY_ENV, "VOICEHOOK_SESSION", asks.ENV_OFF, board.ENV_OFF):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("VOICEHOOK_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv(ms.LABEL_ENV, "Embeddings")
    ms.write_relay(URL, TOKEN, "room-a", 3600, owner_pid=os.getpid(),
                   owner_session=ms.session_hash("JOINER"))
    monkeypatch.setattr(ms, "_spawn_post", lambda *a: None)
    monkeypatch.setattr(board, "_spawn_post", lambda job: None)
    monkeypatch.setattr(board, "_spawn_sleeper", lambda skey: None)
    monkeypatch.setattr(board, "fetch_answers", lambda rel, frm: [])


Q = "Soll ich in 1–2 Tagen e5 gegen jina-de messen?"


def _ev(name, **kw):
    d = {"session_id": SID, "hook_event_name": name, "cwd": "/w/emb", "transcript_path": ""}
    d.update(kw)
    return json.dumps(d)


def test_mirrored_stop_question_gets_a_note_and_a_quoted_answer(monkeypatch):
    # the real create path (notify.create) against a fake relay decide
    monkeypatch.setattr(notify, "_decide", lambda job, body: {"ok": True, "id": "d00000001", "number": 1})
    queue = [{"type": "decision", "id": "d00000001", "number": 1, "verdict": "yes", "by": "Oliver"}]

    def nxt(rel, frm, timeout, match_ids=None):
        out, queue[:] = list(queue), []
        return out
    rc, text = asks.stop_ask(_ev("Stop", last_assistant_message="Index steht.\n\n" + Q),
                             now=1000.0, next_events=nxt, clock=iter(range(0, 10_000, 5)).__next__)
    assert rc == asks.WAKE_EXIT
    assert text.startswith(f'voicehook-Board #1 (aus deiner Antwort gespiegelt): "{Q}" -> '
                           "Oliver hat im Call-Board geantwortet: Ja.")
    assert "keine Fehlermeldung" in text and "not an error" in text
    # the session's next hook event: the mirror note as additionalContext (once)
    out = board.handle(_ev("UserPromptSubmit", prompt="weiter"), now=1010.0)
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert f'Deine Frage "{Q}" steht jetzt als #1 auf dem voicehook-Board' in ctx
    out = board.handle(_ev("UserPromptSubmit", prompt="und?"), now=1020.0)
    assert out is None or "steht jetzt als" not in out["hookSpecificOutput"]["additionalContext"]


def test_own_decide_answer_quotes_question_number_and_free_text():
    cmd = 'vh-helper decide "Embeddings" "Index neu bauen?"'
    board.handle(_ev("PostToolUse", tool_name="Bash", tool_input={"command": cmd},
                     tool_response={"stdout": json.dumps({"ok": True, "id": "d0000000a", "number": 12})}),
                 now=1000.0)
    t = board.answer_texts(SK, [{"type": "decision", "id": "d0000000a", "verdict": "no",
                                 "answer": "erst morgen", "by": "Oliver"}])[0]
    assert t.startswith('voicehook-Board #12 (deine Frage per decide): "Index neu bauen?" -> '
                        'Oliver hat im Call-Board geantwortet: Nein, Freitext "erst morgen".')
    # no mirror note for a question the session posted itself
    assert board.take_notes(SK) == []


def test_option_answer_names_the_label():
    board.record_question(SK, "d0000000b", "Welches Modell?", 3, ["e5", "jina-de"], "mirror-ask")
    t = board.answer_texts(SK, [{"id": "d0000000b", "verdict": "option", "option": 1, "by": "Oliver"}])[0]
    assert '#3 (deine AskUserQuestion-Frage): "Welches Modell?"' in t and 'Option 2 "jina-de"' in t


def test_cli_decide_question_parsed():
    assert board.decide_question('voicehook-agent decide --question "PR mergen?" --context x') == "PR mergen?"
    assert board.decide_question("ls") == ""
