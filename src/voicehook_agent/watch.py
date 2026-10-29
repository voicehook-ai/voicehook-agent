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
from . import sessionnames as vnames
from . import watchjoin as J
from . import watchkanban as K
from . import watchsessions as WS

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
NAMES_PER_TICK = 3                # 0.35.0: model calls for names / topics per tick (token cap applies too)

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
def machine_root() -> Path:
    """0.33.4: ONE watcher per machine, whatever VOICEHOOK_STATE_DIR a session uses (live:
    a session with its own state dir started a second watcher and a second summary card)."""
    env = (os.environ.get(MACHINE_ENV) or "").strip()
    return Path(env) if env else Path.home() / ".voicehook"


MACHINE_ENV = "VOICEHOOK_MACHINE_DIR"   # tests only: where the machine-wide lock lives


def acquire_lock():
    """Open and flock ~/.voicehook/watch.lock (kept open for the process lifetime). The fd,
    or None when another watcher holds it."""
    try:
        import fcntl
    except ImportError:  # pragma: no cover  (Windows)
        return None
    vact._private_dir(machine_root())
    fd = os.open(machine_root() / LOCK_NAME, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
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
        env = {k: v for k, v in os.environ.items() if k != vact.STATE_ENV}   # machine state
        from . import device as vdev
        if vdev.caretaker_active():
            return False    # 0.34.0: the connected caretaker daemon covers the call
        popen(child_argv(extra), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
              stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True,
              cwd=vact.child_cwd(), env=env)
        return True
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------- #
# credential: is a call active?
# --------------------------------------------------------------------------- #
def usable_credentials(now: float | None = None) -> list[dict]:
    """All usable credentials on this machine (relay.json of a join, helper entries)."""
    t = time.time() if now is None else now
    out = []
    rel = vmulti.load_main_relay(t)
    if rel is not None:
        out.append(rel)
    for p, e in vmulti._helper_entries():
        u = vmulti._usable(e, p, t)
        if u is not None:
            out.append(u)
    return out


def credential(now: float | None = None) -> dict | None:
    """The credential of the NEWEST call on this machine: among the usable relay.json of a
    join and the usable helper entries (helper-import), the most recently written one.
    0.33.3: before, the first helper entry by name won, so after a new call the watcher kept
    working for the old one (live 28.10.: Kunde-Talents of the morning call). None = no call
    (gone, expired, marked dead, owner gone)."""
    t = time.time() if now is None else now
    cands = []
    rel = vmulti.load_main_relay(t)
    if rel is not None:
        cands.append(rel)
    for p, e in vmulti._helper_entries():
        u = vmulti._usable(e, p, t)
        if u is not None:
            cands.append(u)
    if not cands:
        return None
    return max(cands, key=lambda d: (float(d.get("written_at") or 0), str(d.get("helper") or "")))


def call_key(rel: dict | None) -> str:
    """Which call a credential belongs to (hash of its token, never the token)."""
    return J.call_hash(rel) if rel else ""


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


def forget_boards_before(t: float) -> None:
    """0.33.3: a new call started at `t` (its credential was written then): boards remembered
    before belong to the old call and must not be merged into the new one."""
    def step(d: dict) -> None:
        for k in [k for k, v in d.items() if not isinstance(v, dict) or float(v.get("at") or 0) < t]:
            d.pop(k, None)
    try:
        _boards_rw(step)
    except OSError:
        pass


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
    """Senders (`<label> (Session Watcher)`, before 0.35.0 `<label> (Wächter)`) of the
    watcher boards this machine remembers."""
    try:
        d = json.loads((vact.state_root() / BOARDS_NAME).read_text(encoding="utf-8"))
        return [k for k in d if isinstance(k, str) and sub_base(k) is not None] if isinstance(d, dict) else []
    except (OSError, ValueError):
        return []


def sub_base(sub: str) -> str | None:
    """The session label of a watcher board sender, None when it is none."""
    for name in (SUB_NAME, *LEGACY_SUB_NAMES):
        if sub.endswith(f" ({name})"):
            return sub[:-len(f" ({name})")]
    return None


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
    ctx = f"Session {label}: {STATE_TEXT[state]}, zuletzt aktiv vor {ago}. Stand vom Session Watcher, nicht von der Session selbst."
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
SUB_NAME = "Session Watcher"           # 0.35.0 (Oliver 29.10.), before "Wächter"
LEGACY_SUB_NAMES = ("Wächter",)        # remembered boards of older versions are still cleared


def sub_label(label: str) -> str:
    """Sender of the separate watcher board of a session (max 40 chars)."""
    return f"{_cut(label, 40 - len(SUB_NAME) - 3)} ({SUB_NAME})"


def _cut(label: str, n: int) -> str:
    """At most n chars, cut at a word when there is one."""
    label = " ".join(str(label).split())
    if len(label) <= n:
        return label
    cut = label[:n + 1].rsplit(" ", 1)[0]
    return (cut if 0 < len(cut) <= n else label[:n]).rstrip(" .,;-")


FETCH_NAME = "Session Watcher"   # 0.35.0, before "Hausmeister"
FETCH_VERIFY_S = 150.0   # a Ja without the session on the board after this long is reported


def fetch_label(label: str) -> str:
    """Sender of the note "Konnte X nicht in den Call holen, weil ..." (max 40 chars)."""
    return f"{_cut(label, 40 - len(FETCH_NAME) - 3)} ({FETCH_NAME})"


def vdev_reason(status: int) -> str:
    from . import device as vdev

    return vdev.reason_text(status)


def fetch_note(label: str, why: str, now: float, since: float) -> dict:
    """Board body of the failure note (own sender, so it never overwrites a session board)."""
    text = f"Konnte {label} nicht in den Call holen, weil {why}."
    ago = ago_text(max(0.0, now - since))
    return {"doing": text[:SENTENCE_MAX * 2], "meta": {"doing": {
        "context": (f"Du hast Ja gesagt, {label} in den Call zu holen (vor {ago}). Der Session Watcher "
                    f"hat es versucht und es hat nicht geklappt: {why}. Er versucht es weiter; "
                    "sobald die Session im Call ist, verschwindet dieser Hinweis.")[:2000],
        "faq": [{"q": f"Warum ist {label} nicht im Call?"[:200], "a": f"Weil {why}."[:200]},
                {"q": "Muss ich etwas tun?",
                 "a": ("Ja, den Rechner neu verbinden: voicehook-agent connect." if "connect" in why
                       else "Nein, der Session Watcher versucht es von selbst weiter.")},
                {"q": "Wann verschwindet der Hinweis?",
                 "a": "Sobald die Session im Call auftaucht."}]}}}


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
            label = str(me["label"])
            if tp and (label == "Session" or label.endswith(" " + vmulti.label_suffix(Path(tp).stem))):
                label = J.label_for(Path(tp).stem, tp)   # 0.35.0: an old `<folder> <4 hex>` label
            out.append({"key": key, "label": label, "agent": _agent_of(tp),
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
            return _visible(out)[:MAX_SESSIONS]
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
                if agent == "codex":   # 0.35.0: never the hex suffix, the folder (unique) instead
                    label = vnames.folder_name(skey, "Codex", now)
                out.append({"key": skey, "label": label, "agent": agent, "tp": p, "ev": "",
                            "ev_text": "", "ev_at": 0.0})
                room -= 1
    except Exception as e:  # noqa: BLE001
        log("discover_error", err=type(e).__name__)
    return _visible(out)[:MAX_SESSIONS]


def _visible(sessions: list[dict]) -> list[dict]:
    """0.33.6: sessions whose label or project folder matches VOICEHOOK_WATCH_NEVER are not
    shown at all (router decision 28.10.: hide the se-testwall test runs, private sessions),
    not only never proposed."""
    return [s for s in sessions if not J.is_never(str(s.get("label") or ""), str(s.get("tp") or ""))]


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


def inactive_sentence(own: str, last: float, now: float, topic: str = "") -> str:
    """Fixed text (no model) of an inactive session: the real state, the time and the last
    real doing of the session itself. Unknown doing: the topic (0.35.0), else only the time."""
    base = f"inaktiv seit {ago_short(now - last)}" if last else "inaktiv, letzte Aktivität unbekannt"
    if own:
        return f"{base}, letzter Stand: {own}"[:OWN_MAX + 60]
    return f"{base}, Thema: {topic}"[:OWN_MAX + 60] if topic else base


def silent_sentence(doing: str, quiet_s: float) -> str:
    return f"keine Meldung seit {ago_short(quiet_s)}, letzter Stand: {doing}"[:OWN_MAX + 60]


def fixed_sentence(state: str, ago_s: float | None, topic: str = "") -> str:
    """ago_s None: last activity unknown (a helper without transcript or hook events).
    0.35.0: with a known topic the sentence names it in plain text."""
    if topic:
        if state == ARBEITET:
            return f"Arbeitet gerade am Thema {topic}."
        if state == FRAGE:
            tail = f", zuletzt aktiv vor {ago_text(ago_s)}" if ago_s is not None else ""
            return f"Wartet beim Thema {topic} auf eine Antwort{tail}."
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


def build_prompt(label: str, state: str, ago_s: float, text: str, topic: str = "") -> str:
    return (f"Sitzung: {label}\n" + (f"Thema: {topic}\n" if topic else "") +
            f"Zustand: {STATE_TEXT[state]}\n"
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
    _LANG[0] = "en" if str(out.get("lang") or "").lower() == "en" else "de"
    return "ok", parse_interval(out.get("interval_min"))


_LANG = ["de"]   # language of the call from the last GET relay/watch (0.35.0)


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
    """The decision card 'Soll <Name> mit Thema <Thema> in den Call?' (sender Session Watcher)."""
    return vrelayops.remote_decide(rel, J.NAME, body)


def poll_answers(rel: dict) -> list[dict]:
    """Answers to the proposals (non-blocking GET <relay>/next?from=Session Watcher)."""
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
                 unbound_fn=None, cred_fn=None, bind_fn=None, names: bool = True,
                 related_fn=None) -> None:
        # 0.34.0: cred_fn injects the credential (the caretaker daemon binds a watcher to the
        # caretaker credential of ONE call); bind_fn(rel, items) writes helper credentials for
        # subscribed sessions. Both None = the classic watcher (credential() of this machine).
        self.cred_fn = cred_fn
        self.bind_fn = bind_fn
        self.names = names                   # 0.35.0: derive names / topics with the model
        self.related = related_fn or J.related
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
        self.greeted = False                 # 0.33.3: one summary card per call
        self.pinned = ""                     # 0.33.5: hash of the credential in use
        self.call_id = ""                    # stable id of the current call (state key)
        self.dead: set[str] = set()          # credentials whose call is over
        self.not_found_until = 0.0
        self.server_min: int | None = None
        self.rel: dict | None = None
        self.kanban_off_until = 0.0          # old server (404 on /tasks): ask again later
        self.status_off_until = 0.0          # old server (404 on GET /status): ask again later
        self.un: dict[str, dict] = {}        # label -> {sig, since, post_at} of unobservable sessions
        # Bug 28.10.: a Ja fetched nobody, silently. label -> {since, bind, post, why, told}:
        # until the session has its credential AND its board is on the server, then gone
        self.fetch: dict[str, dict] = {}
        self.lang = "de"                     # 0.35.0: call language (GET relay/watch "lang")
        self.lister = WS.Lister()            # 0.35.0: session list on the call page
        self.proj: dict[str, str] = {}       # transcript -> project (cache)
        self.kanban_off: dict[str, float] = {}   # label -> rest until (403 not_owner_agent)
        self.kanban_told: set[str] = set()       # labels told about the 403 in this call

    def cred(self, now: float | None = None) -> dict | None:
        """The credential of the call this watcher serves (injected, else credential())."""
        t = self.clock() if now is None else now
        return self.cred_fn(t) if self.cred_fn is not None else credential(t)

    def over_confirmed(self, now: float) -> bool:
        """One more 401/403/410 answer; True when the call is over for sure (OVER_CONFIRM in a
        row over at least OVER_CONFIRM_S)."""
        self.over.append(now)
        self.over = self.over[-OVER_CONFIRM:]
        return len(self.over) >= OVER_CONFIRM and now - self.over[0] >= OVER_CONFIRM_S

    def pick(self, now: float, new_call: bool = False) -> dict | None:
        """0.33.5: the credential of the current call, kept FIXED: proposals are posted and
        their answers fetched with the same credential (live: the newest-credential rule
        switched between helper tokens of the same call, the answers to the summary card were
        fetched under another sender and lost). Another credential is taken only when the
        pinned one is gone (re-import: same call, state kept) or its call is over
        (`new_call`: state starts fresh). 0.34.0: an injected credential (caretaker) is the call."""
        if self.cred_fn is not None:
            return self.cred_fn(now)
        cands = [c for c in usable_credentials(now)
                 if vmulti._token_hash(str(c.get("token") or "")) not in self.dead]
        if not cands:
            return None
        pin = next((c for c in cands if self.pinned and
                    vmulti._token_hash(str(c.get("token") or "")) == self.pinned), None)
        if pin is None or new_call:
            newest = max(cands, key=lambda d: (float(d.get("written_at") or 0), str(d.get("helper") or "")))
            if new_call or not self.call_id:
                if self.call_id:
                    self.switch_call(now)
                self.call_id = "c" + vmulti._token_hash(str(newest.get("token") or ""))
                log("call", why="new" if new_call else "start")
            elif self.pinned:
                log("call", why="repin")
            self.pinned = vmulti._token_hash(str(newest.get("token") or ""))
            pin = newest
        return {**pin, "call_id": self.call_id}

    def switch_call(self, now: float) -> None:
        """A newer call's credential appeared (new helper-import / join): clear the watcher
        boards of the old call and start fresh for the new one (proposals, interval, counters)."""
        log("switch", why="newer-call")
        try:
            if self.rel is not None:
                self.duty_cleanup(self.rel, set())   # the old call's watcher boards
        except Exception:  # noqa: BLE001, S110
            pass
        forget_boards_before(float((self.cred(now) or {}).get("written_at") or now))
        self.mem = {}
        self.over = []
        self.server_min = None
        self.not_found_until = 0.0
        self.greeted = False

    def shutdown(self) -> None:
        """Exit: clear every watcher board this machine remembers (call end, SIGTERM)."""
        cur = self.cred(self.clock())
        rel = cur or self.rel
        if cur is None:
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
        topic = vnames.topic(s["key"])
        if state == STILL:   # 0.33.0: inactive = real state + time + last real doing, no model
            return inactive_sentence(own_doing(s["label"], now), last, now, topic), 0, "fixed"
        fixed = fixed_sentence(state, now - last if last else None, topic)
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
        sent, toks = ask_model(self.cmd, build_prompt(s["label"], state, now - last, text, topic))
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
          post to the separate watcher board (sender `<label> (Session Watcher)`) instead."""
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
        if st == 403:
            self.kanban_refused(label, now)
            return True
        if st in OVER or st >= 500:
            log("error", where="kanban", label=label, status=st)
            return True
        return False

    def kanban_refused(self, label: str, now: float) -> None:
        """0.35.0 (Oliver 29.10.): a 403 (not_owner_agent: the credential is no owner agent)
        is said ONCE per session and call as an activity line, and the Kanban of that session
        rests for NOT_FOUND_RECHECK_S. The fix is owner delegation, never a key: a connected
        machine (`voicehook-agent connect`) gets the Session Watcher credential with owner
        rights from the server."""
        self.kanban_off[label] = now + NOT_FOUND_RECHECK_S
        if label in self.kanban_told:
            return
        self.kanban_told.add(label)
        text = (f"Kanban für {label} nicht gepflegt, weil der Zugang keinen Agenten-Schlüssel hat. "
                "Abhilfe: den Rechner einmal verbinden mit voicehook-agent connect.")
        rel = self.rel
        if rel is not None:
            try:
                post_activity(rel, sub_label(label), text[:SENTENCE_MAX * 2])
            except Exception:  # noqa: BLE001, S110
                pass
        log("kanban", label=label, why="not-owner", told=1)

    def duty_kanban(self, rel: dict, s: dict, label: str, state: str, m: dict, now: float) -> bool:
        """Maintain the owner's Kanban for one observable session (see watchkanban.py).
        True when the card the session works on changed (the board post is then due).
        Never raises; logs labels, card numbers, counts only."""
        if (now < self.kanban_off_until or vmulti._is_local(rel) or s.get("agent") == "codex"
                or not s.get("tp") or s.get("parent") or now < self.kanban_off.get(label, 0.0)):
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
        if st == 403:
            self.kanban_refused(label, now)
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

    # ----- 0.35.0: names, topics, session list -----
    def project(self, tp: str) -> str:
        if tp not in self.proj:
            self.proj[tp] = vnames.project_of(vnames.latest_cwd(tp)) if tp else ""
        return self.proj[tp]

    def derive_names(self, sessions: list[dict], now: float) -> None:
        """Name + topic from the session's own orders (cheapest model, within the token cap):
        once per new latest order, at most NAMES_PER_TICK model calls a tick. The order text
        stays in memory (model input), only the short name / topic is stored. Never raises."""
        n = 0
        for s in sessions:
            if not self.names or n >= NAMES_PER_TICK or not self.model_allowed(now):
                return
            tp = str(s.get("tp") or "")
            if not tp or s.get("parent") or s.get("agent") == "codex":
                continue
            try:
                first, last = vnames.orders(tp)
                h = vnames.order_hash(last)
                if not h or h == vnames.touch_hash(s["key"]):
                    continue
                n += 1
                text, toks = ask_raw(with_system(self.cmd, vnames.DERIVE_SYSTEM),
                                     vnames.derive_prompt(first, last, vact.basename(J.transcript_cwd(tp))))
                if toks:
                    self.spent.append((now, toks))
                name, topic = vnames.parse_derived(text)
                vnames.store(s["key"], now, name=name, topic_text=topic, order_h=h,
                             first_h=vnames.order_hash(first))
                if name or topic:
                    s["label"] = J.label_for(Path(tp).stem, tp)
                log("names", ok=bool(name or topic), tokens=toks)
            except Exception as e:  # noqa: BLE001
                log("error", where="names", type=type(e).__name__)

    def duty_sessions(self, rel: dict, sessions: list[dict], now: float) -> None:
        """Post the session list for the call page (when it changed, or every POST_EVERY_S),
        apply the switches made there, and subscribe sessions switched on in an earlier call.
        A server without the route (404): asked again after NOT_FOUND_RECHECK_S. Never raises."""
        L = self.lister
        if vmulti._is_local(rel) or now < L.off_until:
            return
        try:
            picks = vnames.picks()
            entries = L.build(vrelayops._config_dir() / "projects", now,
                              {x["key"] for x in sessions}, {x["tp"] for x in sessions if x.get("tp")}, picks)
            if L.changed(entries, now):
                st = WS.post_list(rel, entries)
                if st == 404:
                    L.off_until = now + NOT_FOUND_RECHECK_S
                    log("sessions", why="no-route")
                    return
                if st < 300:
                    L.post_at = now
                log("sessions", n=len(entries), status=st)
            st, got = WS.get_picks(rel)
            if st < 300:
                local = vnames.picks()
                for p in got:
                    e = L.ids.get(str(p["id"]))
                    if p.get("saved") and (e is None or local.get(e["key"]) == bool(p.get("on"))):
                        continue        # the account's choice is known here already
                    self.apply_pick(rel, str(p["id"]), bool(p.get("on")), now)
                picks = vnames.picks()
            # switched on before (another call, a restart): in this call too
            sub = J.load(rel)["subscribed"]
            for e in L.ids.values():
                if picks.get(e["key"]) is True and e["key"] not in sub and \
                        e["key"] not in {x["key"] for x in sessions}:
                    self.subscribe(rel, e, now)
        except vrelayops.RelayError:
            log("error", where="sessions", status=503)
        except Exception as e:  # noqa: BLE001
            log("error", where="sessions", type=type(e).__name__)

    def subscribe(self, rel: dict, e: dict, now: float) -> None:
        st = J.load(rel)
        item = {"label": e["label"], "tp": e["tp"], "at": now}
        if e["kind"] == "sub":
            item["parent"] = e.get("parent_label") or ""
        st["subscribed"][e["key"]] = item
        J.save(st)
        if e["kind"] == "session":
            self.bind(rel, [{"key": e["key"], "label": e["label"], "tp": e["tp"]}])
            self.fetch_start(e["key"], e["label"], now)
        log("pick", kind=e["kind"], on=1)

    def apply_pick(self, rel: dict, lid: str, on: bool, now: float) -> None:
        """A switch on the call page: kept on this machine (all calls), applied at once."""
        e = self.lister.ids.get(lid)
        if e is None:
            return
        vnames.set_pick(e["key"], on, now)
        if on:
            self.subscribe(rel, e, now)
            return
        st = J.load(rel)
        st["subscribed"].pop(e["key"], None)
        prop = st["proposed"].get(e["key"])
        if isinstance(prop, dict):
            prop["answer"] = "nein"
        J.save(st)
        self.fetch.pop(e["label"], None)
        log("pick", kind=e["kind"], on=0)

    def duty_join(self, rel: dict, sessions: list[dict], now: float) -> None:
        """Sessions with REAL activity (J.PROPOSE_ACTIVE_S) that are related to this call
        (J.related: same project, a shared name word, picked before) and not part of it:
        propose each ONCE as a decision card "Soll <Name> mit Thema <Thema> in den Call?"
        (sender Session Watcher, name + topic + host only, max J.MAX_OPEN open), apply the
        answers (Ja = covered like a call session from the next tick, Nein = never again in
        this call). Subagents count for their parent and never get a card. Switched off in
        the session list = never proposed. Never raises."""
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
            newly: list[dict] = []   # 0.34.0: sessions that just got a Ja
            for ev in evs:
                a = J.answer_of(ev)
                if not a or a[0] not in by_id:
                    continue
                key = by_id[a[0]]
                prop[key]["answer"] = a[1]
                if key == J.ALL_KEY:     # a summary card of an older version still open
                    for m in prop[key].get("members") or []:
                        prop[m["key"]] = {"id": None, "at": now, "label": m["label"], "tp": m["tp"],
                                          "last_active": now, "answer": a[1]}
                        if a[1] == "ja":
                            st["subscribed"][m["key"]] = {"label": m["label"], "tp": m["tp"], "at": now}
                            newly.append({"key": m["key"], "label": m["label"], "tp": m["tp"]})
                            self.fetch_start(m["key"], m["label"], now)
                    log("join", label="alle", count=len(prop[key].get("members") or []), answer=a[1])
                    continue
                if a[1] == "ja":
                    st["subscribed"][key] = {"label": prop[key]["label"], "tp": prop[key]["tp"], "at": now}
                    newly.append({"key": key, "label": prop[key]["label"], "tp": prop[key]["tp"]})
                    self.fetch_start(key, prop[key]["label"], now)
                log("join", label=prop[key]["label"], answer=a[1])
            if newly:
                J.save(st)
                self.bind(rel, newly)
            cands = self.unbound(rel, now, J.PROPOSE_ACTIVE_S, bound_keys={x["key"] for x in sessions},
                                 bound_tps={x["tp"] for x in sessions if x.get("tp")})
            picks = vnames.picks()
            projects = {self.project(x["tp"]) for x in sessions if x.get("tp")} - {""}
            owner = self.owner_tp(rel)
            if owner:
                projects.add(self.project(owner))
            labels = {str(x["label"]) for x in sessions}
            on = {k for k, v in picks.items() if v}
            cands = [c for c in cands if picks.get(c["key"]) is not False
                     and not J.is_never(c["label"], c["tp"]) and self.related(c, projects, labels, on)]
            fresh = [c for c in cands if c["key"] not in prop]
            self.derive_names([{"key": c["key"], "label": c["label"], "tp": c["tp"], "agent": "claude"}
                               for c in fresh[:J.MAX_OPEN]], now)
            host = vmulti.machine_label()
            is_open = lambda e: not e.get("answer") and now - float(e.get("at") or 0) < J.TTL_S
            n_open = sum(1 for e in prop.values() if is_open(e) and e.get("id"))
            for c in cands:
                e = prop.get(c["key"])
                if e is not None and e.get("in_all") and not e.get("answer"):
                    continue        # waits for an older summary card
                if e is not None:
                    gap = now - float(e.get("last_active") or 0)
                    e["last_active"] = now
                    if e.get("answer") or gap <= J.REPROPOSE_S:
                        continue    # answered, or asked before and not inactive since
                    if is_open(e):
                        n_open -= 1
                if n_open >= J.MAX_OPEN:
                    continue        # over the cap: stays queued, asked when a card is answered
                try:
                    fresh_lbl = J.label_for(Path(c["tp"]).stem, c["tp"]) if os.path.exists(c["tp"]) else ""
                    label = vrelayops.check_from(fresh_lbl if fresh_lbl and fresh_lbl != "Session" else c["label"])
                except vrelayops.RelayError:
                    continue
                topic = WS.topic_for(c["key"], c["tp"], label, str(c.get("folder") or ""))
                res = post_proposal(rel, J.proposal_body(label, host, c["active_at"], now, topic, self.lang))
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

    def owner_tp(self, rel: dict) -> str:
        """Transcript of the session that joined the call (registry), '' unknown."""
        owner = str(rel.get("owner_session") or "")
        if not owner:
            return ""
        try:
            st = json.loads((vact.state_root() / vmulti.STATE_NAME).read_text(encoding="utf-8"))
            e = (st.get("sessions") or {}).get(owner) if isinstance(st, dict) else None
            return str(e.get("tp") or "") if isinstance(e, dict) else ""
        except (OSError, ValueError, AttributeError):
            return ""

    def bind(self, rel: dict, items: list[dict]) -> None:
        """0.34.0 (caretaker): give subscribed sessions their own helper credential for this
        call, so their CLI commands (VOICEHOOK_SESSION_LABEL=<label>) land in it. No-op for the
        classic watcher. Never raises. The result per label feeds self.fetch."""
        if self.bind_fn is None or not items:
            return
        try:
            res = self.bind_fn(rel, items)
        except Exception as e:  # noqa: BLE001
            log("error", where="bind", type=type(e).__name__)
            res = {str(i.get("label") or ""): "der Session Watcher einen internen Fehler hatte" for i in items}
        if not isinstance(res, dict):
            return
        for it in items:
            f = self.fetch.get(str(it.get("label") or ""))
            if f is None or str(it.get("label") or "") not in res:
                continue
            why = str(res[str(it.get("label") or "")] or "")
            f["bind"] = not why
            f["bind_why"] = why

    def fetch_start(self, key: str, label: str, now: float) -> None:
        """A Ja for this session: track it until it really is in the call (its next board post
        is forced, so the proof is a fresh server answer, not an older one)."""
        self.mem.setdefault(key, {})["post_at"] = 0.0
        try:
            label = vrelayops.check_from(label)
        except vrelayops.RelayError:
            return
        self.fetch.setdefault(label, {"since": now, "bind": self.bind_fn is None, "bind_why": "",
                                      "post": False, "post_why": "", "told": ""})

    def fetch_posted(self, label: str, res: dict) -> None:
        f = self.fetch.get(label)
        if f is None:
            return
        if res.get("ok"):
            f["post"], f["post_why"] = True, ""
        else:
            f["post_why"] = vdev_reason(int(res.get("status") or 0))

    def duty_fetch(self, rel: dict, now: float) -> None:
        """Every Ja ends visibly: the session shows up in the call, or a note "Konnte X nicht
        in den Call holen, weil ..." appears on the board (own sender, once per reason, gone as
        soon as the session arrives). Never raises."""
        for label, f in list(self.fetch.items()):
            try:
                if f["bind"] and f["post"]:
                    # 0.35.0: the note and the watcher board share the sender
                    # `<label> (Session Watcher)`; a board posted since replaced the note already
                    if f["told"] and known_board(fetch_label(label), self.clock()) is None:
                        post_board(rel, fetch_label(label), {})
                    log("fetch", label=label, ok=1, after_s=int(now - f["since"]))
                    del self.fetch[label]
                    continue
                why = f["bind_why"] or f["post_why"]
                if not why and now - f["since"] >= FETCH_VERIFY_S:
                    why = "bisher kein Stand der Session im Call angekommen ist"
                if not why or why == f["told"]:
                    continue
                res = post_board(rel, fetch_label(label), fetch_note(label, why, now, f["since"]))
                if res.get("ok"):
                    f["told"] = why
                log("fetch", label=label, ok=0, told=bool(res.get("ok")))
            except Exception as e:  # noqa: BLE001
                log("error", where="fetch", type=type(e).__name__)

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
            "context": (f"Session {label} meldet sich nicht und ist für den Session Watcher nicht sichtbar. "
                        f"Stand ist die letzte Meldung der Sitzung, unverändert seit {ago}.")[:2000],
            "faq": [{"q": f"Warum steht bei {label} nichts Neues?",
                     "a": f"Die Sitzung hat sich seit {ago} nicht gemeldet."},
                    {"q": f"Ist {label} noch aktiv?",
                     "a": "Unbekannt, der Session Watcher kann die Session nicht sehen."},
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
            if sub_base(sub) in live and sub.endswith(f" ({SUB_NAME})"):
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
        rel = self.pick(now)
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
                    # 0.33.5: this call is over; a newer call's credential takes over, else end
                    if self.cred_fn is not None:
                        self.over = []
                        log("end", why="server-over")
                        return END    # caretaker: asks for a new credential if the call is listed
                    self.dead.add(vmulti._token_hash(str(rel.get("token") or "")))
                    self.over = []
                    if self.pick(now, new_call=True) is None:
                        log("end", why="server-over")
                        return END
                    return OK
                log("error", where="watch-over", count=len(self.over))
                return OK
            self.over = []
            if verdict == "old":
                self.server_min = None
                self.not_found_until = now + NOT_FOUND_RECHECK_S
            else:
                self.server_min = minutes
                self.lang = _LANG[0]
        minutes, src = resolve_interval(self.server_min, self.interval_flag,
                                        os.environ.get(ENV_INTERVAL))
        interval_s = minutes * 60.0
        bad = False
        views: dict[str, tuple[str, float]] = {}
        sessions = list(self.discover(rel, now))
        taken = {x["key"] for x in sessions}
        taken_tp = {x["tp"] for x in sessions if x.get("tp")}
        subs = [x for x in J.subscribed_sessions(rel, taken) if x["tp"] not in taken_tp]
        if self.bind_fn is not None and subs:
            # 0.34.0: a written helper entry shows up in discover() as a bare "helper-<name>"
            # without transcript: the subscribed entry (with transcript) is the same session
            names = {vmulti.helper_key(x["label"]) for x in subs}
            sessions = [x for x in sessions
                        if not (str(x["key"]).startswith("helper-") and not x.get("tp")
                                and vmulti.helper_key(x["label"]) in names)]
            taken = {x["key"] for x in sessions}
            self.bind(rel, [{"key": x["key"], "label": x["label"], "tp": x["tp"]} for x in subs])
        sessions += subs
        picks = vnames.picks()   # 0.35.0: switched off in the session list = not covered
        sessions = [x for x in sessions if picks.get(x["key"]) is not False]
        self.derive_names(sessions, now)
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
                self.fetch_posted(label, {"ok": True})   # it reports into the call itself
                continue
            sent, toks, how = self.sentence(s, state, last, now, m, interval_s)
            if s.get("parent"):   # 0.35.0: a subagent picked in the list names its session
                sent = f"gehört zu: {s['parent']}. {sent}"[:OWN_MAX + 60]
            res = self.duty_status(rel, label, sent, state, last, now, m.get('card'))
            self.fetch_posted(label, res)
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
        self.duty_sessions(rel, sessions, now)
        self.duty_join(rel, sessions, now)
        self.duty_fetch(rel, now)
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
