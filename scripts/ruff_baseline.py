#!/usr/bin/env python3
"""ruff mit Altbestand-Baseline (CI-Job "ruff").

Der Altbestand (70 Befunde, Stand f38ec6b) steht als Anzahl je (Datei, Regel)
in scripts/ruff-baseline.json. Die CI bricht, sobald eine Datei fuer eine Regel
MEHR Befunde hat als dort erlaubt, also bei jedem neuen Befund, auch in Dateien
mit Altlast. Weniger ist immer ok; nach einem Aufraeumen die Baseline mit
`python scripts/ruff_baseline.py --update` verkleinern und mitcommitten.

Zaehlung statt Zeilennummer: Verschiebungen durch Edits loesen keinen Fehlalarm aus.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "scripts" / "ruff-baseline.json"


def run_ruff(root: Path = ROOT) -> list[dict]:
    proc = subprocess.run(
        ["ruff", "check", "--output-format", "json", "--exit-zero", "."],
        cwd=root, capture_output=True, text=True, check=True)
    return json.loads(proc.stdout or "[]")


def count(findings: list[dict], root: Path = ROOT) -> Counter:
    c: Counter = Counter()
    for f in findings:
        path = Path(f["filename"])
        rel = path.resolve().relative_to(root.resolve()) if path.is_absolute() else path
        c[(rel.as_posix(), f["code"] or "syntax-error")] += 1
    return c


def to_json(c: Counter) -> dict:
    out: dict[str, dict[str, int]] = {}
    for (path, code), n in sorted(c.items()):
        out.setdefault(path, {})[code] = n
    return out


def from_json(data: dict) -> Counter:
    return Counter({(p, code): n for p, codes in data.items() for code, n in codes.items()})


def compare(current: Counter, baseline: Counter) -> tuple[list[str], list[str]]:
    """(neu, abgebaut): neu = ueber Baseline, abgebaut = unter Baseline."""
    new, fixed = [], []
    for key in sorted(set(current) | set(baseline)):
        cur, base = current.get(key, 0), baseline.get(key, 0)
        if cur > base:
            new.append(f"{key[0]}: {key[1]} {cur} statt erlaubt {base}")
        elif cur < base:
            fixed.append(f"{key[0]}: {key[1]} {cur} statt {base}")
    return new, fixed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--update", action="store_true", help="Baseline auf aktuellen Stand setzen")
    args = ap.parse_args(argv)

    findings = run_ruff()
    current = count(findings)
    if args.update:
        BASELINE.write_text(json.dumps(to_json(current), indent=2, sort_keys=True) + "\n")
        print(f"Baseline geschrieben: {sum(current.values())} Befunde in {BASELINE.relative_to(ROOT)}")
        return 0

    baseline = from_json(json.loads(BASELINE.read_text()))
    new, fixed = compare(current, baseline)
    print(f"ruff: {sum(current.values())} Befunde, Baseline erlaubt {sum(baseline.values())}")
    if fixed:
        print("Abgebaut (Baseline mit --update verkleinern):")
        print("\n".join(f"  {x}" for x in fixed))
    if new:
        print("NEUE ruff-Befunde (ueber Baseline):")
        print("\n".join(f"  {x}" for x in new))
        files = sorted({path for path, _ in current if current[(path, _)] > baseline.get((path, _), 0)})
        sys.stdout.flush()  # Zusammenfassung vor der ruff-Detailausgabe im CI-Log
        subprocess.run(["ruff", "check", *files], cwd=ROOT, check=False)
        print("Fix: Befund beheben (ruff check --fix hilft bei [*]). Baseline NICHT hochsetzen.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
