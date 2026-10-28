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
CTX = "Plan: drei Spuren, 7 Schritte, Umsatz 1.250 EUR"  # full text of the image (0.31.0, required)


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
    r = _req(["show", "--card", "image", "--file", str(s), "--title", "Plan", "--large",
              "--context", CTX])
    assert r == {"cmd": "card", "kind": "image", "image": SVG, "title": "Plan", "context": CTX,
                 "panel": "cards", "large": True}
    p = tmp_path / "x.dat"   # name does not matter, magic bytes do
    p.write_bytes(PNG)
    assert _req(["show", "--card", "image", "--file", str(p), "--context", CTX])["image"] == base64.b64encode(PNG).decode()


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
        cli.main(["show", "--card", "image", "--file", str(f), "--context", CTX])
    assert e.value.code == 2 and msg in capsys.readouterr().out


def test_big_diagram_svg_is_fine():
    lanes = "".join(f'<rect y="{i * 200}" width="1800" height="200" fill="url(#g)"/><text y="{i * 200 + 90}">L{i}</text>'
                    for i in range(4))
    svg = (f'<svg viewBox="0 0 1800 800"><defs><linearGradient id="g"/><marker id="a"/></defs>{lanes}'
           '<path d="M0,0 L10,10" marker-end="url(#a)"/><use href="#a"/></svg>')
    assert vc.image_kind(svg.encode()) == "image/svg+xml"


# ----- join side -----------------------------------------------------------------------------
class _Local:
    """Fake local participant. 0.23.1: an image card is answered by the call page of `host`
    (vh.card_ack) with `ack` (True loaded, False failed, None = page never answers)."""

    def __init__(self) -> None:
        self.published: list[tuple[str, dict, list | None]] = []
        self.ctl = None
        self.ack: bool | None = True

    async def publish_data(self, data, reliable=True, topic="", destination_identities=None):
        obj = json.loads(data)
        self.published.append((topic, obj, destination_identities))
        if topic == "operator.card" and "url" in obj and self.ctl is not None and self.ack is not None:
            asyncio.get_running_loop().call_soon(self.ctl.card_acks.note, {"id": obj["id"], "ok": self.ack}, "host")


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
    if isinstance(getattr(room, "local_participant", None), _Local):
        room.local_participant.ctl = ctl
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
    res = asyncio.run(cli._control_handler(_ctl(room), {"cmd": "card", "kind": "image", "image": SVG, "context": CTX,
                                                        "title": "Plan", "large": True}))
    assert res["ok"] and res["id"] == CID and res["shown"] == 1
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
    req = {"cmd": "card", "kind": "image", "image": SVG, "context": CTX}
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
    res = asyncio.run(cli._control_handler(ctl, {"cmd": "card", "kind": "image", "context": CTX,
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
    res = asyncio.run(cli._control_handler(_ctl(room), {"cmd": "card", "kind": "image", "image": SVG, "context": CTX,
                                                        "large": True}))
    assert res == {"ok": True, "type": "card", "via": "bridge", "id": CID, "seq": 3, "panel": "cards",
                   "large": True, "kind": "image"}
    assert calls == [{"kind": "image", "image": SVG, "context": CTX, "panel": "cards", "large": True}]
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


def test_is_svg_head_without_backtracking():
    assert vc.is_svg(b'  <?xml version="1.0"?>\n<!-- a --><!-- b --><!DOCTYPE svg><svg viewBox="0 0 1 1"/>')
    assert vc.is_svg(b"<SVG>") and not vc.is_svg(b"<svgx>") and not vc.is_svg(b"<html><svg>")
    assert not vc.is_svg(b"<!--" + b"--><!--" * 50000)   # CodeQL ReDoS case, returns fast


@pytest.mark.parametrize("svg", [
    b"<svg><rect style=\"fill:url( ' https://evil.example/p' )\"/></svg>",
    b"<svg><image href=\" https://evil.example/x.png\"/></svg>",
])
def test_external_refs_with_spaces_rejected(svg):
    with pytest.raises(vc.CardError, match="nothing external"):
        vc.image_kind(svg)


# ----- 0.23.1: ok only when a call page loaded the image (live 21.10.2026) ------------------
def _img_ok(monkeypatch):
    _mock_http(monkeypatch, lambda req: httpx.Response(200, json={"ok": True, "id": CID, "url": URL,
                                                                 "mime": "image/png"}))


def test_image_not_loaded_in_page_is_never_ok(monkeypatch):
    _img_ok(monkeypatch)
    room = _room()
    room.local_participant.ack = False   # the page could not load it (server restarted)
    res = asyncio.run(cli._control_handler(_ctl(room), {"cmd": "card", "kind": "image", "image": SVG,
                                                        "context": CTX}))
    assert res["ok"] is False and res["type"] == "not_shown" and "did not load" in res["error"]
    assert res["id"] == CID   # the card went out, the answer says it was not seen


def test_image_without_page_answer_is_never_ok(monkeypatch):
    _img_ok(monkeypatch)
    monkeypatch.setattr(vc, "ACK_WAIT_S", 0.3)
    room = _room()
    room.local_participant.ack = None   # old page or offline: nobody answers
    ctl = _ctl(room)

    async def go():
        return await ctl.card_acks.wait("x" * 16, {"host"}, timeout=0.2)
    assert asyncio.run(go()) == (set(), set())
    res = asyncio.run(cli._control_handler(ctl, {"cmd": "card", "kind": "image", "image": SVG,
                                                        "context": CTX}))
    assert res["ok"] is False and res["type"] == "not_shown"


def test_image_without_human_is_never_ok(monkeypatch):
    _img_ok(monkeypatch)
    room = SimpleNamespace(local_participant=_Local(), remote_participants={
        "voice-ai": _peer("voice-ai", kind=4)})
    res = asyncio.run(cli._control_handler(_ctl(room), {"cmd": "card", "kind": "image", "image": SVG,
                                                        "context": CTX}))
    assert res["ok"] is False and res["type"] == "no_human"


def test_ack_waits_for_all_humans_or_grace():
    async def go():
        acks = vc.Acks()
        acks.expect("a" * 16)
        loop = asyncio.get_running_loop()
        loop.call_later(0.05, acks.note, {"id": "a" * 16, "ok": True}, "h1")
        loop.call_later(0.1, acks.note, {"id": "a" * 16, "ok": False}, "h2")
        loop.call_later(0.1, acks.note, {"id": "b" * 16, "ok": True}, "h3")   # other card: ignored
        t0 = loop.time()
        got = await acks.wait("a" * 16, {"h1", "h2"}, timeout=5.0, grace=3.0)
        return got, loop.time() - t0
    (loaded, failed), took = asyncio.run(go())
    assert loaded == {"h1"} and failed == {"h2"} and took < 1.0   # both answered: no waiting


def test_ack_result_texts():
    assert vc.ack_result({"ok": True}, {"a"}, {"b"}, 2) == {"ok": True, "shown": 1, "not_loaded": 1}
    assert vc.ack_result({"ok": True}, set(), set(), 0)["type"] == "no_human"


def test_helper_card_from_pump_is_checked_and_confirmed():
    room = _room()
    ctl = _ctl(room)
    card = {"id": CID, "kind": "image", "title": "Plan", "text": "", "from": "Mac: Scout", "url": URL,
            "mime": "image/png", "at": 1.0, "context": CTX}
    res = asyncio.run(cli._publish_helper_card(ctl, room, card))
    assert res == {"ok": True, "shown": 1}
    (t, pkt, _), = room.local_participant.published
    assert t == "operator.card" and pkt["from"] == "Mac: Scout" and pkt["seq"] == 1 and pkt["url"] == URL
    bad = asyncio.run(cli._publish_helper_card(ctl, room, {**card, "url": "https://evil.example/x.png"}))
    assert bad["ok"] is False and bad["status"] == 422 and len(room.local_participant.published) == 1
    room.local_participant.ack = False
    gone = asyncio.run(cli._publish_helper_card(ctl, room, {**card, "id": "b" * 16, "url": URL.replace(CID, "b" * 16)}))
    assert gone["ok"] is False and gone["status"] == 409


# --------------------------------------------------------------------------- #
# 0.23.1 live 22.10.2026: a ~700 KB image crashed the control socket
# ("Separator is not found, and chunk exceed the limit", then BrokenPipeError)
# --------------------------------------------------------------------------- #
def _png(n: int) -> bytes:
    import os
    return b"\x89PNG\r\n\x1a\n" + os.urandom(n - 8)


def _show_over_socket(monkeypatch, tmp_path, argv, *, limit=None):
    """`cli.main(argv)` against a real control socket. limit=None: the join's ControlServer;
    else a bare asyncio server with that line limit (the pre-0.23.1 join). -> (rc, seen, out)"""
    from voicehook_agent import session as vs
    sock = tmp_path / "c.sock"
    seen: list[dict] = []

    async def handler(req):
        seen.append(req)
        return {"ok": True, "type": "card", "kind": req.get("kind")}

    async def bare(reader, writer):  # the old join: default 64 KiB line limit
        raw = await reader.readline()
        seen.append(json.loads(raw))
        writer.write(b'{"ok": true}\n')
        await writer.drain()
        writer.close()

    monkeypatch.setattr(cli.vsession, "resolve_socket", lambda *a, **k: sock)
    monkeypatch.setattr(cli, "_helper_relay", lambda *a, **k: None)

    def run_main():
        try:
            cli.main(argv)
        except SystemExit as e:
            return e.code
        return 0

    async def go():
        if limit is None:
            srv = vs.ControlServer(sock, handler)
            await srv.start()
        else:
            srv = await asyncio.start_unix_server(bare, path=str(sock), limit=limit)
        try:
            return await asyncio.to_thread(run_main)
        finally:
            if limit is None:
                await srv.close()
            else:
                srv.close()
    loop = asyncio.new_event_loop()
    loop.set_exception_handler(lambda *_: None)  # the old server's crash is the point
    try:
        rc = loop.run_until_complete(go())
    finally:
        loop.close()
    return rc, seen


def test_700kb_image_goes_through_the_control_socket(monkeypatch, tmp_path, capsys):
    data = _png(700 * 1024)
    f = tmp_path / "shot.png"
    f.write_bytes(data)
    argv = ["show", "--card", "image", "--title", "Shot", "--file", str(f), "--context", CTX]
    rc, seen = _show_over_socket(monkeypatch, tmp_path, argv)
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert rc == 0 and out["ok"] is True, out
    assert len(seen) == 1 and seen[0]["kind"] == "image"
    assert base64.b64decode(seen[0]["image"].split(",", 1)[-1]) == data


def test_positive_control_700kb_image_breaks_a_64kib_join(monkeypatch, tmp_path, capsys):
    """Same request against asyncio's default limit: the join never sees it, the CLI gets
    no answer (the live symptom). Proves the test above really exercises the limit."""
    f = tmp_path / "shot.png"
    f.write_bytes(_png(700 * 1024))
    rc, seen = _show_over_socket(monkeypatch, tmp_path,
                                 ["show", "--card", "image", "--file", str(f), "--context", CTX],
                                 limit=64 * 1024)
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert seen == [] and rc != 0 and out["ok"] is False, out


def test_too_large_image_is_a_clear_error_not_a_crash(monkeypatch, tmp_path, capsys):
    f = tmp_path / "huge.png"
    f.write_bytes(_png(vc.RASTER_MAX + 1024))
    rc, seen = _show_over_socket(monkeypatch, tmp_path, ["show", "--card", "image", "--file", str(f), "--context", CTX])
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert rc == 2 and out["type"] == "invalid" and "image max" in out["error"] and seen == [], out


def test_over_limit_request_answers_instead_of_broken_pipe(monkeypatch, tmp_path):
    """Client: a line over 4 MB is refused before sending. Join: a line over its limit gets
    an answer, and the client reads it instead of dying on BrokenPipeError."""
    from voicehook_agent import session as vs
    sock = tmp_path / "s.sock"

    async def handler(req):
        return {"ok": True}

    async def go():
        srv = vs.ControlServer(sock, handler)
        await srv.start()
        try:
            pre = await asyncio.to_thread(vs.request, sock, {"image": "A" * (vs.REQUEST_MAX + 1)}, 10)
            monkeypatch.setattr(vs, "REQUEST_MAX", 64 * 1024 * 1024)  # client side only now
            late = await asyncio.to_thread(vs.request, sock, {"image": "A" * (6 * 1024 * 1024)}, 10)
        finally:
            await srv.close()
        return pre, late
    pre, late = asyncio.run(go())
    assert pre["ok"] is False and pre["type"] == "invalid" and "too large" in pre["error"]
    assert late["ok"] is False and "too large" in late["error"], late
