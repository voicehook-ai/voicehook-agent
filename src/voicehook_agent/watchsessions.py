"""Session list of the Session Watcher (0.35.0, Oliver 29.10.2026).

The call page shows every local Claude Code session of this machine of the last LIST_DAYS,
grouped by the page into "Heute aktiv" / "Gestern aktiv" / "Älter", each with name, topic,
"zuletzt aktiv" and a switch "mit in den Call". 0.35.6 (Oliver 06.11.: "nur die Heads"):
only main sessions, the ones the user sees in his own UI; subagents of a session (Claude
Code writes them to <session>/subagents/agent-<id>.jsonl) are never listed, and a name
appears once (two transcripts with the same name, e.g. after /resume: the one in the call,
else the newest).

The watcher POSTs the list to <relay>/sessions (voicehook-v4 watch_sessions.py) and fetches
the switches made on the page with GET <relay>/sessions ({"picks": [{id, on, at}]}). A
switch is kept on this machine (sessionnames.PICKS_FILE, survives restarts and new calls):
on = in every call, off = never covered or proposed. No choice = the automatic cards.

Only names, topics, times and opaque ids leave the machine; never transcript content. The
ids are hashes, the page never shows them.

stdlib only; no import of watch.py.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from . import multisession as vmulti
from . import relayops as vrelayops
from . import sessionnames as vnames
from . import watchjoin as J

LIST_DAYS = 7               # sessions touched in the last week (today / yesterday / older)
LIST_MAX = 30               # top-level sessions in the list, newest first
POST_EVERY_S = 300.0        # repost an unchanged list now and then (server memory, restarts)


def _id(prefix: str, value: str) -> str:
    return prefix + hashlib.sha256(f"vh-list|{value}".encode()).hexdigest()[:16]


def session_id_of(key: str) -> str:
    return _id("s", key)


def topic_for(key: str, tp: str, name: str, folder: str) -> str:
    """Derived topic > ai-title > the project folder; never the name itself again."""
    t = vnames.topic(key)
    if not t and tp:
        _c, ai = vmulti._transcript_titles(tp)
        t = vnames.clean(ai, vnames.TOPIC_MAX) if ai and not vnames.is_slug(ai) else ""
    if not t or t.lower().startswith(name.lower()) or name.lower().startswith(t.lower()):
        t = f"Arbeit im Ordner {folder}" if folder and folder.lower() != name.lower() else (folder or "")
        if not t or t.lower() == name.lower():
            t = "ohne erkennbaren Auftrag"
    return t


class Lister:
    """Builds the list (cached per transcript mtime) and remembers id -> session."""

    def __init__(self) -> None:
        self.cache: dict[str, tuple[float, dict]] = {}
        self.ids: dict[str, dict] = {}
        self.sig = ""
        self.post_at = 0.0
        self.off_until = 0.0

    def _top(self, tp: str, m: float) -> dict:
        hit = self.cache.get(tp)
        if hit and hit[0] == m:
            return dict(hit[1])
        stem = Path(tp).stem
        key = vmulti.session_hash(stem)
        cwd = J.transcript_cwd(tp)
        e = {"key": key, "tp": tp, "label": J.label_for(stem, tp),
             "last": vnames.real_activity(tp) or m, "folder": vnames.clean(Path(cwd).name if cwd else ""),
             "project": vnames.clean(vnames.project_of(vnames.latest_cwd(tp)))}
        self.cache[tp] = (m, e)
        return dict(e)

    def build(self, claude_root: Path, now: float, in_call_keys: set[str], in_call_tps: set[str],
              picks: dict[str, bool]) -> list[dict]:
        """List entries for the server, newest first; main sessions only, one per name."""
        tops: list[tuple[float, str]] = []
        try:
            for p in claude_root.glob("*/*.jsonl"):
                try:
                    m = p.stat().st_mtime
                except OSError:
                    continue
                if now - m <= LIST_DAYS * 86400.0:
                    tops.append((m, str(p)))
        except OSError:
            return []
        tops.sort(reverse=True)
        out: list[dict] = []
        ids: dict[str, dict] = {}
        for m, tp in tops:
            if len(out) >= LIST_MAX:
                break
            e = self._top(tp, m)
            if J.is_never(e["label"], tp):
                continue
            try:
                name = vrelayops.check_from(e["label"])
            except vrelayops.RelayError:
                continue
            in_call = e["key"] in in_call_keys or tp in in_call_tps
            dup = next((i for i, x in enumerate(out) if x["name"] == name), None)
            if dup is not None and (not in_call or out[dup]["in_call"]):
                continue           # the newer one (or the one in the call) stays
            sid = session_id_of(e["key"])
            ids[sid] = {"kind": "session", "key": e["key"], "tp": tp, "label": name}
            row = {"id": sid, "name": name, "topic": topic_for(e["key"], tp, name, e["folder"]),
                        "last_active": round(e["last"], 1), "parent": None, "parent_name": "",
                        "on": bool(picks.get(e["key"], in_call)), "in_call": in_call, "agent": "claude",
                        "project": e["project"]}
            if dup is None:
                out.append(row)
            else:                  # the one in the call replaces the newer one, same place
                ids.pop(out[dup]["id"], None)
                out[dup] = row
        self.ids = ids
        return out

    def changed(self, entries: list[dict], now: float) -> bool:
        sig = json.dumps(entries, sort_keys=True)
        if sig != self.sig or now - self.post_at >= POST_EVERY_S:
            self.sig = sig
            return True
        return False


def post_list(rel: dict, entries: list[dict]) -> int:
    status, _out = vrelayops._http(rel, "POST", vrelayops.endpoint(rel, "sessions"),
                                   {"sessions": entries}, 10.0)
    return status


def get_picks(rel: dict) -> tuple[int, list[dict]]:
    """(status, picks): the switches made on the page since the last GET, then the choices
    the server keeps on the ACCOUNT for this machine ("saved": {id: on}, cross-device, also
    after a reinstall) as picks with "saved": True."""
    status, out = vrelayops._http(rel, "GET", vrelayops.endpoint(rel, "sessions"), None, 10.0)
    picks = out.get("picks") if isinstance(out, dict) else None
    got = [p for p in (picks or []) if isinstance(p, dict) and isinstance(p.get("id"), str)]
    saved = out.get("saved") if isinstance(out, dict) else None
    if isinstance(saved, dict):
        fresh = {p["id"] for p in got}
        got = [{"id": k, "on": bool(v), "saved": True} for k, v in saved.items()
               if isinstance(k, str) and isinstance(v, bool) and k not in fresh] + got
    return status, got
