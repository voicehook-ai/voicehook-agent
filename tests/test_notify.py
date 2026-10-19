"""0.18.0: a Claude Code session waits for the human (permission prompt, AskUserQuestion,
idle) -> decision on the call's Decision Board + feed line (Oliver, live call 19.10.2026:
push notifications of his sessions never showed up in the call)."""
from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys

import pytest

from voicehook_agent import activity, notify
from voicehook_agent import multisession as ms
from voicehook_agent import session as vsession

URL = "https://voicehook.ai/api/activity/relay"
TOKEN = "fake.relay.test.only"  # fake, low entropy (gitleaks)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    for k in ("CLAUDE_CODE_SESSION_ID", ms.LABEL_ENV, ms.MACHINE_LABEL_ENV, ms.NO_RELAY_ENV,
              "VOICEHOOK_SESSION"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("VOICEHOOK_AGENT_HOME", str(tmp_path / "home"))


@pytest.fixture
def relay_lines(monkeypatch):
    """Remote relay of the joining session JOINER; relay activity POSTs captured."""
    ms.write_relay(URL, TOKEN, "room-a", 3600, owner_pid=os.getpid(),
                   owner_session=ms.session_hash("JOINER"))
    sent: list[str] = []
    monkeypatch.setattr(ms, "_spawn_post", sent.append)
    return sent


def _ev(name, sid="other-1", **kw) -> str:
    ev = {"session_id": sid, "hook_event_name": name, "cwd": "/Users/x/voicehook-v4",
          "transcript_path": ""}
    ev.update(kw)
    return json.dumps(ev)


def _perm_notif(sid="other-1", tool="Bash"):
    return _ev("Notification", sid=sid, message=f"Claude needs your permission to use {tool}",
               title="Permission needed", notification_type="permission_prompt")


def _perm_req(sid="other-1", desc="Remove node_modules directory", tool="Bash", **inp):
    return _ev("PermissionRequest", sid=sid, tool_name=tool, permission_mode="default",
               tool_input={"command": "rm -rf node_modules", "description": desc, **inp})


def _ask(sid="other-1", question="Which color should the button get?", options=("Teal", "Grey")):
    return _ev("PreToolUse", sid=sid, tool_name="AskUserQuestion", tool_use_id="toolu_1",
               tool_input={"questions": [{"question": question, "header": "Farbe",
                                          "options": [{"label": o, "description": "x"}
                                                      for o in options],
                                          "multiSelect": False}]})


def _handle(raw, now=1000.0):
    jobs: list[dict] = []
    job = notify.handle(raw, now=now, post=jobs.append)
    assert (job is None) == (jobs == [])
    return job


def _progress(raw, now):
    jobs: list[dict] = []
    job = notify.on_progress(raw, now=now, post=jobs.append)
    assert (job is None) == (jobs == [])
    return job


# ----- classify ----------------------------------------------------------------------
def test_classify_notification_types():
    c = notify.classify(json.loads(_perm_notif()))
    assert c["kind"] == "ask" and c["question"] == "Claude needs your permission to use Bash"
    assert c["text"] == "wartet auf Freigabe: Bash" and c["prio"] == notify.PRIO_GENERIC
    idle = notify.classify(json.loads(_ev("Notification", message="Claude is waiting for your input",
                                          notification_type="idle_prompt")))
    assert idle == {"kind": "wait", "text": "wartet auf Eingabe"}
    for t in ("elicitation_dialog", "agent_needs_input"):
        assert notify.classify(json.loads(_ev("Notification", message="Need input",
                                              notification_type=t)))["kind"] == "ask"
    assert notify.classify(json.loads(_ev("Notification", message="x",
                                          notification_type="elicitation_complete"))) == {"kind": "done"}
    for t in ("auth_success", "agent_completed", "quota_auto_resume_fired"):
        assert notify.classify(json.loads(_ev("Notification", message="x",
                                              notification_type=t))) is None


def test_classify_notification_without_type_by_message():
    c = notify.classify(json.loads(_ev("Notification", message="Claude needs your permission to use Edit")))
    assert c["kind"] == "ask" and c["tool"] == "Edit"
    c = notify.classify(json.loads(_ev("Notification", message="Claude is waiting for your input")))
    assert c["kind"] == "wait"


def test_classify_permission_request_names_tool_and_description():
    c = notify.classify(json.loads(_perm_req()))
    assert c["question"] == "Freigabe für Bash: Remove node_modules directory?"
    assert c["prio"] == notify.PRIO_SPECIFIC and c["tool"] == "Bash" and c["options"] == []
    c = notify.classify(json.loads(_ev("PermissionRequest", tool_name="Edit",
                                       tool_input={"file_path": "/Users/x/secret-proj/src/app.py"})))
    assert c["question"] == "Freigabe für Edit: app.py?"
    c = notify.classify(json.loads(_ev("PermissionRequest", tool_name="mcp__github__merge",
                                       tool_input={"pullNumber": 3})))
    assert c["question"] == "Freigabe für mcp__github__merge?"


def test_question_has_no_paths_urls_or_secrets_and_is_capped():
    raw = ("Darf ich /Users/oliver/kunden/acme/.env nach ~/backup/x.tar kopieren, token=hunter2, "
           "siehe https://intern.example.com/a/b?c=d " + "lang " * 60)
    q = notify.clean_text(raw)
    assert "/Users" not in q and "kunden" not in q and "backup" not in q and "hunter2" not in q
    assert ".env" in q and "x.tar" in q and "/a/b" not in q
    assert re.search(r"siehe intern\.example\.com lang", q)      # the URL keeps only its host
    assert len(q) <= notify.QUESTION_MAX


def test_classify_ask_user_question_options():
    c = notify.classify(json.loads(_ask(options=("Teal", "Grey", "Mint"))))
    assert c["question"] == "Which color should the button get?"
    assert c["options"] == ["Teal", "Grey", "Mint"] and c["text"] == "hat eine Frage"
    c = notify.classify(json.loads(_ask(options=("Ant", "Bee", "Cat", "Dog"))))
    assert c["options"] == ["Ant", "Bee", "Cat"]               # 0.21.3: > 3: the 4th in the question
    assert c["question"] == "Which color should the button get? (Dog)"
    assert "More options (answer as free text): Dog" in c["items"][0]["context"]
    assert notify.classify(json.loads(_ev("PreToolUse", tool_name="Bash",
                                          tool_input={"command": "ls"}))) is None


# ----- helper session over the relay ---------------------------------------------------
def test_helper_permission_becomes_decision_and_feed_line(relay_lines, monkeypatch):
    monkeypatch.setenv(ms.LABEL_ENV, "SE Refill")
    job = _handle(_perm_req())
    assert job == {"op": "create", "route": "relay", "skey": ms.session_hash("other-1"),
                   "question": "Freigabe für Bash: Remove node_modules directory?",
                   "options": [], "replace": None, "from": "SE Refill"}
    assert relay_lines == ["SE Refill: wartet auf Freigabe: Bash"]
    state = (activity.state_root() / notify.STATE_NAME).read_text()
    assert "other-1" not in state and "node_modules" not in state   # hashes only


def test_idle_is_only_a_feed_line(relay_lines):
    assert _handle(_ev("Notification", message="Claude is waiting for your input",
                       notification_type="idle_prompt")) is None
    assert relay_lines == ["voicehook-v4 " + ms.label_suffix("other-1") + ": wartet auf Eingabe"]


def test_joining_session_never_asks_through_the_relay(relay_lines):
    assert _handle(_perm_req(sid="JOINER")) is None and relay_lines == []


def test_nothing_without_join_or_relay():
    assert _handle(_perm_req()) is None
    assert not (activity.state_root() / notify.STATE_NAME).exists()
    assert _progress(_ev("Stop"), 1001.0) is None


def test_repeated_push_is_deduped_and_specific_beats_generic(relay_lines):
    assert _handle(_perm_req(), now=1000.0) is not None
    assert _handle(_perm_notif(), now=1001.0) is None             # generic after specific
    assert _handle(_perm_req(), now=1200.0) is None               # the same, still open
    notify.set_id(ms.session_hash("other-2"), "x", "d00000000")   # other session untouched
    assert _handle(_perm_notif(sid="other-2"), now=1000.0)["replace"] is None
    assert notify.set_id(ms.session_hash("other-2"),
                         notify._qkey("Claude needs your permission to use Bash"), "d11111111") is False
    up = _handle(_perm_req(sid="other-2"), now=1002.0)            # specific replaces generic
    assert up["replace"] == "d11111111"


def test_rate_limits(relay_lines):
    assert _handle(_perm_req(desc="eins"), now=1000.0) is not None
    notify.set_id(ms.session_hash("other-1"), notify._qkey("Freigabe für Bash: eins?"), "d1")
    assert _handle(_perm_req(desc="zwei"), now=1005.0) is None    # < ASK_GAP_S
    assert _handle(_perm_req(desc="zwei"), now=1011.0)["replace"] == "d1"
    t = 2000.0
    got = [_handle(_perm_req(sid=f"s{i}"), now=t + i) for i in range(notify.GLOBAL_ASK_MAX + 1)]
    assert all(got[:-1]) and got[-1] is None                       # all sessions together


# ----- the session goes on: decision leaves the board ----------------------------------
def test_progress_deletes_the_open_decision(relay_lines, monkeypatch):
    monkeypatch.setenv(ms.LABEL_ENV, "SE Refill")
    _handle(_perm_req(), now=1000.0)
    sk = ms.session_hash("other-1")
    assert notify.set_id(sk, notify._qkey("Freigabe für Bash: Remove node_modules directory?"),
                         "d0a1b2c3d") is False
    assert _progress(_ev("PostToolUse", tool_name="Read", tool_input={}), 1003.0) is None
    job = _progress(_ev("PostToolUse", tool_name="Bash", tool_input={}), 1004.0)
    assert job == {"op": "delete", "route": "relay", "skey": sk, "delete": "d0a1b2c3d",
                   "from": "SE Refill"}
    assert _progress(_ev("Stop"), 1005.0) is None                  # gone already
    assert _handle(_perm_notif(), now=1006.0) is None              # late push: stale
    assert _handle(_perm_req(desc="next"), now=1020.0) is not None  # a new prompt


def test_progress_before_the_id_arrived_makes_the_child_delete(relay_lines):
    _handle(_ask(), now=1000.0)
    sk = ms.session_hash("other-1")
    assert _progress(_ev("UserPromptSubmit", prompt="egal"), 1001.0) is None  # no id yet
    assert notify.set_id(sk, notify._qkey("Which color should the button get?"), "d9") is True


def test_post_main_creates_replaces_and_deletes(monkeypatch, relay_lines):
    calls: list[dict] = []

    def fake(job, body):
        calls.append(body)
        return {"ok": True, "id": "dnew00001", "number": 4} if "question" in body else {"ok": True}
    monkeypatch.setattr(notify, "_decide", fake)
    job = _handle(_ask(options=("Teal", "Grey")), now=1000.0)
    job["replace"] = "dold00001"
    assert notify.post_main(json.dumps(job)) == 0
    assert calls == [{"id": "dold00001", "delete": True},
                     {"question": "Which color should the button get?", "options": ["Teal", "Grey"],
                      "context": "Topic: Farbe; Teal: x; Grey: x"}]
    st = json.loads((activity.state_root() / notify.STATE_NAME).read_text())
    assert st["sessions"][ms.session_hash("other-1")]["open"]["id"] == "dnew00001"
    calls.clear()
    assert notify.post_main(json.dumps({"op": "delete", "route": "relay", "delete": "bad id!"})) == 0
    assert calls == []                                             # never a malformed id


def test_relay_decide_body_is_valid_for_the_join():
    from voicehook_agent import relayops
    assert relayops._valid_decide({"question": "Freigabe für Bash?"}) == {"question": "Freigabe für Bash?"}
    assert relayops._valid_decide({"id": "d1", "delete": True}) == {"id": "d1", "delete": True}


# ----- the joining session itself: own socket + own activity.log -----------------------
def _own_join(owner: str):
    d = vsession.session_dir("room-a", "claude-x")
    d.mkdir(parents=True)
    vsession.write_info(d, {"pid": os.getpid(), "room": "room-a", "owner_session": owner})
    return d


def test_joining_session_asks_over_its_own_join():
    d = _own_join(ms.session_hash("JOINER"))
    job = _handle(_perm_notif(sid="JOINER"), now=1000.0)
    assert job["route"] == "own" and job["dir"] == str(d) and "from" not in job
    lines = activity.read_tail(d / activity.ACTIVITY_NAME)
    assert len(lines) == 1 and lines[0].endswith(" Notification: wartet auf Freigabe: Bash")
    _handle(_perm_notif(sid="JOINER"), now=1001.0)
    assert len(activity.read_tail(d / activity.ACTIVITY_NAME)) == 1   # deduped
    assert _handle(_perm_notif(sid="other-9"), now=1002.0) is None    # another session: no relay


def test_own_route_decide_goes_to_the_control_socket(monkeypatch):
    d = _own_join(ms.session_hash("JOINER"))
    seen = []
    monkeypatch.setattr(notify, "_own_socket", lambda dir_: d / "ctl.sock")
    monkeypatch.setattr(vsession, "request",
                        lambda sock, obj, timeout=None: seen.append(obj) or {"ok": True, "id": "d5"})
    res = notify._decide({"route": "own", "dir": str(d)}, {"question": "Freigabe für Bash?"})
    assert res["id"] == "d5" and seen == [{"cmd": "decide", "question": "Freigabe für Bash?"}]


# ----- hook process: silent, exit 0, nothing on stdout ---------------------------------
@pytest.mark.parametrize("cmd,raw", [("notification", _perm_notif()),
                                     ("permission-request", _perm_req())])
def test_hook_main_prints_nothing(cmd, raw, monkeypatch, capsys, relay_lines):
    spawned = []
    monkeypatch.setattr(notify, "spawn", spawned.append)
    monkeypatch.setattr(sys, "stdin", io.StringIO(raw))
    assert activity.main([cmd]) == 0
    out = capsys.readouterr()
    assert out.out == "" and out.err == "" and len(spawned) == 1


def test_pre_tool_use_ask_user_question_asks(monkeypatch, relay_lines):
    spawned = []
    monkeypatch.setattr(notify, "spawn", spawned.append)
    monkeypatch.setattr(sys, "stdin", io.StringIO(_ask()))
    assert activity.main(["pre-tool-use"]) == 0
    assert spawned[0]["question"] == "Which color should the button get?"


def test_permission_request_real_process_stdout_empty():
    """PermissionRequest: any stdout could become a decision. It must stay empty."""
    r = subprocess.run([sys.executable, "-m", "voicehook_agent.activity", "permission-request"],
                       input=_perm_req(), capture_output=True, text=True, timeout=30,
                       env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}, check=False)
    assert r.returncode == 0 and r.stdout == "" and r.stderr == ""


def test_install_adds_notification_hooks_to_older_install(tmp_path):
    p = tmp_path / "settings.json"
    old = {ev: [activity.hook_entry(ev)] for ev in ("PreToolUse", "PostToolUse",
                                                     "PostToolUseFailure", "UserPromptSubmit", "Stop")}
    p.write_text(json.dumps({"hooks": old}))
    rc, msg = activity.install(p)
    assert rc == 0 and "Notification" in msg and "PermissionRequest" in msg
    data = json.loads(p.read_text())["hooks"]
    assert data["Notification"] == [activity.hook_entry("Notification")]
    assert data["PermissionRequest"][0]["hooks"][0]["command"] == \
        "voicehook-agent-hook permission-request || true"
    assert activity.install(p)[1].startswith("already installed")


def test_real_hook_process_posts_helper_decision_to_relay(tmp_path):
    """End to end: hook process -> detached child -> POST <relay>/decide with Bearer + from."""
    import http.server
    import threading
    import time

    got: list[tuple[str, str, dict]] = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["content-length"])) or b"{}")
            got.append((self.path, self.headers.get("authorization", ""), body))
            out = json.dumps({"ok": True, "id": "d0a1b2c3d", "number": 1}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/api/activity/relay"
        ms.write_relay(url, TOKEN, "room-a", 3600, owner_pid=os.getpid(),
                       owner_session=ms.session_hash("JOINER"))
        env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path), ms.LABEL_ENV: "SE Refill",
               "VOICEHOOK_BOARD_REMIND": "0"}  # 0.21.0 board reminder has its own tests
        r = subprocess.run([sys.executable, "-m", "voicehook_agent.activity", "permission-request"],
                           input=_perm_req(), capture_output=True, text=True, timeout=30,
                           env=env, check=False)
        assert r.returncode == 0 and r.stdout == ""
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not any(p.endswith("/decide") for p, _, _ in got):
            time.sleep(0.1)
        dec = [g for g in got if g[0].endswith("/decide")]
        assert dec, got
        path, auth, body = dec[0]
        assert path == "/api/activity/relay/decide" and auth == f"Bearer {TOKEN}"
        assert body == {"from": "SE Refill",
                        "question": "Freigabe für Bash: Remove node_modules directory?"}
        st = json.loads((activity.state_root() / notify.STATE_NAME).read_text())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and \
                not st["sessions"][ms.session_hash("other-1")]["open"].get("id"):
            time.sleep(0.1)
            st = json.loads((activity.state_root() / notify.STATE_NAME).read_text())
        assert st["sessions"][ms.session_hash("other-1")]["open"]["id"] == "d0a1b2c3d"
        # the tool ran (approved in the session): the decision leaves the board
        r = subprocess.run([sys.executable, "-m", "voicehook_agent.activity", "post-tool-use"],
                           input=_ev("PostToolUse", tool_name="Bash", tool_input={},
                                     tool_response={"stdout": ""}),
                           capture_output=True, text=True, timeout=30, env=env, check=False)
        assert r.returncode == 0 and r.stdout == ""
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not any(b.get("delete") for _, _, b in got):
            time.sleep(0.1)
        assert {"from": "SE Refill", "id": "d0a1b2c3d", "delete": True} in [b for _, _, b in got]
    finally:
        srv.shutdown()
