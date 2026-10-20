"""Keep every test away from the real ~/.voicehook (join pointers, bridge session) and from
the Claude Code session that runs the tests (CLAUDE_CODE_SESSION_ID marks join owners)."""
import pytest


@pytest.fixture(autouse=True)
def _private_state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("VOICEHOOK_STATE_DIR", str(tmp_path / "vh-state"))
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    # 0.21.0: `hook install` writes the absolute hook path; tests see the bare name unless
    # they set their own (tests/test_hook_abs_path.py)
    monkeypatch.setenv("VOICEHOOK_HOOK_BIN", "voicehook-agent-hook")
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)  # 0.20.0: Codex session id too
    # 0.21.4 notify.unattended(): the runner's own Claude Code / CI env must not decide it
    for k in ("VOICEHOOK_ATTENDED", "CLAUDE_CODE_SESSION_ATTENDED", "CLAUDE_CODE_ENTRYPOINT"):
        monkeypatch.delenv(k, raising=False)
    # 0.21.4 board.py: never a detached state watcher from a test; CLAUDE_PID of the runner
    from voicehook_agent import board
    monkeypatch.setattr(board, "_ensure_watcher", lambda: None)
    monkeypatch.delenv("CLAUDE_PID", raising=False)
    # 0.21.4 join: no `ps` scan of the test runner's machine (tests/test_local_sessions.py)
    monkeypatch.setenv("VOICEHOOK_SESSIONS_HINT", "0")
