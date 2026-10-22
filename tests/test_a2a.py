"""0.24.0 agent-to-agent tasks (Feature #138, Phase 1 local): `voicehook-agent send --to
<label> "<task>"` and its counterpart on the relay-next / listen path.

Pure logic (events, target resolution, board reconstruction from the inbox, reminders) is
tested directly; the CLI send / relay-next / task_wake wiring is tested end-to-end against the
real flock inbox files and the real control socket, exactly like the decide relay tests."""
from __future__ import annotations

import asyncio
import json
import os
import stat

import pytest

from voicehook_agent import a2a, relay, spool
from voicehook_agent import multisession as ms
from voicehook_agent import relayops as ro
from voicehook_agent import session as vsession

cli = pytest.importorskip("voicehook_agent.cli",
                          reason="livekit/httpx not installed in this environment")


# --------------------------------------------------------------------------- #
# fixtures / helpers
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    for k in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_PID", "VOICEHOOK_OWNER_PID", ms.LABEL_ENV,
              ms.MACHINE_LABEL_ENV, ms.NO_RELAY_ENV, "VOICEHOOK_SESSION"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("VOICEHOOK_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))


def _register(labels, hashes=None):
    """Write relay-state.json so registry_labels() reports these session labels."""
    hashes = hashes or {}
    sessions = {}
    for i, lbl in enumerate(labels):
        skey = hashes.get(lbl, f"hash{i:02d}")
        sessions[skey] = {"label": lbl, "at": 9e18}  # far future: always "recent"
    from voicehook_agent import activity as vact
    vact._private_dir(vact.state_root())
    (vact.state_root() / ms.STATE_NAME).write_text(json.dumps({"sessions": sessions}),
                                                    encoding="utf-8")


def _cli(argv, capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(argv)
    out, err = capsys.readouterr()
    lines = [json.loads(x) for x in out.splitlines() if x.strip()]
    return e.value.code, lines, err


# --------------------------------------------------------------------------- #
# pure: events + validation
# --------------------------------------------------------------------------- #
def test_event_factories_shapes():
    ev = a2a.task_event("t0123456789ab", "Sender", "Target", "  do   the  thing ", now=10.0)
    assert ev == {"type": "task", "id": "t0123456789ab", "from": "Sender", "to": "Target",
                  "text": "do the thing", "ts": 10.0}
    ack = a2a.task_acked_event("t0123456789ab", "Sender", "Target", now=11.0)
    assert ack == {"type": "task_acked", "id": "t0123456789ab", "from": "Sender",
                   "to": "Target", "ts": 11.0}
    due = a2a.task_due_event(ev, 2, now=70.0)
    assert due == {"type": "task_due", "id": "t0123456789ab", "from": "Sender", "to": "Target",
                   "text": "do the thing", "reminder_count": 2, "ts": 70.0}


def test_new_id_and_valid_task():
    tid = a2a.new_id()
    assert a2a._ID_RE.match(tid)
    assert a2a.valid_task({"type": "task", "id": tid, "from": "A", "to": "B", "text": "x"})["id"] == tid
    assert a2a.valid_task({"type": "task", "id": "bad", "text": "x"}) is None      # bad id
    assert a2a.valid_task({"type": "task", "id": tid, "text": "  "}) is None        # empty text
    assert a2a.valid_task({"type": "decision", "id": tid, "text": "x"}) is None     # wrong type


def test_wake_types_are_additive_and_registered():
    assert a2a.WAKE_TYPES == ("task", "task_acked", "task_due")
    for t in a2a.WAKE_TYPES:
        assert t in spool.WAKE_TYPES                                  # listen / Stop hook wake
        assert ro._wanted({"type": t}) is True                        # relay-next delivers it
    # inert for an old relay-next filter: unknown non-task types still ignored
    assert ro._wanted({"type": "whatever"}) is False


# --------------------------------------------------------------------------- #
# pure: resolve a label to one local session (404 / 409 / 422, case, self)
# --------------------------------------------------------------------------- #
def test_resolve_target_exact_and_case():
    pool = ["Crown", "SE Refill", "Promo"]
    assert a2a.resolve_target("SE Refill", pool) == "SE Refill"
    assert a2a.resolve_target("crown", pool) == "Crown"            # case-insensitive
    assert a2a.resolve_target("CROWN", pool) == "Crown"


def test_resolve_target_404_409_422():
    with pytest.raises(a2a.TaskError) as e:
        a2a.resolve_target("Ghost", ["Crown", "Promo"])
    assert e.value.status == 404 and "target not in room" in e.value.detail
    with pytest.raises(a2a.TaskError) as e:
        a2a.resolve_target("dup", ["Dup", "DUP"])
    assert e.value.status == 409
    for bad in ("", "   ", "*"):
        with pytest.raises(a2a.TaskError) as e:
            a2a.resolve_target(bad, ["Crown"])
        assert e.value.status == 422                                 # empty or broadcast


def test_resolve_target_excludes_self_and_empty_pool():
    with pytest.raises(a2a.TaskError) as e:
        a2a.resolve_target("Me", ["Me"], me="Me")                   # only candidate is me
    assert e.value.status == 404 and "no other session" in e.value.detail


# --------------------------------------------------------------------------- #
# pure: board reconstruction from the inbox (durable, not RAM)
# --------------------------------------------------------------------------- #
def test_open_tasks_from_inbox_and_ack_cancels():
    t1, t2 = a2a.new_id(), a2a.new_id()
    events = [
        a2a.task_event(t1, "Me", "Crown", "deploy", now=100.0),
        a2a.task_event(t2, "Me", "Promo", "post", now=130.0),
        a2a.task_acked_event(t2, "Me", "Promo", now=135.0),           # t2 accepted -> closed
    ]
    open_ = a2a.open_tasks(events, now=160.0)
    assert [o["id"] for o in open_] == [t1]                           # only t1 still open
    assert open_[0]["to"] == "Crown" and open_[0]["queued_s"] == 60.0
    assert a2a.board_line(open_[0]) == "Auftrag offen bei Crown seit 01:00"


def test_open_tasks_newest_queued_first_and_reminder_count():
    t1, t2 = a2a.new_id(), a2a.new_id()
    events = [
        a2a.task_event(t1, "Me", "A", "x", now=100.0),
        a2a.task_event(t2, "Me", "B", "y", now=50.0),                 # older -> longer open
        a2a.task_due_event({"id": t2, "from": "Me", "to": "B", "text": "y"}, 2, now=120.0),
    ]
    open_ = a2a.open_tasks(events, now=200.0)
    assert [o["id"] for o in open_] == [t2, t1]                       # longest open first
    assert open_[0]["reminders"] == 2 and open_[0]["last_reminder_ts"] == 120.0


# --------------------------------------------------------------------------- #
# pure: reminders (60s due, 30s window, cap 3, no auto-cancel)
# --------------------------------------------------------------------------- #
def test_reminder_clock_due_window_and_cap():
    t = [1000.0]
    r = a2a.TaskReminder(clock=lambda: t[0])
    young = [{"id": "t000000000001", "to": "X", "text": "x", "queued_s": 30.0}]
    assert r.due(young) == []                                         # younger than 60 s
    due = [{"id": "t000000000001", "to": "X", "text": "x", "queued_s": 61.0}]
    first = r.due(due)
    assert len(first) == 1 and first[0]["reminder_count"] == 1
    assert r.due(due) == []                                           # inside the 30 s window
    for expected in (2, 3):
        t[0] += a2a.REMIND_EVERY_S + 1
        got = r.due(due)
        assert len(got) == 1 and got[0]["reminder_count"] == expected
    t[0] += a2a.REMIND_EVERY_S + 1
    assert r.due(due) == []                                          # capped at 3, stays open


def test_due_reminders_stateless_from_inbox():
    # fresh process: no RAM state, the decision comes straight from the inbox counts
    open0 = [{"id": "t000000000009", "to": "X", "text": "x", "queued_s": 61.0,
              "reminders": 0, "last_reminder_ts": 0.0}]
    due = a2a.due_reminders(open0, "Me", now=1000.0)
    assert len(due) == 1 and due[0]["reminder_count"] == 1
    # one already sent 5 s ago: inside the window -> nothing owed
    open1 = [{**open0[0], "reminders": 1, "last_reminder_ts": 995.0}]
    assert a2a.due_reminders(open1, "Me", now=1000.0) == []
    # three already sent: capped
    open3 = [{**open0[0], "reminders": 3, "last_reminder_ts": 0.0}]
    assert a2a.due_reminders(open3, "Me", now=1e9) == []


# --------------------------------------------------------------------------- #
# CLI: send writes the task into the target's inbox (local, durable, 0600)
# --------------------------------------------------------------------------- #
def test_send_local_writes_target_inbox(monkeypatch, capsys):
    monkeypatch.setenv(ms.LABEL_ENV, "Sender")
    _register(["Sender", "Crown"])
    rc, lines, _ = _cli(["send", "--to", "Crown", "Belege rausschicken"], capsys)
    assert rc == 0 and lines[0]["ok"] and lines[0]["to"] == "Crown"
    assert a2a._ID_RE.match(lines[0]["id"]) and lines[0]["woke"] is False  # no live join
    inbox = ro.inbox_path("Crown")
    assert stat.S_IMODE(os.stat(inbox).st_mode) == 0o600               # private
    got = ro.read_inbox(inbox)
    assert [e["type"] for e in got] == ["task"]
    assert got[0]["from"] == "Sender" and got[0]["text"] == "Belege rausschicken"
    # the sender keeps a durable outbox record for its own "offen seit" board
    out = ro.read_jsonl(ro.outbox_path("Sender"))
    assert [e["id"] for e in out] == [lines[0]["id"]]


def test_send_unknown_target_is_clear_error(monkeypatch, capsys):
    monkeypatch.setenv(ms.LABEL_ENV, "Sender")
    _register(["Sender", "Crown"])
    rc, lines, _ = _cli(["send", "--to", "Ghost", "x"], capsys)
    assert rc == 1 and lines[0]["ok"] is False and lines[0]["status"] == 404
    assert "target not in room" in lines[0]["error"]
    assert ro.read_inbox(ro.inbox_path("Ghost")) == []                 # nothing written


def test_send_no_broadcast_in_phase1(monkeypatch, capsys):
    monkeypatch.setenv(ms.LABEL_ENV, "Sender")
    _register(["Sender", "Crown", "Promo"])
    rc, lines, _ = _cli(["send", "--to", "*", "x"], capsys)
    assert rc == 1 and lines[0]["status"] == 422 and "broadcast" in lines[0]["error"]


# --------------------------------------------------------------------------- #
# CLI: the target consumes the task via relay-next and acks back to the sender
# --------------------------------------------------------------------------- #
def _remote_relay(monkeypatch, label):
    """A minimal relay.json so client_relay routes this session as a helper (remote shape, but
    relay-next for tasks reads the local inbox first, so the fake url is never called)."""
    ms.write_relay("http://127.0.0.1:9/api/activity/relay", "fake.token.test", "room-a", 3600,
                   owner_pid=os.getpid(), owner_session=ms.session_hash("JOINER"))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "target-1")
    monkeypatch.setenv(ms.LABEL_ENV, label)


def test_relay_next_delivers_task_and_acks_back(monkeypatch, capsys):
    # sender drops a task into Crown's inbox
    monkeypatch.setenv(ms.LABEL_ENV, "Sender")
    _register(["Sender", "Crown"])
    rc, lines, _ = _cli(["send", "--to", "Crown", "mach X"], capsys)
    assert rc == 0
    tid = lines[0]["id"]
    # Crown consumes it via relay-next: the task event comes out, and an ack goes to Sender
    _remote_relay(monkeypatch, "Crown")
    rc, lines, _ = _cli(["relay-next", "--timeout", "0"], capsys)
    assert rc == 0
    task_lines = [ln for ln in lines if ln.get("type") == "task"]
    assert task_lines and task_lines[0]["id"] == tid and task_lines[0]["text"] == "mach X"
    # Crown's inbox is now empty (consumed), Sender's inbox has the ack
    assert ro.read_inbox(ro.inbox_path("Crown")) == []
    back = ro.read_inbox(ro.inbox_path("Sender"))
    assert [e["type"] for e in back] == ["task_acked"]
    assert back[0]["id"] == tid and back[0]["from"] == "Sender" and back[0]["to"] == "Crown"


def test_tasks_shows_open_then_accepted(monkeypatch, capsys):
    monkeypatch.setenv(ms.LABEL_ENV, "Sender")
    _register(["Sender", "Crown"])
    tid = _cli(["send", "--to", "Crown", "mach X"], capsys)[1][0]["id"]
    # sender's board: one open task at Crown
    rc, lines, _ = _cli(["tasks"], capsys)
    assert rc == 0 and [o["id"] for o in lines[0]["open"]] == [tid]
    assert lines[0]["board"][0].startswith("Auftrag offen bei Crown seit")
    # Crown accepts -> an ack lands in Sender's inbox -> board shows nothing open
    ro.append_jsonl(ro.inbox_path("Sender"), a2a.task_acked_event(tid, "Sender", "Crown"))
    rc, lines, _ = _cli(["tasks"], capsys)
    assert rc == 0 and lines[0]["open"] == []


def test_tasks_to_me_lists_incoming(monkeypatch, capsys):
    monkeypatch.setenv(ms.LABEL_ENV, "Crown")
    ro.append_jsonl(ro.inbox_path("Crown"), a2a.task_event(a2a.new_id(), "Sender", "Crown", "do it"))
    rc, lines, _ = _cli(["tasks", "--to-me"], capsys)
    assert rc == 0 and len(lines[0]["incoming"]) == 1
    assert lines[0]["incoming"][0]["from"] == "Sender" and lines[0]["incoming"][0]["text"] == "do it"


def test_tasks_emits_due_reminder_into_own_inbox(monkeypatch, capsys):
    monkeypatch.setenv(ms.LABEL_ENV, "Sender")
    _register(["Sender", "Crown"])
    old = a2a.task_event(a2a.new_id(), "Sender", "Crown", "alt", now=1.0)  # ancient -> way past due
    ro.append_jsonl(ro.outbox_path("Sender"), old)                          # a task we sent
    rc, lines, _ = _cli(["tasks"], capsys)
    assert rc == 0 and lines[0]["due"] == 1
    back = ro.read_inbox(ro.inbox_path("Sender"))
    due = [e for e in back if e["type"] == "task_due"]
    assert due and due[0]["id"] == old["id"] and due[0]["reminder_count"] == 1


# --------------------------------------------------------------------------- #
# CLI join side: task_wake drains the inbox into the live join's spool (instant wake)
# --------------------------------------------------------------------------- #
def _join_ctl(monkeypatch):
    monkeypatch.setenv(ms.MACHINE_LABEL_ENV, "crown")
    d = vsession.session_dir("room-a", "claude-x")
    d.mkdir(parents=True)
    vsession.write_info(d, {"pid": os.getpid(), "room": "room-a",
                            "owner_session": ms.session_hash("JOINER")})
    ctl = cli._Control("room-a", "claude-x", relay.SayTracker(), relay.EchoSuppressor(False), 0.0)
    ctl.activity_path = d / "activity.log"
    return d, ctl


def test_task_wake_drains_inbox_into_spool_and_acks(monkeypatch):
    _d, ctl = _join_ctl(monkeypatch)
    # a task waits in the joined session's own inbox (label "Joined")
    tid = a2a.new_id()
    ro.append_jsonl(ro.inbox_path("Joined"), a2a.task_event(tid, "Sender", "Joined", "go"))

    async def run():
        reply = await cli._control_handler(ctl, {"cmd": "task_wake", "label": "Joined"})
        # the task is now a front event in the spool: next hands it out
        ev = await ctl.events.get(0)
        return reply, ev

    reply, ev = asyncio.run(run())
    assert reply["ok"] and reply["queued"] == 1
    assert ev["type"] == "task" and ev["id"] == tid and ev["text"] == "go"
    assert ro.read_inbox(ro.inbox_path("Joined")) == []               # consumed from the inbox
    back = ro.read_inbox(ro.inbox_path("Sender"))                      # acked back to the sender
    assert [e["type"] for e in back] == ["task_acked"] and back[0]["id"] == tid


def test_send_wakes_live_join_over_socket(monkeypatch, capsys):
    """End-to-end instant wake: Target runs a real join; send reaches its control socket, the
    task lands as a front event, and send reports woke=True."""
    monkeypatch.setenv(ms.LABEL_ENV, "Sender")
    _register(["Sender", "Target"], hashes={"Target": ms.session_hash("target-sess")})
    d = vsession.session_dir("room-a", "claude-t")
    d.mkdir(parents=True)
    vsession.write_info(d, {"pid": os.getpid(), "room": "room-a",
                            "owner_session": ms.session_hash("target-sess")})
    ctl = cli._Control("room-a", "claude-t", relay.SayTracker(), relay.EchoSuppressor(False), 0.0)
    ctl.activity_path = d / "activity.log"

    async def run():
        server = await asyncio.start_unix_server(
            lambda r, w: _serve_one(ctl, r, w), path=str(d / vsession.SOCKET_NAME))
        try:
            rc, lines, _ = await asyncio.to_thread(_cli, ["send", "--to", "Target", "jetzt"], capsys)
            await asyncio.sleep(0.05)
            ev = await ctl.events.get(0)
            return rc, lines, ev
        finally:
            server.close()
            await server.wait_closed()

    rc, lines, ev = asyncio.run(run())
    assert rc == 0 and lines[0]["woke"] is True
    assert ev["type"] == "task" and ev["text"] == "jetzt"


async def _serve_one(ctl, reader, writer):
    """Minimal control-socket server: one request line -> _control_handler -> one reply line."""
    try:
        while True:
            line = await reader.readline()
            if not line:
                break
            req = json.loads(line.decode("utf-8"))
            reply = await cli._control_handler(ctl, req)
            writer.write((json.dumps(reply) + "\n").encode("utf-8"))
            await writer.drain()
    except (asyncio.IncompleteReadError, ConnectionResetError, ValueError):
        pass
    finally:
        try:
            writer.close()
        except OSError:
            pass
