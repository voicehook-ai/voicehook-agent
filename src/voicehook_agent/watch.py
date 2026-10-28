"""`voicehook-agent watch` (0.32.0): ONE watcher per machine that keeps the board entries of
the local agent sessions (Claude Code, Codex) current and complete while a call is active.

Oliver 28.10.2026: sessions forget to report. A separate, detached process (never a child
of an agent session, it survives the session that started it) looks at every local session
and writes ONE whole German sentence per session to its board entry, with the session label
as sender; a state change also gets an activity line; decisions of a session that went on
(or whose question is obsolete) are removed from the Decision Board.

Start / stop:
* `join` and `helper-import` spawn it detached (`spawn_detached`) once a valid relay /
  helper credential exists. Opt out: VOICEHOOK_WATCH=0. By hand: `voicehook-agent watch`.
* Exactly one per machine: flock on ~/.voicehook/watch.lock; a second start exits 0.
* It ends on its own when the call is over: no usable credential (gone, expired, marked
  dead, owner gone) or the server answers 401/403/410 for the token. Checked every tick.
  Its lifetime is also capped by the credential's expires_at.

Duties per tick, one function each (`Watcher.duty_*`; a Kanban duty can be added the same way):
* status: board entry (`doing` = the sentence) every interval and at once on a state change.
  A session that posted its own board status within the interval (cli `status`,
  `mark_self`) is skipped: it reports itself.
* activity: an activity line on a state change.
* decisions: the open decision of a local session (notify-state.json, relay route only,
  created by the hooks of this machine) is deleted once the session went on or the
  question is older than notify.OPEN_KEEP_S and the session is gone. 404/409 on delete
  count as done. A session that still waits (state frage) keeps its decision; decisions of
  other machines or sessions are never touched (only ids this machine's hooks created).

Per session (registry relay-state.json + transcripts, see `discover`) the state is
arbeitet (hook event or transcript change in the last ACTIVE_S, no Stop since), still
(Stop, or nothing for a while) or frage (last event waits for input / permission /
AskUserQuestion and nothing happened since), plus "zuletzt aktiv".

Wording: the cheapest model writes the sentence from state + the last assistant message
(tail of the transcript, 2000 chars): `claude -p --model haiku` (override
VOICEHOOK_WATCH_MODEL_CMD, `off` = never). No model, a failed call or the hourly token
budget used up: a fixed sentence from the state. At most one model call per session per
interval, and only when the session did something since the last call.

Interval: server setting (GET <relay>/watch, call page: 1, 5, 10 or 15) > --interval >
VOICEHOOK_WATCH_INTERVAL > 15 minutes. Re-read every tick.

Guards: fixed tick (TICK_S), exponential backoff on errors, token budget per hour, no run
without a call. ~/.voicehook/watch.log holds metadata only (label, state, counts, tokens,
durations), size-rotated; never transcript text, never a generated sentence.

stdlib only.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from collections import deque
from pathlib import Path
from urllib.parse import quote

from . import activity as vact
from . import multisession as vmulti
from . import notify
from . import relayops as vrelayops
from . import watchjoin as J
from . import watchkanban as K

ALLOWED_INTERVALS = (1, 5, 10, 15)
DEFAULT_INTERVAL = 15
DEFAULT_TOKENS_PER_HOUR = 40000   # ~25 haiku calls/hour (one call ~1.5k tokens, see _tokens)
TICK_S = 45.0
BACKOFF_MAX_S = 600.0
NOT_FOUND_RECHECK_S = 600.0       # old server (404 on /watch): ask again only now and then
ACTIVE_S = 120.0                  # activity this recent = arbeitet
SETTLE_S = 10.0                   # transcript writes right after a Stop / Notification
RECENT_TRANSCRIPT_S = 1800.0      # unregistered sessions: transcript touched in the last 30 min
REGISTRY_RECENT_S = 7200.0
TAIL_BYTES = 256 * 1024
TEXT_CHARS = 2000
SENTENCE_MAX = 120                # server NOTE_MAX of an activity line
MODEL_TIMEOUT_S = 60.0
LOG_MAX_BYTES = 256 * 1024
BUDGET_WINDOW_S = 3600.0
MAX_SESSIONS = 12

ENV_OFF = "VOICEHOOK_WATCH"
ENV_INTERVAL = "VOICEHOOK_WATCH_INTERVAL"
ENV_TOKENS = "VOICEHOOK_WATCH_TOKENS_PER_HOUR"
ENV_MODEL_CMD = "VOICEHOOK_WATCH_MODEL_CMD"
ENV_ALL = "VOICEHOOK_WATCH_ALL"      # 1 = also sessions only found by their transcript (privacy: off)
ENV_CHILD = "VOICEHOOK_WATCH_CHILD"   # set for the model command: its hooks must not report
LOCK_NAME = "watch.lock"
LOG_NAME = "watch.log"
SELF_NAME = "watch-self.json"

ARBEITET, STILL, FRAGE = "arbeitet", "still", "frage"
END = "end"
OK = "ok"
GONE = (404, 409)                 # delete of a decision: already answered / gone = done
OVER = (401, 403, 410)            # the call is over
# 0.32.3: a single 401/403/410 (seen live during a server deploy) is not the end of the call;
# only OVER_CONFIRM answers in a row, spread over at least OVER_CONFIRM_S, end the watcher.
# The watcher never marks a token dead: it uses another session's credential.
OVER_CONFIRM = 3
OVER_CONFIRM_S = 90.0

DEFAULT_MODEL_CMD = ["claude", "-p", "--model", "haiku", "--output-format", "json",
                     "--tools", "", "--no-session-persistence", "--disable-slash-commands",
                     "--setting-sources", "", "--strict-mcp-config",
                     "--system-prompt",
                     ("Du schreibst fuer einen Sprachassistenten genau EINEN ganzen deutschen "
                     "Satz (hoechstens 110 Zeichen, keine Abkuerzungen, keine Dateipfade, keine "
                     "Anfuehrungszeichen), der den Stand einer Programmier-Sitzung nennt. "
                     "Gib nur den Satz aus.")]

STATE_TEXT = {ARBEITET: "arbeitet gerade", STILL: "ist fertig oder still",
              FRAGE: "wartet auf eine Eingabe oder Freigabe des Menschen"}


# --------------------------------------------------------------------------- #
# settings
# --------------------------------------------------------------------------- #
def disabled(env=None) -> bool:
    env = os.environ if env is None else env
    return str(env.get(ENV_OFF) or "").strip().lower() in ("0", "false", "no", "off")


def parse_interval(v: object) -> int | None:
    try:
        n = int(str(v).strip())
    except (TypeError, ValueError):
        return None
    return n if n in ALLOWED_INTERVALS else None


def resolve_interval(server: object, flag: object, env: object) -> tuple[int, str]:
    """(minutes, source): call-page setting > --interval > $VOICEHOOK_WATCH_INTERVAL > 15."""
    for name, v in (("server", server), ("flag", flag), ("env", env)):
        n = parse_interval(v)
        if n is not None:
            return n, name
    return DEFAULT_INTERVAL, "default"


def tokens_per_hour(flag: object = None, env=None) -> int:
    env = os.environ if env is None else env
    for v in (flag, env.get(ENV_TOKENS)):
        try:
            if v is not None and str(v).strip() != "":
                return max(0, int(str(v).strip()))
        except ValueError:
            continue
    return DEFAULT_TOKENS_PER_HOUR


# --------------------------------------------------------------------------- #
# one per machine + detached start
# --------------------------------------------------------------------------- #
def acquire_lock():
    """Open and flock ~/.voicehook/watch.lock (kept open for the process lifetime). The fd,
    or None when another watcher holds it."""
    try:
        import fcntl
    except ImportError:  # pragma: no cover  (Windows)
        return None
    vact._private_dir(vact.state_root())
    fd = os.open(vact.state_root() / LOCK_NAME, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    try:
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
    except OSError:
        pass
    return fd


def running() -> bool:
    """True when a watcher holds the lock right now."""
    fd = acquire_lock()
    if fd is None:
        return True
    os.close(fd)
    return False


_BOOT = ("import sys; sys.path.insert(0, sys.argv.pop(1)); "
         "from voicehook_agent.cli import main; main()")


def child_argv(extra: list[str] | None = None) -> list[str]:
    return [sys.executable, "-I", "-c", _BOOT, str(Path(__file__).resolve().parents[1]),
            "watch", "--foreground", *(extra or [])]


def spawn_detached(extra: list[str] | None = None, *, popen=subprocess.Popen) -> bool:
    """Start the watcher detached (own session via setsid: not a child of the calling agent
    session, survives it; stdio to /dev/null; cwd = state dir). Only with a valid credential,
    never when VOICEHOOK_WATCH=0, never twice (the child's flock decides; a held lock is
    checked here first). True when a process was started. Never raises."""
    try:
        if disabled() or credential() is None or running():
            return False
        popen(child_argv(extra), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
              stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True,
              cwd=vact.child_cwd())
        return True
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------- #
# credential: is a call active?
# --------------------------------------------------------------------------- #
def credential(now: float | None = None) -> dict | None:
    """The usable relay.json of a join on this machine, else the first usable helper entry
    (helper-import). None = no call (gone, expired, marked dead, owner gone)."""
    t = time.time() if now is None else now
    rel = vmulti.load_main_relay(t)
    if rel is not None:
        return rel
    for p, e in vmulti._helper_entries():
        u = vmulti._usable(e, p, t)
        if u is not None:
            return u
    return None


# --------------------------------------------------------------------------- #
# own board status marker (cli `status`): that session reports itself
# --------------------------------------------------------------------------- #
def mark_self(label: str, now: float | None = None, board: dict | None = None) -> None:
    """A session posted its own board status: the watcher leaves it alone for one interval,
    and remembers the board (a relay /status REPLACES the whole board of a sender, so the
    watcher must merge into it, see merge_board). Best effort, never raises."""
    if board is not None:
        remember_board(label, board, now, own=True)
    try:
        t = time.time() if now is None else now
        path = vact.state_root() / SELF_NAME
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            d = {}
        d = {k: v for k, v in (d if isinstance(d, dict) else {}).items()
             if isinstance(v, (int, float)) and t - v < 7200}
        d[str(label)] = t
        vact._private_dir(vact.state_root())
        vact._write_private(path, json.dumps(d))
    except Exception:  # noqa: BLE001, S110
        pass


BOARDS_NAME = "watch-boards.json"
BOARDS_KEEP = 50
BOARD_KEEP_S = 7200.0


def _boards_rw(fn):
    vact._private_dir(vact.state_root())
    fd = os.open(vact.state_root() / BOARDS_NAME, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        vact._lock(fd)
        try:
            d = json.loads(os.read(fd, 1 << 20).decode("utf-8") or "{}")
        except ValueError:
            d = {}
        d = d if isinstance(d, dict) else {}
        res = fn(d)
        keep = sorted(((k, v) for k, v in d.items() if isinstance(v, dict)),
                      key=lambda kv: float(kv[1].get("at") or 0))[-BOARDS_KEEP:]
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, json.dumps(dict(keep)).encode("utf-8"))
        return res
    finally:
        os.close(fd)


OWN_KEEP_S = 43200.0
OWN_MAX = 300
WATCHER_PREFIXES = ("inaktiv seit", "inaktiv,", "keine Meldung seit", "Die Sitzung ")


def remember_board(label: str, body: dict, now: float | None = None, own: bool = False) -> None:
    """The board body last posted for `label` (by its session or by the watcher). Local file
    0600, never sent anywhere. Best effort. own=True: the SESSION posted it; its `doing` is
    kept separately ("own") and survives every later watcher post (0.33.0: the inactive
    sentence quotes it)."""
    try:
        t = time.time() if now is None else now

        def put(d: dict) -> None:
            old = d.get(str(label)) if isinstance(d.get(str(label)), dict) else {}
            e: dict = {"b": body, "at": t}
            doing = str(body.get("doing") or "").strip()
            if own and doing:
                e["own"], e["own_at"] = doing[:OWN_MAX], t
            elif old.get("own"):
                e["own"], e["own_at"] = old["own"], old.get("own_at", 0)
            d[str(label)] = e
        _boards_rw(put)
    except Exception:  # noqa: BLE001, S110
        pass


def own_doing(label: str, now: float | None = None) -> str:
    """The last real `doing` the session itself posted (never a watcher sentence), else ''."""
    t = time.time() if now is None else now
    try:
        d = json.loads((vact.state_root() / BOARDS_NAME).read_text(encoding="utf-8"))
        e = d.get(str(label)) if isinstance(d, dict) else None
        if isinstance(e, dict) and isinstance(e.get("own"), str) and t - float(e.get("own_at") or 0) < OWN_KEEP_S:
            own = e["own"].strip()
            return "" if own.startswith(WATCHER_PREFIXES) else own
    except (OSError, ValueError, TypeError):
        pass
    return ""


def forget_board(label: str) -> None:
    try:
        _boards_rw(lambda d: d.pop(str(label), None))
    except Exception:  # noqa: BLE001, S110
        pass


def sub_boards() -> list[str]:
    """Senders (`<label> (Wächter)`) of the watcher boards this machine remembers."""
    try:
        d = json.loads((vact.state_root() / BOARDS_NAME).read_text(encoding="utf-8"))
        return [k for k in d if isinstance(k, str) and k.endswith(f" ({SUB_NAME})")] if isinstance(d, dict) else []
    except (OSError, ValueError):
        return []


def observable(s: dict) -> bool:
    """A session with a signal the watcher can read: hook events or a mapped transcript."""
    return bool(float(s.get("ev_at") or 0) or _mtime(s.get("tp") or ""))


def known_board(label: str, now: float | None = None) -> dict | None:
    """The last known board body of this sender (younger than BOARD_KEEP_S), else None."""
    t = time.time() if now is None else now
    try:
        d = json.loads((vact.state_root() / BOARDS_NAME).read_text(encoding="utf-8"))
        e = d.get(str(label)) if isinstance(d, dict) else None
        if isinstance(e, dict) and isinstance(e.get("b"), dict) and t - float(e.get("at") or 0) < BOARD_KEEP_S:
            return e["b"]
    except (OSError, ValueError, TypeError):
        pass
    return None


def doing_meta(label: str, state: str, last: float, now: float, card: int | None = None) -> dict:
    """Mandatory background of the `doing` entry (context + 3 faq), from the state alone."""
    ago = ago_text(now - last) if last else "unbekannter Zeit"
    ctx = f"Sitzung {label}: {STATE_TEXT[state]}, zuletzt aktiv vor {ago}. Stand vom Beobachter, nicht von der Sitzung selbst."
    waiting = state == FRAGE
    faq = [
        {"q": f"Was macht {label} gerade?",
         "a": "Die Sitzung arbeitet gerade." if state == ARBEITET else
              "Die Sitzung wartet auf eine Antwort." if waiting else "Im Moment nichts, sie ist fertig oder still."},
        {"q": f"Wann war {label} zuletzt aktiv?", "a": f"Vor {ago}."},
        {"q": f"Braucht {label} etwas von mir?",
         "a": "Ja, die Sitzung wartet auf eine Eingabe oder Freigabe." if waiting
         else "Nein, im Moment wartet sie auf nichts."},
    ]
    out = {"context": ctx[:2000], "faq": [{"q": f["q"][:200], "a": f["a"][:200]} for f in faq]}
    if card:
        out["card"] = card   # 0.33.0: the server mirror links the item to this Kanban card
    return out


STATE_FIELD = {ARBEITET: "arbeitet", STILL: "leerlauf", FRAGE: "frage"}   # server board states
SUB_NAME = "Wächter"


def sub_label(label: str) -> str:
    """Sender of the separate watcher board of a session (max 40 chars)."""
    return f"{label[:40 - len(SUB_NAME) - 3].rstrip()} ({SUB_NAME})"


def merge_board(base: dict | None, sentence: str, meta: dict | None = None,
                state: str | None = None) -> dict:
    """The board body to post: the known board with ONLY `doing` (and its meta) replaced and
    `state` set from our own detection; open, done, project and their meta stay. No known
    board: doing (+ state) alone."""
    out = {k: v for k, v in (base or {}).items() if k not in ("meta", "state")}
    out["doing"] = sentence
    if state in STATE_FIELD:
        out["state"] = STATE_FIELD[state]
    m = dict((base or {}).get("meta") or {})
    m.pop("doing", None)
    if meta:
        m["doing"] = meta
    if m:
        out["meta"] = m
    return out


def self_posted_at(label: str) -> float:
    try:
        d = json.loads((vact.state_root() / SELF_NAME).read_text(encoding="utf-8"))
        return float(d.get(label) or 0) if isinstance(d, dict) else 0.0
    except (OSError, ValueError, TypeError):
        return 0.0


# --------------------------------------------------------------------------- #
# log: metadata only
# --------------------------------------------------------------------------- #
def log(event: str, **kv: object) -> None:
    """One line `HH:MM:SS event k=v ...` in watch.log (size-rotated). Callers pass labels,
    states, counts, tokens, durations: never transcript text or generated sentences."""
    try:
        path = vact.state_root() / LOG_NAME
        vact._private_dir(vact.state_root())
        try:
            if path.stat().st_size > LOG_MAX_BYTES:
                os.replace(path, path.with_name(LOG_NAME + ".1"))
        except OSError:
            pass
        parts = [time.strftime("%H:%M:%S"), event]
        for k, v in kv.items():
            parts.append(f"{k}={str(v).replace(chr(10), ' ')[:60]}")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            os.write(fd, (" ".join(parts) + "\n").encode("utf-8"))
        finally:
            os.close(fd)
    except Exception:  # noqa: BLE001, S110
        pass


# --------------------------------------------------------------------------- #
# sessions
# --------------------------------------------------------------------------- #
def _mtime(path: str) -> float:
    try:
        return os.stat(path).st_mtime if path else 0.0
    except OSError:
        return 0.0


def _agent_of(path: str) -> str:
    return "codex" if "/.codex/" in path or "/rollout-" in path else "claude"


def _transcripts(root: Path, pattern: str, now: float) -> list[tuple[float, str]]:
    out = []
    try:
        for p in root.glob(pattern):
            m = _mtime(str(p))
            if m and now - m < RECENT_TRANSCRIPT_S:
                out.append((m, str(p)))
    except OSError:
        pass
    return sorted(out, reverse=True)


def _running_agents(ps=None) -> dict[str, int]:
    """Running top-level agent processes per kind (localsessions detection)."""
    from . import localsessions as ls
    try:
        procs = ls.parse_ps((ps or ls._ps)())
    except Exception:  # noqa: BLE001
        return {}
    kinds = {p: ls.agent_kind(a) for p, _, a in procs}
    out: dict[str, int] = {}
    for pid, ppid, _a in procs:
        k = kinds.get(pid)
        if k and not kinds.get(ppid):
            out[k] = out.get(k, 0) + 1
    return out


def _codex_sessions() -> Path:
    from . import agents
    return agents.codex_home() / "sessions"


def watch_all(env=None) -> bool:
    env = os.environ if env is None else env
    return str(env.get(ENV_ALL) or "").strip().lower() in ("1", "true", "yes", "on")


def discover(rel: dict, now: float, *, ps=None, claude_root: Path | None = None,
             codex_root: Path | None = None) -> list[dict]:
    """Local sessions of THIS call: [{key, label, agent, tp, ev, ev_text, ev_at}].
    (a) registry entries that reported through the relay (relay-state.json; the joining
    session itself excluded, it feeds the call directly), (b) sessions with a helper
    credential (helpers/<label>.json). Privacy: other sessions of the machine (private or
    customer work on a shared box) are NOT covered; only with VOICEHOOK_WATCH_ALL=1 also
    unregistered sessions whose transcript changed recently (at most as many as agent
    processes run). Never raises."""
    out: list[dict] = []
    try:
        owner = str(rel.get("owner_session") or "") if not rel.get("helper") else ""
        try:
            st = json.loads((vact.state_root() / vmulti.STATE_NAME).read_text(encoding="utf-8"))
            reg = st.get("sessions") if isinstance(st, dict) else {}
        except (OSError, ValueError):
            reg = {}
        seen_tp: set[str] = set()
        for key, me in (reg or {}).items():
            if not isinstance(me, dict) or not me.get("label") or key == owner:
                continue
            at = max(float(me.get("at") or 0), float(me.get("ev_at") or 0),
                     float(me.get("seen_at") or 0))
            if now - at > REGISTRY_RECENT_S:
                continue
            tp = str(me.get("tp") or "")
            if tp:
                seen_tp.add(tp)
            out.append({"key": key, "label": str(me["label"]), "agent": _agent_of(tp),
                        "tp": tp, "ev": str(me.get("ev") or ""),
                        "ev_text": str(me.get("ev_text") or ""),
                        "ev_at": float(me.get("ev_at") or 0)})
        for p, e in vmulti._helper_entries():   # (b) helper credential = in this call
            if vmulti._usable(e, p, now) is None:
                continue
            name = str(e.get("helper") or "")
            if not name or any(vmulti.helper_key(x["label"]) == name or x["key"] == e.get("owner_session")
                               for x in out):
                continue
            out.append({"key": "helper-" + name, "label": name, "agent": "claude", "tp": "",
                        "ev": "", "ev_text": "", "ev_at": 0.0})
        if not watch_all():
            return out[:MAX_SESSIONS]
        procs = _running_agents(ps)
        roots = ((claude_root or vrelayops._config_dir() / "projects", "*/*.jsonl", "claude"),
                 (codex_root or _codex_sessions(), "**/rollout-*.jsonl", "codex"))
        for root, pat, agent in roots:
            room = procs.get(agent, 0) - sum(1 for s in out if s["agent"] == agent)
            for _m, p in _transcripts(Path(root), pat, now):
                if room <= 0:
                    break
                if p in seen_tp:
                    continue
                stem = Path(p).stem
                skey = vmulti.session_hash(stem)
                if skey == owner or any(s["key"] == skey for s in out):
                    continue
                label = J.label_for(stem, p)
                if agent == "codex":
                    label = f"Codex {vmulti.label_suffix(stem)}"
                out.append({"key": skey, "label": label, "agent": agent, "tp": p, "ev": "",
                            "ev_text": "", "ev_at": 0.0})
                room -= 1
    except Exception as e:  # noqa: BLE001
        log("discover_error", err=type(e).__name__)
    return out[:MAX_SESSIONS]


def classify_state(s: dict, now: float) -> tuple[str, float]:
    """(state, last activity time) of one discovered session."""
    mt = _mtime(s.get("tp") or "")
    ev_at = float(s.get("ev_at") or 0)
    sub = J.sub_mtime(s.get("tp") or "")
    if sub > ev_at + SETTLE_S and 0 <= now - sub <= ACTIVE_S:   # 0.33.0: a subagent works for it
        return ARBEITET, max(mt, sub, ev_at)
    last = max(mt, ev_at)
    ev = s.get("ev")
    quiet = mt <= ev_at + SETTLE_S   # nothing written after the event
    if ev == "wait" and ev_at and quiet:
        return FRAGE, last
    if ev == "stop" and ev_at and quiet:
        return STILL, last
    if last and now - last <= ACTIVE_S:
        return ARBEITET, last
    return STILL, last


def last_assistant_text(path: str, limit: int = TEXT_CHARS) -> str:
    """Tail of the last assistant message of a Claude Code / Codex transcript, scrubbed.
    Stays in memory: handed to the model, never logged or posted."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - TAIL_BYTES))
            data = f.read().decode("utf-8", "replace")
    except OSError:
        return ""
    for line in reversed(data.splitlines()):
        if '"assistant"' not in line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if not isinstance(d, dict):
            continue
        pl = d.get("payload")
        msg = d.get("message") if d.get("type") == "assistant" else \
            (pl if isinstance(pl, dict) and pl.get("role") == "assistant" else None)
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, str):
            parts = [content]
        elif isinstance(content, list):
            parts = [c.get("text") for c in content
                     if isinstance(c, dict) and c.get("type") in ("text", "output_text")
                     and isinstance(c.get("text"), str)]
        else:
            continue
        text = " ".join(p for p in parts if p).strip()
        if text:
            return vact.scrub(text)[-limit:]
    return ""


# --------------------------------------------------------------------------- #
# wording
# --------------------------------------------------------------------------- #
def ago_text(secs: float) -> str:
    m = int(max(0.0, secs) // 60)
    if m < 1:
        return "wenigen Sekunden"
    if m < 60:
        return "einer Minute" if m == 1 else f"{m} Minuten"
    h = m // 60
    return "einer Stunde" if h == 1 else f"{h} Stunden"


def ago_short(secs: float) -> str:
    m = int(max(0.0, secs) // 60)
    if m < 1:
        return "unter 1 Min"
    if m < 60:
        return f"{m} Min"
    h = m // 60
    return f"{h} Std" if h < 48 else f"{h // 24} Tagen"


def inactive_sentence(own: str, last: float, now: float) -> str:
    """Fixed text (no model) of an inactive session: the real state, the time and the last
    real doing of the session itself. Unknown doing: only the time."""
    base = f"inaktiv seit {ago_short(now - last)}" if last else "inaktiv, letzte Aktivität unbekannt"
    return f"{base}, letzter Stand: {own}"[:OWN_MAX + 60] if own else base


def silent_sentence(doing: str, quiet_s: float) -> str:
    return f"keine Meldung seit {ago_short(quiet_s)}, letzter Stand: {doing}"[:OWN_MAX + 60]


def fixed_sentence(state: str, ago_s: float | None) -> str:
    """ago_s None: last activity unknown (a helper without transcript or hook events)."""
    if ago_s is None:
        return {ARBEITET: "Die Sitzung arbeitet gerade.",
                FRAGE: "Die Sitzung wartet auf eine Antwort."}.get(
            state, "Die Sitzung ist im Moment still, ihre letzte Aktivität ist unbekannt.")
    ago = ago_text(ago_s)
    if state == ARBEITET:
        return "Die Sitzung arbeitet gerade."
    if state == FRAGE:
        return f"Die Sitzung wartet auf eine Antwort, zuletzt aktiv vor {ago}."
    return f"Die Sitzung ist fertig und wartet auf den nächsten Auftrag, zuletzt aktiv vor {ago}."


def model_cmd(env=None) -> list[str] | None:
    env = os.environ if env is None else env
    raw = env.get(ENV_MODEL_CMD)
    if raw is not None:
        if raw.strip().lower() in ("", "off", "none", "0"):
            return None
        return shlex.split(raw)
    return list(DEFAULT_MODEL_CMD) if shutil.which("claude") else None


def _tokens(usage: object) -> int:
    """Budget tokens of a `claude -p` usage block: input + output + cache creation; a cache
    read counts a tenth (it is billed that way)."""
    if not isinstance(usage, dict):
        return 0

    def n(k: str) -> int:
        v = usage.get(k)
        return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0
    return (n("input_tokens") + n("output_tokens") + n("cache_creation_input_tokens")
            + n("cache_read_input_tokens") // 10)


def clean_sentence(text: object) -> str:
    s = vact.scrub(str(text or "")).strip().strip("\"'`„“”")
    if len(s) > SENTENCE_MAX:
        s = s[:SENTENCE_MAX].rsplit(" ", 1)[0].rstrip(",;:") + "."
    return s if len(s.split()) >= 3 else ""


def with_system(cmd: list[str], system: str) -> list[str]:
    """The model command with its --system-prompt replaced (the default command's one asks
    for a sentence); a custom command without that flag stays as it is."""
    if "--system-prompt" not in cmd:
        return list(cmd)
    i = cmd.index("--system-prompt")
    return [*cmd[:i + 1], system, *cmd[i + 2:]]


def ask_model(cmd: list[str], prompt: str, run=None) -> tuple[str, int]:
    """(sentence, tokens) from the model command; ('', tokens) on any failure."""
    text, toks = ask_raw(cmd, prompt, run)
    return clean_sentence(text), toks


def ask_raw(cmd: list[str], prompt: str, run=None) -> tuple[str, int]:
    """(text, tokens) from the model command (prompt on stdin; JSON like `claude -p
    --output-format json`: {result, usage}; plain text also works). ('', tokens) on any
    failure. Never raises."""
    env = {**os.environ, ENV_CHILD: "1", "VOICEHOOK_NO_RELAY": "1"}
    try:
        r = (run or subprocess.run)(cmd, input=prompt, capture_output=True, text=True, timeout=MODEL_TIMEOUT_S,
                env=env, cwd=vact.child_cwd(), check=False)
    except (OSError, subprocess.SubprocessError):
        return "", 0
    out = r.stdout or ""
    try:
        d = json.loads(out)
    except ValueError:
        d = None
    if isinstance(d, dict):
        toks = _tokens(d.get("usage"))
        if d.get("is_error") or r.returncode != 0:
            return "", toks
        return str(d.get("result") or ""), toks
    if r.returncode != 0:
        return "", 0
    return out, (len(prompt) + len(out)) // 4


def build_prompt(label: str, state: str, ago_s: float, text: str) -> str:
    return (f"Sitzung: {label}\nZustand: {STATE_TEXT[state]}\n"
            f"Zuletzt aktiv: vor {ago_text(ago_s)}\n"
            f"Letzte Nachricht der Sitzung (Ausschnitt):\n{text or '(keine)'}\n\n"
            "Schreibe jetzt den einen Satz.")


# --------------------------------------------------------------------------- #
# server + posting
# --------------------------------------------------------------------------- #
def server_interval(rel: dict) -> tuple[str, int | None]:
    """('ok', minutes|None) | ('old', None) 404 | ('over', None) 401/403/410. RelayError on
    a network failure."""
    if vmulti._is_local(rel):
        return "old", None
    status, out = vrelayops._http(rel, "GET", vrelayops.endpoint(rel, "watch"), None, 10.0)
    if status in OVER:
        return "over", None
    if status == 404:
        return "old", None
    if status != 200:
        raise vrelayops.RelayError(f"HTTP {status}", status)
    return "ok", parse_interval(out.get("interval_min"))


def post_board(rel: dict, label: str, body: dict) -> dict:
    if vmulti._is_local(rel):
        return vrelayops.local_request(rel, label, "status", body, wait=5.0)
    return vrelayops.remote_status(rel, label, body)


def get_status(rel: dict, label: str) -> tuple[int, dict]:
    """GET <relay>/status?from=<label> -> (http status, {"board", "state"}); 0.33.0 server
    read path (voicehook-v4 #737). A local relay has none: (404, {}). RelayError on network."""
    if vmulti._is_local(rel):
        return 404, {}
    url = f"{vrelayops.endpoint(rel, 'status')}?from={quote(label, safe='')}"
    return vrelayops._http(rel, "GET", url, None, 10.0)


def post_proposal(rel: dict, body: dict) -> dict:
    """The decision card 'Session X arbeitet gerade, soll sie ins Board?' (sender Wächter)."""
    return vrelayops.remote_decide(rel, J.NAME, body)


def poll_answers(rel: dict) -> list[dict]:
    """Answers to the proposals (non-blocking GET <relay>/next?from=Wächter)."""
    return vrelayops.remote_next(rel, J.NAME, 0)


def find_unbound(rel: dict, now: float, active_s: float, **kw) -> list[dict]:
    return J.find_unbound(rel, now, active_s, claude_root=vrelayops._config_dir() / "projects", **kw)


def post_activity(rel: dict, label: str, sentence: str) -> dict:
    if vmulti._is_local(rel):
        ok = vmulti.append_local(rel, f"{label}: {sentence}")
        return {"ok": ok, "status": 200 if ok else 503}
    return vrelayops.remote_activity(rel, label, {"text": sentence})


def delete_decision(rel: dict, label: str, dec_id: str) -> dict:
    body = {"id": dec_id, "delete": True}
    if vmulti._is_local(rel):
        return vrelayops.local_request(rel, label, "decide", body, wait=5.0)
    return vrelayops.remote_decide(rel, label, body)


# --------------------------------------------------------------------------- #
# the watcher
# --------------------------------------------------------------------------- #
class Watcher:
    def __init__(self, *, interval_flag: object = None, tokens: object = None,
                 clock=time.time, discover_fn=discover, cmd: list[str] | None | str = "env",
                 unbound_fn=None) -> None:
        self.interval_flag = interval_flag
        self.cap = tokens_per_hour(tokens)
        self.clock = clock
        self.discover = discover_fn
        self.unbound = unbound_fn or find_unbound
        self.cmd = model_cmd() if cmd == "env" else cmd
        self.spent: deque = deque()          # (time, tokens)
        self.mem: dict[str, dict] = {}       # session key -> {state, post_at, call_at, call_last}
        self.fails = 0
        self.over: list[float] = []          # times of 401/403/410 answers in a row
        self.not_found_until = 0.0
        self.server_min: int | None = None
        self.rel: dict | None = None
        self.kanban_off_until = 0.0          # old server (404 on /tasks): ask again later
        self.status_off_until = 0.0          # old server (404 on GET /status): ask again later
        self.un: dict[str, dict] = {}        # label -> {sig, since, post_at} of unobservable sessions

    def over_confirmed(self, now: float) -> bool:
        """One more 401/403/410 answer; True when the call is over for sure (OVER_CONFIRM in a
        row over at least OVER_CONFIRM_S)."""
        self.over.append(now)
        self.over = self.over[-OVER_CONFIRM:]
        return len(self.over) >= OVER_CONFIRM and now - self.over[0] >= OVER_CONFIRM_S

    def shutdown(self) -> None:
        """Exit: clear every watcher board this machine remembers (call end, SIGTERM)."""
        rel = credential(self.clock()) or self.rel
        if credential(self.clock()) is None:
            J.clear()    # the call is over: opt-ins and proposals end with it
        if rel is not None:
            self.duty_cleanup(rel, set())
        else:
            for sub in sub_boards():
                forget_board(sub)

    # ----- budget -----
    def used(self, now: float) -> int:
        while self.spent and now - self.spent[0][0] > BUDGET_WINDOW_S:
            self.spent.popleft()
        return sum(t for _, t in self.spent)

    def model_allowed(self, now: float) -> bool:
        return self.cmd is not None and self.cap > 0 and self.used(now) < self.cap

    def sentence(self, s: dict, state: str, last: float, now: float, m: dict,
                 interval_s: float) -> tuple[str, int, str]:
        """(sentence, tokens, how) how = model | fixed. One model call per session per
        interval, only when the session did something since the last call."""
        if state == STILL:   # 0.33.0: inactive = real state + time + last real doing, no model
            return inactive_sentence(own_doing(s["label"], now), last, now), 0, "fixed"
        fixed = fixed_sentence(state, now - last if last else None)
        due_call = now - m.get("call_at", 0.0) >= interval_s and last > m.get("call_last", 0.0)
        if not due_call or not self.model_allowed(now) or not s.get("tp"):
            same = m.get("sent") and m.get("sent_state") == state and last <= m.get("call_last", 0.0)
            return (m["sent"] if same else fixed), 0, "fixed"
        text = last_assistant_text(s["tp"])
        m["call_at"], m["call_last"] = now, last
        key = (state, text)
        if m.get("sent") and m.get("call_key") == key:   # the sentence would not change
            return m["sent"], 0, "fixed"
        m["call_key"] = key
        t0 = time.monotonic()
        sent, toks = ask_model(self.cmd, build_prompt(s["label"], state, now - last, text))
        if toks:
            self.spent.append((now, toks))
        log("model", label=s["label"], state=state, tokens=toks, ok=bool(sent),
            ms=int((time.monotonic() - t0) * 1000))
        if sent:
            m["sent"], m["sent_state"] = sent, state
        return (sent, toks, "model") if sent else (fixed, toks, "fixed")

    # ----- duties (one function each; a Kanban duty is added here later) -----
    def duty_status(self, rel: dict, label: str, sentence: str, state: str, last: float,
                    now: float, card: int | None = None) -> dict:
        """Post the sentence. A relay /status REPLACES the whole board of a sender, so:
        * the session's board is known locally (its own `status` posts of a CLI >= 0.32.0,
          or our earlier merge): merge into it, replace only `doing` + meta and `state`;
        * not known (older CLIs post their boards themselves): never touch the main board,
          post to the separate watcher board (sender `<label> Wächter`) instead."""
        meta = doing_meta(label, state, last, now, card)
        base = known_board(label, self.clock())
        if base is None:
            sub = sub_label(label)
            body = merge_board(None, sentence, meta)   # never `state` here: it cannot be removed
            res = post_board(rel, sub, body)
            if res.get("ok"):
                remember_board(sub, body, now)
            return res
        body = merge_board(base, sentence, meta, state)
        res = post_board(rel, label, body)
        if res.get("ok"):
            remember_board(label, body, now)
            sub = sub_label(label)
            if known_board(sub, self.clock()) is not None:   # the session reports itself now
                post_board(rel, sub, {})
                forget_board(sub)
        return res

    def classify_order(self, order: str, cards: list[dict], now: float) -> tuple[str, int | None, str]:
        """(action, nr, title) for one order: the cheapest model within the token cap, else
        `new` with the first sentence as title."""
        fallback = ("new", None, K.fallback_title(order))
        if not self.model_allowed(now):
            return fallback
        text, toks = ask_raw(with_system(self.cmd, K.CLASSIFY_SYSTEM), K.build_classify_prompt(order, cards))
        if toks:
            self.spent.append((now, toks))
        got = K.parse_classification(text, {c["nr"] for c in K.open_cards(cards)})
        log("classify", tokens=toks, ok=bool(got))
        if not got:
            return fallback
        return got[0], got[1], got[2] or fallback[2]

    def _kanban_status(self, st: int, now: float, label: str) -> bool:
        """True = stop for this tick (route missing, credential refused or server error)."""
        if st == 404:
            self.kanban_off_until = now + NOT_FOUND_RECHECK_S
            log("kanban", label=label, why="no-route")
            return True
        if st in OVER or st >= 500:
            log("error", where="kanban", label=label, status=st)
            return True
        return False

    def duty_kanban(self, rel: dict, s: dict, label: str, state: str, m: dict, now: float) -> bool:
        """Maintain the owner's Kanban for one observable session (see watchkanban.py).
        True when the card the session works on changed (the board post is then due).
        Never raises; logs labels, card numbers, counts only."""
        if (now < self.kanban_off_until or vmulti._is_local(rel) or s.get("agent") == "codex"
                or not s.get("tp")):
            return False
        key = s["key"]
        cur = K.load_cursor(key)
        first = cur is None or cur.get("tp") != s["tp"]
        if first:
            try:
                start = max(0, os.stat(s["tp"]).st_size - K.READ_MAX)
            except OSError:
                return False
            cur = {"tp": s["tp"], "off": start, "since": now - K.BACKFILL_S, "card": None,
                   "linked": [], "tries": {}}
        else:
            cur = dict(cur)
        card0 = cur.get("card")
        try:
            orders, new_off, pending = K.read_orders(s["tp"], int(cur.get("off") or 0),
                                                     float(cur.get("since") or 0), first)
            if first or new_off != cur.get("off"):
                cur["off"] = new_off if not orders else cur["off"]
                K.save_cursor(key, cur, now)
            cards: list[dict] | None = None
            done = 0
            if orders:
                st, cards = K.get_cards(rel, label)
                if self._kanban_status(st, now, label):
                    return False
                for o in orders:
                    wording, cut = K.fit_wording(o["text"])
                    if cut:
                        log("kanban", label=label, why="wording-cut")
                    action, nr, title = self.classify_order(wording, cards, now)
                    nr_out, st = None, 0
                    if action == "sub" and nr:
                        st, out = K.call(rel, "POST", f"tasks/{nr}/points",
                                         {"from": label, "text": title[:200], "wording": wording})
                        if st == 404:
                            action = "new"       # the card is gone: a new one
                        elif st < 300:
                            nr_out = nr
                            for c in cards:
                                if c.get("nr") == nr:
                                    c["points"] = [*(c["points"] if isinstance(c.get("points"), list) else []), title]
                    if action == "new":
                        body = {"from": label, "title": title, "wording": wording, "assignee": label,
                                "state": "in_arbeit" if state == ARBEITET else "beauftragt"}
                        st, out = K.call(rel, "POST", "tasks", body)
                        if st < 300:
                            nr_out = K.card_nr(out)
                            if nr_out:
                                cards.append({"nr": nr_out, "title": title, "state": body["state"]})
                                m["card_state"] = body["state"]
                    if st >= 300 and st not in (400, 409, 422):
                        self._kanban_status(st, now, label)
                        break
                    if st >= 300:
                        log("kanban", label=label, why="refused", status=st)
                    done += 1
                    cur["off"] = o["end"]
                    if nr_out:
                        cur["card"] = nr_out
                        if action == "sub":
                            m["card_state"] = None
                    K.save_cursor(key, cur, now)
                if done == len(orders):   # all consumed: skip over trailing non-order lines
                    cur["off"] = max(cur["off"], new_off) if not pending else cur["off"]
                    K.save_cursor(key, cur, now)
                log("kanban", label=label, orders=len(orders), done=done, pending=pending,
                    card=cur.get("card") or 0)
            card = cur.get("card")
            if card and state == ARBEITET and m.get("card_state") != "in_arbeit":
                self._kanban_pick(rel, label, card, cards, cur, m, now)
            if card:
                self._kanban_links(rel, key, label, card, cur, now)
        except vrelayops.RelayError:
            log("error", where="kanban", label=label, status=503)
        except Exception as e:  # noqa: BLE001
            log("error", where="kanban", label=label, type=type(e).__name__)
        m["card"] = cur.get("card")
        return cur.get("card") != card0

    def _kanban_pick(self, rel: dict, label: str, card: int, cards: list[dict] | None,
                     cur: dict, m: dict, now: float) -> None:
        """The card the session works on goes to in_arbeit (never done: that needs proof)."""
        if cards is None:
            st, cards = K.get_cards(rel, label)
            if st >= 300:
                return
        c = next((x for x in cards if x.get("nr") == card), None)
        if c is None or not K.is_open(c):
            cur["card"] = None
            m["card_state"] = None
            return
        if str(c.get("state") or "") == "in_arbeit":
            m["card_state"] = "in_arbeit"
            return
        st, _out = K.call(rel, "POST", f"tasks/{card}/pick", {"from": label})
        log("kanban", label=label, card=card, pick=st)
        m["card_state"] = "in_arbeit" if st < 300 else "other"

    def _kanban_links(self, rel: dict, skey: str, label: str, card: int, cur: dict, now: float) -> None:
        """Link the open decisions this session created (notify-state) to its card."""
        linked, tries = list(cur.get("linked") or []), dict(cur.get("tries") or {})
        for slot_key, slot in notify.relay_slots():
            if slot_key != skey and str(slot.get("from") or "") != label:
                continue
            for dec in dict.fromkeys([slot.get("id"), *(slot.get("ids") or [])]):
                if not isinstance(dec, str) or not dec or dec in linked:
                    continue
                st, _o = K.call(rel, "POST", f"tasks/{card}/link", {"from": label, "decision": dec})
                if st < 300 or st == 409:
                    linked.append(dec)
                elif st == 404:
                    tries[dec] = int(tries.get(dec) or 0) + 1
                    if tries[dec] >= K.LINK_TRIES:
                        linked.append(dec)
                log("kanban", label=label, card=card, link=st)
        cur["linked"], cur["tries"] = linked[-50:], {k: v for k, v in tries.items() if k not in linked}
        K.save_cursor(skey, cur, now)

    def duty_join(self, rel: dict, sessions: list[dict], now: float) -> None:
        """Active sessions that are not part of the call: propose each ONCE as a decision
        card (sender Wächter, label + host only, max J.MAX_OPEN open), apply the answers
        (Ja = covered like a call session from the next tick, Nein = never again in this
        call). Subagents count for their parent. Never raises."""
        if vmulti._is_local(rel) or watch_all():
            return
        try:
            st = J.load(rel)
            prop = st["proposed"]
            try:
                evs = poll_answers(rel)
            except vrelayops.RelayError:
                evs = []
            by_id = {e.get("id"): k for k, e in prop.items() if e.get("id") and not e.get("answer")}
            for ev in evs:
                a = J.answer_of(ev)
                if not a or a[0] not in by_id:
                    continue
                key = by_id[a[0]]
                prop[key]["answer"] = a[1]
                if a[1] == "ja":
                    st["subscribed"][key] = {"label": prop[key]["label"], "tp": prop[key]["tp"], "at": now}
                log("join", label=prop[key]["label"], answer=a[1])
            cands = self.unbound(rel, now, ACTIVE_S, bound_keys={x["key"] for x in sessions},
                                 bound_tps={x["tp"] for x in sessions if x.get("tp")})
            is_open = lambda e: not e.get("answer") and now - float(e.get("at") or 0) < J.TTL_S
            n_open = sum(1 for e in prop.values() if is_open(e))
            host = vmulti.machine_label()
            for c in cands:
                e = prop.get(c["key"])
                if e is not None:
                    gap = now - float(e.get("last_active") or 0)
                    e["last_active"] = now
                    if e.get("answer") or gap <= J.REPROPOSE_S:
                        continue    # answered, or asked before and not inactive since
                    if is_open(e):
                        n_open -= 1
                if J.is_never(c["label"], c["tp"]) or n_open >= J.MAX_OPEN:
                    continue        # over the cap: stays queued, asked when a card is answered
                try:
                    label = vrelayops.check_from(c["label"])
                except vrelayops.RelayError:
                    continue
                res = post_proposal(rel, J.proposal_body(label, host, c["active_at"], now))
                if not res.get("ok") or not res.get("id"):
                    log("join", label=label, why="post-failed", status=res.get("status"))
                    continue
                prop[c["key"]] = {"id": res["id"], "at": now, "label": label, "tp": c["tp"],
                                  "last_active": now, "answer": None}
                n_open += 1
                log("join", label=label, why="proposed")
            J.save(st)
        except Exception as e:  # noqa: BLE001
            log("error", where="join", type=type(e).__name__)

    def duty_unobserved(self, rel: dict, label: str, now: float, interval_s: float) -> None:
        """A session the watcher cannot see (no transcript, no hook events): read its board
        from the server and track locally when it last changed (the server sends no time).
        Unchanged for longer than the interval: `keine Meldung seit X, letzter Stand: ...` on
        the separate watcher board (never the main board, never `state`); cleared when the
        session reports again. A server without the route (404): skipped silently."""
        if now < self.status_off_until:
            return
        try:
            st, out = get_status(rel, label)
        except vrelayops.RelayError:
            return
        if st == 404:
            self.status_off_until = now + NOT_FOUND_RECHECK_S
            return
        if st != 200:
            return
        board = out.get("board")
        doing = str(board.get("doing") or "").strip() if isinstance(board, dict) else ""
        sub = sub_label(label)
        m = self.un.setdefault(label, {})
        if not doing:
            return
        sig = json.dumps(board, sort_keys=True, ensure_ascii=False)
        if m.get("sig") != sig:
            m.update(sig=sig, since=now, post_at=0.0)
            if known_board(sub, self.clock()) is not None:   # it reports again
                post_board(rel, sub, {})
                forget_board(sub)
                log("cleared", label=label, why="reports-again")
            return
        quiet = now - float(m.get("since") or now)
        if quiet <= interval_s or now - float(m.get("post_at") or 0) < interval_s:
            return
        ago = ago_short(quiet)
        body = {"doing": silent_sentence(doing, quiet), "meta": {"doing": {
            "context": (f"Sitzung {label} meldet sich nicht und ist für den Beobachter nicht sichtbar. "
                        f"Stand ist die letzte Meldung der Sitzung, unverändert seit {ago}.")[:2000],
            "faq": [{"q": f"Warum steht bei {label} nichts Neues?",
                     "a": f"Die Sitzung hat sich seit {ago} nicht gemeldet."},
                    {"q": f"Ist {label} noch aktiv?",
                     "a": "Unbekannt, der Beobachter kann die Sitzung nicht sehen."},
                    {"q": "Wessen Stand ist das?",
                     "a": "Der letzte Stand, den die Sitzung selbst gemeldet hat."}]}}}
        res = post_board(rel, sub, body)
        if res.get("ok"):
            remember_board(sub, body, now)
            m["post_at"] = now
        log("silent", label=label, quiet_min=int(quiet // 60), ok=bool(res.get("ok")))

    def duty_cleanup(self, rel: dict, live: set[str]) -> None:
        """Clear the watcher boards of sessions that are gone / no longer observable
        (empty board, then forget). `live` = labels still watched; None-safe on errors."""
        for sub in sub_boards():
            if sub[:-len(f" ({SUB_NAME})")] in live:
                continue
            try:
                post_board(rel, sub, {})
            except Exception:  # noqa: BLE001, S110
                pass
            forget_board(sub)
            log("cleared", sub="board")

    def duty_activity(self, rel: dict, label: str, sentence: str) -> dict:
        return post_activity(rel, label, sentence)

    def duty_decisions(self, rel: dict, views: dict[str, tuple[str, float]], now: float) -> str:
        """Remove decisions of local sessions that went on / are obsolete. OK or END."""
        for skey, slot in notify.relay_slots():
            if (views.get(skey) or ("", 0.0))[0] == FRAGE:
                continue            # still waiting: the question is open, keep it
            at = float(slot.get("at") or 0)
            view = views.get(skey)
            obsolete = view[1] > at + SETTLE_S if view else now - at > notify.OPEN_KEEP_S
            if not obsolete:
                continue
            label = str(slot.get("from") or "")
            if not label:
                continue
            use = vmulti.load_relay(now, session=notify._base(skey)) or rel
            ids = [i for i in dict.fromkeys([slot.get("id"), *(slot.get("ids") or [])])
                   if isinstance(i, str) and i]
            done = True
            for dec_id in ids:
                res = delete_decision(use, label, dec_id)
                st = res.get("status")
                if st in OVER:   # that session's credential, not the call: try again later
                    log("error", where="decide-over", label=label)
                    done = False
                    break
                if not (res.get("ok") or st in GONE):
                    done = False
            if done:
                notify.drop_slot(skey, str(slot.get("id") or ""))
            log("decision", label=label, removed=len(ids), done=done)
        return OK

    def tick(self) -> str:
        """One pass. END (call over), else OK. Errors raise nothing: they count in self.fails."""
        now = self.clock()
        rel = credential(now)
        if rel is None or now >= float(rel.get("expires_at") or 0):
            log("end", why="no-call")
            return END
        if now >= self.not_found_until:
            try:
                verdict, minutes = server_interval(rel)
            except vrelayops.RelayError:
                self.fails += 1
                log("error", where="watch-get", fails=self.fails)
                return OK
            if verdict == "over":
                if self.over_confirmed(now):
                    log("end", why="server-over")
                    return END
                log("error", where="watch-over", count=len(self.over))
                return OK
            self.over = []
            if verdict == "old":
                self.server_min = None
                self.not_found_until = now + NOT_FOUND_RECHECK_S
            else:
                self.server_min = minutes
        minutes, src = resolve_interval(self.server_min, self.interval_flag,
                                        os.environ.get(ENV_INTERVAL))
        interval_s = minutes * 60.0
        bad = False
        views: dict[str, tuple[str, float]] = {}
        sessions = list(self.discover(rel, now))
        taken = {x["key"] for x in sessions}
        taken_tp = {x["tp"] for x in sessions if x.get("tp")}
        sessions += [x for x in J.subscribed_sessions(rel, taken) if x["tp"] not in taken_tp]
        live: set[str] = set()
        for s in sessions:
            if not observable(s):
                # never claim a state that cannot be seen; the server board is tracked instead
                try:
                    ulabel = vrelayops.check_from(s["label"])
                except vrelayops.RelayError:
                    continue
                live.add(ulabel)
                self.duty_unobserved(rel, ulabel, now, interval_s)
                continue
            state, last = classify_state(s, now)
            views[s["key"]] = (state, last)
            try:
                label = vrelayops.check_from(s["label"])
            except vrelayops.RelayError:
                continue
            live.add(label)
            m = self.mem.setdefault(s["key"], {})
            prev = m.get("state")
            changed = prev is not None and prev != state
            m["state"] = state
            card_changed = self.duty_kanban(rel, s, label, state, m, now)
            if card_changed:
                m["post_at"] = 0.0
            if not (prev is None or changed or card_changed or now - m.get("post_at", 0.0) >= interval_s):
                continue
            if now - self_posted_at(label) < interval_s:
                log("skip", label=label, why="self-reporting")
                m["post_at"] = now
                continue
            sent, toks, how = self.sentence(s, state, last, now, m, interval_s)
            res = self.duty_status(rel, label, sent, state, last, now, m.get('card'))
            if res.get("status") in OVER:
                if self.over_confirmed(now):
                    log("end", why="post-over")
                    return END
                log("error", where="post-over", count=len(self.over))
                return OK
            bad = bad or not res.get("ok")
            if changed:
                bad = bad or not self.duty_activity(rel, label, sent[:SENTENCE_MAX]).get("ok")
            m["post_at"] = now
            log("post", label=label, state=state, how=how, tokens=toks, changed=changed,
                ok=bool(res.get("ok")))
        self.rel = rel
        self.duty_join(rel, sessions, now)
        self.duty_cleanup(rel, live)
        if self.duty_decisions(rel, views, now) == END:
            return END
        self.fails = self.fails + 1 if bad else 0
        log("tick", sessions=len(sessions), interval=minutes, src=src, used=self.used(now),
            cap=self.cap, fails=self.fails)
        return OK

    def delay(self) -> float:
        if self.fails <= 0:
            return TICK_S
        return min(BACKOFF_MAX_S, TICK_S * (2 ** min(self.fails, 8)))

    def run(self, sleep=time.sleep) -> int:
        log("start", pid=os.getpid(), cap=self.cap, model=bool(self.cmd))
        try:
            while True:
                try:
                    if self.tick() == END:
                        return 0
                except Exception as e:  # noqa: BLE001  never die on a bug, back off
                    self.fails += 1
                    log("error", where="tick", type=type(e).__name__, fails=self.fails)
                sleep(self.delay())
        finally:
            try:
                self.shutdown()
            except Exception:  # noqa: BLE001, S110
                pass


def main(interval: object = None, tokens: object = None, foreground: bool = False) -> int:
    """`voicehook-agent watch`. Without --foreground: start the detached watcher (quietly
    nothing when one runs already or no call is active), exit 0. With: run here."""
    if not foreground:
        extra = (["--interval", str(interval)] if interval else []) + \
                (["--tokens-per-hour", str(tokens)] if tokens is not None else [])
        os.environ.pop(ENV_OFF, None)   # by hand always tries
        spawn_detached(extra)
        return 0
    if credential() is None:
        return 0
    fd = acquire_lock()
    if fd is None:
        return 0
    try:
        import signal
        signal.signal(signal.SIGTERM, lambda *_a: sys.exit(0))   # finally: clears the boards
    except (ImportError, ValueError, OSError):
        pass
    try:
        return Watcher(interval_flag=interval, tokens=tokens).run()
    finally:
        os.close(fd)
