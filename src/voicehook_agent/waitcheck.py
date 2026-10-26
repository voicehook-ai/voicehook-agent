"""Waiting for the human is a decision, never only a status (0.21.6).

Live 20.10.2026: the promo helper session waited for Oliver's okay but only set
`status --doing "... wartet auf dein Okay zu Musik und Stimme"`: no decision on the board,
its card said "arbeitet", Oliver never saw a question. Now:

* `status` TEXT/--doing and `activity`: a text that waits for the human ("wartet auf",
  "warte auf", "dein OK/Okay", "auf deine Freigabe", "Entscheidung von", "sobald du",
  "waiting for your", ...) is refused (WaitError, exit 2, nothing sent) with a ready
  `decide` command; `--open` items only get a warning.
* `status --waiting TEXT` is the way to show it: the board says "wartet auf dich"
  (state `frage`) and needs `--question` (asked right away) or `--decision ID` (an open one).
* Stop hook: a turn that ends with such a text as the last board and no open decision is
  blocked once with "stelle eine Entscheidung per decide" (board.py helpers, listenguard.py
  for the joining session).

VOICEHOOK_WAIT_CHECK=0 turns it off. Stdlib only, pure.
"""
from __future__ import annotations

import os
import re

ENV_OFF = "VOICEHOOK_WAIT_CHECK"
RULE = "Wenn du auf den Menschen wartest, IMMER decide, nie nur Status."
RULE_EN = "When you wait for the human, ALWAYS decide, never only a status."
STOP_HINT = "stelle eine Entscheidung per decide"
STATE = "frage"   # server helper state "wartet auf eine Antwort von dir" / "waiting for your answer"
# "wartet auf ..." only counts when a person or their answer follows (never "wartet auf CI")
_HUMAN = (r"dein\w*|dich\b|euch\b|eur\w*|ihr\w*|sein\w*|oliver\w*|den\s+(?:menschen|user|nutzer|kunden)"
          r"|(?:die\s+|eine\s+)?(?:freigabe|antwort|entscheidung|r[üu]ckmeldung|best[äa]tigung"
          r"|feedback|input|zustimmung|okay|ok\b|go\b)")
_RX = re.compile(r"(?ix)("
                 r"\bwarte[tn]?\s+(?:noch\s+|gerade\s+)?auf\s+(?:" + _HUMAN + r")"
                 r"|\bdein(?:e|en)?\s+(?:ok|okay|go|freigabe|antwort|entscheidung|zustimmung)\b"
                 r"|\b(?:auf|ihre|eure|seine|olivers)\s+freigabe\b"
                 r"|\bfreigabe\s+(?:von|durch|abwarten|fehlt|n[öo]tig|ben[öo]tigt|ausstehend)\b"
                 r"|\bentscheidung\s+(?:von|durch)\b"
                 r"|\bsobald\s+du\b"
                 r"|\bwenn\s+du\s+(?:ok|okay|ja)\s+sagst\b"
                 r"|\bwaiting\s+(?:for|on)\s+(?:your|you|the\s+human|approval|a\s+decision)\b"
                 r"|\bwait(?:s)?\s+for\s+(?:your|you|approval)\b"
                 r"|\byour\s+(?:ok|okay|go|approval|decision|answer)\b"
                 r"|\bonce\s+you\b"
                 r")")
_DID_RX = re.compile(r"^d[0-9a-f]{8}$")


class WaitError(ValueError):
    """A status/activity text waits for the human: nothing sent, the message says how."""


def off(env=None) -> bool:
    env = os.environ if env is None else env
    return (env.get(ENV_OFF) or "").strip().lower() in ("0", "false", "no", "off")


def waiting(text: object) -> str | None:
    """The phrase that waits for the human, or None."""
    m = _RX.search(str(text or ""))
    return m.group(0) if m else None


def _q(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def question_from(text: str) -> str:
    """A question proposal from the waiting text: "<area>: <object> so freigeben, damit ich
    weitermachen kann?" when the text names what to approve ("... Okay zu Musik und Stimme"),
    else a template to fill in."""
    t = str(text or "").strip()
    area = ""
    m = re.match(r"^\s*([^:]{2,40}):\s*(.+)$", t)
    body = t
    if m:
        area, body = m.group(1).strip(), m.group(2)
    obj = ""
    w = _RX.search(body)
    if w:
        o = re.match(r"(?i)\s*(?:\w+\s+)?(?:zu|für|fuer|zum|zur|on|for)\s+(.+)$", body[w.end():])
        obj = o.group(1).strip(" .,;:-–…") if o else ""
    if len(re.findall(r"[A-Za-zÄÖÜäöüß]{2,}", obj)) < 1:
        return "<Bereich>: <was genau> freigeben, damit <wozu>?"
    return f"{area + ': ' if area else ''}{obj} so freigeben, damit ich weitermachen kann?"


def suggestion(text: str, prog: str = "voicehook-agent") -> str:
    q = question_from(text)
    return (f"{prog} decide --question {_q(q)} --context "
            f"{_q('<was, warum, was bei Ja und was bei Nein passiert>')}")


def message(text: str, what: str = "status --doing", prog: str = "voicehook-agent") -> str:
    phrase = waiting(text) or ""
    return (f"'{text[:80]}' wartet auf den Menschen ('{phrase}'): das ist eine Entscheidung, kein "
            f"Status. Nichts gesendet ({what}). {RULE}\n"
            f"  1. {suggestion(text, prog)}\n"
            f"  2. dann sichtbar wartend: {prog} status --waiting {_q(text[:120])} --decision <id>\n"
            f"  oder beides in einem Schritt: {prog} status --waiting {_q(text[:120])} "
            f"--question \"...?\" --context \"...\"\n"
            f"Aus: {ENV_OFF}=0.")


def check(text: object, what: str = "status --doing", prog: str = "voicehook-agent") -> None:
    """Raise WaitError when `text` waits for the human (and the check is on)."""
    t = str(text or "").strip()
    if t and not off() and waiting(t):
        raise WaitError(message(t, what, prog))


def warn_items(items: list[str]) -> list[str]:
    if off():
        return []
    return [f"[warn] '{it[:60]}' wartet auf den Menschen: {RULE} ({suggestion(it)})"
            for it in items if isinstance(it, str) and waiting(it)]


def decision_ref(value: object) -> str | None:
    """`--decision`: an id (d + 8 hex) or a board number (#3 / 3); None when invalid."""
    v = str(value or "").strip().lstrip("#")
    if _DID_RX.match(v) or (v.isdigit() and 0 < int(v) < 10000):
        return v
    return None


# --------------------------------------------------------------------------- #
# Stop hook: extract a candidate question sentence from the last turn's text
# --------------------------------------------------------------------------- #
_SENT_SEP = re.compile(r"(?<=[.!?])\s+")
_VERB_LIKE = re.compile(r"(?i)\b(?:"
                        r"soll(?:en|te)?|will(?:st|en)?|kann(?:st)?|darf(?:st)?|muss(?:t)?|"
                        r"mag(?:st)?|wäre|würde(?:st)?|ist|sind|hat|haben|bin|bist|warte[nt]?|"
                        r"shall|should|will|can|may|must|would|could|is|are|have|has|do|does|did|"
                        r"sagen|machen|tun|schicken|starten|prüfen|deployen|freigeben|fortfahren|"
                        r"weiterma(?:chen)?|proceed|continue|confirm|start|send|deploy|check|approve"
                        r")\b")
_Q_MIN, _Q_MAX = 12, 160


def extract_question(text: object) -> str | None:
    """Deterministic: the last sentence ending with '?' or the one matching `waiting()`.

    Returns the sentence when it is plausible as a whole question:
    - between _Q_MIN and _Q_MAX chars (after strip)
    - contains at least one verb-like word (Verb pattern above)
    - has at least 3 whitespace-separated tokens
    Otherwise returns None.
    """
    t = str(text or "").strip()
    if not t:
        return None

    # flatten fences / markdown noise (same approach as asks.py)
    import re as _re
    body = _re.sub(r"```.*?(?:```|$)", "", t, flags=_re.DOTALL).strip()

    # split into sentences; consider both "." and "?" boundaries
    sentences = [s.strip() for s in _SENT_SEP.split(body) if s.strip()]

    # 1) last sentence ending with "?"
    q_sent = next((s for s in reversed(sentences) if s.rstrip(" )").endswith("?")), None)

    # 2) fallback: sentence that matches waiting()
    if q_sent is None:
        q_sent = next((s for s in reversed(sentences) if waiting(s)), None)

    if q_sent is None:
        return None

    # plausibility checks
    q = q_sent.strip()
    if not (_Q_MIN <= len(q) <= _Q_MAX):
        return None
    if len(q.split()) < 3:
        return None
    if not _VERB_LIKE.search(q):
        return None
    return q
