"""`voicehook-agent board` / `decide --list` (0.21.4): what is on the call's boards, for the
operator.

Live (20.10.2026): Oliver asked "what does #15 mean?" and the operator could not tell: it
only sees a helper's decision once the human answered (helper_decision). The server sends
the Decision Board state (delta.decisions) to the humans only and has no listing for
operators, so the join keeps its own ledger of what passes through it:

* decisions: its own `decide` and the local helpers' (local relay) with number, asking
  label, question, options, recommend and the hidden context / recent / faq, from the moment
  they are created; answers (operator.decision, helper_decision) mark them decided. Remote
  helpers (relay on another machine, MCP, bridge) ask through the server directly: the join
  sees those only once answered (helper_decision), never while open.
* boards: its own last board and every operator.status it receives from others (helper
  boards with sub + state, other operators), newest per sender.

In memory of the join only, for the duration of the call; nothing is written to disk.
"""
from __future__ import annotations

import time

KEEP = 60               # decisions kept (open ones are never dropped first)
RECENT_DECIDED = 15     # decided ones in the listing
BOARDS_KEEP = 40
NOTE = ("open questions of remote helpers (relay from another machine, MCP, bridge) are not "
        "visible here before they are answered: the server has no decision listing for "
        "operators (delta.decisions goes to the humans only)")


class Ledger:
    def __init__(self, clock=time.time) -> None:
        self.clock = clock
        self.items: dict[str, dict] = {}
        self.boards: dict[str, dict] = {}
        self.own_board: dict | None = None

    # ----- decisions -----------------------------------------------------------------
    def created(self, req: dict, out: dict, frm: str = "") -> None:
        """A decide request and its answer {ok, id, number}: create / change / delete."""
        if not isinstance(out, dict) or not out.get("ok"):
            return
        did = str(out.get("id") or req.get("id") or "")
        if not did:
            return
        if req.get("delete"):
            it = self.items.get(did)
            if it is not None:
                it.update(state="deleted", closed_at=self.clock())
            return
        it = self.items.get(did)
        if it is None:
            it = {"id": did, "state": "open", "at": self.clock(), "from": frm or "self"}
            self.items[did] = it
        if out.get("number") is not None:
            it["number"] = out["number"]
        for k in ("question", "options", "recommend", "context", "recent", "faq"):
            if req.get(k) is not None:
                it[k] = req[k]
        self._trim()

    def answered(self, ev: dict, label: str = "") -> None:
        """operator.decision / helper_decision: the human decided."""
        did = str(ev.get("id") or "")
        if not did:
            return
        it = self.items.get(did)
        if it is None:  # a remote helper's question: first seen with its answer
            it = {"id": did, "at": self.clock(), "from": label or str(ev.get("from") or "?")}
            if ev.get("question"):
                it["question"] = ev["question"]
            self.items[did] = it
        if ev.get("number") is not None:
            it.setdefault("number", ev["number"])
        it.update(state="decided", decided_at=self.clock(),
                  **{k: ev[k] for k in ("verdict", "option", "text", "by") if ev.get(k) is not None})
        self._trim()

    def _trim(self) -> None:
        if len(self.items) <= KEEP:
            return
        closed = sorted((it for it in self.items.values() if it.get("state") != "open"),
                        key=lambda it: it.get("decided_at") or it.get("closed_at") or it["at"])
        for it in closed[:len(self.items) - KEEP]:
            self.items.pop(it["id"], None)

    # ----- boards --------------------------------------------------------------------
    def board(self, sender: str, payload: dict, own: bool = False) -> None:
        if not isinstance(payload, dict):
            return
        if own:
            self.own_board = {**payload, "at": self.clock()}
            return
        key = str(payload.get("sub") or sender or "?")
        self.boards.pop(key, None)
        self.boards[key] = {"from": key, **{k: v for k, v in payload.items() if k != "sub"},
                            "at": self.clock()}
        while len(self.boards) > BOARDS_KEEP:
            self.boards.pop(next(iter(self.boards)))

    # ----- listing -------------------------------------------------------------------
    def listing(self, only_open: bool = False) -> dict:
        def num(it: dict) -> int:
            n = it.get("number")
            return n if isinstance(n, int) and not isinstance(n, bool) else 1 << 30
        opened = sorted((it for it in self.items.values() if it.get("state") == "open"), key=num)
        out: dict = {"ok": True, "type": "board", "open": opened}
        if not only_open:
            done = sorted((it for it in self.items.values() if it.get("state") != "open"),
                          key=lambda it: -(it.get("decided_at") or it.get("closed_at") or it["at"]))
            out["recent"] = done[:RECENT_DECIDED]
            out["own_board"] = self.own_board
            out["boards"] = list(reversed(list(self.boards.values())))
        out["note"] = NOTE
        return out
