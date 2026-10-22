"""Local MCP server (0.20.0): status, decide, next and activity for coding agents without
Claude Code hooks (Codex CLI first, any MCP client works).

`voicehook-agent-hook mcp [--agent codex]` (also `voicehook-agent mcp`) speaks MCP over
stdio (JSON-RPC 2.0, one JSON object per line) and offers four tools:

* `status` {doing, open[], done[]}: this session's status board in the call (all empty
  clears it).
* `decide` {question, options[2-3]?, recommend?, wait_s?}: a question on the Decision
  Board; with wait_s > 0 it waits for the answer.
* `next` {timeout_s?}: the next event for this session: the answers to its own decide
  questions (helper), or the next call event of its own join.
* `activity` {text}: one short line in the call's activity log.

Routing is the CLI's own (relayops.client_relay): a session that runs its own `join`
(its control socket answers) talks to that join; every other session of the machine is a
helper while ~/.voicehook/relay.json is usable (written by the session that joined, or by
`helper-import`). Helper name (`from`): $VOICEHOOK_SESSION_LABEL, else the agent's
default (agents.session_label, e.g. `Codex <cwd basename>`), the same name the agent's
hooks use, so hooks and tools fill one board section.

Nothing is spoken (no `say`): the voice belongs to the agent that runs the join. Tool
output never contains the relay token. stdlib only (starts fast, Codex waits for it).
"""
from __future__ import annotations

import json
import sys

from . import __version__
from . import activity as vact

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER_NAME = "voicehook-local"
NEXT_MAX_S = 25.0
DECIDE_WAIT_MAX_S = 110.0
INSTRUCTIONS = (
    "voicehook: the human follows your work in a voice call. Keep your board fresh with "
    "`status` (what you do now, open, done) whenever the task changes, at least every 3 "
    "minutes while you work. A question only the human can decide goes to `decide` "
    "(yes/no or 2-3 options), never only into the chat; collect the answer with `next` "
    "(or decide wait_s). When you wait for the human, ALWAYS decide, never only a status "
    "(a waiting text in status/activity is refused). In a call: answer every utterance first "
    "(say), hand work longer than 2 tool calls to background agents, keep turns short. "
    "`activity` adds one short line (3-8 words) to the live log; no "
    "paths, secrets or personal data anywhere.")

_STR_LIST = {"type": "array", "items": {"type": "string"}, "maxItems": 10}
TOOLS = [
    {"name": "status",
     "description": "Set this session's status board in the voicehook call: doing = what "
                    "you do right now (one sentence), open/done = short task lists. All "
                    "empty clears the board. Never spoken.",
     "inputSchema": {"type": "object", "properties": {
         "doing": {"type": "string", "description": "current task, one sentence. Never a "
                                                    "text that waits for the human (refused): "
                                                    "When you wait for the human, ALWAYS decide, "
                                                    "never only a status"},
         "open": {**_STR_LIST, "description": "open tasks"},
         "done": {**_STR_LIST, "description": "finished tasks"},
         "waiting": {"type": "string", "description": "0.21.6: you wait for the human: the board "
                     "shows this as 'wartet auf dich'; needs question (+ context) or decision"},
         "question": {"type": "string", "maxLength": 160,
                      "description": "with waiting: the decision to ask now (plain language)"},
         "context": {"type": "string", "maxLength": 1500,
                     "description": "with waiting + question: background of the decision"},
         "decision": {"type": "string", "description": "with waiting: id of an open decision "
                                                       "(from an earlier decide)"}},
         "additionalProperties": False}},
    {"name": "decide",
     "description": "Ask the human a decision on the call's Decision Board: yes/no, or 2-3 "
                    "options. The answer comes from `next` (type decision), or right here "
                    "when wait_s > 0.",
     "inputSchema": {"type": "object", "properties": {
         "question": {"type": "string", "maxLength": 160,
                      "description": "a whole sentence that names the product or area, what "
                                     "happens and why, ending with ?; no bare PR/ticket "
                                     "numbers, digit chains or unexplained abbreviations. Bad: "
                                     "'DB quick wins 620?' Good: 'Sales Engine: vier "
                                     "Datenbank-Indizes anlegen, damit die Admin-Ansicht "
                                     "schneller lädt? (nachts, ohne Ausfall)'"},
         "context": {"type": "string", "maxLength": 1500,
                     "description": "required: 2-4 sentences for the voicebot, never shown: "
                                    "what it is about, why, what yes and what no does"},
         "options": {"type": "array", "items": {"type": "string", "maxLength": 40},
                     "minItems": 2, "maxItems": 3},
         "recommend": {"type": "integer", "minimum": 0, "maximum": 2,
                       "description": "index of the recommended option"},
         "wait_s": {"type": "number", "minimum": 0, "maximum": DECIDE_WAIT_MAX_S,
                    "description": "wait up to this many seconds for the answer (0 = no)"}},
         "required": ["question", "context"], "additionalProperties": False}},
    {"name": "next",
     "description": "The next event for this session: answers to your decide questions "
                    "(type decision), or with your own join the next call event. "
                    "{type: revise, stale: true, items, hint}: queued statements older than "
                    "60 s were dropped unspoken; never repeat them, if still relevant send ONE "
                    "short current summary. {type: timeout} when nothing came.",
     "inputSchema": {"type": "object", "properties": {
         "timeout_s": {"type": "number", "minimum": 0, "maximum": NEXT_MAX_S}},
         "additionalProperties": False}},
    {"name": "activity",
     "description": "One short line (3-8 words) in the call's live activity log: what is "
                    "happening now. No paths, secrets or personal data.",
     "inputSchema": {"type": "object", "properties": {
         "text": {"type": "string", "maxLength": 120}},
         "required": ["text"], "additionalProperties": False}},
]


class ToolError(Exception):
    pass


# --------------------------------------------------------------------------- #
# routing
# --------------------------------------------------------------------------- #
def _label(agent: str) -> str:
    from . import agents
    return agents.session_label(agent)


def _route() -> tuple[str, dict | None]:
    """('relay', relay) for a helper, ('own', None) when a join control socket answers,
    ('', None) when there is no call on this machine."""
    from . import relayops as vrelayops
    from . import session as vsession
    rel = vrelayops.client_relay(None)
    if rel is not None:
        return "relay", rel
    bad = vrelayops.helper_problem(None)  # 0.25.0: a dead helper never takes another's join
    if bad is not None:
        raise ToolError(f"{bad['error']}; {bad['hint']}")
    try:
        vsession.resolve_socket(None, wait=0.0)
        return "own", None
    except (vsession.SessionError, OSError):
        return "", None


NO_CALL = ("no voicehook call on this machine (no running join, no ~/.voicehook/relay.json); "
           "nothing was sent")


def _own(req: dict, timeout: float | None = 30.0) -> dict:
    from . import session as vsession
    try:
        sock = vsession.resolve_socket(None, wait=0.0)
        return vsession.request(sock, req, timeout=timeout)
    except (vsession.SessionError, OSError, ValueError) as e:
        raise ToolError(f"join not reachable: {type(e).__name__}") from None


def _str_list(v: object, name: str) -> list[str]:
    if v is None:
        return []
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise ToolError(f"{name} must be a list of strings")
    return [x.strip() for x in v if x.strip()][:10]


MCP_WAIT_HINT = ("MCP: call `decide` (question, context) first, then `status` with waiting + "
                 "decision (or waiting + question + context in one call).")


def tool_status(args: dict, agent: str) -> dict:
    from . import waitcheck
    doing = args.get("doing")
    waiting = args.get("waiting")
    for k in ("doing", "waiting", "question", "context", "decision"):
        if args.get(k) is not None and not isinstance(args.get(k), str):
            raise ToolError(f"{k} must be a string")
    decision = None
    if waiting is not None:  # 0.21.6: the board waits for the human, with its decision
        if not waiting.strip():
            raise ToolError("waiting needs a text")
        q, ref = (args.get("question") or "").strip(), (args.get("decision") or "").strip()
        if bool(q) == bool(ref):
            raise ToolError("waiting needs question (+ context) or decision. " + waitcheck.RULE)
        if q:
            res = tool_decide({"question": q, "context": args.get("context")}, agent)
            if not res.get("ok"):
                return {**res, "board": None, "note": "decide failed, board not set"}
            decision = {"id": res.get("id"), "number": res.get("number"), "question": q}
        else:
            r = waitcheck.decision_ref(ref)
            if r is None:
                raise ToolError("decision: id d + 8 hex or the board number")
            decision = {"id": r} if r.startswith("d") else {"number": int(r)}
        doing = waiting
    else:
        try:
            waitcheck.check(doing, "status doing")
        except waitcheck.WaitError as e:
            raise ToolError(f"{e}\n{MCP_WAIT_HINT}") from None
    board = {"doing": (doing or "").strip(), "open": _str_list(args.get("open"), "open"),
             "done": _str_list(args.get("done"), "done")}
    if decision is not None:
        board.update(state=waitcheck.STATE, decision=decision)
    rt, rel = _route()
    if rt == "own":
        out = _own({"cmd": "board", "board": board})
        return {**out, "decision": decision} if decision is not None else out
    if rt != "relay":
        raise ToolError(NO_CALL)
    from . import multisession as vmulti
    from . import relayops as vrelayops
    frm = vrelayops.check_from(_label(agent))
    body = vrelayops.status_body(board)
    res = (vrelayops.local_request(rel, frm, "status", body) if vmulti._is_local(rel)
           else vrelayops.remote_status(rel, frm, body))
    if decision is not None and res.get("ok"):
        from . import board as vboard
        vboard.post_state(rel, frm, waitcheck.STATE)
    out = {**res, "type": "board", "from": frm, "via": "relay"}
    if decision is not None:
        out["decision"] = decision
    return out


def tool_decide(args: dict, agent: str) -> dict:
    q = args.get("question")
    if not isinstance(q, str) or not q.strip():
        raise ToolError("question is required")
    ctx = args.get("context")
    if ctx is not None and not isinstance(ctx, str):
        raise ToolError("context must be a string")
    from . import plaintext as vplain
    try:  # 0.21.5 Klartext + mandatory context: nothing sent otherwise
        warnings = vplain.check_decide(q, ctx)
    except vplain.PlainError as e:
        raise ToolError(str(e)) from None
    req: dict = {"cmd": "decide", "question": q.strip()}
    if ctx and ctx.strip():
        req["context"] = ctx.strip()[:1500]
    opts = _str_list(args.get("options"), "options")
    if opts:
        if not 2 <= len(opts) <= 3:
            raise ToolError("options: give 2 or 3 (or none for yes/no)")
        req["options"] = opts
    rec = args.get("recommend")
    if rec is not None:
        if not isinstance(rec, int) or isinstance(rec, bool):
            raise ToolError("recommend must be an integer")
        req["recommend"] = rec
    try:
        wait = max(0.0, min(DECIDE_WAIT_MAX_S, float(args.get("wait_s") or 0)))
    except (TypeError, ValueError):
        raise ToolError("wait_s must be a number") from None
    rt, rel = _route()
    if rt == "own":
        out = _own(req)
        if out.get("ok") and wait > 0:
            out["hint"] = "own join: the answer comes from `next` (type decision)"
        if warnings:
            out["warnings"] = warnings
        return out
    if rt != "relay":
        raise ToolError(NO_CALL)
    from . import multisession as vmulti
    from . import relayops as vrelayops
    frm = vrelayops.check_from(_label(agent))
    body = vrelayops.decide_body(req)
    res = (vrelayops.local_request(rel, frm, "decide", body) if vmulti._is_local(rel)
           else vrelayops.remote_decide(rel, frm, body))
    out = {**res, "type": "decide", "from": frm, "via": "relay"}
    if res.get("ok") and wait > 0 and res.get("id"):
        evs = vrelayops.next_events(rel, frm, wait, match_id=str(res["id"]))
        out["decision"] = evs[0] if evs else None
        if not evs:
            out["timeout"] = True
    if warnings:
        out["warnings"] = warnings
    return out


def tool_next(args: dict, agent: str) -> dict:
    try:
        t = max(0.0, min(NEXT_MAX_S, float(args.get("timeout_s", 20) or 0)))
    except (TypeError, ValueError):
        raise ToolError("timeout_s must be a number") from None
    rt, rel = _route()
    if rt == "own":
        out = _own({"cmd": "next", "timeout": t, "lease": True}, timeout=t + 30.0)
        if out.get("seq") is not None:  # 0.21.5: the spool cursor moves once we have it
            try:
                _own({"cmd": "ack", "seq": out["seq"]}, timeout=5.0)
            except ToolError:
                pass  # no ack: the event comes once more after spool.LEASE_S, never lost
        return out
    if rt != "relay":
        raise ToolError(NO_CALL)
    from . import relayops as vrelayops
    frm = vrelayops.check_from(_label(agent))
    evs = vrelayops.next_events(rel, frm, t)
    if not evs:
        return {"ok": True, "type": "timeout", "via": "relay"}
    return {"ok": True, "type": "events", "events": [vrelayops.view(e) for e in evs], "via": "relay"}


def tool_activity(args: dict, agent: str) -> dict:
    text = args.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ToolError("text is required")
    try:
        vact.note_line(text)
    except ValueError as e:
        raise ToolError(str(e)) from None
    from . import waitcheck
    try:  # 0.21.6: waiting for the human = decide, never an activity line
        waitcheck.check(text, "activity")
    except waitcheck.WaitError as e:
        raise ToolError(f"{e}\n{MCP_WAIT_HINT}") from None
    rt, rel = _route()
    if rt == "own":
        return _own({"cmd": "activity", "text": text.strip()})
    if rt != "relay":
        raise ToolError(NO_CALL)
    from . import agents
    line = agents.relay_note(agent, text.strip(), rel)
    if not line:
        return {"ok": True, "type": "activity", "sent": False, "via": "relay",
                "note": "same line just sent, or throttled"}
    return {"ok": True, "type": "activity", "sent": True, "line": line, "via": "relay"}


HANDLERS = {"status": tool_status, "decide": tool_decide, "next": tool_next,
            "activity": tool_activity}


# --------------------------------------------------------------------------- #
# JSON-RPC over stdio
# --------------------------------------------------------------------------- #
def _result(mid, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _error(mid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def _text(obj: dict, is_error: bool = False) -> dict:
    out = {"content": [{"type": "text", "text": json.dumps(obj, ensure_ascii=False)}]}
    if is_error:
        out["isError"] = True
    return out


def call_tool(name: str, args: object, agent: str) -> dict:
    fn = HANDLERS.get(name)
    if fn is None:
        return _text({"ok": False, "error": f"unknown tool {name!r}"}, True)
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return _text({"ok": False, "error": "arguments must be an object"}, True)
    try:
        out = fn(args, agent)
    except ToolError as e:
        return _text({"ok": False, "error": str(e)}, True)
    except Exception as e:  # noqa: BLE001 - relay/network: a short type, never the token
        status = getattr(e, "status", None)
        return _text({"ok": False, "error": f"{type(e).__name__}: {e}"[:200],
                      **({"status": status} if status else {})}, True)
    return _text(out, not out.get("ok", True))


def handle(msg: object, agent: str) -> dict | None:
    """One JSON-RPC message -> the reply (None for a notification)."""
    if not isinstance(msg, dict):
        return _error(None, -32600, "invalid request")
    mid = msg.get("id")
    method = msg.get("method")
    is_note = "id" not in msg
    if not isinstance(method, str):
        return None if is_note else _error(mid, -32600, "invalid request")
    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
    if method == "initialize":
        want = params.get("protocolVersion")
        ver = want if want in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        return _result(mid, {"protocolVersion": ver, "capabilities": {"tools": {}},
                             "serverInfo": {"name": SERVER_NAME, "version": __version__},
                             "instructions": INSTRUCTIONS})
    if is_note:
        return None  # notifications/initialized, cancelled, ...
    if method == "ping":
        return _result(mid, {})
    if method == "tools/list":
        return _result(mid, {"tools": TOOLS})
    if method == "tools/call":
        return _result(mid, call_tool(str(params.get("name") or ""), params.get("arguments"),
                                      agent))
    if method in ("resources/list", "prompts/list"):
        return _result(mid, {method.split("/")[0]: []})
    return _error(mid, -32601, f"method not found: {method}")


def serve(stdin=None, stdout=None, agent: str = "") -> int:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for raw in stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except ValueError:
            reply: dict | None = _error(None, -32700, "parse error")
        else:
            if isinstance(msg, list):  # batch (2025-03-26)
                replies = [r for r in (handle(m, agent) for m in msg) if r is not None]
                if replies:
                    stdout.write(json.dumps(replies, ensure_ascii=False) + "\n")
                    stdout.flush()
                continue
            reply = handle(msg, agent)
        if reply is not None:
            stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            stdout.flush()
    return 0


def main(argv: list[str]) -> int:
    agent = ""
    rest = list(argv)
    while rest:
        a = rest.pop(0)
        if a == "--agent" and rest:
            agent = rest.pop(0)
        elif a.startswith("--agent="):
            agent = a.split("=", 1)[1]
        else:
            print(f"[error] unknown argument {a!r}", file=sys.stderr)
            return 2
    return serve(agent=agent)
