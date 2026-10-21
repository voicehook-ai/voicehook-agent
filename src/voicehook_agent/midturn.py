"""Call events inside the running turn (0.21.7).

Live 20.10.2026 (Oliver): the operator agent is in the call but deep in a long tool chain; it
sees his new questions only when its turn ends (the background `listen` wakes it then), and
he waits for minutes. The productive behavior now lives in the product:

* The PostToolUse hook of the session that owns the join asks its join (control socket,
  `{"cmd":"take_wake"}`, 1 s) for unread events of TYPES (user, decision, helper_decision)
  and injects them as `hookSpecificOutput.additionalContext` into the running turn:
  "Oliver hat im Call gesagt (vor N s): <text>. Antworte ZUERST per voicehook-agent say,
  delegiere längere Arbeit." A user turn without a `say` since it was spoken and older than
  WAIT_S adds "Oliver wartet seit N s".
* Decision (consume, show once): each such event is shown mid-turn exactly once
  (spool.take). One still unread is CONSUMED there (spool cursor, acked), so the background
  `listen` never delivers it a second time. One that `listen` already took (it takes an
  event within milliseconds, but its exit may only wake the agent at the end of the turn)
  is shown anyway, marked "kam auch über listen": better once twice than minutes late.
  The hook's additionalContext is the delivery; PostToolUse is used because Claude Code
  reliably shows its additionalContext in the running turn (PreToolUse is not used: an
  event taken there could get lost). 0.21.9: a stale drop (revise with stale=true: our
  queued says were older than the server's limit and never spoken) is shown here too.
  Other events (status_request, a normal revise, voicebot_left,
  ...) stay for `listen` only.
* Never for subagents (agent_id), other sessions (owner_session), another agent process
  with the same session id (0.22.2: owner_agent_pid, listenguard.own_join_dirs) or without
  a live join.

VOICEHOOK_MIDTURN_EVENTS=0 turns it off. Stdlib only (runs inside the hook), never raises.
"""
from __future__ import annotations

import json
import os
import time

ENV_OFF = "VOICEHOOK_MIDTURN_EVENTS"
# 0.21.9: "revise:stale" = only the stale drops (a normal revise stays for `listen`)
TYPES = ("user", "decision", "helper_decision", "onboarding", "revise:stale")
WAIT_S = 20.0
TIMEOUT_S = 1.0
TEXT_MAX = 400
RULE = ("Im Call: jede Äußerung zuerst beantworten (say), Arbeit über 2 Tool-Calls an "
        "Hintergrund-Agenten geben, Turns kurz halten.")
RULE_EN = ("In a call: answer every utterance first (say), hand work longer than 2 tool calls "
           "to background agents, keep turns short.")


def off(env=None) -> bool:
    env = os.environ if env is None else env
    return (env.get(ENV_OFF) or "").strip().lower() in ("0", "false", "no", "off")


def _age(ts: object, now: float) -> int | None:
    try:
        return max(0, int(now - float(ts)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _line(ev: dict, now: float, last_say: float | None) -> str:
    who = str(ev.get("speaker") or "Der Mensch").strip()[:40]
    age = _age(ev.get("ts"), now)
    when = f" (vor {age} s)" if age is not None else ""
    typ = ev.get("type")
    if ev.get("stale") is True:  # 0.21.9: our queued says were dropped unspoken (too old)
        items = [i for i in (ev.get("items") or []) if isinstance(i, dict)]
        texts = " ".join(f"\"{str(i.get('text') or '').strip()[:160]}\"" for i in items[:5])
        more = f" (+{len(items) - 5})" if len(items) > 5 else ""
        return (f"Veraltet, nicht gesprochen ({len(items)} Aussage(n)): {texts}{more}. "
                "Nicht wiederholen; falls noch relevant, EINE kurze aktuelle Zusammenfassung "
                "neu senden (`voicehook-agent say --mode overwrite`).")
    if typ == "onboarding":  # 0.21.8: offer the human to bring in the other sessions
        cmds = ev.get("commands") if isinstance(ev.get("commands"), dict) else {}
        inv = "; ".join(f"`{c}`" for c in (cmds.get("invite") or [])[:4])
        return f"{str(ev.get('text') or '').strip()[:300]}{' Befehle: ' + inv + '.' if inv else ''}"
    if typ == "user":
        text = str(ev.get("text") or "").strip()[:TEXT_MAX]
        line = f"{who} hat im Call gesagt{when}: \"{text}\"."
        try:
            unanswered = last_say is None or float(last_say) < float(ev.get("ts"))
        except (TypeError, ValueError):
            unanswered = False
        if unanswered and age is not None and age >= WAIT_S:
            line += f" {who} wartet seit {age} s."
        return line
    verdict = str(ev.get("option_label") or ev.get("text") or ev.get("verdict") or "").strip()[:200]
    num = ev.get("number")
    q = str(ev.get("question") or "").strip()[:160]
    ref = f"#{num}" if num is not None else (ev.get("id") or "")
    if typ == "helper_decision":
        return (f"Antwort im Call an den Helfer {str(ev.get('from') or ev.get('label') or '')[:40]} "
                f"zu {ref} \"{q}\": {verdict}.")
    return f"Entscheidung im Call zu {ref}{' ' + repr(q) if q else ''}: {verdict}."


def text_for(reply: dict) -> str | None:
    """The additionalContext for a take_wake reply (None: nothing new)."""
    evs = [e for e in (reply.get("events") or []) if isinstance(e, dict)]
    if not evs:
        return None
    now = float(reply.get("now") or time.time())
    lines = [_line(e, now, reply.get("last_say_at")) + (" (kam auch über listen)" if e.get("delivered")
                                                         else "") for e in evs]
    has_user = any(e.get("type") in ("user", "onboarding") for e in evs)
    tail = ("Antworte ZUERST per `voicehook-agent say \"...\"`, delegiere längere Arbeit an einen "
            "Hintergrund-Agenten, dann weiter." if has_user else
            "" if all(e.get("stale") is True for e in evs) else
            "Bestätige kurz per `voicehook-agent say`, dann handle danach.")
    return ("voicehook-Call (neu, während du arbeitest): " + " ".join(lines) + (" " + tail if tail else "")
            + f" {RULE} (Neue Ereignisse sind damit gelesen, `listen` bringt sie nicht noch einmal.)")


def take(dir_) -> dict | None:
    from . import session as vsession
    sock = vsession.socket_path(dir_)
    try:
        if vsession.is_fallback(sock):
            vsession.check_private_dir(sock.parent)
        if not vsession.socket_alive(sock, timeout=TIMEOUT_S):
            return None
        return vsession.request(sock, {"cmd": "take_wake", "types": list(TYPES)}, timeout=TIMEOUT_S)
    except (OSError, ValueError, vsession.SessionError):
        return None


def handle(raw: str) -> dict | None:
    """PostToolUse of the joining session: new call events as additionalContext. Never raises."""
    try:
        if off():
            return None
        event = json.loads(raw) if raw and raw.strip() else {}
        if not isinstance(event, dict) or str(event.get("agent_id") or "").strip():
            return None  # a subagent: the operator answers, not its background agent
        from . import listenguard
        for d in listenguard.own_join_dirs(event):
            reply = take(d)
            text = text_for(reply) if isinstance(reply, dict) and reply.get("ok") else None
            if text:
                name = str(event.get("hook_event_name") or "PostToolUse")
                return {"hookSpecificOutput": {"hookEventName": name, "additionalContext": text}}
        return None
    except Exception:  # noqa: BLE001
        return None


def merge(first: dict | None, second: dict | None) -> dict | None:
    """Two additionalContext answers into one (a decision block wins)."""
    if not first or not second:
        return first or second
    if first.get("decision") == "block" or second.get("decision") == "block":
        return first if first.get("decision") == "block" else second
    a = (first.get("hookSpecificOutput") or {}).get("additionalContext") or ""
    b = (second.get("hookSpecificOutput") or {}).get("additionalContext") or ""
    name = (second.get("hookSpecificOutput") or {}).get("hookEventName") or "PostToolUse"
    return {"hookSpecificOutput": {"hookEventName": name, "additionalContext": f"{b} {a}".strip()}}
