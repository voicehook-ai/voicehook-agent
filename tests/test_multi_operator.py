"""0.11.0 multi-operator (v4 feat/multi-operator): say default append, --urgent,
--voice -> vh.voice, revise/say_status of other operators ignored, speaker/op passed on."""
from __future__ import annotations

import asyncio
import io
import json
from types import SimpleNamespace

import pytest
from test_session import _call, _FakeRoom, _join

from voicehook_agent import cli, relay
from voicehook_agent import transport as vt


@pytest.fixture
def fake_env(tmp_path, monkeypatch):
    """Same fake LiveKit room as test_session.fake_env."""
    monkeypatch.setenv("VOICEHOOK_AGENT_HOME", str(tmp_path / "vh"))
    monkeypatch.setattr(cli.rtc, "Room", _FakeRoom)

    async def _mint(*a, **k):
        return {"url": "wss://fake", "token": "t"}

    monkeypatch.setattr(cli, "_mint_token", _mint)
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    _FakeRoom.instances = []
    _FakeRoom.peers = {}
    return tmp_path / "vh"


# ----- say: default mode append, --urgent ------------------------------------------------
def test_client_request_default_mode_is_append():
    args = SimpleNamespace(cmd="say", text=["Hallo"], mode=None, urgent=False)
    assert cli._client_request(args) == {"cmd": "say", "text": "Hallo", "mode": "append"}
    args = SimpleNamespace(cmd="say", text=["Hallo"], mode="overwrite", urgent=True)
    assert cli._client_request(args) == {"cmd": "say", "text": "Hallo", "mode": "overwrite",
                                         "urgent": True}


def test_say_mode_choices_default_append():
    assert cli.SAY_DEFAULT_MODE == "append"
    assert cli._SAY_MODES[0] == "append" and set(cli._SAY_MODES) == {"append", "overwrite", "revise"}


class _Local:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []

    async def publish_data(self, data, reliable=True, topic=""):
        self.published.append((topic, json.loads(data)))


def _ctl() -> cli._Control:
    ctl = cli._Control("blau-tiger", "claude-x", relay.SayTracker(), relay.EchoSuppressor(False), 0.0)
    ctl.attach(SimpleNamespace(local_participant=_Local(), remote_participants={}))
    return ctl


def test_control_say_sends_append_explicitly_and_urgent_priority():
    async def go():
        ctl = _ctl()
        a = await cli._control_handler(ctl, {"cmd": "say", "text": "eins"})  # old client: no mode
        b = await cli._control_handler(ctl, {"cmd": "say", "text": "zwei", "mode": "append",
                                             "urgent": True})
        c = await cli._control_handler(ctl, {"cmd": "say", "text": "drei", "mode": "bogus"})
        return ctl, a, b, c
    ctl, a, b, c = asyncio.run(go())
    says = [p for t, p in ctl.room.local_participant.published if t == "operator.say"]
    assert a["ok"] and b["ok"] and not c["ok"]
    assert says[0]["mode"] == "append" and "priority" not in says[0]   # older servers: no revise default
    assert says[1]["mode"] == "append" and says[1]["priority"] == "urgent"


# ----- --voice -> token param / bridge join field ---------------------------------------
def test_mint_token_sends_voice(monkeypatch):
    sent = {}

    class _Resp:
        status_code = 200
        def json(self): return {"token": "t", "url": "wss://x"}

    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, params=None):
            sent["params"] = params
            return _Resp()

    monkeypatch.setattr(cli.httpx, "AsyncClient", _Client)
    asyncio.run(cli._mint_token("https://voicehook.ai", "r", "deepseek-1", name="DeepSeek",
                                model="v3", voice="Puck"))
    assert sent["params"]["voice"] == "Puck"
    asyncio.run(cli._mint_token("https://voicehook.ai", "r", "deepseek-1", name="DeepSeek", model="v3"))
    assert "voice" not in sent["params"]


def test_join_flag_voice_reaches_mint(fake_env, monkeypatch):
    got = {}

    async def _mint(*a, **k):
        got.update(k)
        return {"url": "wss://fake", "token": "t"}

    monkeypatch.setattr(cli, "_mint_token", _mint)

    async def run():
        join = asyncio.create_task(_join(idle_timeout=0, voice="Puck", no_greet=True))
        await _call({"cmd": "status"})
        await _call({"cmd": "leave"})
        return await asyncio.wait_for(join, 5)
    asyncio.run(run())
    assert got["voice"] == "Puck"


def test_main_join_voice_flag(monkeypatch):
    got = {}

    async def _fake_join(*a, **k):
        got.update(k)
        return 0

    monkeypatch.setattr(cli, "_join", _fake_join)
    try:
        cli.main(["join", "https://voicehook.ai/r/a-b-c-AB12?invite=x", "--name", "DeepSeek",
                  "--model", "v3", "--voice", "Puck"])
    except SystemExit as e:
        assert e.code == 0
    assert got["voice"] == "Puck"


def test_bridge_join_body_has_voice_only_when_set():
    bodies = []

    class _R:
        status_code = 200
        def json(self): return {"session": "s", "peers": [], "identity": "srv-id"}

    class _H:
        async def post(self, url, json=None, headers=None):
            bodies.append(json)
            return _R()
        async def aclose(self): pass

    async def go():
        for v in ("Puck", None):
            room = vt.BridgeRoom("https://x", "a-b-c-AB12", "id", name="D", model="m", voice=v,
                                 client_factory=lambda *_a, **_k: _H())
            room._sse_loop = lambda: asyncio.sleep(0)  # no SSE in this unit test
            await room.connect()
            assert room.identity == "srv-id"
            room._closed = True
    asyncio.run(go())
    assert bodies[0]["voice"] == "Puck" and "voice" not in bodies[1]


# ----- revise / say_status of other operators are ignored --------------------------------
def test_foreign_owner_helper():
    assert relay.foreign_owner({"owner": "deepseek-1"}, {"claude-1"})
    assert not relay.foreign_owner({"owner": "claude-1"}, {"claude-1"})
    assert not relay.foreign_owner({}, {"claude-1"})                 # old server: no field
    assert not relay.foreign_owner({"owner": "x"}, set())            # own identity unknown
    assert relay.foreign_owner({"op": "deepseek-1"}, "claude-1", key="op")


def test_foreign_revise_and_say_status_ignored_in_join(fake_env):
    async def run():
        join = asyncio.create_task(_join(idle_timeout=0, no_greet=True))
        st = await _call({"cmd": "status"})
        me = st["identity"]
        r_say = await _call({"cmd": "say", "text": "Hallo"})
        room = _FakeRoom.instances[0]
        room.emit("operator.revise", {"text": "REVISE fremd", "unspoken": ["ds"], "new": "x",
                                      "owner": "deepseek-1"})
        room.emit("operator.say_status", {"seq": r_say["seq"], "state": "spoken", "owner": "deepseek-1"})
        none = await _call({"cmd": "next", "timeout": 0.2})
        room.emit("operator.revise", {"text": "REVISE meins", "unspoken": ["c"], "new": "y", "owner": me})
        room.emit("operator.say_status", {"seq": r_say["seq"], "state": "queued", "owner": me})
        mine = await _call({"cmd": "next", "timeout": 2})
        await _call({"cmd": "leave"})
        await asyncio.wait_for(join, 5)
        return none, mine
    none, mine = asyncio.run(run())
    assert none["type"] == "timeout" and "say_status" not in none      # nothing foreign leaked
    assert mine["type"] == "revise" and mine["unspoken"] == ["c"]
    assert mine["say_status"] == [{"seq": 1, "state": "queued"}]


# ----- transcript speaker/op ---------------------------------------------------------------
def test_user_turn_event_carries_speaker_only_when_sent():
    ev = relay.user_turn_event("user", "Hallo", {"final": True, "speaker": "Oliver"}, now=1.0)
    assert ev["speaker"] == "Oliver"
    assert "speaker" not in relay.user_turn_event("user", "Hallo", {"final": True}, now=1.0)


def test_join_prints_speaker_and_op(fake_env, capsys):
    async def run():
        join = asyncio.create_task(_join(idle_timeout=0, no_greet=True))
        await _call({"cmd": "status"})
        room = _FakeRoom.instances[0]
        room.emit("transcript", {"role": "operator", "text": "Ich bin DeepSeek.",
                                 "speaker": "DeepSeek", "op": "deepseek-1"})
        room.emit("transcript", {"role": "agent", "text": "Alte Zeile"})
        await _call({"cmd": "leave"})
        await asyncio.wait_for(join, 5)
    asyncio.run(run())
    lines = [json.loads(x) for x in capsys.readouterr().out.splitlines() if x.startswith("{")]
    op = next(x for x in lines if x.get("text") == "Ich bin DeepSeek.")
    assert op["speaker"] == "DeepSeek" and op["op"] == "deepseek-1"
    old = next(x for x in lines if x.get("text") == "Alte Zeile")
    assert "speaker" not in old and "op" not in old
