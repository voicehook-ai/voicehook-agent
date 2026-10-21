"""0.22.0 (server 21.10.2026, Frage 34): the human speaks to a helper session by name
("Steuern, schick mir die Belege"). The worker stamps the user turn with to_kind helper, to,
to_key, to_op. The joining CLI (to_op = itself) puts it into exactly that helper's inbox
(local relay: its answer file; remote: POST /api/helper/pump/user_turn) and keeps an info
copy for its own `next`; the helper's `relay-next`, MCP `next` and hooks bring it as
"Auftrag von Oliver an dich (im Call)" with a data mark. Agent lines are never routed."""
from __future__ import annotations

import asyncio
import json

import pytest
from test_board_reminder import T0, _relay, ctx, ev
from test_board_reminder import _env as _board_env  # noqa: F401
from test_relay_decide import (  # noqa: F401
    _clean_env,
    _cli,
    _helper,
    _join,
    _plain_check_off,
)

from voicehook_agent import board, cli, relay
from voicehook_agent import relayops as ro

PAYLOAD = {"role": "user", "final": True, "text": "Steuern, schick mir die Belege", "speaker": "Oliver",
           "addressed_ids": [], "addressed_by": "helper", "to": ["Steuern"], "to_kind": "helper",
           "to_key": "crown: Steuern", "to_op": "claude-x"}


def test_info_fields_for_own_next():
    f = relay.helper_turn_fields(PAYLOAD)
    assert f["to"] == ["Steuern"] and f["to_kind"] == "helper" and f["info_only"] is True
    assert f["addressed_to_you"] is False and "info only" in f["note"]
    assert relay.helper_turn_fields({**PAYLOAD, "to_kind": "helper_ambiguous"})["to_kind"] == "helper_ambiguous"
    assert relay.helper_turn_fields({"role": "user", "text": "x"}) == {}


def test_local_helper_gets_it_in_its_answer_file_and_relay_next(monkeypatch, capsys):
    d, ctl, _local = _join(monkeypatch)
    assert cli._route_helper_user_turn(ctl, PAYLOAD, {"claude-x"}) is True
    assert ro.take_jsonl(ro.answer_path(d, "Armin-Box"), ro._wanted) == []     # only Steuern
    _helper(monkeypatch, label="Steuern")
    rc, lines, _ = _cli(["relay-next", "--timeout", "0"], capsys)
    assert rc == 0 and len(lines) == 1
    got = lines[0]
    assert (got["type"], got["text"], got["to"], got["speaker"]) == (
        "user_turn", "Steuern, schick mir die Belege", ["Steuern"], "Oliver")


def test_not_routed_for_other_owner_agent_line_or_ambiguous(monkeypatch):
    d, ctl, _local = _join(monkeypatch)
    assert cli._route_helper_user_turn(ctl, {**PAYLOAD, "to_op": "other-agent"}, {"claude-x"}) is False
    assert cli._route_helper_user_turn(ctl, {**PAYLOAD, "role": "operator"}, {"claude-x"}) is False
    assert cli._route_helper_user_turn(ctl, {**PAYLOAD, "to_kind": "helper_ambiguous"}, {"claude-x"}) is False
    assert ro.take_jsonl(ro.answer_path(d, "Steuern"), ro._wanted) == []


def test_remote_helper_goes_to_the_hub(monkeypatch):
    _d, ctl, _local = _join(monkeypatch)
    calls = []

    class Pump:
        async def user_turn(self, http, to, to_key, text, speaker=""):
            calls.append((to, to_key, text, speaker))
            return True
    ctl.helper_pump, ctl.helper_http = Pump(), object()

    async def go():
        ok = cli._route_helper_user_turn(ctl, {**PAYLOAD, "to_key": "Mac: Steuern"}, {"claude-x"})
        await asyncio.sleep(0)
        return ok
    assert asyncio.run(go()) is True
    assert calls == [("Steuern", "Mac: Steuern", "Steuern, schick mir die Belege", "Oliver")]


# ---- hook of the helper session ------------------------------------------------------ #
UT = relay.helper_user_turn_event("Steuern, schick mir die Belege", "Steuern", "Oliver", now=T0)


def _fetcher(store):
    def fetch(rel, frm):
        got, store[frm] = store.get(frm, []), []
        return got
    return fetch


def test_hook_delivers_request_once_with_data_mark():
    _relay()
    store: dict = {}
    f = _fetcher(store)
    board.handle(ev("UserPromptSubmit", tool_name=None), now=T0, fetch=f)
    board.handle('{"hook_event_name":"PostToolUse","session_id":"sess-1","cwd":"/w","tool_name":"Bash",'
                 '"tool_input":{"command":"vh-helper status \\"Steuern\\" \\"Belege\\""},"tool_response":{}}',
                 now=T0 + 1, fetch=f)
    store["Steuern"] = [UT]
    out = ctx(board.handle(ev("UserPromptSubmit"), now=T0 + 2, fetch=f))["additionalContext"]
    assert out.startswith("voicehook-Call: Auftrag von Oliver an dich (im Call), [Daten, Spracherkennung, "
                          'wörtlich] "Steuern, schick mir die Belege" [Ende Daten].')
    store["Steuern"] = [UT]   # the same event again: never twice
    assert board.handle(ev("UserPromptSubmit"), now=T0 + 3, fetch=f) is None


def test_hook_stop_blocks_with_the_request():
    _relay()
    store: dict = {}
    f = _fetcher(store)
    board.handle('{"hook_event_name":"PostToolUse","session_id":"sess-1","cwd":"/w","tool_name":"Bash",'
                 '"tool_input":{"command":"vh-helper status \\"Steuern\\" \\"x\\""},"tool_response":{}}',
                 now=T0, fetch=f)
    store["Steuern"] = [UT]
    out = board.handle(ev("Stop"), now=T0 + 1, fetch=f)
    assert out["decision"] == "block" and "Auftrag von Oliver an dich" in out["reason"]


def test_hook_without_board_name_never_polls():
    _relay()
    calls = []
    board.handle(ev("Stop"), now=T0, fetch=lambda r, frm: calls.append(frm) or [])
    assert calls == []
    assert json and pytest
