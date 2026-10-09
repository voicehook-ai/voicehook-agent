"""Unit-Tests fuer scripts/release_check.py (Logik des Release-Workflows)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import release_check as rc


def make_repo(tmp_path: Path, cli="0.14.0", pkg="0.14.0", plugin="1.1.0") -> Path:
    (tmp_path / "pyproject.toml").write_text(f'[project]\nname = "x"\nversion = "{cli}"\n')
    init = tmp_path / rc.INIT_PY
    init.parent.mkdir(parents=True)
    init.write_text(f'"""doc"""\n\n__version__ = "{pkg}"\n')
    pj = tmp_path / rc.PLUGIN_JSON
    pj.parent.mkdir(parents=True)
    pj.write_text(json.dumps({"name": "voicehook-join", "version": plugin}))
    return tmp_path


@pytest.mark.parametrize("tag,expected", [
    ("cli-v0.14.0", ("cli", "0.14.0")),
    ("plugin-v1.10.2", ("plugin", "1.10.2")),
])
def test_parse_tag_ok(tag, expected):
    assert rc.parse_tag(tag) == expected


@pytest.mark.parametrize("tag", ["v0.14.0", "cli-0.14.0", "cli-v0.14", "plugin-v1.0.0-rc1", "cli-v01.2.3x"])
def test_parse_tag_rejects(tag):
    with pytest.raises(rc.ReleaseError):
        rc.parse_tag(tag)


def test_cli_tag_matches(tmp_path):
    assert rc.check_tag("cli-v0.14.0", make_repo(tmp_path)) == ("cli", "0.14.0")


def test_cli_tag_ignores_plugin_version(tmp_path):
    # getrennte Versionierung: Plugin 1.1.0 stoert einen CLI-Tag nicht
    assert rc.check_tag("cli-v0.14.0", make_repo(tmp_path, plugin="9.9.9"))[0] == "cli"


def test_cli_tag_mismatch_pyproject(tmp_path):
    with pytest.raises(rc.ReleaseError, match="pyproject"):
        rc.check_tag("cli-v0.15.0", make_repo(tmp_path))


def test_cli_tag_mismatch_package_only(tmp_path):
    with pytest.raises(rc.ReleaseError, match="__version__") as ei:
        rc.check_tag("cli-v0.14.0", make_repo(tmp_path, pkg="0.13.0"))
    assert "pyproject" not in str(ei.value)


def test_plugin_tag_matches_and_mismatches(tmp_path):
    root = make_repo(tmp_path)
    assert rc.check_tag("plugin-v1.1.0", root) == ("plugin", "1.1.0")
    with pytest.raises(rc.ReleaseError, match="plugin.json"):
        rc.check_tag("plugin-v0.14.0", root)


def test_previous_tag_same_family_semver():
    tags = ["cli-v0.9.0", "cli-v0.10.0", "cli-v0.14.0", "plugin-v1.0.0", "v0.13.0", "cli-v0.15.0"]
    assert rc.previous_tag("cli-v0.14.0", tags) == "cli-v0.10.0"
    assert rc.previous_tag("plugin-v1.1.0", tags) == "plugin-v1.0.0"
    assert rc.previous_tag("plugin-v1.0.0", tags) is None


def test_plan_push_single(tmp_path):
    entries = rc.plan("push", "plugin-v1.1.0", make_repo(tmp_path), ["plugin-v1.0.0"])
    assert entries == [{"tag": "plugin-v1.1.0", "kind": "plugin", "version": "1.1.0",
                        "prev_tag": "plugin-v1.0.0", "dry_run": False}]


def test_plan_dry_run_derives_both(tmp_path):
    entries = rc.plan("pull_request", None, make_repo(tmp_path), [])
    assert [(e["tag"], e["dry_run"]) for e in entries] == [("cli-v0.14.0", True), ("plugin-v1.1.0", True)]


def test_plan_fails_on_mismatch(tmp_path):
    with pytest.raises(rc.ReleaseError):
        rc.plan("pull_request", None, make_repo(tmp_path, pkg="0.13.0"), [])


def test_plan_push_requires_tag(tmp_path):
    with pytest.raises(rc.ReleaseError):
        rc.plan("push", None, make_repo(tmp_path), [])


def test_gh_args_cli_with_assets():
    assert rc.gh_args("cli-v0.14.0", "cli-v0.13.0", ["dist/a.whl", "dist/a.tar.gz"]) == [
        "release", "create", "cli-v0.14.0", "--verify-tag", "--title", "voicehook-agent CLI 0.14.0",
        "--generate-notes", "--notes-start-tag", "cli-v0.13.0", "dist/a.whl", "dist/a.tar.gz"]


def test_gh_args_plugin_first_release():
    args = rc.gh_args("plugin-v1.1.0", None, [])
    assert "--notes-start-tag" not in args and args[-1] == "--generate-notes"
    assert "voicehook-join Plugin 1.1.0" in args


def test_repo_versions_are_consistent_today():
    """Positivkontrolle gegen das echte Repo: abgeleitete Tags bestehen die Pruefung."""
    for kind in rc.KINDS:
        tag = rc.derive_tag(kind, rc.ROOT)
        assert rc.check_tag(tag, rc.ROOT)[0] == kind


def test_cli_main_exit_codes(tmp_path, capsys):
    assert rc.main(["gh-args", "--tag", "cli-v1.2.3"]) == 0
    assert capsys.readouterr().out.splitlines()[:3] == ["release", "create", "cli-v1.2.3"]
    assert rc.main(["gh-args", "--tag", "v1.2.3"]) == 1
