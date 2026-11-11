"""SS2kwgDK (Oliver 10.11.): a permission dialog of a helper session goes on the call's board, and only
the owner's CLICK allows it (never by voice, never automatically, never a guest). A no denies; anything
else (timeout, free text, missing fields) leaves the dialog in the session as before. Fixtures only."""
# ruff: noqa: F811  (pytest fixtures imported from test_asks)
from __future__ import annotations

import json

import pytest
from test_asks import Clock, FakeBoard, _env, relay  # noqa: F401  (fixtures)

from voicehook_agent import activity, asks, notify
from voicehook_agent import multisession as ms


def _perm(sid="other-1", cmd="git push origin main"):
    return json.dumps({"session_id": sid, "hook_event_name": "PermissionRequest", "cwd": "/w/p",
                       "transcript_path": "", "tool_name": "Bash", "tool_input": {"command": cmd}})


def _answer(verdict, via="click", role="owner", text=""):
    ev = {"type": "decision", "id": "d00000000", "verdict": verdict, "by": "Oliver", "number": 3, "question": "q"}
    if via:
        ev["answered_via"] = via
    if role:
        ev["answered_role"] = role
    if text:
        ev["text"] = text
    return ev


def _run(raw, answers):
    fb, clock = FakeBoard(), Clock()
    jobs: list = []
    notify.handle(raw, now=1000.0, post=jobs.append)   # the parallel permission-request hook
    assert jobs, "permission-request legte keine Entscheidung an"
    fb.create(jobs[0])
    fb.queue = list(answers)
    return asks.permission_wait(raw, next_events=fb.next_events, clock=clock, sleep=lambda s: None), fb


def test_klick_des_besitzers_gibt_frei(relay):
    out, fb = _run(_perm(), [_answer("yes")])
    assert out == {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": "allow"}}}
    assert "git push" in fb.created[0]["question"]                          # konkreter Befehl auf der Karte
    assert notify.open_slot(ms.session_hash("other-1")) is None


@pytest.mark.parametrize("ans", [_answer("yes", via="voice"), _answer("yes", role="guest"),
                                 _answer("yes", via=None), _answer("yes", role=None),
                                 _answer("other", text="mach mal")])
def test_nie_frei_per_stimme_gast_oder_ohne_angabe(relay, ans):
    out, _fb = _run(_perm(), [ans])
    assert out is None                                                     # Dialog bleibt in der Session


@pytest.mark.parametrize("via", ["click", "voice"])
def test_nein_des_besitzers_lehnt_ab(relay, via):
    out, _fb = _run(_perm(), [_answer("no", via=via, text="erst nach dem Call")])
    d = out["hookSpecificOutput"]["decision"]
    assert d["behavior"] == "deny" and "erst nach dem Call" in d["message"]


def test_timeout_laesst_dialog_stehen(relay):
    out, _fb = _run(_perm(), [])
    assert out is None


def test_nur_fuer_helfer_eines_calls_und_abschaltbar(relay, monkeypatch):
    fb = FakeBoard()
    assert asks.permission_wait(_perm(sid="JOINER"), next_events=fb.next_events, sleep=lambda s: None) is None
    monkeypatch.setenv(asks.PERM_WAIT_ENV, "0")
    assert asks.permission_wait(_perm(), next_events=fb.next_events, sleep=lambda s: None) is None
    monkeypatch.delenv(asks.PERM_WAIT_ENV)
    assert asks.permission_wait(json.dumps({"hook_event_name": "PermissionRequest", "session_id": "o",
                                            "tool_name": "AskUserQuestion"}), next_events=fb.next_events) is None


def test_ohne_call_nichts():
    assert asks.permission_wait(_perm(), next_events=FakeBoard().next_events, sleep=lambda s: None) is None


def test_hook_ist_installiert_mit_langem_timeout():
    entry = activity.extra_entry("PermissionRequest")
    (h,) = entry["hooks"]
    assert h["command"].endswith("permission-wait || true") and h["timeout"] > asks.PERM_WAIT_S
    assert entry["matcher"] == "*"
