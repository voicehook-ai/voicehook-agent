# voicehook-agent

[![CI](https://github.com/voicehook-ai/voicehook-agent/actions/workflows/ci.yml/badge.svg?branch=master)](https://github.com/voicehook-ai/voicehook-agent/actions/workflows/ci.yml)
[![CodeQL](https://github.com/voicehook-ai/voicehook-agent/actions/workflows/codeql.yml/badge.svg?branch=master)](https://github.com/voicehook-ai/voicehook-agent/actions/workflows/codeql.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Plugin version](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fvoicehook-ai%2Fvoicehook-agent%2Fmaster%2Fplugins%2Fvoicehook-join%2F.claude-plugin%2Fplugin.json&query=%24.version&label=plugin&prefix=v&color=7c5cff)](plugins/voicehook-join/.claude-plugin/plugin.json)
[![CLI version](https://img.shields.io/badge/dynamic/toml?url=https%3A%2F%2Fraw.githubusercontent.com%2Fvoicehook-ai%2Fvoicehook-agent%2Fmaster%2Fpyproject.toml&query=%24.project.version&label=cli&prefix=v&color=7c5cff)](pyproject.toml)

**Talk to your coding agents instead of reading their output.**

![Installing the voicehook-join plugin in Claude Code: two commands, then the plugin is enabled](https://raw.githubusercontent.com/voicehook-ai/voicehook-agent/master/assets/readme/plugin-install.gif)

[voicehook.ai](https://voicehook.ai/?utm_source=github&utm_campaign=d2-readme) is a voice call in the browser.
Hit "invite agent", hand the link to Claude Code, Codex or opencode, and talk to it
while it keeps working. Delta, the voice in the call, answers from the status board
and FAQ your agent keeps up to date, so you hear what is going on without opening a terminal.

This repo holds the agent side: the `voicehook-join` Claude Code plugin and the
`voicehook-agent` CLI that lets any agent with a shell join a call.

## Three ways to connect

| Your agent | How to connect |
|---|---|
| **Claude Code** | Plugin, two commands (see below). [Details](docs/CLI.md#claude-code-plugin) |
| **claude.ai / Claude Desktop** | Custom connector `https://voicehook.ai/mcp` (Settings, Connectors, Add custom connector; no key). [Guide](https://voicehook.ai/docs/mcp) |
| **Any agent with a shell** | [Quickstart A](https://voicehook.ai/agent/SKILL.md) (HTTPS bridge, nothing to install) or the [CLI](#cli-quickstart) |

Which one? MCP (connector or plugin) is the entry: nothing to install, one agent per call, the only path with
`create_call`, cards and decisions. The CLI is the pro tool for multi-agent work on one machine. The
HTTPS bridge is the protocol underneath and the fallback.

**MCP/bridge = one agent in the call; activity lines are only what that agent writes by hand. For several agents/sessions on one machine working on one call, use the voicehook-agent CLI (pro): its multi-session relay + Claude Code hook feed the live activity log, status and decisions of ALL local sessions into the call automatically. MCP/bridge run on the server and cannot see the user's local sessions.**

| path | role | use it when |
|---|---|---|
| **MCP** (`voicehook.ai/mcp`) | entry + distribution | a single agent in a call, cloud/sandbox sessions, starting a call yourself |
| **CLI** (`voicehook-agent`) | pro tool for multi-agent work | several agents/sessions on one machine in one call, live activity log of all sessions |
| **Bridge** (`/api/bridge/*`) | protocol underneath + fallback | no MCP client and no CLI |

Details and install without git: [docs/CLI.md](docs/CLI.md#which-path-mcp-cli-or-bridge-oliver-19102026-decision-a).

In Claude Code:

```
/plugin marketplace add voicehook-ai/voicehook-agent
/plugin install voicehook-join@voicehook
```

Once connected, paste an invite link (`https://voicehook.ai/r/<slug>?invite=<code>`)
into your agent and it joins the call.

MCP tools (connector and plugin):

| tool | what it does |
|---|---|
| `create_call` | start a new call for your user (main agent only: never delegate `create_call` to a subagent, the link would stay in its thread): `human_url` plus `share_text` (pass it on verbatim: open the link in a normal browser, not an app's built-in browser) |
| `join` | join a call from an invite link with your chip `label`; greets for you in the call language (`greet`, default on); returns the session, the call language and the call context (`first_actions`); before the person opened a `create_call` link it returns `waiting_for_human`: call it again |
| `next` | wait up to 50 s for the next turn (user, decision, operator, context, ended); call it at least every 10 to 15 s, one loop per agent; after a pause it brings `missed` |
| `say` | the voicebot speaks the text |
| `status` | status board (never spoken) the voicebot answers from, optional chip emoji |
| `show` | a card on the call page (text, code, link or image), never spoken |
| `activity` | one line of 3 to 8 words for the activity feed, on every step |
| `decide` | a question for the Decision Board (yes/no or up to 3 options); the answer comes back in `next` |
| `leave` | leave the call |

On 5xx, 503 or a network error the agent simply calls the tool again after 2 s; the session
stays valid. Several agents in one call: [rules](https://voicehook.ai/agent/MULTI-AGENT.md).

## CLI quickstart

```bash
uvx voicehook-agent join 'https://voicehook.ai/r/<slug>?invite=<code>' --name <your-own-name> --model <your-model-name>
```

- `--name` and `--model` are mandatory: a display name the agent picks for itself and the
  exact model it runs on. Name your real model and vendor, never "Claude" unless you are Claude.
- Use the full invite link, including `?invite=<code>`.
- stdout prints what the user and the voicebot say; every line on stdin is spoken.

In a call, keep `voicehook-agent listen` running in your harness's background mode
(Claude Code: `run_in_background`, never `&`/`nohup`): it exits on the first event that
needs you, which wakes you; start it again after handling it. Since 0.21.4 the Stop hook
(`voicehook-agent hook install`) blocks a turn end while your join is connected and nobody
listens ([listen guard](docs/CLI.md#listen-guard-never-end-a-turn-deaf-0214)).

Every decision and every status entry MUST carry hidden background for the voicebot
(0.21.4): `--context "..."` plus the 3 most likely follow-up questions with prepared
answers, `--faq "Frage::Antwort"` (no answer yet: say who works on it and the ETA, "In
Arbeit bei <Name>, Ergebnis ca. 10 Min"). Activity lines may carry them too. On `status`
they attach to the item they follow; without them the CLI prints a hint (see
[docs/CLI.md](docs/CLI.md)).

For a loop without polling (`say`, `next`, `status`, `leave`), the status board, the activity
hook and all flags see [docs/CLI.md](docs/CLI.md). Since 0.21.0: `decide --context "..."`
gives Delta the background of a question, `ui large decisions` / `ui layout thirds` /
`ui focus voicehook` control the call page of the humans, `status|activity --project NAME`
tags your project ([docs/CLI.md](docs/CLI.md#page-control-ui-0210)).

Right after `join` (0.21.4) the CLI lists the other Claude Code / Codex sessions running on
this machine and suggests asking the human "Soll ich die anderen N Sessions hier einbinden,
damit sie ihren Status in den Call bringen?"; on yes the agent runs the printed setup
commands. Agents on MCP or the bridge cannot see local sessions: they say it works in
principle with the CLI installed: `uv tool install voicehook-agent && voicehook-agent hook install`.

Several Claude Code sessions on one machine (0.15.0): run `voicehook-agent hook install`
once; while one session is in a call, every other session reports turn goal, PR, deploy
and errors under its own name in the call's activity feed
([multi-session relay](docs/CLI.md#multi-session-relay-every-claude-code-session-of-a-machine-in-the-call-0150)).
Since 0.16.0 such a helper session can also ask the human (`decide`, answer via
`relay-next` or `decide --wait`) and keep its own status board (`status --doing ...`)
([helper sessions](docs/CLI.md#helper-sessions-decision-board-and-own-status-board-0160)).

OpenAI Codex CLI (0.20.0): `voicehook-agent hook install --agent codex` writes the same
automation into `~/.codex` (hooks.json, `notify`, a local MCP server with `status`,
`decide`, `next`, `activity`; backup first), then trust the hooks once with `/hooks` in
Codex ([other coding agents](docs/CLI.md#other-coding-agents-0200)).

## Pricing

About 10 minutes free every day. Prepaid from 10 EUR, no subscription.

## Documentation

| Document | Contents |
|---|---|
| [docs/CLI.md](docs/CLI.md) | Install, agent loop commands, status board and FAQ, speaking style, activity feed and hooks, shapes, self-update, Claude Code plugin, skill registration |
| [docs/PROTOCOL.md](docs/PROTOCOL.md) | stdin/stdout protocol, data-channel topics, relay flags, HTTPS bridge, wake marker, server behaviour, environment variables |
| [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) | CI jobs, plugin releases, CLI releases, PyPI setup |
| [OPERATOR-PROTOCOL.md](https://voicehook.ai/agent/OPERATOR-PROTOCOL.md) | Server-side operator protocol (voicehook.ai) |

## Development, CI and releases

CI runs tests, ruff, plugin validation, a secret scan and a skill drift check on every PR.
Release tags, the plugin release checklist and the PyPI setup are in
[docs/DEVELOPMENT.md](docs/DEVELOPMENT.md).

## License

MIT, see [LICENSE](LICENSE).

---

Made in Germany, hosted in Nuremberg (Hetzner). No cookies, no tracking, no call recordings.
Nothing you or your agent say is stored. [Privacy policy](https://voicehook.ai/privacy)
