"""`show` + `say --shape-*`: shapes and emotion for the ring (operator.visual).

Parsing and validation like the server (range 0..1, <=200 points, <=2 KB, only
M L Q C Z, multi <=4, label <=24, hold 800..8000), invalid -> exit 2 and nothing
sent, transport choice: WebRTC data channel topic operator.visual vs. bridge
POST /api/bridge/visual."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from voicehook_agent import visual as vv

cli = pytest.importorskip("voicehook_agent.cli", reason="livekit/httpx not installed")
from voicehook_agent import relay
from voicehook_agent import transport as vt

HOUSE = "0.2,0.9 0.2,0.45 0.5,0.15 0.8,0.45 0.8,0.9"


# ----- validation ------------------------------------------------------------------------
def test_preset_ok_and_hold_label_kept():
    s = vv.validate_shape({"type": "preset", "name": "check", "hold_ms": 2000, "label": " Ja "})
    assert s == {"type": "preset", "name": "check", "hold_ms": 2000, "label": "Ja"}


def test_polygon_default_closed_and_open_line():
    s = vv.validate_shape({"type": "polygon", "points": [[0, 0], [1, 0], [0.5, 1]]})
    assert s["closed"] is True and s["points"] == [[0.0, 0.0], [1.0, 0.0], [0.5, 1.0]]
    line = vv.validate_shape({"type": "polygon", "points": [[0, 0], [1, 1]], "closed": False})
    assert line["closed"] is False
    with pytest.raises(vv.VisualError, match="at least 3 points"):
        vv.validate_shape({"type": "polygon", "points": [[0, 0], [1, 1]]})


def test_path_ok_counts_pairs():
    d = "M0.1,0.9 L0.5,0.1 Q0.7,0.2 0.9,0.9 C0.1,0.1 0.2,0.2 0.3,0.3 Z"
    assert vv.path_points(d) == 1 + 1 + 2 + 3
    assert vv.validate_shape({"type": "path", "d": d})["d"] == d


@pytest.mark.parametrize("d,msg", [
    ("M0.1,0.1 A0.5,0.5 0 0 1 0.9,0.9", "'A' not allowed"),
    ("m0.1,0.1 l0.9,0.9", "uppercase"),
    ("L0.1,0.1", "must start with M"),
    ("M0.1,0.1 L0.9", "needs 2 numbers"),
    ("M0.1,0.1 L1.5,0.5", "0..1"),
    ("M0.1,0.1 Z0.2", "Z takes no numbers"),
    ("M0.1,0.1; L0.2,0.2", "not allowed"),
    ("", "non-empty"),
])
def test_path_rejects(d, msg):
    with pytest.raises(vv.VisualError, match=msg):
        vv.validate_shape({"type": "path", "d": d})


@pytest.mark.parametrize("shape,msg", [
    ({"type": "preset", "name": "star"}, "unknown"),
    ({"type": "circle"}, "type must be"),
    ({"type": "polygon", "points": [[0, 0], [1, 0], [0.5, -0.1]]}, "point 3 y must be in 0..1"),
    ({"type": "polygon", "points": [[0, 0], [1, 0], [0.5, True]]}, "must be a number"),
    ({"type": "polygon", "points": [[0, 0], [1, 0], [0.5]]}, r"\[x,y\]"),
    ({"type": "polygon", "points": [[0, 0], [1, 0], [0.5, float("nan")]]}, "0..1"),
    ({"type": "polygon", "points": [[0, 0], [1, 0], [0.5, 1]], "closed": "yes"}, "true or false"),
    ({"type": "preset", "name": "check", "hold_ms": 799}, "800..8000"),
    ({"type": "preset", "name": "check", "hold_ms": 8001}, "800..8000"),
    ({"type": "preset", "name": "check", "hold_ms": 1000.5}, "whole number"),
    ({"type": "preset", "name": "check", "label": "x" * 25}, "max 24"),
    ({"type": "multi", "items": []}, "non-empty"),
    ({"type": "multi", "items": [{"type": "preset", "name": "one"}] * 5}, "max 4"),
    ({"type": "multi", "items": [{"type": "multi", "items": []}]}, "nested"),
    ({"type": "multi", "items": [{"type": "preset", "name": "x"}]}, "multi item 1"),
    ("check", "JSON object"),
])
def test_shape_rejects(shape, msg):
    with pytest.raises(vv.VisualError, match=msg):
        vv.validate_shape(shape)


def test_point_limit_over_whole_multi():
    pts = [[0.5, 0.5]] * 100
    ok = {"type": "multi", "items": [{"type": "polygon", "points": pts[:3]}] * 2}
    vv.validate_shape(ok)
    too_many = {"type": "multi", "items": [{"type": "polygon", "points": pts},
                                           {"type": "polygon", "points": pts},
                                           {"type": "polygon", "points": pts[:3]}]}
    with pytest.raises(vv.VisualError, match="203 points, max 200"):
        vv.validate_shape(too_many)


def test_size_limits_path_2kb_shape_8kb():
    ok = [[0.123456789, 0.987654321]] * 200  # 200 points fit into 8 KB
    vv.validate_shape({"type": "polygon", "points": ok})
    big = [[0.12345678901234568, 0.9876543209876543]] * 200  # ~8.2 KB
    with pytest.raises(vv.VisualError, match="bytes as JSON, max 8192"):
        vv.validate_shape({"type": "polygon", "points": big})
    d = "M0.1,0.1 " + "L0.12345678901234568,0.9876543209876543 " * 60  # 61 pairs, >2 KB
    with pytest.raises(vv.VisualError, match="path d is .* bytes, max 2048"):
        vv.validate_shape({"type": "path", "d": d})


@pytest.mark.parametrize("label,msg", [
    ("Haus <b>", "< or >"),
    ("Haus\nDach", "control characters"),
    ("Haus\u200d", "control characters"),
    ("Haus \U0001F3E0", "emoji"),
    ("Ja \u2705", "emoji"),
])
def test_label_rejects(label, msg):
    with pytest.raises(vv.VisualError, match=msg):
        vv.validate_shape({"type": "preset", "name": "check", "label": label})
    assert vv.validate_shape({"type": "preset", "name": "check", "label": "Größe 1-3, ok?"})


def test_emotion_label_and_object():
    assert vv.validate_emotion("Joy") == {"label": "joy", "valence": 0.8, "arousal": 0.6}
    assert vv.validate_emotion({"label": "sad", "valence": -0.5}) == {
        "label": "sad", "valence": -0.5, "arousal": 0.3}
    with pytest.raises(vv.VisualError, match="unknown"):
        vv.validate_emotion("angry")
    with pytest.raises(vv.VisualError, match="arousal must be in 0..1"):
        vv.validate_emotion({"label": "joy", "arousal": 2})
    assert vv.parse_emotion_arg('{"label":"calm","valence":0.1}')["valence"] == 0.1


# ----- CLI parsing ------------------------------------------------------------------------
def test_parse_points():
    assert vv.parse_points(" 0.1,0.9  0.5,0.1 ") == [[0.1, 0.9], [0.5, 0.1]]
    with pytest.raises(vv.VisualError, match="point 2 '0.5' must be x,y"):
        vv.parse_points("0.1,0.9 0.5")
    with pytest.raises(vv.VisualError, match="must be numbers"):
        vv.parse_points("a,b")


def test_shape_from_args_single_multi_json():
    s = vv.shape_from_args(polygons=[HOUSE], label="Haus", hold_ms=3000)
    assert s["type"] == "polygon" and s["closed"] and s["label"] == "Haus" and s["hold_ms"] == 3000
    assert vv.shape_from_args(polygons=["0,0 1,1"], open_=True)["closed"] is False
    m = vv.shape_from_args(presets=["one", "two"], paths=["M0,0 L1,1"], label="Drei", hold_ms=1500)
    assert m["type"] == "multi" and m["label"] == "Drei"
    assert [i["type"] for i in m["items"]] == ["preset", "preset", "path"]
    assert all(i["hold_ms"] == 1500 for i in m["items"])
    j = vv.shape_from_args(json_text='{"type":"preset","name":"loop"}', label="Kreis")
    assert j == {"type": "preset", "name": "loop", "label": "Kreis"}
    with pytest.raises(vv.VisualError, match="cannot be combined"):
        vv.shape_from_args(presets=["one"], json_text='{"type":"preset","name":"loop"}')
    with pytest.raises(vv.VisualError, match="not valid JSON"):
        vv.shape_from_args(json_text="{nope")
    with pytest.raises(vv.VisualError, match="nothing to show"):
        vv.shape_from_args()


def _args(argv):
    """Parse like main() does, without running a command."""
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


def test_client_request_show_and_say_fields():
    req = cli._client_request(_args(["show", "--polygon", HOUSE, "--label", "Haus",
                                      "--emotion", "calm"]))
    assert req["cmd"] == "visual" and req["shape"]["type"] == "polygon"
    assert req["shape"]["label"] == "Haus" and req["emotion"]["label"] == "calm"
    say = cli._client_request(_args(["say", "Erst testen, dann ausrollen.",
                                     "--shape-preset", "arrow_right", "--emotion", "joy"]))
    assert say["shape"] == {"type": "preset", "name": "arrow_right"}
    assert say["emotion"]["label"] == "joy" and say["text"] == "Erst testen, dann ausrollen."
    plain = cli._client_request(_args(["say", "Hallo"]))
    assert "shape" not in plain and "emotion" not in plain


def test_invalid_show_exits_2_and_sends_nothing(monkeypatch, capsys):
    def boom(*a, **k):
        raise AssertionError("must not contact the join")
    monkeypatch.setattr(cli.vsession, "resolve_socket", boom)
    monkeypatch.setattr(cli.vsession, "request", boom)
    with pytest.raises(SystemExit) as e:
        cli.main(["show", "--polygon", "0,0 1,0 1.5,1"])
    assert e.value.code == 2
    out = json.loads(capsys.readouterr().out.strip())
    assert out["ok"] is False and out["type"] == "invalid" and "0..1" in out["error"]
    with pytest.raises(SystemExit) as e:
        cli.main(["say", "Hallo", "--shape-json", '{"type":"path","d":"M0,0 A1,1 0 0 1 1,1"}'])
    assert e.value.code == 2


def test_valid_show_goes_to_the_join(monkeypatch, capsys):
    sent = {}
    monkeypatch.setattr(cli.vsession, "resolve_socket", lambda s, wait: "/sock")

    def req(sock, r, timeout=None):
        sent.update(r)
        return {"ok": True, "type": "visual", "via": "datachannel"}
    monkeypatch.setattr(cli.vsession, "request", req)
    with pytest.raises(SystemExit) as e:
        cli.main(["show", "--preset", "check", "--hold-ms", "2000"])
    assert e.value.code == 0
    assert sent == {"cmd": "visual", "shape": {"type": "preset", "name": "check", "hold_ms": 2000}}


# ----- transport choice -------------------------------------------------------------------
class _Local:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []

    async def publish_data(self, data, reliable=True, topic=""):
        self.published.append((topic, json.loads(data)))


def _ctl(room) -> cli._Control:
    ctl = cli._Control("blau-tiger", "claude-x", relay.SayTracker(), relay.EchoSuppressor(False), 0.0)
    ctl.attach(room)
    return ctl


SHAPE = {"type": "preset", "name": "check"}


def test_webrtc_publishes_operator_visual_topic():
    room = SimpleNamespace(local_participant=_Local(), remote_participants={})
    res = asyncio.run(cli._control_handler(_ctl(room), {"cmd": "visual", "shape": SHAPE,
                                                        "emotion": "joy"}))
    assert res["ok"] and res["via"] == "datachannel"
    assert room.local_participant.published == [
        ("operator.visual", {"shape": SHAPE,
                             "emotion": {"label": "joy", "valence": 0.8, "arousal": 0.6}})]


def test_bridge_posts_api_bridge_visual():
    calls = []

    class _BridgeLike(SimpleNamespace):
        async def send_visual(self, body):
            calls.append(body)
    room = _BridgeLike(local_participant=_Local(), remote_participants={})
    res = asyncio.run(cli._control_handler(_ctl(room), {"cmd": "visual", "shape": SHAPE}))
    assert res["ok"] and res["via"] == "bridge"
    assert calls == [{"shape": SHAPE}] and room.local_participant.published == []


def test_join_side_revalidates_and_reports_send_errors():
    room = SimpleNamespace(local_participant=_Local(), remote_participants={})
    ctl = _ctl(room)
    bad = asyncio.run(cli._control_handler(ctl, {"cmd": "visual", "shape": {"type": "preset",
                                                                            "name": "star"}}))
    assert bad["ok"] is False and bad["type"] == "invalid"
    assert room.local_participant.published == []

    class _Failing(SimpleNamespace):
        async def send_visual(self, body):
            raise vt.BridgeError("bridge send visual failed: HTTP 404 not found", status=404)
    res = asyncio.run(cli._control_handler(_ctl(_Failing(remote_participants={})),
                                           {"cmd": "visual", "shape": SHAPE}))
    assert res["ok"] is False and "HTTP 404" in res["error"]


def test_say_carries_shape_and_emotion_fields():
    room = SimpleNamespace(local_participant=_Local(), remote_participants={})
    ctl = _ctl(room)
    res = asyncio.run(cli._control_handler(ctl, {
        "cmd": "say", "text": "Ja, genau so.", "shape": SHAPE, "emotion": "joy"}))
    assert res["ok"]
    topic, say = room.local_participant.published[-1]
    assert topic == "operator.say" and say["text"] == "Ja, genau so."
    assert say["shape"] == SHAPE and say["emotion"]["label"] == "joy"
    bad = asyncio.run(cli._control_handler(ctl, {"cmd": "say", "text": "x",
                                                 "shape": {"type": "nope"}}))
    assert bad["ok"] is False and bad["type"] == "invalid"


def test_bridge_room_send_visual_http():
    import httpx
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append((req.url.path, req.headers.get("authorization"), json.loads(req.content)))
        return httpx.Response(200, json={"ok": True})

    async def go():
        room = vt.BridgeRoom("http://vh.test", "a-b-c-AB12", "x", name="C", model="m")
        room._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        room._session = "S3SSION"
        await room.send_visual({"shape": SHAPE})
        await room._http.aclose()
    asyncio.run(go())
    assert seen == [("/api/bridge/visual", "Bearer S3SSION", {"shape": SHAPE})]


def test_rate_limit_local_and_bridge_429(monkeypatch):
    room = SimpleNamespace(local_participant=_Local(), remote_participants={})
    ctl = _ctl(room)
    clock = [100.0]
    monkeypatch.setattr(cli.time, "monotonic", lambda: clock[0])
    req = {"cmd": "visual", "shape": SHAPE}
    first = asyncio.run(cli._control_handler(ctl, req))
    clock[0] = 101.0
    second = asyncio.run(cli._control_handler(ctl, req))
    clock[0] = 102.1
    third = asyncio.run(cli._control_handler(ctl, req))
    assert first["ok"] and third["ok"]
    assert second["ok"] is False and second["type"] == "rate_limited"
    assert "one shape per 2 s" in second["error"] and second["retry_after_s"] == 1.0
    assert len(room.local_participant.published) == 2

    class _Limited(SimpleNamespace):
        async def send_visual(self, body):
            raise vt.BridgeError("bridge send visual failed: HTTP 429 slow down", status=429)

    class _Rejected(SimpleNamespace):
        async def send_visual(self, body):
            raise vt.BridgeError("bridge send visual failed: HTTP 400 bad label", status=400)
    r429 = asyncio.run(cli._control_handler(_ctl(_Limited(remote_participants={})), req))
    assert r429 == {"ok": False, "type": "rate_limited", "error": cli.VISUAL_RATE_ERROR}
    r400 = asyncio.run(cli._control_handler(_ctl(_Rejected(remote_participants={})), req))
    assert r400["type"] == "invalid" and "bad label" in r400["error"]
