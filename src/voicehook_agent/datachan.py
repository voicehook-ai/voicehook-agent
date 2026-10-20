"""Agent-to-agent data from the CLI (0.21.4): `voicehook-agent send-data`, and incoming
`type: data` events in `next` / `listen`.

Same wire as the server's data channel (voicehook-v4 datachannel.py, MCP `send_data`,
POST /api/bridge/data): topic `operator.data`, only operator -> operator, published with
destination_identities (never broadcast, never spoken, never to Delta or the humans):

    {id, from, from_identity, to, to_identities, kind: json|text|file, name?, mime?,
     payload | (url, file_id, size), encoding?: base64, ts}

* Bridge transport: the join POSTs /api/bridge/data (the server resolves the targets and
  builds the packet); a file larger than the inline limit goes up first with POST
  /api/bridge/upload (raw body) and is sent as `file_id`.
* WebRTC: the join resolves the targets from the room (other operators: vh.role=agent, never
  the voicebot kind 4) and publishes the packet itself. Inline only (MAX_PAYLOAD_BYTES for
  the whole packet): the file store lives in the server's bridge, so a larger file needs
  `join --transport bridge` (the error says so).

Incoming: a WebRTC packet counts only from an operator in the room and only when its
to_identities name this join; the bridge delivers the server's ready event. Either way
`next` gets {type: "data", from, from_identity, kind, id, name?, mime?, size?, payload | url,
file_id, summary}; `listen` wakes on it. Content is never logged.
"""
from __future__ import annotations

import base64
import json
import re
import secrets
import time
from typing import Any

TOPIC = "operator.data"
KINDS = ("json", "text", "file")
MAX_PAYLOAD_BYTES = 15_000         # server bridge.MAX_PAYLOAD_BYTES (whole packet)
FILE_MAX_BYTES = 10 * 1024 * 1024  # server datachannel.FILE_MAX_BYTES
NAME_MAX = 200
_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_FILE_ID_RE = re.compile(r"^[0-9a-f]{32}$")


class DataError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _attrs(p: Any) -> dict:
    return dict(getattr(p, "attributes", None) or {})


def _kind(p: Any) -> int:
    try:
        return int(getattr(p, "kind", 0))
    except (TypeError, ValueError):
        return 0


def is_operator(p: Any) -> bool:
    return _attrs(p).get("vh.role") == "agent" and _kind(p) != 4


def label(p: Any) -> str:
    a = _attrs(p)
    return str(a.get("vh.name") or getattr(p, "name", "") or getattr(p, "identity", "") or "Agent")


def _names(p: Any) -> set[str]:
    a = _attrs(p)
    out = {str(getattr(p, "identity", "") or ""), label(p), str(a.get("vh.name") or "")}
    return {n.strip().lower() for n in out if n and n.strip()}


def resolve_targets(participants, to: str) -> list[Any]:
    """'*' = every other operator; else identity or label (case-insensitive). 404 / 409 like
    the server."""
    ops = [p for p in participants if is_operator(p)]
    want = (to or "").strip()
    if not want:
        raise DataError(422, 'to is required: a label, an identity or "*"')
    if want == "*":
        if not ops:
            raise DataError(404, "no other agent in this call")
        return sorted(ops, key=lambda p: p.identity)
    exact = [p for p in ops if p.identity == want]
    if exact:
        return exact
    hits = [p for p in ops if want.lower() in _names(p)]
    if not hits:
        avail = sorted({label(p) for p in ops})
        raise DataError(404, f"unknown target {want!r}; agents in this call: {avail}" if avail
                        else "no other agent in this call")
    if len(hits) > 1:
        raise DataError(409, f"label {want!r} is ambiguous, use the identity: "
                             f"{sorted(p.identity for p in hits)}")
    return hits


def clean_name(name: object) -> str:
    s = "".join(ch for ch in str(name or "") if ch.isprintable())
    return re.split(r"[\\/]", s)[-1].strip()[:NAME_MAX]


def build_packet(*, sender_identity: str, sender_label: str, to: str, targets: list[str],
                 kind: str, payload: Any, name: str = "", mime: str = "",
                 msg_id: str | None = None) -> tuple[dict, int]:
    """The operator.data packet and the payload size; 413 when it does not fit inline."""
    if kind not in KINDS:
        raise DataError(422, f"kind must be one of {list(KINDS)}")
    if msg_id is not None and not _ID_RE.match(msg_id):
        raise DataError(422, "id: 1-64 characters [A-Za-z0-9._-]")
    pkt: dict = {"id": msg_id or secrets.token_hex(8), "from": sender_label or sender_identity,
                 "from_identity": sender_identity, "to": to.strip(), "to_identities": list(targets),
                 "kind": kind}
    if kind == "json":
        size = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        pkt["payload"] = payload
    elif kind == "text":
        if not isinstance(payload, str):
            raise DataError(422, "kind text needs payload as a string")
        size = len(payload.encode("utf-8"))
        pkt["payload"] = payload
    else:
        if not isinstance(payload, (bytes, bytearray)):
            raise DataError(422, "kind file needs the file's bytes")
        size = len(payload)
        pkt.update({"payload": base64.b64encode(bytes(payload)).decode("ascii"), "size": size,
                    "encoding": "base64"})
    nm = clean_name(name)
    if nm:
        pkt["name"] = nm
    if mime:
        pkt["mime"] = str(mime)[:100]
    pkt["ts"] = round(time.time(), 3)
    if len(json.dumps(pkt, ensure_ascii=False).encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise DataError(413, f"data packet too large (max {MAX_PAYLOAD_BYTES} bytes inline)")
    return pkt, size


def fits_inline(raw: bytes, name: str = "", mime: str = "") -> bool:
    """Would a file of these bytes go inline (base64 + packet overhead)?"""
    return 4 * ((len(raw) + 2) // 3) + len(name) + len(mime) + 600 <= MAX_PAYLOAD_BYTES


def human_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def summary(ev: dict) -> str:
    what = ev.get("name") or {"json": "JSON", "text": "Text", "file": "Datei"}.get(str(ev.get("kind")), "Daten")
    size = ev.get("size")
    if not isinstance(size, int):
        p = ev.get("payload")
        size = len(json.dumps(p, ensure_ascii=False).encode("utf-8")) if p is not None and \
            not isinstance(p, str) else len(str(p or "").encode("utf-8"))
    where = " (Download: url, Bearer der Bridge-Session)" if ev.get("url") else ""
    return f"Daten von {ev.get('from') or '?'}: {what} ({human_size(size)}){where}"


def receive_event(own_identity: str, sender: Any, payload: Any) -> dict | None:
    """An incoming operator.data packet -> the `next` event, or None (dropped). `sender`: the
    participant (WebRTC) or None. A bridge transport delivers the server's ready event
    (type data) as payload."""
    if not isinstance(payload, dict):
        return None
    if payload.get("type") == "data":  # bridge: the server checked and built it already
        ev = dict(payload)
    else:
        if sender is None or not is_operator(sender) or getattr(sender, "identity", "") == own_identity:
            return None
        if payload.get("kind") not in KINDS:
            return None
        targets = payload.get("to_identities")
        if not isinstance(targets, list) or own_identity not in targets:
            return None
        ev = {"type": "data", "from": label(sender), "from_identity": sender.identity,
              "kind": payload["kind"], "id": str(payload.get("id") or "")[:64]}
        for k in ("name", "mime", "encoding"):
            if isinstance(payload.get(k), str) and payload[k]:
                ev[k] = payload[k][:NAME_MAX]
        if "file_id" in payload or "url" in payload:
            fid = str(payload.get("file_id") or "")
            if not _FILE_ID_RE.match(fid):
                return None
            ev.update({"url": f"/api/bridge/file/{fid}", "file_id": fid})
        elif "payload" in payload:
            ev["payload"] = payload["payload"]
        else:
            return None
        if isinstance(payload.get("size"), int):
            ev["size"] = payload["size"]
        ev["ts"] = time.time()
    ev["summary"] = summary(ev)
    return ev
