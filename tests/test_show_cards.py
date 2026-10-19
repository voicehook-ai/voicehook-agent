"""0.21.0 `show --card`: cards on the call page (text, code, link, image) like bridge
POST /api/bridge/show and MCP show, plus --panel and --large.

Client: parse + validate (limits like the server, SVG without script/external refs), invalid
-> exit 2 and nothing sent. Join: WebRTC publishes operator.card itself (image stored first via
POST /api/card/image with the join's LiveKit token) and delta.ui only to the humans; bridge
posts /api/bridge/show with the same body."""
from __future__ import annotations

import asyncio
import base64
import json
from types import SimpleNamespace

import pytest

from voicehook_agent import cards as vc

cli = pytest.importorskip("voicehook_agent.cli", reason="livekit/httpx not installed")
import httpx

from voicehook_agent import relay
from voicehook_agent import transport as vt

SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><rect width="10" height="10"/></svg>'
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
CID = "0123456789abcdef"
URL = f"/api/card/blau-tiger/{CID}?e=1&s=x"


def _args(argv):
    seen = {}

    def fake_run(args):
        seen["args"] = args
        return 0
    orig = cli._run_client
    cli._run_client = fake_run
    try:
        with pytest.raises(SystemExit):
            cli.main(argv)
    finally:
        cli._run_client = orig
    return seen["args"]


def _req(argv):
    return cli._client_request(_args(argv))


# ----- client side ---------------------------------------------------------------------------
def test_text_code_link_and_panel_requests(tmp_path):
    assert _req(["show", "--card", "code", "--title", "Fix", "--text", "git push"]) == {
        "cmd": "card", "kind": "code", "text": "git push", "title": "Fix"}
    f = tmp_path / "n.md"
    f.write_text("Zeile 1\n\tZeile 2\n", encoding="utf-8")
    assert _req(["show", "--card", "text", "--file", str(f), "--panel", "cards"]) == {
        "cmd": "card", "kind": "text", "text": "Zeile 1\n\tZeile 2\n", "panel": "cards"}
    assert _req(["show", "--card", "link", "--text", " https://voicehook.ai "])["text"] == "https://voicehook.ai"
    assert _req(["show", "--panel", "none"]) == {"cmd": "card", "panel": "none"}
    assert _req(["show", "--large"]) == {"cmd": "card", "panel": "cards", "large": True}
    # shape flags keep working
    assert _req(["show", "--preset", "check"])["cmd"] == "visual"


def test_image_file_svg_as_text_raster_as_base64(tmp_path):
    s = tmp_path / "plan.svg"
    s.write_text(SVG, encoding="utf-8")
    r = _req(["show", "--card", "image", "--file", str(s), "--title", "Plan", "--large"])
    assert r == {"cmd": "card", "kind": "image", "image": SVG, "title": "Plan", "panel": "cards", "large": True}
    p = tmp_path / "x.dat"   # name does not matter, magic bytes do
    p.write_bytes(PNG)
    assert _req(["show", "--card", "image", "--file", str(p)])["image"] == base64.b64encode(PNG).decode()


@pytest.mark.parametrize("argv,msg", [
    (["show", "--card", "text", "--text", "x" * 4001], "text max 4000"),
    (["show", "--card", "text", "--text", "x", "--title", "t" * 81], "title max 80"),
    (["show", "--card", "link", "--text", "javascript:alert(1)"], "link must be one http"),
    (["show", "--card", "code", "--text", "  "], "text is empty"),
    (["show", "--card", "image", "--text", "x"], "takes --file"),
    (["show", "--card", "image"], "needs --file"),
    (["show", "--text", "x"], "need --card"),
    (["show", "--card", "text", "--text", "x", "--preset", "check"], "cannot be combined with shape"),
    (["show", "--card", "text", "--text", "x", "--panel", "board", "--large"], "--large only with --panel cards"),
])
def test_invalid_card_exit_2_nothing_sent(monkeypatch, capsys, argv, msg):
    def boom(*a, **k):
        raise AssertionError("must not contact the join")
    monkeypatch.setattr(cli.vsession, "resolve_socket", boom)
    with pytest.raises(SystemExit) as e:
        cli.main(argv)
    assert e.value.code == 2
    out = json.loads(capsys.readouterr().out.strip())
    assert out["type"] == "invalid" and msg in out["error"]


@pytest.mark.parametrize("svg,msg", [
    ('<svg><script>alert(1)</script></svg>', "no <script>"),
    ('<svg><foreignObject/></svg>', "no <foreignObject>"),
    ('<svg><rect onclick="x()"/></svg>', "no on* event attributes"),
    ('<svg><image href="https://evil.example/x.png"/></svg>', "nothing external"),
    ("<svg>" + "x" * vc.SVG_MAX + "</svg>", "svg max 300 KB"),
    ("<html></html>", "svg, png, jpeg or webp"),
])
def test_bad_image_files(tmp_path, monkeypatch, capsys, svg, msg):
    monkeypatch.setattr(cli.vsession, "resolve_socket", lambda *a, **k: (_ for _ in ()).throw(AssertionError()))
    f = tmp_path / "x.svg"
    f.write_text(svg, encoding="utf-8")
    with pytest.raises(SystemExit) as e:
        cli.main(["show", "--card", "image", "--file", str(f)])
    assert e.value.code == 2 and msg in capsys.readouterr().out


def test_big_diagram_svg_is_fine():
    lanes = "".join(f'<rect y="{i * 200}" width="1800" height="200" fill="url(#g)"/><text y="{i * 200 + 90}">L{i}</text>'
                    for i in range(4))
    svg = (f'<svg viewBox="0 0 1800 800"><defs><linearGradient id="g"/><marker id="a"/></defs>{lanes}'
           '<path d="M0,0 L10,10" marker-end="url(#a)"/><use href="#a"/></svg>')
    assert vc.image_kind(svg.encode()) == "image/svg+xml"


# ----- join side -----------------------------------------------------------------------------
class _Local:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict, list | None]] = []

    async def publish_data(self, data, reliable=True, topic="", destination_identities=None):
        self.published.append((topic, json.loads(data), destination_identities))


def _peer(identity, kind=0, attrs=None):
    return SimpleNamespace(identity=identity, kind=kind, attributes=attrs or {})


def _room():
    return SimpleNamespace(local_participant=_Local(), remote_participants={
        "host": _peer("host"), "voice-ai": _peer("voice-ai", kind=4),
        "other-op": _peer("other-op", attrs={"vh.role": "agent"})})


def _ctl(room) -> cli._Control:
    ctl = cli._Control("blau-tiger", "claude-x", relay.SayTracker(), relay.EchoSuppressor(False), 0.0)
    ctl.attach(room)
    ctl.api_base, ctl.lk_jwt = "http://vh.test", "LK.JWT.TOKEN"
    return ctl


def test_webrtc_text_card_and_panel_only_to_humans():
    room = _room()
    ctl = _ctl(room)
    res = asyncio.run(cli._control_handler(ctl, {"cmd": "card", "kind": "code", "text": "print(1)",
                                                 "title": "Fix", "panel": "cards"}))
    assert res["ok"] and res["via"] == "datachannel" and res["seq"] == 1 and res["kind"] == "code"
    (t1, card, d1), (t2, ui, d2) = room.local_participant.published
    assert t1 == "operator.card" and d1 is None
    assert card["id"] == res["id"] and len(card["id"]) == 16 and card["kind"] == "code"
    assert card["text"] == "print(1)" and card["title"] == "Fix" and card["seq"] == 1
    assert t2 == "delta.ui" and ui == {"panel": "cards", "open": True, "via": "agent"} and d2 == ["host"]
    again = asyncio.run(cli._control_handler(ctl, {"cmd": "card", "text": "zwei"}))
    assert again["seq"] == 2 and again["kind"] == "text"


def test_panel_none_closes_all_and_large_carries_the_card_id():
    room = _room()
    ctl = _ctl(room)
    asyncio.run(cli._control_handler(ctl, {"cmd": "card", "panel": "none"}))
    assert [p["panel"] for _, p, _ in room.local_participant.published] == ["board", "log", "cards", "decisions"]
    assert all(p["open"] is False for _, p, _ in room.local_participant.published)
    room.local_participant.published.clear()
    res = asyncio.run(cli._control_handler(ctl, {"cmd": "card", "kind": "text", "text": "x", "large": True}))
    assert res["panel"] == "cards" and res["large"] is True
    _, ui, _ = room.local_participant.published[-1]
    assert ui == {"panel": "cards", "open": True, "via": "agent", "large": True, "id": res["id"]}


def test_no_human_no_panel_packet():
    room = SimpleNamespace(local_participant=_Local(), remote_participants={})
    res = asyncio.run(cli._control_handler(_ctl(room), {"cmd": "card", "panel": "board"}))
    assert res["ok"] and "no human" in res["hint"] and room.local_participant.published == []


def _mock_http(monkeypatch, handler):
    real = httpx.AsyncClient

    def factory(*a, **k):
        k["transport"] = httpx.MockTransport(handler)
        return real(*a, **k)
    monkeypatch.setattr(cli.httpx, "AsyncClient", factory)


def test_webrtc_image_uploaded_with_own_token_then_published(monkeypatch):
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append((req.url.path, req.headers.get("authorization"), json.loads(req.content)))
        return httpx.Response(200, json={"ok": True, "id": CID, "url": URL, "mime": "image/svg+xml"})
    _mock_http(monkeypatch, handler)
    room = _room()
    res = asyncio.run(cli._control_handler(_ctl(room), {"cmd": "card", "kind": "image", "image": SVG,
                                                        "title": "Plan", "large": True}))
    assert res["ok"] and res["id"] == CID
    assert seen == [("/api/card/image", "Bearer LK.JWT.TOKEN", {"image": SVG})]
    (t, card, _), (_, ui, _) = room.local_participant.published
    assert t == "operator.card" and card["url"] == URL and card["mime"] == "image/svg+xml"
    assert card["id"] == CID and card["kind"] == "image" and "image" not in card and "<svg" not in json.dumps(card)
    assert ui["large"] is True and ui["id"] == CID


def test_image_upload_errors(monkeypatch):
    answers = iter([httpx.Response(404, json={"detail": "Not Found"}),
                    httpx.Response(422, json={"detail": "svg must be a plain drawing: no <script>"}),
                    httpx.Response(429, json={"detail": "too many images"}),
                    httpx.Response(200, json={"ok": True, "id": CID, "url": "https://evil.example/x"})])
    _mock_http(monkeypatch, lambda req: next(answers))
    room = _room()
    ctl = _ctl(room)
    req = {"cmd": "card", "kind": "image", "image": SVG}
    old = asyncio.run(cli._control_handler(ctl, req))
    assert old["ok"] is False and "newer server" in old["error"]
    bad = asyncio.run(cli._control_handler(ctl, req))
    assert bad["type"] == "invalid" and "no <script>" in bad["error"]
    assert asyncio.run(cli._control_handler(ctl, req))["type"] == "rate_limited"
    assert asyncio.run(cli._control_handler(ctl, req))["ok"] is False   # foreign URL never published
    assert room.local_participant.published == []


def test_expired_token_falls_back_to_pump_token(monkeypatch):
    auths = []

    def handler(req):
        auths.append(req.headers.get("authorization"))
        if req.headers.get("authorization") == "Bearer LK.JWT.TOKEN":
            return httpx.Response(404, json={"detail": "unknown or expired session"})
        return httpx.Response(200, json={"ok": True, "id": CID, "url": URL, "mime": "image/png"})
    _mock_http(monkeypatch, handler)
    room = _room()
    ctl = _ctl(room)
    ctl.helper_pump = SimpleNamespace(token="PUMP")
    res = asyncio.run(cli._control_handler(ctl, {"cmd": "card", "kind": "image",
                                                 "image": base64.b64encode(PNG).decode()}))
    assert res["ok"] and auths == ["Bearer LK.JWT.TOKEN", "Bearer PUMP"]


def test_join_side_revalidates():
    room = _room()
    bad = asyncio.run(cli._control_handler(_ctl(room), {"cmd": "card", "kind": "link", "text": "data:x"}))
    assert bad["ok"] is False and bad["type"] == "invalid" and room.local_participant.published == []


def test_bridge_posts_api_bridge_show():
    calls = []

    class _BridgeLike(SimpleNamespace):
        async def send_card(self, body):
            calls.append(body)
            return {"ok": True, "id": CID, "seq": 3, "panel": "cards", "large": True}
    room = _BridgeLike(local_participant=_Local(), remote_participants={})
    res = asyncio.run(cli._control_handler(_ctl(room), {"cmd": "card", "kind": "image", "image": SVG,
                                                        "large": True}))
    assert res == {"ok": True, "type": "card", "via": "bridge", "id": CID, "seq": 3, "panel": "cards",
                   "large": True, "kind": "image"}
    assert calls == [{"kind": "image", "image": SVG, "panel": "cards", "large": True}]
    assert room.local_participant.published == []

    class _Limited(SimpleNamespace):
        async def send_card(self, body):
            raise vt.BridgeError("bridge send show failed: HTTP 422 text max", status=422)
    r = asyncio.run(cli._control_handler(_ctl(_Limited(remote_participants={})), {"cmd": "card", "text": "x"}))
    assert r["type"] == "invalid"


def test_bridge_room_send_card_http():
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append((req.url.path, req.headers.get("authorization"), json.loads(req.content)))
        return httpx.Response(200, json={"ok": True, "id": CID, "seq": 1})

    async def go():
        room = vt.BridgeRoom("http://vh.test", "a-b-c-AB12", "x", name="C", model="m")
        room._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        room._session = "S3SSION"
        out = await room.send_card({"kind": "text", "text": "hi"})
        await room._http.aclose()
        return out
    assert asyncio.run(go()) == {"ok": True, "id": CID, "seq": 1}
    assert seen == [("/api/bridge/show", "Bearer S3SSION", {"kind": "text", "text": "hi"})]


def test_cycle_stores_token_for_uploads():
    """The WebRTC cycle keeps api_base + the newest LiveKit token on the control (re-minted per
    reconnect), the image upload uses it."""
    import inspect
    src = inspect.getsource(cli._connect_and_listen)
    assert "ctl.api_base, ctl.lk_jwt = api_base" in src
