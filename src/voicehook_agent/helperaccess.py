"""Helper access for every main agent (0.17.0, server voicehook-v4 /api/helper/*).

Helper sessions (another machine, a background agent) feed a call WITHOUT joining: their
own status board, their own decisions (answers only to them) and activity lines. They hold
a helper credential (relay token): data only, never say/join, no chip, never counts for the
solo gate.

Main agent joined over WebRTC (the CLI default): right after connect the join asks the
server for the credential with its LiveKit token (POST /api/helper/credential) and gets a
pump token on top. It writes ~/.voicehook/relay.json (0600) with the credential, like a
bridge join, and runs the pump: GET /api/helper/pump long-polls the helpers' packets, the
join publishes them as itself (operator.status with `sub`, vh.decide with `sender`, the
feed line `relay: Helfer <label>: ...` in its own activity.log) and posts the human's
answers to helper questions back (POST /api/helper/pump/answer). On leave it ends the hub
(POST /api/helper/pump/end): every helper credential of this call answers 410.

Helper side: `decide`, `status` and `relay-next` of a session that is not in the call
(relayops.py, 0.16.0) use that relay.json like the one of a bridge join. `helper-import`
writes the credential on another machine from a credential piped in over ssh (never
through a chat): `voicehook-agent helper-credential --label crown | ssh crown
'voicehook-agent helper-import --as crown'`. 0.22.2: one entry per helper session
(~/.voicehook/helpers/<name>.json), never relay.json (see import_credential).
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from collections.abc import Awaitable, Callable

from . import multisession as vmulti

CREDENTIAL_PATH = "/api/helper/credential"
DEFAULT_HELPER = "helper"  # 0.22.2: entry name when neither --as, a session label nor a label
PUMP_WAIT_S = 25.0
HTTP_TIMEOUT_S = 10.0
_CLOCK = re.compile(r"^\d{2}:\d{2}:\d{2} ")


def credential_from(answer: object) -> dict | None:
    """{token, url, expires_in, pump?} of a /api/helper/credential answer, else None."""
    rel = vmulti.relay_from_answer(answer)
    if rel is None:
        return None
    pump = answer.get("pump") if isinstance(answer, dict) else None
    if isinstance(pump, dict) and isinstance(pump.get("token"), str) and isinstance(pump.get("url"), str):
        rel["pump"] = {"token": pump["token"], "url": pump["url"]}
    return rel


async def mint(http, api_base: str, bearer: str, label: str, *, rotate: bool = False) -> dict | None:
    """POST /api/helper/credential (Bearer = LiveKit token of this join, or its pump token).
    The credential or None (older server: 404/405). Never raises, never prints the token."""
    try:
        r = await http.post(api_base.rstrip("/") + CREDENTIAL_PATH,
                            json={"label": label, "rotate": bool(rotate)},
                            headers={"authorization": f"Bearer {bearer}"})
    except Exception:  # noqa: BLE001
        return None
    if r.status_code != 200:
        return None
    try:
        return credential_from(r.json())
    except ValueError:
        return None


class Pump:
    """The join's side of the helper hub (one per join process, survives reconnects)."""

    def __init__(self, api_base: str, url: str, token: str) -> None:
        self.api_base = api_base.rstrip("/")
        self.url = url
        self.token = token
        self.ended = False

    def _auth(self) -> dict:
        return {"authorization": f"Bearer {self.token}"}

    async def run(self, http, *, publish_status: Callable[[dict], Awaitable[None]],
                  decide: Callable[[dict], Awaitable[dict]],
                  append_activity: Callable[[str], None], stop: asyncio.Event) -> None:
        """Long-poll until `stop` or the hub ended; a network error waits 2 s and retries."""
        while not stop.is_set() and not self.ended:
            try:
                r = await http.get(self.url, params={"timeout": PUMP_WAIT_S}, headers=self._auth(),
                                   timeout=PUMP_WAIT_S + 10)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                await _sleep(stop, 2.0)
                continue
            if r.status_code in (401, 403, 404):
                self.ended = True   # unknown pump token: the call (or its hub) is over
                return
            if r.status_code != 200:
                await _sleep(stop, 2.0)
                continue
            try:
                data = r.json()
            except ValueError:
                continue
            for item in data.get("items") or []:
                try:
                    await self._apply(http, item, publish_status, decide, append_activity)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001, S112  one bad packet never stops the pump
                    continue
            if data.get("ended"):
                self.ended = True
                return

    async def _apply(self, http, item: dict, publish_status, decide, append_activity) -> None:
        kind = item.get("kind") if isinstance(item, dict) else None
        if kind == "activity" and isinstance(item.get("line"), str):
            # server line `HH:MM:SS relay: Helfer <label>: ...`: local time like every line
            append_activity(time.strftime("%H:%M:%S ") + _CLOCK.sub("", item["line"]))
        elif kind == "status" and isinstance(item.get("payload"), dict) and item["payload"].get("sub"):
            await publish_status(item["payload"])
        elif kind == "decide" and isinstance(item.get("body"), dict) and item["body"].get("sender"):
            res = await decide(item["body"])
            await http.post(self.url + "/result", json={"id": str(item.get("id") or ""), "result": res},
                            headers=self._auth(), timeout=HTTP_TIMEOUT_S)

    async def answer(self, http, sender: str, event: dict) -> dict | None:
        """The human answered a helper's question: into that helper's inbox on the server.
        0.18.1: the server's answer (with the copy `helper_decision` for our own `next`)."""
        try:
            r = await http.post(self.url + "/answer", json={"sender": sender, "event": event},
                                headers=self._auth(), timeout=HTTP_TIMEOUT_S)
            out = r.json() if r.status_code == 200 else None
        except Exception:  # noqa: BLE001  best effort, the helper polls again
            return None
        return out if isinstance(out, dict) else None

    async def user_turn(self, http, to: str, to_key: str, text: str, speaker: str = "") -> bool:
        """0.22.0: the human spoke to a helper of this hub by name: into its inbox on the
        server (POST <pump>/user_turn). False = not delivered (unknown helper, network)."""
        try:
            r = await http.post(self.url + "/user_turn",
                                json={"to": to, "to_key": to_key, "text": text, "speaker": speaker},
                                headers=self._auth(), timeout=HTTP_TIMEOUT_S)
        except Exception:  # noqa: BLE001  best effort: the main agent gets the info copy
            return False
        return r.status_code == 200

    async def credential(self, http, label: str, rotate: bool = False) -> dict | None:
        out = await mint(http, self.api_base, self.token, label, rotate=rotate)
        if out is not None and out.get("pump"):
            self.token = out["pump"]["token"]   # rotate: new id, new pump token
        return out

    async def end(self, http) -> None:
        if self.ended:
            return
        self.ended = True
        try:
            await http.post(self.url + "/end", headers=self._auth(), timeout=3.0)
        except Exception:  # noqa: BLE001, S110  best effort: the hub also ends with the call
            pass


async def _sleep(stop: asyncio.Event, s: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=s)
    except asyncio.TimeoutError:
        pass


# --------------------------------------------------------------------------- #
# another machine: helper-import
# --------------------------------------------------------------------------- #
def import_credential(raw: str, name: str | None = None) -> dict:
    """`helper-import [--as NAME]` (0.22.2): ONE helper entry ~/.voicehook/helpers/<name>.json
    (0600) from a credential answer ({token, url, expires_in}, the join's relay.json or a
    /api/helper/credential answer) piped in, e.g. over ssh. Never relay.json any more: that
    one file was shared by every session of the machine, so the last import named all of
    them. No owner pid (another machine's pid means nothing here).
    Name and binding:
    * --as NAME: entry NAME, bound to no session; a session uses it when its
      $VOICEHOOK_SESSION_LABEL is NAME (or it is the only unbound entry);
    * else $VOICEHOOK_SESSION_LABEL, else the credential's label: bound to the agent session
      that runs the import (CLAUDE_CODE_SESSION_ID / CODEX_THREAD_ID); outside one unbound.
    Returns {path, name, bound, legacy}; legacy = a pre-0.22.2 shared relay.json exists."""
    try:
        d = json.loads(raw or "")
    except ValueError:
        raise ValueError("helper-import: stdin is not JSON") from None
    rel = vmulti.relay_from_answer(d)
    if rel is None:
        raise ValueError("helper-import: no {token, url} in the input")
    exp = rel.get("expires_in")
    if exp is None and isinstance(d, dict) and d.get("expires_at"):
        try:
            exp = max(0.0, float(d["expires_at"]) - time.time())
        except (TypeError, ValueError):
            exp = None
    room = str(d.get("room") or "") if isinstance(d, dict) else ""
    label = str(d.get("label") or "") if isinstance(d, dict) else ""
    explicit = vmulti.helper_key(name)
    if name is not None and not explicit:
        raise ValueError("helper-import --as: name must hold letters or digits")
    key = explicit or vmulti.helper_key(os.environ.get(vmulti.LABEL_ENV)) or vmulti.helper_key(label)
    key = key or DEFAULT_HELPER  # a relay.json copy without label, no session label
    owner = "" if explicit else vmulti.own_session_hash()
    path = vmulti.write_helper(key, rel["url"], rel["token"], room, exp, owner_session=owner,
                               label=label)
    return {"path": str(path), "name": key, "bound": bool(owner),
            "legacy": vmulti.is_legacy_helper(vmulti.load_main_relay())}
