"""0.19.0 `voicehook-agent ui`: control the humans' call page. Normalization (aliases, case,
umlauts), the v2 packet on topic delta.ui only to the humans (WebRTC), POST /api/bridge/ui
(bridge), errors before sending, the confirmation line."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from voicehook_agent import cli, relay
from voicehook_agent import transport as vt
from voicehook_agent import ui as vui


# ----- normalization ------------------------------------------------------------------------
@pytest.mark.parametrize("raw, want", [
    ("log", "log"), ("activity", "log"), ("Aktivität", "log"), ("aktivitaet", "log"), ("FEED", "log"),
    ("dec", "dec"), ("decisions", "dec"), ("Entscheidungen", "dec"),
    ("board", "board"), ("status", "board"), ("statusboard", "board"), ("Status Board", "board"),
    ("wrap", "wrap"), ("transcript", "wrap"), ("transkript", "wrap"), ("chat", "wrap"),
    ("cards", "cards"), ("karten", "cards"),
])
def test_window_aliases(raw, want):
    assert vui.window(raw) == want


@pytest.mark.parametrize("raw, want", [
    ("halves", "halves"), ("haelften", "halves"), ("Hälften", "halves"),
    ("quarters", "quarters"), ("viertel", "quarters"),
    ("thirds", "thirds"), ("dreiteiler", "thirds"), ("Drittel", "thirds"),
    ("bigmid", "bigmid"), ("gross-mitte", "bigmid"), ("gross mitte", "bigmid"), ("big", "bigmid"),
    ("bar", "bar"), ("leiste", "bar"),
])
def test_preset_aliases(raw, want):
    assert vui.preset(raw) == want


@pytest.mark.parametrize("raw, want", [
    ("l", "l"), ("left", "l"), ("links", "l"), ("m", "m"), ("middle", "m"), ("Mitte", "m"),
    ("r", "r"), ("right", "r"), ("rechts", "r"), ("b", "b"), ("bottom", "b"), ("unten", "b"),
    ("tl", "tl"), ("tr", "tr"), ("bl", "bl"), ("br", "br"), ("main", "main"), ("side", "side"),
])
def test_slot_aliases(raw, want):
    assert vui.slot(raw) == want


@pytest.mark.parametrize("raw, want", [
    ("voicehook", "voicehook"), ("salesengine", "salesengine"), ("sales engine", "salesengine"),
    ("Sales-Engine", "salesengine"), ("se", "salesengine"), ("kunden", "kunden"),
    ("customers", "kunden"), ("sonstiges", "sonstiges"), ("other", "sonstiges"),
    ("all", "all"), ("alles", "all"),
])
def test_project_aliases(raw, want):
    assert vui.project(raw) == want


@pytest.mark.parametrize("fn, raw", [
    (vui.window, "sidebar"), (vui.window, ""), (vui.preset, "fünftel"), (vui.slot, "oben"),
    (vui.project, "privat"),
])
def test_unknown_values_raise(fn, raw):
    with pytest.raises(vui.UiError, match="unknown .* use one of"):
        fn(raw)


def test_command_bodies():
    assert vui.command("large", ["decisions"]) == {"cmd": "window", "win": "dec", "state": "large"}
    assert vui.command("Close", ["log"]) == {"cmd": "window", "win": "log", "state": "close"}
    assert vui.command("layout", ["dreiteiler"]) == {"cmd": "layout", "preset": "thirds"}
    assert vui.command("move", ["activity", "links"]) == {"cmd": "move", "win": "log", "slot": "l"}
    assert vui.command("move", ["log", "l"], "drittel") == {
        "cmd": "move", "win": "log", "slot": "l", "preset": "thirds"}
    assert vui.command("focus", ["sales", "engine"]) == {"cmd": "focus", "project": "salesengine"}
    assert vui.command("focus", ["alles"]) == {"cmd": "focus", "project": "all"}


@pytest.mark.parametrize("action, args, preset, msg", [
    ("huge", ["log"], None, "unknown ui command"),
    ("large", [], None, "usage"),
    ("large", ["log", "dec"], None, "usage"),
    ("layout", [], None, "usage"),
    ("move", ["log"], None, "usage"),
    ("focus", [], None, "usage"),
    ("large", ["log"], "thirds", "--preset only goes with"),
    ("move", ["log", "l"], "fuenftel", "unknown preset"),
])
def test_command_errors(action, args, preset, msg):
    with pytest.raises(vui.UiError, match=msg):
        vui.command(action, args, preset)


def test_packet_and_summary():
    body = {"cmd": "window", "win": "log", "state": "large"}
    assert vui.packet(body) == {"v": 2, "cmd": "window", "win": "log", "state": "large", "via": "agent"}
    assert vui.summary(body) == "ui: window log large"
    assert vui.summary({"cmd": "move", "win": "log", "slot": "l", "preset": "thirds"}) == \
        "ui: move log l thirds"
    assert vui.summary({"cmd": "focus", "project": "voicehook"}) == "ui: focus voicehook"


# ----- CLI: errors before sending, confirmation line ------------------------------------
def test_invalid_ui_exits_2_and_sends_nothing(monkeypatch, capsys):
    def boom(*a, **k):
        raise AssertionError("must not contact the join")
    monkeypatch.setattr(cli.vsession, "resolve_socket", boom)
    monkeypatch.setattr(cli.vsession, "request", boom)
    with pytest.raises(SystemExit) as e:
        cli.main(["ui", "large", "sidebar"])
    assert e.value.code == 2
    out = json.loads(capsys.readouterr().out.strip())
    assert out["ok"] is False and out["type"] == "invalid" and "unknown window" in out["error"]


def test_valid_ui_goes_to_the_join_and_prints_one_line(monkeypatch, capsys):
    sent = {}
    monkeypatch.setattr(cli.vsession, "resolve_socket", lambda s, wait: "/sock")

    def req(sock, r, timeout=None):
        sent.update(r)
        return {"ok": True, "type": "ui", "via": "datachannel", "applied": r["body"], "humans": 1}
    monkeypatch.setattr(cli.vsession, "request", req)
    with pytest.raises(SystemExit) as e:
        cli.main(["ui", "move", "aktivität", "links", "--preset", "dreiteiler"])
    assert e.value.code == 0
    assert sent == {"cmd": "ui", "body": {"cmd": "move", "win": "log", "slot": "l", "preset": "thirds"}}
    assert capsys.readouterr().out == "ui: move log l thirds\n"


def test_ui_failure_prints_json_and_exits_1(monkeypatch, capsys):
    monkeypatch.setattr(cli.vsession, "resolve_socket", lambda s, wait: "/sock")
    monkeypatch.setattr(cli.vsession, "request",
                        lambda sock, r, timeout=None: {"ok": False, "type": "ui", "error": "ui not sent: x"})
    with pytest.raises(SystemExit) as e:
        cli.main(["ui", "focus", "voicehook"])
    assert e.value.code == 1
    assert json.loads(capsys.readouterr().out)["error"] == "ui not sent: x"


# ----- join side: WebRTC vs bridge --------------------------------------------------------
class _Local:
    def __init__(self) -> None:
        self.published: list[dict] = []

    async def publish_data(self, data, reliable=True, topic="", destination_identities=None):
        self.published.append({"topic": topic, "data": json.loads(data), "reliable": reliable,
                               "to": destination_identities})


def _peer(identity, kind, **attrs):
    return SimpleNamespace(identity=identity, kind=kind, attributes=attrs)


def _ctl(room) -> cli._Control:
    ctl = cli._Control("blau-tiger", "claude-x", relay.SayTracker(), relay.EchoSuppressor(False), 0.0)
    ctl.attach(room)
    return ctl


BODY = {"cmd": "window", "win": "log", "state": "large"}


def test_webrtc_sends_delta_ui_only_to_humans():
    peers = {p.identity: p for p in (
        _peer("delta-worker", 4),                                   # voicebot worker
        _peer("codex-1", 0, **{"vh.role": "agent"}),                # another operator agent
        _peer("oliver", 0, **{"vh.name": "Oliver"}),                # browser
        _peer("phone", 3),                                          # sip
        _peer("rec", 2),                                            # egress
    )}
    room = SimpleNamespace(local_participant=_Local(), remote_participants=peers)
    res = asyncio.run(cli._control_handler(_ctl(room), {"cmd": "ui", "body": BODY}))
    assert res == {"ok": True, "type": "ui", "via": "datachannel", "applied": BODY, "humans": 2}
    assert room.local_participant.published == [{
        "topic": "delta.ui", "reliable": True, "to": ["oliver", "phone"],
        "data": {"v": 2, "cmd": "window", "win": "log", "state": "large", "via": "agent"}}]


def test_webrtc_without_humans_sends_nothing():
    room = SimpleNamespace(local_participant=_Local(),
                           remote_participants={"delta-worker": _peer("delta-worker", 4)})
    res = asyncio.run(cli._control_handler(_ctl(room), {"cmd": "ui", "body": BODY}))
    assert res["ok"] is True and res["humans"] == 0
    assert room.local_participant.published == []  # an empty list would reach everyone


def test_join_side_revalidates():
    room = SimpleNamespace(local_participant=_Local(), remote_participants={"o": _peer("o", 0)})
    ctl = _ctl(room)
    res = asyncio.run(cli._control_handler(ctl, {"cmd": "ui", "body": {"cmd": "window", "win": "x",
                                                                       "state": "large"}}))
    assert res["ok"] is False and res["type"] == "invalid"
    res = asyncio.run(cli._control_handler(ctl, {"cmd": "ui", "body": {"cmd": "layout",
                                                                       "preset": "dreiteiler"}}))
    assert res["applied"] == {"cmd": "layout", "preset": "thirds"}
    assert room.local_participant.published[-1]["data"]["preset"] == "thirds"


def test_bridge_posts_api_bridge_ui():
    calls = []

    class _BridgeLike(SimpleNamespace):
        async def send_ui(self, body):
            calls.append(body)
            return {"ok": True, "applied": body, "humans": 1}
    room = _BridgeLike(local_participant=_Local(), remote_participants={})
    res = asyncio.run(cli._control_handler(_ctl(room), {"cmd": "ui", "body": {
        "cmd": "focus", "project": "salesengine"}}))
    assert res == {"ok": True, "type": "ui", "via": "bridge",
                   "applied": {"cmd": "focus", "project": "salesengine"}, "humans": 1}
    assert calls == [{"cmd": "focus", "project": "salesengine"}]  # no v / via over the bridge
    assert room.local_participant.published == []


def test_bridge_422_is_invalid():
    class _Failing(SimpleNamespace):
        async def send_ui(self, body):
            raise vt.BridgeError("bridge send ui failed: HTTP 422 slot b?", status=422,
                                 body={"detail": "slot x does not exist"})
    res = asyncio.run(cli._control_handler(_ctl(_Failing(remote_participants={})),
                                           {"cmd": "ui", "body": BODY}))
    assert res["ok"] is False and res["type"] == "invalid" and "slot x does not exist" in res["error"]


def test_bridge_room_send_ui_http():
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append((req.url.path, req.headers.get("authorization"), json.loads(req.content)))
        if json.loads(req.content).get("win") == "nope":
            return httpx.Response(422, json={"detail": "win must be one of ..."})
        return httpx.Response(200, json={"ok": True, "applied": json.loads(req.content), "humans": 1})

    async def go():
        room = vt.BridgeRoom("http://vh.test", "a-b-c-AB12", "x", name="C", model="m")
        room._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        room._session = "S3SSION"
        out = await room.send_ui(BODY)
        with pytest.raises(vt.BridgeError) as ei:
            await room.send_ui({"cmd": "window", "win": "nope", "state": "large"})
        await room._http.aclose()
        return out, ei.value
    out, err = asyncio.run(go())
    assert out == {"ok": True, "applied": BODY, "humans": 1}
    assert seen[0] == ("/api/bridge/ui", "Bearer S3SSION", BODY)
    assert err.status == 422 and err.body == {"detail": "win must be one of ..."}
