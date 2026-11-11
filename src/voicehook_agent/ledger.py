"""Open gaps of this agent, as the worker sees them (`operator.ledger`, Ravi 11.11.).

The worker (LiveKit kind AGENT, only to this join's identity) sends the FULL state of what
this agent still owes the call:

    {"v": 1, "ts": 1794400000.0, "agent": "<identity>",
     "thresholds": {"board_stale_s": 600, "unanswered_turn_s": 120, ...},
     "items": [{"id": "board_stale:<identity>", "kind": "board_stale", "since": 1794399300.0,
                "ref": "<id>", "hint": "Board seit 11 min nicht aktualisiert, ..."}, ...]}

`items: []` means all done. Sent on a change of the id set (at most every 60 s, at once when
it becomes empty). Hints never carry the human's words, `ref` is only an ID. Unknown fields
(packet and items, e.g. a future `sub` on helper items) are kept as they are.

* `parse` validates (v == 1, items a list of dicts with str id/kind); an unknown version or a
  broken packet raises `LedgerError` (the join prints one `_meta` line, keeps the old state).
* `write` / `read`: `<join dir>/ledger.json` (0600, tmp + os.replace).
* `event`: the `next` event `{type: "ledger", items, thresholds, text}` on a change of the id
  set (never waking on its own, like human_state).
* `hook_line`: the short todo line the PostToolUse hook of the joining session adds to its
  reminder (midturn.py), at most once per REMIND_GAP_S while the id set stays the same; a file
  older than STALE_S says so instead of showing old items.

Stdlib only (the hook imports it), never raises in the hook path.
"""
from __future__ import annotations

import contextlib
import json
import time
from pathlib import Path

TOPIC = "operator.ledger"
VERSION = 1
FILE_NAME = "ledger.json"
SHOWN_NAME = "ledger-shown.json"   # the hook's marker: which id set it showed, when
STALE_S = 600.0       # ledger.json older than this (by its ts): stale, items not shown
REMIND_GAP_S = 180.0  # the same open id set again in the hook at most this often
HINT_MAX = 160
SHOW_MAX = 5

# short German phrases per known kind (the event's text); an unknown kind uses its hint
KIND_TEXT = {
    "board_stale": "Board seit {age} alt",
    "unanswered_turn": "Äußerung seit {age} unbeantwortet",
    "question_not_decide": "Frage nur als Aktivität gemeldet",
    "waiting_without_card": "Wartet ohne Karte",
}


class LedgerError(ValueError):
    """A packet that is not taken (unknown version, broken shape); the text is for `_meta`."""


def parse(payload: object) -> dict:
    """The packet as a dict (a copy, unknown fields kept) or LedgerError."""
    if not isinstance(payload, dict):
        raise LedgerError("kein Objekt ignoriert")
    v = payload.get("v")
    if not (isinstance(v, int) and not isinstance(v, bool) and v == VERSION):
        raise LedgerError(f"unbekannte Version v={v!r} ignoriert")
    items = payload.get("items")
    if not isinstance(items, list):
        raise LedgerError("items ist keine Liste, ignoriert")
    for it in items:
        if not (isinstance(it, dict) and isinstance(it.get("id"), str) and it["id"]
                and isinstance(it.get("kind"), str) and it["kind"]):
            raise LedgerError("Eintrag ohne id/kind, ignoriert")
    out = dict(payload)
    out["items"] = [dict(it) for it in items]
    if not isinstance(out.get("thresholds"), dict):
        out["thresholds"] = {}
    return out


def ids(state: dict | None) -> frozenset[str]:
    return frozenset(str(it.get("id")) for it in ((state or {}).get("items") or [])
                     if isinstance(it, dict))


def _num(v: object) -> float | None:
    if isinstance(v, bool):
        return None
    try:
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _age(seconds: float) -> str:
    s = max(0, int(seconds))
    return f"{s // 60} min" if s >= 120 else f"{s} s"


def item_text(it: dict, ref_ts: float | None) -> str:
    tmpl = KIND_TEXT.get(str(it.get("kind")))
    since = _num(it.get("since"))
    if tmpl is not None:
        if "{age}" not in tmpl:
            return tmpl
        if since is not None and ref_ts is not None:
            return tmpl.format(age=_age(ref_ts - since))
        return tmpl.replace(" seit {age}", "")
    hint = str(it.get("hint") or "").strip()
    return hint[:HINT_MAX].rstrip(" .") if hint else str(it.get("kind"))[:60]


def summary(state: dict | None) -> str:
    """'2 offene Punkte: Board seit 11 min alt; Frage nur als Aktivität gemeldet.' /
    'Alles erledigt.'"""
    items = [it for it in ((state or {}).get("items") or []) if isinstance(it, dict)]
    if not items:
        return "Alles erledigt."
    ref = _num((state or {}).get("ts"))
    head = "1 offener Punkt" if len(items) == 1 else f"{len(items)} offene Punkte"
    parts = [item_text(it, ref) for it in items[:SHOW_MAX]]
    more = f" (+{len(items) - SHOW_MAX})" if len(items) > SHOW_MAX else ""
    return f"{head}: " + "; ".join(parts) + more + "."


def event(state: dict, now: float | None = None) -> dict:
    """The `next` event of a change of the id set."""
    return {"type": "ledger", "role": "system", "items": list(state.get("items") or []),
            "thresholds": dict(state.get("thresholds") or {}), "text": summary(state),
            "ts": time.time() if now is None else now}


def status_view(state: dict | None) -> dict:
    """For `status`: counts only."""
    return {"items": len(ids(state)) if state else 0, "ts": (state or {}).get("ts")}


def write(dir_: Path, state: dict) -> None:
    """`<join dir>/ledger.json`, atomic (tmp + os.replace), 0600. OSError on a disk error."""
    from .activity import _write_private
    _write_private(Path(dir_) / FILE_NAME, json.dumps(state, ensure_ascii=False))


def read(dir_: Path) -> dict | None:
    """The last valid packet of this join (None: none, unreadable or not valid)."""
    try:
        return parse(json.loads((Path(dir_) / FILE_NAME).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None


def stale(state: dict, now: float) -> float | None:
    """Seconds since the packet's ts when older than STALE_S (no ts: stale), else None."""
    ts = _num(state.get("ts"))
    if ts is None:
        return float("inf")
    age = now - ts
    return age if age > STALE_S else None


def _hint_text(state: dict) -> str:
    items = [it for it in (state.get("items") or []) if isinstance(it, dict)]
    ref = _num(state.get("ts"))
    parts = []
    for it in items[:SHOW_MAX]:
        h = str(it.get("hint") or "").strip()[:HINT_MAX].rstrip(" .")
        parts.append(h or item_text(it, ref))
    more = f" (+{len(items) - SHOW_MAX})" if len(items) > SHOW_MAX else ""
    head = "1 offener Punkt" if len(items) == 1 else f"{len(items)} offene Punkte"
    return f"{head}: " + "; ".join(parts) + more + "."


def hook_line(dir_: Path, now: float | None = None) -> str | None:
    """The todo line for the hook's reminder, or None (nothing new to say). Shown when the id
    set changed since the hook last showed it, and again after REMIND_GAP_S while items stay
    open. A stale file: one note per packet. Never raises."""
    try:
        t = time.time() if now is None else now
        state = read(dir_)
        if state is None:
            return None
        shown_p = Path(dir_) / SHOWN_NAME
        try:
            shown = json.loads(shown_p.read_text(encoding="utf-8"))
            shown = shown if isinstance(shown, dict) else {}
        except (OSError, ValueError):
            shown = {}
        cur = sorted(ids(state))
        old = stale(state, t)
        if old is not None:
            key = ["stale", state.get("ts")]
            if shown.get("ids") == key:
                return None
            text = ("voicehook offene Punkte: Stand veraltet ("
                    + (f"vor {_age(old)}" if old != float("inf") else "ohne Zeit")
                    + "), aktuelle Punkte unbekannt.")
        else:
            key = cur
            if shown.get("ids") == key and (
                    not cur or t - (_num(shown.get("at")) or 0.0) < REMIND_GAP_S):
                return None
            if not cur and shown.get("ids") is None:
                return None   # never shown anything: an empty ledger is nothing to say
            text = ("voicehook offene Punkte: alles erledigt." if not cur
                    else "voicehook offene Punkte (vom Call): " + _hint_text(state))
        with contextlib.suppress(OSError):
            from .activity import _write_private
            _write_private(shown_p, json.dumps({"ids": key, "at": t}))
        return text
    except Exception:  # noqa: BLE001 - a hook must never disturb Claude Code
        return None
