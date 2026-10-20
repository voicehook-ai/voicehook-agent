"""0.21.5 Klartext-Check: decisions, board items and activity lines that nobody in the call can
follow ("db quick wins 620", "SE gate 519 95 grün", "Indizes nach 18h") are refused (decide)
or warned about (status, activity), always with a concrete hint and a bad/good example."""
from __future__ import annotations

import json
import types

import pytest

from voicehook_agent import cli, mcpserver, plaintext


@pytest.fixture(autouse=True)
def _check_on(monkeypatch):
    monkeypatch.delenv(plaintext.ENV_OFF, raising=False)


GOOD = plaintext.GOOD_EXAMPLE
CTX = "Die Admin-Ansicht lädt 8 Sekunden. Ja: Indizes nachts anlegen. Nein: bleibt langsam."


@pytest.mark.parametrize("q, why", [
    ("DB quick wins 620?", "zu kurz"),
    ("SE gate 519 95 grün?", "Ziffernkette '519 95'"),
    ("Indizes nach 18h", "endet nicht mit '?'"),
    ("PR #96 mergen?", "'PR #96' ohne Beschreibung"),
    ("Sollen wir den WF14 Umbau heute noch live schalten, damit es schneller geht?",
     "Abkürzung 'WF14'"),
    ("Darf ich die RLS-Regeln der Datenbank lockern, damit der Import durchläuft?",
     "Abkürzung 'RLS'"),
    ("Sales Engine: die Firmenliste neu laden, damit alles stimmt. Ja", "endet nicht mit '?'"),
])
def test_bad_questions_refused_with_hint(q, why):
    with pytest.raises(plaintext.PlainError) as e:
        plaintext.check_decide(q, CTX)
    msg = str(e.value)
    assert why in msg and "nichts gesendet" in msg
    assert plaintext.BAD_EXAMPLE in msg and plaintext.GOOD_EXAMPLE in msg


@pytest.mark.parametrize("q", [
    GOOD,
    "Soll ich PR #96 mit dem Login-Fix der Sales Engine jetzt mergen, damit der Fehler weg ist?",
    "Sales Engine (SE): die alte Testtabelle löschen, damit wieder Platz frei wird?",
    "Darf ich die CI (automatische Tests auf GitHub) für alle Branches einschalten?",
    "Release 0.21.5 heute um 18:00 veröffentlichen, damit Oliver es testen kann?",
    "Lead-Suche: 519 Firmen aus Bayern übernehmen, damit die Kampagne morgen starten kann?",
])
def test_plain_questions_pass(q):
    assert plaintext.check_decide(q, CTX) == []


def test_context_mandatory_for_new_decisions_only():
    with pytest.raises(plaintext.PlainError, match="--context fehlt"):
        plaintext.check_decide(GOOD, None)
    with pytest.raises(plaintext.PlainError, match="--context fehlt"):
        plaintext.check_decide(GOOD, "   ")
    assert plaintext.check_decide(GOOD, None, new=False) == []   # decide --id changes


def test_purpose_missing_is_only_a_warning():
    w = plaintext.check_decide("Sales Engine: die Firmenliste aus Bayern heute neu laden?", CTX)
    assert w and "wozu" in w[0]


def test_items_warn_but_plain_items_are_quiet():
    assert plaintext.item_warnings(["SE gate 519 95 grün", "Indizes nach 18h"]) != []
    w = plaintext.item_warnings(["db quick wins 620"])
    assert "Ziffernkette '620'" in w[0] and "Abkürzung 'db'" not in w[0]
    assert plaintext.item_warnings([plaintext.GOOD_ITEM,
                                    "Sales Engine: Datenbank-Indizes angelegt, Admin lädt schneller"]) == []


def test_off_switch(monkeypatch):
    monkeypatch.setenv(plaintext.ENV_OFF, "0")
    assert plaintext.check_decide("DB 620?", None) == []
    assert plaintext.item_warnings(["SE 519 95"]) == []


# ----- wiring: CLI, MCP, help ---------------------------------------------------------
def _dec(**kw):
    base = {"cmd": "decide", "question": None, "option": [], "recommend": None, "id": None,
            "delete": False, "context": None, "recent": None, "faq": [], "list": False}
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_cli_decide_refused_exit_2_nothing_sent(monkeypatch, capsys):
    sent = []
    monkeypatch.setattr(cli.vsession, "request", lambda *a, **k: sent.append(a) or {"ok": True})
    with pytest.raises(SystemExit) as e:
        cli.main(["decide", "--question", "DB quick wins 620?", "--wait", "0"])
    assert e.value.code == 2 and sent == []
    out = json.loads(capsys.readouterr().out.splitlines()[0])
    assert out["ok"] is False and out["type"] == "invalid" and "Klartext" in out["error"]


def test_cli_decide_plain_builds_request(capsys):
    req = cli._client_request(_dec(question=GOOD, context=CTX))
    assert req["question"] == GOOD and req["context"] == CTX


def test_cli_status_and_activity_warn_but_send(capsys):
    args = types.SimpleNamespace(cmd="status", text=None, doing="SE gate 519 95 grün", open=[],
                                 done=[], file=None, faq=[], project=None, item_seq=[])
    req = cli._client_request(args)
    assert req["cmd"] == "board" and req["board"]["doing"] == "SE gate 519 95 grün"
    assert "[warn] Klartext" in capsys.readouterr().err
    act = types.SimpleNamespace(cmd="activity", text=["db", "quick", "wins", "620"], context=None,
                                faq=None, project=None)
    assert cli._client_request(act)["cmd"] == "activity"
    assert "[warn] Klartext (Aktivität" in capsys.readouterr().err


def test_mcp_decide_needs_plain_question_and_context(monkeypatch):
    monkeypatch.setattr(mcpserver, "_route", lambda: ("own", None))
    seen = []
    monkeypatch.setattr(mcpserver, "_own", lambda req, timeout=30.0: seen.append(req) or {"ok": True})
    with pytest.raises(mcpserver.ToolError, match="Klartext"):
        mcpserver.tool_decide({"question": "DB quick wins 620?", "context": CTX}, "codex")
    with pytest.raises(mcpserver.ToolError, match="--context fehlt"):
        mcpserver.tool_decide({"question": GOOD}, "codex")
    assert seen == []
    mcpserver.tool_decide({"question": GOOD, "context": CTX}, "codex")
    assert seen[-1]["context"] == CTX
    schema = next(t for t in mcpserver.TOOLS if t["name"] == "decide")["inputSchema"]
    assert schema["required"] == ["question", "context"]
    assert "DB quick wins 620?" in schema["properties"]["question"]["description"]


def test_help_shows_bad_and_good_example(capsys):
    with pytest.raises(SystemExit):
        cli.main(["decide", "--help"])
    out = " ".join(capsys.readouterr().out.split())
    assert "DB quick wins 620?" in out and "Sales Engine: vier Datenbank-Indizes" in out
