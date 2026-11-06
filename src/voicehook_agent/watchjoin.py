"""Which local sessions work right now without being part of the call (0.33.0, Part C).

The Session Watcher (0.35.0 name, before "Wächter" / "Hausmeister") proposes each such
session ONCE on the Decision Board ("Soll <Name> mit Thema <Thema> in den Call?"); on
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

import contextlib
import json
import os
import socket
from pathlib import Path

from . import activity as vact
from . import multisession as vmulti
from . import session as vsession
from . import sessionnames as vnames

STATE_NAME = "watch-subscribed.json"
NAME = "Session Watcher"            # sender of the proposals (0.35.0, before "Wächter")
# 0.35.0 (Oliver 29.10.): a card only for a session with REAL activity (an order, an assistant
# turn or a tool result, by entry timestamps; see sessionnames.real_activity) this recent. Five
# minutes: a working session writes a step at least every few minutes (long tool runs
# included), a session idle longer is not "active right now" and stays in the session list,
# where the human can still pick it. Kept separate from watch.ACTIVE_S (state "arbeitet").
PROPOSE_ACTIVE_S = 300.0
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
    """The call the state belongs to: the watcher's stable call id (0.33.5) or the token."""
    if rel.get("call_id"):
        return str(rel["call_id"])
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
        if e.get("parent"):   # 0.35.6: a subagent picked before (0.35.0): heads only now
            continue
        label = str(e["label"])
        if label == "Session":   # stored before 0.33.2: name it from the transcript now
            label = label_for(Path(str(e["tp"])).stem, str(e["tp"]))
        item = {"key": key, "label": label, "agent": "claude", "tp": str(e["tp"]),
                "ev": "", "ev_text": "", "ev_at": 0.0}
        out.append(item)
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


SESSION_ENVS = ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID")


def _sock_alive(path: Path, timeout: float = 0.2) -> bool:
    """A join's control socket that still answers (a crashed join leaves a dead file)."""
    try:
        if not path.is_socket():
            return False
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(str(path))
        return True
    except OSError:
        return False


def name_of_identity(identity: str) -> str:
    """'malik-crown-5217' -> 'malik' (join identity = <name>-<host>-<suffix>)."""
    parts = str(identity or "").split("-")
    return parts[0].lower() if len(parts) >= 3 else str(identity or "").lower()


def room_joins(rel: dict, *, proc_root: Path = Path("/proc"), sess_root: Path | None = None) -> tuple[set[str], set[str]]:
    """0.35.6 (Oliver 06.11.: "fragt mich der Sessionwatcher staendig, ob Leute in den Call
    sollen, die schon im Call sind"): sessions of THIS machine that sit in THIS room with
    their OWN join (data channel). Returns (transcript stems, names).
    (a) /proc (Linux): every running `voicehook-agent join` whose command line names this
        room; its environment carries the session id of the Claude Code / Codex session
        that started it (CLAUDE_CODE_SESSION_ID / CODEX_THREAD_ID = transcript stem).
    (b) ONLY without /proc (Mac): every join dir of the room with a control socket that still
        answers: its name (identity prefix). Names are a fallback, they are fuzzy.
    Only ids and names, no content. Never raises."""
    room = str((rel or {}).get("room") or "")
    stems: set[str] = set()
    names: set[str] = set()
    if not room:
        return stems, names
    with contextlib.suppress(Exception):
        for d in proc_root.glob("[0-9]*"):
            try:
                cmd = [c.decode("utf-8", "replace") for c in (d / "cmdline").read_bytes().split(b"\0") if c]
            except OSError:
                continue
            line = " ".join(cmd)
            if "join" not in cmd or ("voicehook-agent" not in line and "voicehook_agent" not in line):
                continue
            if f"/r/{room}" not in line and f"room={room}" not in line:
                continue
            try:
                env = (d / "environ").read_bytes().split(b"\0")
            except OSError:
                continue
            for e in env:
                k, _, v = e.decode("utf-8", "replace").partition("=")
                if k in SESSION_ENVS and v.strip():
                    stems.add(v.strip())
    # Review Ines 06.11.: der Namensabgleich ist unscharf (ein fremder Titel mit einem Head-Namen
    # wuerde ausgeblendet). Unter Linux genuegen die Session-Ids; Namen nur ohne /proc.
    if proc_root.is_dir():
        return stems, names
    with contextlib.suppress(Exception):
        rd = (sess_root / room) if sess_root is not None else vsession.room_dir(room)
        for sub in rd.iterdir() if rd.is_dir() else []:
            if _sock_alive(sub / vsession.SOCKET_NAME):
                names.add(name_of_identity(sub.name))
    return stems, names


def _label_names(label: str) -> set[str]:
    return {w.strip("·:,()[]").lower() for w in str(label or "").replace("-", " ").split() if len(w.strip("·:,()[]")) >= 3}


def find_unbound(rel: dict, now: float, active_s: float, *, claude_root: Path,
                 bound_keys: set[str] = frozenset(), bound_tps: set[str] = frozenset(),
                 joined: tuple[set[str], set[str]] | None = None) -> list[dict]:
    """Claude Code sessions with REAL activity within `active_s` (sessionnames.real_activity:
    entry timestamps of orders, assistant turns, tool results; the file time only preselects)
    that are not part of the call: not in the hook registry, no helper credential / noted
    helper session (both end up in the registry or in `bound_keys`), not the joining session,
    not opted in. A subagent (<project>/<session>/subagents/*.jsonl) counts for its parent and
    is never proposed on its own. Newest first, with `project`. Never raises."""
    out: list[dict] = []
    try:
        reg_keys, reg_tps = registry()
        keys = set(reg_keys) | set(bound_keys) | {str(rel.get("owner_session") or "")}
        tps = set(reg_tps) | set(bound_tps)
        subscribed = set(load(rel)["subscribed"])
        j_stems, j_names = room_joins(rel) if joined is None else joined
        keys |= {vmulti.session_hash(x) for x in j_stems}
        seen: dict[str, list[str]] = {}
        for p in claude_root.glob("*/*.jsonl"):
            if now - _mtime(p) <= active_s:
                seen.setdefault(str(p), []).append(str(p))
        for p in claude_root.glob("*/*/subagents/*.jsonl"):
            if now - _mtime(p) > active_s:
                continue
            parent = p.parent.parent
            tp = str(parent.with_name(parent.name + ".jsonl"))
            seen.setdefault(tp, []).append(str(p))
        for tp, files in seen.items():
            stem = Path(tp).stem
            key = vmulti.session_hash(stem)
            if key in keys or tp in tps or key in subscribed or not Path(tp).exists():
                continue
            act = max((vnames.real_activity(f) for f in files), default=0.0)
            if not act or now - act > active_s:
                continue
            lbl = label_for(stem, tp)
            if stem in j_stems or (j_names and _label_names(lbl) & j_names):
                continue        # sits in this room with its own join: already in the call
            cwd = transcript_cwd(tp)
            out.append({"key": key, "label": lbl, "agent": "claude", "tp": tp,
                        "active_at": act, "project": vnames.project_of(vnames.latest_cwd(tp)),
                        "folder": vact.basename(cwd)})
    except Exception:  # noqa: BLE001
        return []
    return sorted(out, key=lambda s: -s["active_at"])


def related(c: dict, projects: set[str], labels: set[str], picked_on: set[str]) -> bool:
    """0.35.0: a card only for a session related to THIS call: the same project as a session
    already in the call (git worktrees count as their main repository), a name that shares a
    word with one in the call, or picked "mit in den Call" in the session list before."""
    if c.get("key") in picked_on:
        return True
    if c.get("project") and c["project"] in projects:
        return True
    words = {w.lower() for w in str(c.get("label") or "").replace("-", " ").split() if len(w) >= 4}
    return any(words & {w.lower() for w in lab.replace("-", " ").split() if len(w) >= 4} for lab in labels)


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
                    f"{names}. Bei Ja zeigt der Session Watcher ab dem nächsten Durchlauf für jede ihren Stand "
                    "auf dem Board, schreibt Aktivität und pflegt das Kanban. Bei Nein fragt er in "
                    "diesem Call nicht wieder; später neu startende Sessions fragt er einzeln.")[:2000],
        "faq": [{"q": "Welche Sessions sind das?", "a": names[:200]},
                {"q": "Was passiert bei Ja?",
                 "a": "Der Session Watcher meldet Stand, Aktivität und Aufträge dieser Sessions in diesem Call."},
                {"q": "Sieht jemand Inhalte daraus?",
                 "a": "Auf der Karte stehen nur Namen und Rechner, nichts aus den Gesprächen."}],
        "ttl_s": int(TTL_S),
    }


def question(name: str, topic: str, lang: str = "de") -> str:
    """The card question, exactly this format (Oliver 29.10.2026)."""
    if lang == "en":
        return f"Should {name} join the call with topic {topic}?"
    return f"Soll {name} mit Thema {topic} in den Call?"


def proposal_body(label: str, host: str, since: float, now: float, topic: str = "",
                  lang: str = "de") -> dict:
    """The decision card: name, topic and host only, never transcript content."""
    mins = max(1, int((now - since) // 60))
    topic = topic or label
    if lang == "en":
        return {
            "question": question(label, topic, "en")[:300],
            "options": ["Yes", "No"],
            "context": (f"The session {label} on {host} was active {mins} minutes ago and is not part "
                        "of this call yet. Yes: the Session Watcher shows its progress on the board, "
                        "writes activity and keeps the Kanban. No: it does not ask again in this call."),
            "faq": [{"q": "What happens on Yes?",
                     "a": "The Session Watcher reports progress, activity and orders of this session in this call."},
                    {"q": "What happens on No?",
                     "a": "The session stays out and is not proposed again in this call."},
                    {"q": "What does the Session Watcher see?",
                     "a": "Only whether the session works, its name and topic, nothing of the conversation."}],
            "ttl_s": TTL_S,
        }
    return {
        "question": question(label, topic)[:300],
        "options": ["Ja", "Nein"],
        "context": (f"Die Session {label} auf {host} war vor {mins} Minuten aktiv und gehört noch "
                    "nicht zu diesem Call. Bei Ja zeigt der Session Watcher ihren Stand auf dem Board, "
                    "schreibt Aktivität und pflegt das Kanban. Bei Nein fragt er in diesem Call nicht wieder."),
        "faq": [{"q": "Was passiert bei Ja?",
                 "a": "Der Session Watcher meldet Stand, Aktivität und Aufträge dieser Session in diesem Call."},
                {"q": "Was passiert bei Nein?",
                 "a": "Die Session bleibt außen vor und wird in diesem Call nicht noch einmal vorgeschlagen."},
                {"q": "Was sieht der Session Watcher?",
                 "a": "Nur ob die Session arbeitet, ihren Namen und ihr Thema, nichts aus dem Gespräch."}],
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
