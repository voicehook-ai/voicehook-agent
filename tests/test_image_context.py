"""0.31.0 image cards carry an invisible `context`: the voicebot Delta cannot see images, so the
full text content of the image (numbers, table rows, labels) travels with every image card.

Missing context for an image: exit 2, nothing sent. Limit = the card text limit (4000), over it
exit 2, never cut. The context lands in the operator.card packet, the bridge show body and the
helper relay show body, and is never logged (errors name lengths only)."""
from __future__ import annotations

import asyncio
import io
import json
import os
from types import SimpleNamespace

import pytest

from voicehook_agent import cards as vc

cli = pytest.importorskip("voicehook_agent.cli", reason="livekit/httpx not installed")
import httpx

from voicehook_agent import mcpserver, relay
from voicehook_agent import multisession as ms
from voicehook_agent import relayops as ro
from voicehook_agent import session as vsession

SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><rect width="10" height="10"/></svg>'
CID = "0123456789abcdef"
URL = f"/api/card/blau-tiger/{CID}?e=1&s=x"
CTX = "Tabelle Umsatz: Q1 1.250 EUR, Q2 1.900 EUR; Titel: Umsatz 2026"
PRIVATE_CTX = "INHALT-NUR-FUER-DELTA"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    for k in ("CLAUDE_PID", "VOICEHOOK_OWNER_PID", ms.LABEL_ENV, ms.MACHINE_LABEL_ENV,
              ms.NO_RELAY_ENV, "VOICEHOOK_SESSION", "CODEX_HOME"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("VOICEHOOK_AGENT_HOME", str(tmp_path / "home"))


def _args(argv):
    seen = {}
    orig = cli._run_client
    cli._run_client = lambda a: seen.setdefault("args", a) and 0
    try:
        with pytest.raises(SystemExit):
            cli.main(argv)
    finally:
        cli._run_client = orig
    return seen["args"]


def _req(argv, stdin=None, monkeypatch=None):
    if stdin is not None:
        monkeypatch.setattr(cli.sys, "stdin", io.StringIO(stdin))
    return cli._client_request(_args(argv))


@pytest.fixture
def svg(tmp_path):
    f = tmp_path / "chart.svg"
    f.write_text(SVG, encoding="utf-8")
    return str(f)


# ----- client: required for images -----------------------------------------------------------
def test_image_without_context_is_rejected_with_an_explanation(svg):
    with pytest.raises(vc.CardError) as e:
        _req(["show", "--card", "image", "--file", svg])
    assert "--context" in str(e.value) and "numbers, table rows, labels" in str(e.value)
    with pytest.raises(vc.CardError):
        _req(["show", "--card", "image", "--file", svg, "--context", "   "])


def test_image_without_context_exits_2_and_sends_nothing(svg, monkeypatch, capsys):
    monkeypatch.setattr(cli.vsession, "resolve_socket", lambda *a, **k: (_ for _ in ()).throw(AssertionError()))
    with pytest.raises(SystemExit) as e:
        cli.main(["show", "--card", "image", "--file", svg])
    assert e.value.code == 2 and "--context" in capsys.readouterr().out


def test_context_in_request_and_optional_for_text(svg):
    r = _req(["show", "--card", "image", "--file", svg, "--context", f"  {CTX}  "])
    assert r["context"] == CTX and r["kind"] == "image"
    assert "context" not in _req(["show", "--card", "text", "--text", "x"])
    assert _req(["show", "--card", "text", "--text", "x", "--context", "y"])["context"] == "y"


def test_context_needs_a_card():
    with pytest.raises(vc.CardError, match="need --card"):
        _req(["show", "--panel", "board", "--context", "x"])


def test_context_file_and_stdin(svg, tmp_path, monkeypatch):
    f = tmp_path / "ctx.txt"
    f.write_text(CTX + "\n", encoding="utf-8")
    assert _req(["show", "--card", "image", "--file", svg, "--context-file", str(f)])["context"] == CTX
    r = _req(["show", "--card", "image", "--file", svg, "--context-file", "-"], stdin=CTX, monkeypatch=monkeypatch)
    assert r["context"] == CTX
    r = _req(["show", "--card", "image", "--file", svg, "--context", "-"], stdin=CTX, monkeypatch=monkeypatch)
    assert r["context"] == CTX
    with pytest.raises(vc.CardError, match="cannot be combined"):
        _req(["show", "--card", "image", "--file", svg, "--context", "a", "--context-file", str(f)])
    with pytest.raises(vc.CardError, match="only one"):
        _req(["show", "--card", "text", "--text", "-", "--context-file", "-"], stdin="x", monkeypatch=monkeypatch)


def test_context_limit_is_the_text_limit_and_never_cuts(svg, tmp_path):
    assert vc.CONTEXT_MAX == vc.TEXT_MAX == 4000
    ok = "x" * 4000
    assert _req(["show", "--card", "image", "--file", svg, "--context", ok])["context"] == ok
    with pytest.raises(vc.CardError) as e:
        _req(["show", "--card", "image", "--file", svg, "--context", PRIVATE_CTX + "x" * 4000])
    assert "context max 4000" in str(e.value) and str(len(PRIVATE_CTX) + 4000) in str(e.value) and PRIVATE_CTX not in str(e.value)
    big = tmp_path / "big.txt"
    big.write_text("y" * 20000, encoding="utf-8")
    with pytest.raises(vc.CardError, match="context max 4000"):
        _req(["show", "--card", "image", "--file", svg, "--context-file", str(big)])


# ----- join: the published packet ------------------------------------------------------------
class _Local:
    def __init__(self):
        self.published = []
        self.ctl = None

    async def publish_data(self, data, reliable=True, topic="", destination_identities=None):
        obj = json.loads(data)
        self.published.append((topic, obj))
        if topic == "operator.card" and "url" in obj and self.ctl is not None:
            asyncio.get_running_loop().call_soon(self.ctl.card_acks.note, {"id": obj["id"], "ok": True}, "host")


def _room():
    return SimpleNamespace(local_participant=_Local(), remote_participants={
        "host": SimpleNamespace(identity="host", kind=0, attributes={})})


def _ctl(room):
    ctl = cli._Control("blau-tiger", "claude-x", relay.SayTracker(), relay.EchoSuppressor(False), 0.0)
    ctl.attach(room)
    ctl.api_base, ctl.lk_jwt = "http://vh.test", "LK.JWT.TOKEN"
    room.local_participant.ctl = ctl
    return ctl


def test_published_packet_carries_context_and_join_rejects_missing(monkeypatch):
    def handler(req):
        return httpx.Response(200, json={"ok": True, "id": CID, "url": URL, "mime": "image/svg+xml"})
    real = httpx.AsyncClient
    monkeypatch.setattr(cli.httpx, "AsyncClient",
                        lambda *a, **k: real(*a, **{**k, "transport": httpx.MockTransport(handler)}))
    room = _room()
    ctl = _ctl(room)
    res = asyncio.run(cli._control_handler(ctl, {"cmd": "card", "kind": "image", "image": SVG, "context": CTX}))
    assert res["ok"], res
    (topic, pkt), = room.local_participant.published
    assert topic == "operator.card" and pkt["context"] == CTX and pkt["kind"] == "image"
    room.local_participant.published.clear()
    bad = asyncio.run(cli._control_handler(ctl, {"cmd": "card", "kind": "image", "image": SVG}))
    assert bad["ok"] is False and bad["type"] == "invalid" and room.local_participant.published == []


def test_text_card_context_is_optional_but_published():
    room = _room()
    asyncio.run(cli._control_handler(_ctl(room), {"cmd": "card", "kind": "code", "text": "x", "context": "Zusatz"}))
    assert room.local_participant.published[0][1]["context"] == "Zusatz"
    room.local_participant.published.clear()
    asyncio.run(cli._control_handler(_ctl(room), {"cmd": "card", "kind": "code", "text": "x"}))
    assert "context" not in room.local_participant.published[0][1]


def test_bridge_show_body_carries_context():
    calls = []

    class _Bridge:
        def __init__(self):
            self.local_participant = _Local()
            self.remote_participants = {}

        async def send_card(self, body):
            calls.append(body)
            return {"ok": True, "id": CID, "seq": 1}
    asyncio.run(cli._control_handler(_bridge_ctl(_Bridge()),
                                     {"cmd": "card", "kind": "image", "image": SVG, "context": CTX}))
    assert calls == [{"kind": "image", "image": SVG, "context": CTX}]


def _bridge_ctl(room):
    ctl = cli._Control("blau-tiger", "claude-x", relay.SayTracker(), relay.EchoSuppressor(False), 0.0)
    ctl.attach(room)
    return ctl


def test_make_card_and_relay_card_keep_context():
    pkt = vc.make_card(seq=1, body={"kind": "text", "text": "t", "context": CTX})
    assert pkt["context"] == CTX
    card = {"id": CID, "kind": "image", "from": "Mac: Scout", "url": URL, "mime": "image/png",
            "context": CTX, "at": 1.0}
    assert vc.relay_card(card, 3)["context"] == CTX
    assert vc.relay_card({k: v for k, v in card.items() if k != "context"}, 3) is None  # image without
    assert vc.relay_card({**card, "context": "z" * 4001}, 3) is None  # dropped, never cut


# ----- helper relay ---------------------------------------------------------------------------
def test_helper_relay_body_carries_context_and_requires_it_for_images():
    body = ro.card_body({"kind": "image", "image": SVG, "context": CTX, "title": "T"})
    assert body["context"] == CTX
    with pytest.raises(ro.RelayError, match="context"):
        ro.card_body({"kind": "image", "image": SVG})
    assert ro._valid_card({"kind": "code", "text": "x", "context": " c "})["context"] == "c"
    assert "context" not in ro._valid_card({"kind": "code", "text": "x"})


def test_helper_remote_show_posts_context(monkeypatch):
    seen = []
    monkeypatch.setattr(ro, "_http", lambda rel, m, url, body, t: seen.append((url, body)) or (200, {"ok": True, "id": CID}))
    ro.remote_show({"url": "http://x/api/activity/relay", "token": "t"}, "Mac: Scout",
                   ro.card_body({"kind": "image", "image": SVG, "context": CTX}))
    assert seen[0][0].endswith("/show") and seen[0][1]["context"] == CTX and seen[0][1]["from"] == "Mac: Scout"


# ----- MCP show -------------------------------------------------------------------------------
def _call(name, args):
    msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}}
    out = io.StringIO()
    mcpserver.serve(io.StringIO(json.dumps(msg) + "\n"), out, agent="codex")
    res = json.loads(out.getvalue().splitlines()[0])["result"]
    return json.loads(res["content"][0]["text"]), res.get("isError", False)


def test_mcp_show_lists_context_and_rejects_image_without_it(tmp_path, monkeypatch):
    tool = next(t for t in mcpserver.TOOLS if t["name"] == "show")
    assert "context" in tool["inputSchema"]["properties"] and "REQUIRES" in tool["description"]
    f = tmp_path / "a.svg"
    f.write_text(SVG, encoding="utf-8")
    monkeypatch.setattr(vsession, "request", lambda *a, **k: (_ for _ in ()).throw(AssertionError("sent")))
    res, err = _call("show", {"card": "image", "file": str(f)})
    assert err and "context" in res["error"]
    res, err = _call("show", {"card": "image", "file": str(f), "context": "x" * 4001})
    assert err and "context max 4000" in res["error"]


def test_mcp_show_own_join_gets_context(tmp_path, monkeypatch):
    f = tmp_path / "a.svg"
    f.write_text(SVG, encoding="utf-8")
    seen = []
    monkeypatch.setattr(vsession, "resolve_socket", lambda s, wait=0.0: "/x/ctl.sock")
    monkeypatch.setattr(vsession, "request", lambda sock, req, timeout=None: seen.append(req) or {"ok": True})
    monkeypatch.setattr(ro, "client_relay", lambda s: None)
    monkeypatch.setattr(ro, "helper_problem", lambda s: None)
    _res, err = _call("show", {"card": "image", "file": str(f), "context": CTX, "title": "T"})
    assert not err and seen[-1]["cmd"] == "card" and seen[-1]["context"] == CTX and seen[-1]["kind"] == "image"


def test_mcp_show_helper_over_remote_relay(tmp_path, monkeypatch):
    f = tmp_path / "a.svg"
    f.write_text(SVG, encoding="utf-8")
    ms.write_relay("https://vh.test/api/activity/relay", "fake.relay.test.only", "room-a", 3600,
                   owner_pid=os.getpid(), owner_session=ms.session_hash("CLAUDE-JOINER"))
    monkeypatch.chdir("/")
    monkeypatch.setenv(ms.LABEL_ENV, "Codex Shop")
    calls = []
    monkeypatch.setattr(ro, "_http", lambda rel, m, url, body, t: calls.append((url, body)) or (200, {"ok": True, "id": CID}))
    res, err = _call("show", {"card": "image", "file": str(f), "context": CTX})
    assert not err and res["via"] == "relay"
    assert calls[-1][0].endswith("/show") and calls[-1][1]["context"] == CTX
