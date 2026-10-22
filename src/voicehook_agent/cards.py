"""Cards on the call page (0.21.0): `voicehook-agent show --card text|code|link|image`.

Same card as bridge `POST /api/bridge/show` and MCP `show` (server voicehook-v4 cards.py):
text, code, a link or an image, never spoken. A card shows up as a small preview top left;
`--panel cards` opens the card view, `board`/`log`/`decisions` open those panels, `none`
closes all (topic delta.ui, only to the humans in the call). `--large` opens the card view big
in the middle with this card at full size (diagrams, swimlanes; implies `--panel cards`).

WebRTC join: the CLI publishes the card itself on topic operator.card (the page takes cards
from every agent participant, the voicebot keeps the last 20 for a browser that reloads).
An image is too large for the data channel: the join stores it on the server first
(POST /api/card/image, Bearer = its own LiveKit token, no new credential) and puts the
signed, room-bound URL into the card. Bridge join: POST /api/bridge/show, same body.

Image arrived (0.23.1, live 21.10.2026: the CLI said ok, the human saw nothing): every call
page loads an image card and answers the sender on topic vh.card_ack {id, ok} (ok false: the
image did not load). The join waits up to ACK_WAIT_S for the humans in the call and answers
ok only when at least one browser loaded the image; otherwise ok false with the reason
(not_shown: no browser confirmed or the image did not load; no_human: nobody in the call).

Limits (checked here before anything is sent, again in the join, and by the server):
text max 4000 chars, title max 80, SVG max 300 KB (draws anything, but no <script>,
<foreignObject>, on* attributes or external references), PNG/JPEG/WebP max 1 MB, link one
http(s) URL. Too large or wrong: exit 2, nothing sent, never cut.
"""
from __future__ import annotations

import base64
import binascii
import json
import os
import re
import secrets
import time
from urllib.parse import urlparse

TOPIC = "operator.card"
UI_TOPIC = "delta.ui"
IMAGE_PATH = "/api/card/image"
KINDS = ("text", "code", "link", "image")
PANELS = ("cards", "board", "log", "decisions", "none")
UI_PANELS = ("board", "log", "cards", "decisions")  # `none` closes these four
TEXT_MAX = 4000
TITLE_MAX = 80
SVG_MAX = 300 * 1024
RASTER_MAX = 1024 * 1024
PACKET_MAX = 14_000
ACK_TOPIC = "vh.card_ack"
ACK_WAIT_S = 15.0      # a phone may need a while for a 1 MB image
ACK_GRACE_S = 2.0      # after the first ok: wait this long for the other humans

_SVG_TAG = re.compile(rb"<svg[\s>]", re.IGNORECASE)


def is_svg(data: bytes) -> bool:
    """Starts with an <svg> element (after an optional XML declaration, comments and a
    DOCTYPE), like the server's check; a plain scan, no backtracking regex."""
    s = data.lstrip()
    if s[:5].lower() == b"<?xml":
        end = s.find(b"?>")
        if end < 0:
            return False
        s = s[end + 2:].lstrip()
    while s.startswith(b"<!--"):
        end = s.find(b"-->", 4)
        if end < 0:
            return False
        s = s[end + 3:].lstrip()
    if s[:9].upper() == b"<!DOCTYPE":
        end = s.find(b">")
        if end < 0:
            return False
        s = s[end + 1:].lstrip()
    return bool(_SVG_TAG.match(s))


# SVG may draw anything but never run or load anything (server voicehook-v4 cards.check_svg)
_SVG_BAD = (
    (re.compile(rb"<\s*script", re.IGNORECASE), "no <script>"),
    (re.compile(rb"<\s*foreignObject", re.IGNORECASE), "no <foreignObject>"),
    (re.compile(rb"<!ENTITY", re.IGNORECASE), "no entities"),
    (re.compile(rb"[\s\"'/]on[a-z]+\s*=", re.IGNORECASE), "no on* event attributes"),
    (re.compile(rb"javascript:", re.IGNORECASE), "no javascript:"),
    (re.compile(rb"@import", re.IGNORECASE), "no @import"),
)
_SVG_REF = re.compile(rb"""href\s*=\s*["']([^"']*)|url\(([^)]*)\)""", re.IGNORECASE)
_SVG_REF_OK = re.compile(rb"^(#|data:image/(png|jpeg|webp|gif)[;,])", re.IGNORECASE)


class CardError(ValueError):
    """Invalid card: nothing sent (client exit 2, join answers type invalid)."""


def _clean(v: object) -> str:
    """Control chars out except newline and tab (code needs its indentation)."""
    s = str(v if v is not None else "")
    return "".join(ch for ch in s if ch in "\n\t" or ch.isprintable())


def check_link(url: str) -> str:
    u = url.strip()
    p = urlparse(u)
    if p.scheme.lower() not in ("http", "https") or not p.netloc or any(c.isspace() for c in u):
        raise CardError("link must be one http(s) URL (no javascript:, data: or other schemes)")
    return u


def check_svg(data: bytes) -> None:
    """CardError when the SVG would run or load something."""
    for rx, what in _SVG_BAD:
        if rx.search(data):
            raise CardError(f"svg must be a plain drawing: {what}")
    for m in _SVG_REF.finditer(data):
        ref = (m.group(1) if m.group(1) is not None else m.group(2)).strip().strip(b"\"'").strip()
        if ref and not _SVG_REF_OK.match(ref):
            raise CardError("svg must be a plain drawing: references only to #ids or data:image, "
                            "nothing external")


def image_kind(data: bytes) -> str:
    """Content type from the magic bytes (never from the file name), else CardError."""
    if is_svg(data):
        if len(data) > SVG_MAX:
            raise CardError(f"svg max {SVG_MAX // 1024} KB")
        check_svg(data)
        return "image/svg+xml"
    if len(data) > RASTER_MAX:
        raise CardError(f"image max {RASTER_MAX // 1024} KB")
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    raise CardError("image must be an svg, png, jpeg or webp file")


def image_field(data: bytes) -> str:
    """The `image` value the server takes: SVG as text, raster as base64."""
    ctype = image_kind(data)
    if ctype == "image/svg+xml":
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return base64.b64encode(data).decode("ascii")
    return base64.b64encode(data).decode("ascii")


def _image_bytes(image: str) -> bytes:
    s = (image or "").strip()
    if not s:
        raise CardError("image is empty")
    if s.startswith("<"):
        return s.encode("utf-8")
    try:
        return base64.b64decode(s, validate=True)
    except (binascii.Error, ValueError):
        raise CardError("image must be svg text or base64 png/jpeg/webp") from None


def validate(req: dict) -> dict:
    """Check a card request {kind?, title?, text?, image?, panel?, large?} -> the clean body
    for POST /api/bridge/show (only the fields that are set). CardError when invalid."""
    panel = req.get("panel")
    panel = str(panel).strip().lower() if panel is not None else None
    if panel is not None and panel not in PANELS:
        raise CardError(f"panel must be one of {' '.join(PANELS)}")
    large = req.get("large") is True
    if large:
        if panel not in (None, "cards"):
            raise CardError("--large only with --panel cards")
        panel = "cards"
    kind = req.get("kind")
    image = req.get("image")
    body: dict = {}
    if kind is None and image is None and not str(req.get("text") or "").strip():
        if panel is None:
            raise CardError("nothing to show: give --card (with --text or --file) and/or --panel")
        return {"panel": panel, **({"large": True} if large else {})}
    kind = str(kind or ("image" if image else "text")).strip().lower()
    if kind not in KINDS:
        raise CardError(f"card must be one of {' '.join(KINDS)}")
    title = " ".join(_clean(req.get("title")).split())
    if len(title) > TITLE_MAX:
        raise CardError(f"title max {TITLE_MAX} characters")
    text = _clean(req.get("text"))
    if kind == "image":
        if text.strip():
            raise CardError("--card image takes --file, not --text")
        image_kind(_image_bytes(str(image or "")))
        body.update(kind=kind, image=str(image).strip())
    else:
        if image:
            raise CardError("image only with --card image")
        if not text.strip():
            raise CardError("text is empty: give --text or --file")
        if len(text) > TEXT_MAX:
            raise CardError(f"text max {TEXT_MAX} characters")
        if kind == "link":
            text = check_link(text)
        body.update(kind=kind, text=text)
    if title:
        body["title"] = title
    if panel is not None:
        body["panel"] = panel
    if large:
        body["large"] = True
    return body


def request_from_args(*, kind: str | None, title: str | None, text: str | None,
                      file: str | None, panel: str | None, large: bool = False,
                      stdin=None) -> dict:
    """`show --card KIND [--title T] [--text T | --file PATH] [--panel P] [--large]` -> the join
    request {cmd: card, ...}. `--text -` reads stdin. Reads the file, checks everything."""
    if text is not None and file is not None:
        raise CardError("--text and --file cannot be combined")
    if kind is None and (title is not None or text is not None or file is not None):
        raise CardError("--title/--text/--file need --card text|code|link|image")
    req: dict = {"kind": kind, "title": title, "panel": panel, "large": True if large else None}
    if text == "-":
        text = stdin.read() if stdin is not None else ""
    if kind == "image":
        if text is not None:
            raise CardError("--card image takes --file, not --text")
        if file is None:
            raise CardError("--card image needs --file PATH (svg, png, jpeg or webp)")
        with open(file, "rb") as fh:
            data = fh.read(max(SVG_MAX, RASTER_MAX) + 1)
        req["image"] = image_field(data)
    elif file is not None:
        if os.path.getsize(file) > TEXT_MAX * 4:  # 4000 chars are at most 16 KB UTF-8
            raise CardError(f"text max {TEXT_MAX} characters")
        with open(file, "rb") as fh:
            raw = fh.read()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise CardError(f"--file must be UTF-8 text for --card {kind}") from None
        req["text"] = text
    elif text is not None:
        req["text"] = text
    return {"cmd": "card", **validate({k: v for k, v in req.items() if v is not None})}


def make_card(*, seq: int, body: dict, url: str | None = None, mime: str | None = None,
              cid: str | None = None, now: float | None = None) -> dict:
    """The operator.card packet {id, seq, kind, title, text, at, url?, mime?} (same shape the
    server builds for the bridge)."""
    card = {"id": cid or secrets.token_hex(8), "seq": seq, "kind": body["kind"],
            "title": body.get("title", ""), "text": body.get("text", ""),
            "at": round(now if now is not None else time.time(), 3)}
    if url is not None:
        card["url"] = url
        card["mime"] = mime or ""
    if len(json.dumps(card, ensure_ascii=False).encode("utf-8")) + 400 > PACKET_MAX:
        raise CardError(f"card too large for the call (max {PACKET_MAX} bytes UTF-8)")
    return card


def ui_packets(panel: str, *, large: bool = False, cid: str | None = None) -> list[dict]:
    """delta.ui packets for `panel` (none = close all four); large: the card view big with
    card `cid` (else the newest)."""
    if panel == "none":
        return [{"panel": p, "open": False, "via": "agent"} for p in UI_PANELS]
    pkt: dict = {"panel": panel, "open": True, "via": "agent"}
    if large and panel == "cards":
        pkt["large"] = True
        if cid:
            pkt["id"] = cid
    return [pkt]


class Acks:
    """vh.card_ack answers of the call pages per image card id (only ids we wait for)."""

    def __init__(self) -> None:
        self._w: dict[str, dict] = {}

    def expect(self, cid: str) -> None:
        import asyncio

        self._w[cid] = {"ok": set(), "bad": set(), "ev": asyncio.Event()}

    def note(self, payload: object, who: str) -> None:
        cid = payload.get("id") if isinstance(payload, dict) else None
        w = self._w.get(cid) if isinstance(cid, str) else None
        if w is None or not who:
            return
        (w["ok"] if payload.get("ok") is True else w["bad"]).add(who)
        w["ev"].set()

    async def wait(self, cid: str, humans: set[str], timeout: float | None = None,
                   grace: float | None = None) -> tuple[set[str], set[str]]:
        """-> (loaded, failed) identities, once every human answered, `grace` s after the first
        ok, or after `timeout`. Forgets the id."""
        import asyncio

        w = self._w.get(cid)
        if w is None or not humans:   # nobody in the call who could load it
            self._w.pop(cid, None)
            return set(), set()
        loop = asyncio.get_running_loop()
        timeout = ACK_WAIT_S if timeout is None else timeout
        grace = ACK_GRACE_S if grace is None else grace
        end = loop.time() + timeout
        first_ok: float | None = None
        try:
            while True:
                if (w["ok"] | w["bad"]) >= humans:
                    break
                now = loop.time()
                if w["ok"] & humans and first_ok is None:
                    first_ok = now
                stop = end if first_ok is None else min(end, first_ok + grace)
                if now >= stop:
                    break
                w["ev"].clear()
                try:
                    await asyncio.wait_for(w["ev"].wait(), timeout=stop - now)
                except asyncio.TimeoutError:
                    pass
            return set(w["ok"]) & humans, set(w["bad"]) & humans
        finally:
            self._w.pop(cid, None)


def ack_result(res: dict, loaded: set[str], failed: set[str], humans: int) -> dict:
    """The join's answer for an image card after waiting for the call pages."""
    if loaded:
        out = {**res, "shown": len(loaded)}
        if failed:
            out["not_loaded"] = len(failed)
        return out
    if humans == 0:
        return {**res, "ok": False, "type": "no_human",
                "error": "no human in the call: the image was not shown to anyone (the card is kept "
                         "and shows up when someone opens the call page)"}
    if failed:
        return {**res, "ok": False, "type": "not_shown",
                "error": "the image did not load in the call page (server lost it, e.g. after a "
                         "restart): send it again"}
    return {**res, "ok": False, "type": "not_shown",
            "error": f"no call page confirmed the image within {int(ACK_WAIT_S)} s (page not "
                     "reloaded since the update, or offline): ask the human to reload, then send again"}


FROM_MAX = 80


def relay_card(card: object, seq: int) -> dict | None:
    """0.23.1 helper card from the server hub (pump kind card): checked again before we publish
    it as ourselves -> the operator.card packet with our seq, or None (dropped)."""
    if not isinstance(card, dict):
        return None
    cid, kind = card.get("id"), card.get("kind")
    if not isinstance(cid, str) or not re.fullmatch(r"[0-9a-f]{16}", cid) or kind not in KINDS:
        return None
    frm = card.get("from")
    if not isinstance(frm, str) or not frm.strip() or len(frm) > FROM_MAX:
        return None
    out = {"id": cid, "seq": seq, "kind": kind, "title": str(card.get("title") or "")[:TITLE_MAX],
           "text": str(card.get("text") or "")[:TEXT_MAX], "from": frm,
           "at": card.get("at") if isinstance(card.get("at"), (int, float)) else round(time.time(), 3)}
    if kind == "image":
        if not image_url_ok(card.get("url"), cid):
            return None
        out["url"] = card["url"]
        out["mime"] = str(card.get("mime") or "")[:40]
    if len(json.dumps(out, ensure_ascii=False).encode("utf-8")) > PACKET_MAX:
        return None
    return out


def image_url_ok(url: object, cid: object) -> bool:
    """The page only loads /api/card/<room>/<id>?... ; anything else from the server is refused."""
    return (isinstance(url, str) and isinstance(cid, str) and bool(re.fullmatch(r"[0-9a-f]{16}", cid))
            and bool(re.match(r"^/api/card/[A-Za-z0-9_-]+/" + cid + r"\?", url)))
