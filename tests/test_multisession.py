"""0.15.0 multi-session relay: the joining session writes ~/.voicehook/relay.json, the hooks
of every OTHER Claude Code session of the machine report turn goal, PR, deploy and errors
to the call's activity relay (POST /api/activity/relay, Bearer relay token)."""
from __future__ import annotations

import asyncio
import io
import json
import os
import stat
import subprocess
import sys
import urllib.error

import pytest

from voicehook_agent import activity, cli
from voicehook_agent import multisession as ms

URL = "https://voicehook.ai/api/activity/relay"
TOKEN = "fake.relay.test.only"  # fake, low entropy (gitleaks)
V4 = "voicehook-v4 " + ms.label_suffix("other-1")  # cwd fallback label of sid other-1


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_PID", "VOICEHOOK_OWNER_PID", ms.LABEL_ENV,
              ms.MACHINE_LABEL_ENV, ms.NO_RELAY_ENV, "VOICEHOOK_SESSION"):
        monkeypatch.delenv(k, raising=False)


def _write(**kw):
    args = {"owner_pid": os.getpid(), "owner_session": ms.session_hash("JOINER")}
    args.update(kw)
    return ms.write_relay(URL, TOKEN, "room-a", 3600, **args)


def _ev(name="PostToolUse", sid="other-1", **kw) -> str:
    ev = {"session_id": sid, "hook_event_name": name, "cwd": "/home/x/voicehook-v4",
          "transcript_path": ""}
    ev.update(kw)
    return json.dumps(ev)


def _bash(cmd, out="", sid="other-1", failed=False, desc=None, **kw):
    inp = {"command": cmd}
    if desc:
        inp["description"] = desc
    if failed:
        return _ev("PostToolUseFailure", sid=sid, tool_name="Bash", tool_input=inp,
                   error="Exit code 1 secret stuff", **kw)
    return _ev(sid=sid, tool_name="Bash", tool_input=inp,
               tool_response={"stdout": out, "stderr": ""}, **kw)


def _run(raw, now=None):
    sent = []
    line = ms.handle_event(raw, now=now, post=sent.append)
    assert (line is None) == (sent == [])
    return line


# ----- relay.json -------------------------------------------------------------------
def test_relay_file_private_atomic_and_minimal():
    path = _write()
    st = os.stat(path)
    assert stat.S_IMODE(st.st_mode) == 0o600
    assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700
    data = json.loads(path.read_text())
    assert set(data) == {"url", "token", "room", "expires_at", "owner_pid", "owner_session",
                         "written_at"}
    assert data["owner_session"] != "JOINER" and len(data["owner_session"]) == 16  # hash only
    assert not [p for p in path.parent.iterdir() if p.name.endswith(".tmp")]  # tmp renamed
    assert ms.load_relay()["token"] == TOKEN  # positive control


def test_write_relay_rejects_bad_input():
    with pytest.raises(ValueError):
        ms.write_relay("wss://lk.example/x", TOKEN, "r", 10)
    with pytest.raises(ValueError):
        ms.write_relay(URL, "", "r", 10)
    assert not ms.relay_path().exists()


def test_remove_only_own_file():
    _write(owner_pid=os.getpid())
    assert ms.remove_relay(owner_pid=os.getpid() + 1) is False          # foreign pid
    assert ms.remove_relay(owner_pid=os.getpid(), room="other") is False  # foreign room
    assert ms.relay_path().exists()
    assert ms.remove_relay(owner_pid=os.getpid(), room="room-a") is True
    assert not ms.relay_path().exists()
    _write(owner_pid=0, owner_session=ms.session_hash("S"))
    assert ms.remove_relay(owner_session=ms.session_hash("T")) is False
    assert ms.remove_relay(owner_session=ms.session_hash("S")) is True


def test_reader_ignores_expired_dead_owner_foreign_and_open_files():
    _write()
    assert ms.load_relay() is not None
    assert ms.load_relay(now=10 ** 11) is None                    # expired
    p = _write(owner_pid=2 ** 22 + 12345)                          # owner gone
    assert ms.load_relay() is None
    _write()
    os.chmod(p, 0o644)                                             # readable by others
    assert ms.load_relay() is None
    os.chmod(p, 0o600)
    ms.mark_dead(TOKEN)                                            # 410 seen
    assert ms.load_relay() is None and ms.relay_path().exists()    # not deleted
    _write()                                                       # fresh token clears it
    assert ms.load_relay() is not None


def test_reader_ignores_symlink(tmp_path):
    real = tmp_path / "elsewhere.json"
    real.write_text(json.dumps({"url": URL, "token": TOKEN, "expires_at": 10 ** 11}))
    os.chmod(real, 0o600)
    activity._private_dir(activity.state_root())
    ms.relay_path().symlink_to(real)
    assert ms.load_relay() is None


# ----- hook: silent paths -----------------------------------------------------------
def test_hook_without_relay_file_is_silent(monkeypatch, capsys):
    assert _run(_bash("gh pr create --fill", "https://github.com/o/r/pull/7")) is None
    spawned = []
    monkeypatch.setattr(ms, "_spawn_post", spawned.append)
    monkeypatch.setattr(sys, "stdin", io.StringIO(_ev("Stop")))
    assert activity.main(["stop"]) == 0 and spawned == []
    assert capsys.readouterr().out == ""
    r = subprocess.run([sys.executable, "-m", "voicehook_agent.activity", "user-prompt-submit"],
                       input=_ev("UserPromptSubmit", prompt="fix the tests"), text=True,
                       capture_output=True, timeout=20,
                       env={**os.environ, "VOICEHOOK_STATE_DIR": str(activity.state_root())},
                       check=False)
    assert r.returncode == 0 and r.stdout == "" and r.stderr == ""
    assert not (activity.state_root() / ms.STATE_NAME).exists()


def test_joining_session_does_not_report_twice():
    _write()
    assert _run(_bash("gh pr create", "https://github.com/o/r/pull/7", sid="JOINER")) is None
    assert _run(_bash("gh pr create", "https://github.com/o/r/pull/7", sid="other")) \
        == "voicehook-v4 " + ms.label_suffix("other") + ": PR #7 geöffnet"  # positive control


def test_garbage_is_silent():
    _write()
    for raw in ("", "not json", "[1,2]", json.dumps({"hook_event_name": "PreToolUse"})):
        assert _run(raw) is None


# ----- hook: what is reported -------------------------------------------------------
def test_pr_create_merge_deploy_lines():
    _write()
    assert _run(_bash("gh pr create --title x --body y",
                      "https://github.com/voicehook-ai/voicehook-v4/pull/414\n")) \
        == V4 + ": PR #414 geöffnet"
    assert _run(_bash("gh pr merge 414 --squash", "✓ Squashed and merged")) \
        == V4 + ": PR #414 gemergt"
    assert _run(_bash("gh pr merge https://github.com/o/r/pull/12 --squash", "")) \
        == V4 + ": PR #12 gemergt"
    assert ms._merge_arg("gh pr merge --squash -R o/r2 31 && echo 5") == "31"
    assert ms._merge_arg("gh pr merge --auto && echo 5") == ""
    assert _run(_bash("cd web && p2ai run -e T='X' -- npx wrangler pages deploy dist", "ok")) \
        == V4 + ": Deploy gelaufen"


def test_heredoc_and_quoted_text_is_not_a_command():
    _write()
    heredoc = "git commit -m \"$(cat <<'EOF'\nfix: run ./deploy.sh only via CI\nEOF\n)\""
    assert not ms._is_deploy(heredoc)
    assert _run(_bash(heredoc, "[main 1a2b3c] fix")) is None
    assert not ms._is_deploy("cat > notes.md <<EOF\nnpm run deploy\ngh pr merge 3\nEOF")
    assert not ms._is_deploy("cat <<-X\n\tmake deploy\n\tX\necho ok")
    assert not ms._is_deploy('git commit -m "x" && python -c "import os; os.system(\'make deploy\')"')
    assert _run(_bash('gh pr create --title "fix: gh pr merge 3"', "/pull/8")) \
        == V4 + ": PR #8 geöffnet"
    # still a deploy after the heredoc ends (positive control)
    assert ms._is_deploy("cat <<EOF > x\nhi\nEOF\nnpm run deploy")
    assert ms._is_deploy("p2ai run -e T='X' -- npx wrangler pages deploy dist")


def test_auto_merge_dry_run_and_workflow_trigger():
    _write()
    assert _run(_bash("gh pr merge 41 --auto --squash", "")) == V4 + ": PR #41 Auto-Merge an"
    assert _run(_bash("gh pr merge 41 --disable-auto", "")) is None
    assert _run(_bash("gh pr merge 42 --squash", "")) == V4 + ": PR #42 gemergt"  # control
    assert not ms._is_deploy("wrangler deploy --dry-run")
    assert not ms._is_deploy("kubectl apply -f k.yaml --dry-run=client")
    assert not ms._is_deploy("bash -n scripts/deploy.sh")
    assert not ms._is_deploy("shellcheck scripts/deploy.sh")
    assert ms._is_deploy("bash -e scripts/deploy.sh")  # control: -e runs it
    assert _run(_bash("gh workflow run deploy.yml -f ref=main", "")) == V4 + ": Deploy angestoßen"
    assert _run(_bash("npm run deploy", "ok")) == V4 + ": Deploy gelaufen"


def test_merge_without_number_takes_it_from_output_or_omits_it():
    _write()
    assert _run(_bash("gh pr merge --squash", "✓ Squashed and merged pull request o/r#77 (t)")) \
        == V4 + ": PR #77 gemergt"
    assert _run(_bash("gh pr merge --rebase", "done", sid="other-x")) \
        == "voicehook-v4 " + ms.label_suffix("other-x") + ": PR gemergt"


def test_error_line_never_carries_description():
    _write()
    line = _run(_bash("x", failed=True, desc="Kunde Mueller Daten loeschen"))
    assert line == V4 + ": Fehler bei Bash" and "Mueller" not in line


def test_relay_url_https_only_http_for_localhost():
    for bad in ("http://evil.example/api/activity/relay", "http://localhost.evil.com/x",
                "http://127.0.0.1.nip.io/x", "ftp://x/y"):
        with pytest.raises(ValueError):
            ms.write_relay(bad, TOKEN, "r", 60)
    for ok in ("https://voicehook.ai/api/activity/relay", "http://localhost:8787/x",
               "http://127.0.0.1:9/x"):
        assert ms.write_relay(ok, TOKEN, "r", 60)
    # a file with plain http to another host (old version) is ignored by readers
    ms.write_relay(URL, TOKEN, "r", 60)
    p = ms.relay_path()
    d = json.loads(p.read_text())
    d["url"] = "http://evil.example/x"
    activity._write_private(p, json.dumps(d))
    assert ms.load_relay() is None


def test_write_private_excl_mode_and_stale_tmp(tmp_path):
    target = tmp_path / "f.json"
    tmp = target.with_name(target.name + f".{os.getpid()}.tmp")
    tmp.write_text("stale")
    os.chmod(tmp, 0o644)
    activity._write_private(target, "new")
    assert target.read_text() == "new" and not tmp.exists()
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o600
    # a symlink planted at the tmp name is removed, never followed
    victim = tmp_path / "victim"
    victim.write_text("keep")
    tmp.symlink_to(victim)
    activity._write_private(target, "again")
    assert victim.read_text() == "keep" and target.read_text() == "again"
    old = os.umask(0)
    try:
        activity._write_private(target, "u")
    finally:
        os.umask(old)
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o600


def test_deploy_detection_ignores_reading_commands():
    assert ms._is_deploy("bash scripts/deploy.sh")
    assert ms._is_deploy("npm run deploy")
    assert ms._is_deploy("FOO=1 make deploy")
    assert not ms._is_deploy("grep -rn deploy .github")
    assert not ms._is_deploy("cat scripts/deploy.sh")
    assert not ms._is_deploy("git log --grep deploy")
    assert not ms._is_deploy("pytest tests/test_deploy.py")


def test_other_bash_and_tools_are_not_reported():
    _write()
    assert _run(_bash("pytest -q", "3 passed", desc="Tests laufen lassen")) is None
    assert _run(_ev(tool_name="Edit", tool_input={"file_path": "/a/b.py"})) is None


def test_errors_short_without_error_text():
    _write()
    line = _run(_bash("pytest -q", failed=True, desc="Tests laufen lassen"))
    assert line == V4 + ": Fehler bei Bash"
    assert "secret" not in line and "Exit" not in line
    assert _run(_bash("gh pr merge 9", failed=True)) == V4 + ": PR #9 Merge fehlgeschlagen"
    assert _run(_bash("./deploy.sh", failed=True)) == V4 + ": Deploy fehlgeschlagen"
    assert _run(_ev("PostToolUseFailure", tool_name="Bash", tool_input={"command": "x"},
                    is_interrupt=True)) is None  # Esc by the user
    assert _run(_ev(tool_name="Read", tool_input={"file_path": "/secret/path.txt"},
                    tool_response={"is_error": True})) == V4 + ": Fehler bei Read"


def test_turn_goal_never_leaks_prompt_words():
    _write()
    line = _run(_ev("UserPromptSubmit", prompt="Bitte fix den Bug bei Kunde Müller, dann PR "
                                               "aufmachen und deployen. Passwort hunter2"))
    assert line == V4 + ": neuer Auftrag: PR, Deploy, Bugfix"
    for w in ("Müller", "hunter2", "Kunde", "Bitte"):
        assert w not in line
    assert ms.prompt_goal("hallo") == "neuer Auftrag"
    assert 3 <= len(line.split(": ", 1)[1].replace(",", "").split()) <= 8


def test_stop_line():
    _write()
    assert _run(_ev("Stop")) == V4 + ": Turn fertig"


def test_label_sources(monkeypatch, tmp_path):
    _write()
    tr = tmp_path / "t.jsonl"
    tr.write_text(json.dumps({"type": "user"}) + "\n"
                  + json.dumps({"type": "custom-title", "customTitle": "crown|Team: Delta"}) + "\n"
                  + json.dumps({"type": "ai-title", "aiTitle": "Generated later"}) + "\n")
    assert _run(_ev("Stop", sid="a", transcript_path=str(tr))) == "crown Team Delta: Turn fertig"
    monkeypatch.setenv(ms.LABEL_ENV, "Mac 2")
    assert _run(_ev("Stop", sid="c", transcript_path=str(tr))) == "Mac 2: Turn fertig"
    monkeypatch.delenv(ms.LABEL_ENV)


def test_label_ai_title_when_not_renamed(tmp_path):
    """Claude Code writes its generated session name as {"type":"ai-title","aiTitle":...}."""
    _write()
    tr = tmp_path / "t.jsonl"
    tr.write_text(json.dumps({"type": "ai-title", "aiTitle": "Enrichment Profil Problem",
                              "sessionId": "x"}) + "\n")
    assert _run(_ev("Stop", sid="a", transcript_path=str(tr))) == \
        "Enrichment Profil Problem: Turn fertig"
    # the label sticks for later events whose transcript is unreadable
    assert _run(_bash("gh pr create", "/pull/5", sid="a")) == "Enrichment Profil Problem: PR #5 geöffnet"


def test_label_ignores_nonexistent_session_title_field():
    _write()
    assert _run(_ev("Stop", sid="b", session_title="Multi Agent CLI")) == \
        "voicehook-v4 " + ms.label_suffix("b") + ": Turn fertig"


def test_label_cwd_fallback_tells_sessions_apart_and_is_capped(tmp_path):
    _write()
    a = _run(_ev("Stop", sid="s-1", cwd="/home/oliver"))
    b = _run(_ev("Stop", sid="s-2", cwd="/home/oliver"))
    assert a != b and a.startswith("oliver ") and b.startswith("oliver ")
    suffix = a.split(":")[0].split()[-1]
    assert len(suffix) == 4 and "s-1" not in a
    tr = tmp_path / "t.jsonl"
    tr.write_text(json.dumps({"type": "ai-title", "aiTitle": "Lange Sitzung " * 8}) + "\n")
    lbl = _run(_ev("Stop", sid="s-3", transcript_path=str(tr))).split(":")[0]
    assert lbl == ("Lange Sitzung " * 8)[:40].strip()
    long_cwd = "/x/" + "d" * 80
    lbl = _run(_ev("Stop", sid="s-4", cwd=long_cwd)).split(":")[0]
    assert len(lbl) <= 40 and lbl.endswith(" " + ms.label_suffix("s-4"))


# ----- throttle ---------------------------------------------------------------------
def test_dedupe_and_gap():
    _write()
    t = 1000.0
    assert _run(_bash("gh pr create", "/pull/5"), now=t) is not None
    assert _run(_bash("gh pr create", "/pull/5"), now=t + 30) is None      # same line
    assert _run(_bash("gh pr create", "/pull/5"), now=t + 200) is not None  # after 120 s
    assert _run(_ev("Stop"), now=t + 300) is not None
    assert _run(_ev("UserPromptSubmit", prompt="tests"), now=t + 301) is None  # < 5 s low gap
    assert _run(_ev("UserPromptSubmit", prompt="tests"), now=t + 307) is not None


def test_global_cap_across_sessions():
    _write()
    t = 5000.0
    got = [_run(_ev("Stop", sid=f"s{i}"), now=t + i * 0.1) for i in range(12)]
    assert sum(g is not None for g in got) == ms.GLOBAL_MAX
    # important lines still pass up to the higher cap
    assert _run(_bash("gh pr create", "/pull/1", sid="imp"), now=t + 2) is not None
    assert _run(_ev("Stop", sid="late"), now=t + 20) is not None  # window over


# ----- POST -------------------------------------------------------------------------
class _Resp:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_post_line_fields(monkeypatch):
    _write()
    seen = {}

    def fake(req, timeout):
        seen.update(url=req.full_url, method=req.get_method(), timeout=timeout,
                    auth=req.get_header("Authorization"), body=json.loads(req.data))
        return _Resp(200)
    monkeypatch.setattr("urllib.request.urlopen", fake)
    assert ms.post_line(V4 + ": PR #7 geöffnet") == 200
    assert seen == {"url": URL, "method": "POST", "timeout": ms.POST_TIMEOUT_S,
                    "auth": f"Bearer {TOKEN}", "body": {"text": V4 + ": PR #7 geöffnet"}}
    assert "?" not in seen["url"]  # token never in the URL


@pytest.mark.parametrize("code", [401, 403, 410])
def test_post_gone_marks_dead_then_silent(monkeypatch, code):
    _write()

    def gone(req, timeout):
        raise urllib.error.HTTPError(req.full_url, code, "gone", {}, io.BytesIO(b""))
    monkeypatch.setattr("urllib.request.urlopen", gone)
    assert ms.post_line("x: y") == code
    assert ms.load_relay() is None and ms.relay_path().exists()
    assert _run(_bash("gh pr create", "/pull/3")) is None  # silent from now on


def test_post_network_error_is_silent(monkeypatch):
    _write()

    def boom(req, timeout):
        raise OSError("down")
    monkeypatch.setattr("urllib.request.urlopen", boom)
    assert ms.post_line("x: y") is None
    assert ms.load_relay() is not None  # a network blip does not kill the token


def test_hook_main_spawns_detached_post(monkeypatch):
    _write()
    spawned = []
    monkeypatch.setattr(ms, "_spawn_post", spawned.append)
    monkeypatch.setattr(sys, "stdin", io.StringIO(_bash("gh pr create", "/pull/77")))
    assert activity.main(["post-tool-use"]) == 0
    assert spawned == [V4 + ": PR #77 geöffnet"]


def test_relay_post_child_reads_text_from_stdin(monkeypatch):
    _write()
    got = []
    monkeypatch.setattr(ms, "post_line", got.append)
    assert ms.relay_post_main(json.dumps({"text": "a: b"})) == 0 and got == ["a: b"]
    assert ms.relay_post_main("garbage") == 0


# ----- join side ----------------------------------------------------------------------
def test_mint_relay_request(monkeypatch):
    seen = {}

    class R(_Resp):
        def read(self):
            return json.dumps({"ok": True, "token": TOKEN, "url": URL, "expires_in": 600}).encode()

    def fake(req, timeout):
        seen.update(url=req.full_url, auth=req.get_header("Authorization"),
                    body=json.loads(req.data))
        return R(200)
    monkeypatch.setattr("urllib.request.urlopen", fake)
    monkeypatch.setenv(ms.MACHINE_LABEL_ENV, "crown")
    assert ms.mint_relay("https://voicehook.ai/", "sess-1") == \
        {"token": TOKEN, "url": URL, "expires_in": 600}
    assert seen == {"url": "https://voicehook.ai/api/bridge/activity/relay-token",
                    "auth": "Bearer sess-1", "body": {"label": "crown"}}


def test_machine_label_capped_no_pipe(monkeypatch):
    monkeypatch.setenv(ms.MACHINE_LABEL_ENV, "a|b:" + "x" * 40)
    lbl = ms.machine_label()
    assert len(lbl) <= ms.MACHINE_LABEL_MAX and "|" not in lbl and ":" not in lbl


def test_bridge_session_save_writes_relay_and_clear_removes(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "JOINER")
    monkeypatch.setattr(ms, "mint_relay", lambda base, sess, label=None:
                        {"token": TOKEN, "url": URL, "expires_in": 600})
    ans = tmp_path / "join.json"
    ans.write_text(json.dumps({"session": "sess-1", "room": "room-a"}))
    with pytest.raises(SystemExit) as e:
        cli.main(["bridge-session", "--save", str(ans), "--base", "https://voicehook.ai"])
    assert e.value.code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["relay"] == str(ms.relay_path()) and TOKEN not in json.dumps(out)
    data = json.loads(ms.relay_path().read_text())
    assert data["owner_session"] == ms.session_hash("JOINER") and data["room"] == "room-a"
    # the joining session itself stays silent, another one reports
    assert _run(_ev("Stop", sid="JOINER")) is None
    assert _run(_ev("Stop", sid="x")) is not None
    with pytest.raises(SystemExit):
        cli.main(["bridge-session", "--clear"])
    assert not ms.relay_path().exists()


def test_bridge_session_no_relay_flag(monkeypatch, tmp_path, capsys):
    called = []
    monkeypatch.setattr(ms, "mint_relay", lambda *a, **k: called.append(1))
    ans = tmp_path / "join.json"
    ans.write_text(json.dumps({"session": "sess-1"}))
    with pytest.raises(SystemExit):
        cli.main(["bridge-session", "--save", str(ans), "--no-relay"])
    assert called == [] and not ms.relay_path().exists()


def test_publish_relay_bridge_and_webrtc(monkeypatch, capsys):
    class Room:
        async def mint_relay(self, label):
            return {"token": TOKEN, "url": URL, "expires_in": 60}
    asyncio.run(cli._publish_relay(Room(), None, "room-a", True))
    assert ms.load_relay()["room"] == "room-a"
    assert TOKEN not in capsys.readouterr().out
    ms.remove_relay(owner_pid=os.getpid())
    # WebRTC: the LiveKit answer's own token/url never count as a relay token
    asyncio.run(cli._publish_relay(object(), {"token": "lk-jwt", "url": "wss://lk"}, "r", True))
    assert not ms.relay_path().exists()
    asyncio.run(cli._publish_relay(object(), {"token": "lk", "url": "wss://lk",
                                              "relay": {"token": TOKEN, "url": URL}}, "r", True))
    assert ms.load_relay()["token"] == TOKEN


# --------------------------------------------------------------------------- #
# detached child: never imports modules from the reporting session's cwd
# --------------------------------------------------------------------------- #
def _plant(tmp_path):
    proj = tmp_path / "untrusted-repo"
    proj.mkdir()
    marker = tmp_path / "PWNED"
    for mod in ("json", "hashlib", "urllib"):
        (proj / f"{mod}.py").write_text(f"open({str(marker)!r}, 'a').write('{mod}')\n")
    return proj, marker


def test_old_dash_m_child_would_import_from_cwd(tmp_path):
    """Control: the plant works, `python -m` from that cwd runs it."""
    proj, marker = _plant(tmp_path)
    subprocess.run([sys.executable, "-m", "voicehook_agent.activity", "relay-post"],
                   check=False, input=b"{}", cwd=proj, timeout=30, capture_output=True,
                   env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)})
    assert marker.exists()


def test_spawn_post_child_isolated_from_cwd_and_still_posts(tmp_path, monkeypatch):
    import http.server
    import threading
    got = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            got.append(json.loads(self.rfile.read(int(self.headers["content-length"]))))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        ms.write_relay(f"http://127.0.0.1:{srv.server_port}/api/activity/relay", TOKEN, "room-a",
                       3600, owner_pid=os.getpid(), owner_session="x")
        proj, marker = _plant(tmp_path)
        monkeypatch.chdir(proj)
        procs = []
        real = subprocess.Popen

        def spy(argv, **kw):
            p = real(argv, **kw)
            procs.append((argv, kw, p))
            return p
        monkeypatch.setattr(subprocess, "Popen", spy)
        ms._spawn_post("Session: Turn fertig")
        argv, kw, p = procs[0]
        p.wait(timeout=30)
    finally:
        srv.shutdown()
    assert argv[1] == "-I" and "-m" not in argv
    assert kw["cwd"] == str(activity.state_root())
    assert not marker.exists()
    assert got == [{"text": "Session: Turn fertig"}]  # positive control: the child really ran


def test_bridge_flusher_child_isolated(monkeypatch):
    seen = []
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **kw: seen.append((argv, kw)))
    activity._spawn_flusher(1.5)
    argv, kw = seen[0]
    assert argv[:3] == [sys.executable, "-I", "-c"] and argv[-2:] == ["bridge-flush", "1.500"]
    assert "-m" not in argv and kw["cwd"]


# --------------------------------------------------------------------------- #
# old tool-line path: only the joining session (no unlabeled double lines)
# --------------------------------------------------------------------------- #
def _owned_join(tmp_path, monkeypatch, owner: str):
    from voicehook_agent import session as vsession
    monkeypatch.setenv("VOICEHOOK_AGENT_HOME", str(tmp_path / "home"))
    d = vsession.session_dir("room-a", "claude-x")
    d.mkdir(parents=True)
    vsession.write_info(d, {"pid": os.getpid(), "room": "room-a", "owner_session": owner})
    return d


def _tool(sid):
    return _ev(sid=sid, tool_name="Bash", tool_input={"description": "Tests laufen lassen"},
               tool_response={"stdout": ""})


def test_other_session_tool_lines_skip_the_join_of_another_session(tmp_path, monkeypatch):
    d = _owned_join(tmp_path, monkeypatch, ms.session_hash("JOINER"))
    assert activity.post_tool_use(_tool("other-1")) is False
    assert activity.pre_tool_use(_tool("other-1")) is False
    assert not (d / activity.ACTIVITY_NAME).exists()
    assert activity.post_tool_use(_tool("JOINER")) is True  # the joining session itself
    assert (d / activity.ACTIVITY_NAME).read_text().endswith(" Bash: Tests laufen lassen\n")


def test_join_without_known_owner_keeps_old_behavior(tmp_path, monkeypatch):
    d = _owned_join(tmp_path, monkeypatch, "")
    assert activity.post_tool_use(_tool("other-1")) is True
    assert (d / activity.ACTIVITY_NAME).exists()


def test_bridge_session_feeds_only_the_session_that_saved_it(monkeypatch):
    sent = []
    monkeypatch.setattr(activity, "bridge_send", lambda text, now=None: sent.append(text) or True)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "JOINER")
    activity.save_bridge_session({"session": "sess-x"}, "https://voicehook.ai")
    assert activity.load_bridge_session()["owner_session"] == ms.session_hash("JOINER")
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
    assert activity.post_tool_use(_tool("other-1")) is False and sent == []
    assert activity.post_tool_use(_tool("JOINER")) is True and len(sent) == 1


# --------------------------------------------------------------------------- #
# WebRTC join (default transport): local relay into the join's own activity.log
# --------------------------------------------------------------------------- #
def test_webrtc_join_writes_local_relay(tmp_path, monkeypatch, capsys):
    d = _owned_join(tmp_path, monkeypatch, ms.session_hash("JOINER"))
    monkeypatch.setenv(ms.MACHINE_LABEL_ENV, "crown")
    # rtc.Room has no mint_relay, /api/token has no `relay`: only LiveKit token + url
    asyncio.run(cli._publish_relay(object(), {"token": "lk-jwt", "url": "wss://lk"}, "room-a",
                                   True, d))
    rel = ms.load_relay()
    assert rel["kind"] == "local" and rel["dir"] == str(d.resolve()) and rel["room"] == "room-a"
    assert rel["label"] == "crown" and "token" not in rel
    assert stat.S_IMODE(os.stat(ms.relay_path()).st_mode) == 0o600
    assert "(local)" in capsys.readouterr().out
    assert ms.remove_relay(owner_pid=os.getpid(), room="room-a") and not ms.relay_path().exists()


def test_local_relay_line_lands_labeled_in_join_log(tmp_path, monkeypatch):
    d = _owned_join(tmp_path, monkeypatch, ms.session_hash("JOINER"))
    ms.write_local_relay(d, "room-a", label="crown", owner_session=ms.session_hash("JOINER"))
    monkeypatch.setenv(ms.LABEL_ENV, "SE Refill")
    spawned = []
    monkeypatch.setattr(ms, "_spawn_post", spawned.append)
    assert ms.handle_event(_ev("Stop", sid="other-1"), now=5000.0) == "SE Refill: Turn fertig"
    assert spawned == []  # local: no network child
    assert ms.handle_event(_ev("Stop", sid="JOINER"), now=5100.0) is None  # the joiner itself
    lines = activity.read_tail(d / activity.ACTIVITY_NAME)
    assert len(lines) == 1 and lines[0].endswith(" relay: Helfer crown: SE Refill: Turn fertig")
    assert activity.is_relay(lines[0])


def test_local_relay_refuses_dir_of_other_pid(tmp_path, monkeypatch):
    d = _owned_join(tmp_path, monkeypatch, "")
    ms.write_local_relay(d, "room-a", owner_pid=os.getpid(), owner_session="")
    rel = dict(ms.load_relay(), owner_pid=os.getpid() + 1)
    assert ms.append_local(rel, "x: y") is False
    assert not (d / activity.ACTIVITY_NAME).exists()


def test_local_relay_through_real_hook_process(tmp_path, monkeypatch):
    d = _owned_join(tmp_path, monkeypatch, ms.session_hash("JOINER"))
    ms.write_local_relay(d, "room-a", label="mac", owner_session=ms.session_hash("JOINER"))
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path), ms.LABEL_ENV: "Doku",
           "VOICEHOOK_BOARD_REMIND": "0"}  # 0.21.0 board reminder has its own tests
    r = subprocess.run([sys.executable, "-m", "voicehook_agent.activity", "post-tool-use"],
                       input=_bash("gh pr create --fill", out="https://github.com/o/r/pull/77",
                                   sid="other-2"),
                       capture_output=True, text=True, env=env, timeout=30, check=False)
    assert r.returncode == 0 and r.stdout == ""
    lines = activity.read_tail(d / activity.ACTIVITY_NAME)
    assert len(lines) == 1 and lines[0].endswith(" relay: Helfer mac: Doku: PR #77 geöffnet")


def test_relay_lines_do_not_count_as_joining_agent_activity():
    due = activity.ActivityDue(due_s=60, now=0.0)
    due.observe(["10:00:00 relay: Helfer mac: X: Turn fertig"], 10.0)
    assert due.line_at is None and due.hook_at is None
    due.observe(["10:00:00 relay: Helfer mac: X: Turn fertig", "10:00:05 Bash: Tests"], 20.0)
    assert due.line_at == 20.0 and due.hook_at == 20.0
