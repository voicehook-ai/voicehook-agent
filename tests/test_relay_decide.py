"""0.16.0: helper sessions (Claude Code sessions of this machine that are not in the call)
put questions on the Decision Board and keep their own status board over relay.json.
Remote relay: HTTP (fake server here). Local relay: request/answer files in the join dir."""
from __future__ import annotations

import asyncio
import http.server
import json
import os
import stat
import threading
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from voicehook_agent import cli, relay
from voicehook_agent import multisession as ms
from voicehook_agent import relayops as ro
from voicehook_agent import session as vsession


@pytest.fixture(autouse=True)
def _plain_check_off(monkeypatch):
    """These tests are about the decide wiring with short sample questions; the 0.21.5
    Klartext rule has its own tests (tests/test_plaintext.py)."""
    monkeypatch.setenv("VOICEHOOK_PLAIN_CHECK", "0")


TOKEN = "fake.relay.test.only"  # fake, low entropy (gitleaks)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    for k in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_PID", "VOICEHOOK_OWNER_PID", ms.LABEL_ENV,
              ms.MACHINE_LABEL_ENV, ms.NO_RELAY_ENV, "VOICEHOOK_SESSION"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("VOICEHOOK_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))


# --------------------------------------------------------------------------- #
# fake relay server (Vertrag B)
# --------------------------------------------------------------------------- #
class _Fake:
    def __init__(self):
        self.calls: list[dict] = []
        self.replies: dict[str, list[tuple[int, dict]]] = {}

    def reply(self, name, status, body):
        self.replies.setdefault(name, []).append((status, body))


@pytest.fixture
def server():
    fake = _Fake()

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def _serve(self):
            u = urlsplit(self.path)
            n = int(self.headers.get("content-length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}") if n else None
            name = u.path.rsplit("/", 1)[-1]
            fake.calls.append({"method": self.command, "path": u.path, "query": parse_qs(u.query),
                               "raw_path": self.path, "auth": self.headers.get("authorization"),
                               "body": body})
            q = fake.replies.get(name) or [(200, {"ok": True} if name != "next" else {"events": []})]
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
# routing: helper vs own join
# --------------------------------------------------------------------------- #
def test_routing(monkeypatch, server, tmp_path):
    assert ro.client_relay(None) is None  # no relay.json
    _remote(server)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "JOINER")
    assert ro.client_relay(None) is None  # the joining session: own join
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "other-1")
    assert ro.client_relay(None)["token"] == TOKEN  # positive control: helper
    assert ro.client_relay("room-a/claude-x") is None  # explicit --session
    monkeypatch.setenv(ms.NO_RELAY_ENV, "1")
    assert ro.client_relay(None) is None
    monkeypatch.delenv(ms.NO_RELAY_ENV)
    # other-1 runs its own join (another call): its own join wins
    d = vsession.session_dir("room-b", "claude-y")
    d.mkdir(parents=True)
    vsession.write_info(d, {"pid": os.getpid(), "owner_session": ms.session_hash("other-1")})
    assert ro.client_relay(None) is None
    os.unlink(d / vsession.INFO_NAME)
    # outside Claude Code: relay only when no join socket answers
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
    monkeypatch.setattr(ro, "_socket_answers", lambda: True)
    assert ro.client_relay(None) is None
    monkeypatch.setattr(ro, "_socket_answers", lambda: False)
    assert ro.client_relay(None) is not None


def test_session_label_sources(monkeypatch, tmp_path):
    sid = "0f1e2d3c-aaaa-bbbb-cccc-1234567890ab"
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", sid)
    monkeypatch.chdir(tmp_path)
    fallback = ro.session_label()
    assert fallback == f"{tmp_path.name} {ms.label_suffix(sid)}"
    proj = tmp_path / "claude" / "projects" / "-home-x"
    proj.mkdir(parents=True)
    (proj / f"{sid}.jsonl").write_text(
        json.dumps({"type": "ai-title", "aiTitle": "Auto Name"}) + "\n"
        + json.dumps({"type": "custom-title", "customTitle": "Doku Sweep"}) + "\n")
    assert ro.session_label() == "Doku Sweep"  # same rule as the hook: custom-title first
    monkeypatch.setenv(ms.LABEL_ENV, "SE Refill")
    assert ro.session_label() == "SE Refill"
    assert ro.find_transcript("../../etc/passwd") == ""


def test_session_label_uses_label_the_hook_saw(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "other-1")
    st = {"sessions": {ms.session_hash("other-1"): {"label": "Hook Name"}}}
    ms.vact._private_dir(ms.vact.state_root())
    (ms.vact.state_root() / ms.STATE_NAME).write_text(json.dumps(st))
    assert ro.session_label() == "Hook Name"


def test_endpoint_from_relay_url():
    assert ro.endpoint({"url": "https://voicehook.ai/api/activity/relay"}, "decide") == \
        "https://voicehook.ai/api/activity/relay/decide"
    assert ro.endpoint({"url": "https://x.dev/pre/api/activity/relay/"}, "next") == \
        "https://x.dev/pre/api/activity/relay/next"
    assert ro.endpoint({"url": "https://x.dev/other"}, "status") == \
        "https://x.dev/api/activity/relay/status"


# --------------------------------------------------------------------------- #
# remote relay over HTTP
# --------------------------------------------------------------------------- #
def test_decide_via_remote_relay(monkeypatch, server, capsys):
    _remote(server)
    _helper(monkeypatch)
    server.reply("decide", 200, {"ok": True, "id": "d0a1b2c3d", "number": 3})
    rc, lines, err = _cli(["decide", "--question", "Deploy jetzt?", "--option", "Ja",
                           "--option", "Nein", "--recommend", "0"], capsys)
    assert rc == 0
    assert lines == [{"ok": True, "id": "d0a1b2c3d", "number": 3, "type": "decide", "via": "relay"}]
    c = server.calls[-1]
    assert c["method"] == "POST" and c["path"] == "/api/activity/relay/decide"
    assert c["auth"] == f"Bearer {TOKEN}" and TOKEN not in c["raw_path"]
    assert c["body"] == {"from": "SE Refill", "question": "Deploy jetzt?",
                         "options": ["Ja", "Nein"], "recommend": 0}
    # nothing on stderr but the 0.21.4 context/faq hint: no token, no question
    assert err.replace(relay.MANDATORY_HINT + "\n", "") == ""


def test_decide_update_and_delete_via_relay(monkeypatch, server, capsys):
    _remote(server)
    _helper(monkeypatch)
    server.reply("decide", 200, {"ok": True, "id": "d0a1b2c3d", "number": 3})
    _cli(["decide", "--id", "d0a1b2c3d", "--delete"], capsys)
    assert server.calls[-1]["body"] == {"from": "SE Refill", "id": "d0a1b2c3d", "delete": True}


@pytest.mark.parametrize("code", [422, 403, 409, 410, 503])
def test_decide_errors_pass_through(monkeypatch, server, capsys, code):
    _remote(server)
    _helper(monkeypatch)
    server.reply("decide", code, {"detail": "nope"})
    rc, lines, _ = _cli(["decide", "--question", "Q?"], capsys)
    assert rc == 1 and lines[0]["ok"] is False and lines[0]["status"] == code
    assert lines[0]["error"] == "nope" and lines[0]["via"] == "relay"
    assert ms.load_relay() is not None  # only 401 kills the token


def test_401_marks_token_dead(monkeypatch, server, capsys):
    _remote(server)
    _helper(monkeypatch)
    server.reply("status", 401, {"detail": "bad token"})
    rc, _, _ = _cli(["status", "--doing", "x"], capsys)
    assert rc == 1 and ms.load_relay() is None


def test_status_via_remote_relay(monkeypatch, server, capsys):
    _remote(server)
    _helper(monkeypatch)
    rc, lines, err = _cli(["status", "--doing", "Tests fixen", "--open", "PR", "--done", "Repro",
                           "--faq", "Wann?::gleich"], capsys)
    assert rc == 0
    meta = {"done": [{"faq": [{"q": "Wann?", "a": "gleich"}]}]}  # 0.21.4: per item, over the relay too
    assert lines == [{"ok": True, "type": "board", "via": "relay",
                      "board": {"doing": "Tests fixen", "open": ["PR"], "done": ["Repro"],
                                "meta": meta}}]
    assert server.calls[-1]["path"] == "/api/activity/relay/status"
    assert server.calls[-1]["body"] == {"from": "SE Refill", "doing": "Tests fixen",
                                        "open": ["PR"], "done": ["Repro"], "meta": meta}
    assert "faq" in err and "Wann" not in err  # warned, content never on stderr
    _cli(["status", ""], capsys)  # '' = finished: all empty removes the card
    assert server.calls[-1]["body"] == {"from": "SE Refill", "doing": "", "open": [], "done": []}


def test_status_without_args_never_goes_to_relay(monkeypatch, server, capsys):
    _remote(server)
    _helper(monkeypatch)
    rc, lines, _ = _cli(["status", "--wait", "0"], capsys)
    assert rc == 3 and lines[0]["type"] == "no-session" and server.calls == []


def test_own_join_and_opt_out_never_use_relay(monkeypatch, server, capsys):
    _remote(server)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "JOINER")
    rc, lines, _ = _cli(["decide", "--question", "Q?", "--wait", "0"], capsys)
    assert rc == 3 and lines[0]["type"] == "no-session"
    _helper(monkeypatch)
    monkeypatch.setenv(ms.NO_RELAY_ENV, "1")
    rc, lines, _ = _cli(["decide", "--question", "Q?", "--wait", "0"], capsys)
    assert rc == 3 and server.calls == []


def test_relay_next_prints_events(monkeypatch, server, capsys):
    _remote(server)
    _helper(monkeypatch)
    ev = {"type": "decision", "id": "d0a1b2c3d", "number": 3, "verdict": "option",
          "option": 0, "text": "Ja", "by": "Oliver"}
    server.reply("next", 200, {"events": [ev, dict(ev, id="d999", changed=True)]})
    server.reply("next", 200, {"events": []})
    rc, lines, err = _cli(["relay-next", "--timeout", "90"], capsys)
    assert rc == 0 and err == ""
    assert lines == [dict(ev, via="relay"), dict(ev, id="d999", changed=True, via="relay")]
    c = server.calls[0]
    assert c["method"] == "GET" and c["path"] == "/api/activity/relay/next"
    assert c["query"] == {"from": ["SE Refill"], "timeout": ["25"]}  # capped at 25
    rc, lines, _ = _cli(["relay-next", "--timeout", "0"], capsys)
    assert rc == 0 and lines == [{"ok": True, "type": "timeout", "via": "relay"}]


def test_relay_next_without_relay(capsys):
    rc, lines, _ = _cli(["relay-next", "--timeout", "0"], capsys)
    assert rc == 3 and lines[0]["type"] == "no-relay"


def test_decide_wait_keeps_other_answers(monkeypatch, server, capsys):
    _remote(server)
    _helper(monkeypatch)
    server.reply("decide", 200, {"ok": True, "id": "dNEW", "number": 4})
    other = {"type": "decision", "id": "dOLD", "number": 2, "verdict": "yes", "by": "O"}
    mine = {"type": "decision", "id": "dNEW", "number": 4, "verdict": "no", "by": "O"}
    server.reply("next", 200, {"events": [other]})
    server.reply("next", 200, {"events": [mine]})
    server.reply("next", 200, {"events": []})
    rc, lines, _ = _cli(["decide", "--question", "Q?", "--wait", "--timeout", "30"], capsys)
    assert rc == 0 and len(lines) == 1
    assert lines[0]["id"] == "dNEW" and lines[0]["decision"] == mine
    inbox = ro.inbox_path("SE Refill")
    assert stat.S_IMODE(os.stat(inbox).st_mode) == 0o600
    n = len(server.calls)
    rc, lines, _ = _cli(["relay-next", "--timeout", "0"], capsys)
    assert lines == [dict(other, via="relay")] and len(server.calls) == n  # from the inbox


def test_decide_wait_timeout(monkeypatch, server, capsys):
    _remote(server)
    _helper(monkeypatch)
    server.reply("decide", 200, {"ok": True, "id": "dNEW", "number": 4})
    rc, lines, _ = _cli(["decide", "--question", "Q?", "--wait", "--timeout", "0"], capsys)
    assert rc == 0 and lines[0]["decision"] is None and lines[0]["timeout"] is True


def test_network_error_only_class(monkeypatch, capsys):
    ms.write_relay("http://127.0.0.1:9/api/activity/relay", TOKEN, "room-a", 3600,
                   owner_pid=os.getpid(), owner_session=ms.session_hash("JOINER"))
    _helper(monkeypatch)
    rc, lines, err = _cli(["decide", "--question", "Geheimfrage?"], capsys)
    assert rc == 1 and lines[0]["status"] == 503 and "Error" in lines[0]["error"]
    assert TOKEN not in json.dumps(lines) and "Geheimfrage" not in err


# --------------------------------------------------------------------------- #
# local relay (WebRTC join): files in the join dir, Vertrag A on LiveKit
# --------------------------------------------------------------------------- #
class _Local:
    def __init__(self, replies):
        self.replies = list(replies)
        self.rpcs: list[dict] = []
        self.data: list[tuple[str, dict]] = []

    async def perform_rpc(self, *, destination_identity, method, payload, response_timeout):
        self.rpcs.append({"to": destination_identity, "method": method,
                          "payload": json.loads(payload)})
        return self.replies.pop(0)

    async def publish_data(self, data, reliable=True, topic=""):
        self.data.append((topic, json.loads(data)))


def _join(monkeypatch, replies=()):
    monkeypatch.setenv(ms.MACHINE_LABEL_ENV, "crown")
    d = vsession.session_dir("room-a", "claude-x")
    d.mkdir(parents=True)
    vsession.write_info(d, {"pid": os.getpid(), "room": "room-a",
                            "owner_session": ms.session_hash("JOINER")})
    ms.write_local_relay(d, "room-a", owner_session=ms.session_hash("JOINER"))
    ctl = cli._Control("room-a", "claude-x", relay.SayTracker(), relay.EchoSuppressor(False), 0.0)
    ctl.activity_path = d / "activity.log"
    local = _Local(replies)
    ctl.attach(SimpleNamespace(local_participant=local, remote_participants={
        "delta": SimpleNamespace(identity="delta-worker", kind=4)}))
    return d, ctl, local


def _with_join(ctl, d, fn):
    """Run the join's request loop while `fn` (the helper, blocking) runs in a thread."""
    async def go():
        loop_task = asyncio.create_task(cli._relay_request_loop(ctl, d))
        try:
            return await asyncio.to_thread(fn)
        finally:
            ctl.quit.set()
            await loop_task
    return asyncio.run(go())


def test_local_decide_end_to_end(monkeypatch, capsys):
    d, ctl, local = _join(monkeypatch, [json.dumps({"ok": True, "id": "d0a1b2c3d", "number": 1})])
    _helper(monkeypatch)
    rc, lines, err = _with_join(ctl, d, lambda: _cli(
        ["decide", "--question", "Merge?", "--option", "Ja", "--option", "Nein"], capsys))
    assert rc == 0 and err == relay.MANDATORY_HINT + "\n"   # 0.21.4: no context/faq: hint only
    assert lines == [{"ok": True, "id": "d0a1b2c3d", "number": 1, "type": "decide", "via": "relay"}]
    assert local.rpcs[0]["method"] == "vh.decide"
    assert local.rpcs[0]["payload"] == {"question": "Merge?", "options": ["Ja", "Nein"],
                                        "sender": "crown: SE Refill"}
    for name in (ro.REQ_NAME, ro.answer_path(d, "SE Refill").name):
        assert stat.S_IMODE(os.stat(d / name).st_mode) == 0o600
    # the decision event for that sender goes to the helper, not into our own `next`
    ev = {"type": "decision", "id": "d0a1b2c3d", "number": 1, "verdict": "option", "option": 0,
          "text": "Ja", "by": "Oliver", "sender": "crown: SE Refill"}
    ctl.events.arm()

    async def route():
        return cli._route_helper_decision(ctl, ev)
    assert asyncio.run(route()) is True
    # 0.18.1: our own `next` gets only a copy (helper_decision), never the `decision` itself
    hd = asyncio.run(ctl.events.get(0))
    assert {k: hd.get(k) for k in ("type", "from", "label", "id", "number", "question", "verdict", "option", "by")} == {
        "type": "helper_decision", "from": "SE Refill", "label": "crown", "id": "d0a1b2c3d", "number": 1,
        "question": "Merge?", "verdict": "option", "option": 0, "by": "Oliver"}
    assert "sender" not in hd and "SendMessage" in hd["hint"] and len(ctl.events) == 0
    assert cli._route_helper_decision(ctl, {"type": "decision", "id": "dOWN"}) is False  # own
    rc, lines, _ = _cli(["relay-next", "--timeout", "0"], capsys)
    assert rc == 0 and lines == [dict(ev, via="relay")]
    rc, lines, _ = _cli(["relay-next", "--timeout", "0"], capsys)
    assert lines == [{"ok": True, "type": "timeout", "via": "relay"}]  # consumed


def test_local_status_gets_own_sub(monkeypatch, capsys):
    d, ctl, local = _join(monkeypatch)
    _helper(monkeypatch)
    rc, lines, _ = _with_join(ctl, d, lambda: _cli(
        ["status", "--doing", "Doku", "--open", "README"], capsys))
    assert rc == 0 and lines[0]["via"] == "relay" and lines[0]["ok"] is True
    assert local.data == [("operator.status", {"doing": "Doku", "open": ["README"], "done": [],
                                               "sub": "crown: SE Refill"})]
    assert ctl.clock.board_now() is None  # the join's own board is untouched


def test_local_decide_wait(monkeypatch, capsys):
    d, ctl, local = _join(monkeypatch, [json.dumps({"ok": True, "id": "dNEW", "number": 2})])
    _helper(monkeypatch)
    ev = {"type": "decision", "id": "dNEW", "verdict": "yes", "by": "O", "sender": "crown: SE Refill"}

    def answer_later():
        import time
        deadline = time.monotonic() + 10
        while not local.rpcs and time.monotonic() < deadline:
            time.sleep(0.05)
        ctl.relay_senders.setdefault("crown: SE Refill", "SE Refill")
        cli._route_helper_decision(ctl, dict(ev, id="dOTHER"))
        cli._route_helper_decision(ctl, ev)

    threading.Thread(target=answer_later, daemon=True).start()
    rc, lines, _ = _with_join(ctl, d, lambda: _cli(
        ["decide", "--question", "Q?", "--wait", "--timeout", "10"], capsys))
    assert rc == 0 and lines[0]["decision"] == ev
    rc, lines, _ = _cli(["relay-next", "--timeout", "0"], capsys)
    assert lines == [dict(ev, id="dOTHER", via="relay")]  # the other one stayed queued


def test_local_invalid_body_gets_422(monkeypatch):
    d, ctl, _ = _join(monkeypatch)
    rid = "0123456789abcdef"
    ro.append_jsonl(d / ro.REQ_NAME, {"v": 1, "rid": rid, "from": "X", "op": "decide",
                                      "body": {"options": "nope"}})
    ro.append_jsonl(d / ro.REQ_NAME, {"v": 1, "rid": "bad", "from": "X", "op": "decide",
                                      "body": {"question": "q"}})
    ctl.quit.set()
    for obj in ro.drain_requests(d):
        asyncio.run(cli._serve_relay_request(ctl, d, obj))
    got = ro.take_jsonl(ro.answer_path(d, "X"), lambda o: True)
    assert got == [{"type": "result", "rid": rid, "ok": False, "status": 422,
                    "error": "invalid relay request"}]
    assert (d / ro.REQ_NAME).read_text() == ""  # drained


def test_local_refuses_symlink_open_file_and_other_pid(monkeypatch, tmp_path):
    d, _, _ = _join(monkeypatch)
    rel = ms.load_relay()
    target = tmp_path / "planted.jsonl"
    target.write_text(json.dumps({"rid": "0123456789abcdef", "from": "X", "op": "status",
                                  "body": {}}) + "\n")
    os.chmod(target, 0o600)
    os.symlink(target, d / ro.REQ_NAME)
    assert ro.drain_requests(d) == []  # never through a symlink
    with pytest.raises(ro.RelayError):
        ro.local_request(rel, "X", "status", {}, wait=0)
    os.unlink(d / ro.REQ_NAME)
    ans = ro.answer_path(d, "X")
    ans.write_text(json.dumps({"type": "decision", "id": "d1"}) + "\n")
    os.chmod(ans, 0o644)
    assert ro.take_jsonl(ans, lambda o: True) == []  # readable by others: ignored
    os.chmod(ans, 0o600)
    assert ro.take_jsonl(ans, lambda o: True) == [{"type": "decision", "id": "d1"}]  # control
    with pytest.raises(ro.RelayError):
        ro.local_dir(dict(rel, owner_pid=os.getpid() + 1))


def test_local_no_join_answer_times_out(monkeypatch):
    _join(monkeypatch)
    res = ro.local_request(ms.load_relay(), "X", "status", {"doing": "a"}, wait=0)
    assert res["ok"] is False and res["status"] == 504


def test_answer_file_is_capped(tmp_path):
    p = tmp_path / "a.jsonl"
    for i in range(50):
        ro.append_jsonl(p, {"i": i, "pad": "x" * 100}, cap=2000)
    got = ro.take_jsonl(p, lambda o: True)
    assert got[-1]["i"] == 49 and len(got) < 50


def test_join_cleans_relay_files(tmp_path):
    (tmp_path / ro.REQ_NAME).write_text("x")
    (tmp_path / "relay-ans-abc.jsonl").write_text("x")
    (tmp_path / "keep.txt").write_text("x")
    ro.clear_dir(tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["keep.txt"]


# ----- 0.21.9: stale drops of a helper's queued updates reach the helper ------------------
def test_relay_next_brings_stale_drop(monkeypatch, server, capsys):
    _remote(server)
    _helper(monkeypatch)
    stale = {"type": "revise", "stale": True, "items": [{"seq": 7, "text": "Refill bei 40 %"}],
             "threshold_s": 60}
    server.reply("next", 200, {"events": [stale]})
    server.reply("next", 200, {"events": []})
    rc, lines, _ = _cli(["relay-next", "--timeout", "0"], capsys)
    assert rc == 0 and lines[0]["stale"] is True and lines[0]["via"] == "relay"
    assert lines[0]["items"] == [{"seq": 7, "text": "Refill bei 40 %"}]
    assert lines[0]["hint"] == relay.STALE_HINT and "VERALTET" in lines[0]["text"]


def test_local_answer_file_stale_is_taken(tmp_path):
    p = tmp_path / "answers.jsonl"
    ro.append_jsonl(p, {"type": "revise", "stale": True, "items": ["alt"]})
    ro.append_jsonl(p, {"type": "revise", "text": "kein stale"})   # positive control: stays
    got = ro.take_jsonl(p, ro._wanted)
    assert [g.get("stale") for g in got] == [True]
    assert ro.take_jsonl(p, lambda o: True) == [{"type": "revise", "text": "kein stale"}]


# --------------------------------------------------------------------------- #
# 0.23.1: show --card of a helper session (POST <relay>/show)
# --------------------------------------------------------------------------- #
PNG1 = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)


def test_helper_show_image_goes_over_relay(monkeypatch, server, tmp_path, capsys):
    _remote(server)
    _helper(monkeypatch, label="Scout")
    f = tmp_path / "shot.png"
    f.write_bytes(PNG1)
    server.reply("show", 200, {"ok": True, "type": "card", "id": "a" * 16, "kind": "image",
                               "from": "Mac: Scout", "shown": 1})
    code, lines, _ = _cli(["show", "--card", "image", "--title", "Bild", "--file", str(f)], capsys)
    assert code == 0 and lines[-1]["ok"] and lines[-1]["via"] == "relay" and lines[-1]["shown"] == 1
    call = server.calls[-1]
    assert call["path"].endswith("/api/activity/relay/show") and call["auth"] == f"Bearer {TOKEN}"
    assert call["body"]["from"] == "Scout" and call["body"]["kind"] == "image" and call["body"]["title"] == "Bild"
    assert "?" not in call["raw_path"]   # token never in the URL


def test_helper_show_not_shown_is_an_error(monkeypatch, server, tmp_path, capsys):
    _remote(server)
    _helper(monkeypatch, label="Scout")
    f = tmp_path / "shot.png"
    f.write_bytes(PNG1)
    server.reply("show", 409, {"detail": "the image did not load in the call page"})
    code, lines, _ = _cli(["show", "--card", "image", "--file", str(f)], capsys)
    assert code == 1 and lines[-1]["ok"] is False and lines[-1]["status"] == 409


def test_helper_show_no_panels(monkeypatch, server, capsys):
    _remote(server)
    _helper(monkeypatch, label="Scout")
    code, lines, _ = _cli(["show", "--card", "text", "--text", "hallo", "--panel", "board"], capsys)
    assert code == 1 and lines[-1]["ok"] is False and "main agent" in lines[-1]["error"]
    assert not server.calls


def test_local_relay_card_only_text_kinds():
    ok = ro.parse_request({"rid": "a" * 16, "from": "Scout", "op": "card",
                           "body": {"kind": "link", "text": "https://voicehook.ai"}})
    assert ok[3] == {"kind": "link", "text": "https://voicehook.ai", "title": ""}
    for body in ({"kind": "image", "image": "x"}, {"kind": "link", "text": "javascript:x"},
                 {"kind": "text", "text": ""}, {"kind": "text", "text": "x" * 4001}):
        assert ro.parse_request({"rid": "a" * 16, "from": "Scout", "op": "card", "body": body})[3] is None
