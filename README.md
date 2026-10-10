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
| `create_call` | start a new call for your user: `human_url` plus `share_text` (pass it on verbatim: open the link in a normal browser, not an app's built-in browser) |
| `join` | join a call from an invite link with your chip `label`; returns the session, the call language and the call context |
| `next` | wait up to 50 s for the next turn (user, decision, operator, context, ended) |
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

For a loop without polling (`say`, `next`, `status`, `leave`), the status board, the activity
hook and all flags see [docs/CLI.md](docs/CLI.md).

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
