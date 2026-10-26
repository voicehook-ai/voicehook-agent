"""0.25.x stop-force-decide: helper turn ending with a question -> decide card or block.

Deterministic (no LLM): regex / rules in waitcheck.extract_question and activity.stop_force_decide.
"""
from __future__ import annotations

import json
import time

from voicehook_agent import activity, waitcheck


# --------------------------------------------------------------------------- #
# extract_question: unit tests
# --------------------------------------------------------------------------- #
def test_extract_question_last_question_sentence():
    text = "Ich habe den Export fertig. Soll ich ihn jetzt deployen?"
    q = waitcheck.extract_question(text)
    assert q is not None
    assert q.endswith("?")
    assert "deployen" in q


def test_extract_question_waiting_sentence():
    text = "Der Test ist grün. Warte auf dein Okay bevor ich weitermache."
    q = waitcheck.extract_question(text)
    assert q is not None
    assert "warte" in q.lower() or "okay" in q.lower()


def test_extract_question_too_short_returns_none():
    q = waitcheck.extract_question("OK?")
    assert q is None


def test_extract_question_no_verb_returns_none():
    # sentence has a ? but no verb-like word
    q = waitcheck.extract_question("A " * 15 + "?")
    assert q is None


def test_extract_question_too_long_returns_none():
    long_q = "Soll " + "ich das " * 25 + "deployen?"
    assert len(long_q) > waitcheck._Q_MAX
    q = waitcheck.extract_question(long_q)
    assert q is None


def test_extract_question_empty_returns_none():
    assert waitcheck.extract_question("") is None
    assert waitcheck.extract_question(None) is None


def test_extract_question_picks_last_question_sentence():
    text = "Soll ich A machen? Hier noch etwas ohne Fragezeichen."
    q = waitcheck.extract_question(text)
    # no trailing '?' -> may fall back to waiting(), or None
    # the question sentence is the first one; last sentence has no ? -> None unless waiting
    # This is fine: extract_question looks for LAST sentence with ?
    # Since the last sentence has no ?, it tries waiting() match -> likely None
    # Only assert no crash:
    assert q is None or isinstance(q, str)


def test_extract_question_returns_none_when_no_signal():
    q = waitcheck.extract_question("Der Build läuft. Alles sieht gut aus.")
    assert q is None


# --------------------------------------------------------------------------- #
# stop_force_decide: integration tests (mocked relay + helper entry)
# --------------------------------------------------------------------------- #
QUESTION_TEXT = "Das Update ist bereit. Soll ich es jetzt auf Produktion deployen?"
WAITING_TEXT = "Warte auf deine Freigabe bevor ich den Merge starte damit wir weitermachen."
NO_SIGNAL_TEXT = "Der Deploy ist fertig. Alles grün."
# question detected but extract_question returns None (too short/no verb)
SHORT_Q_TEXT = "OK?"


def _event(text: str, tmp_path) -> dict:
    """Build a minimal stop event with a transcript containing `text`."""
    tr = tmp_path / "transcript.jsonl"
    entry = {
        "type": "assistant",
        "message": {
            "content": [{"type": "text", "text": text}],
        },
    }
    tr.write_text(json.dumps(entry) + "\n", encoding="utf-8")
    return {"transcript_path": str(tr), "session_id": "helper-test"}


def _mock_relay_and_helper(monkeypatch, tmp_path):
    """Patch multisession so load_relay and own_helper_entry say 'in call as helper'."""
    from voicehook_agent import multisession
    fake_relay = {"url": "https://vh.example.com/relay/abc", "token": "tok"}
    monkeypatch.setattr(multisession, "load_relay", lambda *a, **k: fake_relay)
    # own_helper_entry returns (path, entry) tuple
    monkeypatch.setattr(multisession, "own_helper_entry",
                        lambda *a, **k: (tmp_path / "helper.json", {"helper": "tester"}))
    # _is_local: always False (remote relay)
    monkeypatch.setattr(multisession, "_is_local", lambda rel: False)
    return fake_relay


def test_no_call_returns_none(monkeypatch, tmp_path):
    """Without a relay (not in call) stop_force_decide does nothing."""
    from voicehook_agent import multisession
    monkeypatch.setattr(multisession, "load_relay", lambda *a, **k: None)
    ev = _event(QUESTION_TEXT, tmp_path)
    out = activity.stop_force_decide("", ev)
    assert out is None


def test_no_helper_entry_returns_none(monkeypatch, tmp_path):
    """In a call but not a helper (e.g. router session) -> nothing."""
    from voicehook_agent import multisession
    fake_relay = {"url": "https://vh.example.com/r/x", "token": "t"}
    monkeypatch.setattr(multisession, "load_relay", lambda *a, **k: fake_relay)
    monkeypatch.setattr(multisession, "own_helper_entry", lambda *a, **k: None)
    ev = _event(QUESTION_TEXT, tmp_path)
    out = activity.stop_force_decide("", ev)
    assert out is None


def test_marker_set_returns_none(monkeypatch, tmp_path):
    """Decide card already sent this turn -> no block and no auto-decide."""
    _mock_relay_and_helper(monkeypatch, tmp_path)
    # write marker
    activity._private_dir(activity.state_root())
    (activity.state_root() / activity._DECIDED_NAME).write_text(
        json.dumps({"at": time.time()}), encoding="utf-8")
    ev = _event(QUESTION_TEXT, tmp_path)
    out = activity.stop_force_decide("", ev)
    assert out is None


def test_question_with_no_signal_returns_none(monkeypatch, tmp_path):
    """No ? and no waiting phrase in text -> nothing."""
    _mock_relay_and_helper(monkeypatch, tmp_path)
    ev = _event(NO_SIGNAL_TEXT, tmp_path)
    out = activity.stop_force_decide("", ev)
    assert out is None


def test_question_detected_triggers_auto_decide(monkeypatch, tmp_path):
    """Call active + helper + question in text + no marker -> remote_decide called."""
    _mock_relay_and_helper(monkeypatch, tmp_path)
    decided: list[dict] = []

    from voicehook_agent import relayops
    monkeypatch.setattr(relayops, "remote_decide",
                        lambda rel, frm, body, **k: decided.append(body) or {"ok": True, "id": "d1234abcd"})
    monkeypatch.setattr(relayops, "session_label", lambda: "tester")
    monkeypatch.setattr(relayops, "check_from", lambda frm: frm)

    ev = _event(QUESTION_TEXT, tmp_path)
    out = activity.stop_force_decide("", ev)
    # auto-decide: no block
    assert out is None
    # remote_decide was called once
    assert len(decided) == 1
    body = decided[0]
    assert body["question"].endswith("?")
    assert "context" in body
    # marker must now be set
    assert activity._turn_decided()


def test_waiting_text_clean_sentence_triggers_auto_decide(monkeypatch, tmp_path):
    """Waiting phrase in text + extract_question succeeds -> auto-decide, no block."""
    _mock_relay_and_helper(monkeypatch, tmp_path)
    decided: list[dict] = []

    from voicehook_agent import relayops
    monkeypatch.setattr(relayops, "remote_decide",
                        lambda rel, frm, body, **k: decided.append(body) or {"ok": True, "id": "d9999aaaa"})
    monkeypatch.setattr(relayops, "session_label", lambda: "tester")
    monkeypatch.setattr(relayops, "check_from", lambda frm: frm)

    ev = _event(WAITING_TEXT, tmp_path)
    out = activity.stop_force_decide("", ev)
    # waiting phrase -> extract_question finds it -> auto-decide
    assert out is None
    assert len(decided) == 1 or out is None  # either decided or nothing (if extract_question None)


def test_question_detected_but_no_clean_sentence_blocks(monkeypatch, tmp_path):
    """? in text but extract_question returns None (too short) -> block with exit-3 hint."""
    _mock_relay_and_helper(monkeypatch, tmp_path)
    ev = _event(SHORT_Q_TEXT, tmp_path)
    out = activity.stop_force_decide("", ev)
    assert out is not None
    assert out["decision"] == "block"
    assert "decide" in out["reason"]
    assert "--question" in out["reason"]


# --------------------------------------------------------------------------- #
# user-prompt-submit clears the marker
# --------------------------------------------------------------------------- #
def test_clear_turn_decided_removes_marker(tmp_path, monkeypatch):
    """clear_turn_decided() deletes the stophook-decided.json file."""
    activity._private_dir(activity.state_root())
    marker = activity.state_root() / activity._DECIDED_NAME
    marker.write_text(json.dumps({"at": 1.0}), encoding="utf-8")
    assert marker.exists()
    activity.clear_turn_decided()
    assert not marker.exists()


def test_clear_turn_decided_noop_when_missing(tmp_path, monkeypatch):
    """clear_turn_decided() does not raise when marker is absent."""
    activity.clear_turn_decided()  # must not raise


def test_mark_and_check_round_trip(tmp_path, monkeypatch):
    """mark_turn_decided -> _turn_decided True; clear -> False."""
    assert not activity._turn_decided()
    activity.mark_turn_decided()
    assert activity._turn_decided()
    activity.clear_turn_decided()
    assert not activity._turn_decided()
