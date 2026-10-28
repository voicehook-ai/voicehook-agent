"""Which local sessions work right now without being part of the call (0.33.0, Part C).

The watcher proposes each such session ONCE on the Decision Board (sender `Wächter`); on
"Ja" it covers the session like a call session (status, activity, Kanban) under the
session's label with the watcher's own relay credential (the server binds a board to the
call through the relay token and maps `from` to its own slot, so no extra credential is
needed). "Nein" = never proposed again in this call.

Privacy: the card holds the session label and the host, nothing of the transcript.
VOICEHOOK_WATCH_NEVER (comma separated, case-insensitive substrings of the label or the
project folder) are never proposed. VOICEHOOK_WATCH_ALL=1 covers everything without asking.

State file ~/.voicehook/watch-subscribed.json (0600), bound to the call by a hash of the
relay token: another call (or the end of this one) starts it empty.

stdlib only; no import of watch.py.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from . import activity as vact
from . import multisession as vmulti

STATE_NAME = "watch-subscribed.json"
NAME = "Wächter"                    # sender of the proposals
ENV_NEVER = "VOICEHOOK_WATCH_NEVER"
MAX_OPEN = 3
REPROPOSE_S = 7200.0                # inactive this long, then active again: ask again
TTL_S = 7200                        # an unanswered card expires on the server after this
KEEP = 200


def never_patterns(env=None) -> list[str]:
    env = os.environ if env is None else env
    return [p.strip().lower() for p in str(env.get(ENV_NEVER) or "").split(",") if p.strip()]


def is_never(label: str, tp: str, env=None) -> bool:
    pats = never_patterns(env)
    if not pats:
        return False
    hay = f"{label}\n{Path(tp).parent.name if tp else ''}".lower()
    return any(p in hay for p in pats)


# --------------------------------------------------------------------------- #
# state
# --------------------------------------------------------------------------- #
def _path() -> Path:
    return vact.state_root() / STATE_NAME


def call_hash(rel: dict) -> str:
    return vmulti._token_hash(str(rel.get("token") or ""))


def load(rel: dict) -> dict:
    # {"call", "subscribed": {key: {label, tp, at}}, "proposed": {key: {id, at, label, tp,
    #  last_active, answer: None|"ja"|"nein"}}}
    """The state of THIS call (empty for another call)."""
    try:
        d = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        d = {}
    if not isinstance(d, dict) or d.get("call") != call_hash(rel):
        d = {"call": call_hash(rel)}
    for k in ("subscribed", "proposed"):
        if not isinstance(d.get(k), dict):
            d[k] = {}
    return d


def save(st: dict) -> None:
    try:
        for k in ("subscribed", "proposed"):
            items = sorted(st.get(k, {}).items(), key=lambda kv: float((kv[1] or {}).get("at") or 0))
            st[k] = dict(items[-KEEP:])
        vact._private_dir(vact.state_root())
        vact._write_private(_path(), json.dumps(st))
    except Exception:  # noqa: BLE001, S110
        pass


def clear() -> None:
    try:
        _path().unlink()
    except OSError:
        pass


def subscribed_sessions(rel: dict, taken: set[str]) -> list[dict]:
    """Opted-in sessions as discover() entries (not those already discovered)."""
    out = []
    for key, e in load(rel)["subscribed"].items():
        if key in taken or not isinstance(e, dict) or not e.get("tp") or not e.get("label"):
            continue
        label = str(e["label"])
        if label == "Session":   # stored before 0.33.2: name it from the transcript now
            label = label_for(Path(str(e["tp"])).stem, str(e["tp"]))
        out.append({"key": key, "label": label, "agent": "claude", "tp": str(e["tp"]),
                    "ev": "", "ev_text": "", "ev_at": 0.0})
    return out


# --------------------------------------------------------------------------- #
# finding active, unbound sessions
# --------------------------------------------------------------------------- #
def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def sub_mtime(tp: str) -> float:
    """Newest write of a session's subagent transcripts (<session>/subagents/*.jsonl)."""
    if not tp:
        return 0.0
    d = Path(tp.removesuffix(".jsonl")) / "subagents"
    try:
        return max((_mtime(p) for p in d.glob("*.jsonl")), default=0.0)
    except OSError:
        return 0.0


def registry() -> tuple[set[str], set[str]]:
    """(session keys, transcript paths) the hook registry knows, whatever their age."""
    try:
        st = json.loads((vact.state_root() / vmulti.STATE_NAME).read_text(encoding="utf-8"))
        reg = st.get("sessions") if isinstance(st, dict) else {}
    except (OSError, ValueError):
        reg = {}
    reg = reg if isinstance(reg, dict) else {}
    return set(reg), {str(e.get("tp")) for e in reg.values() if isinstance(e, dict) and e.get("tp")}


def find_unbound(rel: dict, now: float, active_s: float, *, claude_root: Path,
                 bound_keys: set[str] = frozenset(), bound_tps: set[str] = frozenset()) -> list[dict]:
    """Claude Code sessions active within `active_s` that are not part of the call: not in
    the hook registry, no helper credential / noted helper session (both end up in the
    registry or in `bound_keys`), not the joining session, not opted in. A subagent
    transcript counts for its parent session (<project>/<session>/subagents/*.jsonl); a
    subagent of a bound session is never proposed on its own. Newest first. Never raises."""
    out: list[dict] = []
    try:
        reg_keys, reg_tps = registry()
        keys = set(reg_keys) | set(bound_keys) | {str(rel.get("owner_session") or "")}
        tps = set(reg_tps) | set(bound_tps)
        subscribed = set(load(rel)["subscribed"])
        seen: dict[str, float] = {}
        for p in claude_root.glob("*/*.jsonl"):
            seen[str(p)] = max(seen.get(str(p), 0.0), _mtime(p))
        for p in claude_root.glob("*/*/subagents/*.jsonl"):
            parent = p.parent.parent
            tp = str(parent.with_name(parent.name + ".jsonl"))
            seen[tp] = max(seen.get(tp, 0.0), _mtime(p))
        for tp, m in seen.items():
            stem = Path(tp).stem
            key = vmulti.session_hash(stem)
            if not m or now - m > active_s or key in keys or tp in tps or key in subscribed:
                continue
            out.append({"key": key, "label": label_for(stem, tp), "agent": "claude", "tp": tp,
                        "active_at": m})
    except Exception:  # noqa: BLE001
        return []
    return sorted(out, key=lambda s: -s["active_at"])


def transcript_cwd(tp: str, lines: int = 80) -> str:
    """Working directory a Claude Code transcript records (field `cwd` of its first lines),
    else ''. Only the directory path, never message content."""
    try:
        with open(tp, encoding="utf-8", errors="replace") as f:
            for _ in range(lines):
                line = f.readline()
                if not line:
                    break
                if '"cwd"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                cwd = d.get("cwd") if isinstance(d, dict) else None
                if isinstance(cwd, str) and cwd:
                    return cwd
    except OSError:
        pass
    return ""


def label_for(stem: str, tp: str) -> str:
    """Session label like the hooks build it: /rename title, else ai-title, else
    `<basename of cwd> <4 hex>`; the cwd comes from the transcript (the plain fallback
    "Session" made cards read "Session Session arbeitet ...")."""
    return vmulti.session_label({"session_id": stem, "cwd": transcript_cwd(tp), "transcript_path": tp})


# --------------------------------------------------------------------------- #
# card + answers
# --------------------------------------------------------------------------- #
SUMMARY_NAME = "watch-summary.json"
SUMMARY_GAP_S = 900.0   # 0.33.4: at most one summary card per machine in 15 min (a re-import
                        # of a credential looked like a new call and asked again)


def _summary_path() -> Path:
    env = (os.environ.get("VOICEHOOK_MACHINE_DIR") or "").strip()
    return (Path(env) if env else Path.home() / ".voicehook") / SUMMARY_NAME


def summary_recent(now: float) -> bool:
    try:
        at = float(json.loads(_summary_path().read_text(encoding="utf-8")).get("at") or 0)
    except (OSError, ValueError, AttributeError):
        return False
    return now - at < SUMMARY_GAP_S


def mark_summary(now: float) -> None:
    try:
        vact._private_dir(_summary_path().parent)
        fd = os.open(_summary_path(), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            os.write(fd, json.dumps({"at": now}).encode("utf-8"))
        finally:
            os.close(fd)
    except OSError:
        pass


ALL_KEY = "__alle__"   # 0.33.3: the summary card at the start of a call
ALL_MAX = 12


def summary_body(labels: list[str], host: str, now: float) -> dict:
    """One card for every session working in parallel that is not in the call yet: names and
    host only, never transcript content."""
    n = len(labels)
    names = ", ".join(labels)
    return {
        "question": f"{n} Sessions arbeiten gerade parallel auf {host} und sind nicht im Call, "
                    f"alle abonnieren?"[:300],
        "options": ["Ja, alle abonnieren", "Nein"],
        "context": (f"Auf {host} arbeiten gerade {n} Sessions, die noch nicht zu diesem Call gehören: "
                    f"{names}. Bei Ja zeigt der Wächter ab dem nächsten Durchlauf für jede ihren Stand "
                    "auf dem Board, schreibt Aktivität und pflegt das Kanban. Bei Nein fragt er in "
                    "diesem Call nicht wieder; später neu startende Sessions fragt er einzeln.")[:2000],
        "faq": [{"q": "Welche Sessions sind das?", "a": names[:200]},
                {"q": "Was passiert bei Ja?",
                 "a": "Der Wächter meldet Stand, Aktivität und Aufträge dieser Sessions in diesem Call."},
                {"q": "Sieht jemand Inhalte daraus?",
                 "a": "Auf der Karte stehen nur Namen und Rechner, nichts aus den Gesprächen."}],
        "ttl_s": int(TTL_S),
    }


def _named(label: str) -> str:
    """'Session <label>' unless the label already starts with Session."""
    return label if label.lower().startswith("session") else f"Session {label}"


def proposal_body(label: str, host: str, since: float, now: float) -> dict:
    """The decision card: label and host only, never transcript content."""
    mins = max(1, int((now - since) // 60))
    return {
        "question": f"{_named(label)} arbeitet gerade auf {host}, soll sie ins Board?"[:300],
        "options": ["Ja", "Nein"],
        "context": (f"Die Sitzung {label} auf {host} arbeitet seit etwa {mins} Minuten und gehört noch "
                    "nicht zu diesem Call. Bei Ja zeigt der Wächter ihren Stand auf dem Board, "
                    "schreibt Aktivität und pflegt das Kanban. Bei Nein fragt er nicht wieder."),
        "faq": [{"q": "Was passiert bei Ja?",
                 "a": "Der Wächter meldet Stand, Aktivität und Aufträge dieser Sitzung in diesem Call."},
                {"q": "Was passiert bei Nein?",
                 "a": "Die Sitzung bleibt außen vor und wird in diesem Call nicht noch einmal vorgeschlagen."},
                {"q": "Was sieht der Wächter?",
                 "a": "Nur ob die Sitzung arbeitet und ihre Aufträge, nichts wird ohne dein Ja gelesen."}],
        "ttl_s": TTL_S,
    }


def answer_of(ev: dict) -> tuple[str, str] | None:
    """(decision id, 'ja'|'nein') of an answer event of the proposals, else None."""
    if not isinstance(ev, dict) or ev.get("type", "decision") != "decision" or not ev.get("id"):
        return None
    verdict, opt = ev.get("verdict"), ev.get("option")
    if verdict == "yes" or (verdict == "option" and opt == 0):
        return str(ev["id"]), "ja"
    if verdict == "no" or (verdict == "option" and opt == 1):
        return str(ev["id"]), "nein"
    return None
