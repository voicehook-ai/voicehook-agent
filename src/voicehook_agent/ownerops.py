"""`voicehook-agent task assign <nr> <head>`: an owner op on the account's Kanban (server
voicehook-v4 apps/agent/kanban_routes.py, `POST /api/cli/tasks/{nr}/assign {assignee}`).

The server allows owner ops ONLY for the LiveKit token of a CLI that joined WITH the owner's
agent key (`VOICEHOOK_AGENT_KEY`, the token carries the signed vh.owner_key / vh.owner_kid),
while the owner is alone with own agents in the room. The key alone answers 403 owner_only.
So the request is sent by the running join itself (control socket, cmd `task_assign`) with
ITS LiveKit token: the token never leaves the join process, is never printed, logged or
written to disk. Callers throttle; a 429 is passed on as it is.
"""
from __future__ import annotations

import httpx

CMD = "task_assign"
PATH = "/api/cli/tasks/{nr}/assign"
HTTP_TIMEOUT_S = 15.0
HEAD_MAX = 80

HINT_JOIN = ("kein eigener Join mit Schlüssel: mit VOICEHOOK_AGENT_KEY beitreten "
             "(VOICEHOOK_AGENT_KEY=vhak_... voicehook-agent join <link>)")
HINT_OWNER_ONLY = ("Join ohne Schlüssel oder Inhaber nicht allein mit eigenen Agenten: mit "
                   "VOICEHOOK_AGENT_KEY neu beitreten, nur Inhaber und eigene Agenten im Raum")
HINT_GUEST = "jemand anderes ist im Call (Gast oder fremder Agent): Inhaber-Aktionen gesperrt"
HINT_EXPIRED = "Join-Token abgelaufen: voicehook-agent rejoin"
HINT_RATE = "zu viele Anfragen, später noch einmal"


class AssignError(ValueError):
    pass


def head_label(head: str) -> str:
    """The chip label of the new head (server: _assignee_arg, a string = an agent label)."""
    label = " ".join(str(head or "").split())
    if not label:
        raise AssignError("head is empty")
    if len(label) > HEAD_MAX:
        raise AssignError(f"head max {HEAD_MAX} characters")
    return label


def request(nr: object, head: str) -> dict:
    """`task assign <nr> <head>` -> control request; AssignError on bad input."""
    if not isinstance(nr, int) or isinstance(nr, bool) or nr < 1:
        raise AssignError("nr must be a card number >= 1")
    return {"cmd": CMD, "nr": nr, "head": head_label(head)}


def _hint(status: int, error: str, detail: str) -> str | None:
    if status == 403 and error in ("owner_only", "not_owner_agent"):
        return HINT_OWNER_ONLY
    if status == 403 and error == "guest_present":
        return HINT_GUEST
    if status in (401, 404) and "expired" in detail.lower():
        return HINT_EXPIRED
    if status == 410:
        return "Call beendet"
    if status == 429:
        return HINT_RATE
    return None


def result(nr: int, status: int, data: object) -> dict:
    """Server answer -> one line {ok, op, nr, status, error?, hint?, assignee?}."""
    data = data if isinstance(data, dict) else {}
    if status == 200 and data.get("ok") is not False:
        card = data.get("card") if isinstance(data.get("card"), dict) else {}
        out: dict = {"ok": True, "op": "assign", "nr": nr, "status": status}
        a = card.get("assignee")
        if isinstance(a, dict) and a.get("label"):
            out["assignee"] = str(a["label"])
        return out
    error = str(data.get("error") or (f"HTTP {status}" if status else "request failed"))
    detail = str(data.get("detail") or "")
    out = {"ok": False, "op": "assign", "nr": nr, "status": status, "error": error[:120]}
    if detail:
        out["detail"] = detail[:200]
    hint = _hint(status, error, detail)
    if hint:
        out["hint"] = hint
    return out


def failed(nr: object, error: str, hint: str | None = None, status: int = 0) -> dict:
    out: dict = {"ok": False, "op": "assign", "nr": nr, "status": status, "error": error}
    if hint:
        out["hint"] = hint
    return out


async def post(api_base: str, lk_jwt: str, nr: int, head: str, headers: dict) -> dict:
    """POST the assign with the join's LiveKit token. Only the LiveKit token: a pump or bridge
    token never gets owner ops (server _owner_actor). Errors name the exception type only."""
    if not api_base or not lk_jwt:
        return failed(nr, "no_livekit_join", HINT_JOIN)
    url = api_base.rstrip("/") + PATH.format(nr=nr)
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S, headers=headers) as http:
            r = await http.post(url, json={"assignee": head},
                                headers={"authorization": f"Bearer {lk_jwt}"})
    except httpx.HTTPError as e:
        return failed(nr, f"request failed ({type(e).__name__})")
    try:
        data = r.json()
    except ValueError:
        data = {}
    return result(nr, r.status_code, data)
