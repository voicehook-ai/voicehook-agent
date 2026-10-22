"""0.25.0 Artefakt-Karten: PDF, CSV and Markdown (and images) as cards in the call, plus
attachments on the status board (`status --attach`).

Server contract (voicehook-v4, built in parallel):
1. WebRTC join: POST /api/card/file {kind, name, data(base64)} -> {ok, id, url, mime, name, size,
   kind}; then the CLI publishes operator.card {id, seq, kind, title, text, at, url, mime, name,
   size} and waits for vh.card_ack like for images. 404/405 = old server, 413/422/429 errors.
2. Helper: POST /api/activity/relay/show {from, kind, title, text, name, data}; the server
   publishes and confirms itself.
3. Board field attach [{id, name, kind}] (max 8), also on relay/status.
4. The pump passes `attach` of a helper's status payload through unchanged."""
from __future__ import annotations

import asyncio
import base64
import json
import os
from types import SimpleNamespace

import pytest

from voicehook_agent import cards as vc

cli = pytest.importorskip("voicehook_agent.cli", reason="livekit/httpx not installed")
import httpx

from voicehook_agent import helperaccess as vhelp
from voicehook_agent import relay
from voicehook_agent import relayops as ro

CID = "0123456789abcdef"
CID2 = "fedcba9876543210"
URL = f"/api/card/blau-tiger/{CID}?e=1&s=x"
PDF = b"%PDF-1.7\n" + b"0" * 2000 + b"\n%%EOF\n"
CSV = b"Name;Wert\nUmsatz;12\n"
MD = "# Bericht\n\nAlles grün.\n".encode()


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("VOICEHOOK_PLAIN_CHECK", "0")


def _no_join(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("must not contact the join")
    monkeypatch.setattr(cli.vsession, "resolve_socket", boom)
    monkeypatch.setattr(cli.vsession, "request", boom)
    monkeypatch.setattr(cli, "_helper_relay", lambda *a, **k: None)


def _main(argv, capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(argv)
    out = capsys.readouterr().out
    return e.value.code, [json.loads(x) for x in out.splitlines() if x.strip()]


def _req(argv):
    seen = {}
    orig = cli._run_client
    cli._run_client = lambda args: seen.setdefault("args", args) and 0
    try:
        with pytest.raises(SystemExit):
            cli.main(argv)
    finally:
        cli._run_client = orig
    return cli._client_request(seen["args"])


# ----- client: kind from the extension, limits -------------------------------------------------
@pytest.mark.parametrize("name,kind", [
    ("Bericht.pdf", "pdf"), ("x.CSV", "csv"), ("notes.md", "md"), ("README.markdown", "md"),
    ("shot.png", "image"), ("a.jpg", "image"), ("a.jpeg", "image"), ("a.webp", "image"),
    ("plan.svg", "image")])
def test_kind_from_extension(name, kind):
    assert vc.kind_from_name(f"/tmp/dir/{name}") == kind


def test_show_file_without_card_detects_kind_and_defaults_title(tmp_path):
    f = tmp_path / "Quartal Bericht.pdf"
    f.write_bytes(PDF)
    r = _req(["show", "--file", str(f)])
    assert r == {"cmd": "card", "kind": "pdf", "name": "Quartal Bericht.pdf", "title": "Quartal Bericht.pdf",
                 "data": base64.b64encode(PDF).decode()}
    c = tmp_path / "zahlen.csv"
    c.write_bytes(CSV)
    r = _req(["show", "--card", "csv", "--file", str(c), "--title", "Zahlen", "--text", "Stand heute"])
    assert r["kind"] == "csv" and r["title"] == "Zahlen" and r["text"] == "Stand heute"
    assert base64.b64decode(r["data"]) == CSV and r["name"] == "zahlen.csv"
    m = tmp_path / "n.markdown"
    m.write_bytes(MD)
    assert _req(["show", "--file", str(m)])["kind"] == "md"
    # an image by extension (magic bytes still decide the type)
    p = tmp_path / "shot.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    r = _req(["show", "--file", str(p)])
    assert r["kind"] == "image" and r["title"] == "shot.png"


def _big(path, n, head=b"%PDF-1.7\n"):
    with open(path, "wb") as fh:
        fh.write(head)
        fh.truncate(n)


@pytest.mark.parametrize("name,make,msg", [
    ("big.pdf", lambda f: _big(f, 11 * 1024 * 1024), "pdf max 10 MB"),
    ("fake.pdf", lambda f: f.write_bytes(b"<html>no pdf</html>"), "starts with %PDF-"),
    ("big.csv", lambda f: _big(f, 2 * 1024 * 1024 + 1, b"a;b\n"), "csv max 2 MB"),
    ("latin.csv", lambda f: f.write_bytes("Größe;1\n".encode("latin-1")), "csv must be UTF-8"),
    ("nul.md", lambda f: f.write_bytes(b"# a\x00b"), "no NUL"),
    ("empty.md", lambda f: f.write_bytes(b"  \n"), "md file is empty"),
    ("notes.txt", lambda f: f.write_bytes(b"x"), "extension must be"),
])
def test_bad_files_exit_2_nothing_sent(tmp_path, monkeypatch, capsys, name, make, msg):
    _no_join(monkeypatch)
    f = tmp_path / name
    make(f)
    rc, lines = _main(["show", "--file", str(f)], capsys)
    assert rc == 2 and lines[0]["type"] == "invalid" and msg in lines[0]["error"], lines


def test_pdf_card_needs_file(monkeypatch, capsys):
    _no_join(monkeypatch)
    rc, lines = _main(["show", "--card", "pdf", "--text", "x"], capsys)
    assert rc == 2 and "needs --file" in lines[0]["error"]


def test_10mb_pdf_goes_through_the_control_socket(monkeypatch, tmp_path, capsys):
    """10 MB PDF = ~13.4 MB base64: the control socket (0.25.0: 16 MB) takes it whole."""
    from voicehook_agent import session as vs
    f = tmp_path / "big.pdf"
    data = b"%PDF-1.7\n" + os.urandom(vc.PDF_MAX - 9)
    f.write_bytes(data)
    sock = tmp_path / "c.sock"
    seen: list[dict] = []

    async def handler(req):
        seen.append(req)
        return {"ok": True, "type": "card", "kind": req.get("kind"), "id": CID}

    monkeypatch.setattr(cli.vsession, "resolve_socket", lambda *a, **k: sock)
    monkeypatch.setattr(cli, "_helper_relay", lambda *a, **k: None)

    def run_main():
        try:
            cli.main(["show", "--file", str(f)])
        except SystemExit as e:
            return e.code
        return 0

    async def go():
        srv = vs.ControlServer(sock, handler, read_timeout=20.0)
        await srv.start()
        try:
            return await asyncio.to_thread(run_main)
        finally:
            await srv.close()
    rc = asyncio.run(go())
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert rc == 0 and out["ok"] is True, out
    assert len(seen) == 1 and base64.b64decode(seen[0]["data"]) == data


# ----- join: upload + operator.card + ack ------------------------------------------------------
class _Local:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict, list | None]] = []
        self.ctl = None
        self.ack: bool | None = True

    async def publish_data(self, data, reliable=True, topic="", destination_identities=None):
        obj = json.loads(data)
        self.published.append((topic, obj, destination_identities))
        if topic == "operator.card" and "url" in obj and self.ctl is not None and self.ack is not None:
            asyncio.get_running_loop().call_soon(self.ctl.card_acks.note, {"id": obj["id"], "ok": self.ack}, "host")


def _room():
    return SimpleNamespace(local_participant=_Local(), remote_participants={
        "host": SimpleNamespace(identity="host", kind=0, attributes={}),
        "voice-ai": SimpleNamespace(identity="voice-ai", kind=4, attributes={})})


def _ctl(room) -> cli._Control:
    ctl = cli._Control("blau-tiger", "claude-x", relay.SayTracker(), relay.EchoSuppressor(False), 0.0)
    ctl.attach(room)
    ctl.api_base, ctl.lk_jwt = "http://vh.test", "LK.JWT.TOKEN"
    room.local_participant.ctl = ctl
    return ctl


def _mock_http(monkeypatch, handler):
    real = httpx.AsyncClient

    def factory(*a, **k):
        k["transport"] = httpx.MockTransport(handler)
        return real(*a, **k)
    monkeypatch.setattr(cli.httpx, "AsyncClient", factory)


def _pdf_req(**extra):
    return {"cmd": "card", "kind": "pdf", "name": "Bericht.pdf", "title": "Bericht",
            "data": base64.b64encode(PDF).decode(), **extra}


def test_webrtc_pdf_uploaded_then_published_ok_after_ack(monkeypatch):
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append((req.url.path, req.headers.get("authorization"), json.loads(req.content)))
        return httpx.Response(200, json={"ok": True, "id": CID, "url": URL, "mime": "application/pdf",
                                         "name": "Bericht.pdf", "size": len(PDF), "kind": "pdf"})
    _mock_http(monkeypatch, handler)
    room = _room()
    res = asyncio.run(cli._control_handler(_ctl(room), _pdf_req(text="Seite 3 lesen")))
    assert res["ok"] and res["id"] == CID and res["shown"] == 1 and res["kind"] == "pdf"
    assert res["name"] == "Bericht.pdf" and res["size"] == len(PDF)
    assert seen == [("/api/card/file", "Bearer LK.JWT.TOKEN",
                     {"kind": "pdf", "name": "Bericht.pdf", "data": base64.b64encode(PDF).decode()})]
    (t, card, dest), = room.local_participant.published
    assert t == "operator.card" and dest is None
    assert card["id"] == CID and card["kind"] == "pdf" and card["url"] == URL
    assert card["mime"] == "application/pdf" and card["name"] == "Bericht.pdf" and card["size"] == len(PDF)
    assert card["title"] == "Bericht" and card["text"] == "Seite 3 lesen" and card["seq"] == 1
    assert "data" not in card and "PDF-1.7" not in json.dumps(card)   # never the file itself


def test_pdf_without_page_answer_is_never_ok(monkeypatch):
    _mock_http(monkeypatch, lambda r: httpx.Response(200, json={"ok": True, "id": CID, "url": URL,
                                                                "mime": "application/pdf"}))
    monkeypatch.setattr(vc, "FILE_ACK_WAIT_S", 0.2)
    room = _room()
    room.local_participant.ack = None
    res = asyncio.run(cli._control_handler(_ctl(room), _pdf_req()))
    assert res["ok"] is False and res["type"] == "not_shown" and "file" in res["error"]
    room.local_participant.ack = False
    res = asyncio.run(cli._control_handler(_ctl(room), _pdf_req()))
    assert res["ok"] is False and "did not load" in res["error"]


def test_pdf_without_human_is_no_human(monkeypatch):
    _mock_http(monkeypatch, lambda r: httpx.Response(200, json={"ok": True, "id": CID, "url": URL,
                                                                "mime": "application/pdf"}))
    room = SimpleNamespace(local_participant=_Local(), remote_participants={})
    res = asyncio.run(cli._control_handler(_ctl(room), _pdf_req()))
    assert res["ok"] is False and res["type"] == "no_human" and res["id"] == CID


def test_file_upload_errors_old_server_and_limits(monkeypatch):
    answers = iter([httpx.Response(404, json={"detail": "Not Found"}),
                    httpx.Response(405, json={"detail": "Method Not Allowed"}),
                    httpx.Response(413, json={"detail": "pdf max 10 MB"}),
                    httpx.Response(422, json={"detail": "pdf must be a PDF file"}),
                    httpx.Response(429, json={"detail": "slow down"}),
                    httpx.Response(200, json={"ok": True, "id": CID, "url": "https://evil.example/x.pdf"})])
    _mock_http(monkeypatch, lambda req: next(answers))
    room = _room()
    ctl = _ctl(room)
    run = lambda: asyncio.run(cli._control_handler(ctl, _pdf_req()))
    for _ in range(2):
        old = run()
        assert old["ok"] is False and old["error"] == cli.CARD_FILE_OLD_SERVER
        assert "newer server" in old["error"] and "/api/card/file" in old["error"]
    big = run()
    assert big["type"] == "invalid" and "too large" in big["error"]
    assert run()["type"] == "invalid"
    assert run()["type"] == "rate_limited"
    assert run()["ok"] is False   # foreign URL never published
    assert room.local_participant.published == []


def test_join_revalidates_file_cards():
    room = _room()
    ctl = _ctl(room)
    bad = asyncio.run(cli._control_handler(ctl, {**_pdf_req(), "data": base64.b64encode(b"<html>").decode()}))
    assert bad["type"] == "invalid" and "%PDF-" in bad["error"]
    bad = asyncio.run(cli._control_handler(ctl, {**_pdf_req(), "data": "!!no base64!!"}))
    assert bad["type"] == "invalid"
    assert room.local_participant.published == []


def test_bridge_show_carries_kind_data_name():
    calls = []

    class _BridgeLike(SimpleNamespace):
        async def send_card(self, body):
            calls.append(body)
            return {"ok": True, "id": CID, "seq": 2}
    room = _BridgeLike(local_participant=_Local(), remote_participants={})
    res = asyncio.run(cli._control_handler(_ctl(room), _pdf_req()))
    assert res == {"ok": True, "type": "card", "via": "bridge", "id": CID, "seq": 2, "kind": "pdf"}
    assert calls == [{"kind": "pdf", "name": "Bericht.pdf", "data": base64.b64encode(PDF).decode(),
                      "title": "Bericht"}]


def test_helper_pdf_card_from_pump_is_published_with_name():
    room = _room()
    ctl = _ctl(room)
    card = {"id": CID, "kind": "pdf", "title": "Bericht", "text": "", "from": "Mac: Scout", "url": URL,
            "mime": "application/pdf", "name": "Bericht.pdf", "size": 2048, "at": 1.0}
    res = asyncio.run(cli._publish_helper_card(ctl, room, card))
    assert res == {"ok": True, "shown": 1}
    (_, pkt, _), = room.local_participant.published
    assert pkt["kind"] == "pdf" and pkt["name"] == "Bericht.pdf" and pkt["size"] == 2048 and pkt["url"] == URL
    assert vc.relay_card({**card, "url": "https://evil.example/x.pdf"}, 2) is None


# ----- helper: relay/show with data + name ------------------------------------------------------
def _relay(monkeypatch, answers: dict[str, list]):
    calls = []

    def fake_http(rel, method, url, body, timeout):
        name = url.rsplit("/", 1)[-1]
        calls.append((name, body))
        q = answers.get(name) or []
        return q.pop(0) if q else (200, {"ok": True})
    monkeypatch.setattr(ro, "_http", fake_http)
    monkeypatch.setattr(cli, "_helper_relay", lambda *a, **k: {
        "url": "https://vh.test/api/activity/relay", "token": "fake.relay.test.only", "kind": "server"})
    monkeypatch.setattr(ro, "session_label", lambda: "Scout")
    return calls


def test_helper_show_pdf_goes_to_relay_show_with_data_and_name(monkeypatch, tmp_path, capsys):
    calls = _relay(monkeypatch, {"show": [(200, {"ok": True, "type": "card", "id": CID, "kind": "pdf",
                                                 "from": "Scout", "shown": 1})]})
    f = tmp_path / "Bericht.pdf"
    f.write_bytes(PDF)
    rc, lines = _main(["show", "--file", str(f), "--text", "Kurz lesen"], capsys)
    assert rc == 0 and lines[0] == {"ok": True, "id": CID, "kind": "pdf", "from": "Scout", "shown": 1,
                                    "type": "card", "via": "relay"}
    (name, body), = calls
    assert name == "show" and body == {"from": "Scout", "kind": "pdf", "title": "Bericht.pdf",
                                       "text": "Kurz lesen", "name": "Bericht.pdf",
                                       "data": base64.b64encode(PDF).decode()}


def test_helper_show_not_shown_409_and_old_server(monkeypatch, tmp_path, capsys):
    calls = _relay(monkeypatch, {"show": [(409, {"detail": "no human in the call"}),
                                          (422, {"detail": "card must be one of text code link image"})]})
    f = tmp_path / "z.csv"
    f.write_bytes(CSV)
    rc, lines = _main(["show", "--file", str(f)], capsys)
    assert rc == 1 and lines[0]["status"] == 409 and "no human" in lines[0]["error"]
    rc, lines = _main(["show", "--file", str(f)], capsys)
    assert rc == 1 and "newer server" in lines[0]["error"]
    assert len(calls) == 2


# ----- status --attach ---------------------------------------------------------------------------
def _files(tmp_path):
    a, b = tmp_path / "Bericht.pdf", tmp_path / "zahlen.csv"
    a.write_bytes(PDF)
    b.write_bytes(CSV)
    return str(a), str(b)


def test_status_attach_join_uploads_each_then_board_with_both_ids(monkeypatch, tmp_path, capsys):
    a, b = _files(tmp_path)
    sent = []
    ids = iter([CID, CID2])

    def fake_request(sock, req, timeout=None):
        sent.append((req, timeout))
        if req["cmd"] == "card":
            return {"ok": True, "type": "card", "id": next(ids), "kind": req["kind"], "shown": 1}
        return {"ok": True, "type": "board", "board": req["board"]}
    monkeypatch.setattr(cli.vsession, "resolve_socket", lambda *a, **k: "sock")
    monkeypatch.setattr(cli.vsession, "request", fake_request)
    monkeypatch.setattr(cli, "_helper_relay", lambda *a, **k: None)
    rc, lines = _main(["status", "--doing", "Bericht fertig", "--attach", a, "--attach", b], capsys)
    assert rc == 0, lines
    assert [r["cmd"] for r, _ in sent] == ["card", "card", "board"]
    assert sent[0][0]["kind"] == "pdf" and sent[1][0]["kind"] == "csv"
    assert sent[0][1] == cli.PHONE_SOCK_TIMEOUT_S["card"]
    board = sent[2][0]["board"]
    assert board["attach"] == [{"id": CID, "name": "Bericht.pdf", "kind": "pdf"},
                               {"id": CID2, "name": "zahlen.csv", "kind": "csv"}]
    assert "attach_cards" not in sent[2][0]
    assert lines[0]["board"]["attach"] == board["attach"]


def test_status_attach_join_card_not_shown_without_human_still_attaches(monkeypatch, tmp_path, capsys):
    a, _ = _files(tmp_path)
    sent = []

    def fake_request(sock, req, timeout=None):
        sent.append(req)
        if req["cmd"] == "card":
            return {"ok": False, "type": "no_human", "id": CID, "error": "no human in the call"}
        return {"ok": True, "type": "board", "board": req["board"]}
    monkeypatch.setattr(cli.vsession, "resolve_socket", lambda *a, **k: "sock")
    monkeypatch.setattr(cli.vsession, "request", fake_request)
    monkeypatch.setattr(cli, "_helper_relay", lambda *a, **k: None)
    rc, _ = _main(["status", "Bericht", "--attach", a], capsys)
    assert rc == 0 and sent[-1]["board"]["attach"][0]["id"] == CID


def test_status_attach_upload_fails_exit_1_status_not_sent(monkeypatch, tmp_path, capsys):
    a, b = _files(tmp_path)
    sent = []

    def fake_request(sock, req, timeout=None):
        sent.append(req)
        if req["kind"] == "csv":
            return {"ok": False, "type": "card", "error": cli.CARD_FILE_OLD_SERVER}
        return {"ok": True, "type": "card", "id": CID}
    monkeypatch.setattr(cli.vsession, "resolve_socket", lambda *a, **k: "sock")
    monkeypatch.setattr(cli.vsession, "request", fake_request)
    monkeypatch.setattr(cli, "_helper_relay", lambda *a, **k: None)
    rc, lines = _main(["status", "Bericht", "--attach", a, "--attach", b], capsys)
    assert rc == 1 and lines[0]["type"] == "attach" and "zahlen.csv" in lines[0]["error"]
    assert "status not sent" in lines[0]["error"] and "newer server" in lines[0]["error"]
    assert [r["cmd"] for r in sent] == ["card", "card"]   # no board


def test_status_attach_checks_all_files_before_sending(monkeypatch, tmp_path, capsys):
    _no_join(monkeypatch)
    a, _ = _files(tmp_path)
    big = tmp_path / "big.pdf"
    _big(big, 11 * 1024 * 1024)
    rc, lines = _main(["status", "x", "--attach", a, "--attach", str(big)], capsys)
    assert rc == 2 and "pdf max 10 MB" in lines[0]["error"]
    rc, lines = _main(["status", "x", *(["--attach", a] * 9)], capsys)
    assert rc == 2 and "max 8" in lines[0]["error"]
    rc, lines = _main(["status", "--attach", a], capsys)
    assert rc == 2 and "goes with a board" in lines[0]["error"]


def test_status_attach_helper_relay_show_then_status_with_ids(monkeypatch, tmp_path, capsys):
    a, b = _files(tmp_path)
    calls = _relay(monkeypatch, {"show": [(200, {"ok": True, "id": CID, "kind": "pdf", "shown": 1}),
                                          (200, {"ok": True, "id": CID2, "kind": "csv", "shown": 1})]})
    rc, lines = _main(["status", "--doing", "Bericht fertig", "--attach", a, "--attach", b], capsys)
    assert rc == 0, lines
    assert [n for n, _ in calls] == ["show", "show", "status"]
    assert calls[0][1]["kind"] == "pdf" and calls[0][1]["name"] == "Bericht.pdf" and "data" in calls[0][1]
    assert calls[2][1]["attach"] == [{"id": CID, "name": "Bericht.pdf", "kind": "pdf"},
                                     {"id": CID2, "name": "zahlen.csv", "kind": "csv"}]
    assert calls[2][1]["doing"] == "Bericht fertig" and calls[2][1]["from"] == "Scout"


def test_status_attach_helper_upload_fails_no_status(monkeypatch, tmp_path, capsys):
    a, b = _files(tmp_path)
    calls = _relay(monkeypatch, {"show": [(200, {"ok": True, "id": CID}), (413, {"detail": "csv max 2 MB"})]})
    rc, lines = _main(["status", "x", "--attach", a, "--attach", b], capsys)
    assert rc == 1 and lines[0]["via"] == "relay" and "csv max 2 MB" in lines[0]["error"]
    assert [n for n, _ in calls] == ["show", "show"]


def test_status_body_keeps_only_valid_attach():
    body = ro.status_body({"doing": "x", "attach": [
        {"id": CID, "name": "a.pdf", "kind": "pdf"}, {"id": "nothex", "name": "b", "kind": "pdf"},
        {"id": CID2, "name": "c.exe", "kind": "exe"}]})
    assert body["attach"] == [{"id": CID, "name": "a.pdf", "kind": "pdf"}]


# ----- pump: attach of a helper's status goes through unchanged ---------------------------------
def test_pump_passes_attach_through_and_join_publishes_it():
    payload = {"sub": "helper:abc", "from": "Mac: Scout", "doing": "Bericht",
               "attach": [{"id": CID, "name": "Bericht.pdf", "kind": "pdf"}]}
    published = []

    async def publish_status(p):
        published.append(p)

    async def nop(*_a):
        return {}

    async def go():
        pump = vhelp.Pump("https://vh.test", "https://vh.test/api/helper/pump", "PUMP")
        await pump._apply(None, {"kind": "status", "payload": payload}, publish_status, nop, lambda _l: None)
    asyncio.run(go())
    assert published == [payload]

    # and the join's own publish path (cli._run_helper_pump publish_status) sends it unfiltered
    room = _room()
    ctl = _ctl(room)
    res = asyncio.run(cli._control_handler(ctl, {"cmd": "board", "board": {
        "doing": "Bericht", "attach": [{"id": CID, "name": "Bericht.pdf", "kind": "pdf"}]}}))
    assert res["ok"]
    (t, pkt, _), = room.local_participant.published
    assert t == "operator.status" and pkt["attach"] == [{"id": CID, "name": "Bericht.pdf", "kind": "pdf"}]


def test_run_helper_pump_publish_status_does_not_filter():
    """Source check: the pump's publish_status publishes the payload as it came."""
    import inspect
    src = inspect.getsource(cli._run_helper_pump)
    assert 'data = json.dumps(payload, ensure_ascii=False)' in src
