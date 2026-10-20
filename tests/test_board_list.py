"""0.21.4 `voicehook-agent board` / `decide --list`: the operator sees the open and recent
decisions (number, asking label, question, options, hidden context) and the boards its join
has seen. Live 20.10.2026: Oliver asked "what does #15 mean?", the operator could not tell."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from voicehook_agent import boardlist, cli, relay


def test_ledger_open_then_answered_then_listing():
    t = [100.0]
    lg = boardlist.Ledger(clock=lambda: t[0])
    lg.created({"question": "PR #99 mergen?", "options": ["Ja", "Später"], "context": "CI grün",
                "faq": [{"q": "Risiko?", "a": "gering"}]}, {"ok": True, "id": "d00000015", "number": 15})
    lg.created({"question": "Deploy?"}, {"ok": True, "id": "d00000016", "number": 16},
               frm="mac: Scout")
    lg.created({"question": "kaputt"}, {"ok": False, "error": "x"})          # failed: nothing
    out = lg.listing()
    assert [it["number"] for it in out["open"]] == [15, 16]
    first = out["open"][0]
    assert first["from"] == "self" and first["context"] == "CI grün" and first["options"] == ["Ja", "Später"]
    assert out["open"][1]["from"] == "mac: Scout"
    t[0] = 200.0
    lg.answered({"type": "decision", "id": "d00000015", "number": 15, "verdict": "option",
                 "option": 0, "by": "Oliver"})
    out = lg.listing()
    assert [it["number"] for it in out["open"]] == [16]
    assert out["recent"][0]["verdict"] == "option" and out["recent"][0]["by"] == "Oliver"
    assert "server has no decision listing" in out["note"]
    # a remote helper's question first seen with its answer
    t[0] = 300.0
    lg.answered({"type": "helper_decision", "id": "d00000017", "number": 17, "question": "Mergen?",
                 "verdict": "yes", "by": "Oliver"}, "crown: SE Refill")
    assert lg.listing()["recent"][0]["from"] == "crown: SE Refill"
    lg.created({"id": "d00000016", "delete": True}, {"ok": True})
    assert lg.listing(only_open=True)["open"] == []


def test_ledger_boards_newest_per_sender():
    lg = boardlist.Ledger()
    lg.board("server", {"sub": "crown: Scout", "doing": "Tests", "state": "arbeitet"})
    lg.board("server", {"sub": "crown: Scout", "doing": "fertig", "state": "leerlauf"})
    lg.board("op-2", {"doing": "Deploy"})
    lg.board("", {"doing": "mein Board"}, own=True)
    out = lg.listing()
    assert [b["from"] for b in out["boards"]] == ["op-2", "crown: Scout"]
    assert out["boards"][1]["state"] == "leerlauf" and out["own_board"]["doing"] == "mein Board"


def test_ledger_keeps_open_ones_when_trimming():
    lg = boardlist.Ledger()
    lg.created({"question": "offen"}, {"ok": True, "id": "dopen0000", "number": 1})
    for i in range(boardlist.KEEP + 5):
        lg.created({"question": f"q{i}"}, {"ok": True, "id": f"d{i:08d}", "number": i + 2})
        lg.answered({"id": f"d{i:08d}", "verdict": "yes"})
    assert len(lg.items) <= boardlist.KEEP and "dopen0000" in lg.items


class _Local:
    def __init__(self):
        self.published = []

    async def publish_data(self, data, reliable=True, topic=""):
        self.published.append((topic, json.loads(data)))


def test_join_records_own_decide_and_lists_it(monkeypatch):
    ctl = cli._Control("room", "claude-x", relay.SayTracker(), relay.EchoSuppressor(False), 0.0)
    ctl.attach(SimpleNamespace(local_participant=_Local(), remote_participants={}))

    async def fake_rpc(c, req):
        return {"ok": True, "type": "decide", "id": "d0000000f", "number": 15}
    monkeypatch.setattr(cli, "_decide_rpc", fake_rpc)

    async def run():
        await cli._control_handler(ctl, {"cmd": "decide", "question": "PR #99 mergen?",
                                         "context": "CI grün"})
        await cli._control_handler(ctl, {"cmd": "board", "board": {"doing": "x", "open": [], "done": []}})
        return await cli._control_handler(ctl, {"cmd": "board_list", "open": False})
    out = asyncio.run(run())
    assert out["open"][0]["number"] == 15 and out["open"][0]["context"] == "CI grün"
    assert out["own_board"]["doing"] == "x"


def test_cli_board_and_decide_list_requests():
    seen = {}
    orig = cli._run_client
    cli._run_client = lambda a: seen.setdefault("a", a) and 0
    try:
        for argv in (["board"], ["decide", "--list"], ["board", "--open-only"]):
            seen.clear()
            try:
                cli.main(argv)
            except SystemExit:
                pass
            req = cli._client_request(seen["a"])
            assert req["cmd"] == "board_list"
            assert req["open"] is (argv == ["board", "--open-only"])
    finally:
        cli._run_client = orig
