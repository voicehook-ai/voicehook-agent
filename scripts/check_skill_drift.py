#!/usr/bin/env python3
"""Drift-Check Plugin-Skill vs. oeffentliche Quelle (CI-Job "skill-drift").

Die kanonische Fassung von SKILL.md/REFERENCE.md liegt im (privaten) voicehook-v4-Repo
und wird unter https://voicehook.ai/agent/ ausgeliefert. Das Plugin muss byte-gleich sein.
Drift ist zeitweise legitim (v4 deployt vor dem Plugin-Sync), deshalb eigener Job.
"""
from __future__ import annotations

import argparse
import difflib
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = Path("plugins/voicehook-join/skills/voicehook-join")
FILES = ("SKILL.md", "REFERENCE.md")
DEFAULT_BASE = "https://voicehook.ai/agent/"


class FetchError(RuntimeError):
    pass


def fetch(url: str, timeout: float = 30) -> bytes:
    req = urllib.request.Request(url, headers={"user-agent": "voicehook-agent-ci/skill-drift"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        ctype = resp.headers.get("content-type", "")
        body = resp.read()
    # Unbekannte Pfade liefern dort 200 + SPA-HTML: nie als Inhalt vergleichen.
    if "markdown" not in ctype and "text/plain" not in ctype:
        raise FetchError(f"{url} liefert content-type {ctype!r} statt Markdown (SPA-Fallback?)")
    return body


def diff(local: bytes, remote: bytes, name: str, limit: int = 40) -> str:
    lines = list(difflib.unified_diff(
        local.decode("utf-8", "replace").splitlines(),
        remote.decode("utf-8", "replace").splitlines(),
        fromfile=f"plugin/{name}", tofile=f"voicehook.ai/agent/{name}", lineterm=""))
    more = len(lines) - limit
    return "\n".join(lines[:limit]) + (f"\n... (+{more} Zeilen)" if more > 0 else "")


def check(root: Path, base: str, fetcher=fetch) -> list[str]:
    problems = []
    for name in FILES:
        local_path = root / SKILL_DIR / name
        url = base + name
        try:
            remote = fetcher(url)
        except Exception as exc:  # noqa: BLE001 (Netz/HTTP-Fehler als Befund melden)
            problems.append(f"{name}: Abruf fehlgeschlagen: {exc}")
            continue
        local = local_path.read_bytes()
        if local != remote:
            problems.append(f"{name}: DRIFT ({len(local)} B lokal vs {len(remote)} B live)\n"
                            + diff(local, remote, name))
        else:
            print(f"{name}: byte-gleich mit {url}")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base-url", default=DEFAULT_BASE)
    args = ap.parse_args(argv)
    problems = check(ROOT, args.base_url)
    if not problems:
        return 0
    print("\n\n".join(problems))
    print(f"""
Plugin-Skill weicht von {args.base_url} ab. Synchronisieren:
  for f in {' '.join(FILES)}; do curl -fsSL {args.base_url}$f -o {SKILL_DIR}/$f; done
  git diff {SKILL_DIR}   # pruefen, dann committen (ggf. plugin.json version anheben)
Ist die Website der veraltete Stand, stattdessen voicehook-v4 deployen.
Kurzzeitige Drift nach einem v4-Deploy ist erwartet; dieser Job ist kein Required Check.""")
    return 1


if __name__ == "__main__":
    sys.exit(main())
