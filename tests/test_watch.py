"""0.32.0 `voicehook-agent watch`: one detached watcher per machine while a call is active."""
from __future__ import annotations

import json
import os
import time

import pytest

from voicehook_agent import cli, notify
from voicehook_agent import multisession as vmulti
from voicehook_agent import relayops as vrelayops
from voicehook_agent import watch as W

NOW = 1_800_000_000.0
URL = "https://voicehook.test/api/activity/relay"
TOK = "tok-" + "a" * 12


def _relay(owner_pid=0, expires=NOW + 3600, **extra):
    return {"url": URL, "token": TOK, "room": "r", "expires_at": expires, "owner_pid": owner_pid,
            "owner_session": "ownerhash", **extra}


def _write_relay(tmp_path_state=None, **kw):
    vmulti.write_relay(URL, TOK, "room", 3600)


class Fake:
    """Fake server + model + clock for one Watcher."""

    def __init__(self, monkeypatch, sessions=None, watch=(200, {"interval_min": None})):
        self.t = NOW
        self.sessions = sessions or []
        self.watch = watch
        self.posts: list[tuple[str, str, dict]] = []
        self.calls = 0
        self.rel = _relay()
        monkeypatch.setattr(W, "credential", lambda now=None: self.rel)
        monkeypatch.setattr(W, "server_interval", self._server)
        monkeypatch.setattr(W, "post_board", self._board)
        monkeypatch.setattr(W, "post_activity", lambda rel, label, s: self._post("act", label, s))
        monkeypatch.setattr(W, "delete_decision", self._delete)
        self.unbound: list[dict] = []
        self.proposals: list[dict] = []
        self.answers: list[dict] = []
        monkeypatch.setattr(W, "post_proposal", self._propose)
        monkeypatch.setattr(W, "poll_answers", lambda rel: [self.answers.pop(0)] if self.answers else [])
        self.status = (404, {})          # GET <relay>/status?from=: old server by default
        monkeypatch.setattr(W, "get_status", lambda rel, label: self.status)
        self.deleted: list[str] = []
        self.delete_status = 200

    def _propose(self, rel, body):
        self.proposals.append(body)
        return {"ok": True, "id": f"d{len(self.proposals):08d}"}

    def _server(self, rel):
        st, body = self.watch
        if st in (401, 403, 410):
            return "over", None
        if st == 404:
            return "old", None
        if st >= 500:
            raise vrelayops.RelayError("x", st)
        return "ok", W.parse_interval(body.get("interval_min"))

    def _post(self, kind, label, s, body=None):
        self.posts.append((kind, label, {"s": s, "body": body}))
        return {"ok": True, "status": 200}

    def _board(self, rel, label, body):
        return self._post("board", label, body.get("doing"), body)

    def _delete(self, rel, label, dec_id):
        self.deleted.append(dec_id)
        return {"ok": self.delete_status == 200, "status": self.delete_status}

    def model(self, sentence="Die Sitzung baut gerade die neue Seite.", tokens=1500):
        self.cmd_calls = []

        def run(cmd, input=None, **kw):
            self.cmd_calls.append(input)
            class R:
                returncode = 0
                stdout = json.dumps({"result": sentence, "usage": {"input_tokens": tokens, "output_tokens": 0}})
            return R()
        return run

    def watcher(self, **kw):
        w = W.Watcher(clock=lambda: self.t, discover_fn=lambda rel, now: self.sessions,
                      unbound_fn=lambda rel, now, active_s, **kw: [dict(u) for u in self.unbound],
                      cmd=kw.pop("cmd", ["fake-model"]), **kw)
        return w


def sess(key="k1", label="Alpha", ev="", ev_at=0.0, tp=""):
    return {"key": key, "label": label, "agent": "claude", "tp": tp, "ev": ev, "ev_text": "",
            "ev_at": ev_at}


# ----- interval ------------------------------------------------------------------------
def test_interval_precedence_and_allowed_values():
    assert W.ALLOWED_INTERVALS == (1, 5, 10, 15)
    assert W.resolve_interval(5, 10, "1") == (5, "server")
    assert W.resolve_interval(None, 10, "1") == (10, "flag")
    assert W.resolve_interval(None, None, "1") == (1, "env")
    assert W.resolve_interval(None, None, None) == (15, "default")
    assert W.resolve_interval(60, None, "7") == (15, "default")   # 60 and 7 are not allowed
    assert W.resolve_interval(None, 3, "abc") == (15, "default")


def test_server_setting_is_reread_every_tick(monkeypatch):
    f = Fake(monkeypatch, [sess(ev="stop", ev_at=NOW - 500)])
    w = f.watcher()
    f.watch = (200, {"interval_min": 5})
    assert w.tick() == W.OK
    f.watch = (200, {"interval_min": None})
    f.t += 1
    assert w.tick() == W.OK
    assert w.server_min is None


def test_old_server_404_ignored_and_rechecked_late(monkeypatch):
    f = Fake(monkeypatch, [], watch=(404, {}))
    w = f.watcher(interval_flag=10)
    assert w.tick() == W.OK
    assert w.not_found_until > f.t


def test_server_410_ends_only_when_confirmed(monkeypatch):
    """0.32.3: one 401/403/410 (seen live during a deploy) is not the end; three in a row over
    at least OVER_CONFIRM_S are. The watcher never marks the token dead."""
    for st in (401, 403, 410):
        f = Fake(monkeypatch, [], watch=(st, {}))
        w = f.watcher()
        assert w.tick() == W.OK
        f.t += 45
        assert w.tick() == W.OK
        f.t += 50
        assert w.tick() == W.END
    f = Fake(monkeypatch, [], watch=(410, {}))
    w = f.watcher()
    assert w.tick() == W.OK
    f.watch = (200, {"interval_min": 5})      # the server is back: the count starts again
    f.t += 45
    assert w.tick() == W.OK and w.over == []


# ----- lock / detach -------------------------------------------------------------------
def test_single_instance_lock():
    fd = W.acquire_lock()
    assert fd is not None
    assert W.acquire_lock() is None      # a second watcher exits quietly
    assert W.running() is True
    os.close(fd)
    assert W.running() is False


def test_second_foreground_start_exits_0(monkeypatch):
    monkeypatch.setattr(W, "credential", lambda now=None: _relay())
    fd = W.acquire_lock()
    started = []
    monkeypatch.setattr(W.Watcher, "run", lambda self, sleep=None: started.append(1) or 0)
    assert W.main(foreground=True) == 0
    assert started == []
    os.close(fd)


def test_spawn_detached_needs_call_and_not_running(monkeypatch):
    spawned = []
    popen = lambda argv, **kw: spawned.append((argv, kw))
    monkeypatch.setenv("VOICEHOOK_WATCH", "1")
    assert W.spawn_detached(popen=popen) is False                   # no credential: no call
    vmulti.write_relay(URL, TOK, "room", 3600)
    assert W.spawn_detached(popen=popen) is True
    argv, kw = spawned[0]
    assert kw["start_new_session"] is True and kw["stdin"] is not None
    assert argv[-2:] == ["watch", "--foreground"][-2:] or "--foreground" in argv
    fd = W.acquire_lock()
    assert W.spawn_detached(popen=popen) is False                   # one is running
    os.close(fd)
    monkeypatch.setenv("VOICEHOOK_WATCH", "0")
    assert W.spawn_detached(popen=popen) is False                   # opt-out
    assert len(spawned) == 1


def test_join_and_helper_import_spawn(monkeypatch):
    calls = []
    monkeypatch.setattr(cli.vwatch, "spawn_detached", lambda *a, **k: calls.append(1))
    cred = json.dumps({"token": TOK, "url": URL, "expires_in": 3600})
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(cred))
    args = type("A", (), {"as_name": "crown"})()
    assert cli._run_helper_import(args) == 0
    assert calls == [1]


def test_helper_credential_counts_as_call(monkeypatch):
    monkeypatch.undo() if False else None
    assert W.credential() is None
    vmulti.write_helper("crown", URL, TOK, "room", 3600, owner_session="")
    assert W.credential() is not None
    vmulti.mark_dead(TOK)
    assert W.credential() is None


def test_exits_without_call(monkeypatch):
    f = Fake(monkeypatch, [sess()])
    f.rel = None
    assert f.watcher().tick() == W.END
    f.rel = _relay(expires=NOW - 1)
    assert f.watcher().tick() == W.END


# ----- state ---------------------------------------------------------------------------
def test_state_detection(tmp_path):
    tp = tmp_path / "t.jsonl"
    tp.write_text("{}")
    os.utime(tp, (NOW - 400, NOW - 400))
    base = {"tp": str(tp)}
    assert W.classify_state({**base, "ev": "stop", "ev_at": NOW - 300}, NOW)[0] == W.STILL
    assert W.classify_state({**base, "ev": "wait", "ev_at": NOW - 300}, NOW)[0] == W.FRAGE
    os.utime(tp, (NOW - 30, NOW - 30))   # the transcript moved on after the question
    st, last = W.classify_state({**base, "ev": "wait", "ev_at": NOW - 300}, NOW)
    assert st == W.ARBEITET and last == NOW - 30
    assert W.classify_state({**base, "ev": "stop", "ev_at": NOW - 300}, NOW)[0] == W.ARBEITET
    os.utime(tp, (NOW - 3000, NOW - 3000))
    assert W.classify_state({**base, "ev": "turn", "ev_at": NOW - 3000}, NOW)[0] == W.STILL
    assert W.classify_state({"tp": "", "ev": "turn", "ev_at": NOW - 20}, NOW)[0] == W.ARBEITET


def test_last_assistant_text_claude_and_codex(tmp_path):
    p = tmp_path / "c.jsonl"
    p.write_text("\n".join([
        json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "erste"}]}}),
        json.dumps({"type": "user", "message": {"content": "frage"}}),
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Bash"}, {"type": "text", "text": "wort " * 600 + "ende"}]}}),
    ]))
    t = W.last_assistant_text(str(p))
    assert len(t) == 2000 and t.endswith("ende")
    q = tmp_path / "x.jsonl"
    q.write_text(json.dumps({"payload": {"role": "assistant",
                                         "content": [{"type": "output_text", "text": "codex sagt"}]}}))
    assert W.last_assistant_text(str(q)) == "codex sagt"


def test_hook_event_recorded_in_registry(tmp_path):
    vmulti.write_relay(URL, TOK, "room", 3600, owner_session="someoneelse")
    ev = json.dumps({"hook_event_name": "Stop", "session_id": "sess-1234567890", "cwd": "/x/proj",
                     "transcript_path": "/x/t.jsonl"})
    now = time.time()
    assert vmulti.handle_event(ev, now=now, post=lambda line: None) is not None
    st = json.loads((tmp_path / "vh-state" / vmulti.STATE_NAME).read_text())
    me = next(iter(st["sessions"].values()))
    assert me["ev"] == "stop" and me["tp"] == "/x/t.jsonl" and me["ev_at"] == pytest.approx(now, abs=5)
    rel = _relay()
    got = W.discover(rel, now, ps=lambda: "", claude_root=tmp_path / "none", codex_root=tmp_path / "none")
    assert len(got) == 1 and got[0]["ev"] == "stop"


def test_watcher_model_call_hooks_do_not_register(monkeypatch):
    vmulti.write_relay(URL, TOK, "room", 3600, owner_session="someoneelse")
    monkeypatch.setenv("VOICEHOOK_WATCH_CHILD", "1")
    ev = json.dumps({"hook_event_name": "Stop", "session_id": "sess-1234567890", "cwd": "/x"})
    assert vmulti.handle_event(ev, now=NOW, post=lambda line: None) is None


def test_discover_unregistered_transcripts_capped_by_processes(tmp_path, monkeypatch):
    root = tmp_path / "projects" / "p"
    root.mkdir(parents=True)
    for n in ("aaaaaaaa-1", "bbbbbbbb-2"):
        (root / f"{n}.jsonl").write_text(json.dumps({"type": "ai-title", "aiTitle": f"Titel {n[:1]}"}))
    ps = "  10     1 claude\n"
    kw = {"ps": lambda: ps, "claude_root": tmp_path / "projects", "codex_root": tmp_path / "none"}
    assert W.discover(_relay(), time.time(), **kw) == []        # privacy default: not covered
    monkeypatch.setenv("VOICEHOOK_WATCH_ALL", "1")
    got = W.discover(_relay(), time.time(), **kw)
    assert len(got) == 1                      # one claude process = one session
    got = W.discover(_relay(), time.time(), ps=lambda: "", claude_root=tmp_path / "projects",
                     codex_root=tmp_path / "none")
    assert got == []


# ----- posting -------------------------------------------------------------------------
def test_first_tick_board_only_then_change_adds_activity(monkeypatch):
    f = Fake(monkeypatch, [sess(ev="turn", ev_at=NOW - 10)])
    w = f.watcher(cmd=None)
    assert w.tick() == W.OK
    assert [p[0] for p in f.posts] == ["board"] and f.posts[0][1] == "Alpha (Wächter)"
    f.sessions = [sess(ev="stop", ev_at=NOW + 5)]
    f.t += 60                       # well inside the interval: a state change posts at once
    w.tick()
    assert [p[0] for p in f.posts] == ["board", "board", "act"]
    f.t += 60
    w.tick()
    assert len(f.posts) == 3        # unchanged, inside the interval: nothing


def test_posts_again_after_interval(monkeypatch):
    f = Fake(monkeypatch, [sess(ev="stop", ev_at=NOW - 500)])
    w = f.watcher(cmd=None, interval_flag=5)
    w.tick()
    f.t += 200
    w.tick()
    assert len(f.posts) == 1
    f.t += 200
    w.tick()
    assert len(f.posts) == 2


def test_skip_self_reporting_session(monkeypatch):
    f = Fake(monkeypatch, [sess(label="Beta", ev="stop", ev_at=NOW - 500)])
    W.mark_self("Beta", now=f.t - 30)
    w = f.watcher(cmd=None, interval_flag=5)
    w.tick()
    assert f.posts == []
    f.t += 400                      # the self post is older than the interval now
    w.tick()
    assert [p[1] for p in f.posts] == ["Beta (Wächter)"]


def test_cli_status_marks_self(monkeypatch):
    W.mark_self("Gamma", now=time.time())
    assert time.time() - W.self_posted_at("Gamma") < 5
    assert W.self_posted_at("Nobody") == 0.0


# ----- model / budget ------------------------------------------------------------------
def test_model_sentence_and_budget_fallback(monkeypatch, tmp_path):
    tp = tmp_path / "t.jsonl"
    tp.write_text(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "fertig"}]}}))
    f = Fake(monkeypatch, [sess(ev="turn", ev_at=NOW - 5, tp=str(tp))])
    os.utime(tp, (NOW - 5, NOW - 5))
    run = f.model(tokens=1500)
    monkeypatch.setattr(W.subprocess, "run", run)
    w = f.watcher(tokens=2000, interval_flag=1)
    w.tick()
    assert f.posts[0][2]["s"] == "Die Sitzung baut gerade die neue Seite."
    assert w.used(f.t) == 1500 and len(f.cmd_calls) == 1
    assert "fertig" in f.cmd_calls[0]
    # activity again, interval passed, budget not yet used up -> model; then used up -> fixed
    tp.write_text(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "zweiter Stand"}]}}))
    os.utime(tp, (f.t + 70, f.t + 70)); f.t += 80
    w.tick()
    assert len(f.cmd_calls) == 2 and w.used(f.t) == 3000
    tp.write_text(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "dritter Stand"}]}}))
    os.utime(tp, (f.t + 70, f.t + 70)); f.t += 80
    w.tick()
    assert len(f.cmd_calls) == 2                    # budget used up: no call
    assert "Die Sitzung" in f.posts[-1][2]["s"] and f.posts[-1][2]["s"] != "Die Sitzung baut gerade die neue Seite."
    f.t += 3600                                     # the hour rolls over: budget is free again
    assert w.used(f.t) == 0


def test_one_model_call_per_session_per_interval(monkeypatch, tmp_path):
    tp = tmp_path / "t.jsonl"
    tp.write_text("{}")
    f = Fake(monkeypatch, [sess(ev="turn", ev_at=NOW - 5, tp=str(tp))])
    monkeypatch.setattr(W.subprocess, "run", f.model())
    w = f.watcher(tokens=100000, interval_flag=15)
    w.tick()
    f.sessions = [sess(ev="stop", ev_at=NOW + 20, tp=str(tp))]   # state change inside the interval
    f.t += 60
    w.tick()
    assert len(f.cmd_calls) == 1
    assert f.posts[-1][0] == "act" and f.posts[-1][2]["s"].startswith("inaktiv seit")


def test_no_model_means_fixed_sentence(monkeypatch):
    f = Fake(monkeypatch, [sess(ev="wait", ev_at=NOW - 100)])
    w = f.watcher(cmd=None)
    w.tick()
    assert f.posts[0][2]["s"].startswith("Die Sitzung wartet auf eine Antwort")
    assert W.model_cmd({"VOICEHOOK_WATCH_MODEL_CMD": "off"}) is None
    assert W.model_cmd({"VOICEHOOK_WATCH_MODEL_CMD": "my-llm --x 1"}) == ["my-llm", "--x", "1"]


def test_default_budget_is_configurable(monkeypatch):
    assert W.tokens_per_hour() == W.DEFAULT_TOKENS_PER_HOUR == 40000
    assert W.tokens_per_hour(5) == 5
    assert W.tokens_per_hour(None, {W.ENV_TOKENS: "123"}) == 123
    assert W.tokens_per_hour(None, {W.ENV_TOKENS: "x"}) == W.DEFAULT_TOKENS_PER_HOUR


def test_ask_model_parses_usage_and_failures():
    class R:
        def __init__(self, out, rc=0): self.stdout, self.returncode = out, rc
    ok = json.dumps({"result": "Die Arbeit läuft gut.", "usage": {
        "input_tokens": 2, "output_tokens": 10, "cache_creation_input_tokens": 500,
        "cache_read_input_tokens": 1000}})
    assert W.ask_model(["m"], "p", run=lambda *a, **k: R(ok)) == ("Die Arbeit läuft gut.", 612)
    assert W.ask_model(["m"], "p", run=lambda *a, **k: R(ok, 1))[0] == ""
    assert W.ask_model(["m"], "p", run=lambda *a, **k: R("Ein ganzer Satz hier.")) [0] == "Ein ganzer Satz hier."
    def boom(*a, **k): raise OSError("x")
    assert W.ask_model(["m"], "p", run=boom) == ("", 0)


# ----- backoff / loop ------------------------------------------------------------------
def test_backoff_exponential_and_capped(monkeypatch):
    f = Fake(monkeypatch, [], watch=(503, {}))
    w = f.watcher()
    assert w.delay() == W.TICK_S
    delays = []
    for _ in range(12):
        w.tick()
        delays.append(w.delay())
    assert delays[0] == W.TICK_S * 2 and delays[1] == W.TICK_S * 4
    assert max(delays) == W.BACKOFF_MAX_S
    f.watch = (200, {"interval_min": None})
    f.sessions = []
    w.tick()
    assert w.delay() == W.TICK_S


def test_run_loop_sleeps_fixed_and_ends(monkeypatch):
    f = Fake(monkeypatch, [])
    w = f.watcher(cmd=None)
    sleeps = []
    ticks = []

    def sleep(s):
        sleeps.append(s)
        ticks.append(1)
        if len(ticks) == 3:
            f.rel = None
    assert w.run(sleep=sleep) == 0
    assert sleeps == [W.TICK_S] * 3


def test_run_survives_tick_exception(monkeypatch):
    f = Fake(monkeypatch, [])
    w = f.watcher(cmd=None)
    n = []
    def tick():
        n.append(1)
        if len(n) == 1:
            raise ValueError("bug")
        return W.END
    w.tick = tick
    sleeps = []
    assert w.run(sleep=sleeps.append) == 0
    assert sleeps == [W.TICK_S * 2]


# ----- decisions -----------------------------------------------------------------------
def _slot(skey="k1", at=NOW - 100, dec="d12345678", label="Alpha", **kw):
    def step(st):
        st["sessions"][skey] = {"seen": at, "open": {"q": "x", "at": at, "prio": 2, "route": "relay",
                                                    "id": dec, "resolved": False, "from": label, **kw}}
    notify._with_state(step)


def test_decision_removed_when_session_went_on(monkeypatch, tmp_path):
    tp = tmp_path / "t.jsonl"; tp.write_text("{}")
    os.utime(tp, (NOW - 20, NOW - 20))
    f = Fake(monkeypatch, [sess(ev="wait", ev_at=NOW - 100, tp=str(tp))])
    _slot()
    w = f.watcher(cmd=None)
    assert w.tick() == W.OK
    assert f.deleted == ["d12345678"]
    assert notify.relay_slots() == []


def test_open_decision_of_waiting_session_is_kept(monkeypatch, tmp_path):
    tp = tmp_path / "t.jsonl"; tp.write_text("{}")
    os.utime(tp, (NOW - 300, NOW - 300))
    f = Fake(monkeypatch, [sess(ev="wait", ev_at=NOW - 100, tp=str(tp))])
    _slot()
    f.watcher(cmd=None).tick()
    assert f.deleted == [] and len(notify.relay_slots()) == 1


def test_decision_of_vanished_session_removed_only_when_old(monkeypatch):
    f = Fake(monkeypatch, [])
    _slot(at=NOW - 100)
    w = f.watcher(cmd=None)
    w.tick()
    assert f.deleted == []                       # young: maybe the session is just not listed
    f.t = NOW + notify.OPEN_KEEP_S + 10
    f.rel = _relay(expires=f.t + 100)
    w.tick()
    assert f.deleted == ["d12345678"]


def test_delete_409_is_done_and_5xx_retries(monkeypatch, tmp_path):
    tp = tmp_path / "t.jsonl"; tp.write_text("{}")
    os.utime(tp, (NOW - 20, NOW - 20))
    f = Fake(monkeypatch, [sess(ev="wait", ev_at=NOW - 100, tp=str(tp))])
    _slot(ids=["d12345678", "d87654321"])
    f.delete_status = 503
    w = f.watcher(cmd=None)
    w.tick()
    assert len(notify.relay_slots()) == 1        # failed: slot stays, retried next tick
    f.delete_status = 409
    w.tick()
    assert notify.relay_slots() == []


def test_delete_410_keeps_running(monkeypatch, tmp_path):
    """A 410 on deleting a decision is that session's credential, not the call: keep going,
    keep the slot, try again later."""
    tp = tmp_path / "t.jsonl"; tp.write_text("{}")
    os.utime(tp, (NOW - 20, NOW - 20))
    f = Fake(monkeypatch, [sess(ev="wait", ev_at=NOW - 100, tp=str(tp))])
    _slot()
    f.delete_status = 410
    assert f.watcher(cmd=None).tick() == W.OK


def test_only_relay_slots_of_this_machine(monkeypatch):
    notify._with_state(lambda st: st["sessions"].update(
        own={"open": {"q": "x", "at": NOW, "route": "own", "id": "dabc", "resolved": False}}))
    assert notify.relay_slots() == []


# ----- logs ----------------------------------------------------------------------------
def test_log_has_metadata_only_and_rotates(monkeypatch, tmp_path):
    secret_text = "GEHEIMER-TRANSKRIPT-INHALT"
    secret_sentence = "Geheimer erzeugter Satz hier."
    tp = tmp_path / "t.jsonl"
    tp.write_text(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": secret_text}]}}))
    f = Fake(monkeypatch, [sess(ev="turn", ev_at=NOW - 5, tp=str(tp))])
    os.utime(tp, (NOW - 5, NOW - 5))
    monkeypatch.setattr(W.subprocess, "run", f.model(sentence=secret_sentence))
    w = f.watcher(tokens=50000)
    w.tick()
    txt = (tmp_path / "vh-state" / W.LOG_NAME).read_text()
    assert "label=Alpha" in txt and "tokens=1500" in txt and "state=arbeitet" in txt
    assert secret_text not in txt and secret_sentence not in txt and "Geheimer" not in txt
    monkeypatch.setattr(W, "LOG_MAX_BYTES", 50)
    W.log("x", a=1)
    W.log("y", a=1)
    assert (tmp_path / "vh-state" / (W.LOG_NAME + ".1")).exists()


# ----- board merge (a relay /status replaces the whole board of a sender) -------------
def test_merge_keeps_open_done_and_their_meta():
    base = {"doing": "alt", "open": ["a", "b"], "done": ["c"], "project": "P",
            "meta": {"doing": {"context": "alt"}, "open": [{"context": "x"}, {}], "done": [{"faq": []}]}}
    out = W.merge_board(base, "Neuer Satz hier drin.", {"context": "neu", "faq": []})
    assert out["doing"] == "Neuer Satz hier drin."
    assert out["open"] == ["a", "b"] and out["done"] == ["c"] and out["project"] == "P"
    assert out["meta"]["open"] == base["meta"]["open"] and out["meta"]["done"] == base["meta"]["done"]
    assert out["meta"]["doing"] == {"context": "neu", "faq": []}
    assert base["doing"] == "alt" and base["meta"]["doing"] == {"context": "alt"}   # input untouched
    assert W.merge_board(None, "Satz mit drei Worten.") == {"doing": "Satz mit drei Worten."}
    assert W.merge_board({}, "Satz mit drei Worten.", {"context": "c"})["meta"] == {"doing": {"context": "c"}}


def test_doing_meta_has_context_and_three_faq():
    from voicehook_agent import relay
    m = W.doing_meta("Alpha", W.FRAGE, NOW - 300, NOW)
    assert "Alpha" in m["context"] and "5 Minuten" in m["context"]
    assert len(m["faq"]) == 3 and relay.clean_meta(m) == m


def test_watcher_posts_merged_board_with_own_open_items(monkeypatch):
    f = Fake(monkeypatch, [sess(ev="stop", ev_at=NOW - 500)])
    W.mark_self("Alpha", now=f.t - 1000, board={"doing": "selbst", "open": ["Frage 1"], "done": ["Fertig 1"],
                                               "meta": {"open": [{"context": "warum"}]}})
    w = f.watcher(cmd=None, interval_flag=5)
    w.tick()
    body = f.posts[0][2]["body"]
    assert body["doing"].startswith("inaktiv seit") and body["open"] == ["Frage 1"]
    assert body["done"] == ["Fertig 1"] and body["meta"]["open"] == [{"context": "warum"}]
    assert body["meta"]["doing"]["context"] and len(body["meta"]["doing"]["faq"]) == 3
    # the next watcher post merges into its own last post: open/done still there
    f.t += 400
    w.tick()
    assert f.posts[-1][2]["body"]["open"] == ["Frage 1"]


def test_unknown_board_goes_to_separate_watcher_board_never_main(monkeypatch):
    f = Fake(monkeypatch, [sess(label="Neu", ev="stop", ev_at=NOW - 500)])
    f.watcher(cmd=None).tick()
    assert f.posts[0][1] == "Neu (Wächter)" and f.posts[0][1] != "Neu"
    assert all(p[1] != "Neu" for p in f.posts if p[0] == "board")
    body = f.posts[0][2]["body"]
    assert set(body) == {"doing", "meta"} and set(body["meta"]) == {"doing"}   # never `state` here


def test_cleared_board_is_forgotten(monkeypatch):
    from voicehook_agent import board
    monkeypatch.setattr(vrelayops, "remote_status", lambda rel, frm, body, **k: {"ok": True})
    W.remember_board("Alpha", {"doing": "x", "open": ["o"]})
    assert W.known_board("Alpha") is not None
    assert board.clear_board(_relay(), "Alpha") is True
    assert W.known_board("Alpha") is None
    W.remember_board("Old", {"doing": "x"}, now=time.time() - 99999)
    assert W.known_board("Old") is None


def test_sub_label_fits_and_is_clean():
    long = "L" * 40
    assert len(W.sub_label(long)) <= 40 and W.sub_label(long).endswith("(Wächter)")
    assert vrelayops.check_from(W.sub_label(long))


def test_state_set_on_every_post_and_stale_frage_dropped(monkeypatch):
    f = Fake(monkeypatch, [sess(ev="stop", ev_at=NOW - 500)])
    W.mark_self("Alpha", now=f.t - 1000, board={"doing": "x", "open": ["o"], "state": "frage"})
    w = f.watcher(cmd=None, interval_flag=5)
    w.tick()
    b = f.posts[0][2]["body"]
    assert b["state"] == "leerlauf" and b["open"] == ["o"]       # session moved on: frage gone
    f.sessions = [sess(ev="wait", ev_at=NOW + 100)]
    f.t += 400
    w.tick()
    assert [p for p in f.posts if p[0] == "board"][-1][2]["body"]["state"] == "frage"
    assert W.merge_board({"state": "frage"}, "Satz mit drei Worten.", None, W.ARBEITET)["state"] == "arbeitet"


def test_watcher_board_is_cleared_once_session_reports_itself(monkeypatch):
    f = Fake(monkeypatch, [sess(ev="stop", ev_at=NOW - 500)])
    w = f.watcher(cmd=None, interval_flag=5)
    w.tick()
    assert W.known_board("Alpha (Wächter)", f.t) is not None
    W.mark_self("Alpha", now=f.t + 10, board={"doing": "selbst", "open": ["o"]})
    f.t += 400
    w.tick()
    kinds = [(p[1], p[2]["body"]) for p in f.posts if p[0] == "board"]
    assert kinds[-2] == ("Alpha", kinds[-2][1]) and kinds[-2][1]["open"] == ["o"]
    assert kinds[-1] == ("Alpha (Wächter)", {})
    assert W.known_board("Alpha (Wächter)", f.t) is None


def test_unregistered_session_not_posted_by_default_but_with_optin(monkeypatch, tmp_path):
    root = tmp_path / "projects" / "p"
    root.mkdir(parents=True)
    (root / "cccccccc-3.jsonl").write_text(json.dumps({"type": "ai-title", "aiTitle": "Privat Gehalt"}))
    kw = {"ps": lambda: "  10     1 claude\n", "claude_root": tmp_path / "projects",
          "codex_root": tmp_path / "none"}
    f = Fake(monkeypatch)
    f.sessions = None
    w = W.Watcher(clock=time.time, discover_fn=lambda rel, now: W.discover(rel, now, **kw), cmd=None,
                  unbound_fn=lambda *a, **k: [])
    f.t = time.time()
    f.rel = _relay(expires=f.t + 3600)
    w.clock = lambda: f.t
    w.tick()
    assert f.posts == []
    monkeypatch.setenv("VOICEHOOK_WATCH_ALL", "1")
    w.mem.clear()
    w.tick()
    assert [p[1] for p in f.posts] == ["Privat Gehalt (Wächter)"]


def test_helper_credential_session_is_covered(monkeypatch, tmp_path):
    vmulti.write_helper("crown", URL, TOK, "room", 3600, owner_session="")
    got = W.discover(_relay(), time.time(), ps=lambda: "", claude_root=tmp_path / "n", codex_root=tmp_path / "n")
    assert [g["label"] for g in got] == ["crown"] and got[0]["tp"] == ""
    # a registered session of the same helper is not listed twice
    vmulti.write_relay(URL, TOK, "room", 3600, owner_session="someoneelse")
    vmulti.handle_event(json.dumps({"hook_event_name": "Stop", "session_id": "sess-1234567890",
                                    "cwd": "/x/crown"}), post=lambda line: None)
    f = Fake(monkeypatch, [g for g in got])
    f.t = time.time()
    f.rel = _relay(expires=f.t + 3600)
    w = f.watcher(cmd=None)
    w.clock = lambda: f.t
    w.tick()
    assert f.posts == []          # no signal (no transcript, no hook events): no claim, no post


def test_unobservable_session_gets_no_status_but_decision_cleanup(monkeypatch):
    f = Fake(monkeypatch, [sess(key="helper-crown", label="crown")])   # no tp, no ev
    _slot(skey="helper-crown", at=NOW - 100, label="crown")
    w = f.watcher(cmd=None)
    f.t = NOW + notify.OPEN_KEEP_S + 10
    f.rel = _relay(expires=f.t + 100)
    assert w.tick() == W.OK
    assert f.posts == []                 # nothing posted, not even an activity line
    assert f.deleted == ["d12345678"]    # old decision cleanup still applies


def test_watcher_board_never_carries_state_but_main_does(monkeypatch):
    f = Fake(monkeypatch, [sess(label="Neu", ev="wait", ev_at=NOW - 20),
                           sess(key="k2", label="Alt", ev="wait", ev_at=NOW - 20)])
    W.remember_board("Alt", {"doing": "x", "open": ["o"]}, now=f.t - 1000)
    f.watcher(cmd=None).tick()
    by = {p[1]: p[2]["body"] for p in f.posts if p[0] == "board"}
    assert "state" not in by["Neu (Wächter)"] and by["Alt"]["state"] == "frage"


def test_watcher_boards_cleared_when_session_disappears(monkeypatch):
    f = Fake(monkeypatch, [sess(ev="stop", ev_at=NOW - 500)])
    w = f.watcher(cmd=None)
    w.tick()
    assert W.sub_boards() == ["Alpha (Wächter)"]
    f.sessions = []
    f.t += 60
    w.tick()
    assert [(p[1], p[2]["body"]) for p in f.posts if p[0] == "board"][-1] == ("Alpha (Wächter)", {})
    assert W.sub_boards() == []


def test_watcher_boards_cleared_on_exit(monkeypatch):
    f = Fake(monkeypatch, [sess(ev="stop", ev_at=NOW - 500)])
    w = f.watcher(cmd=None)
    w.tick()
    assert W.sub_boards() == ["Alpha (Wächter)"]
    cleared = []
    monkeypatch.setattr(W, "post_board", lambda rel, label, body: cleared.append((label, body)) or {"ok": True})
    f.rel = None                          # the call is over: credential gone, last relay is used
    w.shutdown()
    assert cleared == [("Alpha (Wächter)", {})] and W.sub_boards() == []


def test_run_clears_boards_in_finally(monkeypatch):
    f = Fake(monkeypatch, [sess(ev="stop", ev_at=NOW - 500)])
    w = f.watcher(cmd=None)
    posted = []
    monkeypatch.setattr(W, "post_board", lambda rel, label, body: posted.append((label, body)) or {"ok": True})
    def sleep(_s):
        raise SystemExit(0)               # SIGTERM handler path
    with pytest.raises(SystemExit):
        w.run(sleep=sleep)
    assert ("Alpha (Wächter)", {}) in posted and W.sub_boards() == []


def test_helper_command_makes_its_session_observable(monkeypatch, tmp_path):
    """0.32.2: a helper command inside a Claude Code session (no hooks installed) notes
    label -> transcript in the registry; the watcher then sees that session."""
    from voicehook_agent import multisession as M
    from voicehook_agent import relayops as R
    proj = tmp_path / "cfg" / "projects" / "-home-x"
    proj.mkdir(parents=True)
    tp = proj / "sess-1.jsonl"
    tp.write_text("{}\n")
    monkeypatch.setattr(R, "_config_dir", lambda: tmp_path / "cfg")
    M.note_helper_session("dev-9", env={"CLAUDE_CODE_SESSION_ID": "sess-1"})
    M.note_helper_session("dev-9", env={})                                  # outside a session: nothing
    M.note_helper_session("dev-8", env={"CLAUDE_CODE_SESSION_ID": "../x"})  # never a path
    found = [s for s in W.discover(_relay(), time.time(), ps=lambda: "", claude_root=tmp_path / "none",
                                   codex_root=tmp_path / "none") if s["label"] == "dev-9"]
    assert len(found) == 1 and found[0]["tp"] == str(tp)
    assert not [s for s in W.discover(_relay(), time.time(), ps=lambda: "",
                                      claude_root=tmp_path / "none", codex_root=tmp_path / "none")
                if s["label"] == "dev-8"]


# ----- 0.33.0 Part A: truthful status ----------------------------------------------------
def test_inactive_text_has_time_and_last_real_doing(monkeypatch):
    f = Fake(monkeypatch, [sess(ev="stop", ev_at=NOW - 3700)])
    W.mark_self("Alpha", now=f.t - 5000, board={"doing": "baut die Kartenansicht", "open": ["x"]})
    w = f.watcher(cmd=["fake-model"], interval_flag=5)
    w.tick()
    assert f.posts[0][2]["s"] == "inaktiv seit 1 Std, letzter Stand: baut die Kartenansicht"
    # the watcher's own sentence never replaces the remembered doing of the session
    f.t += 400
    w.tick()
    assert W.own_doing("Alpha", f.t) == "baut die Kartenansicht"
    assert f.posts[-1][2]["s"].startswith("inaktiv seit 1 Std, letzter Stand: baut die")


def test_inactive_text_without_known_doing_and_no_model(monkeypatch):
    f = Fake(monkeypatch, [sess(ev="stop", ev_at=NOW - 3700)])
    monkeypatch.setattr(W.subprocess, "run", lambda *a, **k: pytest.fail("model called for inactive"))
    f.watcher(tokens=100000).tick()
    assert f.posts[0][2]["s"] == "inaktiv seit 1 Std"


def test_model_not_called_when_sentence_would_not_change(monkeypatch, tmp_path):
    tp = tmp_path / "t.jsonl"
    tp.write_text(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "gleich"}]}}))
    f = Fake(monkeypatch, [sess(ev="turn", ev_at=NOW - 5, tp=str(tp))])
    os.utime(tp, (NOW - 5, NOW - 5))
    monkeypatch.setattr(W.subprocess, "run", f.model())
    w = f.watcher(tokens=100000, interval_flag=1)
    w.tick()
    os.utime(tp, (f.t + 70, f.t + 70)); f.t += 80      # activity, same last message
    w.tick()
    assert len(f.cmd_calls) == 1
    assert f.posts[-1][2]["s"] == "Die Sitzung baut gerade die neue Seite."


def test_unobservable_session_silent_then_cleared(monkeypatch):
    f = Fake(monkeypatch, [sess(key="helper-crown", label="crown")])
    f.status = (200, {"board": {"doing": "teste den Import"}, "state": "arbeitet"})
    w = f.watcher(cmd=None, interval_flag=5)
    assert w.tick() == W.OK and f.posts == []           # baseline, nothing yet
    f.t += 200
    w.tick()
    assert f.posts == []                                # not longer than the interval
    f.t += 200
    w.tick()
    b = [p for p in f.posts if p[0] == "board"]
    assert len(b) == 1 and b[0][1] == "crown (Wächter)"
    assert b[0][2]["s"] == "keine Meldung seit 6 Min, letzter Stand: teste den Import"
    assert "state" not in b[0][2]["body"] and len(b[0][2]["body"]["meta"]["doing"]["faq"]) == 3
    f.t += 60
    w.tick()
    assert len([p for p in f.posts if p[0] == "board"]) == 1   # once per interval
    f.status = (200, {"board": {"doing": "fertig mit dem Import"}, "state": "arbeitet"})
    f.t += 60
    w.tick()
    last = [p for p in f.posts if p[0] == "board"][-1]
    assert last[1] == "crown (Wächter)" and last[2]["body"] == {}
    assert W.sub_boards() == []


def test_unobservable_old_server_404_skips_silently(monkeypatch):
    f = Fake(monkeypatch, [sess(key="helper-crown", label="crown")])
    w = f.watcher(cmd=None, interval_flag=1)
    w.tick()
    assert f.posts == [] and w.status_off_until > f.t


# ----- 0.33.0 Part B: Kanban maintenance --------------------------------------------------
from voicehook_agent import watchkanban as K


def _iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def _user(text, ts=None, **kw):
    ts = ts or _iso(NOW - 100)
    return json.dumps({"type": "user", "timestamp": ts, "message": {"role": "user", "content": text}, **kw}) + "\n"


def _tp(tmp_path, *texts):
    tp = tmp_path / "s.jsonl"
    tp.write_text("".join(_user(t) for t in texts))
    os.utime(tp, (NOW - 5, NOW - 5))
    return tp


class KB:
    """Fake Kanban server behind watchkanban.call."""

    def __init__(self, monkeypatch):
        self.cards: list[dict] = []
        self.calls: list[tuple[str, str, dict | None]] = []
        self.fail = {}                      # path prefix -> status
        monkeypatch.setattr(K, "call", self.call)

    def call(self, rel, method, path, body=None):
        self.calls.append((method, path, body))
        for pre, st in self.fail.items():
            if path.startswith(pre):
                return st, {}
        if method == "GET":
            return 200, {"states": [], "cards": self.cards}
        if path == "tasks":
            for c in self.cards:
                if c["orders"][0]["wording"] == body["wording"]:
                    return 200, {"nr": c["nr"]}
            nr = len(self.cards) + 1
            self.cards.append({"nr": nr, "title": body["title"], "state": body["state"],
                               "orders": [{"wording": body["wording"]}], "points": []})
            return 201, {"nr": nr}
        if path.endswith("/points"):
            nr = int(path.split("/")[1])
            next(c for c in self.cards if c["nr"] == nr)["points"].append(body["text"])
            return 201, {}
        if path.endswith("/pick"):
            nr = int(path.split("/")[1])
            next(c for c in self.cards if c["nr"] == nr)["state"] = "in_arbeit"
            return 200, {}
        return 200, {}

    def writes(self, suffix=""):
        return [c for c in self.calls if c[0] == "POST" and c[1].endswith(suffix)]


def _kw(f, tp, state_ev="turn", label="Alpha"):
    return f.watcher(cmd=None, interval_flag=5), [sess(label=label, ev=state_ev, ev_at=NOW - 5, tp=str(tp))]


def test_new_order_creates_card_with_wording_and_assignee(monkeypatch, tmp_path):
    kb = KB(monkeypatch)
    tp = _tp(tmp_path, "Baue die Kartenansicht fertig. Danach den Export.")
    f = Fake(monkeypatch)
    w, f.sessions = _kw(f, tp)
    w.tick()
    (_m, _path, body), = kb.writes("tasks")
    assert body["from"] == "Alpha" and body["assignee"] == "Alpha" and body["state"] == "in_arbeit"
    assert body["wording"] == "Baue die Kartenansicht fertig. Danach den Export."
    assert body["title"] == "Baue die Kartenansicht fertig."          # no model: first sentence
    # the card nr lands in the doing meta of the board post
    board = [p for p in f.posts if p[0] == "board"][-1][2]["body"]
    assert board["meta"]["doing"]["card"] == 1


def test_idle_session_gets_beauftragt(monkeypatch, tmp_path):
    kb = KB(monkeypatch)
    tp = _tp(tmp_path, "Mach bitte den Import")
    os.utime(tp, (NOW - 3000, NOW - 3000))
    f = Fake(monkeypatch)
    w, f.sessions = _kw(f, tp, "stop")
    f.sessions[0]["ev_at"] = NOW - 3000
    w.tick()
    assert kb.writes("tasks")[0][2]["state"] == "beauftragt"


def test_cursor_prevents_duplicates_across_ticks_and_restart(monkeypatch, tmp_path):
    kb = KB(monkeypatch)
    tp = _tp(tmp_path, "Erster Auftrag hier")
    f = Fake(monkeypatch)
    w, f.sessions = _kw(f, tp)
    w.tick()
    f.t += 50
    w.tick()
    w2 = f.watcher(cmd=None, interval_flag=5)      # a restarted watcher: cursor is on disk
    f.t += 50
    w2.tick()
    assert len(kb.writes("tasks")) == 1
    with open(tp, "a") as fh:
        fh.write(_user("Zweiter Auftrag da"))
    f.t += 50
    w2.tick()
    assert len(kb.writes("tasks")) == 2


def test_second_order_classified_sub_adds_point(monkeypatch, tmp_path):
    kb = KB(monkeypatch)
    tp = _tp(tmp_path, "Baue die Kartenansicht")
    f = Fake(monkeypatch)
    w, f.sessions = _kw(f, tp)
    w.tick()
    w.cmd = ["fake-model"]
    seen = []

    def run(cmd, input=None, **kw):
        seen.append((cmd, input))
        class R:
            returncode = 0
            stdout = json.dumps({"result": '{"action":"sub","nr":1,"title":"Farben anpassen"}',
                                 "usage": {"input_tokens": 100, "output_tokens": 10}})
        return R()
    monkeypatch.setattr(W.subprocess, "run", run)
    with open(tp, "a") as fh:
        fh.write(_user("Und die Farben der Karten anpassen"))
    f.t += 50
    w.tick()
    (_m, path, body), = kb.writes("/points")
    assert path == "tasks/1/points" and body["text"] == "Farben anpassen"
    assert body["wording"] == "Und die Farben der Karten anpassen"
    assert "1: Baue die Kartenansicht" in seen[0][1] and w.used(f.t) == 110
    assert len(kb.writes("tasks")) == 1


def test_model_garbage_or_cap_reached_falls_back_to_new(monkeypatch, tmp_path):
    kb = KB(monkeypatch)
    tp = _tp(tmp_path, "Erster Auftrag hier", "Zweiter Auftrag hier")
    f = Fake(monkeypatch)
    f.sessions = [sess(ev="turn", ev_at=NOW - 5, tp=str(tp))]
    monkeypatch.setattr(W.subprocess, "run", f.model(sentence="kein json", tokens=5000))
    w = f.watcher(tokens=5000, interval_flag=5)      # one call uses the whole cap
    w.tick()
    assert len(f.cmd_calls) == 1                     # cap reached after the first order
    assert [c[2]["title"] for c in kb.writes("tasks")] == ["Erster Auftrag hier", "Zweiter Auftrag hier"]


def test_at_most_five_orders_per_tick_rest_next_tick(monkeypatch, tmp_path):
    kb = KB(monkeypatch)
    tp = _tp(tmp_path, *[f"Auftrag Nummer {i} bitte" for i in range(7)])
    f = Fake(monkeypatch)
    w, f.sessions = _kw(f, tp)
    w.tick()
    assert len(kb.writes("tasks")) == 5
    f.t += 50
    w.tick()
    assert len(kb.writes("tasks")) == 7


def test_non_orders_are_skipped_router_kept(monkeypatch, tmp_path):
    kb = KB(monkeypatch)
    tp = tmp_path / "s.jsonl"
    tool = json.dumps({"type": "user", "message": {"content": [{"type": "tool_result", "content": "x y z"}]}}) + "\n"
    tp.write_text(tool + _user("<system-reminder>hallo welt</system-reminder>") + _user("ok")
                  + _user("[Router] Oliver sagt: bitte den Build reparieren") + _user("x y", isMeta=True)
                  + _user("hinweis", isSidechain=True))
    os.utime(tp, (NOW - 5, NOW - 5))
    f = Fake(monkeypatch)
    w, f.sessions = _kw(f, tp)
    w.tick()
    ws = [c[2]["wording"] for c in kb.writes("tasks")]
    assert ws == ["[Router] Oliver sagt: bitte den Build reparieren"]


def test_old_orders_before_first_sight_are_ignored_and_long_wording_cut(monkeypatch, tmp_path):
    kb = KB(monkeypatch)
    tp = tmp_path / "s.jsonl"
    long = ("Das ist ein langer Satz. " * 100).strip()
    tp.write_text(_user("Uralter Auftrag hier", ts="2020-01-01T00:00:00Z") + _user(long))
    os.utime(tp, (NOW - 5, NOW - 5))
    f = Fake(monkeypatch)
    w, f.sessions = _kw(f, tp)
    w.tick()
    ws = [c[2]["wording"] for c in kb.writes("tasks")]
    assert len(ws) == 1 and len(ws[0]) <= K.WORDING_MAX and ws[0].endswith("Satz.")


def test_pick_sets_in_arbeit_and_never_done(monkeypatch, tmp_path):
    kb = KB(monkeypatch)
    tp = _tp(tmp_path, "Baue die Kartenansicht")
    f = Fake(monkeypatch)
    w, f.sessions = _kw(f, tp)
    w.tick()
    kb.cards[0]["state"] = "beauftragt"              # someone put it back
    w.mem[f.sessions[0]["key"]]["card_state"] = None
    f.t += 50
    w.tick()
    assert [c[1] for c in kb.writes("/pick")] == ["tasks/1/pick"]
    assert not kb.writes("/done")


def test_decision_of_session_is_linked_to_card(monkeypatch, tmp_path):
    kb = KB(monkeypatch)
    tp = _tp(tmp_path, "Baue die Kartenansicht")
    f = Fake(monkeypatch)
    w, f.sessions = _kw(f, tp)
    _slot(skey=f.sessions[0]["key"], at=NOW - 10, dec="d777", label="Alpha")
    w.tick()
    assert [(c[1], c[2]["decision"]) for c in kb.writes("/link")] == [("tasks/1/link", "d777")]
    f.t += 50
    w.tick()
    assert len(kb.writes("/link")) == 1              # linked once


def test_old_server_404_on_tasks_skips_silently_and_retries_later(monkeypatch, tmp_path):
    kb = KB(monkeypatch)
    kb.fail["tasks"] = 404
    tp = _tp(tmp_path, "Baue die Kartenansicht")
    f = Fake(monkeypatch)
    w, f.sessions = _kw(f, tp)
    assert w.tick() == W.OK
    assert [p for p in f.posts if p[0] == "board"]     # the status duty is unaffected
    n = len(kb.calls)
    f.t += 50
    w.tick()
    assert len(kb.calls) == n                          # not asked again before the recheck
    kb.fail = {}
    f.t += W.NOT_FOUND_RECHECK_S
    f.rel = _relay(expires=f.t + 3600)
    w.tick()
    assert len(kb.writes("tasks")) == 1                # cursor kept: nothing lost


def test_server_error_keeps_cursor_and_logs_no_text(monkeypatch, tmp_path):
    kb = KB(monkeypatch)
    kb.fail["tasks"] = 500
    tp = _tp(tmp_path, "Geheimer Auftrag Wortlaut")
    f = Fake(monkeypatch)
    w, f.sessions = _kw(f, tp)
    w.tick()
    kb.fail = {}
    f.t += 50
    w.tick()
    assert len(kb.writes("tasks")) >= 1 and kb.cards[0]["orders"][0]["wording"] == "Geheimer Auftrag Wortlaut"
    logtxt = (W.vact.state_root() / W.LOG_NAME).read_text()
    assert "Geheimer" not in logtxt and "kanban" in logtxt


def test_cursor_file_is_private(monkeypatch, tmp_path):
    KB(monkeypatch)
    tp = _tp(tmp_path, "Baue die Kartenansicht")
    f = Fake(monkeypatch)
    w, f.sessions = _kw(f, tp)
    w.tick()
    assert oct((W.vact.state_root() / K.CURSOR_NAME).stat().st_mode & 0o777) == "0o600"


def test_clean_meta_accepts_card_only_as_positive_int():
    from voicehook_agent import relay
    assert relay.clean_meta({"card": 3}) == {"card": 3}
    assert relay.clean_meta({"card": 0}) is None and relay.clean_meta({"card": True}) is None


# ----- 0.33.0 Part C: active sessions outside the call ------------------------------------
from voicehook_agent import watchjoin as J


def _cand(key="u1", label="Fremd", tp="/x/proj/u1.jsonl", at=NOW - 10):
    return {"key": key, "label": label, "agent": "claude", "tp": tp, "active_at": at}


def test_unbound_active_session_proposed_once_with_label_and_host_only(monkeypatch):
    f = Fake(monkeypatch)
    f.unbound = [_cand()]
    w = f.watcher(cmd=None)
    w.tick()
    f.t += 50
    w.tick()
    assert len(f.proposals) == 1
    b = f.proposals[0]
    assert "Fremd" in b["question"] and vmulti.machine_label() in b["question"]
    assert b["options"] == ["Ja", "Nein"] and len(b["faq"]) == 3
    assert "jsonl" not in json.dumps(b) and "/x/proj" not in json.dumps(b)
    logtxt = (W.vact.state_root() / W.LOG_NAME).read_text()
    assert "/x/proj" not in logtxt


def test_ja_covers_session_next_tick_like_a_call_session(monkeypatch, tmp_path):
    KB(monkeypatch)
    tp = _tp(tmp_path, "Baue die Kartenansicht")
    f = Fake(monkeypatch)
    f.unbound = [_cand(key="u1", label="Fremd", tp=str(tp))]
    w = f.watcher(cmd=None, interval_flag=5)
    w.tick()
    assert [p for p in f.posts if p[0] == "board"] == []
    f.answers = [{"type": "decision", "id": "d00000001", "verdict": "option", "option": 0}]
    f.t += 50
    w.tick()
    f.unbound = []                    # now covered: no longer listed as unbound
    f.t += 50
    w.tick()
    labels = {p[1] for p in f.posts if p[0] == "board"}
    assert "Fremd (Wächter)" in labels or "Fremd" in labels
    assert len(f.proposals) == 1


def test_nein_never_again_even_after_inactivity(monkeypatch):
    f = Fake(monkeypatch)
    f.unbound = [_cand()]
    w = f.watcher(cmd=None)
    w.tick()
    f.answers = [{"type": "decision", "id": "d00000001", "verdict": "no"}]
    f.t += 50
    w.tick()
    f.unbound = []
    f.t += 3 * 3600
    f.rel = _relay(expires=f.t + 3600)
    w.tick()
    f.unbound = [_cand(at=f.t)]
    f.t += 50
    w.tick()
    assert len(f.proposals) == 1


def test_repropose_only_after_two_hours_inactive(monkeypatch):
    f = Fake(monkeypatch)
    f.unbound = [_cand()]
    w = f.watcher(cmd=None)
    w.tick()
    f.unbound = []
    f.t += J.REPROPOSE_S + 100
    f.rel = _relay(expires=f.t + 3600)
    w.tick()
    f.unbound = [_cand(at=f.t)]
    f.t += 50
    w.tick()
    assert len(f.proposals) == 2


def test_call_start_one_summary_card_ja_subscribes_all(monkeypatch):
    """0.33.3 (Oliver, new call): at the start ONE card '5 Sessions arbeiten parallel ...
    alle abonnieren?', Ja subscribes every listed session; no single cards meanwhile."""
    f = Fake(monkeypatch)
    f.unbound = [_cand(key=f"u{i}", label=f"S{i}") for i in range(5)]
    w = f.watcher(cmd=None)
    w.tick()
    assert len(f.proposals) == 1
    q = f.proposals[0]["question"]
    assert q.startswith("5 Sessions arbeiten gerade parallel") and "alle abonnieren" in q
    assert "S0" in f.proposals[0]["context"] and "S4" in f.proposals[0]["context"]
    f.t += 50
    w.tick()
    assert len(f.proposals) == 1                       # waits for the answer
    f.answers = [{"type": "decision", "id": "d00000001", "verdict": "yes"}]
    f.t += 50
    w.tick()
    sub = J.load(W.credential())["subscribed"]
    assert {f"u{i}" for i in range(5)} <= set(sub)


def test_after_summary_single_cards_capped_at_three(monkeypatch):
    f = Fake(monkeypatch)
    f.unbound = [_cand(key=f"u{i}", label=f"S{i}") for i in range(2)]
    w = f.watcher(cmd=None)
    w.tick()
    f.answers = [{"type": "decision", "id": "d00000001", "verdict": "no"}]
    f.t += 50
    w.tick()                                           # summary answered Nein: never again
    f.unbound = [_cand(key=f"n{i}", label=f"N{i}") for i in range(5)]
    f.answers = []
    f.t += 50
    w.tick()
    singles = [p for p in f.proposals[1:]]
    assert len(singles) == 3 and all("parallel" not in p["question"] for p in singles)


def test_never_env_filters_label_and_all_env_skips_asking(monkeypatch):
    f = Fake(monkeypatch)
    f.unbound = [_cand(key="a", label="Kunde Mueller"), _cand(key="b", label="Okay")]
    monkeypatch.setenv(J.ENV_NEVER, "kunde, geheim")   # one left after the filter: single card
    f.watcher(cmd=None).tick()
    assert [q["question"].split()[1] for q in f.proposals] == ["Okay"]
    assert J.is_never("Projekt GEHEIM 1", "") and not J.is_never("Okay", "")
    assert J.is_never("x", "/h/.claude/projects/-home-kunde-app/s.jsonl")
    f2 = Fake(monkeypatch)
    f2.unbound = [_cand()]
    monkeypatch.setenv(W.ENV_ALL, "1")
    f2.watcher(cmd=None).tick()
    assert f2.proposals == []


def test_find_unbound_maps_subagents_to_parent(tmp_path):
    root = tmp_path / "projects"
    (root / "p1" / "bound1" / "subagents").mkdir(parents=True)
    (root / "p1" / "free1" / "subagents").mkdir(parents=True)
    for name in ("bound1", "free1"):
        (root / "p1" / f"{name}.jsonl").write_text("{}")
        os.utime(root / "p1" / f"{name}.jsonl", (NOW - 5000, NOW - 5000))   # parent quiet
        (root / "p1" / name / "subagents" / "agent-x.jsonl").write_text("{}")
        os.utime(root / "p1" / name / "subagents" / "agent-x.jsonl", (NOW - 5, NOW - 5))
    bound_tp = str(root / "p1" / "bound1.jsonl")
    got = J.find_unbound(_relay(), NOW, W.ACTIVE_S, claude_root=root, bound_tps={bound_tp})
    assert [g["tp"] for g in got] == [str(root / "p1" / "free1.jsonl")]       # subagent of free1 = free1
    by_hash = J.find_unbound(_relay(), NOW, W.ACTIVE_S, claude_root=root,
                             bound_keys={vmulti.session_hash("bound1"), vmulti.session_hash("free1")})
    assert by_hash == []
    owner = J.find_unbound(_relay(), NOW, W.ACTIVE_S, claude_root=root, bound_tps={bound_tp},
                           ) and J.find_unbound({**_relay(), "owner_session": vmulti.session_hash("free1")},
                                                NOW, W.ACTIVE_S, claude_root=root, bound_tps={bound_tp})
    assert owner == []


def test_subagent_activity_keeps_parent_arbeitet(tmp_path):
    tp = tmp_path / "sess.jsonl"
    tp.write_text("{}")
    os.utime(tp, (NOW - 900, NOW - 900))
    (tmp_path / "sess" / "subagents").mkdir(parents=True)
    sub = tmp_path / "sess" / "subagents" / "a.jsonl"
    sub.write_text("{}")
    os.utime(sub, (NOW - 10, NOW - 10))
    state, _last = W.classify_state(sess(ev="turn", ev_at=NOW - 900, tp=str(tp)), NOW)
    assert state == W.ARBEITET
    state, _ = W.classify_state(sess(ev="stop", ev_at=NOW - 5, tp=str(tp)), NOW)
    assert state == W.STILL                         # a Stop after the subagent wrote wins


def test_opt_ins_end_with_the_call(monkeypatch):
    f = Fake(monkeypatch)
    st = J.load(f.rel)
    st["subscribed"]["k"] = {"label": "X", "tp": "/t.jsonl", "at": NOW}
    J.save(st)
    assert J.subscribed_sessions(f.rel, set())[0]["label"] == "X"
    assert J.subscribed_sessions(_relay(token="tok-" + "b" * 12), set()) == []     # another call
    w = f.watcher(cmd=None)
    f.rel = None
    monkeypatch.setattr(W, "post_board", lambda *a: {"ok": True})
    w.rel = _relay()
    w.shutdown()
    assert not (W.vact.state_root() / J.STATE_NAME).exists()


def test_unbound_label_uses_transcript_cwd_not_bare_session(tmp_path):
    """Live 28.10.: the card read "Session Session arbeitet ..." (no title, cwd unknown)."""
    from voicehook_agent import watchjoin as J
    tp = tmp_path / "abcd1234-0000.jsonl"
    tp.write_text(json.dumps({"type": "user", "cwd": "/home/x/wt-shop", "message": {"content": "hi"}}) + "\n")
    label = J.label_for(tp.stem, str(tp))
    assert label.startswith("wt-shop ") and label != "Session"
    q = J.proposal_body(label, "crown", 0.0, 120.0)["question"]
    assert q.startswith("Session wt-shop ") and "Session Session" not in q
    assert J.proposal_body("Session 1a2b", "crown", 0.0, 120.0)["question"].startswith("Session 1a2b arbeitet")


def test_credential_is_the_newest_call_not_the_first_by_name():
    """Live 28.10.: after a new call the watcher kept the morning credential of the helper
    first by name (Kunde-Talents) and never came along."""
    vmulti.write_helper("Aaa-alt", URL, "tok" + "old1", "room", 3600, owner_session="", now=time.time() - 3000)
    vmulti.write_helper("Zzz-neu", URL, "tok" + "new1", "room", 3600, owner_session="", now=time.time())
    assert W.credential()["helper"] == "Zzz-neu"


def test_watcher_switches_to_a_newer_call(monkeypatch):
    f = Fake(monkeypatch, [])
    w = f.watcher(cmd=None)
    w.tick()
    old = W.call_key(w.rel)
    f.rel = {**f.rel, "token": "tok" + "new2"}          # a newer call's credential appears
    f.t += 50
    w.tick()
    assert W.call_key(w.rel) != old and w.greeted is False
