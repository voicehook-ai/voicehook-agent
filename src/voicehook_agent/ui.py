"""Control the call page of the humans in the room: `voicehook-agent ui` (0.19.0).

    ui open|close|min|medium|large|max <window>   one window (state)
    ui layout <preset>                            the layout preset
    ui move <window> <slot> [--preset P]          a window into a slot (of preset P)
    ui focus <project|all>                        show only one project

Contract (server voicehook-v4 uicmd.py, schema v2): data packet topic `delta.ui`, reliable,
sent ONLY to the humans in the room (relay.is_human_peer: no operator agent, not the
voicebot worker), the page applies it like its own click:

    {"v":2, "cmd":"window", "win":W, "state":S, "via":"agent"}
    {"v":2, "cmd":"layout", "preset":P, "via":"agent"}
    {"v":2, "cmd":"move", "win":W, "slot":SL, "preset"?:P, "via":"agent"}
    {"v":2, "cmd":"focus", "project":PR, "via":"agent"}

Bridge transport: `POST /api/bridge/ui` with the body without v/via -> {ok, applied} or 422.
Values are normalized here (aliases, case, umlauts as given); an unknown value fails with a
clear message before anything is sent.
"""
from __future__ import annotations

import re

TOPIC = "delta.ui"
BRIDGE_PATH = "/api/bridge/ui"
VERSION = 2
VIA = "agent"

WINDOWS = ("log", "dec", "board", "wrap", "cards")
STATES = ("open", "close", "min", "medium", "large", "max")
PRESETS = ("halves", "quarters", "thirds", "bigmid", "bar")
SLOTS = ("l", "m", "r", "b", "tl", "tr", "bl", "br", "main", "side")
PROJECTS = ("voicehook", "salesengine", "kunden", "sonstiges", "all")
ACTIONS = (*STATES, "layout", "move", "focus")

WINDOW_ALIASES = {
    "activity": "log", "aktivitaet": "log", "aktivität": "log", "feed": "log",
    "decisions": "dec", "entscheidungen": "dec",
    "status": "board", "statusboard": "board",
    "transcript": "wrap", "transkript": "wrap", "chat": "wrap",
    "karten": "cards",
}
PRESET_ALIASES = {
    "dreiteiler": "thirds", "drittel": "thirds", "viertel": "quarters",
    "haelften": "halves", "hälften": "halves", "gross-mitte": "bigmid", "big": "bigmid",
    "leiste": "bar",
}
SLOT_ALIASES = {
    "left": "l", "links": "l", "middle": "m", "mitte": "m", "right": "r", "rechts": "r",
    "bottom": "b", "unten": "b",
}
PROJECT_ALIASES = {
    "sales engine": "salesengine", "se": "salesengine", "customers": "kunden",
    "other": "sonstiges", "alles": "all",
}
PROJECT_FIELD_MAX = 40   # `--project` on status/activity (server projects.PROJECT_MAX)


class UiError(ValueError):
    """Unknown window/state/preset/slot/project or a wrong number of arguments."""


def _key(value: object) -> str:
    return " ".join(str(value or "").split()).casefold()


def _squash(key: str) -> str:
    return re.sub(r"[\s_-]+", "", key)


def _pick(value: object, canon: tuple, aliases: dict, what: str) -> str:
    """Canonical value for `value`: itself, an alias, or the same ignoring space/-/_."""
    k = _key(value)
    table = {c: c for c in canon} | aliases
    if k in table:
        return table[k]
    loose = {_squash(a): c for a, c in table.items()}
    got = loose.get(_squash(k))
    if got and k:
        return got
    shown = repr(str(value)) if str(value or "").strip() else "nothing"
    raise UiError(f"unknown {what} {shown}: use one of {', '.join(canon)}")


def window(value: object) -> str:
    return _pick(value, WINDOWS, WINDOW_ALIASES, "window")


def preset(value: object) -> str:
    return _pick(value, PRESETS, PRESET_ALIASES, "preset")


def slot(value: object) -> str:
    return _pick(value, SLOTS, SLOT_ALIASES, "slot")


def project(value: object) -> str:
    return _pick(value, PROJECTS, PROJECT_ALIASES, "project")


def command(action: str, args: list[str] | None = None, preset_opt: str | None = None) -> dict:
    """`ui <action> <args...> [--preset P]` -> body without v/via. UiError when invalid."""
    act = _key(action)
    args = [a for a in (args or []) if str(a).strip()]

    def need(n: int, usage: str) -> None:
        if len(args) != n:
            raise UiError(f"usage: voicehook-agent ui {usage}")

    if preset_opt is not None and act != "move":
        raise UiError("--preset only goes with `ui move`")
    if act in STATES:
        need(1, f"{act} <window>  (window: {', '.join(WINDOWS)})")
        return {"cmd": "window", "win": window(args[0]), "state": act}
    if act == "layout":
        need(1, f"layout <preset>  (preset: {', '.join(PRESETS)})")
        return {"cmd": "layout", "preset": preset(args[0])}
    if act == "move":
        need(2, "move <window> <slot> [--preset P]")
        body = {"cmd": "move", "win": window(args[0]), "slot": slot(args[1])}
        if preset_opt is not None:
            body["preset"] = preset(preset_opt)
        return body
    if act == "focus":
        if not args:
            raise UiError(f"usage: voicehook-agent ui focus <project|all>  (project: {', '.join(PROJECTS)})")
        return {"cmd": "focus", "project": project(" ".join(args))}  # "sales engine"
    raise UiError(f"unknown ui command {action!r}: use one of {', '.join(ACTIONS)}")


def validate(body: object) -> dict:
    """Join side: check a body again (same rules), canonical form."""
    if not isinstance(body, dict):
        raise UiError("ui body must be an object")
    c = body.get("cmd")
    if c == "window":
        st = _key(body.get("state"))
        if st not in STATES:
            raise UiError(f"unknown state {body.get('state')!r}: use one of {', '.join(STATES)}")
        return {"cmd": "window", "win": window(body.get("win")), "state": st}
    if c == "layout":
        return {"cmd": "layout", "preset": preset(body.get("preset"))}
    if c == "move":
        out = {"cmd": "move", "win": window(body.get("win")), "slot": slot(body.get("slot"))}
        if body.get("preset") is not None:
            out["preset"] = preset(body["preset"])
        return out
    if c == "focus":
        return {"cmd": "focus", "project": project(body.get("project"))}
    raise UiError("cmd must be one of window, layout, move, focus")


def packet(body: dict) -> dict:
    """Data packet for topic delta.ui (WebRTC)."""
    return {"v": VERSION, **body, "via": VIA}


def summary(body: dict) -> str:
    """One confirmation line: `ui: window log large`, `ui: move log l thirds`, ..."""
    keys = {"window": ("win", "state"), "layout": ("preset",),
            "move": ("win", "slot", "preset"), "focus": ("project",)}.get(body.get("cmd"), ())
    return " ".join(["ui:", str(body.get("cmd"))] + [str(body[k]) for k in keys if body.get(k)])


def project_field(value: str | None) -> str | None:
    """`--project NAME` of status/activity: 1-40 chars (whitespace collapsed), else ValueError.
    None = not given. The server maps it to its project key (focus filter)."""
    if value is None:
        return None
    p = " ".join(str(value).split())
    if not 1 <= len(p) <= PROJECT_FIELD_MAX:
        raise ValueError(f"--project must be 1 to {PROJECT_FIELD_MAX} characters")
    return p
