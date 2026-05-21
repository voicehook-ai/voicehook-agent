"""Unit tests for the pure relay logic (no LiveKit / network needed)."""
from __future__ import annotations

import pytest

from voicehook_agent import relay


# --------------------------------------------------------------------------- #
# #11 — LineBuffer newline tolerance
# --------------------------------------------------------------------------- #
def test_linebuffer_yields_complete_lines():
    buf = relay.LineBuffer()
    assert list(buf.feed("a\nb\nc")) == ["a", "b"]
    assert buf.pending == "c"


def test_linebuffer_partial_then_completed():
    buf = relay.LineBuffer()
    assert list(buf.feed("hel")) == []
    assert list(buf.feed("lo\n")) == ["hello"]
    assert buf.pending == ""


def test_linebuffer_flush_surfaces_unterminated_tail():
    """The core #11 bug: a control line without trailing newline must NOT be
    silently swallowed — flush surfaces it as (line, complete=False)."""
    buf = relay.LineBuffer()
    assert list(buf.feed('{"topic":"senior.say","text":"hi"}')) == []  # no newline
    flushed = list(buf.flush())
    assert flushed == [('{"topic":"senior.say","text":"hi"}', False)]
    # buffer is drained after flush
    assert buf.pending == ""


def test_linebuffer_flush_ignores_blank_tail():
    buf = relay.LineBuffer()
    list(buf.feed("done\n   "))
    assert list(buf.flush()) == []  # whitespace-only tail → nothing to warn


def test_linebuffer_strips_carriage_return():
    buf = relay.LineBuffer()
    assert list(buf.feed("a\r\nb\r\n")) == ["a", "b"]


# --------------------------------------------------------------------------- #
# #12 — TurnNotifier
# --------------------------------------------------------------------------- #
def test_notifier_wakes_on_user_turn():
    n = relay.TurnNotifier(wake_only_user=True)
    d = n.consider("user", "hallo", room="r1", now=100.0)
    assert d.wake is True
    assert d.payload["role"] == "user"
    assert d.payload["text"] == "hallo"
    assert d.payload["room"] == "r1"
    assert d.payload["timestamp"] == 100.0


def test_notifier_filters_agent_role_when_user_only():
    n = relay.TurnNotifier(wake_only_user=True)
    assert n.consider("agent", "meine TTS", now=1.0).wake is False


def test_notifier_wake_all_includes_agent():
    n = relay.TurnNotifier(wake_only_user=False)
    assert n.consider("agent", "x", now=1.0).wake is True


def test_notifier_dedupes_identical_consecutive():
    n = relay.TurnNotifier()
    assert n.consider("user", "same", now=1.0).wake is True
    assert n.consider("user", "same", now=2.0).wake is False
    # different text wakes again
    assert n.consider("user", "other", now=3.0).wake is True
    # back to first text (not consecutive) wakes again
    assert n.consider("user", "same", now=4.0).wake is True


def test_notifier_skips_non_final():
    n = relay.TurnNotifier()
    assert n.consider("user", "partial", payload={"final": False}, now=1.0).wake is False
    assert n.consider("user", "done", payload={"final": True}, now=2.0).wake is True


def test_notifier_skips_empty():
    n = relay.TurnNotifier()
    assert n.consider("user", "   ", now=1.0).wake is False


# --------------------------------------------------------------------------- #
# #10 — EchoSuppressor
# --------------------------------------------------------------------------- #
def test_echo_suppresses_own_relayed_tts():
    e = relay.EchoSuppressor(enabled=True)
    e.record_sent("Hallo Olli, hier ist Claude.")
    assert e.should_suppress("agent", "hallo  olli,   hier ist claude.") is True


def test_echo_does_not_suppress_user():
    e = relay.EchoSuppressor(enabled=True)
    e.record_sent("foo")
    assert e.should_suppress("user", "foo") is False


def test_echo_consumes_match_so_genuine_repeat_shows():
    e = relay.EchoSuppressor(enabled=True)
    e.record_sent("repeat me")
    assert e.should_suppress("agent", "repeat me") is True
    # the same text again (not in ring anymore) is a genuine echo we let pass
    assert e.should_suppress("agent", "repeat me") is False


def test_echo_disabled_passes_everything():
    e = relay.EchoSuppressor(enabled=False)
    e.record_sent("foo")
    assert e.should_suppress("agent", "foo") is False


# --------------------------------------------------------------------------- #
# #9 — SayTracker
# --------------------------------------------------------------------------- #
def test_saytracker_tags_increasing_seq():
    t = relay.SayTracker()
    a = t.tag("one", now=1.0)
    b = t.tag("two", now=2.0)
    assert (a.seq, b.seq) == (1, 2)
    env = t.envelope(a)
    assert env["text"] == "one" and env["_seq"] == 1 and env["_ts"] == 1.0


def test_saytracker_ttl_expiry():
    t = relay.SayTracker(ttl=5.0)
    say = t.tag("hi", now=100.0)
    stale, reason = t.is_stale(say, now=104.0)
    assert stale is False
    stale, reason = t.is_stale(say, now=106.0)
    assert stale is True and "ttl" in reason


def test_saytracker_supersede_by_newer_user_turn():
    t = relay.SayTracker(ttl=None)
    say = t.tag("answer", now=10.0)
    # a user spoke AFTER we queued this say → it's now stale
    t.note_user_turn(now=12.0)
    stale, reason = t.is_stale(say, now=13.0)
    assert stale is True and "superseded" in reason


def test_saytracker_no_ttl_not_stale_without_supersede():
    t = relay.SayTracker(ttl=None)
    say = t.tag("answer", now=10.0)
    assert t.is_stale(say, now=9999.0) == (False, "")


# --------------------------------------------------------------------------- #
# #6 — backoff + terminal-disconnect classification
# --------------------------------------------------------------------------- #
def test_backoff_caps_and_grows():
    delays = list(relay.backoff_delays(base=1.0, factor=2.0, cap=10.0, attempts=6))
    assert delays == [1.0, 2.0, 4.0, 8.0, 10.0, 10.0]


def test_terminal_disconnect_classification():
    assert relay.is_terminal_disconnect("CLIENT_INITIATED") is True
    assert relay.is_terminal_disconnect("ROOM_DELETED") is True
    assert relay.is_terminal_disconnect("client_initiated") is True  # case-insensitive
    # transient → reconnect
    assert relay.is_terminal_disconnect("SIGNAL_CLOSE") is False
    assert relay.is_terminal_disconnect("STATE_MISMATCH") is False
    assert relay.is_terminal_disconnect("") is False
