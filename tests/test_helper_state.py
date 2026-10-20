"""0.21.4: the state of a helper session in the call is its REAL state, never a sticky
`arbeitet`.

Live (20.10.2026): many helper sessions showed `arbeitet` while idle. Causes fixed here:
Claude Code sends no Stop after Esc (state stayed `arbeitet`), a closed or crashed session
sent nothing at all, detached posts could arrive out of order (an older `arbeitet` after
the Stop's `leerlauf`), local helpers sent no state (the page derived `arbeitet` from a
fresh board), and a Stop with an open question was `leerlauf` instead of `wartet`."""
from __future__ import annotations

import json
import os

import pytest

from voicehook_agent import activity, board, relayops
from voicehook_agent import multisession as ms

URL = "https://voicehook.ai/api/activity/relay"
TOKEN = "fake.relay.test.only"  # fake, low entropy (gitleaks)
T0 = 1_800_000_000.0
REAL_SESSION_PID = board.session_pid
POSTS: list[dict] = []
SID = "sess-1"
SK = ms.session_hash(SID)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in (ms.LABEL_ENV, ms.NO_RELAY_ENV, board.ENV_OFF, board.STATE_ENV_OFF):
        monkeypatch.delenv(k, raising=False)
    POSTS.clear()
    monkeypatch.setattr(board, "_spawn_post", POSTS.append)
    monkeypatch.setattr(board, "_spawn_sleeper", lambda skey: None)
    monkeypatch.setattr(board, "fetch_answers", lambda rel, frm: [])
    monkeypatch.setattr(board, "session_pid", lambda *a, **k: 4242)


def _relay(local_dir=None):
    if local_dir is not None:
        return ms.write_local_relay(local_dir, "room-a", owner_pid=os.getpid(), owner_session="own",
                                    now=T0 - 10)
    return ms.write_relay(URL, TOKEN, "room-a", 3600 * 24 * 365 * 10, owner_pid=os.getpid(),
                          owner_session="own", now=T0 - 10)


def ev(name="PostToolUse", **kw):
    d = {"hook_event_name": name, "session_id": SID, "cwd": "/work/salesengine",
         "transcript_path": ""}
    if name.startswith("PostToolUse") or name == "PreToolUse":
        d.setdefault("tool_name", "Read")
        d.setdefault("tool_input", {"file_path": "/x/a.py"})
    d.update(kw)
    return json.dumps(d)


def _states():
    return [p["state"] for p in POSTS if p.get("op") == "state"]


def _me():
    return json.loads((activity.state_root() / board.STATE_NAME).read_text())["sessions"][SK]


# ----- Stop: leerlauf, or wartet with an open question; always sent ------------------
def test_stop_is_leerlauf_and_always_sent():
    _relay()
    board.handle(ev("UserPromptSubmit", prompt="los"), now=T0)
    board.handle(ev("Stop", last_assistant_message="Fertig."), now=T0 + 5)
    board.handle(ev("Stop", last_assistant_message="Fertig."), now=T0 + 6)   # resent, heals
    assert _states() == ["arbeitet", "leerlauf", "leerlauf"]


def test_stop_with_open_decide_or_question_is_wartet():
    _relay()
    board.handle(ev(tool_name="Bash", tool_input={"command": 'vh-helper decide "S" "PR mergen?"'},
                    tool_response={"stdout": "{}"}), now=T0)
    board.handle(ev("Stop", last_assistant_message="Fertig."), now=T0 + 5)
    assert _states()[-1] == "wartet"
    POSTS.clear()
    board.handle(ev("Stop", session_id="sess-2", last_assistant_message="Soll ich mergen?"),
                 now=T0 + 6)
    assert _states() == ["wartet"]


def test_status_doing_after_the_turn_does_not_force_arbeitet():
    _relay()
    board.handle(ev("Stop", last_assistant_message="Fertig."), now=T0)
    POSTS.clear()
    # a board update (meta) right after the Stop: the session is not working again
    board.handle(ev(tool_name="Bash", tool_input={"command": 'vh-helper status "S" "PR offen"'},
                    tool_response={"stdout": "{}"}), now=T0 + 1)
    assert _me()["state"] == "leerlauf" and _states() == []


# ----- ordering: a stale detached post never overwrites a newer state ----------------
def test_stale_job_is_skipped(monkeypatch):
    _relay()
    sent = []
    monkeypatch.setattr(board, "post_state", lambda rel, frm, state: sent.append(state) or True)
    board.handle(ev("UserPromptSubmit", prompt="los"), now=T0)
    work_job = POSTS[-1]
    board.handle(ev("Stop", last_assistant_message="Fertig."), now=T0 + 1)
    stop_job = POSTS[-1]
    assert board.run_job(stop_job) is True          # the Stop's child runs first ...
    assert board.run_job(work_job) is False         # ... the older one comes late: skipped
    assert sent == ["leerlauf"]


# ----- local relay: state goes to the join -----------------------------------------
def test_local_relay_posts_state_through_the_join(tmp_path, monkeypatch):
    d = tmp_path / "join"
    d.mkdir()
    (d / "session.json").write_text(json.dumps({"pid": os.getpid()}))
    _relay(local_dir=d)
    reqs = []
    monkeypatch.setattr(relayops, "local_request",
                        lambda rel, frm, op, body, **k: reqs.append((frm, op, body)) or {"ok": True})
    assert board.post_state(ms.load_relay(T0), "SE Gate", "leerlauf") is True
    assert reqs == [("SE Gate", "state", {"state": "leerlauf"})]


def test_relay_request_parses_state_op():
    line = {"rid": "0123456789abcdef", "from": "SE Gate", "op": "state", "body": {"state": "wartet"}}
    assert relayops.parse_request(line) == ("0123456789abcdef", "SE Gate", "state", {"state": "wartet"})
    bad = dict(line, body={"state": "kaputt"})
    assert relayops.parse_request(bad)[3] is None


# ----- no Stop after Esc / session gone: the watcher -----------------------------------
def test_watcher_idles_a_session_without_events_after_esc():
    _relay()
    board.handle(ev("UserPromptSubmit", prompt="los"), now=T0)
    board.handle(ev("PostToolUseFailure", tool_name="Bash", is_interrupt=True), now=T0 + 10)
    jobs, live = board.watch_step(now=T0 + 10 + board.IDLE_GUESS_S - 1, alive=lambda p: True)
    assert jobs == [] and live == 1                            # not long enough
    jobs, live = board.watch_step(now=T0 + 10 + board.IDLE_GUESS_S + 1, alive=lambda p: True)
    assert [(j["op"], j["state"]) for j in jobs] == [("state", "leerlauf")]
    assert _me()["state"] == "leerlauf"


def test_watcher_keeps_arbeitet_while_a_tool_runs():
    _relay()
    board.handle(ev("UserPromptSubmit", prompt="los"), now=T0)
    board.handle(ev("PreToolUse", tool_name="Bash", tool_input={"command": "make build"}), now=T0 + 1)
    jobs, _ = board.watch_step(now=T0 + board.IDLE_GUESS_S + 60, alive=lambda p: True)
    assert jobs == []                                           # a long build is no idle
    jobs, _ = board.watch_step(now=T0 + board.INFLIGHT_MAX_S + 60, alive=lambda p: True)
    assert [j["state"] for j in jobs] == ["leerlauf"]           # but not forever


def test_watcher_removes_a_dead_session():
    _relay()
    board.handle(ev("UserPromptSubmit", prompt="los"), now=T0)
    assert _me()["pid"] == 4242
    jobs, live = board.watch_step(now=T0 + 10, alive=lambda p: False)
    assert jobs == [] and live == 1                             # too fresh: maybe a pid race
    jobs, live = board.watch_step(now=T0 + board.GONE_QUIET_S + 1, alive=lambda p: False)
    assert [j["op"] for j in jobs] == ["gone"] and live == 0
    assert board.watch_step(now=T0 + 500, alive=lambda p: False) == ([], 0)   # once


def test_session_end_hook_removes_the_board(monkeypatch):
    _relay()
    board.handle(ev("UserPromptSubmit", prompt="los"), now=T0)
    board.handle(ev("SessionEnd", reason="prompt_input_exit"), now=T0 + 5)
    assert POSTS[-1]["op"] == "gone" and _me()["gone"]
    cleared, sent = [], []
    monkeypatch.setattr(board, "clear_board", lambda rel, frm: cleared.append(frm) or True)
    monkeypatch.setattr(board, "post_state", lambda rel, frm, state: sent.append(state) or True)
    assert board.run_job(POSTS[-1]) is True
    assert cleared == [POSTS[-1]["from"]] and sent == ["leerlauf"]
    # the session comes back (resume): alive again
    board.handle(ev("UserPromptSubmit", prompt="weiter"), now=T0 + 60)
    assert "gone" not in _me()


def test_session_end_hook_installed():
    assert activity.HOOK_COMMANDS["SessionEnd"] == "voicehook-agent-hook session-end || true"
    assert activity.hook_entry("SessionEnd") == {"hooks": [
        {"type": "command", "command": "voicehook-agent-hook session-end || true", "timeout": 3}]}


def test_session_pid_skips_shells():
    procs = {100: ("sh", 50), 50: ("claude", 1)}
    monkey_ppid = 100
    import voicehook_agent.board as b
    orig = b.os.getppid
    b.os.getppid = lambda: monkey_ppid
    try:
        assert REAL_SESSION_PID(env={}, proc=lambda p: procs.get(p, ("", 0))) == 50
        assert REAL_SESSION_PID(env={"CLAUDE_PID": "777"}, proc=lambda p: ("", 0)) == 777
        assert REAL_SESSION_PID(env={}, proc=lambda p: ("", 0)) == 0
    finally:
        b.os.getppid = orig


def test_join_merges_state_and_board_for_local_helpers():
    """The join keeps board + state per helper like the server (cli._serve_relay_request)."""
    import asyncio

    from voicehook_agent import cli

    class Room:
        def __init__(self):
            self.sent = []

            class LP:
                async def publish_data(lp, data, reliable, topic):
                    self.sent.append((topic, json.loads(data)))
            self.local_participant = LP()

    class Ctl:
        def __init__(self, room):
            self.room = room
            self.relay_helpers: dict = {}
            self.relay_senders: dict = {}

        async def wait_room(self, t):
            return self.room

    room = Room()
    ctl = Ctl(room)

    async def run(dir_):
        for op, body in (("status", {"doing": "PR offen"}), ("state", {"state": "leerlauf"}),
                         ("status", {"doing": "Tests"})):
            await cli._serve_relay_request(ctl, dir_, {"rid": "0123456789abcdef", "from": "SE",
                                                       "op": op, "body": body})
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as td:
        asyncio.run(run(Path(td)))
    payloads = [p for t, p in room.sent if t == "operator.status"]
    assert payloads[0]["doing"] == "PR offen" and "state" not in payloads[0]
    assert payloads[1]["doing"] == "PR offen" and payloads[1]["state"] == "leerlauf"
    assert payloads[2]["doing"] == "Tests" and payloads[2]["state"] == "leerlauf"
