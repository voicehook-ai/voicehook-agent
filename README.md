# voicehook-agent

Zero-install CLI that lets any LLM agent (Claude Code, Cursor, ZeroClaw, Hermes,
Codex, …) join a [voicehook.ai](https://voicehook.ai) voice-call as a 2nd
participant. No SDK, no MCP server, no learning curve — stdin/stdout protocol.

## TL;DR

```bash
uvx voicehook-agent join https://voicehook.ai/r/<slug>?go=1
```

- **stdout** prints incoming user + voice-ai turns as `[role] text`
- **stdin** lines are spoken by voice-ai (TTS via Google Chirp3-HD)
- **Ctrl-D / Ctrl-C** disconnects cleanly

## Install

### One-shot (per-call, recommended)

```bash
uvx voicehook-agent join https://voicehook.ai/r/<slug>?go=1
```

[uv](https://github.com/astral-sh/uv) downloads the package on demand. Zero state.

### Persistent (one-time install)

```bash
uv tool install voicehook-agent
# or:
pipx install voicehook-agent
```

Then:

```bash
voicehook-agent join https://voicehook.ai/r/<slug>?go=1
```

## Agent-skill registration

Append this skill description to your agent's instructions (e.g. `~/.claude/CLAUDE.md` for
Claude Code, `$CODEX_HOME/skills/voicehook-agent/SKILL.md` for Codex):

```bash
curl -fsSL https://voicehook.ai/agent/SKILL.md
```

The agent then knows to invoke `voicehook-agent join <url>` whenever a user
shares a voicehook invite.

## Protocol

### Interactive mode (default)

```
$ voicehook-agent join https://voicehook.ai/r/abc-def-ghi-XYZ4?go=1
[system] connecting room=abc-def-ghi-XYZ4 as identity=agent-cli-7f3a via https://voicehook.ai
[system] connected — 1 peers: ['agent-AJ_qwerty1234']
[hint] type a line to senior.say (voice-ai speaks it). Ctrl-D to quit.
[user] Hallo, wer bist du?
Ich bin dein Pair-Programming-Brain.    ← typed by agent (voice-ai TTS speaks it)
[agent] Ich bin dein Pair-Programming-Brain.
[user] super, lass uns starten
…
```

### JSON mode

```bash
voicehook-agent join <url> --json
```

stdout (JSONL):
```json
{"role": "user", "text": "Hallo", "topic": "transcript"}
```

stdin (JSONL):
```json
{"text": "Hi there"}                                          → senior.say (default)
{"topic": "senior.persona", "text": "Du bist X..."}           → live system-prompt update
{"topic": "senior.interrupt"}                                 → cut off voice-ai
{"topic": "senior.inject", "role": "user", "text": "..."}     → force voice-ai reply
```

## Topics

| Topic              | Direction | Purpose                                |
|--------------------|-----------|----------------------------------------|
| `transcript`       | in        | Live user + voice-ai turns             |
| `senior.say`       | out       | TTS push (voice-ai speaks your text)   |
| `senior.persona`   | out       | live update voice-ai system prompt     |
| `senior.interrupt` | out       | cut off voice-ai mid-sentence          |
| `senior.inject`    | out       | force voice-ai to react (user-role)    |

## Environment

| Variable               | Default                 | Purpose                          |
|------------------------|-------------------------|----------------------------------|
| `VOICEHOOK_API_BASE`   | `https://voicehook.ai`  | Token-mint endpoint base URL     |

## License

MIT.
