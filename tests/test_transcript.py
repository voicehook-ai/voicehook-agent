"""`voicehook-agent transcript` and Delta's send_transcript (calltranscript.py) against a fake
worker that speaks the server protocol (operator.transcript_request -> operator.transcript)."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from voicehook_agent import calltranscript as ct
from voicehook_agent import cli, midturn, relay, spool

SECRET = "Geheimer Satz aus dem Gespräch"
WORKER = SimpleNamespace(identity="agent-worker", kind=4, name="Delta", attributes={})
HUMAN = SimpleNamespace(identity="host-1", kind=0, name="Oliver", attributes={})
OTHER_OP = SimpleNamespace(identity="codex-b", kind=0, name="Codex", attributes={"vh.role": "agent"})
PEERS = {p.identity: p for p in (WORKER, HUMAN, OTHER_OP)}


def _lines(n, t0=1_700_000_000.0):
    return [{"id": f"l{i}", "t": t0 + 60 * i, "speaker": "Oliver" if i % 2 == 0 else "Delta",
             "role": "user" if i % 2 == 0 else "agent", "text": f"{SECRET} {i} " + "x" * 300}
            for i in range(n)]


def _packets(head, lines, truncated=False, part_bytes=3000):
    """The server's calltranscript.packets in short (same envelope, small parts)."""
    groups, cur, used = [], [], 0
    for ln in lines:
        size = len(json.dumps(ln)) + 1
        if cur and used + size > part_bytes:
            groups.append(cur)
            cur, used = [], 0
        cur.append(ln)
        used += size
    groups.append(cur)
    return [{**head, "part": i + 1, "parts": len(groups), "truncated": truncated,
             "total": len(lines), "lines": g} for i, g in enumerate(groups)]


class FakeWorker:
    """local_participant: answers operator.transcript_request like the server's worker."""

    def __init__(self, lines, owner=True, reverse=False, drop_last=False):
        self.lines, self.owner, self.reverse, self.drop_last = lines, owner, reverse, drop_last
        self.sent = []
        self.ctl = None
        self.room = None

    async def publish_data(self, data, reliable=True, topic="", destination_identities=None):
        body = json.loads(data)
        self.sent.append((topic, body, destination_identities))
        if topic != ct.TOPIC_REQUEST:
            return
        if not self.owner:
            pkts = [{"id": body["id"], "error": "not_allowed"}]
        else:
            lines = self.lines
            if "max_lines" in body:
                lines = lines[-body["max_lines"]:]
            pkts = _packets({"id": body["id"]}, lines)
        if self.reverse:
            pkts = pkts[::-1]
        if self.drop_last:
            pkts = pkts[:-1]
        loop = asyncio.get_running_loop()
        for p in pkts:
            pkt = SimpleNamespace(participant=WORKER, topic=ct.TOPIC, data=json.dumps(p).encode())
            loop.call_soon(cli._on_transcript, self.ctl, self.room, pkt, p, True)


def _setup(worker):
    room = SimpleNamespace(local_participant=worker, remote_participants=dict(PEERS))
    ctl = cli._Control("room", "claude-me", relay.SayTracker(), relay.EchoSuppressor(False), 0.0)
    ctl.attach(room)
    worker.ctl, worker.room = ctl, room
    return ctl, room


def test_request_assembles_all_parts_in_order(capsys):
    lines = _lines(40)
    w = FakeWorker(lines, reverse=True)
    ctl, _ = _setup(w)
    out = asyncio.run(cli._control_handler(ctl, {"cmd": "transcript", "since_s": 600, "max_lines": 30}))
    topic, body, dest = w.sent[0]
    assert topic == "operator.transcript_request" and dest == ["agent-worker"]
    assert body["since_s"] == 600 and body["max_lines"] == 30 and body["id"].startswith("tr-")
    assert out["ok"] and out["n"] == 30 and out["parts"] > 1
    assert [x["id"] for x in out["lines"]] == [x["id"] for x in lines[-30:]]
    assert ctl.transcript_waits == {} and len(ctl.transcripts) == 0
    assert SECRET not in capsys.readouterr().out  # the join's stdout never gets the lines


def test_not_allowed_clear_message():
    ctl, _ = _setup(FakeWorker([], owner=False))
    out = asyncio.run(cli._control_handler(ctl, {"cmd": "transcript"}))
    assert out["ok"] is False and out["error"] == "not_allowed"
    assert "eigene Agenten des Inhabers mit Schlüssel" in out["message"]


def test_timeout_on_missing_part():
    ctl, _ = _setup(FakeWorker(_lines(40), drop_last=True))
    out = asyncio.run(cli._control_handler(ctl, {"cmd": "transcript", "timeout": 1}))
    assert out["ok"] is False and out["error"] == "timeout"
    assert out["parts"] > 1 and out["parts_received"] == out["parts"] - 1
    assert len(ctl.transcripts) == 0


def test_no_worker():
    w = FakeWorker([])
    ctl, room = _setup(w)
    room.remote_participants = {"host-1": HUMAN}
    out = asyncio.run(cli._control_handler(ctl, {"cmd": "transcript"}))
    assert out["ok"] is False and out["status"] == 503 and w.sent == []


def _deliver(ctl, room, pkts, sender=WORKER):
    for p in pkts:
        cli._on_transcript(ctl, room, SimpleNamespace(participant=sender), p, True)


def test_delta_send_becomes_one_event(capsys):
    async def run():
        ctl, room = _setup(FakeWorker([]))
        ctl.events.arm()
        _deliver(ctl, room, _packets({"by": "delta", "minutes": 5}, _lines(20)))
        assert len(ctl.events) == 1
        ev = await ctl.events.get(0)
        return ev
    ev = asyncio.run(run())
    assert ev["type"] == "transcript" and ev["by"] == "delta" and ev["minutes"] == 5
    assert ev["n"] == 20 and len(ev["lines"]) == 20 and SECRET in ev["text"]
    assert ev["summary"] == "Delta hat dir den Verlauf der letzten 5 Minuten geschickt (20 Zeilen)."
    out = capsys.readouterr().out
    assert SECRET not in out and '"n": 20' in out
    assert "transcript" in spool.WAKE_TYPES and "transcript" in cli.LISTEN_WAKE


def test_foreign_sender_dropped():
    async def run():
        ctl, room = _setup(FakeWorker([]))
        ctl.events.arm()
        pk = _packets({"by": "delta"}, _lines(2))
        _deliver(ctl, room, pk, sender=HUMAN)
        _deliver(ctl, room, pk, sender=OTHER_OP)
        n_bad = len(ctl.events)
        _deliver(ctl, room, pk, sender=SimpleNamespace(identity="agent-worker"))  # bridge: no kind
        return n_bad, len(ctl.events)
    assert asyncio.run(run()) == (0, 1)


def test_spool_file_keeps_only_metadata(tmp_path):
    async def run():
        q = spool.SpoolQueue(tmp_path, armed=True)
        q.put_nowait(ct.delta_event({"lines": _lines(3), "minutes": 2, "total": 3}))
        await asyncio.sleep(0)
        disk = (tmp_path / spool.EVENTS_NAME).read_text()
        assert SECRET not in disk and '"volatile": true' in disk and '"n": 3' in disk
        # mid-turn: announced without content, NOT consumed
        shown = q.take(midturn.TYPES)
        assert len(shown) == 1 and "lines" not in shown[0] and q.unread() == 1
        assert q.take(midturn.TYPES) == []  # announced once
        text = midturn.text_for({"ok": True, "events": shown, "now": 0})
        assert "Delta hat dir den Verlauf der letzten 2 Minuten geschickt (3 Zeilen)" in text
        assert SECRET not in text and "voicehook-agent next" in text
        ev = await q.get(0)  # next/listen still delivers the full content
        assert len(ev["lines"]) == 3
        # compaction rewrites the file: still no content
        q.put_nowait(ct.delta_event({"lines": _lines(2)}))
        await asyncio.sleep(0)
        q._compact()
        assert SECRET not in (tmp_path / spool.EVENTS_NAME).read_text()
        # a join restart: the stub comes back marked lost, with a hint
        q2 = spool.SpoolQueue(tmp_path)
        assert q2.resume() == 1
        ev2 = await q2.get(0)
        assert ev2["type"] == "transcript" and ev2["lost"] is True and ev2["lines"] == []
        assert "voicehook-agent transcript" in ev2["hint"]
    asyncio.run(run())


def test_render_and_print(capsys):
    t = 1_700_000_000.0
    txt = ct.render([{"t": t, "speaker": "Oliver", "text": "Hallo"},
                     {"t": t, "speaker": "Delta", "text": "Hi", "interrupted": True}])
    hhmm = __import__("time").strftime("%H:%M", __import__("time").localtime(t))
    assert txt == f"{hhmm} Oliver: Hallo\n{hhmm} Delta: Hi [unterbrochen]"
    assert cli._print_transcript({"ok": False, "error": "not_allowed",
                                  "message": ct.NOT_ALLOWED_DE}) == 1
    assert "eigene Agenten des Inhabers" in capsys.readouterr().out
    assert cli._print_transcript({"ok": True, "lines": []}) == 0
    assert "noch keine Zeilen" in capsys.readouterr().out


def _parse(argv):
    seen = {}
    orig = cli._run_client
    cli._run_client = lambda a: seen.setdefault("a", a) and 0
    try:
        try:
            cli.main(argv)
        except SystemExit:
            pass
    finally:
        cli._run_client = orig
    return cli._client_request(seen["a"]), seen["a"]


def test_cli_requests():
    req, a = _parse(["transcript", "--since-min", "5", "--max-lines", "100", "--json"])
    assert req == {"cmd": "transcript", "timeout": 10.0, "since_s": 300, "max_lines": 100}
    assert a.json is True
    req, a = _parse(["transcript", "--session", "abc"])
    assert req == {"cmd": "transcript", "timeout": 10.0} and a.session == "abc" and a.json is False


def test_assembler_new_delta_send_restarts():
    a = ct.Assembler()
    p = _packets({"by": "delta"}, _lines(40))
    assert len(p) > 2
    assert a.add(p[0]) is None
    assert a.add(p[0]) is None  # a new send (part 1 again) restarts, no mixing
    for x in p[1:-1]:
        assert a.add(x) is None
    key, res = a.add(p[-1])
    assert key == ct.DELTA_KEY and len(res["lines"]) == 40
    assert a.add({"part": 1, "parts": 1, "lines": []}) is None  # neither id nor by: dropped
