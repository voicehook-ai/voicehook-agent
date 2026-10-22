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


# ----- Router-Fund 21.10.: the server's overview (Router-Fund 21.10.) ------------------------------
# The main join (WebRTC) saw neither the boards nor the open questions of its helpers:
# `board` gave boards=[], open=[]. The server lists both (RPC vh.decisions).

HELPERS = ["Mac: Steuern", "crown: Armin-Box", "Mac: voicehook-dev"]


def _server_listing():
    return {"ok": True, "count": 4,
            "open": [{"id": f"d0000000{i}", "number": i + 1, "question": f"Frage {i}?",
                      "from": f"Claude: {s}", "sender": s, "state": "open", "age_s": 5, "mine": True}
                     for i, s in enumerate(HELPERS)]
            + [{"id": "d000000ff", "number": 4, "question": "Eigene?", "from": "Claude",
                "state": "open", "age_s": 2, "mine": True}],
            "recent": [{"id": "d000000aa", "number": 9, "question": "Alt?", "state": "decided",
                        "verdict": "yes", "by": "Oliver", "decided_age_s": 30}],
            "boards": [{"from": s, "name": s.split(": ")[1], "doing": "arbeitet", "open": [], "done": [],
                        "state": "arbeitet", "age_s": 3} for s in HELPERS]}


class _RpcLocal(_Local):
    def __init__(self, answer):
        super().__init__()
        self.answer = answer
        self.calls = []

    async def perform_rpc(self, *, destination_identity, method, payload, response_timeout=8.0):
        self.calls.append((destination_identity, method))
        return json.dumps(self.answer)


def _worker_room(answer):
    worker = SimpleNamespace(identity="agent-w", kind=4, attributes={})
    return SimpleNamespace(local_participant=_RpcLocal(answer), remote_participants={"agent-w": worker})


def test_board_list_shows_all_helpers_from_server(monkeypatch):
    ctl = cli._Control("room", "claude-x", relay.SayTracker(), relay.EchoSuppressor(False), 0.0)
    room = _worker_room(_server_listing())
    ctl.attach(room)
    monkeypatch.setattr(cli, "_kind_label", lambda p: "agent" if p.identity == "agent-w" else "human")

    async def fake_rpc(c, req):
        return {"ok": True, "type": "decide", "id": "d000000ff", "number": 4}
    monkeypatch.setattr(cli, "_decide_rpc", fake_rpc)

    async def run():
        await cli._control_handler(ctl, {"cmd": "decide", "question": "Eigene?", "context": "c"})
        await cli._control_handler(ctl, {"cmd": "board", "board": {"doing": "Router", "open": [], "done": []}})
        return await cli._control_handler(ctl, {"cmd": "board_list", "open": False})
    out = asyncio.run(run())
    assert room.local_participant.calls == [("agent-w", "vh.decisions")]
    assert out["source"] == "server" and "note" not in out
    assert sorted(b["name"] for b in out["boards"]) == ["Armin-Box", "Steuern", "voicehook-dev"]
    assert [r.get("sender") for r in out["open"]] == HELPERS + [None]
    assert any(r["id"] == "d000000ff" and r["mine"] for r in out["open"])   # own question still there
    assert out["recent"][0]["verdict"] == "yes" and out["own_board"]["doing"] == "Router"


def test_board_list_falls_back_to_ledger_when_server_refuses(monkeypatch):
    ctl = cli._Control("room", "claude-x", relay.SayTracker(), relay.EchoSuppressor(False), 0.0)
    ctl.attach(_worker_room({"ok": False, "status": 403, "detail": "only agents in the call may list decisions"}))
    monkeypatch.setattr(cli, "_kind_label", lambda p: "agent")
    ctl.ledger.created({"question": "Eigene?"}, {"ok": True, "id": "d000000ff", "number": 4})
    out = asyncio.run(cli._control_handler(ctl, {"cmd": "board_list", "open": False}))
    assert out["source"] == "join" and "403" in out["note"] and out["open"][0]["id"] == "d000000ff"
    assert out["boards"] == []


def test_merged_prefers_server_keeps_local_extras_and_fresh_answers():
    t = [100.0]
    lg = boardlist.Ledger(clock=lambda: t[0])
    lg.board("op-2", {"doing": "Deploy"})                       # other operator, only seen here
    lg.board("x", {"sub": "Mac: Steuern", "doing": "alt"})      # the server has the newer one
    lg.board("x", {"sub": "Mac: Weg", "doing": "", "open": [], "done": []})   # cleared
    lg.created({"question": "Frage 0?"}, {"ok": True, "id": "d00000000", "number": 1})
    lg.answered({"id": "d00000000", "verdict": "no", "by": "Oliver"})           # decided a moment ago
    t[0] = 110.0
    out = lg.merged(_server_listing())
    assert [b["from"] for b in out["boards"]] == HELPERS + ["op-2"]
    assert out["boards"][0]["doing"] == "arbeitet" and out["boards"][-1]["age_s"] == 10
    assert "d00000000" not in [r["id"] for r in out["open"]]
    assert {r["id"] for r in out["recent"]} == {"d00000000", "d000000aa"}
    only = lg.merged(_server_listing(), only_open=True)
    assert set(only) == {"ok", "type", "source", "open"}
    assert lg.merged(None)["source"] == "join"


def test_pump_and_relay_boards_land_in_ledger(monkeypatch):
    """Without the server listing the join still knows the helper boards it published itself
    (LiveKit never echoes our own packets back)."""
    ctl = cli._Control("room", "claude-x", relay.SayTracker(), relay.EchoSuppressor(False), 0.0)
    room = SimpleNamespace(local_participant=_Local(), remote_participants={})
    ctl.helper_pump, ctl.helper_http = SimpleNamespace(), object()
    seen = {}

    async def run_pump(http, publish_status, decide, append_activity, stop, show_card=None):
        await publish_status({"sub": "crown: Armin-Box", "doing": "Update", "open": [], "done": []})
        seen["d"] = await decide({"question": "Neustart?", "sender": "crown: Armin-Box"})
    ctl.helper_pump.run = run_pump

    async def fake_rpc(c, req):
        return {"ok": True, "type": "decide", "id": "d00000077", "number": 7}
    monkeypatch.setattr(cli, "_decide_rpc", fake_rpc)
    asyncio.run(cli._run_helper_pump(room, ctl, asyncio.Event()))
    out = ctl.ledger.listing()
    assert out["boards"][0]["from"] == "crown: Armin-Box" and out["boards"][0]["doing"] == "Update"
    assert out["open"][0]["from"] == "crown: Armin-Box" and out["open"][0]["number"] == 7


def test_bridge_room_list_decisions_http():
    import httpx

    from voicehook_agent import transport as vt
    seen = []

    def handler(req):
        seen.append((req.method, req.url.path, req.headers.get("authorization")))
        if len(seen) == 1:
            return httpx.Response(200, json=_server_listing())
        return httpx.Response(403, json={"detail": "nope"})

    async def go():
        room = vt.BridgeRoom("http://vh.test", "a-b-c-AB12", "x", name="C", model="m")
        room._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        room._session = "S3SSION"
        out = await room.list_decisions()
        try:
            await room.list_decisions()
            err = None
        except vt.BridgeError as e:
            err = e.status
        await room._http.aclose()
        return out, err
    out, err = asyncio.run(go())
    assert len(out["boards"]) == 3 and err == 403
    assert seen[0] == ("GET", "/api/bridge/decisions", "Bearer S3SSION")
