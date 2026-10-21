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

Router-Fund 21.10.: the server lists them after all (RPC vh.decisions, bridge
GET /api/bridge/decisions): every open question of the call, the last decided ones (`recent`)
and every helper board of the room (`boards`). `merged` puts that listing over this ledger:
the server wins for questions it knows, the ledger adds what only it saw (own board, boards of
other operators, answers of the last seconds). Without the server listing: the ledger alone
plus `note`.
"""
from __future__ import annotations

import time

KEEP = 60               # decisions kept (open ones are never dropped first)
RECENT_DECIDED = 15     # decided ones in the listing
BOARDS_KEEP = 40
NOTE = ("open questions of remote helpers (relay from another machine, MCP, bridge) are not "
        "visible here before they are answered: the server has no decision listing for "
        "operators (delta.decisions goes to the humans only)")


SERVER_NOTE = "server listing unavailable ({why}), showing what this join saw itself: " + NOTE


def _rows(v: object) -> list[dict]:
    return [r for r in v if isinstance(r, dict)] if isinstance(v, list) else []


def _cleared(b: dict) -> bool:
    """An emptied board (helper removed it) is gone on the server too."""
    return not (b.get("doing") or b.get("open") or b.get("done"))


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

    def merged(self, server: dict | None, only_open: bool = False) -> dict:
        """Router-Fund 21.10.: the server's listing ({ok, open, recent?, boards?}) over this ledger."""
        local = self.listing(only_open=only_open)
        if not isinstance(server, dict) or not server.get("ok"):
            why = "?"
            if isinstance(server, dict):
                why = str(server.get("status") or server.get("error") or server.get("detail") or "?")
            local["note"] = SERVER_NOTE.format(why=why[:80])
            local["source"] = "join"
            return local
        srv_open = _rows(server.get("open"))
        srv_recent = _rows(server.get("recent"))
        seen_recent = {str(r.get("id")) for r in srv_recent}
        # decided here a moment ago, the server row may still say open: the answer wins
        done_here = {it["id"] for it in local.get("recent") or [] if it.get("state") == "decided"}
        srv_open = [r for r in srv_open if str(r.get("id")) not in done_here]
        out: dict = {"ok": True, "type": "board", "source": "server", "open": srv_open}
        if server.get("truncated"):
            out["truncated"] = True
        if only_open:
            return out
        recent = srv_recent + [it for it in local.get("recent") or [] if it["id"] not in seen_recent]
        recent.sort(key=lambda r: r.get("decided_age_s") if isinstance(r.get("decided_age_s"), int)
                    else 0)
        out["recent"] = recent[:RECENT_DECIDED]
        out["own_board"] = local.get("own_board")
        srv_boards = _rows(server.get("boards"))
        names = {str(b.get("from")) for b in srv_boards}
        now = self.clock()
        extra = [{**b, "age_s": max(0, int(now - b["at"]))} for b in local.get("boards") or []
                 if str(b.get("from")) not in names and not _cleared(b)]
        out["boards"] = srv_boards + extra
        return out
