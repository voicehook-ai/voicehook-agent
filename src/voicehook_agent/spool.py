"""Durable event spool of a running `join` (0.21.5).

Live calls (20.10.2026): a Claude Code agent was marked absent whenever it worked between two
`next` calls. Presence hung on the agent's own long poll: `operator.alive` only went out while
a `next` waited or had ended within 15 s, so every turn of real work meant "not reachable" for
Delta, `listening: 0` in `status`, and the Stop hook blocked.

Now the join itself is the listener: once armed (the first `say`/`next`), every event for
`next` goes into an append-only JSONL spool in the join's session dir, and the join keeps
sending `operator.alive` on its own (cli.py `_alive_loop`). `next` / `listen` / the MCP `next`
read from the spool with a cursor:

* `lease(timeout)` hands out the oldest unread event (front events such as `status_request`
  and `call_end` first, newest first, like EventQueue.put_front_nowait) with its `seq`; the
  consumer prints it and then `ack(seq)`s it. An event whose consumer dies before the ack is
  handed out again after LEASE_S, so a killed `listen` never loses an event; an acked one is
  never handed out again (cursor in `cursor.json`, written atomically).
* `get(timeout)` is the old one-shot form (lease + ack at once) for callers without an ack.
* The spool survives a restart of the join in the same session dir (self-update re-exec)
  when it is younger than RESUME_S; `clear()` deletes both files when the join leaves.

Files: `events.jsonl` + `cursor.json`, mode 0600 (O_NOFOLLOW), in the 0700 session dir. The
spool IS the event queue, so it holds the call's turns; nothing else in this module logs
event content. Disk errors never break the call: the spool then keeps working in memory.

Stdlib only.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

EVENTS_NAME = "events.jsonl"
CURSOR_NAME = "cursor.json"
LEASE_S = 15.0        # an un-acked event goes out again after this (consumer died)
RESUME_S = 600.0      # a spool left by a join restart is picked up when younger than this
COMPACT_AT = 400      # acked events kept in the file before it is rewritten
MAXLEN = 200          # unread events kept (oldest dropped, counted in `dropped`)
RECENT_KEEP = 20      # 0.21.7: acked events kept in memory after a compaction (midturn.take)
# never kept across a join restart: only for the waiting `next` of the leaving process
EPHEMERAL = frozenset({"restarting"})
# 0.21.5: events that need the agent (the Stop hook warns on them, `listen` wakes on them)
WAKE_TYPES = ("user", "decision", "helper_decision", "voicebot_left", "missed",
              "status_request", "revise", "call_end", "data", "onboarding")  # 0.21.8 onboarding
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def wanted(ev: dict, types) -> bool:
    """0.21.9: `ev` matches one of `types`; "<type>:stale" matches only stale events of that
    type (midturn shows a stale-drop revise, a normal revise stays for `listen`)."""
    typ = ev.get("type")
    return typ in types or (ev.get("stale") is True and f"{typ}:stale" in types)


def _open_private(path: Path, flags: int):
    fd = os.open(path, flags | _NOFOLLOW, 0o600)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)  # a pre-existing file or the umask never widens it
    except OSError:
        os.close(fd)
        raise
    return fd


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        tmp.unlink()
    except OSError:
        pass
    fd = _open_private(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    try:
        data = text.encode("utf-8")
        while data:
            data = data[os.write(fd, data):]
    finally:
        os.close(fd)
    os.replace(tmp, path)


class SpoolQueue:
    """Drop-in for session.EventQueue (arm / put / put_nowait / put_front_nowait / get /
    close / len / armed / closed / dropped), backed by the spool files when `dir_` is set
    (None: memory only, the same semantics)."""

    MAXLEN = MAXLEN

    def __init__(self, dir_: Path | None = None, maxlen: int | None = None,
                 armed: bool = False, clock=time.monotonic) -> None:
        self.dir = Path(dir_) if dir_ is not None else None
        self.maxlen = maxlen or self.MAXLEN
        self.armed = armed
        self.closed = False
        self.dropped = 0
        self.clock = clock
        self._items: list[dict] = []      # {"seq", "ts", "front", "event"}, seq ascending
        self._acked: set[int] = set()
        self._leases: dict[int, float] = {}  # seq -> lease deadline (clock)
        self._seq = 0
        self._cursor = 0                   # every seq <= cursor is acked
        self._cond = asyncio.Condition()
        self._disk_ok = self.dir is not None
        self.last_consumed: float | None = None  # clock of the last ack
        self._shown: dict[int, float] = {}  # 0.21.7: seq -> clock, shown to the agent mid-turn

    # ------------------------------------------------------------------ files
    @property
    def events_path(self) -> Path | None:
        return self.dir / EVENTS_NAME if self.dir is not None else None

    @property
    def cursor_path(self) -> Path | None:
        return self.dir / CURSOR_NAME if self.dir is not None else None

    def _disk_error(self, e: OSError) -> None:
        if self._disk_ok:
            print(f"[warn] event spool not writable, keeping events in memory: "
                  f"{type(e).__name__}", file=sys.stderr, flush=True)
        self._disk_ok = False

    def _append_file(self, item: dict) -> None:
        if not self._disk_ok:
            return
        try:
            fd = _open_private(self.events_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT)
            try:
                os.write(fd, (json.dumps(item, ensure_ascii=False) + "\n").encode("utf-8"))
            finally:
                os.close(fd)
        except OSError as e:
            self._disk_error(e)

    def _write_cursor(self) -> None:
        if not self._disk_ok:
            return
        above = sorted(s for s in self._acked if s > self._cursor)
        try:
            _write_atomic(self.cursor_path, json.dumps(
                {"cursor": self._cursor, "acked": above, "seq": self._seq, "at": time.time()}))
        except OSError as e:
            self._disk_error(e)

    def _compact(self) -> None:
        """Rewrite the file with the unread events only (acked ones are history; the newest
        acked ones stay in memory for midturn.take)."""
        keep = [it for it in self._items if it["seq"] not in self._acked]
        recent = [it for it in self._items if it["seq"] in self._acked][-min(RECENT_KEEP, COMPACT_AT // 2):]
        self._items = sorted(keep + recent, key=lambda it: it["seq"])
        self._acked = {s for s in self._acked if s > self._cursor} | {it["seq"] for it in recent}
        if not self._disk_ok:
            return
        try:
            _write_atomic(self.events_path, "".join(json.dumps(it, ensure_ascii=False) + "\n"
                                                    for it in keep))
        except OSError as e:
            self._disk_error(e)

    def resume(self, max_age_s: float = RESUME_S) -> int:
        """Load a spool a restarted join left in this dir (younger than `max_age_s`); an older
        one is deleted. Returns the number of unread events picked up."""
        if self.dir is None:
            return 0
        try:
            os.chmod(self.dir, 0o700)  # the spool holds the call's turns: owner only
        except OSError:
            pass
        ev_path, cur_path = self.events_path, self.cursor_path
        try:
            age = time.time() - os.lstat(ev_path).st_mtime
        except OSError:
            self.clear()
            return 0
        if age > max_age_s:
            self.clear()
            return 0
        try:
            cur = json.loads(cur_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cur = {}
        cursor = int(cur.get("cursor") or 0) if isinstance(cur, dict) else 0
        acked = {int(s) for s in (cur.get("acked") or [])} if isinstance(cur, dict) else set()
        items: list[dict] = []
        try:
            raw = ev_path.read_text(encoding="utf-8")
        except OSError:
            raw = ""
        for line in raw.splitlines():
            try:
                it = json.loads(line)
                seq = int(it["seq"])
            except (ValueError, KeyError, TypeError):
                continue  # a torn last line of a crash
            if not isinstance(it.get("event"), dict):
                continue
            self._seq = max(self._seq, seq, cursor)
            if seq <= cursor or seq in acked or it["event"].get("type") in EPHEMERAL:
                continue
            items.append(it)
        self._items = sorted(items, key=lambda it: it["seq"])
        self._cursor = max(cursor, self._seq if not self._items else self._items[0]["seq"] - 1)
        self._acked = set()
        if self._items:
            self.armed = True
        self._compact()
        self._write_cursor()
        return len(self._items)

    def clear(self) -> None:
        """Delete the spool files (the join leaves: no turn of the call stays on disk)."""
        for p in (self.events_path, self.cursor_path):
            if p is None:
                continue
            try:
                p.unlink()
            except OSError:
                pass

    # ------------------------------------------------------------------ state
    def __len__(self) -> int:
        """Unread events that are not handed out right now (= `pending`)."""
        now = self.clock()
        return sum(1 for it in self._items if it["seq"] not in self._acked
                   and self._leases.get(it["seq"], 0.0) <= now)

    def unread(self) -> int:
        """Unread events including the ones handed out but not acked yet."""
        return sum(1 for it in self._items if it["seq"] not in self._acked)

    def unread_wake(self, types=WAKE_TYPES) -> int:
        return sum(1 for it in self._items if it["seq"] not in self._acked
                   and it["event"].get("type") in types)

    def last_consumed_s(self) -> float | None:
        if self.last_consumed is None:
            return None
        return round(max(0.0, self.clock() - self.last_consumed), 1)

    def arm(self) -> None:
        self.armed = True

    # ------------------------------------------------------------------ produce
    def _add(self, event: dict, front: bool = False) -> bool:
        if not self.armed:
            return False
        before = len(self._acked)
        if front:  # one of a kind in line: an older unread one of the same type is superseded
            typ = event.get("type")
            for it in self._items:
                if (it["front"] and it["event"].get("type") == typ and it["seq"] not in self._acked
                        and it["seq"] not in self._leases):
                    self._acked.add(it["seq"])
        unread = [it for it in self._items if it["seq"] not in self._acked
                  and it["seq"] not in self._leases]
        if len(unread) >= self.maxlen:
            victim = (unread[-1] if front else
                      next((it for it in unread if not it["front"]), unread[0]))
            self._acked.add(victim["seq"])
            self.dropped += 1
        self._seq += 1
        item = {"seq": self._seq, "ts": time.time(), "front": bool(front), "event": event}
        self._items.append(item)
        self._append_file(item)
        self._advance(persist=len(self._acked) > before)  # superseded / dropped: persisted
        return True

    def _advance(self, persist: bool = False) -> None:
        """Move the cursor over contiguous acked seqs and persist it (also when `persist`: an
        ack above the cursor); compact the file now and then."""
        unacked = {it["seq"] for it in self._items if it["seq"] not in self._acked}
        moved = False
        while self._cursor < self._seq and (self._cursor + 1) not in unacked:
            self._cursor += 1  # acked (or compacted away, which only happens to acked ones)
            moved = True
        if moved or persist:
            self._write_cursor()
        if len(self._items) - len(unacked) >= COMPACT_AT:
            self._compact()

    async def put(self, event: dict) -> None:
        async with self._cond:
            if self._add(event):
                self._cond.notify_all()

    def put_nowait(self, event: dict) -> None:
        if self._add(event):
            asyncio.get_running_loop().create_task(self._notify())

    def put_front_nowait(self, event: dict) -> None:
        if self._add(event, front=True):
            asyncio.get_running_loop().create_task(self._notify())

    async def _notify(self) -> None:
        async with self._cond:
            self._cond.notify_all()

    async def close(self) -> None:
        async with self._cond:
            self.closed = True
            self._cond.notify_all()

    # ------------------------------------------------------------------ consume
    def _pick(self) -> dict | None:
        now = self.clock()
        free = [it for it in self._items if it["seq"] not in self._acked
                and self._leases.get(it["seq"], 0.0) <= now]
        if not free:
            return None
        fronts = [it for it in free if it["front"]]
        return max(fronts, key=lambda it: it["seq"]) if fronts else free[0]

    def _next_lease_end(self) -> float | None:
        now = self.clock()
        ends = [d for s, d in self._leases.items() if d > now and s not in self._acked]
        return min(ends) if ends else None

    def _take(self, lease_s: float) -> tuple[int, dict] | None:
        it = self._pick()
        if it is None:
            return None
        self._leases[it["seq"]] = self.clock() + lease_s
        return it["seq"], dict(it["event"])

    async def lease(self, timeout: float | None, lease_s: float | None = None
                    ) -> tuple[int | None, dict] | None:
        """(seq, event) of the next unread event, handed out for `lease_s` seconds (default
        LEASE_S); None on timeout; (None, {"type": "ended"}) once closed and nothing is left.
        Arms the spool."""
        self.arm()
        lease_s = LEASE_S if lease_s is None else lease_s
        got = self._take(lease_s)
        if got is not None:
            return got
        if self.closed:
            return None, {"type": "ended"}
        if timeout is not None and timeout <= 0:
            return None
        deadline = None if timeout is None else self.clock() + timeout

        async def _wait():
            async with self._cond:
                while True:
                    got = self._take(lease_s)
                    if got is not None:
                        return got
                    if self.closed:
                        return None, {"type": "ended"}
                    wait = None
                    end = self._next_lease_end()  # a dead consumer's lease runs out
                    if end is not None:
                        wait = max(0.01, end - self.clock())
                    if deadline is not None:
                        left = deadline - self.clock()
                        if left <= 0:
                            return None
                        wait = left if wait is None else min(wait, left)
                    try:
                        await asyncio.wait_for(self._cond.wait(), timeout=wait)
                    except asyncio.TimeoutError:
                        pass
        return await _wait()

    def ack(self, seq: int) -> bool:
        """The consumer has the event: never hand it out again. False: unknown/already acked."""
        try:
            seq = int(seq)
        except (TypeError, ValueError):
            return False
        self._leases.pop(seq, None)
        if seq in self._acked or seq <= self._cursor or not any(it["seq"] == seq
                                                                for it in self._items):
            return False
        self._acked.add(seq)
        self.last_consumed = self.clock()
        self._advance(persist=True)  # out of order too: a restart never hands it out again
        return True

    def release(self, seq: int) -> None:
        """Give a leased event back at once (the reply could not be delivered)."""
        self._leases.pop(seq, None)
        try:
            asyncio.get_running_loop().create_task(self._notify())
        except RuntimeError:
            pass

    def take(self, types, max_n: int = 5, recent_s: float = 600.0) -> list[dict]:
        """0.21.7 (midturn.py): events of `types` the agent has not been shown mid-turn yet,
        oldest first, younger than `recent_s` (by `ts`). Each is shown once; one still unread
        is consumed (acked), so `listen`/`next` never deliver it again. One a consumer
        (`listen`) already took is shown too, marked `delivered: true`: the background listen
        may not have woken the agent yet in the middle of its turn."""
        wall = time.time()
        out: list[dict] = []
        for it in self._items:
            seq, ev = it["seq"], it["event"]
            if len(out) >= max(0, max_n):
                break
            if not wanted(ev, types) or seq in self._shown:
                continue
            try:
                if wall - float(ev.get("ts") or it.get("ts") or wall) > recent_s:
                    continue
            except (TypeError, ValueError):
                pass
            delivered = seq in self._acked or seq <= self._cursor
            self._shown[seq] = self.clock()
            if not delivered:
                self.ack(seq)
            out.append({**ev, "delivered": delivered})
        while len(self._shown) > 2 * self.maxlen:
            self._shown.pop(next(iter(self._shown)))
        return out

    async def get(self, timeout: float | None) -> dict | None:
        """EventQueue.get: the oldest event, consumed at once (lease + ack)."""
        got = await self.lease(timeout)
        if got is None:
            return None
        seq, ev = got
        if seq is not None:
            self.ack(seq)
        return ev


def unread_on_disk(dir_: Path, types=WAKE_TYPES) -> tuple[int, int]:
    """(unread, unread wake-worthy) of the spool in `dir_`, read from the files (for the Stop
    hook when the join process is gone). (0, 0) without a spool. Never raises."""
    try:
        cur = json.loads((Path(dir_) / CURSOR_NAME).read_text(encoding="utf-8"))
        cursor = int(cur.get("cursor") or 0)
        acked = {int(s) for s in (cur.get("acked") or [])}
    except (OSError, ValueError, TypeError, AttributeError):
        cursor, acked = 0, set()
    try:
        raw = (Path(dir_) / EVENTS_NAME).read_text(encoding="utf-8")
    except OSError:
        return 0, 0
    n = wake = 0
    for line in raw.splitlines():
        try:
            it = json.loads(line)
            seq, typ = int(it["seq"]), it["event"].get("type")
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
        if seq <= cursor or seq in acked or typ in EPHEMERAL:
            continue
        n += 1
        wake += typ in types
    return n, wake
