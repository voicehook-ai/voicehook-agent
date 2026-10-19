"""0.19.0: voicebot_left / voicebot_back in the CLI's own `next` (WebRTC and bridge transport),
the same events bridge/MCP `next` carry since voicehook-v4 #475."""
from __future__ import annotations

import asyncio

from test_session import (  # noqa: F401  (fixture)
    _call,
    _FakeRoom,
    _join,
    _peer,
    fake_env,
)

from voicehook_agent import relay


class Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


def test_watch_left_back_takeover_and_no_human():
    c = Clock()
    w = relay.VoicebotWatch(clock=c, wall=lambda: 1.0)
    assert w.left("user", ["agent"], True) is None                      # a human, not the bot
    assert w.left("agent", ["agent", "user"], True) is None             # takeover
    assert w.left("agent", ["user"], False) is None                     # nobody left: call ends
    ev = w.left("agent", ["user"], True, room="r1")
    assert ev["type"] == "voicebot_left" and ev["room"] == "r1" and "keep calling next" in ev["text"]
    assert w.left("agent", ["user"], True) is None                      # once
    assert w.joined("user") is None
    c.t += 17.4
    back = w.joined("agent", room="r1")
    assert back["type"] == "voicebot_back" and back["seconds"] == 17 and "17 s" in back["text"]
    assert w.joined("agent") is None                                    # nothing pending


def test_cli_next_returns_voicebot_left_and_back(fake_env):  # noqa: F811
    bot = _peer("agent-AJ_x", kind=4)
    human = _peer("host-ut46y7", kind=0)
    _FakeRoom.peers = {bot.identity: bot, human.identity: human}

    async def run():
        join = asyncio.create_task(_join(idle_timeout=0))
        for _ in range(50):
            if _FakeRoom.instances and "participant_disconnected" in _FakeRoom.instances[0].handlers:
                break
            await asyncio.sleep(0.05)
        room = _FakeRoom.instances[0]
        await asyncio.sleep(0.3)
        await _call({"cmd": "next", "timeout": 0})  # arms the queue (as any agent's first next)
        del room.remote_participants[bot.identity]
        room.handlers["participant_disconnected"](bot)
        left = await _call({"cmd": "next", "timeout": 2})
        new = _peer("agent-AJ_y", kind=4)
        room.remote_participants[new.identity] = new
        room.handlers["participant_connected"](new)
        back = await _call({"cmd": "next", "timeout": 2})
        await _call({"cmd": "leave"})
        await asyncio.wait_for(join, 5)
        return left, back

    left, back = asyncio.run(run())
    assert left["type"] == "voicebot_left", left
    assert back["type"] == "voicebot_back" and isinstance(back["seconds"], int), back
