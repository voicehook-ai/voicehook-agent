"""The session's own questions on the call's Decision Board, answered there (0.21.3).

Claude Code marks its questions to the human: an AskUserQuestion dialog, or a reply that
ends with a question ("Soll ich ...?", an A/B list). While a call runs (a usable
~/.voicehook/relay.json, this session a helper, see notify.route), they now show up in the
call's decisions exactly as the session asked them, with their options:

* AskUserQuestion (notify.py, PreToolUse): every question its own decision, option labels
  as options (2-3; a 4th is named in the question), descriptions as hidden context.
  `voicehook-agent-hook ask-wait` (an extra PreToolUse hook, matcher AskUserQuestion,
  timeout ASK_HOOK_TIMEOUT_S) waits up to ASK_WAIT_S for the board answers of ALL
  questions and then answers the tool itself: `permissionDecision: allow` + `updatedInput`
  with `answers` {question: chosen label or free text}. No complete answer in time: the
  dialog shows in the session as before; the board decisions stay until it is answered.
* A reply that ends with an open question (stop_question: last paragraph ends with "?",
  starts with "Soll ich"/"Willst du"/"Shall I"/..., or lists options A)/B)/1./2.):
  `voicehook-agent-hook stop-wait` (an extra Stop hook, `async` + `asyncRewake`) posts it
  once per question (dedupe hash) as yes/no or with the detected options, then waits up to
  STOP_WAIT_S for the answer; the answer wakes the idle session (exit 2 -> stderr arrives
  as a system reminder: `voicehook call: answer from ... : <option>`). The session goes on
  by itself (a prompt in the terminal, a tool): the decision leaves the board (notify.py)
  and the waiter ends quietly. Codex (agents.py): the same question is posted on Stop; the
  answer arrives with the session's next hook event (board.py), Codex has no rewake.

Never: a question or option with a secret in it (vact.scrub found one), paths (basename
only), URLs (host only); the joining session itself (it speaks), subagents, a Stop that
already continues a Stop hook. VOICEHOOK_AUTO_ASK=0 turns both off, VOICEHOOK_ASK_WAIT_S
sets the AskUserQuestion wait (0 = do not wait, only post). stdlib only.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time

from . import activity as vact
from . import notify as vnotify

ENV_OFF = "VOICEHOOK_AUTO_ASK"
ASK_WAIT_ENV = "VOICEHOOK_ASK_WAIT_S"
ASK_WAIT_S = 90.0            # AskUserQuestion: wait this long for the board answers
ASK_HOOK_TIMEOUT_S = 100     # settings timeout of the ask-wait hook (> ASK_WAIT_S)
ID_WAIT_S = 15.0             # the parallel pre-tool-use child creates the decisions
STOP_WAIT_S = 1780.0         # Stop question: wait this long for the answer
STOP_HOOK_TIMEOUT_S = 1800   # settings timeout of the async stop-wait hook
POLL_CHUNK_S = 25.0          # one relay long poll
WAKE_EXIT = 3                # stop-wait -> the settings command maps it to exit 2 (rewake)
SEEN_KEEP = 30
TEXT_MAX = 20_000            # look at most at the last 20k chars of a reply

_FENCE = re.compile(r"```.*?(?:```|$)", re.DOTALL)
_OPT_LINE = re.compile(r"^\s*(?:[-*•]\s*)?(?:\*\*)?\(?([A-Da-d]|[1-4])(?:[).:]|\s[-–—])\)?"
                       r"(?:\*\*)?\s+(\S.*?)\s*$")
_INLINE_OPT = re.compile(r"(?:^|[\s(/,;])\(?([A-D])[):]\s+(.+?)(?=\s*(?:[/,;]\s*|\s)\(?[A-D][):]\s|[?.]?\s*$)")
_ASK_START = re.compile(
    r"(?i)^(?:soll(?:en)? (?:ich|wir)|willst du|wollen wir|möchtest du|magst du|darf ich|"
    r"passt das|ok so|okay so|einverstanden|shall i|should i|do you want|want me to|"
    r"would you like|can i|may i|ok to|okay to|sound good|go ahead)\b")
_MD = re.compile(r"(\*\*|__|`|^#+\s*|^>\s*)", re.MULTILINE)


def _off() -> bool:
    return (os.environ.get(ENV_OFF) or "").strip().lower() in ("0", "false", "no", "off")


def ask_wait_s() -> float:
    try:
        return max(0.0, min(float(os.environ.get(ASK_WAIT_ENV) or ASK_WAIT_S),
                            ASK_HOOK_TIMEOUT_S - 5.0))
    except ValueError:
        return ASK_WAIT_S


# --------------------------------------------------------------------------- #
# a reply that ends with a question
# --------------------------------------------------------------------------- #
def _plain(text: str) -> str:
    return " ".join(_MD.sub("", text).split())


def _options_of(lines: list[str]) -> tuple[list[str], list[str]]:
    """(option texts, the other lines) of a block whose lines list options A)/B)/1./2."""
    opts: list[tuple[str, str]] = []
    rest: list[str] = []
    for ln in lines:
        m = _OPT_LINE.match(ln)
        if m:
            opts.append((m.group(1).upper(), _plain(m.group(2))))
        else:
            rest.append(ln)
    keys = [k for k, _ in opts]
    seq_a, seq_1 = list("ABCD")[:len(keys)], [str(i) for i in range(1, len(keys) + 1)]
    if len(opts) < 2 or keys not in (seq_a, seq_1):
        return [], lines
    return [v for _, v in opts], rest


def _inline_options(line: str) -> tuple[list[str], str]:
    """`Welche? A) x / B) y` -> (["x", "y"], "Welche?")."""
    ms = list(_INLINE_OPT.finditer(line))
    keys = [m.group(1) for m in ms]
    if len(ms) < 2 or keys != list("ABCD")[:len(keys)]:
        return [], line
    head = line[:ms[0].start()].strip(" :-–")
    return [m.group(2).strip(" ,;/") for m in ms], head


def _last_sentence(par: str) -> str:
    parts = re.split(r"(?<=[.!?])\s+", par.strip())
    return parts[-1].strip() if parts else ""


def stop_question(text: object) -> dict | None:
    """The open question a reply ends with, or None:
    {question, options (2-3 or []), labels, raw, context}. Language as written."""
    if not isinstance(text, str) or not text.strip():
        return None
    body = _FENCE.sub("", text[-TEXT_MAX:]).strip()
    pars = [p for p in re.split(r"\n\s*\n", body) if p.strip()]
    if not pars:
        return None
    last = [ln for ln in pars[-1].splitlines() if ln.strip()]
    opts, rest = _options_of(last)
    if opts:
        head = [_plain(x) for x in rest if _plain(x)]
        if not head and len(pars) > 1:
            head = [_plain(x) for x in pars[-2].splitlines() if _plain(x)]
        question = _last_sentence(head[-1]) if head else ""
        if not question:
            return None
        ctx_src = " ".join(head[:-1])
    else:
        line = _plain(last[-1])
        opts, head_line = _inline_options(line)
        if opts:
            question = _last_sentence(head_line) or head_line
        else:
            sent = _last_sentence(" ".join(_plain(x) for x in last))
            if not (sent.rstrip(" )").endswith("?") or _ASK_START.match(sent)):
                return None
            question = sent
        ctx_src = " ".join(_plain(x) for x in last[:-1])
    if not question or len(question) < 4:
        return None
    item = vnotify.ask_items({"questions": [{"question": question,
                                             "options": [{"label": o} for o in opts]}]})
    if not item:
        return None          # a secret in the question
    it = item[0]
    it.pop("raw", None)       # only AskUserQuestion needs the original text (answers key)
    prev = _plain(pars[-2]) if len(pars) > 1 and not ctx_src else ctx_src
    if prev and not vnotify._redacted(prev):
        it["context"] = vnotify._cut("; ".join(x for x in (it.get("context"), vnotify.clean_text(prev, 600)) if x),
                                     vnotify.CONTEXT_MAX)
    return it


def last_message(event: dict) -> str:
    """Stop: `last_assistant_message`, else the last assistant text of the transcript."""
    msg = event.get("last_assistant_message")
    if isinstance(msg, str):
        return msg
    path = event.get("transcript_path")
    if not isinstance(path, str) or not path:
        return ""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 262_144))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return ""
    for ln in reversed(lines):
        try:
            o = json.loads(ln)
        except ValueError:
            continue
        m = o.get("message") if isinstance(o, dict) and o.get("type") == "assistant" else None
        content = m.get("content") if isinstance(m, dict) else None
        if isinstance(content, list):
            texts = [c.get("text") for c in content if isinstance(c, dict) and c.get("type") == "text"
                     and isinstance(c.get("text"), str)]
            if texts:
                return "\n\n".join(texts)
    return ""


# --------------------------------------------------------------------------- #
# dedupe: one Stop question once per session
# --------------------------------------------------------------------------- #
def _hash(item: dict) -> str:
    return hashlib.sha256(("vh-stop-ask|" + item["question"] + "|" + "|".join(item["options"]))
                          .encode()).hexdigest()[:16]


def _first_time(skey: str, h: str, t: float) -> bool:
    def step(st: dict):
        me = st["sessions"].get(skey) if isinstance(st["sessions"].get(skey), dict) else {}
        seen = [x for x in (me.get("stop_seen") or []) if isinstance(x, str)]
        if h in seen:
            return False
        me["stop_seen"] = (seen + [h])[-SEEN_KEEP:]
        me["seen"] = t
        st["sessions"][skey] = me
        return True
    return bool(vnotify._with_state(step))


# --------------------------------------------------------------------------- #
# answers
# --------------------------------------------------------------------------- #
def tool_answer(item: dict, ev: dict) -> str:
    """The answer of one decision event for AskUserQuestion's `answers`: the chosen
    option's original label (+ free text), else the free text, else Yes/No."""
    text = vact.scrub(str(ev.get("text") or "")).strip()[:300]
    opt = ev.get("option")
    labels = item.get("labels") or []
    if ev.get("verdict") == "option" and isinstance(opt, int) and not isinstance(opt, bool) \
            and 0 <= opt < len(labels):
        return f"{labels[opt]} ({text})" if text else labels[opt]
    if text:
        return text
    return {"yes": "Yes", "no": "No"}.get(str(ev.get("verdict") or ""), "")


def _wait_answers(rel: dict, frm: str, ids: list[str], deadline: float, skey: str, qk: str,
                  next_events=None, now=time.monotonic) -> dict | None:
    """{id: event} once every id has an answer; None on timeout or when the session went on
    (its slot was resolved)."""
    from . import relayops
    nxt = next_events or relayops.next_events
    got: dict = {}
    while True:
        left = deadline - now()
        if left <= 0:
            return None
        try:
            evs = nxt(rel, frm, min(POLL_CHUNK_S, left), match_ids=set(ids) - set(got))
        except Exception:  # noqa: BLE001 - relay gone: give up quietly
            return None
        for e in evs:
            if isinstance(e, dict) and e.get("id") in ids and e.get("id") not in got:
                got[e["id"]] = e
        if len(got) == len(ids):
            return got
        slot = vnotify.open_slot(skey)
        if slot is None or slot.get("q") != qk:
            return None


def _helper(event: dict) -> tuple[str, dict, str] | None:
    """(skey, relay, from) when this session is a helper of a running call, else None."""
    from . import multisession as vmulti
    if _off() or event.get("agent_id"):
        return None
    skey = vnotify._skey(event)
    rt, _dir, rel = vnotify.route(skey)
    if rt != "relay" or rel is None:
        return None
    return skey, rel, vmulti.session_label(event, vnotify._cached_label(skey))


def _ids_of(skey: str, qk: str, n: int, wait_s: float, sleep=time.sleep) -> list[str]:
    """The ids the parallel pre-tool-use child stored for this ask (it may still be asking)."""
    end = time.monotonic() + wait_s
    while True:
        slot = vnotify.open_slot(skey)
        if slot is None or slot.get("q") != qk:
            return []
        ids = [i for i in (slot.get("ids") or ([slot["id"]] if slot.get("id") else []))
               if isinstance(i, str)]
        if len(ids) >= n:
            return ids
        if time.monotonic() >= end:
            return []
        sleep(0.3)


def ask_wait(raw: str, now: float | None = None, next_events=None, clock=time.monotonic,
             sleep=time.sleep, create=None) -> dict | None:
    """`voicehook-agent-hook ask-wait` (PreToolUse, AskUserQuestion): the hook output that
    answers the tool from the board, or None (the dialog shows). Never raises."""
    try:
        event = json.loads(raw) if raw and raw.strip() else {}
        if not isinstance(event, dict) or event.get("tool_name") != vnotify.ASK_TOOL:
            return None
        wait = ask_wait_s()
        who = _helper(event) if wait > 0 else None
        if who is None:
            return None
        skey, rel, frm = who
        info = vnotify.classify(event)
        if not info or info.get("tool") != vnotify.ASK_TOOL:
            return None
        items = info.get("items") or []
        t = time.time() if now is None else now
        qk = vnotify.group_key(info)
        start = clock()
        adm = vnotify.admit_ask(skey, info, "relay", t, {"from": frm})
        if adm is not None:
            ids = (create or vnotify.create)(vnotify.ask_job("relay", skey, info, adm, {"from": frm}))
        else:
            ids = _ids_of(skey, qk, len(items), ID_WAIT_S, sleep=sleep)
        if not ids or len(ids) != len(items):
            return None
        got = _wait_answers(rel, frm, ids, start + wait, skey, qk, next_events, clock)
        if got is None:
            return None
        answers = {}
        for item, dec_id in zip(items, ids):
            a = tool_answer(item, got[dec_id])
            if not a:
                return None
            answers[item["raw"]] = a
        vnotify.forget(skey, qk)
        from . import board
        board.mark_delivered(skey, list(got.values()))
        inp = dict(event.get("tool_input") or {})
        inp["answers"] = answers
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow",
                                       "permissionDecisionReason": "Answered in the voicehook call",
                                       "updatedInput": inp}}
    except Exception:  # noqa: BLE001 - a hook must never disturb Claude Code
        return None


def stop_ask(raw: str, wait: bool = True, now: float | None = None, post=None,
             next_events=None, clock=time.monotonic, create=None) -> tuple[int, str]:
    """Stop: post the question the reply ends with (once), and with `wait` wait for its
    answer: (WAKE_EXIT, answer text) wakes the session, else (0, ""). Never raises."""
    try:
        event = json.loads(raw) if raw and raw.strip() else {}
        if not isinstance(event, dict) or event.get("stop_hook_active") is True:
            return 0, ""
        who = _helper(event)
        if who is None:
            return 0, ""
        skey, rel, frm = who
        item = stop_question(last_message(event))
        if item is None:
            return 0, ""
        t = time.time() if now is None else now
        if not _first_time(skey, _hash(item), t):
            return 0, ""
        info = {"kind": "ask", "question": item["question"], "options": item["options"],
                "items": [item], "tool": "", "prio": vnotify.PRIO_SPECIFIC}
        extra = {"from": frm, "kind": "stop"}
        start = clock()
        adm = vnotify.admit_ask(skey, info, "relay", t, extra)
        if adm is None:
            return 0, ""
        from . import board
        board.note_ask(skey, rel, frm, t)
        job = vnotify.ask_job("relay", skey, info, adm, extra)
        if not wait:
            (post or vnotify.spawn)(job)
            return 0, ""
        ids = (create or vnotify.create)(job)
        if not ids:
            return 0, ""
        qk = vnotify.group_key(info)
        got = _wait_answers(rel, frm, ids, start + STOP_WAIT_S, skey, qk, next_events, clock)
        if not got:
            return 0, ""
        vnotify.forget(skey, qk)
        evs = list(got.values())
        board.mark_delivered(skey, evs)
        return WAKE_EXIT, " ".join(board.answer_text(dict(e, text=tool_answer(item, e)))
                                   if e.get("verdict") == "option" else board.answer_text(e)
                                   for e in evs)
    except Exception:  # noqa: BLE001
        return 0, ""


def main(cmd: str) -> int:
    """`voicehook-agent-hook ask-wait|stop-wait` (stdin: the hook event)."""
    try:
        raw = sys.stdin.read()
    except Exception:  # noqa: BLE001
        raw = ""
    if cmd == "ask-wait":
        out = ask_wait(raw)
        if out:
            print(json.dumps(out, ensure_ascii=False), flush=True)
        return 0
    rc, text = stop_ask(raw)
    if rc and text:
        print(text, file=sys.stderr, flush=True)
    return rc
