"""Unit-Tests fuer scripts/ruff_baseline.py und scripts/check_skill_drift.py."""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import check_skill_drift as drift
import ruff_baseline as rb


# ----- ruff_baseline -------------------------------------------------------------------
def test_count_relativizes_paths(tmp_path):
    findings = [{"filename": str(tmp_path / "src" / "a.py"), "code": "BLE001"},
                {"filename": str(tmp_path / "src" / "a.py"), "code": "BLE001"},
                {"filename": str(tmp_path / "b.py"), "code": None}]
    assert rb.count(findings, tmp_path) == Counter({("src/a.py", "BLE001"): 2, ("b.py", "syntax-error"): 1})


def test_json_roundtrip():
    c = Counter({("a.py", "F401"): 1, ("a.py", "S110"): 3, ("b.py", "F811"): 2})
    assert rb.from_json(rb.to_json(c)) == c


def test_compare_new_finding_in_legacy_file_breaks():
    base = Counter({("cli.py", "BLE001"): 17})
    new, fixed = rb.compare(Counter({("cli.py", "BLE001"): 18}), base)
    assert new and not fixed


def test_compare_new_file_or_rule_breaks():
    base = Counter({("cli.py", "BLE001"): 1})
    assert rb.compare(Counter({("cli.py", "BLE001"): 1, ("new.py", "F401"): 1}), base)[0]
    assert rb.compare(Counter({("cli.py", "BLE001"): 1, ("cli.py", "F401"): 1}), base)[0]


def test_compare_reduction_is_ok():
    new, fixed = rb.compare(Counter({("cli.py", "BLE001"): 16}), Counter({("cli.py", "BLE001"): 17}))
    assert not new and fixed


def test_baseline_file_matches_documented_legacy():
    import json
    data = json.loads(rb.BASELINE.read_text())
    assert sum(rb.from_json(data).values()) <= 70


# ----- check_skill_drift -----------------------------------------------------------------
def _repo(tmp_path, skill=b"S", ref=b"R"):
    d = tmp_path / drift.SKILL_DIR
    d.mkdir(parents=True)
    (d / "SKILL.md").write_bytes(skill)
    (d / "REFERENCE.md").write_bytes(ref)
    return tmp_path


def test_drift_identical(tmp_path):
    remote = {"https://x/SKILL.md": b"S", "https://x/REFERENCE.md": b"R"}
    assert drift.check(_repo(tmp_path), "https://x/", remote.__getitem__) == []


def test_drift_detected_with_diff(tmp_path):
    remote = {"https://x/SKILL.md": b"S\nneu\n", "https://x/REFERENCE.md": b"R"}
    problems = drift.check(_repo(tmp_path), "https://x/", remote.__getitem__)
    assert len(problems) == 1 and "SKILL.md: DRIFT" in problems[0] and "+neu" in problems[0]


def test_drift_byte_exact_trailing_newline(tmp_path):
    remote = {"https://x/SKILL.md": b"S\n", "https://x/REFERENCE.md": b"R"}
    assert drift.check(_repo(tmp_path), "https://x/", remote.__getitem__)


def test_fetch_error_is_reported(tmp_path):
    def boom(url):
        raise drift.FetchError("SPA-Fallback")
    problems = drift.check(_repo(tmp_path), "https://x/", boom)
    assert len(problems) == 2 and all("Abruf fehlgeschlagen" in p for p in problems)


@pytest.mark.parametrize("ctype,ok", [("text/markdown; charset=utf-8", True), ("text/plain", True),
                                      ("text/html; charset=utf-8", False)])
def test_fetch_rejects_html(monkeypatch, ctype, ok):
    class Resp:
        def __init__(self):
            self.headers = {"content-type": ctype}

        def read(self): return b"x"
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr(drift.urllib.request, "urlopen", lambda *a, **k: Resp())
    if ok:
        assert drift.fetch("https://x/SKILL.md") == b"x"
    else:
        with pytest.raises(drift.FetchError):
            drift.fetch("https://x/SKILL.md")
