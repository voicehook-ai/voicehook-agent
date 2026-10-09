#!/usr/bin/env python3
"""Release-Plan und Versionspruefung fuer .github/workflows/release.yml.

Zwei getrennte Tag-Familien (CLI und Plugin werden unabhaengig versioniert):
  cli-vX.Y.Z     muss == pyproject [project].version == voicehook_agent.__version__ sein;
                 Release mit sdist + wheel als Assets.
  plugin-vX.Y.Z  muss == plugins/voicehook-join/.claude-plugin/plugin.json "version" sein;
                 Release ohne Python-Artefakte.

Unterbefehle:
  plan     --event E [--tag T]   druckt die Release-Matrix als JSON (eine Zeile pro Tag)
                                 und bricht bei Versionsabweichung mit Exitcode 1 ab.
                                 push: genau T; sonst Trockenlauf fuer T bzw. beide
                                 aus den Dateien abgeleiteten Tags.
  gh-args  --tag T [--prev P] [ASSET...]
                                 druckt die Argumente fuer `gh` (eins pro Zeile), damit
                                 Echtlauf und Trockenlauf denselben Befehl verwenden.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_JSON = Path("plugins/voicehook-join/.claude-plugin/plugin.json")
INIT_PY = Path("src/voicehook_agent/__init__.py")
TAG_RX = re.compile(r"^(cli|plugin)-v(\d+)\.(\d+)\.(\d+)$")
KINDS = ("cli", "plugin")


class ReleaseError(ValueError):
    pass


def parse_tag(tag: str) -> tuple[str, str]:
    m = TAG_RX.match(tag)
    if not m:
        raise ReleaseError(f"Tag {tag!r} passt nicht zu cli-vX.Y.Z oder plugin-vX.Y.Z")
    return m.group(1), ".".join(m.group(2, 3, 4))


def read_versions(root: Path) -> dict[str, str | None]:
    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    init = (root / INIT_PY).read_text(encoding="utf-8")
    m = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', init, re.MULTILINE)
    plugin = json.loads((root / PLUGIN_JSON).read_text(encoding="utf-8"))
    return {
        "pyproject": pyproject["project"]["version"],
        "package": m.group(1) if m else None,
        "plugin": plugin.get("version"),
    }


def check_tag(tag: str, root: Path) -> tuple[str, str]:
    kind, version = parse_tag(tag)
    v = read_versions(root)
    if kind == "cli":
        sources = {"pyproject.toml [project].version": v["pyproject"],
                   f"{INIT_PY} __version__": v["package"]}
    else:
        sources = {f"{PLUGIN_JSON} version": v["plugin"]}
    bad = [f"  {src} = {val!r}" for src, val in sources.items() if val != version]
    if bad:
        raise ReleaseError(f"Tag {tag} (Version {version}) passt nicht zu:\n" + "\n".join(bad))
    return kind, version


def derive_tag(kind: str, root: Path) -> str:
    v = read_versions(root)
    return f"{kind}-v{v['pyproject'] if kind == 'cli' else v['plugin']}"


def _key(tag: str) -> tuple[int, ...]:
    m = TAG_RX.match(tag)
    return tuple(int(x) for x in m.group(2, 3, 4)) if m else ()


def previous_tag(tag: str, tags: list[str]) -> str | None:
    """Hoechster Tag derselben Familie mit kleinerer Version (Startpunkt der Notes)."""
    kind, _ = parse_tag(tag)
    older = [t for t in tags if TAG_RX.match(t) and t.startswith(f"{kind}-v") and _key(t) < _key(tag)]
    return max(older, key=_key) if older else None


def git_tags(root: Path) -> list[str]:
    out = subprocess.run(["git", "tag", "-l"], cwd=root, capture_output=True, text=True, check=True)
    return out.stdout.split()


def plan(event: str, tag: str | None, root: Path, tags: list[str]) -> list[dict]:
    if event == "push":
        if not tag:
            raise ReleaseError("push ohne Tag")
        wanted = [tag]
    else:
        wanted = [tag] if tag else [derive_tag(k, root) for k in KINDS]
    entries = []
    for t in wanted:
        kind, version = check_tag(t, root)
        entries.append({"tag": t, "kind": kind, "version": version,
                        "prev_tag": previous_tag(t, tags) or "",
                        "dry_run": event != "push"})
    return entries


def gh_args(tag: str, prev: str | None, assets: list[str]) -> list[str]:
    kind, version = parse_tag(tag)
    title = (f"voicehook-agent CLI {version}" if kind == "cli"
             else f"voicehook-join Plugin {version}")
    args = ["release", "create", tag, "--verify-tag", "--title", title, "--generate-notes"]
    if prev:
        args += ["--notes-start-tag", prev]
    return args + list(assets)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--event", required=True)
    p.add_argument("--tag", default="")
    g = sub.add_parser("gh-args")
    g.add_argument("--tag", required=True)
    g.add_argument("--prev", default="")
    g.add_argument("assets", nargs="*")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "plan":
            print(json.dumps(plan(args.event, args.tag or None, ROOT, git_tags(ROOT))))
        else:
            print("\n".join(gh_args(args.tag, args.prev or None, args.assets)))
    except ReleaseError as exc:
        print(f"::error::{exc}".replace("\n", "%0A"), file=sys.stderr)
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
