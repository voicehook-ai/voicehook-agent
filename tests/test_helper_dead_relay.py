"""0.25.0: a helper session whose relay died never posts through another session's join.

Live finding (crown, 22.10.2026): helper "voicehook dev-3" had its own helper entry; the
relay token was marked dead (relay.dead, operator rejoin). client_relay returned None, the
CLI fell back to resolve_socket(None) and took the ONLY live join of the machine, the one
of the session "Selfhost". Every decide from then on landed as Selfhost's own question
(board: agent=from=Selfhost, sender=None) and the answers went to Selfhost, not to dev-3.
Wanted: a clear error with a hint (fresh helper credential), nothing sent to the foreign join.
"""
from __future__ import annotations

import http.server
import json
import os
import socket
import threading
import time
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


def _helper_dev3(monkeypatch, url, *, dead=True, expires_in=3600):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "dev3")
    monkeypatch.setenv(ms.LABEL_ENV, "voicehook-dev-3")
    ms.write_helper("voicehook-dev-3", url, TOKEN, "room-a", expires_in,
                    owner_session=ms.session_hash("dev3"))
    if dead:
        ms.mark_dead(TOKEN)


HELPER_ARGVS = (
    ["decide", "--question", "A oder B?", "--option", "A", "--option", "B",
     "--context", "Test", "--wait", "0"],
    ["status", "--doing", "Tests laufen", "--wait", "0"],
    ["status", "--waiting", "Merge", "--question", "Merge: PR freigeben, damit es live geht?",
     "--context", "CI gruen", "--wait", "0"],
    ["activity", "Tests gruen", "--wait", "0"],
    ["next", "--timeout", "0", "--wait", "0"],
)


@pytest.mark.parametrize("argv", HELPER_ARGVS, ids=lambda a: " ".join(a[:2]))
def test_dead_relay_never_goes_to_foreign_join(monkeypatch, relay_srv, foreign_join, capsys, argv):
    url, calls = relay_srv
    _helper_dev3(monkeypatch, url)
    rc, lines, _ = _cli(argv, capsys)
    assert foreign_join == [], "helper posted through the join of another session"
    assert calls == []
    assert rc == 3 and lines[-1]["ok"] is False and lines[-1]["type"] == "relay-dead", lines
    assert "helper-import --as voicehook-dev-3" in lines[-1]["hint"]


def test_expired_helper_entry_is_the_same_clear_error(monkeypatch, relay_srv, foreign_join,
                                                      capsys):
    url, calls = relay_srv
    _helper_dev3(monkeypatch, url, dead=False, expires_in=60)
    real = time.time
    monkeypatch.setattr(ms.time, "time", lambda: real() + 3600)
    rc, lines, _ = _cli(HELPER_ARGVS[0], capsys)
    assert foreign_join == [] and calls == []
    assert rc == 3 and lines[-1]["type"] == "relay-expired", lines


def test_positive_control_live_relay_keeps_sender(monkeypatch, relay_srv, foreign_join, capsys):
    """Same setup, relay alive: over the relay with from = the helper label, never the join."""
    url, calls = relay_srv
    _helper_dev3(monkeypatch, url, dead=False)
    rc, lines, _ = _cli(HELPER_ARGVS[0], capsys)
    assert rc == 0 and lines[-1]["via"] == "relay", lines
    assert [c["name"] for c in calls] == ["decide"]
    assert calls[0]["body"]["from"] == "voicehook-dev-3"
    assert foreign_join == []


def test_positive_control_owner_of_the_join_still_uses_it(monkeypatch, relay_srv, foreign_join,
                                                         capsys):
    """The session that runs the join (Selfhost) keeps its own join, even next to a dead
    helper entry of another session in the same state dir."""
    url, _ = relay_srv
    ms.write_helper("voicehook-dev-3", url, TOKEN, "room-a", 3600,
                    owner_session=ms.session_hash("dev3"))
    ms.mark_dead(TOKEN)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "selfhost")
    monkeypatch.setenv(ms.LABEL_ENV, "voicehook-selfhost")
    rc, lines, _ = _cli(HELPER_ARGVS[0], capsys)
    assert rc == 0 and lines[-1]["number"] == 179, lines
    assert [r["cmd"] for r in foreign_join] == ["decide"]


def test_explicit_session_still_wins(monkeypatch, relay_srv, foreign_join, capsys):
    """--session is a deliberate choice of the join (documented rule 1), unchanged."""
    url, _ = relay_srv
    _helper_dev3(monkeypatch, url)
    rc, _lines, _ = _cli(HELPER_ARGVS[0][:-2] + [
        "--session", "orbit-prime-brisk-2V59/selfhost-crown-0218", "--wait", "0"], capsys)
    assert rc == 0 and [r["cmd"] for r in foreign_join] == ["decide"]


def test_mcp_route_dead_relay_is_an_error_not_the_foreign_join(monkeypatch, relay_srv,
                                                              foreign_join):
    url, _ = relay_srv
    _helper_dev3(monkeypatch, url)
    with pytest.raises(mcpserver.ToolError) as e:
        mcpserver._route()
    assert "helper-import --as voicehook-dev-3" in str(e.value)
    assert foreign_join == []
