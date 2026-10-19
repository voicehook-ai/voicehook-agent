"""Keep every test away from the real ~/.voicehook (join pointers, bridge session) and from
the Claude Code session that runs the tests (CLAUDE_CODE_SESSION_ID marks join owners)."""
import pytest


@pytest.fixture(autouse=True)
def _private_state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("VOICEHOOK_STATE_DIR", str(tmp_path / "vh-state"))
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)  # 0.20.0: Codex session id too
