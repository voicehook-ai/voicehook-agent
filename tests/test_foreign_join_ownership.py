"""0.25.0 fail-closed: a session never uses the join of ANOTHER agent session by accident.

Live finding (crown, 22.10.2026): helper "voicehook dev-3" lost its relay (relay.dead); every
command fell back to resolve_socket(None), which took the ONLY live join of the machine (the
one of the session "Selfhost"), and posted there without a sender. The fix sits in ONE place:
resolve_socket(None) only picks joins this session owns (session.split_owned), so every
command (decide, status, activity, next, listen, say, ui, leave, show, MCP) follows it.

Ownership is robust: owner_session (same session id, e.g. `claude --resume`), or the same
agent process after /clear (a new session id; session.json owner_agent_pid). Deliberate
choices stay: --session, VOICEHOOK_NO_RELAY=1, no agent session at all (a human, a script).
"""
from __future__ import annotations

import http.server
import json
import os
import socket
import threading
from urllib.parse import urlsplit

import pytest

from voicehook_agent import cli, mcpserver
from voicehook_agent import multisession as ms
from voicehook_agent import session as vsession

TOKEN = "fake.relay.dead.test.only"  # fake, low entropy (gitleaks)


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    for k in ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "CLAUDE_PID", "VOICEHOOK_OWNER_PID",
              ms.LABEL_ENV, ms.MACHINE_LABEL_ENV, ms.NO_RELAY_ENV, "VOICEHOOK_SESSION"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("VOICEHOOK_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("VOICEHOOK_PLAIN_CHECK", "0")


@pytest.fixture
def relay_srv():
    calls: list[dict] = []

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _serve(self):
            n = int(self.headers.get("content-length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}") if n else None
            name = urlsplit(self.path).path.rsplit("/", 1)[-1]
            calls.append({"name": name, "body": body})
            out = ({"ok": True, "id": "d00000001", "number": 7} if name == "decide"
                   else {"events": []} if name == "next" else {"ok": True})
            data = json.dumps(out).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = _serve

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}/api/activity/relay", calls
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.fixture
def foreign_join():
    """The join of ANOTHER session (Selfhost) on this machine: session.json names its owner,
    the control socket records every request line and answers like a join would."""
    d = vsession.session_dir("orbit-prime-brisk-2V59", "selfhost-crown-0218")
    d.mkdir(parents=True)
    (d / vsession.INFO_NAME).write_text(json.dumps(
        {"pid": os.getpid(), "owner_session": ms.session_hash("selfhost")}), encoding="utf-8")
    got: list[dict] = []
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(vsession.socket_path(d)))
    srv.listen(8)
    stop = threading.Event()

    def loop():
        srv.settimeout(0.1)
        while not stop.is_set():
            try:
                c, _ = srv.accept()
            except OSError:
                continue
            with c:
                c.settimeout(2.0)
                buf = b""
                try:
                    while not buf.endswith(b"\n"):
                        chunk = c.recv(65536)
                        if not chunk:
                            break
                        buf += chunk
                except OSError:
                    continue
                if not buf.strip():
                    continue  # socket_alive probe
                got.append(json.loads(buf))
                c.sendall(b'{"ok": true, "id": "d00000179", "number": 179}\n')

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    try:
        yield got
    finally:
        stop.set()
        t.join(1.0)
        srv.close()


def _cli(argv, capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(argv)
    out, err = capsys.readouterr()
    return e.value.code, [json.loads(x) for x in out.splitlines() if x.strip()], err


SESS = "orbit-prime-brisk-2V59/selfhost-crown-0218"
JOIN_PID = os.getpid()  # a live pid that stands for the agent process that started the join


def _join_dir():
    return vsession.session_dir(*SESS.split("/"))


def _info(**kw):
    d = _join_dir()
    info = json.loads((d / vsession.INFO_NAME).read_text(encoding="utf-8"))
    info.update(kw)
    (d / vsession.INFO_NAME).write_text(json.dumps(info), encoding="utf-8")


def _agent_pid(monkeypatch, pid):
    from voicehook_agent import board
    monkeypatch.setattr(board, "agent_pid", lambda *a, **k: pid)


def _helper_session(monkeypatch, pid=4242):
    """A helper session (dev3): another session id and another agent process than the join."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "dev3")
    _agent_pid(monkeypatch, pid)


# --------------------------------------------------------------------------- #
# Punkt 3 (and the Grundsatz): no command of another session reaches the join
# --------------------------------------------------------------------------- #
OTHER_ARGVS = (
    ["say", "Hallo", "--wait", "0"],
    ["ui", "open", "board", "--wait", "0"],
    ["leave", "--wait", "0"],
    ["status", "--wait", "0"],
    ["show", "--preset", "check", "--wait", "0"],
    ["show", "--panel", "board", "--wait", "0"],
    ["decide", "--question", "A oder B?", "--option", "A", "--option", "B",
     "--context", "Test", "--wait", "0"],
    ["status", "--doing", "Tests laufen", "--wait", "0"],
    ["activity", "Tests gruen", "--wait", "0"],
    ["next", "--timeout", "0", "--wait", "0"],
)


@pytest.mark.parametrize("argv", OTHER_ARGVS, ids=lambda a: " ".join(a[:2]))
def test_other_session_never_uses_foreign_join(monkeypatch, foreign_join, capsys, argv):
    """No helper credential, no relay.json: before 0.25.0 every one of these went to the
    Selfhost join (leave even ended it). Now a clear error, nothing sent."""
    _helper_session(monkeypatch)
    rc, lines, _ = _cli(argv, capsys)
    assert foreign_join == [], f"{argv[0]} reached the join of another session"
    assert rc == 3, lines
    if lines:  # `ui` prints JSON too; every command answers with the same type
        assert lines[-1]["ok"] is False and lines[-1]["type"] == "foreign-join", lines
        assert "--session" in lines[-1]["error"] and SESS in lines[-1]["error"]


def test_status_waiting_other_session_never_uses_foreign_join(monkeypatch, foreign_join, capsys):
    _helper_session(monkeypatch)
    rc, lines, _ = _cli(["status", "--waiting", "Merge", "--question",
                         "Merge: PR freigeben, damit es live geht?", "--context", "CI gruen",
                         "--wait", "0"], capsys)
    assert foreign_join == [] and rc == 3
    assert lines[-1]["type"] == "foreign-join", lines


# --------------------------------------------------------------------------- #
# Punkt 2: listen never steals the next events of another session's join
# --------------------------------------------------------------------------- #
def test_listen_never_takes_foreign_next_events(monkeypatch, foreign_join, capsys):
    _helper_session(monkeypatch)
    rc, lines, _ = _cli(["listen", "--timeout", "1", "--wait", "0"], capsys)
    assert foreign_join == [], "listen leased/acked next events of another session's join"
    assert rc == 3 and lines[-1]["type"] == "foreign-join", lines


def test_listen_dead_helper_never_takes_foreign_next_events(monkeypatch, relay_srv,
                                                          foreign_join, capsys):
    url, calls = relay_srv
    _helper_session(monkeypatch)
    monkeypatch.setenv(ms.LABEL_ENV, "voicehook-dev-3")
    ms.write_helper("voicehook-dev-3", url, TOKEN, "room-a", 3600,
                    owner_session=ms.session_hash("dev3"))
    ms.mark_dead(TOKEN)
    rc, lines, _ = _cli(["listen", "--timeout", "1", "--wait", "0"], capsys)
    assert foreign_join == [] and calls == []
    assert rc == 3 and lines[-1]["ok"] is False, lines


def test_mcp_other_session_is_an_error_not_the_foreign_join(monkeypatch, foreign_join):
    _helper_session(monkeypatch)
    with pytest.raises(mcpserver.ToolError) as e:
        mcpserver._route()
    assert "--session" in str(e.value)
    assert foreign_join == []


# --------------------------------------------------------------------------- #
# Punkt 1: the owner of the join keeps it after /clear (new session id, same process)
# --------------------------------------------------------------------------- #
OWNER_ARGVS = (
    ["decide", "--question", "A oder B?", "--option", "A", "--option", "B",
     "--context", "Test", "--wait", "0"],
    ["status", "--doing", "Tests laufen", "--wait", "0"],
    ["activity", "Tests gruen", "--wait", "0"],
    ["next", "--timeout", "0", "--wait", "0"],
    ["say", "Hallo", "--wait", "0"],
)


@pytest.mark.parametrize("argv", OWNER_ARGVS, ids=lambda a: " ".join(a[:2]))
def test_owner_after_clear_keeps_own_join_despite_dead_helper_entry(monkeypatch, relay_srv,
                                                                   foreign_join, capsys, argv):
    """Selfhost runs the join (owner_session = hash of the OLD session id, owner_agent_pid =
    its agent process), then /clear: a new session id. A dead, unbound helper entry is in the
    state dir (e.g. from an earlier helper-import over ssh). Before: relay-dead on its own
    decide/board/activity/next. Now: its own join."""
    url, calls = relay_srv
    _info(owner_session=ms.session_hash("selfhost-old"), owner_agent_pid=JOIN_PID)
    ms.write_helper("old-helper", url, TOKEN, "room-a", 3600, owner_session="")
    ms.mark_dead(TOKEN)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "selfhost-after-clear")
    _agent_pid(monkeypatch, JOIN_PID)
    rc, lines, _ = _cli(argv, capsys)
    assert calls == []
    assert rc == 0, lines
    assert len(foreign_join) == 1 and lines[-1].get("type") not in ("relay-dead", "foreign-join")


def test_owner_after_clear_never_posts_as_helper_of_its_own_join(monkeypatch, foreign_join,
                                                                 capsys):
    """Same owner after /clear, with the relay.json its own WebRTC join wrote (owner_session =
    the old id). Before: the new id did not match, so its decide went over its own relay as if
    it were a helper (sender = its label). Now: straight to its own join."""
    d = _join_dir()
    _info(owner_session=ms.session_hash("selfhost-old"), owner_agent_pid=JOIN_PID)
    ms.write_local_relay(d, "orbit-prime-brisk-2V59", owner_pid=JOIN_PID,
                         owner_session=ms.session_hash("selfhost-old"))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "selfhost-after-clear")
    _agent_pid(monkeypatch, JOIN_PID)
    rc, lines, _ = _cli(OWNER_ARGVS[0], capsys)
    assert rc == 0 and [r["cmd"] for r in foreign_join] == ["decide"], lines
    assert lines[-1].get("via") != "relay"


def test_owner_after_resume_same_session_id_keeps_own_join(monkeypatch, foreign_join, capsys):
    """`claude --resume` keeps the session id, the agent process is a new one."""
    _info(owner_session=ms.session_hash("selfhost"), owner_agent_pid=99999999)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "selfhost")
    _agent_pid(monkeypatch, 4242)
    rc, _lines, _ = _cli(["say", "Hallo", "--wait", "0"], capsys)
    assert rc == 0 and [r["cmd"] for r in foreign_join] == ["say"]


def test_dead_agent_process_of_the_join_is_no_ownership(monkeypatch, foreign_join, capsys):
    """owner_agent_pid only counts while that process lives (pid reuse after a reboot)."""
    _info(owner_session=ms.session_hash("selfhost"), owner_agent_pid=99999999)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "dev3")
    _agent_pid(monkeypatch, 99999999)
    rc, _lines, _ = _cli(["say", "Hallo", "--wait", "0"], capsys)
    assert rc == 3 and foreign_join == []


# --------------------------------------------------------------------------- #
# positive controls: deliberate choices are unchanged
# --------------------------------------------------------------------------- #
def test_explicit_session_reaches_the_named_join(monkeypatch, foreign_join, capsys):
    _helper_session(monkeypatch)
    rc, _lines, _ = _cli(["say", "Hallo", "--session", SESS, "--wait", "0"], capsys)
    assert rc == 0 and [r["cmd"] for r in foreign_join] == ["say"]


def test_no_relay_env_drives_the_machine_join(monkeypatch, foreign_join, capsys):
    _helper_session(monkeypatch)
    monkeypatch.setenv(ms.NO_RELAY_ENV, "1")
    rc, _lines, _ = _cli(["say", "Hallo", "--wait", "0"], capsys)
    assert rc == 0 and [r["cmd"] for r in foreign_join] == ["say"]


def test_outside_an_agent_session_the_only_join_is_used(monkeypatch, foreign_join, capsys):
    rc, _lines, _ = _cli(["say", "Hallo", "--wait", "0"], capsys)
    assert rc == 0 and [r["cmd"] for r in foreign_join] == ["say"]


def test_live_helper_relay_still_goes_over_the_relay(monkeypatch, relay_srv, foreign_join,
                                                     capsys):
    url, calls = relay_srv
    _helper_session(monkeypatch)
    monkeypatch.setenv(ms.LABEL_ENV, "voicehook-dev-3")
    ms.write_helper("voicehook-dev-3", url, TOKEN, "room-a", 3600,
                    owner_session=ms.session_hash("dev3"))
    rc, lines, _ = _cli(OWNER_ARGVS[0], capsys)
    assert rc == 0 and lines[-1]["via"] == "relay" and foreign_join == []
    assert [c["name"] for c in calls] == ["decide"]


# --------------------------------------------------------------------------- #
# Punkt 4: helper_state follows _usable; show --card of a dead helper
# --------------------------------------------------------------------------- #
def _hand_copied_entry(url, *, ttl=3600):
    """A raw `helper-credential` answer put into helpers/ by hand: expires_in, no expires_at."""
    ms.write_helper("voicehook-dev-3", url, TOKEN, "room-a", ttl,
                    owner_session=ms.session_hash("dev3"))
    p = ms.helpers_dir() / "voicehook-dev-3.json"
    d = json.loads(p.read_text(encoding="utf-8"))
    d.pop("expires_at")
    d["expires_in"] = ttl
    p.write_text(json.dumps(d), encoding="utf-8")
    os.chmod(p, 0o600)
    return p, d


def test_helper_state_hand_copied_entry_live_and_dead(relay_srv):
    url, _ = relay_srv
    p, d = _hand_copied_entry(url)
    assert ms.helper_state(d, p) == "live"   # same rule as _usable: mtime + expires_in
    ms.mark_dead(TOKEN)
    assert ms.helper_state(d, p) == "dead"   # never "expired" for lack of expires_at


def test_helper_state_without_any_expiry_is_unusable(relay_srv):
    url, _ = relay_srv
    p, d = _hand_copied_entry(url)
    d.pop("expires_in")
    assert ms.helper_state(d, p) == "unusable"


def test_dead_hand_copied_entry_is_relay_dead(monkeypatch, relay_srv, foreign_join, capsys):
    url, calls = relay_srv
    _helper_session(monkeypatch)
    monkeypatch.setenv(ms.LABEL_ENV, "voicehook-dev-3")
    _hand_copied_entry(url)
    ms.mark_dead(TOKEN)
    rc, lines, _ = _cli(OWNER_ARGVS[0], capsys)
    assert foreign_join == [] and calls == []
    assert rc == 3 and lines[-1]["type"] == "relay-dead", lines


def test_show_card_dead_helper_is_relay_dead(monkeypatch, relay_srv, foreign_join, capsys):
    url, calls = relay_srv
    _helper_session(monkeypatch)
    monkeypatch.setenv(ms.LABEL_ENV, "voicehook-dev-3")
    ms.write_helper("voicehook-dev-3", url, TOKEN, "room-a", 3600,
                    owner_session=ms.session_hash("dev3"))
    ms.mark_dead(TOKEN)
    rc, lines, _ = _cli(["show", "--card", "text", "--title", "Stand", "--text", "Tests gruen",
                         "--wait", "0"], capsys)
    assert foreign_join == [] and calls == []
    assert rc == 3 and lines[-1]["type"] == "relay-dead", lines


def test_split_owned_unit():
    """split_owned: own hash, same agent process; unattributed and foreign are foreign."""
    root = vsession.sessions_root()
    dirs = {}
    for name, info in (("mine", {"owner_session": ms.session_hash("a")}),
                       ("proc", {"owner_session": ms.session_hash("old"),
                                 "owner_agent_pid": JOIN_PID}),
                       ("none", {}),
                       ("other", {"owner_session": ms.session_hash("b"),
                                  "owner_agent_pid": JOIN_PID + 1})):
        d = root / "room" / name
        d.mkdir(parents=True)
        (d / vsession.INFO_NAME).write_text(json.dumps(info), encoding="utf-8")
        dirs[name] = d
    mine, foreign = vsession.split_owned(list(dirs.values()), ms.session_hash("a"),
                                         agent_pid=lambda: JOIN_PID)
    assert {d.name for d in mine} == {"mine", "proc"}
    # Folge-PR Randfall 2: a join without owner is no own join of an agent session
    assert {d.name for d in foreign} == {"none", "other"}
    mine, foreign = vsession.split_owned(list(dirs.values()), "")
    assert len(mine) == 4 and foreign == []



# =========================================================================== #
# 0.25.0 Folge-PR: Randfaelle fremder Joins
# =========================================================================== #
OTHER_PROC = 4242  # a second agent process (a live pid is faked via _alive)


def _alive(monkeypatch, *pids):
    """These pids count as live agent processes (besides the real ones)."""
    from voicehook_agent import activity as vact
    real = vact._pid_alive
    monkeypatch.setattr(vact, "_pid_alive", lambda p: p in pids or real(p))


# --------------------------------------------------------------------------- #
# Randfall 1: same session id, another LIVE agent process is no owner
# --------------------------------------------------------------------------- #
def test_same_session_id_in_another_agent_process_is_not_the_owner(monkeypatch, foreign_join,
                                                                   capsys):
    """`claude --continue` in a second terminal (or an inherited CLAUDE_CODE_SESSION_ID, Codex
    started from a Claude shell): the same session id, the join's agent process still lives,
    this is another one. Before: owner_session matched, it drove the join."""
    _info(owner_session=ms.session_hash("selfhost"), owner_agent_pid=JOIN_PID)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "selfhost")
    _agent_pid(monkeypatch, OTHER_PROC)
    rc, lines, _ = _cli(["say", "Hallo", "--wait", "0"], capsys)
    assert foreign_join == [] and rc == 3, lines
    assert lines[-1]["type"] == "foreign-join", lines


@pytest.mark.parametrize("join_pid,me", [(JOIN_PID, JOIN_PID),   # the owner itself
                                         (0, OTHER_PROC),        # join without agent pid
                                         (99999999, OTHER_PROC)],  # resume: old process gone
                         ids=["same-process", "pid-unknown", "pid-dead"])
def test_same_session_id_owner_rules(monkeypatch, foreign_join, capsys, join_pid, me):
    _info(owner_session=ms.session_hash("selfhost"), owner_agent_pid=join_pid)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "selfhost")
    _agent_pid(monkeypatch, me)
    rc, lines, _ = _cli(["say", "Hallo", "--wait", "0"], capsys)
    assert rc == 0 and [r["cmd"] for r in foreign_join] == ["say"], lines


def test_owned_by_unit(monkeypatch):
    own = ms.session_hash("a")
    _alive(monkeypatch, 5001, 5002)
    me = 5001
    def ob(**info):
        return vsession.owned_by(info, own, agent_pid=lambda: me)
    assert ob(owner_session=own)                                    # no pid recorded
    assert ob(owner_session=own, owner_agent_pid=5001)              # same process
    assert ob(owner_session=own, owner_agent_pid=99999999)          # recorded process gone
    assert not ob(owner_session=own, owner_agent_pid=5002)          # other live process
    assert ob(owner_session=ms.session_hash("old"), owner_agent_pid=5001)  # after /clear
    assert not ob(owner_session=ms.session_hash("b"), owner_agent_pid=5002)
    assert not ob(owner_session="", owner_agent_pid=5001)           # no owner: never "owned"


def test_owned_by_pid_reuse_start_time(monkeypatch):
    """Optional hardening: a recorded start time of the agent process must still match, a
    reused pid (another process now) is no ownership."""
    own = ms.session_hash("a")
    monkeypatch.setattr(vsession, "proc_start", lambda pid: "111" if pid == JOIN_PID else "")
    after_clear = {"owner_session": ms.session_hash("old"), "owner_agent_pid": JOIN_PID}
    assert vsession.owned_by({**after_clear, "owner_agent_start": "111"}, own,
                             agent_pid=lambda: JOIN_PID)
    assert not vsession.owned_by({**after_clear, "owner_agent_start": "222"}, own,
                                 agent_pid=lambda: JOIN_PID)
    same_id = {"owner_session": own, "owner_agent_pid": JOIN_PID}
    # a reused pid counts as gone: the same session id owns again (resume rule)
    assert vsession.owned_by({**same_id, "owner_agent_start": "222"}, own,
                             agent_pid=lambda: OTHER_PROC)
    assert not vsession.owned_by({**same_id, "owner_agent_start": "111"}, own,
                                 agent_pid=lambda: OTHER_PROC)


def test_join_records_agent_start_time():
    assert vsession.proc_start(os.getpid()) != ""
    assert vsession.proc_start(99999999) == ""


# --------------------------------------------------------------------------- #
# Randfall 2: a join without owner needs --session inside an agent session
# --------------------------------------------------------------------------- #
def test_join_without_owner_needs_session_in_an_agent_session(monkeypatch, foreign_join,
                                                              capsys):
    """Before: a join without owner_session was usable by EVERY agent session, so a session
    without a helper entry posted into it without a sender."""
    _info(owner_session="")
    _helper_session(monkeypatch)
    rc, lines, _ = _cli(["say", "Hallo", "--wait", "0"], capsys)
    assert foreign_join == [] and rc == 3, lines
    assert lines[-1]["type"] == "foreign-join" and "--session" in lines[-1]["error"]


def test_join_without_owner_with_explicit_session(monkeypatch, foreign_join, capsys):
    _info(owner_session="")
    _helper_session(monkeypatch)
    rc, _lines, _ = _cli(["say", "Hallo", "--session", SESS, "--wait", "0"], capsys)
    assert rc == 0 and [r["cmd"] for r in foreign_join] == ["say"]


def test_join_without_owner_outside_an_agent_session(monkeypatch, foreign_join, capsys):
    """A human or a script (no session id, no agent process): unchanged."""
    _info(owner_session="")
    rc, _lines, _ = _cli(["say", "Hallo", "--wait", "0"], capsys)
    assert rc == 0 and [r["cmd"] for r in foreign_join] == ["say"]


def test_join_without_owner_helper_goes_over_the_relay(monkeypatch, relay_srv, foreign_join,
                                                       capsys):
    """Positive control: the join's relay.json carries a helper session (with sender)."""
    url, calls = relay_srv
    _info(owner_session="")
    ms.write_relay(url, TOKEN, "orbit-prime-brisk-2V59", 3600, owner_pid=os.getpid(),
                   owner_session="")
    _helper_session(monkeypatch)
    rc, lines, _ = _cli(OWNER_ARGVS[0], capsys)
    assert rc == 0 and lines[-1]["via"] == "relay" and foreign_join == [], lines
    assert [c["name"] for c in calls] == ["decide"]


# --------------------------------------------------------------------------- #
# Randfall 3: no session id (local MCP server): ownership by the agent process
# --------------------------------------------------------------------------- #
def test_mcp_without_session_id_never_takes_a_foreign_join(monkeypatch, foreign_join):
    """`voicehook-agent mcp` is started by the agent (Codex, Claude Code) and need not see
    CLAUDE_CODE_SESSION_ID/CODEX_THREAD_ID. Before: _caller()=='' switched the filter off and
    it drove the join of another session."""
    _info(owner_agent_pid=JOIN_PID)
    _agent_pid(monkeypatch, OTHER_PROC)
    with pytest.raises(mcpserver.ToolError) as e:
        mcpserver._route()
    assert "--session" in str(e.value) and foreign_join == []


def test_mcp_without_session_id_owns_by_agent_process(monkeypatch, foreign_join):
    _info(owner_agent_pid=JOIN_PID)
    _agent_pid(monkeypatch, JOIN_PID)
    assert mcpserver._route() == ("own", None)


def test_mcp_without_session_id_join_without_owner_is_closed(monkeypatch, foreign_join):
    _info(owner_session="", owner_agent_pid=0)
    _agent_pid(monkeypatch, OTHER_PROC)
    with pytest.raises(mcpserver.ToolError):
        mcpserver._route()
    assert foreign_join == []


def test_cli_without_session_id_in_an_agent_process(monkeypatch, foreign_join, capsys):
    _info(owner_agent_pid=JOIN_PID)
    _agent_pid(monkeypatch, OTHER_PROC)
    rc, _lines, _ = _cli(["say", "Hallo", "--wait", "0"], capsys)
    assert rc == 3 and foreign_join == []


# --------------------------------------------------------------------------- #
# Randfall 4: the hooks use the same ownership (owner after /clear keeps its join)
# --------------------------------------------------------------------------- #
def _tool_event(sid, **kw):
    return json.dumps({"hook_event_name": "PostToolUse", "session_id": sid, "cwd": "/tmp",
                       "tool_name": "Edit", "tool_use_id": "tu-" + sid,
                       "tool_input": {"file_path": "/tmp/x/notes.txt"},
                       "tool_response": {}, **kw})


def _log():
    from voicehook_agent import activity as vact
    p = _join_dir() / vact.ACTIVITY_NAME
    return p.read_text(encoding="utf-8") if p.exists() else ""


def _after_clear(monkeypatch, *, local_relay=True):
    """Selfhost runs the join (old session id, its agent process JOIN_PID), now /clear."""
    _info(owner_session=ms.session_hash("selfhost-old"), owner_agent_pid=JOIN_PID)
    if local_relay:
        ms.write_local_relay(_join_dir(), "orbit-prime-brisk-2V59", owner_pid=JOIN_PID,
                             owner_session=ms.session_hash("selfhost-old"))
    _agent_pid(monkeypatch, JOIN_PID)


def test_activity_hook_owner_after_clear_writes_own_join(monkeypatch, foreign_join):
    from voicehook_agent import activity as vact
    _after_clear(monkeypatch)
    assert vact._handle(_tool_event("selfhost-after-clear"), "post", None) is True
    assert _log().strip(), "the owner's tool line is missing in its own join"


def test_activity_hook_other_session_never_writes_the_join(monkeypatch, foreign_join):
    """Positive control: another session (and the same id in another live process)."""
    from voicehook_agent import activity as vact
    _after_clear(monkeypatch)
    _agent_pid(monkeypatch, OTHER_PROC)
    _alive(monkeypatch, OTHER_PROC)
    assert vact._handle(_tool_event("dev3"), "post", None) is False
    _info(owner_session=ms.session_hash("selfhost"))
    _info(owner_agent_pid=JOIN_PID)
    assert vact._handle(_tool_event("selfhost"), "post", None) is False
    assert _log() == ""


def test_relay_hook_owner_after_clear_is_silent(monkeypatch, foreign_join):
    """Before: the new id did not match relay.json's owner_session, so the owner's own turn
    went over its own relay as a 'Helfer' line."""
    _after_clear(monkeypatch)
    sent: list[str] = []
    ev = json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": "selfhost-after-clear",
                     "prompt": "Bitte die Tests reparieren", "cwd": "/tmp"})
    assert ms.handle_event(ev, post=sent.append) is None and sent == []


def test_relay_hook_helper_session_still_posts(monkeypatch, foreign_join):
    _after_clear(monkeypatch)
    _agent_pid(monkeypatch, OTHER_PROC)
    sent: list[str] = []
    ev = json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": "dev3",
                     "prompt": "Bitte die Tests reparieren", "cwd": "/tmp"})
    assert ms.handle_event(ev, post=sent.append) and len(sent) == 1


def test_relay_hook_same_id_other_process_posts_as_helper(monkeypatch, foreign_join):
    _info(owner_session=ms.session_hash("selfhost"), owner_agent_pid=JOIN_PID)
    ms.write_local_relay(_join_dir(), "orbit-prime-brisk-2V59", owner_pid=JOIN_PID,
                         owner_session=ms.session_hash("selfhost"))
    _agent_pid(monkeypatch, OTHER_PROC)
    sent: list[str] = []
    ev = json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": "selfhost",
                     "prompt": "Bitte die Tests reparieren", "cwd": "/tmp"})
    assert ms.handle_event(ev, post=sent.append) and len(sent) == 1


def test_notify_route_owner_after_clear_is_own(monkeypatch, foreign_join):
    from voicehook_agent import notify
    _after_clear(monkeypatch)
    kind, d, _rel = notify.route(ms.session_hash("selfhost-after-clear"))
    assert kind == "own" and d is not None and d.resolve() == _join_dir().resolve()


def test_notify_route_helper_is_relay(monkeypatch, foreign_join):
    from voicehook_agent import notify
    _after_clear(monkeypatch)
    _agent_pid(monkeypatch, OTHER_PROC)
    assert notify.route(ms.session_hash("dev3"))[0] == "relay"


def test_board_hook_owner_after_clear_is_its_own_board(monkeypatch, foreign_join):
    """The joining session has its own board (status_due, own `next`): the helper board
    hook stays silent for it, also after /clear."""
    from voicehook_agent import board
    _after_clear(monkeypatch)
    posted: list = []
    ev = json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": "selfhost-after-clear",
                     "prompt": "weiter", "cwd": "/tmp"})
    assert board.handle(ev, spawn=lambda *a, **k: posted.append(a),
                        post=lambda *a, **k: posted.append(a),
                        fetch=lambda *a, **k: None, watch=lambda *a, **k: None) is None
    assert posted == []
    st = board.vact.state_root() / board.STATE_NAME
    assert not st.exists() or ms.session_hash("selfhost-after-clear") not in st.read_text()


def test_codex_own_join_line_owner_after_clear(monkeypatch, foreign_join):
    from voicehook_agent import agents
    _after_clear(monkeypatch, local_relay=False)
    agents._own_join_line({"session_id": "selfhost-after-clear"}, "Turn fertig")
    assert "Turn fertig" in _log()


def test_codex_own_join_line_other_session(monkeypatch, foreign_join):
    from voicehook_agent import agents
    _after_clear(monkeypatch, local_relay=False)
    _agent_pid(monkeypatch, OTHER_PROC)
    agents._own_join_line({"session_id": "dev3"}, "Turn fertig")
    assert _log() == ""
