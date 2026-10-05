"""0.10.0: activity feed. Claude Code PostToolUse hook -> <session_dir>/activity.log ->
join publishes the newest lines as operator.activity (Oliver 02.10.2026)."""
from __future__ import annotations

import asyncio
import json
import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from voicehook_agent import activity, cli, relay
from voicehook_agent import session as vsession

LINE_RX = re.compile(r"^\d\d:\d\d:\d\d [A-Za-z0-9_.:\-]{1,40}(: .+)?$")

FAKE_SECRETS = [
    "sk_live_51Habcdefghijklmnop",
    "sk-ant-api03-abcdefghijklmnop",
    "rk_live_51Habcdefghijklmn",
    "re_abc123def456ghi",
    "whsec_abcdefghijklmnop1234",
    "vhw_abcdefgh12345678",
    "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
    "github_pat_11ABCDEFG0123456789_abcdefgh",
    "xoxb-123456789012-abcdefghij",
    "AKIAIOSFODNN7EXAMPLE",
    "AIzaSyA1234567890abcdefghijklmnop",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl",
    "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo0MjQyNDI0Mg==",
]


# ----- scrubber -----------------------------------------------------------------------
@pytest.mark.parametrize("secret", FAKE_SECRETS)
def test_scrubber_removes_fake_secrets(secret):
    out = activity.scrub(f"deploy mit {secret} jetzt")
    assert secret not in out
    assert "[redacted]" in out
    assert out.startswith("deploy mit ") and out.endswith(" jetzt")


@pytest.mark.parametrize("text", [
    "Bearer abcdef0123456789xyz", "bearer   tok.en-value", "curl -H 'Authorization: Bearer xyz123'"])
def test_scrubber_removes_bearer(text):
    assert "[redacted]" in activity.scrub(text)
    assert "xyz" not in activity.scrub(text).replace("[redacted]", "")


@pytest.mark.parametrize("text,gone", [
    ("token=hunter2", "hunter2"), ("password=geheim", "geheim"),
    ("--api-key=abc", "abc"), ("secret='a b'", "a b"), ("key=val123", "val123")])
def test_scrubber_removes_key_value(text, gone):
    out = activity.scrub(text)
    assert gone not in out and "[redacted]" in out


@pytest.mark.parametrize("text", [
    "Tests laufen lassen", "Run the test suite", "Show working tree status",
    "Restliche Dateien pruefen", "re-run the build", "rest of the risk check",
    "Install package dependencies", "Monkey patch the clock"])
def test_scrubber_keeps_normal_descriptions(text):
    assert activity.scrub(text) == text


def test_scrubber_strips_control_chars_and_collapses_whitespace():
    assert activity.scrub("a\nb\t\tc\x1b[31m  d\u2028e") == "a b c [31m d e"


# ----- hook line ----------------------------------------------------------------------
@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("VOICEHOOK_AGENT_HOME", str(tmp_path / "vh"))
    monkeypatch.delenv("VOICEHOOK_SESSION", raising=False)
    return tmp_path / "vh"


def _join_dir(slug="blau-tiger-fluss-AB12", ident="claude-x", pid=None) -> Path:
    d = vsession.session_dir(slug, ident)
    d.mkdir(parents=True)
    vsession.write_info(d, {"pid": os.getpid() if pid is None else pid, "room": slug,
                            "identity": ident})
    return d


def _event(**kw) -> str:
    ev = {"session_id": "s1", "cwd": "/home/x/secret-project", "hook_event_name": "PostToolUse",
          "tool_name": "Bash", "tool_input": {}, "tool_response": {}}
    ev.update(kw)
    return json.dumps(ev)


def _lines(d: Path) -> list[str]:
    return (d / activity.ACTIVITY_NAME).read_text().splitlines()


def test_bash_with_description_line_format(home):
    d = _join_dir()
    now = time.mktime((2026, 10, 2, 17, 12, 3, 0, 0, -1))
    ok = activity.post_tool_use(_event(
        tool_input={"command": "pytest -q tests/", "description": "Tests laufen lassen"},
        tool_response={"stdout": "154 passed"}), now=now)
    assert ok
    assert _lines(d) == ["17:12:03 Bash: Tests laufen lassen"]


def test_edit_line_has_no_path_and_no_content(home):
    d = _join_dir()
    activity.post_tool_use(_event(tool_name="Edit", tool_input={
        "file_path": "/home/x/secret-project/app.py", "old_string": "a = 1", "new_string": "a = 2"},
        tool_response={"filePath": "/home/x/secret-project/app.py"}))
    (line,) = _lines(d)
    assert LINE_RX.match(line) and line.endswith(" Edit")
    for leak in ("/home", "app.py", "secret-project", "a = 1", "a = 2"):
        assert leak not in line


def test_command_secret_and_response_never_written(home):
    d = _join_dir()
    activity.post_tool_use(_event(
        tool_input={"command": "curl -H 'Authorization: Bearer abc' https://x?k=sk_live_51Habcdefghijk",
                    "description": "Status abfragen"},
        tool_response={"stdout": "RESPONSE-BODY-XYZ", "stderr": "boom"}))
    text = (d / activity.ACTIVITY_NAME).read_text()
    assert text.count("\n") == 1
    for leak in ("curl", "Authorization", "sk_live", "RESPONSE-BODY-XYZ", "boom", "https"):
        assert leak not in text
    assert text.endswith(" Bash: Status abfragen\n")


def test_secret_in_description_is_scrubbed(home):
    d = _join_dir()
    activity.post_tool_use(_event(tool_input={"description": "Key ghp_abcdefghijklmnopqrstuvwxyz01234 testen"}))
    assert _lines(d)[0].endswith(" Bash: Key [redacted] testen")


def test_one_line_per_call_mode_0600_and_trim(home):
    d = _join_dir()
    for i in range(activity.TRIM_AT + 1):
        activity.post_tool_use(_event(tool_input={"description": f"Schritt {i}"}))
    lines = _lines(d)
    assert len(lines) == activity.TRIM_KEEP
    assert lines[-1].endswith(f"Schritt {activity.TRIM_AT}")
    mode = stat.S_IMODE(os.stat(d / activity.ACTIVITY_NAME).st_mode)
    assert mode == 0o600


def test_tool_name_sanitized_and_mcp_kept(home):
    d = _join_dir()
    activity.post_tool_use(_event(tool_name="mcp__claude_ai_Supabase__execute_sql"))
    activity.post_tool_use(_event(tool_name="Bad Tool; rm -rf /" + "x" * 60))
    l1, l2 = _lines(d)
    assert l1.endswith(" mcp__claude_ai_Supabase__execute_sql")
    assert " " not in l2.split(" ", 1)[1] and len(l2.split(" ", 1)[1]) <= 40
    assert activity.scrub_line(l1) == l1  # defensive re-scrub keeps the tool name


def test_no_live_join_no_write(home):
    assert activity.post_tool_use(_event()) is False
    assert not list(home.glob("**/activity.log"))


def test_dead_pid_is_not_a_live_join(home):
    d = _join_dir(pid=2 ** 22 + 12345)
    assert activity.post_tool_use(_event()) is False
    assert not (d / activity.ACTIVITY_NAME).exists()


def test_two_live_joins_no_write(home):
    a = _join_dir(ident="claude-a")
    b = _join_dir(slug="rot-fuchs-berg-CD34", ident="claude-b")
    assert activity.post_tool_use(_event()) is False
    assert not (a / activity.ACTIVITY_NAME).exists() and not (b / activity.ACTIVITY_NAME).exists()


def test_voicehook_session_env_wins(home, monkeypatch):
    a = _join_dir(ident="claude-a")
    b = _join_dir(slug="rot-fuchs-berg-CD34", ident="claude-b")
    monkeypatch.setenv("VOICEHOOK_SESSION", "rot-fuchs-berg-CD34/claude-b")
    assert activity.post_tool_use(_event(tool_name="Read")) is True
    assert not (a / activity.ACTIVITY_NAME).exists()
    assert _lines(b)[0].endswith(" Read")


def test_garbage_stdin_never_raises(home):
    _join_dir()
    for raw in ("", "not json", "[1,2]", "{\"tool_name\": null}"):
        activity.post_tool_use(raw)


def test_hook_cli_exit_0_and_silent_stdout(home, tmp_path):
    d = _join_dir()
    env = {**os.environ, "VOICEHOOK_AGENT_HOME": str(home), "PYTHONPATH": str(Path(cli.__file__).parents[1])}
    env.pop("VOICEHOOK_SESSION", None)
    for argv in (["-m", "voicehook_agent.activity", "post-tool-use"],
                 ["-m", "voicehook_agent.cli", "hook", "post-tool-use"]):
        r = subprocess.run([sys.executable, *argv],
                           input=_event(tool_input={"description": "Run the test suite"}),
                           capture_output=True, text=True, env=env, timeout=30, check=False)
        assert r.returncode == 0 and r.stdout == ""
    r = subprocess.run([sys.executable, "-m", "voicehook_agent.activity", "post-tool-use"],
                       input="{{{", capture_output=True, text=True, env=env, timeout=30, check=False)
    assert r.returncode == 0 and r.stdout == ""
    lines = _lines(d)
    assert len(lines) == 2 and all(ln.endswith(" Bash: Run the test suite") for ln in lines)
    print("example activity line:", lines[0])


# ----- installer ----------------------------------------------------------------------
def test_snippet_shape():
    assert activity.snippet() == {"hooks": {"PostToolUse": [{"matcher": "*", "hooks": [
        {"type": "command", "command": "voicehook-agent-hook post-tool-use", "timeout": 5}]}]}}


def test_install_idempotent_keeps_other_keys(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"model": "opus", "hooks": {
        "Stop": [{"hooks": [{"type": "command", "command": "say done"}]}],
        "PostToolUse": [{"matcher": "Edit", "hooks": [{"type": "command", "command": "fmt"}]}]}}))
    assert activity.install(p)[0] == 0
    first = p.read_text()
    assert activity.install(p)[0] == 0
    assert p.read_text() == first
    data = json.loads(first)
    assert data["model"] == "opus"
    assert data["hooks"]["Stop"] == [{"hooks": [{"type": "command", "command": "say done"}]}]
    post = data["hooks"]["PostToolUse"]
    assert post[0]["matcher"] == "Edit" and post[1] == activity.hook_entry()


def test_install_creates_missing_file(tmp_path):
    p = tmp_path / "new" / "settings.json"
    assert activity.main(["install", "--settings", str(p)]) == 0
    assert json.loads(p.read_text()) == activity.snippet()


def test_install_refuses_invalid_json(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text("{ not json")
    rc, msg = activity.install(p)
    assert rc == 1 and "not valid JSON" in msg
    assert p.read_text() == "{ not json"


def test_cli_hook_subcommand_print(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["hook", "print"])
    assert e.value.code == 0
    assert json.loads(capsys.readouterr().out) == activity.snippet()


# ----- publish loop -------------------------------------------------------------------
def test_publisher_rate_limit_last_one_wins():
    pub = activity.ActivityPublisher(window=5.0)
    assert not pub.due([], 0.0)                  # unchanged (empty) -> nothing
    assert pub.due(["a"], 0.0)
    pub.sent(["a"], 0.0)
    assert not pub.due(["a"], 1.0)               # unchanged
    assert not pub.due(["a", "b"], 2.0)          # inside the window
    assert not pub.due(["a", "b", "c"], 4.9)     # still inside
    assert pub.due(["a", "b", "c"], 5.0)         # window over -> the latest state
    pub.sent(["a", "b", "c"], 5.0)
    assert not pub.due(["a", "b", "c"], 60.0)


class _Local:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict, bool]] = []

    async def publish_data(self, data, reliable=True, topic=""):
        self.published.append((topic, json.loads(data), reliable))


def test_activity_loop_publishes_change_then_latest_after_window(tmp_path):
    log = tmp_path / "activity.log"
    clock = {"t": 0.0}

    async def run():
        ctl = cli._Control("blau-tiger", "claude-x", relay.SayTracker(),
                           relay.EchoSuppressor(False), 0.0)
        local = _Local()
        ctl.attach(SimpleNamespace(local_participant=local, remote_participants={}))
        ctl.activity_interval = 0.01
        ctl.activity_clock = lambda: clock["t"]
        task = asyncio.create_task(cli._activity_loop(ctl, log))

        async def settle():
            await asyncio.sleep(0.05)

        await settle()
        assert local.published == []              # empty file -> nothing
        log.write_text("17:00:00 Bash: eins\n")
        await settle()
        assert len(local.published) == 1
        for i in range(2, 5):                     # several writes inside 5 s
            clock["t"] += 1.0
            with log.open("a") as f:
                f.write(f"17:00:0{i} Edit\n")
            await settle()
        assert len(local.published) == 1
        clock["t"] = 5.0                          # window over -> latest state once
        await settle()
        clock["t"] = 30.0                         # unchanged -> nothing
        await settle()
        with log.open("a") as f:
            for i in range(30):
                f.write(f"17:01:{i:02d} Read\n")
            f.write("17:02:00 Bash: Token token=hunter2 setzen\n")
        await settle()
        ctl.quit.set()
        await task
        return local.published

    pubs = asyncio.run(run())
    assert len(pubs) == 3
    assert all(t == "operator.activity" and rel for t, _, rel in pubs)
    assert pubs[0][1]["lines"] == ["17:00:00 Bash: eins"]
    assert pubs[1][1]["lines"] == ["17:00:00 Bash: eins", "17:00:02 Edit", "17:00:03 Edit",
                                   "17:00:04 Edit"]
    last = pubs[2][1]
    assert set(last) == {"lines", "ts"} and isinstance(last["ts"], float)
    assert len(last["lines"]) == 15
    assert last["lines"][-1] == "17:02:00 Bash: Token token=[redacted] setzen"  # re-scrubbed
    assert last["lines"][0] == "17:01:16 Read"   # oldest first


def test_activity_topic_is_known():
    assert "operator.activity" in cli.KNOWN_OUT_TOPICS
