"""Default "eine Stimme" (Oliver 21.10.2026): in der Regel ein Agent mit Stimme als Router, alle
anderen Sessions per CLI-Relay als stille Helfer. Kernsatz wörtlich wie im Kanon voicehook-v4
(apps/agent/tests/test_one_voice_default.py dort) in docs/CLI.md und im Plugin-Skill.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CORE = ("Default: one voice. One agent joins with voice and acts as the router; it talks to the human "
        "and routes work. Every other session on the machine connects as a silent helper through the "
        "CLI relay (board, activity, decide), never with voice. Several voiced agents in one call are "
        "possible (each gets its own voice, #522) but only when the human asks for it; otherwise it "
        "gets too noisy.")


@pytest.mark.parametrize("rel", ["docs/CLI.md", "plugins/voicehook-join/skills/voicehook-join/SKILL.md"])
def test_one_voice_default(rel):
    assert CORE in " ".join((ROOT / rel).read_text(encoding="utf-8").replace("**", "").split()), rel
