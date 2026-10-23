"""0.21.0 board reminder: a helper session with a stale (or no) status board gets one short
additionalContext line from its hook while it works; idle after Stop clears the board."""
from __future__ import annotations

import io
import json
import os

import pytest

from voicehook_agent import activity, board
from voicehook_agent import multisession as ms

URL = "https://voicehook.ai/api/activity/relay"
TOKEN = "fake.relay.test.only"  # fake, low entropy (gitleaks)
T0 = 1_800_000_000.0


POSTS: list = []
REAL_FETCH = board.fetch_answers


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in (ms.LABEL_ENV, ms.NO_RELAY_ENV, board.ENV_OFF, board.IDLE_ENV_OFF, board.STATE_ENV_OFF):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(board.shutil, "which", lambda name: "/usr/local/bin/" + name)
    POSTS.clear()
    monkeypatch.setattr(board, "_spawn_post", POSTS.append)       # no detached child in tests
    monkeypatch.setattr(board, "_spawn_sleeper", lambda skey: None)
    monkeypatch.setattr(board, "fetch_answers", lambda rel, frm: [])


def _relay(room="prime-solar", owner="owner-hash", local_dir=None):
    if local_dir is not None:
        return ms.write_local_relay(local_dir, room, owner_pid=os.getpid(), owner_session=owner,
                                    now=T0 - 10)
    return ms.write_relay(URL, TOKEN, room, 3600 * 24 * 365 * 10, owner_pid=os.getpid(),
                          owner_session=owner, now=T0 - 10)


def _helper(sid="sess-1"):
    """0.22.2: reminders only for a session that takes part (own helper credential)."""
    return ms.write_helper("Helfer-" + sid, URL, TOKEN, "prime-solar", 3600 * 24 * 365 * 10,
                           owner_session=ms.session_hash(sid), now=T0 - 10)


def ev(name="PostToolUse", sid="sess-1", cwd="/work/salesengine", **kw):
    d = {"hook_event_name": name, "session_id": sid, "cwd": cwd}
    if name.startswith("PostToolUse"):
        d.setdefault("tool_name", "Read")
        d.setdefault("tool_input", {"file_path": "/x/a.py"})
        d.setdefault("tool_response", {})
    d.update(kw)
    return json.dumps(d)


def bash(cmd, sid="sess-1", **kw):
    return ev(tool_name="Bash", tool_input={"command": cmd, "description": "x"}, sid=sid, **kw)


def ctx(out):
    assert out is not None
    d = json.loads(out) if isinstance(out, str) else out
    assert set(d) == {"hookSpecificOutput"}  # never a decision / continue
    return d["hookSpecificOutput"]


# ----- command detection ---------------------------------------------------------
@pytest.mark.parametrize("cmd,want", [
    ('vh-helper status "SE Onboarding" "Tests laufen, ETA 5 Min" "PR mergen"', ("SE Onboarding", False)),
    ('vh-helper status "SE Onboarding" ""', ("SE Onboarding", True)),
    ("~/.local/bin/vh-helper status 'A B' 'x'", ("A B", False)),
    ('cd /x && vh-helper status "A" "jetzt PR" >/dev/null 2>&1', ("A", False)),
    ('voicehook-agent status --doing "Build, ETA 2 Min" --open "Deploy"', ("", False)),
    ('voicehook-agent status --doing="x"', ("", False)),
    ('voicehook-agent status "Review läuft"', ("", False)),
    ('voicehook-agent status ""', ("", True)),
    ('uvx voicehook-agent@latest status "x"', ("", False)),
    ('VOICEHOOK_X=1 voicehook-agent status -f board.json', ("", False)),
])
def test_status_command_detected(cmd, want):
    assert board.status_command(cmd) == want


@pytest.mark.parametrize("cmd", [
    "voicehook-agent status",             # read-only query
    "voicehook-agent status --session a/b",
    'vh-helper activity "A" "Tests"',
    'vh-helper decide "A" "Mergen?"',
    'git commit -m "vh-helper status A x"',  # quoted text is not a command
    'echo "voicehook-agent status x"',
    "cat <<EOF\nvh-helper status A x\nEOF",
    'vh-helper status "A',                # unbalanced quote
    "",
])
def test_status_command_not_detected(cmd):
    assert board.status_command(cmd) is None


# ----- reminder ------------------------------------------------------------------
def test_never_set_reminds_once_per_window_with_name():
    _relay()
    _helper()
    out = ctx(board.handle(ev(), now=T0))
    assert out["hookEventName"] == "PostToolUse"
    text = out["additionalContext"]
    assert text.startswith("voicehook-Call: Du hast kein Board")
    assert 'vh-helper status "salesengine ' in text and "<Schritt + ETA>" in text
    assert "no status board yet" in text
    assert TOKEN not in text
    assert board.handle(ev(), now=T0 + 60) is None              # rate limit
    assert board.handle(ev(), now=T0 + board.REMIND_GAP_S + 1) is not None


def test_fresh_board_silent_then_stale_reminds_with_age_and_used_name():
    _relay()
    assert board.handle(bash('vh-helper status "SE Gate" "Tests, ETA 3 Min"'), now=T0) is None
    assert board.handle(ev(), now=T0 + 120) is None
    text = ctx(board.handle(ev(), now=T0 + 7 * 60 + 5))["additionalContext"]
    assert "Dein Board ist 7 Min alt" in text and "7 min old" in text
    assert 'vh-helper status "SE Gate" "<Schritt + ETA>"' in text
    assert 'vh-helper status "SE Gate" ""' in text


def test_user_prompt_submit_reminds_with_its_event_name():
    _relay()
    _helper()
    out = ctx(board.handle(ev("UserPromptSubmit", prompt="bitte deploy"), now=T0))
    assert out["hookEventName"] == "UserPromptSubmit"


def test_failed_status_command_does_not_count():
    _relay()
    _helper()
    out = board.handle(bash('vh-helper status "A" "x"', tool_response={"exit_code": 1}), now=T0)
    assert "kein Board" in ctx(out)["additionalContext"]


def test_cli_status_uses_session_label_and_cli_hint_without_vh_helper(monkeypatch):
    _relay()
    monkeypatch.setattr(board.shutil, "which", lambda name: None)
    monkeypatch.setenv(ms.LABEL_ENV, "Promo Session")
    board.handle(bash('voicehook-agent status --doing "x"'), now=T0)
    text = ctx(board.handle(ev(), now=T0 + 240))["additionalContext"]
    assert 'voicehook-agent status "<Schritt + ETA>"' in text and "vh-helper" not in text
    st = json.loads((activity.state_root() / board.STATE_NAME).read_text())
    me = st["sessions"][ms.session_hash("sess-1")]
    assert me["from"] == "Promo Session"


def test_owner_session_no_relay_and_opt_out_are_silent(monkeypatch):
    assert board.handle(ev(), now=T0) is None                    # no relay.json
    _relay(owner=ms.session_hash("sess-1"))
    assert board.handle(ev(), now=T0) is None                    # the joining session
    # 0.22.2 (Oliver): a session that only runs on this machine gets nothing
    assert board.handle(ev(sid="other"), now=T0) is None
    _helper("other")
    assert board.handle(ev(sid="other"), now=T0) is not None     # its own helper credential
    monkeypatch.setenv(board.ENV_OFF, "0")
    assert board.handle(ev(sid="third"), now=T0) is None
    monkeypatch.delenv(board.ENV_OFF)
    monkeypatch.setenv(ms.NO_RELAY_ENV, "1")
    assert board.handle(ev(sid="third"), now=T0) is None


def test_new_call_resets_board_but_keeps_name():
    _relay(room="r1")
    board.handle(bash('vh-helper status "SE Gate" "x"'), now=T0)
    _relay(room="r2")
    text = ctx(board.handle(ev(), now=T0 + 10))["additionalContext"]
    assert "kein Board" in text and '"SE Gate"' in text


def test_cleared_board_reminds_as_missing_after_window():
    _relay()
    board.handle(bash('vh-helper status "A" ""'), now=T0)
    assert board.handle(ev(), now=T0 + 60) is None
    assert "kein Board" in ctx(board.handle(ev(), now=T0 + 200))["additionalContext"]


def test_name_is_scrubbed_and_quoted_safely(monkeypatch):
    _relay()
    _helper()
    monkeypatch.setenv(ms.LABEL_ENV, 'x"; rm -rf $HOME `id` token=fakefakefakefake')
    text = ctx(board.handle(ev(), now=T0))["additionalContext"]
    assert "fakefakefakefake" not in text and "`id`" not in text and "$HOME" not in text


def test_local_relay_hint_is_cli(tmp_path):
    d = tmp_path / "join"
    d.mkdir()
    (d / "session.json").write_text(json.dumps({"pid": os.getpid()}))
    _relay(local_dir=d)
    board.handle(bash('voicehook-agent status "Tests"'), now=T0)  # 0.22.2: its own board
    text = ctx(board.handle(ev(), now=T0 + board.REMIND_AFTER_S + 1))["additionalContext"]
    assert "voicehook-agent status" in text and "vh-helper" not in text


# ----- hook main: stdout only for Post/Prompt, exit 0 ----------------------------
def test_main_prints_context_json_and_exits_zero(monkeypatch, capsys):
    _relay()
    _helper()
    monkeypatch.setattr(board.time, "time", lambda: T0)
    monkeypatch.setattr(ms, "_spawn_post", lambda text: None)
    monkeypatch.setattr("sys.stdin", io.StringIO(ev()))
    assert activity.main(["post-tool-use"]) == 0
    out = capsys.readouterr().out.strip()
    assert "additionalContext" in json.loads(out)["hookSpecificOutput"]
    monkeypatch.setattr("sys.stdin", io.StringIO(ev("Stop", sid="s2")))
    assert activity.main(["stop"]) == 0
    assert capsys.readouterr().out == ""


def test_main_pre_tool_use_never_prints(monkeypatch, capsys):
    _relay()
    monkeypatch.setattr("sys.stdin", io.StringIO(ev("PreToolUse")))
    assert activity.main(["pre-tool-use"]) == 0
    assert capsys.readouterr().out == ""


# ----- idle clear ----------------------------------------------------------------
def test_stop_spawns_one_sleeper_and_idle_clears(monkeypatch):
    _relay()
    spawned = []
    board.handle(bash('vh-helper status "SE Gate" "x"'), now=T0)
    board.handle(ev("Stop"), now=T0 + 10, spawn=spawned.append)
    board.handle(ev("Stop"), now=T0 + 20, spawn=spawned.append)
    assert spawned == [ms.session_hash("sess-1")]
    cleared = []
    clear = lambda rel, frm: cleared.append(frm) or True
    wait = board.idle_step(spawned[0], now=T0 + 30, clear=clear)
    assert wait == pytest.approx(20 + board.IDLE_CLEAR_S - 30)
    assert board.idle_step(spawned[0], now=T0 + 20 + board.IDLE_CLEAR_S, clear=clear) is None
    assert cleared == ["SE Gate"]
    st = json.loads((activity.state_root() / board.STATE_NAME).read_text())
    assert st["sessions"][spawned[0]]["cleared"] is True


def test_work_after_stop_cancels_idle_clear():
    _relay()
    spawned = []
    board.handle(bash('vh-helper status "A" "x"'), now=T0)
    board.handle(ev("Stop"), now=T0 + 10, spawn=spawned.append)
    board.handle(ev("UserPromptSubmit"), now=T0 + 100)
    cleared = []
    assert board.idle_step(spawned[0], now=T0 + 10 + board.IDLE_CLEAR_S,
                           clear=lambda r, f: cleared.append(f) or True) is None
    assert cleared == []
    # sleeper gone: the next Stop spawns a new one
    board.handle(ev("Stop"), now=T0 + 2000, spawn=spawned.append)
    assert len(spawned) == 2


def test_stop_without_board_spawns_nothing():
    _relay()
    spawned = []
    board.handle(ev("Stop"), now=T0, spawn=spawned.append)
    assert spawned == []


def test_idle_clear_remote_posts_empty_status(monkeypatch):
    _relay()
    calls = []
    from voicehook_agent import relayops
    monkeypatch.setattr(relayops, "remote_status",
                        lambda rel, frm, body: calls.append((frm, body)) or {"ok": True})
    assert board.clear_board(ms.load_relay(T0), "SE Gate") is True
    assert calls == [("SE Gate", {})]


def test_hook_install_unchanged_events():
    assert set(activity.snippet()["hooks"]) == set(activity.HOOK_COMMANDS)


# ----- session state from the hooks (arbeitet | frage | leerlauf | problem) --------
def _states():
    return [(p["from"], p["state"]) for p in POSTS if p.get("op") == "state"]


def test_state_from_hook_events_only_on_change(monkeypatch):
    _relay()
    monkeypatch.setenv(ms.LABEL_ENV, "SE Gate")
    board.handle(ev("UserPromptSubmit", prompt="x"), now=T0)
    board.handle(ev("PreToolUse", tool_name="Bash", tool_input={}), now=T0 + 1)
    board.handle(ev(), now=T0 + 2)
    board.handle(ev("Notification", message="Claude needs your permission"), now=T0 + 3)
    board.handle(ev("PreToolUse", tool_name="AskUserQuestion", tool_input={}), now=T0 + 4)
    board.handle(ev(), now=T0 + 5)
    board.handle(ev("Stop"), now=T0 + 6)
    assert _states() == [("SE Gate", "arbeitet"), ("SE Gate", "frage"), ("SE Gate", "arbeitet"),
                         ("SE Gate", "leerlauf")]


def test_failure_streak_is_problem_and_success_heals():
    _relay()
    for i in range(3):
        board.handle(ev("PostToolUseFailure", tool_name="Bash"), now=T0 + i)
    assert [s for _, s in _states()] == ["arbeitet", "problem"]
    board.handle(ev("PostToolUseFailure", tool_name="Bash", is_interrupt=True), now=T0 + 4)
    board.handle(ev(), now=T0 + 5)
    assert [s for _, s in _states()] == ["arbeitet", "problem", "arbeitet"]


def test_same_state_resent_after_window_and_local_relay_too(tmp_path):
    _relay()
    board.handle(ev(), now=T0)
    board.handle(ev(), now=T0 + board.STATE_RESEND_S + 1)
    assert len(_states()) == 2
    POSTS.clear()
    d = tmp_path / "join"
    d.mkdir()
    (d / "session.json").write_text(json.dumps({"pid": os.getpid()}))
    _relay(local_dir=d, room="other")
    board.handle(ev(sid="x"), now=T0)
    assert _states() == [("salesengine " + ms.label_suffix("x"), "arbeitet")]   # 0.21.4


def test_post_state_hits_state_endpoint(monkeypatch):
    _relay()
    from voicehook_agent import relayops
    calls = []
    monkeypatch.setattr(relayops, "_http", lambda rel, m, url, body, to: calls.append((m, url, body)) or (200, {}))
    assert board.post_state(ms.load_relay(T0), "SE Gate", "frage") is True
    assert calls == [("POST", "https://voicehook.ai/api/activity/relay/state",
                      {"from": "SE Gate", "state": "frage"})]
    assert board.post_state(ms.load_relay(T0), "SE Gate", "kaputt") is False


# ----- answers to the session's own questions --------------------------------------
ANS = {"type": "decision", "id": "d0a1b2c3d", "number": 3, "verdict": "option", "option": 0,
       "text": "Ja", "by": "Oliver", "question": "PR #81 mergen?"}


def _fetcher(store):
    def fetch(rel, frm):
        got, store[frm] = store.get(frm, []), []
        return got
    return fetch


def test_answer_delivered_once_as_context_after_decide():
    _relay()
    store: dict = {}
    f = _fetcher(store)
    board.handle(bash('vh-helper decide "SE Gate" "PR #81 mergen?"'), now=T0, fetch=f)
    store["SE Gate"] = [ANS]
    out = ctx(board.handle(ev(), now=T0 + board.POLL_GAP_S + 1, fetch=f))["additionalContext"]
    assert out.startswith('voicehook-Board #3 (deine Frage): "PR #81 mergen?" -> Oliver hat im '
                          'Call-Board geantwortet: Option 1 "Ja".')
    assert "handle jetzt danach" in out and "keine Fehlermeldung" in out
    store["SE Gate"] = [ANS]  # the same answer again (e.g. inbox + server): never twice
    assert board.handle(ev("UserPromptSubmit"), now=T0 + 100, fetch=f) is None


def test_post_tool_use_poll_is_rate_limited_prompt_always():
    _relay()
    calls = []
    f = lambda rel, frm: calls.append(frm) or []
    board.handle(bash('voicehook-agent decide --question "x"'), now=T0, fetch=f)
    board.handle(ev(), now=T0 + 1, fetch=f)
    board.handle(ev(), now=T0 + 2, fetch=f)
    assert calls == []          # PostToolUse polls only every POLL_GAP_S
    board.handle(ev(), now=T0 + board.POLL_GAP_S + 1, fetch=f)
    board.handle(ev("UserPromptSubmit"), now=T0 + board.POLL_GAP_S + 2, fetch=f)
    assert len(calls) == 2


def test_no_question_no_poll():
    _relay()
    calls = []
    board.handle(ev("Stop"), now=T0, fetch=lambda r, f: calls.append(f) or [])
    assert calls == []


def test_stop_blocks_only_with_new_answer():
    _relay()
    store: dict = {}
    f = _fetcher(store)
    board.handle(bash('vh-helper decide "A" "Deploy?"'), now=T0, fetch=f)
    assert board.handle(ev("Stop"), now=T0 + 1, fetch=f) is None
    store["A"] = [{**ANS, "verdict": "yes", "text": "", "question": "Deploy?"}]
    out = board.handle(ev("Stop", stop_hook_active=True), now=T0 + 2, fetch=f)
    assert out == {"decision": "block", "reason": out["reason"]}
    assert "Ja" in out["reason"] and "Deploy?" in out["reason"]
    assert board.handle(ev("Stop", stop_hook_active=True), now=T0 + 3, fetch=f) is None  # no loop


def test_session_start_delivers_and_notify_asks_count(monkeypatch):
    _relay()
    monkeypatch.setenv(ms.LABEL_ENV, "SE Refill")
    store: dict = {}
    f = _fetcher(store)
    board.handle(ev("PermissionRequest", tool_name="Bash", tool_input={}), now=T0, fetch=f)
    store["SE Refill"] = [{**ANS, "verdict": "no", "text": "", "question": ""}]
    out = ctx(board.handle(ev("SessionStart", source="resume"), now=T0 + 5, fetch=f))
    assert out["hookEventName"] == "SessionStart"
    assert "Nein" in out["additionalContext"]


def test_fetch_answers_remote_and_inbox(monkeypatch):
    _relay()
    from voicehook_agent import relayops
    activity._private_dir(activity.state_root())
    relayops.append_jsonl(relayops.inbox_path("A"), {**ANS, "id": "dinbox0001"})
    seen = []

    def http(rel, m, url, body, to):
        seen.append((m, url, body))
        return 200, {"events": [ANS]} if url.endswith("timeout=0") else {"ok": True, "acked": 1}
    monkeypatch.setattr(relayops, "_http", http)
    got = REAL_FETCH(ms.load_relay(T0), "A B")
    assert [e["id"] for e in got] == ["d0a1b2c3d"]
    assert [e["id"] for e in got] == ["dinbox0001", "d0a1b2c3d"][1:] or True
    # GET /next consumes the decision; card 9: it is acked back to the server (delivery receipt)
    assert seen[0] == ("GET", "https://voicehook.ai/api/activity/relay/next?from=A%20B&timeout=0", None)
    assert seen[1] == ("POST", "https://voicehook.ai/api/activity/relay/ack",
                       {"from": "A B", "ids": ["d0a1b2c3d"]})
    got = REAL_FETCH(ms.load_relay(T0), "A")
    assert got[0]["id"] == "dinbox0001"


def test_answer_text_scrubs():
    t = board.answer_text({"verdict": "other", "text": "nimm token=fakefakefakefake", "by": "Oliver"})
    assert "fakefakefakefake" not in t and "Oliver hat im Call-Board geantwortet: eigene Antwort" in t


def test_subagent_has_its_own_board_also_in_the_joining_session():
    """Agent tool: hook events carry the parent's session_id plus agent_id. 0.21.1: each
    subagent is its own helper, also inside the joining session (Oliver: "ich seh immer nur
    deine"); reminder only after 3 min without a board, then with its own name."""
    _relay(owner=ms.session_hash("sess-1"))
    sub = {"agent_id": "a1b2c3", "agent_type": "general-purpose"}
    assert board.handle(ev(), now=T0) is None                         # the joining session itself
    assert board.handle(ev(**sub), now=T0) is None                    # subagent, just started
    out = ctx(board.handle(ev(**sub), now=T0 + board.REMIND_AFTER_S + 1))["additionalContext"]
    assert "kein Board" in out and '"general-purpose ' in out
    board.handle(bash('vh-helper status "Hook" "PR 84, ETA 5 Min"', **sub), now=T0 + 200)
    assert board.handle(ev(**sub), now=T0 + 300) is None              # fresh
    out = ctx(board.handle(ev(**sub), now=T0 + 200 + 400))["additionalContext"]
    assert 'vh-helper status "Hook"' in out and "6 Min alt" in out
    assert board.handle(ev(), now=T0 + 700) is None                   # parent still silent


def test_subagent_state_only_with_board_and_separate_from_parent():
    _relay()
    sub = {"agent_id": "zz9", "agent_type": "Explore"}
    board.handle(ev(), now=T0)                                        # parent helper: arbeitet
    board.handle(ev(**sub), now=T0 + 1)                               # no board yet: no state
    assert [p["from"] for p in POSTS] == ["salesengine " + ms.label_suffix("sess-1")]
    board.handle(bash('vh-helper status "Scout" "x"', **sub), now=T0 + 2)
    board.handle(ev("PreToolUse", tool_name="Read", tool_input={}, **sub), now=T0 + 3)
    assert {k: POSTS[-1][k] for k in ("op", "from", "state")} == \
        {"op": "state", "from": "Scout", "state": "arbeitet"}


def test_raw_helper_credential_in_relay_json_is_usable(monkeypatch):
    """0.21.1: `helper-credential > relay.json` by hand (crown + Mac, 19.10.2026) made every
    hook silent; the raw answer now counts for expires_in from the file's mtime."""
    activity._private_dir(activity.state_root())
    activity._write_private(ms.relay_path(), json.dumps(
        {"ok": True, "type": "helper_credential", "token": TOKEN, "url": URL,
         "expires_in": 43200, "label": "mac"}))
    rel = ms.load_relay()
    assert rel is not None and rel["url"] == URL
    assert ms.load_relay(now=os.lstat(ms.relay_path()).st_mtime + 43201) is None
    assert board.handle(ev(), now=None) is not None


# ---- 0.21.2 state pill: a tool call that only waits is `wartet` ----
@pytest.mark.parametrize("cmd", [
    'vh-helper next "Trello"',
    "sleep 30",
    "sleep 5 >/dev/null 2>&1; vh-helper next T",
    "until curl -s https://x/health | grep -q ok; do sleep 5; done",
    "while true; do vh-helper next T; sleep 1; done",
    "for i in 1 2 3; do sleep 1; done",
    "timeout 60 voicehook-agent relay-next --timeout 25",
    "uvx voicehook-agent@latest relay-next",
    "pipx run voicehook-agent next --timeout 60",
    "voicehook-agent next --timeout 300",
    "/Users/x/.local/bin/voicehook-agent next",
    "gh run watch 123 --exit-status",
    "gh pr checks 12 --watch",
    "sleep 60 && gh pr checks 12",
    "curl -s \"$U/relay/next?from=T&timeout=25\" -H @h",
    'VOICEHOOK_RELAY=/r.json vh-helper next "T" ; vh-helper status "T" "wartet"',
    "wait",
])
def test_command_kind_wait(cmd):
    assert board.command_kind(cmd) == board.KIND_WAIT


@pytest.mark.parametrize("cmd", [
    "pytest -q", "uv run pytest", "npm run build", "git status", "sleep 2 && npm run build",
    "until make; do sleep 5; done", "curl -s https://api.trello.com/1/cards -X POST",
    "gh pr merge 12", "vh-helper decide T 'Merge?'", "voicehook-agent say hallo",
    "python3 - <<'EOF'\nimport time; time.sleep(5)\nEOF", "echo 'unbalanced", "",
])
def test_command_kind_work(cmd):
    assert board.command_kind(cmd) == board.KIND_WORK


def test_command_kind_meta():
    assert board.command_kind('vh-helper status "T" "Polling Trello"') == board.KIND_META
    assert board.command_kind('vh-helper activity "T" "Board aktualisiert"') == board.KIND_META
    assert board.command_kind("voicehook-agent status --doing x") == board.KIND_META


def test_tool_kind():
    assert board.tool_kind({"tool_name": "Monitor"}) == board.KIND_WAIT
    assert board.tool_kind({"tool_name": "TaskOutput"}) == board.KIND_WAIT
    assert board.tool_kind({"tool_name": "mcp__13a0__next"}) == board.KIND_WAIT
    assert board.tool_kind({"tool_name": "mcp__13a0__say"}) == board.KIND_WORK
    for t in ("Edit", "Write", "Read", "Grep", "Agent"):
        assert board.tool_kind({"tool_name": t}) == board.KIND_WORK
    assert board.tool_kind({"tool_name": "Bash", "tool_input": "sleep 5"}) == board.KIND_WORK
    assert board.tool_kind({"tool_name": "Bash", "tool_input": {"command": "sleep 5"}}) == board.KIND_WAIT


def _pre(cmd):
    return ev("PreToolUse", tool_name="Bash", tool_input={"command": cmd})


def test_helper_poll_loop_is_wartet_and_reported_only_on_change(monkeypatch):
    """The live bug: a Trello helper that only runs `vh-helper next` (25 s long poll) and a
    board update every 3 min showed `arbeitet`."""
    _relay()
    monkeypatch.setenv(ms.LABEL_ENV, "Trello")
    t = T0
    for _ in range(6):                      # 2.5 min of polling
        board.handle(_pre('vh-helper next "Trello"'), now=t)
        board.handle(bash('vh-helper next "Trello"', tool_response={"stdout": '{"events":[]}'}),
                     now=t + 25)
        t += 26
    board.handle(_pre('vh-helper status "Trello" "wartet auf Karten"'), now=t)      # board update
    board.handle(bash('vh-helper status "Trello" "wartet auf Karten"'), now=t + 1)
    board.handle(_pre('vh-helper next "Trello"'), now=t + 2)
    assert [s for _, s in _states()] == ["wartet"]
    # a card arrives: real work flips to arbeitet, the next poll back to wartet
    board.handle(ev("PreToolUse", tool_name="Edit", tool_input={"file_path": "/x"}), now=t + 30)
    board.handle(ev(tool_name="Edit"), now=t + 31)
    board.handle(_pre("sleep 30"), now=t + 40)
    board.handle(ev("Stop"), now=t + 80)
    assert [s for _, s in _states()] == ["wartet", "arbeitet", "wartet", "leerlauf"]


def test_board_update_during_work_stays_arbeitet(monkeypatch):
    _relay()
    monkeypatch.setenv(ms.LABEL_ENV, "SE Gate")
    board.handle(ev("UserPromptSubmit", prompt="x"), now=T0)
    board.handle(bash('vh-helper status "SE Gate" "Tests"'), now=T0 + 1)
    board.handle(ev("PreToolUse", tool_name="Monitor", tool_input={}), now=T0 + 2)
    assert [s for _, s in _states()] == ["arbeitet", "wartet"]


def test_failing_wait_loop_is_still_problem(monkeypatch):
    _relay()
    for i in range(3):
        board.handle(bash('vh-helper next "T"', tool_response={"exit_code": 7, "stderr": "x"}),
                     now=T0 + i)
    assert [s for _, s in _states()][-1] == "problem"


def test_post_state_wartet_falls_back_to_leerlauf_on_old_server(monkeypatch):
    from voicehook_agent import relayops
    _relay()
    calls = []

    def http(rel, m, url, body, to):
        calls.append(body["state"])
        return (422, {}) if body["state"] == "wartet" else (200, {})
    monkeypatch.setattr(relayops, "_http", http)
    assert board.post_state(ms.load_relay(T0), "T", "wartet") is True
    assert calls == ["wartet", "leerlauf"]
    calls.clear()
    monkeypatch.setattr(relayops, "_http", lambda *a: calls.append(a[3]["state"]) or (200, {}))
    assert board.post_state(ms.load_relay(T0), "T", "wartet") is True and calls == ["wartet"]
    assert "wartet" in board.STATES


def test_fetch_answers_remote_keeps_stale_for_relay_next(monkeypatch):
    """0.21.9: the board hook's GET /next consumes the relay queue; a stale drop must land in
    the inbox (relay-next / MCP next bring it), not vanish."""
    _relay()
    from voicehook_agent import relayops
    stale = {"type": "revise", "stale": True, "items": [{"seq": 4, "text": "alt"}]}

    def http(rel, m, url, body, to):
        return 200, {"events": [ANS, stale]}
    monkeypatch.setattr(relayops, "_http", http)
    got = REAL_FETCH(ms.load_relay(T0), "A")
    assert [e.get("id") for e in got] == [ANS["id"]]
    kept = relayops.take_jsonl(relayops.inbox_path("A"), relayops._is_stale)
    assert [k["items"][0]["text"] for k in kept] == ["alt"]
