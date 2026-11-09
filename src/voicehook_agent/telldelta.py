"""`voicehook-agent tell-delta "<Anweisung>" [--wörtlich] [--vorrang] [--wait SEC]` (0.35.9).

The own main agent gives Delta (the voicebot) an instruction WITHOUT speaking itself: the
instruction goes into Delta's announcement list, the worker bundles and orders it (server and
worker part: voicehook-v4, Ravi). Transport (Ravi, 09.11.):

    join -> worker   topic `delta.tell` (reliable, only to the worker)
                     {"id": "<own id>", "text": "<instruction>", "verbatim": bool,
                      "priority": "normal" | "vorrang"}
    worker -> sender topic `delta.tell_status`
                     {"id", "state": "queued" | "spoken" | "covered" | "rejected", "reason"?}

verbatim = Delta speaks the text unchanged; priority "vorrang" = before the other announcements.
Only the own main agent: a join WITH `VOICEHOOK_AGENT_KEY` whose token carries
vh.owner_agent=1. A join without the key, a helper session (relay credential) or a guest get a
clear error and nothing is sent; the worker answers `rejected` / `not_owner_agent` as the
final word. stdlib only.
"""
from __future__ import annotations

import asyncio
import re
import secrets
import time

TOPIC = "delta.tell"
STATUS_TOPIC = "delta.tell_status"
CMD = "delta_tell"
STATES = ("queued", "spoken", "covered", "rejected")
FINAL = ("spoken", "covered", "rejected")
PRIORITIES = ("normal", "vorrang")
TEXT_MAX = 2000
ID_RX = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
KEEP = 50
SOCK_EXTRA_S = 30.0

HINT_NO_KEY = ("nur der eigene Haupt-Agent darf Delta Anweisungen geben: mit VOICEHOOK_AGENT_KEY "
               "beitreten (VOICEHOOK_AGENT_KEY=vhak_... voicehook-agent join <link>)")
HINT_NOT_OWNER = ("der Schlüssel gehört nicht zum Konto dieses Raums (owner-agent: not granted): "
                  "nur der eigene Haupt-Agent des Inhabers darf Delta Anweisungen geben")
HINT_HELPER = ("Helfer-Session ohne eigenen Join: tell-delta geht nur über den Join des Haupt-"
               "Agenten; schreib die Anweisung dem Haupt-Agenten (send) oder als activity")
HINT_JOIN = ("kein laufender Join: der Haupt-Agent tritt mit VOICEHOOK_AGENT_KEY bei "
             "(voicehook-agent join <link>)")
HINT_NO_DELTA = "Delta ist gerade nicht im Call, gleich noch einmal"
HINT_OLD_JOIN = "der laufende Join kennt tell-delta nicht: voicehook-agent rejoin"
NOTES = {
    "queued": "steht in Deltas Ansage-Liste",
    "spoken": "Delta hat es gesagt",
    "covered": "schon abgedeckt: Delta hatte es bereits gesagt oder eine Ansage umfasst es",
    "rejected": "abgelehnt",
}


class TellError(ValueError):
    pass


def new_id() -> str:
    return "dt-" + secrets.token_hex(5)


def clean_text(text: object) -> str:
    t = " ".join(str(text or "").split())
    if not t:
        raise TellError("Anweisung ist leer")
    if len(t) > TEXT_MAX:
        raise TellError(f"Anweisung max {TEXT_MAX} Zeichen")
    return t


def request(text: object, verbatim: bool = False, vorrang: bool = False, wait: float = 0.0) -> dict:
    """CLI args -> control request {cmd: delta_tell, text, verbatim, priority, wait}."""
    try:
        w = float(wait or 0)
    except (TypeError, ValueError):
        raise TellError("--wait braucht Sekunden") from None
    if w < 0:
        raise TellError("--wait braucht Sekunden >= 0")
    return {"cmd": CMD, "text": clean_text(text), "verbatim": bool(verbatim),
            "priority": "vorrang" if vorrang else "normal", "wait": w}


def payload(tid: str, req: dict) -> dict:
    """The `delta.tell` packet (Ravi's format, exactly these four fields)."""
    prio = req.get("priority") if req.get("priority") in PRIORITIES else "normal"
    return {"id": tid, "text": clean_text(req.get("text")), "verbatim": bool(req.get("verbatim")),
            "priority": prio}


def owner_problem(agent_key: bool, granted: bool | None) -> dict | None:
    """Refusal before anything is sent: no key = not the owner's main agent; a token that
    says not granted = the key is not the one of this room's account. granted None (bridge
    answer, unreadable token) = send, the worker decides."""
    if not agent_key:
        return {"error": "not_owner_agent", "hint": HINT_NO_KEY}
    if granted is False:
        return {"error": "not_owner_agent", "hint": HINT_NOT_OWNER}
    return None


def failed(error: str, hint: str | None = None, tid: str | None = None) -> dict:
    out: dict = {"ok": False, "type": "delta_tell", "error": error}
    if tid:
        out["id"] = tid
    if hint:
        out["hint"] = hint
    return out


def reject_hint(reason: str) -> str | None:
    if reason == "not_owner_agent":
        return HINT_NOT_OWNER
    return None


def answer(tid: str, rec: dict | None, waited: bool) -> dict:
    """One JSON line for the CLI: {ok, type, id, state, note, reason?, hint?, waited?}."""
    state = (rec or {}).get("state") or "sent"
    out: dict = {"ok": state != "rejected", "type": "delta_tell", "id": tid, "state": state}
    if state in NOTES:
        out["note"] = NOTES[state]
    reason = (rec or {}).get("reason")
    if reason:
        out["reason"] = reason
    if state == "rejected":
        out["error"] = reason or "rejected"
        h = reject_hint(str(reason or ""))
        if h:
            out["hint"] = h
    if waited and state not in FINAL:
        out["timeout"] = True
    return out


class Tracker:
    """Own `delta.tell` ids -> last state from `delta.tell_status`; waiters per id."""

    def __init__(self) -> None:
        self._recs: dict[str, dict] = {}
        self._events: dict[str, asyncio.Event] = {}

    def sent(self, tid: str, text: str, now: float | None = None) -> None:
        self._recs[tid] = {"id": tid, "state": "sent", "text": text[:80],
                           "at": time.time() if now is None else now}
        while len(self._recs) > KEEP:
            old = next(iter(self._recs))
            self._recs.pop(old)
            self._events.pop(old, None)

    def get(self, tid: str) -> dict | None:
        return self._recs.get(tid)

    def waiting(self, tid: str) -> bool:
        """A `tell-delta --wait` is waiting for this id right now (it shows the answer itself)."""
        return tid in self._events

    def table(self) -> list[dict]:
        return [{k: v for k, v in r.items() if k != "at"} for r in self._recs.values()]

    def update(self, data: object) -> dict | None:
        """Apply one `delta.tell_status` packet. Only own ids count. Returns the change."""
        if not isinstance(data, dict):
            return None
        tid, state = str(data.get("id") or ""), data.get("state")
        rec = self._recs.get(tid)
        if rec is None or state not in STATES:
            return None
        if rec["state"] in FINAL and state == "queued":
            return None   # a late queued never undoes spoken/covered/rejected
        rec["state"] = state
        reason = data.get("reason")
        if isinstance(reason, str) and reason:
            rec["reason"] = reason[:60]
        if state in FINAL:
            ev = self._events.get(tid)
            if ev is not None:
                ev.set()
        return {"id": tid, "state": state, **({"reason": rec["reason"]} if rec.get("reason") else {})}

    async def wait(self, tid: str, timeout: float) -> dict | None:
        rec = self._recs.get(tid)
        if rec is None or rec["state"] in FINAL or timeout <= 0:
            return rec
        ev = self._events.setdefault(tid, asyncio.Event())
        try:
            await asyncio.wait_for(ev.wait(), timeout)
        except asyncio.TimeoutError:
            pass
        finally:
            self._events.pop(tid, None)
        return self._recs.get(tid)


def status_event(change: dict) -> dict:
    """A rejected tell after the CLI returned: a `next` event so the agent learns it."""
    reason = str(change.get("reason") or "")
    ev = {"type": "delta_tell_status", "id": change["id"], "state": change["state"], "ts": time.time()}
    if reason:
        ev["reason"] = reason
    h = reject_hint(reason)
    if h:
        ev["hint"] = h
    return ev
