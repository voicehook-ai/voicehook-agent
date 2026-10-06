"""`say --help` sagt, dass der Text vorgelesen wird (ganze, natürliche Sätze)."""

import pytest

from voicehook_agent.cli import main


def test_say_help_mentions_spoken_style(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["say", "--help"])
    assert exc.value.code == 0
    out = " ".join(capsys.readouterr().out.split())
    assert "Text wird vorgelesen: ganze, natürliche Sätze." in out
    assert "caveman" in out
