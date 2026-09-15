"""Tests for the voice-friendly auto-greet (_compose_greet)."""
from __future__ import annotations

import pytest

cli = pytest.importorskip(
    "voicehook_agent.cli",
    reason="livekit/httpx not installed in this environment",
)


def test_full_greet_with_username_topic_prompt():
    text = cli._compose_greet(
        name="DeepSeek", username="Olli",
        topic="den Multi-Agent-Flow zu testen", prompt="Was möchtest du besprechen?",
    )
    assert text == (
        "Hallo Olli, hier ist DeepSeek. Ich bin dem Call beigetreten, "
        "wir waren gerade dabei den Multi-Agent-Flow zu testen. Was möchtest du besprechen?"
    )


def test_greet_no_username():
    text = cli._compose_greet(name="DeepSeek", topic="die Demo")
    assert text.startswith("Hallo, hier ist DeepSeek. Ich bin dem Call beigetreten, wir waren gerade dabei die Demo.")


def test_greet_no_topic():
    text = cli._compose_greet(name="DeepSeek", username="Olli")
    assert text == "Hallo Olli, hier ist DeepSeek. Ich bin dem Call beigetreten."


def test_greet_minimal_no_name_leak():
    # never a hardcoded vendor: name is passed explicitly, no default brand
    text = cli._compose_greet(name="hermes")
    assert text == "Hallo, hier ist hermes. Ich bin dem Call beigetreten."
