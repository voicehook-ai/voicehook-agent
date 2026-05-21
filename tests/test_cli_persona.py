"""Tests for persona resolution incl. --strict-relay (#8).

These import voicehook_agent.cli, which imports livekit/httpx; if those aren't
installed the module is skipped rather than failing the whole suite."""
from __future__ import annotations

import pytest

cli = pytest.importorskip(
    "voicehook_agent.cli",
    reason="livekit/httpx not installed in this environment",
)


def test_persona_inline_wins(tmp_path):
    f = tmp_path / "p.txt"
    f.write_text("FILE PERSONA", encoding="utf-8")
    assert cli._load_persona("INLINE", str(f), strict_relay=True) == "INLINE"


def test_persona_file_beats_strict(tmp_path):
    f = tmp_path / "p.txt"
    f.write_text("FILE PERSONA", encoding="utf-8")
    assert cli._load_persona(None, str(f), strict_relay=True) == "FILE PERSONA"


def test_strict_relay_loads_bundled_template():
    text = cli._load_persona(None, None, strict_relay=True)
    assert text is not None
    assert "STRICT RELAY MODE" in text
    # the hard rules the SEV-1 issue (#8) demands
    assert "senior.say" in text


def test_no_persona_returns_none():
    assert cli._load_persona(None, None, strict_relay=False) is None


def test_known_topics_include_backchannel():
    # #10 — senior.backchannel must be a recognized pass-through topic
    assert "senior.backchannel" in cli.KNOWN_OUT_TOPICS
