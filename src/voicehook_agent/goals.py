"""Leitsterne (goals) of the call (0.36.0, server voicehook-v4 goals.for_room).

The owner fixes up to 3 guiding stars per call (goals bound to the room first, then goals he
pinned for the account). They give every agent a direction; tying a step to one is optional:

    voicehook-agent goal list [--json]                    the guiding stars of this call
    voicehook-agent goal propose "Titel" --done-when "…"  propose one: a card on the Decision
                                                          Board, it counts only after the
                                                          owner's click (never by voice)
    voicehook-agent status … --goal N                     this board serves guiding star N
    voicehook-agent decide … --goal N                     this question serves guiding star N

Transport (server voicehook-v4 kanban_live / goals_routes):
    join -> worker   topic `operator.goal` {op: "propose", title, done_when?, req}
    worker -> join   topic `operator.goal_status` {req, ok, number?, error?, code?}
    anyone -> room   topic `vh.goals` {v} (no content): the guiding stars changed; the join reads
                     them again (GET /api/bridge/goals with its own LiveKit token) and puts a
                     `next` event {type: "goals", goals, text} when they differ
Only own agents of the owner get guiding stars; a guest's agent gets 403 and no event. Without
`--goal` status and decide only print a soft hint to stderr (never an error, never a decide
duty: Oliver 10.11., "micromanagement ist ein hänger"). stdlib + httpx only.
"""
from __future__ import annotations

import asyncio
import secrets
from typing import Any

PING_TOPIC = "vh.goals"
OP_TOPIC = "operator.goal"
STATUS_TOPIC = "operator.goal_status"
PATH = "/api/bridge/goals"
CMD_LIST = "goal_list"
CMD_PROPOSE = "goal_propose"
CMDS = (CMD_LIST, CMD_PROPOSE)
TITLE_MAX = 80        # server goals.TITLE_MAX
DONE_MAX = 200        # server goals.DONE_MAX
LEIT_MAX = 3          # server goals.LEIT_MAX
HTTP_TIMEOUT_S = 3.0
ACK_WAIT_S = 10.0

HINT_JOIN = "kein laufender Join: voicehook-agent join <link>"
HINT_OLD_JOIN = "der laufende Join kennt goal nicht: voicehook-agent rejoin"
HINT_NOT_OWNER = ("nur eigene Agenten des Inhabers sehen und schlagen Leitsterne vor: mit "
                  "VOICEHOOK_AGENT_KEY beitreten")
HINT_NO_DELTA = "Delta ist gerade nicht im Call, gleich noch einmal"
HINT_NO_ANSWER = "keine Antwort vom Voicebot, gleich noch einmal (oder der Server ist älter)"
STATUS_QUESTION = "Welchem Leitstern dient dein Schritt? (--goal N, wenn es passt)"


class GoalError(ValueError):
    pass


def _clean(v: object) -> str:
    return " ".join(str(v or "").split())


def check_ref(v: object) -> int:
    """`--goal N`: guiding star number >= 1, or 0 = explicitly none."""
    try:
        n = int(str(v).strip())
    except (TypeError, ValueError):
        raise GoalError("--goal braucht eine Leitstern-Nummer (0 = keiner)") from None
    if n < 0 or n > 99_999:
        raise GoalError("--goal braucht eine Leitstern-Nummer (0 = keiner)")
    return n


def propose_request(title: object, done_when: object = None) -> dict:
    """`goal propose` -> control request; GoalError on bad input (server limits)."""
    t = _clean(title)
    if not t:
        raise GoalError("Titel ist leer")
    if len(t) > TITLE_MAX:
        raise GoalError(f"Titel max {TITLE_MAX} Zeichen")
    out: dict = {"cmd": CMD_PROPOSE, "title": t}
    d = _clean(done_when)
    if d:
        if len(d) > DONE_MAX:
            raise GoalError(f"--done-when max {DONE_MAX} Zeichen")
        out["done_when"] = d
    return out


def payload(req: dict, rid: str) -> dict:
    out = {"op": "propose", "title": req["title"], "req": rid}
    if req.get("done_when"):
        out["done_when"] = req["done_when"]
    return out


def new_req() -> str:
    return "g-" + secrets.token_hex(4)


def norm(rows: object) -> list[dict]:
    """Server rows -> [{nr, title, done_when, source}] (at most LEIT_MAX, bad rows dropped)."""
    out = []
    for g in rows if isinstance(rows, list) else []:
        if not isinstance(g, dict) or not isinstance(g.get("nr"), int) or not _clean(g.get("title")):
            continue
        out.append({"nr": g["nr"], "title": _clean(g["title"])[:TITLE_MAX],
                    "done_when": _clean(g.get("done_when"))[:DONE_MAX],
                    **({"source": g["source"]} if isinstance(g.get("source"), str) else {})})
    return out[:LEIT_MAX]


def line(rows: list[dict]) -> str:
    """One line for the `_meta` hint and the plain output: 'Leitsterne: #2 X (fertig, wenn Y); #5 Z'."""
    if not rows:
        return ""
    parts = []
    for g in rows:
        t = g["title"].rstrip(" .")
        parts.append(f"#{g['nr']} {t}" + (f" (fertig, wenn {g['done_when'].rstrip(' .')})" if g.get("done_when") else ""))
    return "Leitsterne: " + "; ".join(parts)


def event(rows: list[dict]) -> dict:
    """`next` event after a change (like the server's bridge event)."""
    text = (line(rows) + ". Ordne deinen Schritt einem Leitstern zu, wenn es passt (--goal N); nie Pflicht."
            if rows else "Keine Leitsterne mehr für diesen Call.")
    return {"type": "goals", "role": "system", "goals": rows, "text": text}


def missing_hint(rows: list[dict]) -> str | None:
    """Soft hint when status/decide comes without --goal while the call has guiding stars."""
    if not rows:
        return None
    nums = ", ".join(f"#{g['nr']}" for g in rows)
    return f"[hint] Ohne --goal: dient der Schritt einem Leitstern ({nums})? Dann --goal N. Nur ein Hinweis."


def status_hint(hint: str, rows: list[dict]) -> str:
    """status_due hint plus the question which guiding star the step serves (only with stars)."""
    return f"{hint} {STATUS_QUESTION}" if rows and STATUS_QUESTION not in hint else hint


async def fetch(api_base: str, token: str, *, client: Any = None) -> list[dict] | None:
    """GET /api/bridge/goals with the join's own LiveKit token. None = not available (old server,
    guest agent 403, network); never raises. The token is never printed or logged."""
    if not api_base or not token:
        return None
    try:
        import httpx

        own = client is None
        c = client or httpx.AsyncClient(timeout=HTTP_TIMEOUT_S)
        try:
            r = await c.get(api_base.rstrip("/") + PATH, headers={"Authorization": f"Bearer {token}"})
        finally:
            if own:
                await c.aclose()
        if r.status_code != 200:
            return None
        body = r.json()
        return norm(body.get("goals")) if isinstance(body, dict) else None
    except Exception:  # noqa: BLE001  network, old server, bad JSON: no guiding stars
        return None


class Acks:
    """Answers of the worker on operator.goal_status, by request id."""

    def __init__(self) -> None:
        self._w: dict[str, asyncio.Future] = {}

    def expect(self, rid: str) -> asyncio.Future:
        fut = asyncio.get_running_loop().create_future()
        self._w[rid] = fut
        return fut

    def note(self, payload: object) -> bool:
        if not isinstance(payload, dict):
            return False
        fut = self._w.pop(str(payload.get("req") or ""), None)
        if fut is None or fut.done():
            return False
        fut.set_result(dict(payload))
        return True

    def drop(self, rid: str) -> None:
        self._w.pop(rid, None)


def answer(ack: dict | None) -> dict:
    """Worker ack -> one JSON line for `goal propose`."""
    if ack is None:
        return {"ok": False, "type": "goal", "error": "timeout", "hint": HINT_NO_ANSWER}
    if ack.get("ok"):
        return {"ok": True, "type": "goal", "number": ack.get("number"),
                "note": "steht auf dem Decision Board; gilt erst mit dem Klick des Inhabers"}
    err = str(ack.get("error") or "failed")
    out = {"ok": False, "type": "goal", "error": err}
    if err == "not_owner_agent":
        out["hint"] = HINT_NOT_OWNER
    return out


def failed(error: str, hint: str | None = None) -> dict:
    out: dict = {"ok": False, "type": "goal", "error": error}
    if hint:
        out["hint"] = hint
    return out
