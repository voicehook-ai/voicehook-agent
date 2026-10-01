"""Packaging invariants for `uvx voicehook-agent` (PyPI-ready, nothing published)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    tomllib = pytest.importorskip("tomli")

import voicehook_agent

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_version_single_source():
    assert PYPROJECT["project"]["version"] == voicehook_agent.__version__


def test_entry_point_matches_package_name():
    # `uvx voicehook-agent` resolves the console script named like the package
    proj = PYPROJECT["project"]
    assert proj["name"] == "voicehook-agent"
    assert proj["scripts"]["voicehook-agent"] == "voicehook_agent.cli:main"


def test_runtime_dependencies_stay_lean():
    names = sorted(d.split(">")[0].split("=")[0].split("<")[0].strip()
                   for d in PYPROJECT["project"]["dependencies"])
    assert names == ["httpx", "livekit"]


def test_strict_relay_persona_ships_in_package():
    assert (Path(voicehook_agent.__file__).parent / "strict-relay.txt").is_file()
