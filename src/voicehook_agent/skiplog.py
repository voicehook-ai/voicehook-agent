"""What the CLI sorts out, visible with its reason (Audit "Stille Caps", Oliver 10.11.2026).

One line `HH:MM:SS <where> why=<reason> k=v ...` in ~/.voicehook/skipped.log (0600, size-rotated
like watch.log). Callers pass places, reasons, labels and counts: never message or question text.
Used by hooks (asks.stop_ask) and the join loop (relay.UserContinuation), which have no log of
their own. Never raises.

stdlib only.
"""
from __future__ import annotations

import os
import time

from . import activity as vact

LOG_NAME = "skipped.log"
LOG_MAX_BYTES = 256 * 1024


def note(where: str, why: str, **kv: object) -> None:
    try:
        root = vact.state_root()
        path = root / LOG_NAME
        vact._private_dir(root)
        try:
            if path.stat().st_size > LOG_MAX_BYTES:
                os.replace(path, path.with_name(LOG_NAME + ".1"))
        except OSError:
            pass
        parts = [time.strftime("%H:%M:%S"), where, f"why={why}"]
        parts += [f"{k}={str(v).replace(chr(10), ' ')[:60]}" for k, v in kv.items()]
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            os.write(fd, (" ".join(parts) + "\n").encode("utf-8"))
        finally:
            os.close(fd)
    except Exception:  # noqa: BLE001, S110
        pass


def read_lines() -> list[str]:
    """The current log lines (tests, `voicehook-agent` diagnostics)."""
    try:
        return (vact.state_root() / LOG_NAME).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
