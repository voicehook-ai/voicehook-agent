"""Klartext-Check for decisions, board items and activity lines (0.21.5).

Live 20.10.2026 (Oliver): decisions and board entries were gibberish nobody could follow,
"db quick wins 620", "SE gate 519 95 grün", "Indizes nach 18h". Now:

* `decide --question` (also over the relay and the MCP `decide`): a whole sentence (at least
  QUESTION_MIN_CHARS chars and QUESTION_MIN_WORDS words) that ends with `?` (a parenthesis may
  follow), no bare PR/ticket number without a description, no digit chains, no unexplained
  abbreviation (SE, WF, RLS, CI, DB ... only as "Sales Engine (SE)" / "SE (Sales Engine)").
  `--context` is mandatory for a new decision. Violations: PlainError, nothing is sent.
  No purpose word ("damit", "um ... zu", "so that", ...): a warning only.
* `status --doing/--open/--done` items and `activity` lines: the same rules for digit chains,
  bare numbers and abbreviations plus at least ITEM_MIN_WORDS words, as warnings on stderr
  (never refused while the agent works).

Every message names the rule and how to fix it, with a bad and a good example.
VOICEHOOK_PLAIN_CHECK=0 turns the check off. Stdlib only, pure.
"""
from __future__ import annotations

import os
import re

ENV_OFF = "VOICEHOOK_PLAIN_CHECK"
QUESTION_MIN_CHARS = 30
QUESTION_MIN_WORDS = 5
ITEM_MIN_WORDS = 3
BAD_EXAMPLE = "DB quick wins 620?"
GOOD_EXAMPLE = ("Sales Engine: vier Datenbank-Indizes anlegen, damit die Admin-Ansicht schneller "
                "lädt? (nachts, ohne Ausfall)")
BAD_ITEM = "SE gate 519 95 grün"
GOOD_ITEM = "Sales Engine: Freigabe-Prüfung für 519 Firmen bestanden, 95 Prozent grün"
EXAMPLES = f'schlecht: "{BAD_EXAMPLE}"  gut: "{GOOD_EXAMPLE}"'
ITEM_EXAMPLES = f'schlecht: "{BAD_ITEM}"  gut: "{GOOD_ITEM}"'
RULE = ("Klartext-Regel: ganzer Satz, nennt Produkt oder Bereich, sagt was passiert und wozu; "
        "keine nackten PR-/Ticketnummern, keine Ziffernketten, Abkürzungen nur mit Erklärung; "
        "Entscheidungen enden mit ? und haben --context.")
# common enough for anyone in the call; everything else needs an explanation
ALLOWED_ABBREV = frozenset({"PR", "OK", "KI", "AI", "UI", "URL", "API", "PDF", "SMS", "FAQ",
                            "ETA", "EU", "USA", "IT", "CEO", "CTO", "CSV", "PIN", "ID"})
_REF_RX = re.compile(r"(?i)(?:\b(?:PR|MR|pull request|ticket|issue|bug)\s*#?\s*\d+|#\d+)")
_ABBREV_RX = re.compile(r"^[A-ZÄÖÜ](?=[A-ZÄÖÜ0-9]*[A-ZÄÖÜ])[A-ZÄÖÜ0-9]{1,6}$")
_NUM_RX = re.compile(r"^\d+(?:[.,]\d+)?%?$")
_TIME_RX = re.compile(r"^\d{1,2}:\d{2}$")
_YEAR_RX = re.compile(r"^(?:19|20)\d\d$")
_PURPOSE_RX = re.compile(r"(?i)\b(damit|um\b.+\bzu|sodass|so dass|weil|wozu|für|zum|zur|"
                         r"so that|in order to|because|to)\b")
_STOP = frozenset({"der", "die", "das", "den", "dem", "des", "ein", "eine", "und", "oder", "mit",
                   "für", "von", "im", "in", "am", "zu", "auf", "ist", "the", "a", "an", "and",
                   "or", "with", "for", "of", "on", "to", "is"})


class PlainError(ValueError):
    """A decision is not plain language: nothing sent, the message says how to fix it."""


def off(env=None) -> bool:
    env = os.environ if env is None else env
    return (env.get(ENV_OFF) or "").strip().lower() in ("0", "false", "no", "off")


def _tokens(text: str) -> list[str]:
    return [t for t in re.split(r"\s+", text.strip()) if t]


def _bare(tok: str) -> str:
    return tok.strip(".,;:!?\"'()[]{}<>«»„“”")


def _words(text: str) -> list[str]:
    return [w for w in (_bare(t) for t in _tokens(text)) if re.search(r"[A-Za-zÄÖÜäöüß]", w)]


def digit_chain(text: str) -> str | None:
    """The first unexplained number: two numbers in a row ("519 95"), or a number of two or
    more digits that no word follows ("wins 620"). Times, years, single digits and numbers
    with a unit or noun after them ("519 Firmen", "10 Minuten") are fine."""
    toks = [_bare(t) for t in _tokens(_REF_RX.sub(" ", text))]
    toks = [t for t in toks if t]
    for i, t in enumerate(toks):
        if not _NUM_RX.match(t) or _TIME_RX.match(t) or _YEAR_RX.match(t):
            continue
        nxt = toks[i + 1] if i + 1 < len(toks) else ""
        if _NUM_RX.match(nxt):
            return f"{t} {nxt}"
        if len(re.sub(r"\D", "", t)) >= 2 and not re.match(r"[A-Za-zÄÖÜäöüß]", nxt):
            return t
    return None


def bare_ref(text: str) -> str | None:
    """A PR/ticket number with (almost) nothing else that says what it is about."""
    m = _REF_RX.search(text)
    if not m:
        return None
    rest = [w for w in _words(_REF_RX.sub(" ", text)) if w.lower() not in _STOP]
    return m.group(0).strip() if len(rest) < 4 else None


def abbreviations(text: str) -> list[str]:
    """Unexplained abbreviations: not in ALLOWED_ABBREV and neither "(SE)" after a phrase
    nor "SE (Sales Engine)"."""
    out: list[str] = []
    toks = _tokens(text)
    for i, raw in enumerate(toks):
        nxt = toks[i + 1] if i + 1 < len(toks) else ""
        for tok in _bare(raw).split("-"):  # "RLS-Regeln", "CI-Lauf"
            if not _ABBREV_RX.match(tok) or tok in ALLOWED_ABBREV or tok in out:
                continue
            if re.search(r"\(\s*" + re.escape(tok) + r"\s*\)", text):
                continue  # "Sales Engine (SE)"
            if nxt.startswith("("):
                continue  # "SE (Sales Engine)"
            out.append(tok)
    return out


def _common(text: str) -> list[str]:
    probs = []
    ref = bare_ref(text)
    if ref:
        probs.append(f"'{ref}' ohne Beschreibung: sag, worum es geht (z. B. \"PR mit dem "
                     f"Login-Fix der Sales Engine mergen\")")
    chain = digit_chain(text)
    if chain:
        probs.append(f"Ziffernkette '{chain}': sag, was die Zahl ist (\"519 Firmen\", \"95 Prozent\")")
    for ab in abbreviations(text):
        probs.append(f"Abkürzung '{ab}' unerklärt: ausschreiben oder \"Langform ({ab})\"")
    return probs


def question_problems(question: str) -> list[str]:
    """Rule violations of a decision question (empty = fine)."""
    q = (question or "").strip()
    probs = []
    if len(q) < QUESTION_MIN_CHARS or len(_words(q)) < QUESTION_MIN_WORDS:
        probs.append(f"zu kurz: ein ganzer Satz (mindestens {QUESTION_MIN_CHARS} Zeichen, "
                     f"{QUESTION_MIN_WORDS} Wörter) mit Produkt/Bereich, was passiert und wozu")
    if "?" not in q or not re.fullmatch(r"\s*(\([^()]*\))?\s*", q.rsplit("?", 1)[1]):
        probs.append("endet nicht mit '?' (danach höchstens eine Klammer wie \"(nachts, ohne Ausfall)\")")
    return probs + _common(q)


def question_warnings(question: str) -> list[str]:
    if _PURPOSE_RX.search(question or ""):
        return []
    return ["sag auch wozu: \"..., damit ...\" / \"..., so that ...\""]


def item_problems(text: str) -> list[str]:
    """Rule violations of a board item or activity line (warnings)."""
    t = (text or "").strip()
    if not t:
        return []
    probs = []
    if len(_words(t)) < ITEM_MIN_WORDS:
        probs.append(f"zu knapp: mindestens {ITEM_MIN_WORDS} Wörter, mit Bereich und was passiert")
    return probs + _common(t)


def check_decide(question: str, context: str | None, new: bool = True) -> list[str]:
    """Raise PlainError for a decision that breaks the rule; return the warnings."""
    if off():
        return []
    probs = question_problems(question)
    if new and not (context or "").strip():
        probs.append("--context fehlt (Pflicht): 2-4 Sätze, worum es geht, warum, was bei Ja "
                     "und was bei Nein passiert")
    if probs:
        raise PlainError("Entscheidung nicht im Klartext, nichts gesendet:\n- " + "\n- ".join(probs)
                         + f"\n{EXAMPLES}\n{RULE}")
    return [f"[hint] Klartext: {w}" for w in question_warnings(question)]


def item_warnings(items: list[str], what: str = "Board-Eintrag") -> list[str]:
    """One stderr line per item that breaks the rule ([] = all fine or the check is off)."""
    if off():
        return []
    out = []
    for it in items:
        probs = item_problems(it)
        if probs:
            out.append(f"[warn] Klartext ({what} \"{it[:60]}\"): " + "; ".join(probs)
                       + f". {ITEM_EXAMPLES}")
    return out
