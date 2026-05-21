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
- **`/q`, `{"topic":"quit"}`, or SIGTERM/Ctrl-C** ends the session

> Since 0.2.0, `--keep-alive` is the default: **stdin-EOF no longer quits** and
> transient room-disconnects auto-reconnect. Run with a closed stdin in the
> background without the FIFO sleep-holder hack. See [Relay flags](#relay-flags).

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
[hint] type a line to senior.say (voice-ai speaks it). /q to quit (Ctrl-D no longer quits under --keep-alive).
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
| `transcript`        | in        | Live user + voice-ai turns             |
| `_wake`             | out*      | Wake marker on each finalized user-turn (#12) |
| `_meta`             | out*      | Connection / room-state events         |
| `senior.say`        | out       | TTS push (voice-ai speaks your text); tagged `_seq`/`_ts` (#9) |
| `senior.persona`    | out       | live update voice-ai system prompt     |
| `senior.interrupt`  | out       | cut off voice-ai mid-sentence          |
| `senior.inject`     | out       | force voice-ai to react (user-role)    |
| `senior.backchannel`| out       | silent operator↔agent side-channel, relayed as-is (#10) |

*`out` here = emitted on the CLI's **stdout** (not published to the room).

## Relay flags

Hardening flags (0.2.0) for unattended / background relay operation:

| Flag | Issue | Effect |
|------|-------|--------|
| `--keep-alive` / `--no-keep-alive` | #6 | stdin-EOF does **not** quit; auto-reconnect (exp. backoff, cap 30s) on transient disconnect until the host leaves / room closes / `/q` / SIGTERM. Default: on. |
| `--notify-url <url>` | #12 | POST `{role,text,room,timestamp}` to `<url>` on each finalized user-turn. |
| `--wake-only-user` / `--wake-all` | #12 | Only role=user wakes (default); `--wake-all` also wakes on agent turns (debug). |
| `--suppress-echo` | #10 | Drop the agent's own relayed TTS (role=agent transcript matching a recent `senior.say`) from the stdout stream. |
| `--say-ttl <sec>` | #9 | Drop a `senior.say` older than `<sec>` seconds, or superseded by a newer user-turn, instead of speaking it stale. |
| `--strict-relay` | #8 | Inject a bundled strict-relay persona at connect: the voicebot speaks **only** pushed text and never self-generates. Reuses `--persona-file` semantics; overridden by `--persona`/`--persona-file`. |

### Wake marker (JSON mode)

```json
{"role":"system","text":"user-turn","topic":"_wake","role":"user","text":"...","room":"<slug>","timestamp":1.0}
```

A monitor can `grep '"topic": "_wake"'` to re-invoke a coding agent per turn
(push, not poll). Wake events are **deduped** (identical consecutive turns fire
once) and **role-filtered** (the agent's own echoed TTS never wakes → no loop).

### FIFO newline tolerance (#11)

A control line written **without** a trailing newline is no longer silently
swallowed. On stdin-EOF the held tail is processed and a visible warning is
emitted on stderr (`[warn] stdin closed with un-terminated line …`). Prefer
`printf '%s\n'` over bare `jq -nc` when writing to the FIFO.

### Server-side dependencies (honest notes)

- **#9 say-TTL** is best-effort client-side: the server does not currently
  *ack* that a `senior.say` was spoken, so "spoken within TTL" is approximated
  by age + supersede-by-newer-user-turn. The wire payload carries `_seq`/`_ts`
  so a future server ack can correlate. Delivery-ack (#10 F8) and a true
  interrupt-confirmation (#10 F7) need server support and are not implemented.
- **#8 strict-relay** is enforced via persona injection only. Hard server-side
  enforcement (LLM self-generation truly disabled) is tracked server-side
  (voicehook-v3#28/#48); the CLI ships the strongest available client lever.

## Environment

| Variable               | Default                 | Purpose                          |
|------------------------|-------------------------|----------------------------------|
| `VOICEHOOK_API_BASE`   | `https://voicehook.ai`  | Token-mint endpoint base URL     |

## License

MIT.
