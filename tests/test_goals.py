"""0.36.0 Leitsterne (goals.py): `goal list`, `goal propose`, `status --goal`, `decide --goal`,
the guiding stars as the first line of the `_meta` hint, a soft hint without --goal (never an
error, never a decide duty) and the `next` event `goals` after a change. Server side mocked
(voicehook-v4 goals_routes / kanban_live)."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from voicehook_agent import cli, goals, relay

ROWS = [{"nr": 2, "title": "Preisseite live", "done_when": "Seite antwortet 200", "source": "room"},
        {"nr": 5, "title": "Kunden versorgt", "done_when": "", "source": "pinned"}]


class FakeLP:
    def __init__(self, ack=None) -> None:
        self.sent: list[tuple[dict, str, list | None]] = []
        self.ack = ack          # callable(payload) -> ack dict, delivered like the worker would
        self.ctl = None

    async def publish_data(self, data, reliable=True, topic="", destination_identities=None):
        body = json.loads(data)
        self.sent.append((body, topic, destination_identities))
        if self.ack is not None and self.ctl is not None:
            asyncio.get_running_loop().call_later(0.01, self.ctl.goal_acks.note, self.ack(body))

    async def perform_rpc(self, *, destination_identity, method, payload, response_timeout):
        self.sent.append((json.loads(payload), method, [destination_identity]))
        return '{"ok": true, "id": "d0a1b2c3d", "number": 4}'


def _peer(ident: str, kind: int):
    return SimpleNamespace(identity=ident, kind=kind, attributes={}, track_publications={})


def _ctl(worker: bool = True, ack=None):
    ctl = cli._Control("room-a", "claude-1", None, None, 0, 0)
    lp = FakeLP(ack)
    lp.ctl = ctl
    peers = {"oliver": _peer("oliver", 0)}
    if worker:
        peers["delta-worker"] = _peer("delta-worker", 4)
    ctl.room = SimpleNamespace(local_participant=lp, remote_participants=peers)

    async def wait_room(timeout):
        return ctl.room

    ctl.wait_room = wait_room
    return ctl


def _handle(ctl, req):
    return asyncio.run(cli._control_handler(ctl, req))


# ----- Eingaben, Zeile, Hinweise -----
def test_propose_request_and_server_limits():
    assert goals.propose_request("  Leitsterne   live ", " zwei Agenten sehen es ") == {
        "cmd": "goal_propose", "title": "Leitsterne live", "done_when": "zwei Agenten sehen es"}
    assert goals.propose_request("x" * 80, "y" * 200)["title"] == "x" * 80          # Grenzen genau
    for title, done in (("", None), ("   ", None), ("x" * 81, None), ("ok", "y" * 201)):
        with pytest.raises(goals.GoalError):
            goals.propose_request(title, done)


@pytest.mark.parametrize("v,want", [("2", 2), (0, 0), (" 7 ", 7)])
def test_check_ref(v, want):
    assert goals.check_ref(v) == want


@pytest.mark.parametrize("v", ["-1", "zwei", "", None, "100000"])
def test_check_ref_bad(v):
    with pytest.raises(goals.GoalError):
        goals.check_ref(v)


def test_line_event_and_hints():
    assert goals.line([]) == ""
    assert goals.line(goals.norm(ROWS)) == "Leitsterne: #2 Preisseite live (fertig, wenn Seite antwortet 200); #5 Kunden versorgt"
    ev = goals.event(goals.norm(ROWS))
    assert ev["type"] == "goals" and [g["nr"] for g in ev["goals"]] == [2, 5] and "nie Pflicht" in ev["text"]
    assert goals.event([])["text"] == "Keine Leitsterne mehr für diesen Call."
    assert goals.missing_hint([]) is None
    h = goals.missing_hint(ROWS)
    assert h.startswith("[hint]") and "#2, #5" in h and "Nur ein Hinweis" in h
    assert goals.status_hint("Board alt.", ROWS) == "Board alt. " + goals.STATUS_QUESTION
    assert goals.status_hint("Board alt.", []) == "Board alt."


def test_norm_drops_bad_rows_and_caps_three():
    rows = ROWS + [{"nr": "x", "title": "kaputt"}, {"nr": 8, "title": " "}, {"nr": 9, "title": "Drei"},
                   {"nr": 10, "title": "Vier"}]
    assert [g["nr"] for g in goals.norm(rows)] == [2, 5, 9]
    assert goals.norm(None) == [] and goals.norm({"goals": ROWS}) == []


# ----- Abruf mit dem eigenen Token (Mock-Server) -----
def _client(status: int, body: object, seen: list):
    def handler(req: httpx.Request) -> httpx.Response:
        seen.append((req.url.path, req.headers.get("authorization")))
        return httpx.Response(status, json=body)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_fetch_uses_own_token_and_tolerates_errors():
    seen: list = []

    async def run(status, body):
        async with _client(status, body, seen) as c:
            return await goals.fetch("https://voicehook.ai", "lk-token", client=c)

    assert asyncio.run(run(200, {"ok": True, "goals": ROWS})) == goals.norm(ROWS)
    assert seen[0] == ("/api/bridge/goals", "Bearer lk-token")
    assert asyncio.run(run(403, {"ok": False, "error": "not_owner_agent"})) is None   # Gast-Agent
    assert asyncio.run(run(404, {})) is None                                          # alter Server
    assert asyncio.run(run(200, ["kaputt"])) is None
    assert asyncio.run(goals.fetch("", "t")) is None and asyncio.run(goals.fetch("https://x", "")) is None


def test_refresh_puts_next_event_only_on_change(monkeypatch):
    ctl = _ctl()
    feed = [goals.norm(ROWS), goals.norm(ROWS), goals.norm(ROWS[:1]), None]

    async def fake_fetch(api, tok, **kw):
        return feed.pop(0)

    monkeypatch.setattr(goals, "fetch", fake_fetch)
    got: list[dict] = []
    monkeypatch.setattr(ctl.events, "put_nowait", got.append)

    async def run():
        await cli._goals_refresh(ctl, event=False)      # Join: erster Stand, kein Ereignis
        await cli._goals_refresh(ctl)                   # gleich: nichts
        await cli._goals_refresh(ctl)                   # geändert: ein Ereignis
        await cli._goals_refresh(ctl)                   # nicht lesbar: Stand bleibt
    asyncio.run(run())
    assert [(e["type"], [g["nr"] for g in e["goals"]]) for e in got] == [("goals", [2])]
    assert ctl.goals == goals.norm(ROWS[:1]) and ctl.clock.goal_rows == goals.norm(ROWS[:1])


# ----- goal propose / list über den Join -----
def test_propose_sends_operator_goal_to_worker_and_returns_card_number():
    ctl = _ctl(ack=lambda b: {"req": b["req"], "ok": True, "number": 7})
    out = _handle(ctl, goals.propose_request("Leitsterne live", "zwei Agenten sehen es"))
    assert out == {"ok": True, "type": "goal", "number": 7,
                   "note": "steht auf dem Decision Board; gilt erst mit dem Klick des Inhabers"}
    [(body, topic, dest)] = ctl.room.local_participant.sent
    assert topic == "operator.goal" and dest == ["delta-worker"]
    assert body["op"] == "propose" and body["title"] == "Leitsterne live" and body["done_when"] == "zwei Agenten sehen es"


def test_propose_refused_for_guest_agent_and_timeout(monkeypatch):
    ctl = _ctl(ack=lambda b: {"req": b["req"], "ok": False, "error": "not_owner_agent"})
    out = _handle(ctl, goals.propose_request("Fremd"))
    assert out["ok"] is False and out["error"] == "not_owner_agent" and out["hint"] == goals.HINT_NOT_OWNER
    monkeypatch.setattr(goals, "ACK_WAIT_S", 0.05)
    out = _handle(_ctl(), goals.propose_request("Keine Antwort"))
    assert out["ok"] is False and out["error"] == "timeout"


def test_propose_without_worker_or_bad_input_sends_nothing():
    ctl = _ctl(worker=False)
    assert _handle(ctl, goals.propose_request("Ziel"))["error"] == "no_delta"
    assert _handle(_ctl(), {"cmd": "goal_propose", "title": "x" * 81})["type"] == "invalid"
    assert ctl.room.local_participant.sent == []


def test_goal_list(monkeypatch):
    async def fake_fetch(api, tok, **kw):
        return goals.norm(ROWS)

    monkeypatch.setattr(goals, "fetch", fake_fetch)
    out = _handle(_ctl(), {"cmd": "goal_list"})
    assert out["ok"] and out["type"] == "goals" and [g["nr"] for g in out["goals"]] == [2, 5]
    assert out["line"].startswith("Leitsterne: #2")

    async def none_fetch(api, tok, **kw):
        return None

    monkeypatch.setattr(goals, "fetch", none_fetch)
    out = _handle(_ctl(), {"cmd": "goal_list"})
    assert out["ok"] is False and out["error"] == "unavailable"


# ----- status --goal, decide --goal, weicher Hinweis -----
def _args(**kw):
    base = {"cmd": "status", "text": None, "doing": None, "open": [], "done": [], "file": None, "faq": []}
    base.update(kw)
    return SimpleNamespace(**base)


def test_status_goal_lands_in_board():
    req = cli._client_request(_args(doing="Preisseite: baut den Header", goal="2"))
    assert req["board"]["goal"] == 2
    assert "goal" not in cli._client_request(_args(doing="Preisseite: baut den Header"))["board"]
    with pytest.raises(goals.GoalError):
        cli._client_request(_args(doing="x", goal="zwei"))


def test_board_soft_hint_only_with_stars_and_without_goal():
    ctl = _ctl()
    board = {"doing": "Preisseite: baut den Header"}
    assert "goal_hint" not in _handle(ctl, {"cmd": "board", "board": dict(board)})   # keine Leitsterne
    ctl.goals = goals.norm(ROWS)
    out = _handle(ctl, {"cmd": "board", "board": dict(board)})
    assert out["ok"] is True and out["goal_hint"].startswith("[hint]")                  # nie ein Fehler
    out = _handle(ctl, {"cmd": "board", "board": {**board, "goal": 2}})
    assert out["ok"] is True and "goal_hint" not in out
    published = [b for b, t, _d in ctl.room.local_participant.sent if t == "operator.status"]
    assert published[-1]["goal"] == 2


def test_decide_goal_rides_in_rpc_and_soft_hint():
    ctl = _ctl()
    ctl.goals = goals.norm(ROWS)
    out = _handle(ctl, {"cmd": "decide", "question": "Preisseite: neuen Header heute live schalten?", "goal": 2})
    assert out["ok"] is True and "goal_hint" not in out
    rpc = [b for b, m, _d in ctl.room.local_participant.sent if m == "vh.decide"]
    assert rpc[-1]["goal"] == 2
    out = _handle(ctl, {"cmd": "decide", "question": "Preisseite: neuen Header heute live schalten?"})
    assert out["ok"] is True and out["goal_hint"].startswith("[hint]")
    out = _handle(ctl, {"cmd": "decide", "id": "d0a1b2c3d", "context": "neu"})        # Änderung: kein Hinweis
    assert "goal_hint" not in out


def test_status_due_asks_which_star(monkeypatch):
    c = relay.TurnClock(now=0.0)
    assert goals.STATUS_QUESTION not in c.status_due(now=1.0)["hint"]                  # leeres Board, keine Leitsterne
    c.goal_rows = goals.norm(ROWS)
    assert c.status_due(now=1.0)["hint"].endswith(goals.STATUS_QUESTION)


# ----- _meta hint: Leitsterne als erste Zeile -----
def test_meta_hint_first_line_goals(capsys):
    cli._emit_share_hint(True, goals.line(goals.norm(ROWS)))
    obj = json.loads(capsys.readouterr().out.strip())
    assert obj["_meta"] == "hint" and obj["text"].split("\n")[0].startswith("Leitsterne: #2 Preisseite live")
    assert obj["text"].split("\n")[1] == cli.SHARE_TEXT and obj["url"] == cli.SHARE_URL
    cli._emit_share_hint(True)                                                          # ohne Leitsterne wie bisher
    assert json.loads(capsys.readouterr().out.strip())["text"] == cli.SHARE_TEXT
    cli._emit_share_hint(True, "Leitsterne: #2 X", share=False)                         # --quiet: nur die Leitsterne
    obj = json.loads(capsys.readouterr().out.strip())
    assert obj["text"] == "Leitsterne: #2 X" and "url" not in obj
    cli._emit_share_hint(False, "Leitsterne: #2 X")
    err = capsys.readouterr().err.splitlines()
    assert err[0] == "Leitsterne: #2 X" and err[1].startswith(cli.SHARE_TEXT)
