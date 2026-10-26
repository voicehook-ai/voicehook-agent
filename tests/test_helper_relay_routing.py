"""0.22.2: one routing rule for every helper command, one helper credential per session.

Live findings with 0.22.1 (21.10.2026):
1. `status` went over the relay as a helper, `activity` (and `next`, `show`) did not: on
   crown "no running join found", on a Mac with several joins "several joins running".
2. `status --faq` warned "--faq is not sent over the relay", but the faq arrived in meta.
3. All helpers of a machine shared ONE ~/.voicehook/relay.json: the last `helper-import`
   named all of them (the strategy session's activity showed up as "Helfer Zapmail").
4. `status --faq` was capped at 6 per call: doing + 2 open + 1 done with 3 each lost pairs.
"""
from __future__ import annotations

import http.server
import io
import json
import os
import threading
from urllib.parse import urlsplit

import pytest

from voicehook_agent import cli, relay
from voicehook_agent import multisession as ms
from voicehook_agent import relayops as ro
from voicehook_agent import session as vsession

TOKEN = "fake.relay.test.only"  # fake, low entropy (gitleaks)


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    for k in ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "CLAUDE_PID", "VOICEHOOK_OWNER_PID",
              ms.LABEL_ENV, ms.MACHINE_LABEL_ENV, ms.NO_RELAY_ENV, "VOICEHOOK_SESSION"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("VOICEHOOK_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("VOICEHOOK_PLAIN_CHECK", "0")


class _Fake:
    def __init__(self):
        self.calls: list[dict] = []
        self.replies: dict[str, dict] = {}
        self.queue: dict[str, list] = {}  # name -> [(status, body)] answered first, in order


@pytest.fixture
def server():
    fake = _Fake()

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _serve(self):
            u = urlsplit(self.path)
            n = int(self.headers.get("content-length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}") if n else None
            name = u.path.rsplit("/", 1)[-1]
            fake.calls.append({"method": self.command, "path": u.path, "body": body,
                               "auth": self.headers.get("authorization")})
            q = fake.queue.get(name)
            status, out = q.pop(0) if q else (200, fake.replies.get(name) or (
                {"events": []} if name == "next" else {"ok": True}))
            data = json.dumps(out).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = _serve

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    fake.url = f"http://127.0.0.1:{srv.server_address[1]}/api/activity/relay"
    try:
        yield fake
    finally:
        srv.shutdown()
        srv.server_close()


def _cli(argv, capsys, stdin=None, monkeypatch=None):
    if stdin is not None:
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    with pytest.raises(SystemExit) as e:
        cli.main(argv)
    out, err = capsys.readouterr()
    return e.value.code, [json.loads(x) for x in out.splitlines() if x.strip()], err


def _cred(fake, label, token=TOKEN):
    return json.dumps({"ok": True, "type": "helper_credential", "token": token, "url": fake.url,
                       "expires_in": 3600, "label": label})


def _legacy(fake):
    """relay.json as `helper-import` of 0.22.1 wrote it: no owner pid, no owner session."""
    return ms.write_relay(fake.url, TOKEN, "room-a", 3600, owner_pid=0, owner_session="")


def _two_joins():
    """Two local joins answering on their control socket (the Mac case)."""
    import socket
    socks = []
    for ident in ("claude-a", "claude-b"):
        d = vsession.session_dir("room-x", ident)
        d.mkdir(parents=True)
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(str(vsession.socket_path(d)))
        s.listen(1)
        socks.append(s)
    return socks


# --------------------------------------------------------------------------- #
# 1. activity / next / show route like status and decide
# --------------------------------------------------------------------------- #
def test_activity_goes_over_relay_without_own_join(monkeypatch, server, capsys):
    """crown: legacy helper relay.json, no join of its own (master: no-session, exit 3)."""
    _legacy(server)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "helper-1")
    monkeypatch.setenv(ms.LABEL_ENV, "Strategie")
    rc, lines, err = _cli(["activity", "Tests gruen", "--context", "Suite lief",
                           "--faq", "Wie viele?::alle"], capsys)
    assert rc == 0 and lines[0]["via"] == "relay" and lines[0]["type"] == "activity"
    call = server.calls[-1]
    assert call["path"] == "/api/activity/relay" and call["auth"] == f"Bearer {TOKEN}"
    assert call["body"] == {"from": "Strategie", "text": "Tests gruen", "context": "Suite lief",
                            "faq": [{"q": "Wie viele?", "a": "alle"}]}
    assert "shared helper credential" in err  # legacy file recognized, with a warning


def test_activity_over_relay_with_several_local_joins(monkeypatch, server, capsys):
    """Mac: several joins run locally, this session owns none (master: several joins)."""
    socks = _two_joins()
    try:
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "helper-1")
        monkeypatch.setenv(ms.LABEL_ENV, "Strategie")
        ms.write_helper("Strategie", server.url, TOKEN, "room-a", 3600, owner_session="")
        rc, lines, err = _cli(["activity", "Tests gruen"], capsys)
        assert rc == 0 and lines[0]["via"] == "relay", (lines, err)
        rc, lines, _ = _cli(["decide", "--question", "Merge?", "--context", "PR gruen"], capsys)
        assert rc == 0 and lines[0]["via"] == "relay"  # decide stays on the relay
        assert [c["path"].rsplit("/", 1)[-1] for c in server.calls] == ["relay", "decide"]
    finally:
        for s in socks:
            s.close()


def test_next_as_helper_returns_relay_events(monkeypatch, server, capsys):
    _legacy(server)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "helper-1")
    monkeypatch.setenv(ms.LABEL_ENV, "Strategie")
    server.replies["next"] = {"events": [{"type": "decision", "id": "d0a1b2c3d", "number": 1,
                                          "verdict": "option", "option": 0, "text": "Ja"}]}
    rc, lines, _ = _cli(["next", "--timeout", "1"], capsys)
    assert rc == 0 and lines[0]["type"] == "decision" and lines[0]["via"] == "relay"
    # the decision came from GET /next; card 9: consuming it sends a delivery receipt afterwards
    assert [c["path"] for c in server.calls[-2:]] == [
        "/api/activity/relay/next", "/api/activity/relay/ack"]
    assert server.calls[-1]["body"] == {"from": "Strategie", "ids": ["d0a1b2c3d"]}


def test_show_card_as_helper_goes_over_relay(monkeypatch, server, capsys):
    """0.23.1: show --card of a helper is a helper card (POST relay/show, server #577)."""
    _legacy(server)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "helper-1")
    server.replies["show"] = {"ok": True, "id": "c1", "kind": "text", "from": "Helfer"}
    rc, lines, _ = _cli(["show", "--card", "text", "--title", "T", "--text", "x"], capsys)
    assert rc == 0 and lines[0]["type"] == "card" and lines[0]["via"] == "relay", lines
    assert server.calls[-1]["path"] == "/api/activity/relay/show"


def test_show_shape_as_helper_is_a_clear_error_not_no_session(monkeypatch, server, capsys):
    """Ring shapes have no relay route: a clear error, nothing sent."""
    _legacy(server)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "helper-1")
    rc, lines, _ = _cli(["show", "--preset", "arrow_up"], capsys)
    assert rc == 1 and lines[0]["type"] == "helper-unsupported" and lines[0]["via"] == "relay"
    assert server.calls == []


def test_positive_control_own_join_keeps_join_path(monkeypatch, server, capsys):
    """The main agent (owner of relay.json) never goes over the relay, even with entries."""
    ms.write_relay(server.url, TOKEN, "room-a", 3600, owner_pid=os.getpid(),
                   owner_session=ms.session_hash("JOINER"))
    ms.write_helper("Zapmail", server.url, "fake.other.test.only", "room-a", 3600,
                    owner_session=ms.session_hash("someone-else"))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "JOINER")
    for argv in (["activity", "x", "--wait", "0"], ["next", "--timeout", "0", "--wait", "0"],
                 ["decide", "--question", "Q?", "--wait", "0"]):
        rc, lines, _ = _cli(argv, capsys)
        assert rc == 3 and lines[0]["type"] == "no-session", argv
    assert server.calls == []
    # explicit --session always wins, also for a helper
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "helper-1")
    rc, lines, _ = _cli(["activity", "x", "--session", "room-a/claude-x", "--wait", "0"], capsys)
    assert rc == 3 and "hint" not in lines[0] and server.calls == []


def test_no_relay_no_join_gives_hint(capsys):
    rc, lines, _ = _cli(["activity", "x", "--wait", "0"], capsys)
    assert rc == 3 and lines[0]["type"] == "no-session" and "helper-import --as" in lines[0]["hint"]


# --------------------------------------------------------------------------- #
# 2. + 4. status --faq: per item in meta, no false warning, no cap at 6
# --------------------------------------------------------------------------- #
def _faq3(tag):
    out = []
    for i in range(3):
        out += ["--faq", f"{tag} Frage {i}?::Antwort {i}"]
    return out


def test_status_faq_three_per_item_all_arrive_without_warning(monkeypatch, server, capsys):
    ms.write_helper("Strategie", server.url, TOKEN, "room-a", 3600, owner_session="")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "helper-1")
    argv = (["status", "--doing", "Tests fixen", "--context", "c"] + _faq3("D")
            + ["--open", "PR", "--context", "c"] + _faq3("O1")
            + ["--open", "Deploy", "--context", "c"] + _faq3("O2")
            + ["--done", "Repro", "--context", "c"] + _faq3("X"))
    rc, lines, err = _cli(argv, capsys)
    assert rc == 0 and lines[0]["via"] == "relay"
    meta = server.calls[-1]["body"]["meta"]
    assert [len(m["faq"]) for m in [meta["doing"], *meta["open"], *meta["done"]]] == [3, 3, 3, 3]
    assert meta["open"][1]["faq"][2] == {"q": "O2 Frage 2?", "a": "Antwort 2"}
    assert "--faq" not in err and "dropped" not in err  # neither "not sent" nor "more than 6"


def test_status_faq_own_join_not_capped_at_six(monkeypatch, capsys):
    sent = []
    monkeypatch.setattr(vsession, "resolve_socket", lambda *a, **k: "sock")
    monkeypatch.setattr(vsession, "request", lambda sock, req, timeout=None: sent.append(req) or
                        {"ok": True, "type": "board"})
    rc, _, err = _cli(["status", "--doing", "A", "--context", "c", *_faq3("D"), "--open", "B",
                       "--context", "c", *_faq3("O"), "--done", "C", "--context", "c",
                       *_faq3("X")], capsys)
    m = sent[0]["board"]["meta"]
    assert rc == 0 and [len(x["faq"]) for x in [m["doing"], *m["open"], *m["done"]]] == [3, 3, 3]
    assert len(sent[0]["board"]["faq"]) == relay.FAQ_MAX  # flat legacy field stays capped
    assert "more than" not in err and "[hint]" not in err  # nothing dropped, nothing missing


def test_status_faq_warns_only_when_really_dropped(monkeypatch, server, capsys):
    ms.write_helper("Strategie", server.url, TOKEN, "room-a", 3600, owner_session="")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "helper-1")
    rc, _, err = _cli(["status", "--doing", "A", *_faq3("D"), "--faq", "Vierte?::zu viel"], capsys)
    assert rc == 0 and "more than 3" in err  # the 4th pair of one item: really dropped


# --------------------------------------------------------------------------- #
# 3. one helper credential per session
# --------------------------------------------------------------------------- #
def test_three_helpers_import_one_after_another_each_posts_as_itself(monkeypatch, server, capsys):
    """Master: the last import overwrote relay.json for all (everything 'Helfer Zapmail')."""
    names = {"s-armin": "Armin-Box", "s-strat": "Strategie", "s-zap": "Zapmail"}
    for sid, label in names.items():
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", sid)
        rc, lines, _ = _cli(["helper-import"], capsys, stdin=_cred(server, label, f"tok.{label}"),
                            monkeypatch=monkeypatch)
        assert rc == 0 and lines[0]["name"] == label and lines[0]["bound"] is True
        assert "tok." not in json.dumps(lines)  # never the token
    assert not ms.relay_path().exists()  # relay.json (the main join's) is never touched
    for sid, label in names.items():
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", sid)
        rc, lines, _ = _cli(["activity", f"Arbeit von {label}"], capsys)
        assert rc == 0 and server.calls[-1]["auth"] == f"Bearer tok.{label}"
        rc, _, _ = _cli(["status", "--doing", f"Board von {label}"], capsys)
        assert rc == 0 and server.calls[-1]["auth"] == f"Bearer tok.{label}"
    # the hook of each session takes its own entry too
    sent = []
    monkeypatch.setattr(ms, "_spawn_post", lambda line, sess="": sent.append(
        ms.load_relay(session=sess)["token"]))
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
    for sid in names:
        ev = {"session_id": sid, "hook_event_name": "Stop", "cwd": "/tmp/x"}
        assert ms.handle_event(json.dumps(ev)) is not None
    assert sent == [f"tok.{label}" for label in names.values()]


def test_helper_import_as_name_matched_by_session_label(monkeypatch, server, capsys):
    """Over ssh (no session): `--as NAME`, used by the session whose label is NAME."""
    for label in ("crown", "zapmail"):
        rc, lines, err = _cli(["helper-import", "--as", label], capsys,
                              stdin=_cred(server, "egal", f"tok.{label}"), monkeypatch=monkeypatch)
        assert rc == 0 and lines[0]["bound"] is False and "VOICEHOOK_SESSION_LABEL" in err
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "any")
    assert ro.client_relay(None) is None  # two unbound entries, no label: ambiguous, none
    monkeypatch.setenv(ms.LABEL_ENV, "zapmail")
    assert ro.client_relay(None)["token"] == "tok.zapmail"
    monkeypatch.setenv(ms.LABEL_ENV, "crown")
    assert ro.client_relay(None)["token"] == "tok.crown"


def test_single_unbound_entry_serves_like_before(monkeypatch, server, capsys):
    rc, _, _ = _cli(["helper-import", "--as", "crown"], capsys, stdin=_cred(server, "crown"),
                    monkeypatch=monkeypatch)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "later-session")
    assert rc == 0 and ro.client_relay(None)["token"] == TOKEN


def test_never_another_sessions_entry_with_two_entries(monkeypatch, server):
    """Two entries, different sessions, no label: ambiguous -> each session only finds its own."""
    ms.write_helper("Zapmail", server.url, "tok.zap", "room-a", 3600,
                    owner_session=ms.session_hash("s-zap"))
    ms.write_helper("Strat", server.url, "tok.strat", "room-a", 3600,
                    owner_session=ms.session_hash("s-strat"))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s-strat")
    assert ms.load_helper()["token"] == "tok.strat"
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s-zap")
    assert ms.load_helper()["token"] == "tok.zap"
    # A third, unrelated session sees None (multiple entries, no match)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s-other")
    assert ms.load_helper() is None and ro.client_relay(None) is None


def test_main_join_hook_feed_unchanged_with_helper_entries(monkeypatch, server):
    """Positive control: a session without an entry still feeds the main join's relay.json."""
    ms.write_relay(server.url, TOKEN, "room-a", 3600, owner_pid=os.getpid(),
                   owner_session=ms.session_hash("JOINER"))
    ms.write_helper("Zapmail", server.url, "tok.zap", "room-a", 3600,
                    owner_session=ms.session_hash("s-zap"))
    spawned = []
    monkeypatch.setattr(ms, "_spawn_post", lambda *a: spawned.append(a))
    ev = {"session_id": "other-1", "hook_event_name": "Stop", "cwd": "/tmp/x"}
    line = ms.handle_event(json.dumps(ev))
    assert line and spawned == [(line,)]  # one arg: the child loads relay.json as before
    assert ms.load_relay(session=ms.session_hash("other-1"))["token"] == TOKEN
    assert ms.handle_event(json.dumps({**ev, "session_id": "JOINER"})) is None  # the joiner


def test_dead_marks_are_per_token(server):
    ms.mark_dead("tok.a")
    ms.mark_dead("tok.b")
    assert ms._is_dead("tok.a") and ms._is_dead("tok.b") and not ms._is_dead("tok.c")


# --------------------------------------------------------------------------- #
# 0.22.2 nachtrag: 503 = wait and retry, never a dead credential (v4 #586)
# --------------------------------------------------------------------------- #
def test_503_is_retried_with_backoff_never_dead(monkeypatch, server, capsys):
    waits = []
    monkeypatch.setattr(ro, "_sleep", waits.append)
    ms.write_helper("Strategie", server.url, TOKEN, "room-a", 3600, owner_session="")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "helper-1")
    for name, argv in (("status", ["status", "--doing", "Tests"]), ("relay", ["activity", "x"]),
                       ("decide", ["decide", "--question", "Merge?", "--context", "c"]),
                       ("next", ["relay-next", "--timeout", "0"])):
        server.queue[name] = [(503, {"detail": "main agent away"})] * 2
        rc, lines, _ = _cli(argv, capsys)
        assert rc == 0 and lines[0].get("ok", True) is not False, (name, lines)
    assert waits == list(ro.RETRY_503_S[:2]) * 4 and not ms._is_dead(TOKEN)
    server.queue["status"] = [(503, {"detail": "away"})] * 9  # stays away: an error, not dead
    rc, lines, _ = _cli(["status", "--doing", "Tests"], capsys)
    assert rc == 1 and lines[0]["status"] == 503 and not ms._is_dead(TOKEN)
    assert ms.load_relay()["token"] == TOKEN


# --------------------------------------------------------------------------- #
# 0.22.2 nachtrag (Oliver): only routed call events reach a session
# --------------------------------------------------------------------------- #
def _hook(name, sid, **kw):
    from voicehook_agent import board
    ev = {"hook_event_name": name, "session_id": sid, "cwd": "/work/x", **kw}
    if name == "PostToolUse":
        ev.setdefault("tool_name", "Read")
        ev.setdefault("tool_input", {"file_path": "/x"})
        ev.setdefault("tool_response", {})
    return board.handle(json.dumps(ev), now=None)


def test_helper_sees_only_its_routed_events_bystander_nothing(monkeypatch, server):
    from voicehook_agent import board
    monkeypatch.setattr(board, "_spawn_post", lambda job: None)
    monkeypatch.setattr(board, "_spawn_sleeper", lambda skey: None)
    monkeypatch.setattr(board, "_ensure_watcher", lambda: None)
    inbox: dict = {}
    monkeypatch.setattr(board, "fetch_answers", lambda rel, frm: inbox.pop(frm, []))
    ms.write_relay(server.url, TOKEN, "room-a", 3600, owner_pid=os.getpid(),
                   owner_session=ms.session_hash("ROUTER"))
    ms.write_helper("A", server.url, "tok.a", "room-a", 3600, owner_session=ms.session_hash("s-a"))
    ms.write_helper("B", server.url, "tok.b", "room-a", 3600, owner_session=ms.session_hash("s-b"))
    for sid, label in (("s-a", "Helfer A"), ("s-b", "Helfer B")):  # both set their board
        monkeypatch.setenv(ms.LABEL_ENV, label)
        _hook("PostToolUse", sid, tool_name="Bash",
              tool_input={"command": 'voicehook-agent status "Arbeit"', "description": "x"})
    inbox["Helfer A"] = [{"type": "user_turn", "text": "A, schick die Belege", "to": "Helfer A",
                          "to_kind": "helper", "ts": 1}]
    monkeypatch.setenv(ms.LABEL_ENV, "Helfer B")
    out_b = _hook("UserPromptSubmit", "s-b")
    monkeypatch.setenv(ms.LABEL_ENV, "Helfer A")
    out_a = _hook("UserPromptSubmit", "s-a")
    ctx_a = json.dumps(out_a or {}, ensure_ascii=False)
    assert "schick die Belege" in ctx_a                       # routed to A by name
    assert "schick die Belege" not in json.dumps(out_b or {}, ensure_ascii=False)
    monkeypatch.delenv(ms.LABEL_ENV)
    for name in ("PostToolUse", "UserPromptSubmit", "Stop"):  # a bystander: nothing at all
        assert _hook(name, "bystander") is None, name


# --------------------------------------------------------------------------- #
# 0.24.x: imported helper (no relay.json) -- single-entry fallback
# --------------------------------------------------------------------------- #
def test_imported_helper_single_entry_found_without_label_or_session_match(monkeypatch, server, tmp_path):
    """Core regression: helper-import wrote helpers/<name>.json with owner_session bound to
    the importing session. Hook fires without VOICEHOOK_SESSION_LABEL and with a different
    VOICEHOOK_STATE_DIR (own tmp dir per helper). No relay.json exists.
    own_helper_entry / load_relay must return the single usable entry even though
    owner_session does not match the hook's session hash."""
    monkeypatch.setenv("VOICEHOOK_AGENT_HOME", str(tmp_path / "home"))
    # Write ONE helper entry, bound to session 's-import' (the session that ran helper-import)
    ms.write_helper("helper-bot", server.url, TOKEN, "room-a", 3600,
                    owner_session=ms.session_hash("s-import"))
    assert not ms.relay_path().exists()  # no relay.json -- the imported-helper case
    # Hook fires as session 's-hook' (different hash, no VOICEHOOK_SESSION_LABEL set)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s-hook")
    sess = ms.session_hash("s-hook")
    # Without the fix: own_helper_entry returns None -> load_relay returns None
    # With the fix: single usable entry is returned
    result = ms.own_helper_entry(session=sess)
    assert result is not None, "single usable entry must be found even with non-matching owner_session"
    assert result[1]["helper"] == "helper-bot"
    assert ms.load_relay(session=sess) is not None
    assert ms.load_relay(session=sess)["token"] == TOKEN


def test_label_match_takes_precedence_over_single_entry_fallback(monkeypatch, server, tmp_path):
    """Zweig 1 (label match) still wins over the single-entry fallback."""
    monkeypatch.setenv("VOICEHOOK_AGENT_HOME", str(tmp_path / "home"))
    ms.write_helper("named-bot", server.url, TOKEN, "room-a", 3600,
                    owner_session=ms.session_hash("s-other"))
    ms.write_helper("other-bot", server.url, "fake.other.tok.x", "room-a", 3600,
                    owner_session=ms.session_hash("s-second"))
    monkeypatch.setenv(ms.LABEL_ENV, "named-bot")
    result = ms.own_helper_entry(session=ms.session_hash("s-unrelated"))
    assert result is not None and result[1]["helper"] == "named-bot"
    assert ms.load_relay(session=ms.session_hash("s-unrelated"))["token"] == TOKEN


def test_session_match_takes_precedence_over_single_entry_fallback(monkeypatch, server, tmp_path):
    """Zweig 2 (session match) still wins over the single-entry fallback."""
    monkeypatch.setenv("VOICEHOOK_AGENT_HOME", str(tmp_path / "home"))
    ms.write_helper("my-bot", server.url, TOKEN, "room-a", 3600,
                    owner_session=ms.session_hash("s-mine"))
    monkeypatch.delenv(ms.LABEL_ENV, raising=False)
    result = ms.own_helper_entry(session=ms.session_hash("s-mine"))
    assert result is not None and result[1]["helper"] == "my-bot"


def test_multiple_bound_entries_no_label_no_match_returns_none(monkeypatch, server, tmp_path):
    """Several bound entries, no label, no session match: ambiguous -> None."""
    monkeypatch.setenv("VOICEHOOK_AGENT_HOME", str(tmp_path / "home"))
    ms.write_helper("bot-a", server.url, TOKEN, "room-a", 3600,
                    owner_session=ms.session_hash("s-a"))
    ms.write_helper("bot-b", server.url, "fake.other.tok.y", "room-a", 3600,
                    owner_session=ms.session_hash("s-b"))
    monkeypatch.delenv(ms.LABEL_ENV, raising=False)
    result = ms.own_helper_entry(session=ms.session_hash("s-unrelated"))
    assert result is None, "multiple entries without label/session match must be None (ambiguous)"
    assert ms.load_relay(session=ms.session_hash("s-unrelated")) is None
