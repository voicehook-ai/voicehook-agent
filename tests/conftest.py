"""Keep every test away from the real ~/.voicehook (join pointers, bridge session)."""
import pytest


@pytest.fixture(autouse=True)
def _private_state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("VOICEHOOK_STATE_DIR", str(tmp_path / "vh-state"))
