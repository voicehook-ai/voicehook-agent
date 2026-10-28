"""Kanban maintenance of the watcher (0.33.0, `Watcher.duty_kanban` in watch.py).

Per observable Claude Code session and tick: new user orders from the transcript become
cards (or sub-points) on the owner's Kanban, the card the session works on is set to
`in_arbeit`, decisions the session created are linked to it. Server routes (activity relay,
same bearer token): GET /tasks, POST /tasks, POST /tasks/{nr}/points|link|pick.

Privacy: order text goes only to the local model command and to the owner's own Kanban
(wording). Logs and errors carry labels, card numbers and counts, never text.

stdlib only; no import of watch.py (the watcher passes the model in).
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from . import activity as vact
from . import relayops as vrelayops

CURSOR_NAME = "watch-kanban.json"
MAX_ORDERS_PER_TICK = 5
WORDING_MAX = 1500          # server limit of a wording (422 above)
TITLE_MAX = 80
READ_MAX = 4 << 20          # bytes of a transcript read per tick
BACKFILL_S = 43200.0        # first sight of a session: only orders of the last 12 h
CURSOR_KEEP = 40
OPEN_CARDS_MAX = 25
POINTS_SHOWN = 5
DONE_STATES = ("done", "erledigt", "fertig", "abgeschlossen")
LINK_TRIES = 3

_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)
_SKIP_PREFIX = ("<command-", "<local-command", "<user-prompt-submit-hook", "<task-notification",
                "<bash-", "<ide_", "Caveat:", "[Request interrupted", "<system-reminder")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


# --------------------------------------------------------------------------- #
# server calls (one place, faked in tests)
# --------------------------------------------------------------------------- #
def call(rel: dict, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    """(status, json) of <relay>/<path>. RelayError on a network failure."""
    return vrelayops._http(rel, method, vrelayops.endpoint(rel, path), body, 15.0)


def get_cards(rel: dict, label: str) -> tuple[int, list[dict]]:
    st, out = call(rel, "GET", f"tasks?scope=all&from={quote(label, safe='')}")
    cards = out.get("cards") if isinstance(out.get("cards"), list) else []
    return st, [c for c in cards if isinstance(c, dict) and isinstance(c.get("nr"), int)]


def card_nr(out: dict) -> int | None:
    for src in (out, out.get("card"), out.get("task")):
        if isinstance(src, dict) and isinstance(src.get("nr"), int) and not isinstance(src["nr"], bool):
            return src["nr"]
    return None


def is_open(card: dict) -> bool:
    return str(card.get("state") or "").lower() not in DONE_STATES


# --------------------------------------------------------------------------- #
# cursor store (0600): per session key {tp, off, card, linked, tries, at}
# --------------------------------------------------------------------------- #
def _path() -> Path:
    return vact.state_root() / CURSOR_NAME


def load_all() -> dict:
    try:
        d = json.loads(_path().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def load_cursor(key: str) -> dict | None:
    e = load_all().get(key)
    return e if isinstance(e, dict) else None


def save_cursor(key: str, cur: dict, now: float) -> None:
    try:
        d = load_all()
        d[key] = {**cur, "at": now}
        keep = sorted(((k, v) for k, v in d.items() if isinstance(v, dict)),
                      key=lambda kv: float(kv[1].get("at") or 0))[-CURSOR_KEEP:]
        vact._private_dir(vact.state_root())
        vact._write_private(_path(), json.dumps(dict(keep)))
    except Exception:  # noqa: BLE001, S110
        pass


# --------------------------------------------------------------------------- #
# transcript -> orders
# --------------------------------------------------------------------------- #
def order_text(d: object) -> str:
    """The order text of one Claude Code transcript record, '' when it is not a user order
    (assistant, tool result, meta / sidechain message, system or hook output)."""
    if not isinstance(d, dict) or d.get("type") != "user" or d.get("isMeta") or d.get("isSidechain"):
        return ""
    msg = d.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, str):
        parts = [content]
    elif isinstance(content, list):
        if any(isinstance(c, dict) and c.get("type") == "tool_result" for c in content):
            return ""
        parts = [c.get("text") for c in content
                 if isinstance(c, dict) and c.get("type") == "text" and isinstance(c.get("text"), str)]
    else:
        return ""
    kept = []
    for p in parts:
        p = _REMINDER.sub("", p).strip()
        if p and not p.startswith(_SKIP_PREFIX):
            kept.append(p)
    text = "\n".join(kept).strip()
    return text if len(text.split()) >= 2 else ""   # "ja" / "ok" is no order


def _ts(d: dict) -> float | None:
    v = d.get("timestamp")
    if not isinstance(v, str):
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def read_orders(tp: str, off: int, since: float, first: bool,
                limit: int = MAX_ORDERS_PER_TICK) -> tuple[list[dict], int, int]:
    """([{text, end}], new offset, pending) from byte `off`. At most `limit` orders; the
    cursor stops after the last one returned, the rest counts as pending (next tick).
    first=True (no cursor yet): start at the last READ_MAX bytes. Orders older than `since`
    (first sight minus BACKFILL_S) are ignored. Only complete lines are consumed."""
    try:
        size = os.stat(tp).st_size
        with open(tp, "rb") as f:
            start = max(0, size - READ_MAX) if first else off
            if start > size:
                return [], size, 0
            f.seek(start)
            data = f.read(READ_MAX)
    except OSError:
        return [], off, 0
    pos = 0
    if first and start > 0:   # skip the cut line
        nl = data.find(b"\n")
        pos = len(data) if nl < 0 else nl + 1
    orders: list[dict] = []
    new_off = start + pos
    pending = 0
    while pos < len(data):
        nl = data.find(b"\n", pos)
        if nl < 0:
            break                        # incomplete last line
        end = start + nl + 1
        line = data[pos:nl]
        pos = nl + 1
        text = ""
        if b'"user"' in line:
            try:
                d = json.loads(line)
            except ValueError:
                d = None
            text = order_text(d)
            if text:
                t = _ts(d)
                if t is not None and t < since:
                    text = ""
        if text:
            if len(orders) >= limit:
                pending += 1
                continue
            orders.append({"text": text, "end": end})
        if pending == 0:
            new_off = end
    return orders, new_off, pending


# --------------------------------------------------------------------------- #
# wording, title, classification
# --------------------------------------------------------------------------- #
def fit_wording(text: str, limit: int = WORDING_MAX) -> tuple[str, bool]:
    """(wording, cut). Whole text when it fits, else the first complete sentences up to the
    limit (or a word boundary when even the first sentence is too long)."""
    text = vact.scrub(text).strip()
    if len(text) <= limit:
        return text, False
    out = ""
    for sent in _SENTENCE_END.split(text):
        cand = f"{out} {sent}".strip() if out else sent
        if len(cand) > limit:
            break
        out = cand
    if not out:
        out = text[:limit].rsplit(" ", 1)[0] or text[:limit]
    return out, True


def _clean_title(s: str) -> str:
    s = " ".join(vact.scrub(str(s or "")).split()).strip("\"'`„“” ")
    if len(s) > TITLE_MAX:
        s = s[:TITLE_MAX - 1].rsplit(" ", 1)[0].rstrip(",;:.") + "…"
    return s


def fallback_title(text: str) -> str:
    """No model: the first sentence of the order, max TITLE_MAX chars."""
    t = re.sub(r"^\[Router\]\s*", "", text.strip())
    first = _SENTENCE_END.split(" ".join(t.split()), 1)[0]
    return _clean_title(first) or "Auftrag"


CLASSIFY_SYSTEM = ("Du ordnest einen Auftrag in ein Kanban ein. Antworte NUR mit einem JSON-Objekt "
                   '{"action":"new"|"sub","nr":Zahl oder null,"title":"kurzer deutscher Titel, '
                   'hoechstens 80 Zeichen"}. action sub nur, wenn der Auftrag klar zu einer der '
                   "offenen Karten gehoert (dann deren nr), sonst new.")


def _points_titles(card: dict) -> list[str]:
    pts = card.get("points")
    out = []
    if isinstance(pts, list):
        for p in pts[:POINTS_SHOWN]:
            t = p if isinstance(p, str) else (p.get("text") or p.get("title") or "") if isinstance(p, dict) else ""
            if t:
                out.append(str(t)[:60])
    return out


def open_cards(cards: list[dict]) -> list[dict]:
    return [c for c in cards if is_open(c)][-OPEN_CARDS_MAX:]


def build_classify_prompt(order: str, cards: list[dict]) -> str:
    lines = []
    for c in open_cards(cards):
        pts = _points_titles(c)
        lines.append(f"{c['nr']}: {str(c.get('title') or '')[:80]}" + (f" [Punkte: {'; '.join(pts)}]" if pts else ""))
    return (f"{CLASSIFY_SYSTEM}\n\nOffene Karten:\n{chr(10).join(lines) or '(keine)'}\n\n"
            f"Auftrag:\n{vact.scrub(order)[:WORDING_MAX]}\n\nAntworte jetzt nur mit dem JSON.")


def parse_classification(text: str, open_nrs: set[int]) -> tuple[str, int | None, str] | None:
    """(action, nr, title) from the model answer, None when unusable. A `sub` for a card that
    is not open becomes `new`."""
    m = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except ValueError:
        return None
    if not isinstance(d, dict):
        return None
    title = _clean_title(d.get("title") or "")
    nr = d.get("nr")
    if d.get("action") == "sub" and isinstance(nr, int) and not isinstance(nr, bool) and nr in open_nrs:
        return "sub", nr, title
    return ("new", None, title) if title else None
