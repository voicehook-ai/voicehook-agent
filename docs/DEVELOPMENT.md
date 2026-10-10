# Development, CI and releases

For maintainers. Back to the [README](../README.md).

## CI

CI (`.github/workflows/ci.yml`) runs on every PR and on pushes to `master`:

| Job | What it checks |
|-----|----------------|
| `tests (py3.10)`, `tests (py3.13)` | full pytest suite + `scripts/tests` |
| `ruff (neue Befunde brechen)` (new findings fail) | ruff 0.16.10 against `scripts/ruff-baseline.json` (legacy findings counted per file and rule; any new one fails). After cleaning up: `python scripts/ruff_baseline.py --update` |
| `plugin validate` | `claude plugin validate --strict` for the plugin and the marketplace (no login needed) |
| `secret-scan (gitleaks)` | gitleaks over the full history; known fake test secrets are allowlisted by exact value and file in `.gitleaks.toml` |
| `skill-drift (voicehook.ai/agent/mcp)` | plugin `SKILL.md` byte-identical to https://voicehook.ai/agent/mcp/SKILL.md. Drift right after a website deploy is expected; resync with the command the job prints |

## Plugin releases

The plugin is pinned by `version` in
`plugins/voicehook-join/.claude-plugin/plugin.json`; users get a change ONLY when that
string changes. On every change under `plugins/voicehook-join/`: copy `SKILL.md`
byte-identical from voicehook-v4 `skills/voicehook-join-mcp/` (= live
https://voicehook.ai/agent/mcp/SKILL.md), bump `version` (skill text or protocol change = minor
`1.x.0`, typo/wording = patch `1.0.x`, breaking = major), run
`claude plugin validate plugins/voicehook-join` and `claude plugin validate .`.
The plugin is the MCP server entry plus the skill; the CLI and its activity hook
(`voicehook-agent hook install`) are separate and not part of the plugin.

## Releases

Releases (`.github/workflows/release.yml`) use two independent tag families:

- `cli-vX.Y.Z`: must equal `pyproject.toml` `version` and `voicehook_agent.__version__`. Creates a GitHub release with sdist + wheel and generated notes since the previous `cli-v*` tag.
- `plugin-vX.Y.Z`: must equal `version` in `plugins/voicehook-join/.claude-plugin/plugin.json`. Creates a GitHub release without Python artifacts.

```bash
git tag cli-v0.14.0 && git push origin cli-v0.14.0
```

PRs touching versions or the release workflow, and manual runs (`workflow_dispatch`), execute the same checks and build as a dry run and only print the `gh release create` command.

**PyPI (active for `cli-v*` tags).** The `pypi-publish` job uses Trusted Publishing (OIDC, no API token) and runs after the GitHub release for every pushed `cli-v*` tag that bumps the CLI version. One-time setup:

1. On pypi.org: *Your projects / Publishing* -> *Add a new pending publisher* (or the project's *Publishing* settings once it exists): owner `voicehook-ai`, repository `voicehook-agent`, workflow `release.yml`, environment `pypi`.
2. On GitHub: the environment `pypi` (Settings -> Environments), optionally with required reviewers and a tag rule `cli-v*`.
