"""Live-Bug 21.10.2026 (Call onyx-calm-lumen-P6PG): helper_decision #21 kam beim Router als
from="Steuern", #22 als "Armin-Box", beide Sessions sagten "nicht von mir".

Ursache: #21/#22 waren automatisch erkannte Entwürfe des Servers (question_check) aus den
Status-Boards dieser Helfer. Die Rückfrage question_check erreichte den Helfer nie: die CLI
(Router per WebRTC) ignorierte operator.question_check komplett, und auch beim Helfer selbst
filterten relay-next / Hook alles außer decision weg. Nach 90 s legte der Server die Frage
selbst an; die Antwort kam beim Helfer als "Antwort auf deine eigene Frage" ohne Frage und
ohne Hinweis "automatisch erkannt" an.

Hier: die CLI reicht question_check an den richtigen Helfer weiter (Hub/lokales Relay, sonst
eigenes `next`), relay-next und Hook liefern es aus, und eine automatisch erkannte Antwort
sagt das samt Frage."""
from __future__ import annotations

import asyncio
import json
import os

import pytest
from test_helper_access import PUMP, PUMP_URL, FakeHttp

from voicehook_agent import board, cli, relay, relayops
from voicehook_agent import helperaccess as vhelp
from voicehook_agent import multisession as ms
from voicehook_agent import session as vsession

QC = {"type": "question_check", "id": "q1a2b3c4d", "quote": "wartet auf Freigabe", "kind": "status",
      "wait_s": 90, "text": "Deine Meldung klingt nach einer Frage", "sender": "crown: Steuern"}


@pytest.fixture(autouse=True)
def _home(monkeypatch, tmp_path):
    """Nie das echte ~/.voicehook-agent (Join-Verzeichnisse, Inboxen)."""
    monkeypatch.setenv("VOICEHOOK_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.delenv(ms.LABEL_ENV, raising=False)


def _ctl() -> cli._Control:
    return cli._Control("room-a", "claude-1", None, None, 0, 0)


# ---- Router (Join) -------------------------------------------------------------------- #
def test_question_check_for_hub_helper_goes_to_pump_not_own_next():
    ctl = _ctl()
    ctl.helper_pump = vhelp.Pump("https://voicehook.ai", PUMP_URL, PUMP)
    ctl.helper_http = FakeHttp([])

    async def go():
        ctl.events.arm()
        assert cli._route_question_check(ctl, QC, "webrtc") is True
        await asyncio.sleep(0.05)
        return len(ctl.events)
    assert asyncio.run(go()) == 0
    assert ctl.helper_http.posts == [(PUMP_URL + "/answer", {"sender": "crown: Steuern", "event": QC},
                                      {"authorization": f"Bearer {PUMP}"})]


def test_own_question_check_lands_in_own_next():
    ctl = _ctl()
    own = {k: v for k, v in QC.items() if k != "sender"}

    async def go():
        ctl.events.arm()
        assert cli._route_question_check(ctl, own, "webrtc") is True
        return await ctl.events.get(0)
    ev = asyncio.run(go())
    assert ev["type"] == "question_check" and ev["id"] == "q1a2b3c4d" and "sender" not in ev


def test_bridge_transport_leaves_helper_question_check_to_server():
    ctl = _ctl()

    async def go():
        ctl.events.arm()
        cli._route_question_check(ctl, QC, "bridge")
        return len(ctl.events)
    assert asyncio.run(go()) == 0


def test_local_relay_helper_gets_question_check_in_answer_file(monkeypatch):
    monkeypatch.setenv(ms.MACHINE_LABEL_ENV, "crown")
    d = vsession.session_dir("room-a", "claude-x")
    d.mkdir(parents=True)
    ctl = _ctl()
    ctl.activity_path = d / "activity.log"
    ctl.relay_senders["crown: Steuern"] = "Steuern"
    assert cli._route_question_check(ctl, QC, "webrtc") is True
    got = relayops.take_jsonl(relayops.answer_path(d, "Steuern"), relayops._wanted)
    assert [e["id"] for e in got] == ["q1a2b3c4d"]


def test_local_status_registers_sender_for_routing(monkeypatch):
    """Ein Helfer, der nur sein Board setzt (nie decide), ist trotzdem zustellbar."""
    from test_relay_decide import _join as join_local
    d, ctl, _local = join_local(monkeypatch)
    obj = {"v": 1, "rid": "0123456789abcdef", "from": "Steuern", "op": "status",
           "body": {"doing": "wartet auf Freigabe", "open": [], "done": []}}
    asyncio.run(cli._serve_relay_request(ctl, d, obj))
    assert ctl.relay_senders.get("crown: Steuern") == "Steuern"


# ---- Helfer ----------------------------------------------------------------------------- #
def test_relay_next_wants_question_check():
    assert relayops._wanted(QC) is True


def test_answer_text_says_auto_detected_with_question():
    ev = {"type": "decision", "id": "d1", "number": 21, "verdict": "yes", "by": "Oliver", "auto": True,
          "question": "Freigabe für die Abgabe?"}
    txt = board.answer_text(ev, None)
    assert "automatisch erkannt" in txt and "Freigabe für die Abgabe?" in txt and "#21" in txt
    assert "deine eigene Frage" not in txt
    own = board.answer_text({"type": "decision", "id": "d2", "number": 9, "verdict": "yes", "by": "O"}, None)
    assert "deine eigene Frage" in own and "automatisch" not in own


def test_question_check_text_for_hook():
    txt = board.answer_text(QC, None)
    assert "wartet auf Freigabe" in txt and "decide" in txt and "90" in txt


def test_helper_decision_copy_marks_auto_and_question():
    ev = relay.helper_decision_event({"type": "decision", "id": "d1", "number": 21, "verdict": "yes",
                                      "by": "O", "sender": "crown: Steuern", "auto": True,
                                      "question": "Freigabe?"})
    assert (ev["from"], ev["label"], ev["question"], ev["auto"]) == ("Steuern", "crown", "Freigabe?", True)


# ---- Hook: Board-Helfer ohne eigene Frage bekommt die Rückfrage ------------------------- #
@pytest.fixture
def hook_env(monkeypatch, tmp_path):
    for k in ("CLAUDE_CODE_SESSION_ID", ms.NO_RELAY_ENV, "VOICEHOOK_SESSION", board.ENV_OFF, board.STATE_ENV_OFF):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("VOICEHOOK_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv(ms.LABEL_ENV, "Steuern")
    monkeypatch.setattr(board, "_spawn_post", lambda job: None)
    monkeypatch.setattr(board, "_spawn_sleeper", lambda skey: None)
    monkeypatch.setattr(ms, "_spawn_post", lambda *a: None)
    ms.write_relay("https://voicehook.ai/api/activity/relay", "fake.relay.test.only", "room-a", 3600,
                   owner_pid=os.getpid(), owner_session=ms.session_hash("JOINER"))


def _ev(name, **kw):
    d = {"session_id": "helper-1", "hook_event_name": name, "cwd": "/w/proj", "transcript_path": ""}
    d.update(kw)
    return json.dumps(d)


def test_hook_delivers_question_check_to_board_only_helper(hook_env):
    asked: list = []

    def fetch(rel, frm):
        asked.append(frm)
        return [dict(QC)] if frm == "Steuern" else []
    outs = [board.handle(_ev("PostToolUse", tool_name="Bash",
                             tool_input={"command": 'vh-helper status "Steuern" "wartet auf Freigabe"'},
                             tool_response={"stdout": "{}", "stderr": ""}), now=1000.0, fetch=fetch),
            board.handle(_ev("UserPromptSubmit", prompt="weiter"), now=1100.0, fetch=fetch)]
    assert "Steuern" in asked
    ctxs = [o["hookSpecificOutput"]["additionalContext"] for o in outs if o]
    hits = [c for c in ctxs if "klingt nach einer Frage" in c]
    assert len(hits) == 1, ctxs                                       # genau einmal zugestellt
    assert "wartet auf Freigabe" in hits[0] and "decide" in hits[0]
