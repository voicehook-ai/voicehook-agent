"""Helper liveness: operator sees stale helpers in the board listing.

feat/helper-liveness-cli: server emits operator.helper_stale / operator.helper_fresh on
LiveKit; the CLI ledger tracks per-helper liveness state and injects it into board entries
so the operator can see stale helpers at a glance."""
from __future__ import annotations

import http.server
import json
import os
import threading
from urllib.parse import parse_qs, urlsplit

import pytest

from voicehook_agent import boardlist, cli
from voicehook_agent import multisession as ms

TOKEN = "fake.relay.test.only"  # fake, low entropy (gitleaks)


# --------------------------------------------------------------------------- #
# helpers / fixtures
# --------------------------------------------------------------------------- #

class _FakeServer:
    def __init__(self):
        self.calls: list[dict] = []
        self.replies: dict[str, list[tuple[int, dict]]] = {}

    def reply(self, name, status, body):
        self.replies.setdefault(name, []).append((status, body))


@pytest.fixture
def server():
    fake = _FakeServer()

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def _serve(self):
            u = urlsplit(self.path)
            n = int(self.headers.get("content-length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}") if n else None
            name = u.path.rsplit("/", 1)[-1]
            fake.calls.append({"method": self.command, "path": u.path,
                               "query": parse_qs(u.query), "body": body,
                               "auth": self.headers.get("authorization")})
            q = fake.replies.get(name) or [(200, {"ok": True} if name != "next"
                                            else {"events": []})]
            status, out = q.pop(0) if len(q) > 1 else q[0]
            data = json.dumps(out).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = _serve

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    fake.url = f"http://127.0.0.1:{srv.server_address[1]}/api/activity/relay"
    try:
        yield fake
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    for k in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_PID", "VOICEHOOK_OWNER_PID",
              ms.LABEL_ENV, ms.MACHINE_LABEL_ENV, ms.NO_RELAY_ENV, "VOICEHOOK_SESSION"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("VOICEHOOK_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))


def _remote(fake, owner="JOINER"):
    return ms.write_relay(fake.url, TOKEN, "room-a", 3600, owner_pid=os.getpid(),
                          owner_session=ms.session_hash(owner))


def _helper(monkeypatch, sid="other-1", label="SE Refill"):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", sid)
    if label:
        monkeypatch.setenv(ms.LABEL_ENV, label)


def _cli(argv, capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(argv)
    out, err = capsys.readouterr()
    lines = [json.loads(x) for x in out.splitlines() if x.strip()]
    return e.value.code, lines, err


# --------------------------------------------------------------------------- #
# 1. stale event sets liveness to stale
# --------------------------------------------------------------------------- #
def test_helper_stale_event_sets_liveness_stale():
    lg = boardlist.Ledger()
    lg.board("server", {"sub": "crown: Scout", "doing": "Tests", "state": "arbeitet"})
    lg.liveness_update("crown: Scout", "stale", "no_poll")
    out = lg.listing()
    boards = out["boards"]
    scout = next(b for b in boards if b["from"] == "crown: Scout")
    assert scout["liveness"] == "stale"
    assert scout["liveness_grund"] == "no_poll"


# --------------------------------------------------------------------------- #
# 2. fresh event clears liveness (no liveness field after alive)
# --------------------------------------------------------------------------- #
def test_helper_fresh_event_clears_liveness():
    lg = boardlist.Ledger()
    lg.board("server", {"sub": "crown: Scout", "doing": "Tests"})
    lg.liveness_update("crown: Scout", "stale", "no_poll")
    # sanity: stale is set
    scout_before = next(b for b in lg.listing()["boards"] if b["from"] == "crown: Scout")
    assert scout_before.get("liveness") == "stale"
    # now clear it
    lg.liveness_update("crown: Scout", "alive", None)
    scout_after = next(b for b in lg.listing()["boards"] if b["from"] == "crown: Scout")
    assert "liveness" not in scout_after
    assert "liveness_grund" not in scout_after


# --------------------------------------------------------------------------- #
# 3. listing shows stale helper; alive helper has no liveness field
# --------------------------------------------------------------------------- #
def test_board_listing_shows_stale_helper():
    lg = boardlist.Ledger()
    lg.board("server", {"sub": "crown: Scout", "doing": "Tests"})
    lg.board("server", {"sub": "mac: Indexer", "doing": "Indexing"})
    lg.liveness_update("crown: Scout", "stale", "no_ack")
    out = lg.listing()
    scout = next(b for b in out["boards"] if b["from"] == "crown: Scout")
    indexer = next(b for b in out["boards"] if b["from"] == "mac: Indexer")
    # stale helper: liveness fields injected
    assert scout["liveness"] == "stale"
    assert scout["liveness_grund"] == "no_ack"
    # alive helper: no liveness fields
    assert "liveness" not in indexer
    assert "liveness_grund" not in indexer


# --------------------------------------------------------------------------- #
# 4. ack fires on relay-next consume
# --------------------------------------------------------------------------- #
def test_ack_fires_on_relay_next_consume(monkeypatch, server, capsys):
    _remote(server)
    _helper(monkeypatch)
    ev = {"type": "decision", "id": "d0a1b2c3d", "number": 3, "verdict": "yes", "by": "Oliver"}
    server.reply("next", 200, {"events": [ev]})
    rc, lines, err = _cli(["relay-next", "--timeout", "0"], capsys)
    assert rc == 0 and err == ""
    assert any(ln.get("type") == "decision" and ln.get("id") == "d0a1b2c3d" for ln in lines)
    acks = [c for c in server.calls if c["path"] == "/api/activity/relay/ack"]
    assert len(acks) == 1 and acks[0]["method"] == "POST"
    assert acks[0]["body"] == {"from": "SE Refill", "ids": ["d0a1b2c3d"]}
