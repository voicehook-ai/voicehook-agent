"""0.21.6: waiting for the human is a decision, never only a status.

Live 20.10.2026: the promo helper waited for Oliver's okay but only set
`status --doing "... wartet auf dein Okay zu Musik und Stimme"`; no decision on the board, its
card said "arbeitet", Oliver never saw a question."""
from __future__ import annotations

import asyncio
import json
import types

import pytest
from test_board_reminder import _env, _relay, bash, ev  # noqa: F401  (autouse fixture)
from test_session import _call, _join, fake_env  # noqa: F401  (fixture)

from voicehook_agent import (
    board,
    cli,
    listenguard,
    mcpserver,
    plaintext,
    relayops,
    waitcheck,
)
from voicehook_agent import session as vsession

PROMO = "Promo-Video: wartet auf dein Okay zu Musik und Stimme"
Q = "Promo-Video: Musik und Stimme so freigeben, damit ich den Schnitt fertig machen kann?"
CTX = "Musik und Sprecherstimme sind gewählt. Ja: ich schneide fertig. Nein: ich suche neu aus."
T0 = 1_800_000_000.0


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    for k in (waitcheck.ENV_OFF, plaintext.ENV_OFF):
        monkeypatch.delenv(k, raising=False)


# ----- detection --------------------------------------------------------------------------
@pytest.mark.parametrize("text", [
    PROMO, "warte auf Freigabe von Oliver", "Entscheidung von Oliver nötig",
    "Deploy, sobald du ja sagst", "Waiting for your approval on the copy",
    "Promo: warte auf deine Freigabe für die Stimme", "Vertrag liegt bereit, dein OK fehlt",
    "Sales Engine: Import wartet noch auf Olivers Rückmeldung"])
def test_waiting_texts_detected(text):
    assert waitcheck.waiting(text)


@pytest.mark.parametrize("text", [
    "Build wartet auf CI", "Sales Engine: Freigabe-Prüfung für 519 Firmen bestanden",
    "Tests laufen, ETA 5 Minuten", "Deploy wartet auf den Health-Check"])
def test_machine_waits_and_work_are_fine(text):
    assert waitcheck.waiting(text) is None


def test_suggestion_is_a_ready_decide_command():
    s = waitcheck.suggestion(PROMO)
    assert s.startswith("voicehook-agent decide --question ")
    assert "Promo-Video: Musik und Stimme so freigeben" in s and "--context" in s
    assert "<Bereich>" in waitcheck.suggestion("wartet auf Oliver")   # nothing to name: template


# ----- CLI: status / activity refused, exit 2, nothing sent ------------------------------
@pytest.fixture
def no_send(monkeypatch):
    sent = []
    monkeypatch.setattr(vsession, "request", lambda *a, **k: sent.append(a) or {"ok": True})
    monkeypatch.setattr(vsession, "resolve_socket", lambda *a, **k: "sock")
    monkeypatch.setattr(relayops, "client_relay", lambda s: None)
    return sent


@pytest.mark.parametrize("argv", [
    ["status", "--doing", PROMO], ["status", PROMO], ["activity", "warte", "auf", "dein", "Okay"]])
def test_cli_refuses_waiting_text(no_send, capsys, argv):
    with pytest.raises(SystemExit) as e:
        cli.main(argv + ["--wait", "0"])
    out = json.loads(capsys.readouterr().out.splitlines()[0])
    assert e.value.code == 2 and no_send == []
    assert out["type"] == "invalid" and waitcheck.RULE in out["error"]
    assert "voicehook-agent decide --question" in out["error"] and "status --waiting" in out["error"]


def test_cli_open_item_only_warns_and_off_switch(no_send, capsys, monkeypatch):
    with pytest.raises(SystemExit) as e:
        cli.main(["status", "--doing", "Promo-Video: Schnitt der ersten Szene läuft",
                  "--open", "Promo: warte auf deine Freigabe für die Stimme", "--wait", "0"])
    assert e.value.code == 0 and len(no_send) == 1
    assert "wartet auf den Menschen" in capsys.readouterr().err
    monkeypatch.setenv(waitcheck.ENV_OFF, "0")
    with pytest.raises(SystemExit) as e:
        cli.main(["status", "--doing", PROMO, "--wait", "0"])
    assert e.value.code == 0 and len(no_send) == 2


# ----- status --waiting ------------------------------------------------------------------
def _wargs(**kw):
    base = {"cmd": "status", "waiting": PROMO, "question": None, "decision": None, "context": None,
            "open": [], "done": [], "session": None, "wait": 0.0}
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_waiting_needs_question_or_decision(no_send, capsys):
    assert cli._run_waiting(_wargs()) == 2
    assert "braucht --question" in json.loads(capsys.readouterr().out)["error"]
    assert cli._run_waiting(_wargs(question=Q, decision="d12345678")) == 2
    assert no_send == []


def test_waiting_with_question_asks_then_sets_board(monkeypatch, capsys):
    seen = []

    def request(sock, req, timeout=None):
        seen.append(req)
        return {"ok": True, "id": "d0a1b2c3d", "number": 4} if req["cmd"] == "decide" else {"ok": True}
    monkeypatch.setattr(vsession, "request", request)
    monkeypatch.setattr(vsession, "resolve_socket", lambda *a, **k: "sock")
    monkeypatch.setattr(relayops, "client_relay", lambda s: None)
    assert cli._run_waiting(_wargs(question=Q, context=CTX)) == 0
    assert [r["cmd"] for r in seen] == ["decide", "board"]
    assert seen[0]["question"] == Q and seen[0]["context"] == CTX
    b = seen[1]["board"]
    assert b["doing"] == PROMO and b["state"] == "frage"
    assert b["decision"] == {"id": "d0a1b2c3d", "number": 4, "question": Q}
    out = json.loads(capsys.readouterr().out)
    assert out["state"] == "frage" and out["decision"]["number"] == 4


def test_waiting_question_must_be_plain_and_nothing_set(no_send, capsys):
    assert cli._run_waiting(_wargs(question="Okay?", context=CTX)) == 2
    assert no_send == [] and "Klartext" in json.loads(capsys.readouterr().out)["error"]


def test_waiting_with_decision_must_be_open(monkeypatch, capsys):
    seen = []

    def request(sock, req, timeout=None):
        seen.append(req)
        if req["cmd"] == "board_list":
            return {"ok": True, "open": [{"id": "d0a1b2c3d", "number": 4, "question": Q}]}
        return {"ok": True}
    monkeypatch.setattr(vsession, "request", request)
    monkeypatch.setattr(vsession, "resolve_socket", lambda *a, **k: "sock")
    monkeypatch.setattr(relayops, "client_relay", lambda s: None)
    assert cli._run_waiting(_wargs(decision="#4")) == 0
    assert seen[-1]["board"]["decision"]["id"] == "d0a1b2c3d"
    assert cli._run_waiting(_wargs(decision="#9")) == 2
    assert "keine offene Entscheidung" in capsys.readouterr().out
    assert cli._run_waiting(_wargs(decision="xyz")) == 2


def test_waiting_over_the_relay_posts_state_frage(monkeypatch, capsys):
    rel = {"url": "https://x/api/activity/relay", "token": "fake.relay.test.only"}
    monkeypatch.setattr(relayops, "client_relay", lambda s: rel)
    monkeypatch.setattr(relayops, "session_label", lambda: "Promo")
    calls = []
    monkeypatch.setattr(relayops, "remote_decide", lambda r, f, b: calls.append(("decide", b))
                        or {"ok": True, "id": "d0a1b2c3d", "number": 2})
    monkeypatch.setattr(relayops, "remote_status", lambda r, f, b: calls.append(("status", b))
                        or {"ok": True})
    monkeypatch.setattr(board, "post_state", lambda r, f, s: calls.append(("state", s)) or True)
    assert cli._run_waiting(_wargs(question=Q, context=CTX)) == 0
    assert [c[0] for c in calls] == ["decide", "status", "state"]
    assert calls[1][1]["state"] == "frage" and calls[2][1] == "frage"
    assert json.loads(capsys.readouterr().out)["via"] == "relay"


# ----- MCP ---------------------------------------------------------------------------------
def test_mcp_status_and_activity_refuse_waiting_and_support_waiting(monkeypatch):
    monkeypatch.setattr(mcpserver, "_route", lambda: ("own", None))
    seen = []
    monkeypatch.setattr(mcpserver, "_own", lambda req, timeout=30.0: seen.append(req)
                        or ({"ok": True, "id": "d0a1b2c3d", "number": 1} if req["cmd"] == "decide"
                            else {"ok": True}))
    with pytest.raises(mcpserver.ToolError, match="ALWAYS decide|IMMER decide"):
        mcpserver.tool_status({"doing": PROMO}, "codex")
    with pytest.raises(mcpserver.ToolError, match="decide"):
        mcpserver.tool_activity({"text": "warte auf dein Okay"}, "codex")
    with pytest.raises(mcpserver.ToolError, match="question"):
        mcpserver.tool_status({"waiting": PROMO}, "codex")
    assert seen == []
    out = mcpserver.tool_status({"waiting": PROMO, "question": Q, "context": CTX}, "codex")
    assert [r["cmd"] for r in seen] == ["decide", "board"]
    assert seen[1]["board"]["state"] == "frage" and out["decision"]["id"] == "d0a1b2c3d"
    props = next(t for t in mcpserver.TOOLS if t["name"] == "status")["inputSchema"]["properties"]
    assert {"waiting", "question", "context", "decision"} <= set(props)
    assert "ALWAYS decide" in mcpserver.INSTRUCTIONS


# ----- Stop hook: helper session (board.py) ---------------------------------------------------
def test_helper_stop_blocks_once_on_waiting_board_without_decision(tmp_path):
    _relay()
    board.handle(bash(f'vh-helper status "Promo" "{PROMO}"'), now=T0)
    out = board.handle(ev("Stop"), now=T0 + 5)
    assert out["decision"] == "block" and waitcheck.STOP_HINT in out["reason"]
    assert "voicehook-agent decide --question" in out["reason"]
    assert board.handle(ev("Stop"), now=T0 + 10) is None          # told once: no loop


def test_helper_stop_quiet_with_open_decision_or_waiting_flag(tmp_path):
    _relay()
    board.handle(bash(f'vh-helper status "Promo" "{PROMO}"'), now=T0)
    board.handle(bash(f'vh-helper decide "Promo" "{Q}"'), now=T0 + 1)
    assert board.handle(ev("Stop"), now=T0 + 5) is None
    board.handle(bash(f'voicehook-agent status --waiting "{PROMO}" --decision d0a1b2c3d',
                      sid="sess-2"), now=T0)
    assert board.handle(ev("Stop", sid="sess-2"), now=T0 + 5) is None


def test_status_doing_parse():
    assert board.status_doing(f'vh-helper status "Promo" "{PROMO}" "Schnitt"') == (PROMO, False)
    assert board.status_doing('voicehook-agent status --waiting "W" --decision d0a1b2c3d') == ("W", True)
    assert board.status_doing('voicehook-agent status "Tests laufen" --open x') == ("Tests laufen", False)


# ----- Stop hook: the joining session (listenguard.py) ------------------------------------------
def _st(**kw):
    st = {"ok": True, "connected": True, "listening": True, "consumers": 1, "unread": 0,
          "unread_wake": 0, "listen_idle_s": 0.0, "leaving": False, "pending": 0,
          "board_waiting": PROMO, "open_decisions": 0}
    st.update(kw)
    return st


def test_join_stop_blocks_once_without_decision():
    out = listenguard.decide({}, _st(), None, T0)
    assert out["decision"] == "block" and waitcheck.STOP_HINT in out["reason"]
    assert listenguard.decide({}, _st(), {"pending": 0, "at": T0, "waiting": PROMO}, T0) is None
    assert listenguard.decide({}, _st(open_decisions=1), None, T0) is None
    assert listenguard.decide({}, _st(board_waiting=None), None, T0) is None


@pytest.mark.usefixtures("fake_env")
def test_join_status_reports_waiting_board(monkeypatch):
    async def run():
        join = asyncio.create_task(_join(idle_timeout=0))
        await _call({"cmd": "board", "board": {"doing": PROMO, "open": [], "done": []}})
        st1 = await _call({"cmd": "status"})
        await _call({"cmd": "board", "board": {"doing": PROMO, "open": [], "done": [],
                                               "state": "frage"}})
        st2 = await _call({"cmd": "status"})
        await _call({"cmd": "leave"})
        await asyncio.wait_for(join, 10)
        return st1, st2

    st1, st2 = asyncio.run(run())
    assert st1["board_waiting"] == PROMO and st1["open_decisions"] == 0
    assert st2["board_waiting"] is None       # shown with status --waiting (state frage)


def test_onboarding_and_help_carry_the_rule(capsys):
    h = cli.onboarding_hint({"LANG": "de_DE.UTF-8"})
    assert waitcheck.RULE in h["text"] and h["rule"]["waiting"] == waitcheck.RULE
    assert waitcheck.RULE_EN in cli.onboarding_hint({"LANG": "en_US"})["text"]
    with pytest.raises(SystemExit):
        cli.main(["status", "--help"])
    out = " ".join(capsys.readouterr().out.split())
    assert "--waiting" in out and "IMMER decide" in out
