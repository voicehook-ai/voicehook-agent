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


def test_join_without_recorded_owner_stays_usable(monkeypatch, foreign_join, capsys):
    """A join started outside any agent session (no owner_session) is nobody else's."""
    _info(owner_session="")
    _helper_session(monkeypatch)
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
    """split_owned: own hash, same agent process, unattributed, foreign."""
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
    assert {d.name for d in mine} == {"mine", "proc", "none"}
    assert [d.name for d in foreign] == ["other"]
    mine, foreign = vsession.split_owned(list(dirs.values()), "")
    assert len(mine) == 4 and foreign == []

