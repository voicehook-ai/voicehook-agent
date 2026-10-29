"""Session list of the Session Watcher (0.35.0, Oliver 29.10.2026).

The call page shows every local Claude Code session of this machine of the last LIST_DAYS,
grouped by the page into "Heute aktiv" / "Gestern aktiv" / "Älter", each with name, topic,
"zuletzt aktiv" and a switch "mit in den Call". Subagents of a session (Claude Code writes
them to <session>/subagents/agent-<id>.jsonl, see sessionnames.subagents) are listed
indented under their session ("gehört zu: <Name>"), never as sessions of their own.

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
SUB_RECENT_S = 6 * 3600.0   # finished subagents stay listed this long
SUB_PER_SESSION = 5
ENTRIES_MAX = 80            # server limit
POST_EVERY_S = 300.0        # repost an unchanged list now and then (server memory, restarts)


def _id(prefix: str, value: str) -> str:
    return prefix + hashlib.sha256(f"vh-list|{value}".encode()).hexdigest()[:16]


def session_id_of(key: str) -> str:
    return _id("s", key)


def sub_key(parent_key: str, agent_id: str) -> str:
    """Watcher key of a subagent (subscription, picks)."""
    return "sub-" + hashlib.sha256(f"vh-sub|{parent_key}|{agent_id}".encode()).hexdigest()[:16]


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
             "project": vnames.clean(vnames.project_of(cwd))}
        self.cache[tp] = (m, e)
        return dict(e)

    def build(self, claude_root: Path, now: float, in_call_keys: set[str], in_call_tps: set[str],
              picks: dict[str, bool]) -> list[dict]:
        """List entries for the server, newest first; subagents follow their session."""
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
            if len([x for x in out if not x["parent"]]) >= LIST_MAX or len(out) >= ENTRIES_MAX:
                break
            e = self._top(tp, m)
            if J.is_never(e["label"], tp):
                continue
            try:
                name = vrelayops.check_from(e["label"])
            except vrelayops.RelayError:
                continue
            in_call = e["key"] in in_call_keys or tp in in_call_tps
            sid = session_id_of(e["key"])
            ids[sid] = {"kind": "session", "key": e["key"], "tp": tp, "label": name}
            out.append({"id": sid, "name": name, "topic": topic_for(e["key"], tp, name, e["folder"]),
                        "last_active": round(e["last"], 1), "parent": None, "parent_name": "",
                        "on": bool(picks.get(e["key"], in_call)), "in_call": in_call, "agent": "claude",
                        "project": e["project"]})
            if now - m > 86400.0:
                continue
            n = 0
            for sub in vnames.subagents(tp):
                if n >= SUB_PER_SESSION or len(out) >= ENTRIES_MAX:
                    break
                age = now - sub["mtime"]
                if age > SUB_RECENT_S and (sub["finished"] or age > 86400.0):
                    continue
                skey = sub_key(e["key"], sub["id"])
                uid = _id("u", skey)
                s_in = skey in in_call_keys or sub["tp"] in in_call_tps
                ids[uid] = {"kind": "sub", "key": skey, "tp": sub["tp"], "label": sub["name"],
                            "parent_label": name, "parent_key": e["key"]}
                out.append({"id": uid, "name": sub["name"],
                            "topic": "" if sub["topic"].lower().startswith(sub["name"].lower()) else sub["topic"],
                            "last_active": round(vnames.real_activity(sub["tp"]) or sub["mtime"], 1),
                            "parent": sid, "parent_name": name,
                            "on": bool(picks.get(skey, s_in)), "in_call": s_in, "agent": "subagent",
                            "project": e["project"]})
                n += 1
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
    status, out = vrelayops._http(rel, "GET", vrelayops.endpoint(rel, "sessions"), None, 10.0)
    picks = out.get("picks") if isinstance(out, dict) else None
    return status, [p for p in (picks or []) if isinstance(p, dict) and isinstance(p.get("id"), str)]
