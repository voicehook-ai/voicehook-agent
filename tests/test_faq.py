"""0.10.0: `status --faq "Frage::Antwort"` -> board field faq[{q, a}]; every status_due
hint asks for it (Oliver 02.10.2026)."""
from __future__ import annotations

from types import SimpleNamespace

from voicehook_agent import cli, relay


def _args(**kw):
    base = {"cmd": "status", "text": None, "doing": None, "open": [], "done": [], "file": None,
            "faq": []}
    base.update(kw)
    return SimpleNamespace(**base)


def test_parse_faq_splits_on_first_separator_and_strips():
    assert relay.parse_faq(["  Wann fertig? :: in 10 min  ", "Was ist a::b? :: x::y"]) == [
        {"q": "Wann fertig?", "a": "in 10 min"}, {"q": "Was ist a", "a": "b? :: x::y"}]


def test_parse_faq_skips_invalid_with_warning():
    warns: list[str] = []
    out = relay.parse_faq(["ohne trenner", "::nur antwort", "nur frage::", "  ::  ", "ok?::ja"],
                          warn=warns.append)
    assert out == [{"q": "ok?", "a": "ja"}]
    assert len(warns) == 4 and all("--faq" in w for w in warns)


def test_parse_faq_caps_pairs_and_length():
    out = relay.parse_faq([f"F{i}::A{i}" for i in range(9)] + ["x" * 300 + "::" + "y" * 300])
    assert len(out) == 6 and out[-1] == {"q": "F5", "a": "A5"}
    long = relay.parse_faq(["x" * 300 + "::" + "y" * 300])[0]
    assert len(long["q"]) == 200 and len(long["a"]) == 200


def test_board_payload_with_faq():
    req = cli._client_request(_args(doing="baut den Fix", faq=["Wann live?::in 15 min",
                                                              "Tests gruen?::ja, 204"]))
    assert req == {"cmd": "board", "board": {
        "doing": "baut den Fix", "open": [], "done": [],
        "faq": [{"q": "Wann live?", "a": "in 15 min"}, {"q": "Tests gruen?", "a": "ja, 204"}]}}


def test_faq_alone_sends_a_board():
    assert cli._client_request(_args(faq=["a?::b"]))["board"]["faq"] == [{"q": "a?", "a": "b"}]


def test_without_faq_payload_unchanged():
    req = cli._client_request(_args(doing="x"))
    assert req == {"cmd": "board", "board": {"doing": "x", "open": [], "done": []}}
    # only invalid --faq values: the field stays out
    assert "faq" not in cli._client_request(_args(doing="x", faq=["kaputt"]))["board"]


def test_argparse_accepts_repeatable_faq(monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "_run_client", lambda a: seen.setdefault("args", a) and 0)
    try:
        cli.main(["status", "--doing", "d", "--faq", "a?::b", "--faq", "c?::d"])
    except SystemExit:
        pass
    assert seen["args"].faq == ["a?::b", "c?::d"]


def test_status_due_hints_mention_faq():
    for hint in (*relay.STATUS_HINTS.values(), relay.SAY_PROGRESS_HINT):
        assert "Welche 3 Fragen stellt der Nutzer wahrscheinlich als Nächstes?" in hint
        assert "--faq" in hint
        assert "voicehook-agent status --doing" in hint  # existing text kept
    clock = relay.TurnClock(due_s=45)
    assert "--faq" in clock.status_due(now=1.0)["hint"]
