"""Pfadwahl MCP / CLI / Bridge (Oliver 19.10.2026, Entscheidung "A"): jeder Kanal dieses Repos
sagt dasselbe wie voicehook-v4 (Kanon web/agent/_snippets/which-path.md dort).

Kernsatz wörtlich in README, docs/CLI.md, Plugin-README und Plugin-Skill; `--help` trägt den
Teilsatz "cannot see the user's local sessions". Dazu die Aussage der Tabelle, dass CLI-`show`
Karten zeigt (0.21.0, "Cards from the CLI").
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from voicehook_agent.cli import main

ROOT = Path(__file__).resolve().parents[1]
CORE = ("MCP/bridge = one agent in the call; activity lines are only what that agent writes by hand. "
        "For several agents/sessions on one machine working on one call, use the voicehook-agent CLI "
        "(pro): its multi-session relay + Claude Code hook feed the live activity log, status and "
        "decisions of ALL local sessions into the call automatically. MCP/bridge run on the server "
        "and cannot see the user's local sessions.")
FACT_PART = "cannot see the user's local sessions"
CHANNELS = ["README.md", "docs/CLI.md", "plugins/voicehook-join/README.md",
            "plugins/voicehook-join/skills/voicehook-join/SKILL.md"]


def _flat(text: str) -> str:
    return " ".join(text.replace("**", "").split())


def has_core(text: str) -> bool:
    return CORE in _flat(text)


@pytest.mark.parametrize("rel", CHANNELS)
def test_core_sentence_in_every_channel(rel):
    assert has_core((ROOT / rel).read_text(encoding="utf-8")), rel


def test_table_in_cli_doc_and_plugin_skill():
    for rel in ("docs/CLI.md", "plugins/voicehook-join/skills/voicehook-join/SKILL.md"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert '## Which path: MCP, CLI or bridge? (Oliver 19.10.2026, decision "A")' in text, rel
        rows = [ln for ln in text.splitlines() if ln.startswith(("| **MCP**", "| **CLI**", "| **Bridge**"))]
        assert len(rows) == 3, rel
        assert "**Why the CLI stays:**" in text and "**Cards from the CLI (0.21.0+):**" in text, rel


def test_help_header_says_what_the_cli_is_for(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    out = _flat(capsys.readouterr().out)
    assert "the pro tool for several agents or Claude Code sessions on one machine" in out
    assert FACT_PART in out


def test_cli_show_has_cards_and_shapes(capsys):
    """0.21.0: Tabelle sagt 'Cards from the CLI (0.21.0+)'; show kann Karten und Ringformen."""
    with pytest.raises(SystemExit):
        main(["show", "--help"])
    out = capsys.readouterr().out
    for flag in ("--preset", "--card", "--text", "--file", "--panel", "--large"):
        assert flag in out, flag


def test_no_dashes_in_core():
    assert not re.search("[–—]", CORE)


def test_positive_control_old_text_fails():
    old = ("Several Claude Code sessions on one machine (0.15.0): run `voicehook-agent hook install` "
           "once; while one session is in a call, every other session reports turn goal, PR, deploy")
    assert not has_core(old) and FACT_PART not in old
    assert has_core("x **" + CORE.replace(". ", ".\n") + "** y")
