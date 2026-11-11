"""speaker_owner (user / transcript turns) and from_owner / from_owner_agent (data events):
only from what LiveKit signs (participant kind, token attributes), never from names or packet
fields of a client. Fail-closed: unsure -> false."""
from __future__ import annotations

import asyncio
import base64
import io
import json
from types import SimpleNamespace
from typing import ClassVar

import pytest

from voicehook_agent import agentkey, calltranscript, datachan, relay
from voicehook_agent import session as vs

cli = pytest.importorskip("voicehook_agent.cli", reason="livekit/httpx not installed")

TAG = "a1b2c3d4e5f60718"
OTHER = "ffffffffffffffff"


def _jwt(attrs: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps({"attributes": attrs}).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJIUzI1NiJ9.{body}.sig"


def _p(identity, kind=0, **attrs):
    return SimpleNamespace(identity=identity, kind=kind, name="", attributes=attrs,
                           track_publications={})


OWNER_HUMAN = _p("host-1", **{"vh.owner": "1", "vh.owner_acct": TAG, "vh.name": "Oliver"})
GUEST_AS_OWNER = _p("guest-9", **{"vh.name": "Oliver"})   # calls itself like the owner
OWN_AGENT = _p("claude-a", **{"vh.role": "agent", "vh.name": "Scout", "vh.owner_agent": "1",
                              "vh.owner_acct": TAG})
FOREIGN_AGENT = _p("claude-x", **{"vh.role": "agent", "vh.name": "Scout", "vh.owner_agent": "1",
                                  "vh.owner_acct": OTHER})
PLAIN_AGENT = _p("codex-b", **{"vh.role": "agent", "vh.name": "Codex"})
WORKER = _p("agent-AJ_worker", kind=4)
BRIDGE_SENDER = SimpleNamespace(identity="claude-a")        # bridge: identity only

RAW = {"id": "m1", "kind": "text", "payload": "hallo", "to_identities": ["me"]}


# ---- data: from_owner / from_owner_agent ------------------------------------------------ #
def test_data_own_agent_with_matching_tag():
    ev = datachan.receive_event("me", OWN_AGENT, RAW, own_acct=TAG)
    assert ev["from_owner_agent"] is True and ev["from_owner"] is False


def test_data_owner_agent_with_foreign_tag_is_false():
    ev = datachan.receive_event("me", FOREIGN_AGENT, RAW, own_acct=TAG)
    assert ev["from_owner_agent"] is False and ev["from_owner"] is False


def test_data_owner_agent_without_own_tag_is_false():
    # this join is no owner agent (no tag in its token): nothing to compare with
    assert datachan.receive_event("me", OWN_AGENT, RAW, own_acct="")["from_owner_agent"] is False


def test_data_missing_attributes_is_false():
    ev = datachan.receive_event("me", PLAIN_AGENT, RAW, own_acct=TAG)
    assert ev["from_owner_agent"] is False and ev["from_owner"] is False
    assert datachan.from_owner(None) is False and datachan.from_owner_agent(None, TAG) is False
    assert datachan.from_owner(SimpleNamespace(identity="x", attributes=None)) is False


def test_data_owner_flag_from_signed_attribute_only():
    assert datachan.from_owner(OWNER_HUMAN) is True
    assert datachan.from_owner(GUEST_AS_OWNER) is False   # the name proves nothing


def test_data_bridge_is_false_even_if_the_packet_claims_true():
    bridge_ev = {"type": "data", "from": "Scout", "from_identity": "claude-a", "kind": "text",
                 "payload": "x", "from_owner": True, "from_owner_agent": True}
    for sender in (None, BRIDGE_SENDER):
        ev = datachan.receive_event("me", sender, bridge_ev, own_acct=TAG)
        assert ev["from_owner"] is False and ev["from_owner_agent"] is False


def test_own_acct_tag_from_own_token():
    assert agentkey.owner_acct_tag(_jwt({"vh.owner_agent": "1", "vh.owner_acct": TAG})) == TAG
    assert agentkey.owner_acct_tag(_jwt({"vh.owner_acct": TAG})) == ""        # no owner agent
    assert agentkey.owner_acct_tag(_jwt({"vh.owner_agent": "1"})) == ""       # no tag
    assert agentkey.owner_acct_tag("not-a-jwt") == "" and agentkey.owner_acct_tag(None) == ""


# ---- transcript: speaker_owner ---------------------------------------------------------- #
def test_speaker_owner_only_from_agent_kind_and_real_true():
    assert relay.speaker_owner({"speaker_owner": True}, WORKER) is True
    assert relay.speaker_owner({"speaker_owner": True}, OWNER_HUMAN) is False     # not AGENT
    assert relay.speaker_owner({"speaker_owner": True}, OWN_AGENT) is False       # operator, kind 0
    assert relay.speaker_owner({"speaker_owner": True}, BRIDGE_SENDER) is False   # no kind
    assert relay.speaker_owner({"speaker_owner": True}, None) is False
    assert relay.speaker_owner({"speaker_owner": "true"}, WORKER) is False        # not a bool
    assert relay.speaker_owner({"speaker_owner": 1}, WORKER) is False
    assert relay.speaker_owner({}, WORKER) is False                               # missing
    assert relay.speaker_owner({"speaker_owner": False}, WORKER) is False


def test_user_turn_event_carries_speaker_owner():
    assert relay.user_turn_event("user", "hi", {}, now=1.0)["speaker_owner"] is False
    assert relay.user_turn_event("user", "hi", {}, now=1.0, speaker_owner=True)["speaker_owner"] is True
    assert relay.helper_user_turn_event("x", "Scout")["speaker_owner"] is False


def test_call_transcript_lines_speaker_owner():
    pkt = {"by": "delta", "lines": [{"text": "a", "speaker_owner": True},
                                    {"text": "b", "speaker_owner": "yes"}, {"text": "c"}]}
    ok = calltranscript.with_speaker_owner(pkt, trusted=True)["lines"]
    assert [ln["speaker_owner"] for ln in ok] == [True, False, False]
    bad = calltranscript.with_speaker_owner(pkt, trusted=False)["lines"]
    assert [ln["speaker_owner"] for ln in bad] == [False, False, False]


# ---- end-to-end through the join (fake LiveKit room) ----------------------------------- #
SLUG = "abc-def-ghi-XYZ4"


class _LP:
    async def publish_data(self, data, reliable=True, topic="", **_):
        return None


class _Room:
    instances: ClassVar[list] = []

    def __init__(self):
        self.handlers = {}
        self.remote_participants = {p.identity: p for p in (WORKER, OWN_AGENT, FOREIGN_AGENT,
                                                            OWNER_HUMAN, GUEST_AS_OWNER)}
        self.local_participant = _LP()
        _Room.instances.append(self)

    def on(self, event, fn=None):
        self.handlers[event] = fn

    async def connect(self, url, token):
        return None

    async def disconnect(self):
        return None

    def emit(self, topic, obj, sender):
        self.handlers["data_received"](SimpleNamespace(
            topic=topic, data=json.dumps(obj).encode("utf-8"), participant=sender))


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("VOICEHOOK_AGENT_HOME", str(tmp_path / "vh"))
    monkeypatch.setattr(cli.rtc, "Room", _Room)

    async def _mint(*a, **k):
        return {"url": "wss://fake", "token": _jwt({"vh.owner_agent": "1", "vh.owner_acct": TAG})}

    monkeypatch.setattr(cli, "_mint_token", _mint)
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    _Room.instances = []


async def _call(req):
    sock = await asyncio.to_thread(vs.resolve_socket, None, 5.0)
    return await asyncio.to_thread(vs.request, sock, req, 10.0)


def test_join_end_to_end(env):
    async def run():
        join = asyncio.create_task(cli._join(f"https://voicehook.example/r/{SLUG}", "me", "Claude",
                                             True, None, model="opus-5.5", idle_timeout=0))
        await _call({"cmd": "status"})
        await _call({"cmd": "next", "timeout": 0.1})   # arms the queue
        room = _Room.instances[0]
        out = []
        for sender, obj in (
            (WORKER, {"role": "user", "text": "eins", "speaker": "Oliver", "speaker_owner": True}),
            (GUEST_AS_OWNER, {"role": "user", "text": "zwei", "speaker": "Oliver", "speaker_owner": True}),
            (BRIDGE_SENDER, {"role": "user", "text": "drei", "speaker_owner": True}),
            (WORKER, {"role": "user", "text": "vier", "speaker": "Gast"}),
        ):
            room.emit("transcript", obj, sender)
            out.append(await _call({"cmd": "next", "timeout": 2}))
        for sender in (OWN_AGENT, FOREIGN_AGENT):
            room.emit(datachan.TOPIC, RAW, sender)
            out.append(await _call({"cmd": "next", "timeout": 2}))
        await _call({"cmd": "leave"})
        await asyncio.wait_for(join, 5)
        return out

    u1, u2, u3, u4, d1, d2 = asyncio.run(run())
    # 0.36.1: a transcript only from the worker; the guest's and the unknown bridge sender's lines
    # never reach `next` (dropped, counted, one _meta line)
    assert [u.get("text") for u in (u1, u2, u3, u4)] == ["eins", None, None, "vier"], \
        [(u.get("type"), u.get("text")) for u in (u1, u2, u3, u4, d1, d2)]
    assert [u["speaker_owner"] for u in (u1, u4)] == [True, False]
    assert d1["type"] == "data" and d1["from_owner_agent"] is True and d1["from_owner"] is False
    assert d2["type"] == "data" and d2["from_owner_agent"] is False
