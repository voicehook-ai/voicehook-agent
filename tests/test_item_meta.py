"""0.21.4: every status item, activity line and decision may carry a hidden `context` and
`faq` [{q, a}] (max 3) for the voicebot, never shown to the human.

Wire format (coordinated field names context / faq):
* status board: the board as before plus `meta` {doing: {context, faq}, open: [{..}],
  done: [{..}]} (open/done aligned with the board's lists, {} = nothing for that item);
  every faq also stays in the board-level `faq` (servers before 0.21.4 read only that);
* operator.activity: `meta` aligned with `lines`;
* decide: `faq` next to the existing `context`.
A server without the fields ignores them (extra keys), nothing breaks."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from voicehook_agent import cli, relay, relayops


def _parse(argv):
    seen = {}

    def grab(a):
        seen["args"] = a
        return 0
    orig = cli._run_client
    cli._run_client = grab
    try:
        cli.main(argv)
    except SystemExit:
        pass
    finally:
        cli._run_client = orig
    return cli._client_request(seen["args"])


def test_status_context_and_faq_attach_to_the_item_they_follow():
    req = _parse(["status", "Baue den Fix", "--context", "Ticket 12, Kunde wartet",
                  "--faq", "Wann fertig?::In 20 Minuten",
                  "--open", "Tests", "--context", "nur unit",
                  "--open", "Doku",
                  "--done", "Analyse", "--faq", "Was war es?::Ein Race", "--faq", "Wo?::board.py"])
    b = req["board"]
    assert b["doing"] == "Baue den Fix" and b["open"] == ["Tests", "Doku"] and b["done"] == ["Analyse"]
    assert b["meta"] == {
        "doing": {"context": "Ticket 12, Kunde wartet", "faq": [{"q": "Wann fertig?", "a": "In 20 Minuten"}]},
        "open": [{"context": "nur unit"}, {}],
        "done": [{"faq": [{"q": "Was war es?", "a": "Ein Race"}, {"q": "Wo?", "a": "board.py"}]}]}
    # every faq also on the board level, as servers before 0.21.4 read it
    assert [f["q"] for f in b["faq"]] == ["Wann fertig?", "Was war es?", "Wo?"]


def test_status_without_meta_unchanged():
    req = _parse(["status", "--doing", "x", "--open", "a"])
    assert req["board"] == {"doing": "x", "open": ["a"], "done": []}


def test_meta_aligned_after_board_file(tmp_path):
    f = tmp_path / "b.json"
    f.write_text(json.dumps({"doing": "d", "open": ["alt1", "alt2"]}))
    req = _parse(["status", "-f", str(f), "--open", "neu", "--context", "c"])
    assert req["board"]["open"] == ["alt1", "alt2", "neu"]
    assert req["board"]["meta"] == {"open": [{}, {}, {"context": "c"}]}


def test_item_faq_max_three_and_context_max():
    argv = ["status", "--doing", "x"]
    for i in range(5):
        argv += ["--faq", f"q{i}?::a{i}"]
    req = _parse(argv)
    assert len(req["board"]["meta"]["doing"]["faq"]) == relay.ITEM_FAQ_MAX
    with pytest.raises(ValueError):
        relay.item_meta("x" * (relay.ITEM_CONTEXT_MAX + 1), [])


def test_activity_and_decide_carry_context_and_faq():
    req = _parse(["activity", "Tests laufen", "--context", "pytest, 900 Tests", "--faq", "Wie lange?::2 Min"])
    assert req == {"cmd": "activity", "text": "Tests laufen", "context": "pytest, 900 Tests",
                   "faq": [{"q": "Wie lange?", "a": "2 Min"}]}
    req = _parse(["decide", "--question", "PR #99 mergen?", "--context", "CI grün",
                  "--faq", "Was ändert sich?::Zustand echt", "--faq", "Risiko?::gering"])
    assert req["faq"] == [{"q": "Was ändert sich?", "a": "Zustand echt"}, {"q": "Risiko?", "a": "gering"}]
    assert req["context"] == "CI grün"


def test_relay_bodies_keep_meta_and_faq_and_validate():
    board = {"doing": "x", "open": ["a"], "done": [],
             "meta": {"doing": {"context": "c"}, "open": [{"faq": [{"q": "q", "a": "a"}]}]}}
    body = relayops.status_body(board)
    assert body["meta"] == board["meta"]
    assert relayops._valid_status(body)["meta"] == board["meta"]
    bad = dict(body, meta={"open": [{}, {}]})            # longer than the open list
    assert relayops._valid_status(bad) is None
    assert relayops.decide_body({"question": "q?", "faq": [{"q": "a", "a": "b"}]})["faq"]
    assert relayops._valid_decide({"question": "q?", "faq": [{"q": "a", "a": "b"}]})["faq"]
    assert relayops._valid_decide({"question": "q?", "faq": "nope"}) is None


class _Local:
    def __init__(self):
        self.published = []

    async def publish_data(self, data, reliable=True, topic=""):
        self.published.append((topic, json.loads(data)))


def _ctl(tmp_path):
    ctl = cli._Control("room", "claude-x", relay.SayTracker(), relay.EchoSuppressor(False), 0.0)
    ctl.attach(SimpleNamespace(local_participant=_Local(), remote_participants={}))
    ctl.activity_path = tmp_path / "activity.log"
    return ctl


def test_join_sends_activity_meta_aligned_with_lines(tmp_path):
    ctl = _ctl(tmp_path)

    async def run():
        r1 = await cli._control_handler(ctl, {"cmd": "activity", "text": "eins"})
        r2 = await cli._control_handler(ctl, {"cmd": "activity", "text": "zwei", "context": "k",
                                              "faq": [{"q": "q", "a": "a"}]})
        await cli._publish_activity(ctl, [r1["line"], r2["line"]])
    asyncio.run(run())
    _topic, payload = ctl.room.local_participant.published[-1]
    assert payload["meta"] == [{}, {"context": "k", "faq": [{"q": "q", "a": "a"}]}]


def test_join_board_and_decide_pass_meta_through(tmp_path):
    ctl = _ctl(tmp_path)
    board = {"doing": "x", "open": [], "done": [], "meta": {"doing": {"context": "c"}}}
    asyncio.run(cli._control_handler(ctl, {"cmd": "board", "board": board}))
    assert ctl.room.local_participant.published[-1] == ("operator.status", board)
