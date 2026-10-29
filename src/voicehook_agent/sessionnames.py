"""Readable session names and topics (0.35.0, Oliver 29.10.2026: "Session Watcher").

Before, a session without /rename or ai-title was called `<cwd basename> <4 hex>` ("oliver
43cb"), and the Session Watcher's cards and board lines showed exactly that. Now every name
a human sees is plain text, never an id:

* name: /rename title > a name the Session Watcher derived from the session's own order
  (cheapest model, e.g. "Resend-Einrichtung") > Claude Code's ai-title > the project folder
  (`oliver`, a second session in the same folder `oliver 2`). Random slugs like
  "crown-moonlit-pascal" are never used.
* topic: what the session works on now, derived from its LATEST order (e.g. "DNS-Einträge
  prüfen"); without one the ai-title, else the project folder.

Names are unique per machine (a clash gets " 2", " 3"): one file ~/.voicehook/
session-names.json (0600, machine-wide like watch.lock, whatever VOICEHOOK_STATE_DIR a
session uses), keyed by the session hash, entries forgotten after NAME_KEEP_S. The hook side
(multisession.session_label) reads it, the watcher writes derived names into it.

Privacy: the file holds names, topics and hashes only. The order text is read from the
transcript, handed to the local model command and dropped; never logged, never posted.

stdlib only; imports activity only (multisession imports this module).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path

from . import activity as vact

NAMES_FILE = "session-names.json"
PICKS_FILE = "session-picks.json"
MACHINE_ENV = "VOICEHOOK_MACHINE_DIR"
NAME_KEEP_S = 14 * 86400.0      # a name is kept two weeks after the session was last seen
NAME_MAX = 40                   # server: `from` 1-40 characters
TOPIC_MAX = 80
ORDER_CHARS = 600               # order text handed to the model (head of the order)
HEAD_BYTES = 256 * 1024
TAIL_BYTES = 256 * 1024
PICKS_MAX = 500

# a random slug ("crown-moonlit-pascal", "vivid-ivory"): lower-case words only, joined by '-'
_SLUG = re.compile(r"^[a-z]+(?:-[a-z]+){1,3}$")
_WRAPPERS = re.compile(r"<(?:system-reminder|command-[a-z]+|local-command-[a-z]+|task-notification)"
                       r"\b[^>]*>.*?</(?:system-reminder|command-[a-z]+|local-command-[a-z]+|task-notification)>",
                       re.DOTALL)
_TAG = re.compile(r"</?[a-zA-Z][a-zA-Z0-9_-]*(?:\s[^>]*)?>")


def machine_root() -> Path:
    env = (os.environ.get(MACHINE_ENV) or "").strip()
    return Path(env) if env else Path.home() / ".voicehook"


def _h(value: str) -> str:
    return hashlib.sha256(f"vh-name|{value}".encode()).hexdigest()[:16]


def clean(raw: object, limit: int = NAME_MAX) -> str:
    """One line, scrubbed, no '|' / ':' (sender separators), at most `limit` chars cut at a word."""
    s = re.sub(r"[|:]", " ", vact.scrub(str(raw or "")))
    s = " ".join(s.split()).strip(" .,;-")
    if len(s) > limit:
        cut = s[:limit + 1].rsplit(" ", 1)[0]
        s = (cut if 0 < len(cut) <= limit else s[:limit]).rstrip(" .,;-")
    return s


def is_slug(name: str) -> bool:
    """A generated random name ("crown-moonlit-pascal"), not a human or topic name."""
    return bool(_SLUG.match(name or "")) and name.count("-") >= 2


# --------------------------------------------------------------------------- #
# file
# --------------------------------------------------------------------------- #
def _rw(name: str, fn):
    root = machine_root()
    vact._private_dir(root)
    fd = os.open(root / name, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        vact._lock(fd)
        try:
            d = json.loads(os.read(fd, 1 << 21).decode("utf-8") or "{}")
        except ValueError:
            d = {}
        d = d if isinstance(d, dict) else {}
        res = fn(d)
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, json.dumps(d, ensure_ascii=False).encode("utf-8"))
        return res
    finally:
        os.close(fd)


def _read(name: str) -> dict:
    try:
        d = json.loads((machine_root() / name).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def entry(skey: str) -> dict:
    e = _read(NAMES_FILE).get(skey) if skey else None
    return e if isinstance(e, dict) else {}


def _prune(d: dict, now: float) -> None:
    for k in [k for k, v in d.items() if not isinstance(v, dict) or now - float(v.get("at") or 0) > NAME_KEEP_S]:
        d.pop(k, None)


def _unique(d: dict, skey: str, want: str) -> str:
    taken = {str(v.get("name") or "").lower() for k, v in d.items() if k != skey and isinstance(v, dict)}
    if want.lower() not in taken:
        return want
    for n in range(2, 100):
        sfx = f" {n}"
        cand = want[:NAME_MAX - len(sfx)].rstrip() + sfx
        if cand.lower() not in taken:
            return cand
    return want


def folder_name(skey: str, folder: str, now: float) -> str:
    """The stored name of this session, else its project folder made unique on this machine
    (stored, so it stays the same across hook events). '' without folder."""
    base = clean(folder)
    if not skey:
        return base
    try:
        def step(d: dict) -> str:
            _prune(d, now)
            e = d.get(skey) if isinstance(d.get(skey), dict) else None
            if e and e.get("name"):
                e["at"] = now
                return str(e["name"])
            if not base:
                return ""
            name = _unique(d, skey, base)
            d[skey] = {"name": name, "src": "folder", "at": now}
            return name
        return _rw(NAMES_FILE, step)
    except OSError:
        return base


def derived_name(skey: str) -> str:
    """A name the watcher derived from the session's order ('' when none)."""
    e = entry(skey)
    return str(e.get("name") or "") if e.get("src") == "order" else ""


def topic(skey: str) -> str:
    return str(entry(skey).get("topic") or "")


def store(skey: str, now: float, *, name: str = "", topic_text: str = "", order_h: str = "",
          first_h: str = "") -> str:
    """Save a derived name (made unique) and/or topic. Returns the stored name."""
    if not skey:
        return ""
    name, topic_text = clean(name), clean(topic_text, TOPIC_MAX)

    def step(d: dict) -> str:
        _prune(d, now)
        e = dict(d.get(skey) or {}) if isinstance(d.get(skey), dict) else {}
        if name and not is_slug(name) and e.get("src") != "order":
            e["name"], e["src"] = _unique(d, skey, name), "order"
            e["first_h"] = first_h
        if topic_text:
            e["topic"] = topic_text
        if order_h:
            e["order_h"] = order_h
        e["at"] = now
        d[skey] = e
        return str(e.get("name") or "")
    try:
        return _rw(NAMES_FILE, step)
    except OSError:
        return name


def touch_hash(skey: str) -> str:
    return str(entry(skey).get("order_h") or "")


# --------------------------------------------------------------------------- #
# persistent choices from the session list ("mit in den Call", per session / subagent)
# --------------------------------------------------------------------------- #
def picks() -> dict[str, bool]:
    return {k: bool(v.get("on")) for k, v in _read(PICKS_FILE).items() if isinstance(v, dict)}


def set_pick(key: str, on: bool, now: float) -> None:
    def step(d: dict) -> None:
        d[str(key)] = {"on": bool(on), "at": now}
        if len(d) > PICKS_MAX:
            for k, _v in sorted(d.items(), key=lambda kv: float((kv[1] or {}).get("at") or 0))[:len(d) - PICKS_MAX]:
                d.pop(k, None)
    try:
        _rw(PICKS_FILE, step)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# transcript reading: orders, project, real activity, subagents
# --------------------------------------------------------------------------- #
def _lines(path: str, tail: bool) -> list[str]:
    cut = False
    try:
        with open(path, "rb") as f:
            if tail:
                f.seek(0, os.SEEK_END)
                start = max(0, f.tell() - TAIL_BYTES)
                cut = start > 0
                f.seek(start)
                data = f.read()
            else:
                data = f.read(HEAD_BYTES)
    except OSError:
        return []
    out = data.decode("utf-8", "replace").splitlines()
    return out[1:] if cut else out   # the first line of a cut tail is partial


def order_text(d: dict) -> str:
    """The order of a user entry (typed by the human or sent by another agent), '' for tool
    results, meta lines, commands and reminders."""
    if not isinstance(d, dict) or d.get("type") != "user" or d.get("isMeta"):
        return ""
    msg = d.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, list):
        if any(isinstance(c, dict) and c.get("type") == "tool_result" for c in content):
            return ""
        content = " ".join(str(c.get("text") or "") for c in content
                           if isinstance(c, dict) and c.get("type") == "text")
    if not isinstance(content, str):
        return ""
    text = _WRAPPERS.sub(" ", content)
    if text.lstrip().startswith(("Caveat:", "[Request interrupted")):
        return ""
    text = " ".join(_TAG.sub(" ", text).split())
    return text if len(text.split()) >= 2 else ""


def orders(tp: str) -> tuple[str, str]:
    """(first order, latest order) of a transcript, each at most ORDER_CHARS. In memory only."""
    first = ""
    for line in _lines(tp, tail=False):
        if '"user"' not in line:
            continue
        try:
            first = order_text(json.loads(line))
        except ValueError:
            continue
        if first:
            break
    last = ""
    for line in reversed(_lines(tp, tail=True)):
        if '"user"' not in line:
            continue
        try:
            last = order_text(json.loads(line))
        except ValueError:
            continue
        if last:
            break
    return first[:ORDER_CHARS], (last or first)[:ORDER_CHARS]


def order_hash(text: str) -> str:
    return _h(text) if text else ""


def _ts(v: object) -> float:
    if not isinstance(v, str) or not v:
        return 0.0
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def real_activity(tp: str) -> float:
    """Time of the last REAL step of a session: a human/agent order, an assistant turn or a
    tool result (entry timestamps, not the file time: Claude Code also rewrites its
    transcript for bookkeeping lines like last-prompt, mode or attachments). 0.0 unknown."""
    for line in reversed(_lines(tp, tail=True)):
        if '"user"' not in line and '"assistant"' not in line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if isinstance(d, dict) and d.get("type") in ("user", "assistant") and not d.get("isMeta"):
            t = _ts(d.get("timestamp"))
            if t:
                return t
    return 0.0


def transcript_cwd(tp: str, lines: int = 80) -> str:
    for line in _lines(tp, tail=False)[:lines]:
        if '"cwd"' not in line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        cwd = d.get("cwd") if isinstance(d, dict) else None
        if isinstance(cwd, str) and cwd:
            return cwd
    return ""


def project_of(cwd: str) -> str:
    """Project of a working directory: the main repository of a git worktree
    (~/voicehook-v4-watcher -> voicehook-v4), else the folder name. '' unknown."""
    if not cwd:
        return ""
    p = Path(cwd)
    try:
        g = p / ".git"
        if g.is_file():
            m = re.match(r"gitdir:\s*(.+)", g.read_text(encoding="utf-8", errors="replace").strip())
            if m:
                parts = Path(m.group(1).strip()).parts
                if "worktrees" in parts:
                    i = parts.index("worktrees")
                    if i >= 2 and parts[i - 1] == ".git":
                        return parts[i - 2]
    except OSError:
        pass
    return p.name


def subagents(tp: str) -> list[dict]:
    """Subagents (Claude Code Task / background agents and forks) of a session: Claude Code
    writes them to <session>/subagents/agent-<id>.jsonl next to <session>.jsonl, with
    `isSidechain: true` and the parent's sessionId in every line, plus agent-<id>.meta.json
    whose `description` is the short task the parent gave. [{id, name, topic, finished,
    mtime, tp}] newest first; the name comes from the description only (never the prompt)."""
    if not tp:
        return []
    d = Path(tp.removesuffix(".jsonl")) / "subagents"
    out = []
    try:
        files = list(d.glob("agent-*.jsonl"))
    except OSError:
        return []
    for p in files:
        try:
            m = p.stat().st_mtime
        except OSError:
            continue
        meta: dict = {}
        try:
            meta = json.loads(p.with_name(p.stem + ".meta.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
        meta = meta if isinstance(meta, dict) else {}
        desc = clean(meta.get("description"))
        out.append({"id": p.stem.removeprefix("agent-"), "name": desc or "Unteragent",
                    "topic": clean(meta.get("description"), TOPIC_MAX),
                    "finished": bool(meta.get("finished")), "mtime": m, "tp": str(p)})
    return sorted(out, key=lambda s: -s["mtime"])


# --------------------------------------------------------------------------- #
# deriving name + topic with the model
# --------------------------------------------------------------------------- #
DERIVE_SYSTEM = (
    "Du benennst Programmier-Sitzungen fuer eine Uebersicht. Antworte NUR mit JSON "
    '{"name": "...", "thema": "..."}. name: 1 bis 3 Woerter, ein Thema-Name aus dem ersten '
    "Auftrag (z.B. Resend-Einrichtung, Kartoffel-Lasttest), hoechstens 30 Zeichen. thema: "
    "2 bis 6 Woerter, woran die Sitzung laut letztem Auftrag gerade arbeitet, hoechstens 60 "
    "Zeichen. Deutsch, keine Dateipfade, keine IDs, keine Anfuehrungszeichen, keine "
    "Firmennamen erfinden, keine Gedankenstriche.")


def derive_prompt(first: str, last: str, folder: str) -> str:
    return (f"Projektordner: {folder or '(unbekannt)'}\n"
            f"Erster Auftrag:\n{first or '(keiner)'}\n\n"
            f"Letzter Auftrag:\n{last or first or '(keiner)'}\n\n"
            "Gib jetzt das JSON aus.")


def parse_derived(text: str) -> tuple[str, str]:
    """(name, topic) from the model's answer; ('', '') when unusable."""
    m = re.search(r"\{.*\}", str(text or ""), re.DOTALL)
    if not m:
        return "", ""
    try:
        d = json.loads(m.group(0))
    except ValueError:
        return "", ""
    if not isinstance(d, dict):
        return "", ""
    name = clean(str(d.get("name") or "").replace("–", "-").replace("—", "-"))
    top = clean(str(d.get("thema") or d.get("topic") or "").replace("–", "-").replace("—", "-"),
                TOPIC_MAX)
    if is_slug(name) or re.search(r"\b[0-9a-f]{6,}\b", name):
        name = ""
    if re.search(r"\b[0-9a-f]{6,}\b", top):
        top = ""
    return name, top
