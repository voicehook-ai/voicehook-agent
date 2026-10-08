"""Shapes + emotion for the ring (Kringel) in the call: `show` and `say --shape-*`.

Contract (server: topic `operator.visual`, field `shape`/`emotion` on `operator.say`,
bridge `POST /api/bridge/visual`), coordinates 0..1 (0,0 = top left):

    shape = {"type":"preset","name":<PRESETS>,"hold_ms":800..8000}
          | {"type":"polygon","points":[[x,y],...],"closed":bool,"hold_ms":..}
          | {"type":"path","d":"<SVG path, only M L Q C Z>","hold_ms":..}
          | {"type":"multi","items":[<preset|polygon|path>, ... max 4]}
          ; every shape may carry "label" (<= 24 chars)
    emotion = {"label":<EMOTIONS>,"valence":-1..1,"arousal":0..1}

At most 200 points over the whole shape, a path string `d` at most 2 KB, the whole shape
at most 8 KB as compact JSON. Label: at most 24 chars, no control characters, emoji or
`<>`. The server draws at most one shape per 2 s per operator (bridge: 429; invalid: 400).
The CLI validates like the server so a bad shape fails here with a clear message (exit 2)
instead of silently not being drawn.
"""
from __future__ import annotations

import json
import math
import re
import unicodedata

TOPIC = "operator.visual"
BRIDGE_PATH = "/api/bridge/visual"

PRESETS = (
    "arrow_up", "arrow_right", "check", "cross", "question", "loop", "split3",
    "scale", "heart", "bolt", "one", "two", "three",
)
# label -> default (valence, arousal), same table as the server
EMOTIONS: dict[str, tuple[float, float]] = {
    "neutral": (0.0, 0.3),
    "joy": (0.8, 0.6),
    "calm": (0.4, 0.2),
    "curious": (0.3, 0.5),
    "excited": (0.7, 0.9),
    "concerned": (-0.4, 0.5),
    "frustrated": (-0.7, 0.8),
    "sad": (-0.7, 0.3),
}
HOLD_MS_MIN, HOLD_MS_MAX = 800, 8000
LABEL_MAX = 24
MULTI_MAX = 4
POINTS_MAX = 200
PATH_BYTES_MAX = 2048       # one path string d
BYTES_MAX = 8192            # whole shape as compact JSON
RATE_S = 2.0                # server: one shape per 2 s per operator
# SVG path: only absolute M L Q C Z; numbers per segment
PATH_ARGS = {"M": 2, "L": 2, "Q": 4, "C": 6, "Z": 0}
_PATH_TOKEN = re.compile(r"[A-Za-z]|[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")
_PATH_JUNK = re.compile(r"[^A-Za-z0-9.,+\-eE\s]")


class VisualError(ValueError):
    """Shape/emotion does not match the contract; message says what and where."""


# ----- helpers ------------------------------------------------------------------------
def _num(v: object, what: str, lo: float, hi: float) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise VisualError(f"{what} must be a number, got {v!r}")
    f = float(v)
    if not math.isfinite(f) or f < lo or f > hi:
        raise VisualError(f"{what} must be in {lo:g}..{hi:g}, got {v!r}")
    return f


def _coord(v: object, what: str) -> float:
    return _num(v, what, 0.0, 1.0)


def _hold(raw: dict, where: str) -> dict:
    if "hold_ms" not in raw or raw["hold_ms"] is None:
        return {}
    v = raw["hold_ms"]
    if isinstance(v, bool) or not isinstance(v, int) and not (isinstance(v, float) and v.is_integer()):
        raise VisualError(f"{where}hold_ms must be a whole number of ms, got {v!r}")
    _num(v, f"{where}hold_ms", HOLD_MS_MIN, HOLD_MS_MAX)
    return {"hold_ms": int(v)}


def _label(raw: dict, where: str) -> dict:
    if "label" not in raw or raw["label"] is None:
        return {}
    v = raw["label"]
    if not isinstance(v, str) or not v.strip():
        raise VisualError(f"{where}label must be a non-empty string")
    v = v.strip()
    if len(v) > LABEL_MAX:
        raise VisualError(f"{where}label is {len(v)} chars, max {LABEL_MAX}: {v!r}")
    for ch in v:
        if ch in "<>":
            raise VisualError(f"{where}label must not contain < or >: {v!r}")
        if unicodedata.category(ch).startswith("C"):
            raise VisualError(f"{where}label must not contain control characters: {v!r}")
        if _is_emoji(ch):
            raise VisualError(f"{where}label must not contain emoji: {v!r}")
    return {"label": v}


def _is_emoji(ch: str) -> bool:
    o = ord(ch)
    return (o >= 0x1F000 or 0x2600 <= o <= 0x27BF or 0x2B00 <= o <= 0x2BFF
            or 0xFE00 <= o <= 0xFE0F or o in (0x200D, 0x20E3, 0x2B50, 0x2B55, 0x3030, 0x303D))


# ----- path ---------------------------------------------------------------------------
def path_points(d: str) -> int:
    """Validate an SVG path (only M L Q C Z, absolute, numbers 0..1) and return its
    number of coordinate pairs. Raises VisualError."""
    if not isinstance(d, str) or not d.strip():
        raise VisualError("path d must be a non-empty string")
    size = len(d.encode("utf-8"))
    if size > PATH_BYTES_MAX:
        raise VisualError(f"path d is {size} bytes, max {PATH_BYTES_MAX}")
    bad = _PATH_JUNK.search(d)
    if bad:
        raise VisualError(f"path d: character {bad.group(0)!r} not allowed")
    toks = _PATH_TOKEN.findall(d)
    if "".join(toks) != re.sub(r"[\s,]+", "", d):
        raise VisualError(f"path d could not be parsed: {d[:60]!r}")
    if toks and toks[0] in ("m", "l", "q", "c", "z"):
        raise VisualError("path commands must be uppercase (absolute): M L Q C Z")
    if not toks or toks[0] != "M":
        raise VisualError("path d must start with M (absolute move-to)")
    pairs = 0
    i = 0
    while i < len(toks):
        cmd = toks[i]
        if cmd not in PATH_ARGS:
            raise VisualError(
                f"path command {cmd!r} not allowed, only M L Q C Z (uppercase, absolute)")
        i += 1
        nums: list[str] = []
        while i < len(toks) and not toks[i].isalpha():
            nums.append(toks[i])
            i += 1
        n = PATH_ARGS[cmd]
        if n == 0:
            if nums:
                raise VisualError("path command Z takes no numbers")
            continue
        if not nums or len(nums) % n:
            raise VisualError(f"path command {cmd} needs {n} numbers (x,y pairs), got {len(nums)}")
        for k, s in enumerate(nums):
            _coord(float(s), f"path {cmd} {'xy'[k % 2]}")
        pairs += len(nums) // 2
    return pairs


# ----- shapes -------------------------------------------------------------------------
def _one(raw: object, where: str = "") -> tuple[dict, int]:
    """Validate one non-multi shape -> (normalized, points)."""
    if not isinstance(raw, dict):
        raise VisualError(f"{where}shape must be a JSON object")
    t = raw.get("type")
    if t == "preset":
        name = raw.get("name")
        if name not in PRESETS:
            raise VisualError(f"{where}preset {name!r} unknown, one of: {', '.join(PRESETS)}")
        out = {"type": "preset", "name": name}
        pts = 0
    elif t == "polygon":
        points = raw.get("points")
        if not isinstance(points, list):
            raise VisualError(f"{where}polygon points must be a list of [x,y]")
        closed = raw.get("closed", True)
        if not isinstance(closed, bool):
            raise VisualError(f"{where}polygon closed must be true or false")
        need = 3 if closed else 2
        if len(points) < need:
            raise VisualError(
                f"{where}{'closed ' if closed else 'open '}polygon needs at least {need} points, "
                f"got {len(points)}")
        norm = []
        for k, p in enumerate(points):
            if not isinstance(p, (list, tuple)) or len(p) != 2:
                raise VisualError(f"{where}polygon point {k + 1} must be [x,y], got {p!r}")
            norm.append([_coord(p[0], f"{where}polygon point {k + 1} x"),
                         _coord(p[1], f"{where}polygon point {k + 1} y")])
        out = {"type": "polygon", "points": norm, "closed": closed}
        pts = len(norm)
    elif t == "path":
        d = raw.get("d")
        pts = path_points(d)
        out = {"type": "path", "d": d.strip()}
    elif t == "multi":
        raise VisualError(f"{where}multi cannot be nested")
    else:
        raise VisualError(f"{where}type must be preset, polygon, path or multi, got {t!r}")
    out.update(_hold(raw, where))
    out.update(_label(raw, where))
    return out, pts


def validate_shape(raw: object) -> dict:
    """Contract check like the server. Returns the normalized shape, raises VisualError."""
    if isinstance(raw, dict) and raw.get("type") == "multi":
        items = raw.get("items")
        if not isinstance(items, list) or not items:
            raise VisualError("multi items must be a non-empty list")
        if len(items) > MULTI_MAX:
            raise VisualError(f"multi has {len(items)} items, max {MULTI_MAX}")
        norm, pts = [], 0
        for k, it in enumerate(items):
            s, n = _one(it, f"multi item {k + 1}: ")
            norm.append(s)
            pts += n
        shape = {"type": "multi", "items": norm}
        shape.update(_hold(raw, "multi "))
        shape.update(_label(raw, "multi "))
    else:
        shape, pts = _one(raw)
    if pts > POINTS_MAX:
        raise VisualError(f"shape has {pts} points, max {POINTS_MAX}")
    size = len(json.dumps(shape, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    if size > BYTES_MAX:
        raise VisualError(f"shape is {size} bytes as JSON, max {BYTES_MAX}")
    return shape


def validate_emotion(raw: object) -> dict:
    """'joy' or {"label":"joy","valence":..,"arousal":..} -> full contract form."""
    if isinstance(raw, str):
        raw = {"label": raw.strip().lower()}
    if not isinstance(raw, dict):
        raise VisualError("emotion must be a label or an object {label, valence, arousal}")
    label = raw.get("label")
    if label not in EMOTIONS:
        raise VisualError(f"emotion {label!r} unknown, one of: {', '.join(EMOTIONS)}")
    dv, da = EMOTIONS[label]
    v = raw.get("valence", dv)
    a = raw.get("arousal", da)
    return {"label": label,
            "valence": round(_num(v, "emotion valence", -1.0, 1.0), 2),
            "arousal": round(_num(a, "emotion arousal", 0.0, 1.0), 2)}


# ----- CLI parsing --------------------------------------------------------------------
def parse_points(text: str) -> list[list[float]]:
    """'0.1,0.9 0.5,0.1 0.9,0.9' -> [[0.1,0.9],[0.5,0.1],[0.9,0.9]] (numbers only;
    ranges are checked by validate_shape)."""
    pts = []
    for k, tok in enumerate(text.split()):
        parts = tok.split(",")
        if len(parts) != 2:
            raise VisualError(f"--polygon point {k + 1} {tok!r} must be x,y (e.g. 0.5,0.1)")
        try:
            x, y = float(parts[0]), float(parts[1])
        except ValueError:
            raise VisualError(f"--polygon point {k + 1} {tok!r}: x and y must be numbers") from None
        pts.append([x, y])
    if not pts:
        raise VisualError("--polygon needs points like '0.1,0.9 0.5,0.1 0.9,0.9'")
    return pts


def parse_json_arg(text: str, flag: str) -> object:
    try:
        return json.loads(text)
    except ValueError as e:
        raise VisualError(f"{flag} is not valid JSON: {e}") from None


def parse_emotion_arg(text: str | None) -> dict | None:
    """--emotion LABEL or --emotion '{"label":..,"valence":..}'."""
    if text is None:
        return None
    t = text.strip()
    return validate_emotion(parse_json_arg(t, "--emotion") if t.startswith("{") else t)


def shape_from_args(*, presets=(), polygons=(), paths=(), json_text: str | None = None,
                    open_: bool = False, label: str | None = None,
                    hold_ms: int | None = None) -> dict:
    """Build + validate the shape for `show`. Several --preset/--polygon/--path -> multi."""
    items: list[dict] = []
    items += [{"type": "preset", "name": n} for n in presets]
    items += [{"type": "polygon", "points": parse_points(p), "closed": not open_}
              for p in polygons]
    items += [{"type": "path", "d": d} for d in paths]
    if json_text is not None:
        if items:
            raise VisualError("--json cannot be combined with --preset/--polygon/--path")
        shape = parse_json_arg(json_text, "--json")
        if not isinstance(shape, dict):
            raise VisualError("--json must be a shape object")
        shape = dict(shape)
    elif not items:
        raise VisualError("nothing to show: give --preset, --polygon, --path or --json")
    elif len(items) == 1:
        shape = items[0]
    else:
        shape = {"type": "multi", "items": items}
    if hold_ms is not None:
        if shape.get("type") == "multi" and json_text is None:
            for it in shape["items"]:
                it["hold_ms"] = hold_ms
        else:
            shape["hold_ms"] = hold_ms
    if label is not None:
        shape["label"] = label
    return validate_shape(shape)


def say_fields(shape_preset: str | None, shape_json: str | None,
               emotion: str | None) -> dict:
    """Optional `say` fields {shape, emotion} (validated), empty when none given."""
    out: dict = {}
    if shape_preset is not None and shape_json is not None:
        raise VisualError("say: use --shape-preset or --shape-json, not both")
    if shape_preset is not None:
        out["shape"] = validate_shape({"type": "preset", "name": shape_preset})
    elif shape_json is not None:
        out["shape"] = validate_shape(parse_json_arg(shape_json, "--shape-json"))
    emo = parse_emotion_arg(emotion)
    if emo is not None:
        out["emotion"] = emo
    return out


def payload(shape: dict, emotion: dict | None = None) -> dict:
    """operator.visual / POST /api/bridge/visual body."""
    body: dict = {"shape": shape}
    if emotion is not None:
        body["emotion"] = emotion
    return body
