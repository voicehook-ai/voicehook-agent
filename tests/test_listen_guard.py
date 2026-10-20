"""0.21.4 listen guard: the joining session never ends its turn while nobody listens.

Live calls (20.10.2026): the operator agent was joined (`status` connected) but ended its turn
with no `next` waiting, or started the listener detached; the user's sentences piled up as
`pending`. The Stop hook now blocks that stop with the pending count and the command;
`voicehook-agent listen` is the gapless loop to start in the background."""
from __future__ import annotations

import asyncio
import io
import json
import types

import pytest
from test_session import _call, _join, fake_env  # noqa: F401  (fixture)

from voicehook_agent import activity, cli, listenguard, multisession

NOW = 1_000_000.0
SID = "sess-listen-guard"


def _status(**kw):
    st = {"ok": True, "type": "status", "connected": True, "pending": 2, "listening": 0,
          "listen_idle_s": 30.0, "leaving": False}
    st.update(kw)
    return st


# --------------------------------------------------------------------------- #
# decision logic
# --------------------------------------------------------------------------- #
def test_in_call_without_listener_blocks_with_pending_count():
    out = listenguard.decide({"session_id": SID}, _status(pending=3), None, NOW)
    assert out["decision"] == "block"
    assert "nobody is listening (3 pending)" in out["reason"]
    assert "run_in_background" in out["reason"]
    assert listenguard.LISTEN_CMD in out["reason"]


def test_listener_alive_allows_stop():
    assert listenguard.decide({}, _status(listening=1, listen_idle_s=0.0), None, NOW) is None


def test_gap_between_two_next_of_a_loop_is_not_deaf():
    assert listenguard.decide({}, _status(listen_idle_s=1.2), None, NOW) is None
    assert listenguard.decide({}, _status(listen_idle_s=listenguard.LISTEN_GAP_S + 1),
                              None, NOW) is not None


def test_never_listened_since_join_blocks():
    assert listenguard.decide({}, _status(listen_idle_s=None), None, NOW) is not None


def test_not_in_call_allows_stop():
    assert listenguard.decide({}, None, None, NOW) is None  # no own join
    assert listenguard.decide({}, _status(connected=False), None, NOW) is None
    assert listenguard.decide({}, _status(leaving=True), None, NOW) is None
    assert listenguard.decide({}, {"ok": False, "error": "x"}, None, NOW) is None


def test_join_without_listening_field_never_blocks():
    old = _status()
    del old["listening"]
    assert listenguard.decide({}, old, None, NOW) is None


def test_subagent_stop_never_blocks():
    assert listenguard.decide({"agent_id": "a1"}, _status(), None, NOW) is None


def test_stop_hook_active_allows_same_pending_state():
    ev = {"stop_hook_active": True}
    last = {"pending": 2, "at": NOW - 5}
    assert listenguard.decide(ev, _status(pending=2), last, NOW) is None
    assert listenguard.decide(ev, _status(pending=1), last, NOW) is None


def test_stop_hook_active_blocks_again_only_when_more_pending():
    ev = {"stop_hook_active": True}
    assert listenguard.decide(ev, _status(pending=4), {"pending": 2, "at": NOW - 5}, NOW)
    # a stale block state (older than LAST_BLOCK_KEEP_S) does not count
    stale = {"pending": 2, "at": NOW - listenguard.LAST_BLOCK_KEEP_S - 1}
    assert listenguard.decide(ev, _status(pending=2), stale, NOW)


def test_env_off(monkeypatch):
    monkeypatch.setenv(listenguard.ENV_OFF, "0")
    monkeypatch.setattr(listenguard, "own_join_dirs", lambda ev: 1 / 0)  # never reached
    assert listenguard.handle(json.dumps({"session_id": SID})) is None


def test_merge_keeps_both_reasons():
    a = {"decision": "block", "reason": "A"}
    b = {"decision": "block", "reason": "B"}
    assert listenguard.merge(a, b) == {"decision": "block", "reason": "A\n\nB"}
    assert listenguard.merge(None, b) is b
    ctx = {"hookSpecificOutput": {"additionalContext": "x"}}
    assert listenguard.merge(ctx, b) is b


# --------------------------------------------------------------------------- #
# end to end: the real join counts waiting `next`, the hook asks it
# --------------------------------------------------------------------------- #
def _stop(raw_event: dict) -> dict | None:
    return listenguard.handle(json.dumps({"hook_event_name": "Stop", **raw_event}))


@pytest.mark.usefixtures("fake_env")
def test_hook_against_running_join(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)  # join records this owner

    async def run():
        join = asyncio.create_task(_join(idle_timeout=0))
        st = await _call({"cmd": "status"})
        assert st["listening"] is False and st["consumers"] == 0  # 0.21.5: spool not armed yet
        assert st["listen_idle_s"] is None and st["leaving"] is False
        own = await asyncio.to_thread(_stop, {"session_id": SID})
        foreign = await asyncio.to_thread(_stop, {"session_id": "another-session"})
        again = await asyncio.to_thread(_stop, {"session_id": SID, "stop_hook_active": True})
        waiter = asyncio.create_task(_call({"cmd": "next", "timeout": 3}))
        await asyncio.sleep(0.3)
        st2 = await _call({"cmd": "status"})
        listening = await asyncio.to_thread(_stop, {"session_id": SID})
        await waiter
        st3 = await _call({"cmd": "status"})
        await _call({"cmd": "leave"})
        await asyncio.wait_for(join, 10)
        return own, foreign, again, st2, listening, st3

    own, foreign, again, st2, listening, st3 = asyncio.run(run())
    assert own and own["decision"] == "block" and "(0 pending)" in own["reason"]
    assert foreign is None                       # another Claude Code session: never
    assert again is None                         # stop_hook_active, same pending: no loop
    assert st2["consumers"] == 1 and st2["listen_idle_s"] == 0.0
    assert st2["listening"] is True              # 0.21.5: join + spool hold the call
    assert listening is None                     # a `next` waits: the stop goes through
    assert st3["consumers"] == 0 and st3["listen_idle_s"] < listenguard.LISTEN_GAP_S
    assert st3["listening"] is True and st3["unread"] == 0


def test_hook_main_prints_block_on_stop(monkeypatch, capsys):
    block = {"decision": "block", "reason": "R"}
    monkeypatch.setattr(listenguard, "handle", lambda raw: block)
    monkeypatch.setattr(activity, "relay_hook", lambda raw: None)
    monkeypatch.setattr(activity, "notify_progress", lambda raw: None)
    monkeypatch.setattr(activity, "board_hook", lambda raw: None)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": SID})))
    assert activity.main(["stop"]) == 0
    assert json.loads(capsys.readouterr().out) == block


def test_hook_main_no_guard_on_other_events(monkeypatch, capsys):
    monkeypatch.setattr(listenguard, "handle", lambda raw: 1 / 0)
    monkeypatch.setattr(activity, "relay_hook", lambda raw: None)
    monkeypatch.setattr(activity, "notify_progress", lambda raw: None)
    monkeypatch.setattr(activity, "board_hook", lambda raw: None)
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    assert activity.main(["user-prompt-submit"]) == 0
    assert capsys.readouterr().out == ""


def test_own_join_dirs_needs_known_session(monkeypatch):
    monkeypatch.setattr(multisession, "own_session_hash", lambda: "")
    assert listenguard.own_join_dirs({}) == []


# --------------------------------------------------------------------------- #
# `voicehook-agent listen`
# --------------------------------------------------------------------------- #
def _args(**kw):
    base = {"session": None, "wait": 0.0, "timeout": 0.0, "poll": 10.0}
    base.update(kw)
    return types.SimpleNamespace(**base)


def _replies(*items):
    seen = []

    def request(sock, req, timeout=None):
        seen.append(req)
        return items[len(seen) - 1]
    return request, seen


def test_listen_skips_non_actionable_and_exits_on_user(capsys):
    request, seen = _replies({"ok": True, "type": "timeout", "pending": 0},
                             {"ok": True, "type": "human_state", "pending": 0},
                             {"ok": True, "type": "voicebot_back", "pending": 0},
                             {"ok": True, "type": "user", "text": "Hallo?", "pending": 1})
    rc = cli._run_listen(_args(), request=request, resolve=lambda s, wait: "sock")
    assert rc == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["text"] == "Hallo?"
    assert len(seen) == 4 and all(r == {"cmd": "next", "timeout": 10.0, "lease": True}
                                  for r in seen)  # 0.21.5: no seq in these replies, no ack


def test_listen_wakes_on_every_actionable_type(capsys):
    for kind in ("helper_decision", "voicebot_left", "missed", "decision", "status_request"):
        request, _ = _replies({"ok": True, "type": kind, "pending": 0})
        assert cli._run_listen(_args(), request=request, resolve=lambda s, wait: "sock") == 0
        assert json.loads(capsys.readouterr().out)["type"] == kind


def test_listen_ended_and_no_session_exit_3(capsys):
    request, _ = _replies({"ok": False, "type": "ended"})
    assert cli._run_listen(_args(), request=request, resolve=lambda s, wait: "sock") == 3

    def nojoin(s, wait):
        raise cli.vsession.SessionError("no running join")
    assert cli._run_listen(_args(), request=request, resolve=nojoin) == 3
    assert "no-session" in capsys.readouterr().out


def test_listen_total_timeout(capsys):
    t = [0.0]

    def request(sock, req, timeout=None):
        t[0] += req["timeout"]
        return {"ok": True, "type": "timeout", "pending": 0}
    rc = cli._run_listen(_args(timeout=100.0), request=request,
                         resolve=lambda s, wait: "sock", clock=lambda: t[0])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["type"] == "timeout"
    assert t[0] == 100.0
