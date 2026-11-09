"""`voicehook-agent task assign <nr> <head>` / `task discard <nr>`: owner ops on the account's
Kanban (server voicehook-v4 apps/agent/kanban_routes.py, `POST /api/cli/tasks/{nr}/{op}`,
_owner_op_event: assign {assignee}, discard {} = mark as discarded, never a deletion).

The server allows owner ops ONLY for the LiveKit token of a CLI that joined WITH the owner's
agent key (`VOICEHOOK_AGENT_KEY`, the token carries the signed vh.owner_key / vh.owner_kid),
while the owner is alone with own agents in the room. The key alone answers 403 owner_only.
So the request is sent by the running join itself (control socket, cmd `task_op` with field
`op`; `task_assign` stays as the 0.35.8 alias for assign) with ITS LiveKit token: the token
never leaves the join process, is never printed, logged or written to disk. Callers throttle;
a 429 is passed on as it is. No DELETE route, ever.
"""
from __future__ import annotations

import httpx

CMD = "task_op"
CMD_ASSIGN = "task_assign"   # first form (assign only), still accepted by the join
CMDS = (CMD, CMD_ASSIGN)
OPS = ("assign", "discard")
PATH = "/api/cli/tasks/{nr}/{op}"
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


def _check_nr(nr: object) -> int:
    if not isinstance(nr, int) or isinstance(nr, bool) or nr < 1:
        raise AssignError("nr must be a card number >= 1")
    return nr


def request(nr: object, head: str) -> dict:
    """`task assign <nr> <head>` -> control request; AssignError on bad input."""
    return op_request("assign", nr, head)


def op_request(op: object, nr: object, head: object = None) -> dict:
    """`task <op> <nr> [head]` -> control request {cmd: task_op, op, nr, head?}."""
    if op not in OPS:
        raise AssignError(f"op must be one of {', '.join(OPS)}")
    req: dict = {"cmd": CMD, "op": op, "nr": _check_nr(nr)}
    if op == "assign":
        req["head"] = head_label(str(head or ""))
    return req


def from_control(req: dict) -> dict:
    """A control request (task_op, or the task_assign alias) -> checked request."""
    op = "assign" if req.get("cmd") == CMD_ASSIGN else req.get("op")
    return op_request(op, req.get("nr"), req.get("head"))


def body(req: dict) -> dict:
    """Server body: assign {assignee}, discard {} (server _owner_op_event)."""
    return {"assignee": req["head"]} if req["op"] == "assign" else {}


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


def result(nr: int, status: int, data: object, op: str = "assign") -> dict:
    """Server answer -> one line {ok, op, nr, status, error?, hint?, assignee?}."""
    data = data if isinstance(data, dict) else {}
    if status == 200 and data.get("ok") is not False:
        card = data.get("card") if isinstance(data.get("card"), dict) else {}
        out: dict = {"ok": True, "op": op, "nr": nr, "status": status}
        a = card.get("assignee")
        if op == "assign" and isinstance(a, dict) and a.get("label"):
            out["assignee"] = str(a["label"])
        return out
    error = str(data.get("error") or (f"HTTP {status}" if status else "request failed"))
    detail = str(data.get("detail") or "")
    out = {"ok": False, "op": op, "nr": nr, "status": status, "error": error[:120]}
    if detail:
        out["detail"] = detail[:200]
    hint = _hint(status, error, detail)
    if hint:
        out["hint"] = hint
    return out


def failed(nr: object, error: str, hint: str | None = None, status: int = 0,
           op: str = "assign") -> dict:
    out: dict = {"ok": False, "op": op, "nr": nr, "status": status, "error": error}
    if hint:
        out["hint"] = hint
    return out


async def post_op(api_base: str, lk_jwt: str, req: dict, headers: dict) -> dict:
    """POST the owner op with the join's LiveKit token. Only the LiveKit token: a pump or bridge
    token never gets owner ops (server _owner_actor). Errors name the exception type only."""
    op, nr = req["op"], req["nr"]
    if not api_base or not lk_jwt:
        return failed(nr, "no_livekit_join", HINT_JOIN, op=op)
    url = api_base.rstrip("/") + PATH.format(nr=nr, op=op)
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S, headers=headers) as http:
            r = await http.post(url, json=body(req),
                                headers={"authorization": f"Bearer {lk_jwt}"})
    except httpx.HTTPError as e:
        return failed(nr, f"request failed ({type(e).__name__})", op=op)
    try:
        data = r.json()
    except ValueError:
        data = {}
    return result(nr, r.status_code, data, op)


async def post(api_base: str, lk_jwt: str, nr: int, head: str, headers: dict) -> dict:
    """assign (0.35.8 form)."""
    return await post_op(api_base, lk_jwt, {"op": "assign", "nr": nr, "head": head}, headers)
