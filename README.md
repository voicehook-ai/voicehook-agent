# voicehook-agent

Zero-install CLI that lets any LLM agent (Claude Code, Cursor, ZeroClaw, Hermes,
Codex, …) join a [voicehook.ai](https://voicehook.ai) voice-call as a 2nd
participant. No SDK, no MCP server, no learning curve — stdin/stdout protocol.

## TL;DR

```bash
uvx voicehook-agent join 'https://voicehook.ai/r/<slug>?invite=<code>' --name Claude --model opus-5.5
```

- **`--name` + `--model` are mandatory** (since 0.4.0): your self-chosen display
  name and the exact model you run on. Without both, `join` exits with code 2
  and explains what to pass. The web call UI shows them in the Agent chip as
  `Name · model` (a loading ring is shown there until you have joined).
- **Use the full invite link**, including its `?invite=<code>` part. The CLI
  sends that code as `op_invite` on `GET /api/token` and never prints it. A bare
  slug or a link without `?invite=` is rejected once the server enforces operator
  invites (`403 operator invite required`); an invalid code gives `403 invalid invite`.
- **stdout** prints incoming user + voice-ai turns as `[role] text`
- **stdin** lines are spoken by voice-ai (TTS via Google Chirp3-HD)
- **`/q`, `{"topic":"quit"}`, or SIGTERM/Ctrl-C** ends the session

## Agent loop without polling (0.5.0)

Start `join` once in the background, then drive the call with one-shot
commands. No FIFO, no tmux, no `sleep; tail`:

```bash
voicehook-agent join 'https://voicehook.ai/r/<slug>?invite=<code>' --name Claude --model opus-5.5 --json \
  > ~/.voicehook-agent/call.log 2>&1 &

voicehook-agent say "Hallo, ich bin jetzt im Call."     # speak one line
voicehook-agent next --timeout 60                       # blocks until the user said something
# {"ok": true, "type": "user", "role": "user", "text": "Wie geht's?", "ts": 1.0, "pending": 0}
voicehook-agent say "Gut, danke. Woran arbeiten wir?"
voicehook-agent next --timeout 60
voicehook-agent leave --say "Bis bald."                 # clean exit
```

| Command | Output (one JSON line) | Exit |
|---|---|---|
| `say <text> [--mode revise\|overwrite\|append]` | `{"ok":true,"seq":3}` | 0 ok, 1 failed |
| `next [--timeout SEC]` | `{"type":"user","text":...}`, `{"type":"revise","text":...,"unspoken":[...]}` (answer with `say --mode overwrite`), `{"type":"timeout"}`, `{"type":"ended"}` | 0, 3 on `ended` |
| `says` (0.9.0) | `{"type":"says","says":[{"seq":3,"state":"spoken","spoken_chars":12,"age_s":4.1,"text":"..."}]}`: last state of each own say (`sent` until the voicebot's first receipt) | 0 |
| `leave [--say TEXT]` | `{"type":"leaving"}` | 0 |
| `status` | room, identity, connected, pending events, idle seconds, peers | 0 |
| `status [TEXT] [--doing T] [--open T]... [--done T]... [-f board.json]` (0.7.0) | `{"type":"board","board":{...}}`: sends your status board | 0 ok, 1 failed |

- `next` returns ONE event, oldest first; `pending` says how many more are queued.
- **Say receipts (0.9.0):** the voicebot reports each say as `operator.say_status
  {seq, state, spoken_chars}` (`seq` = the `seq` that `say` returned; states `queued`,
  `spoken`, `interrupted`, `requeued`, `replaced`). `next` carries the changes since the
  last `next` as `"say_status": [{"seq":3,"state":"spoken"}]` (`spoken_chars` only for
  `interrupted`/`requeued`); a say stuck in `queued`/`requeued` for more than 20 s adds
  `"say_hint"` (do not push more; if outdated, `say --mode overwrite` a short version).
  `says` shows the last state of every say.
  Turns spoken while you were thinking are kept, never lost. `--timeout 0` only
  returns what is already queued. Each event carries `ts` (unix time it was
  spoken).
- Queueing starts with your first `say` or `next`. Turns spoken before that are
  not queued (a stdout/FIFO-only agent never piles up a backlog), and the queue
  keeps at most the 200 newest events.
- The commands find the running join on their own (they wait up to `--wait 30`
  seconds for it to come up, so `say` right after starting `join` works). With
  several joins on one machine they list them and ask for
  `--session <slug>/<identity>` (a slug alone is enough when only one join runs
  in that room). No running join = exit 3.
- Transport: one Unix socket per join at
  `~/.voicehook-agent/sessions/<slug>/<identity>/ctl.sock` (mode 0600; override
  the root with `VOICEHOOK_AGENT_HOME`). Several agents can join the same room
  from one machine. If that path is too long for a Unix socket it moves to
  `/tmp/voicehook-agent-<uid>/<hash>.sock`; that directory must be a real
  directory (no symlink) owned by you with mode 0700, otherwise `join` and the
  commands refuse it. A second `join` with the SAME identity into the same room
  exits 2 with a hint (`leave --session <slug>/<identity>` first, a different
  `--name`/`--identity`, or `--no-control`). `--no-control` turns the socket off;
  stdin/FIFO keeps working as before.
- **Orphan guard:** `join` leaves by itself when the agent sent no `say`/`next`
  (or stdin line) for `--idle-timeout` minutes (default 10, `0` = off). A blocked
  `next` counts as alive, but at most `--idle-timeout` long (0.8.0). The timer spans
  reconnects: a reconnect is no sign of life. `--owner-pid PID` (0.8.0, e.g.
  `$PPID`) leaves as soon as that process ends; `$VOICEHOOK_AGENT_HOME/holder` (the
  FIFO holder of the skill quickstart) is watched the same way. An ended join exits
  at once even while the FIFO holder keeps stdin open (before 0.8.0 it hung up to
  24 h). Before leaving voice-ai says `--idle-say` (German default,
  `''` = silent). SIGTERM/SIGHUP also leave cleanly; under `nohup` (SIGHUP
  ignored) closing the terminal does not end the call.
- **Persona guard:** if another operator agent is already in the room (LiveKit
  attribute `vh.role=agent`, set by the server for every operator token), `join`
  does NOT push `--persona`/`--persona-file`/`--strict-relay`/`--graph`; it says so
  on stdout (`_meta`) and stderr. `--force-persona` overrides. An explicit
  `{"topic":"operator.persona"}` on stdin is still sent as you wrote it.
- Speak the language of the call: answer in the language the user speaks
  (the auto-greet is German).
- **Read `agent_said` (0.7.0):** `next` carries `agent_said: [..]`, the voicebot's own
  lines since the last `next` (transcript role `agent`, never echoes of your `say`; oldest
  first, max 3 lines / 400 chars). Never repeat what it already said; correct it in one
  sentence if it was wrong; if it already answered fully, `say` nothing or only the missing fact.
- **Keep the main loop free (0.7.0):** between `next` and `say` do nothing slow.
  Anything over ~3 s (shell, web, file edits, builds, lookups) goes to a background
  agent/subtask; meanwhile `say` a short holding line and `status` the board. The CLI
  measures the time from a `user` turn leaving `next` to your next `say`; over 8 s the
  following `next` carries `"latency_warning": {"seconds": X, "hint": "delegate slow
  work, keep main loop free"}`. Nothing is spoken automatically.
- **Status board (0.7.0):** on every task change (started, finished, new) send the whole
  board: `voicehook-agent status --doing "baut gerade den Fix" --open "Tests" --done
  "Analyse"` (or `-f board.json` with `{doing, open[], done[]}`); `status ""` when
  finished. It replaces the last board at a fixed place in the voicebot's instructions
  (never spoken, server budget 600 chars, at most one update per 5 s applied). The
  voicebot answers "was macht Claude gerade?" from it and calls you by your `--name`.
  When the user asks, `next` yields `{"type":"status_request"}`: send `status` at once.
  `next` adds `"status_stale": true` when your board is older than 5 min and the user
  spoke since.
- **Keep the board fresh (0.9.0):** the voicebot (Delta) answers the user from your
  board while you work in the background; a stale board makes it answer wrong. `next`
  therefore adds `"status_due": true`, `"status_reason"`, `"board_age_s"` and a `"hint"`
  with the exact command when
  - the board is empty or was never set (`empty`),
  - the user asked for the status (`status_request`; that event is also put first in
    line, carries `"hint": "Nutzer fragt nach Stand: Board jetzt aktualisieren: ..."`
    and stays due until your next board),
  - the board is older than `--status-due SEC` (default 45, env
    `VOICEHOOK_STATUS_DUE`, 0 = off) while `doing`/`open` is set, or the user spoke
    after it (`stale`). A finished board (only `done`) does not nag by age.

  A `say` that reports progress ("fertig", "live", "deploye", "merged" ...) without a
  board push since your previous `say` returns `"status_reason": "say_progress"` and
  the same `hint`. Set the board on every request, delegation, result and deploy step;
  `doing` may hold the interim state and an ETA ("deployt Worker, ETA 2 min"), but the
  worker keeps at most 120 chars per entry. When finished, send `--done` items instead
  of clearing (an empty board counts as due). Example:

  ```json
  {"ok": true, "type": "timeout", "pending": 0, "status_due": true,
   "status_reason": "stale", "board_age_s": 61.2,
   "hint": "Board veraltet, Delta antwortet sonst falsch: jetzt aktualisieren: voicehook-agent status --doing \"<Zwischenstand, ETA>\" --open \"<offen>\" --done \"<erledigt>\". Welche 3 Fragen stellt der Nutzer wahrscheinlich als Nächstes? Beantworte sie vorab per --faq \"Frage::Antwort\""}
  ```
- **FAQ on the board (0.10.0):** on EVERY board update also predict the user's next
  likely questions and answer them in advance, so Delta can answer without asking you:
  `voicehook-agent status --doing "deployt den Worker, ETA 2 min" --faq "Wann ist es
  live?::in etwa 2 Minuten" --faq "Laufen die Tests?::ja, alle gruen"`. `--faq` is
  repeatable and split on the first `::` (both halves stripped; an item with an empty
  half is skipped with a warning on stderr); at most 6 pairs, question and answer capped
  to 200 chars each. Payload: `{"doing": "...", "open": [], "done": [], "faq": [{"q":
  "Wann ist es live?", "a": "in etwa 2 Minuten"}]}`. Without `--faq` the field is
  omitted. Every `status_due` hint now also asks: "Welche 3 Fragen stellt der Nutzer
  wahrscheinlich als Nächstes? Beantworte sie vorab per --faq".

> Since 0.2.0, `--keep-alive` is the default: **stdin-EOF no longer quits** and
> transient room-disconnects auto-reconnect. Run with a closed stdin in the
> background without the FIFO sleep-holder hack. See [Relay flags](#relay-flags).

## Activity feed (operator.activity, 0.10.0)

Delta knows what the coding agent is doing in the background, without asking. A Claude
Code `PostToolUse` hook writes ONE short line per tool call into `activity.log` of the
running join's session dir; the join publishes the newest 15 lines (oldest first) as
`operator.activity` `{"lines": ["17:12:03 Bash: Tests laufen lassen", "17:12:09 Edit"],
"ts": 1759418000.0}`, only on change and at most once per 5 s (a change inside the
window goes out when it ends, last one wins).

Install once (merges idempotently into `~/.claude/settings.json`, keeps all other
keys and hooks, refuses to touch invalid JSON):

```bash
voicehook-agent hook install              # or: --settings PATH
voicehook-agent hook print                # the snippet, to paste by hand
```

```json
{"hooks": {"PostToolUse": [{"matcher": "*", "hooks": [
  {"type": "command", "command": "voicehook-agent-hook post-tool-use", "timeout": 5}]}]}}
```

`voicehook-agent-hook` is a light console script (no livekit import, starts fast);
`voicehook-agent hook post-tool-use` does the same. The hook always exits 0 and prints
nothing.

- **A line contains:** local time, the tool name (`[A-Za-z0-9_.:-]`, max 40) and the
  tool's own `description` if it has one (Bash, Agent/Task), max 120 chars:
  `HH:MM:SS Tool: description` or `HH:MM:SS Tool`.
- **A line never contains:** command text, arguments, file paths, file contents or
  tool output. The description runs through a secret scrubber (API keys like `sk_`,
  `rk_`, `re_`, `whsec_`, `vhw_`, `ghp_`, `github_pat_`, `xox?-`, `AKIA`, `AIza`,
  `Bearer ...`, JWTs, `key=`/`token=`/`password=`/`secret=` values, long base64/hex
  strings become `[redacted]`); the join scrubs again before publishing.
- **Which call:** `VOICEHOOK_SESSION=<slug>/<identity>` wins; otherwise the one live
  join on this machine. With zero or several live joins nothing is written, so one
  Claude session never leaks into another call. The file is cleared when a join starts
  and ends, mode 0600, trimmed to the last 50 lines above 200.

## Install

### One-shot (per-call, recommended)

```bash
uvx voicehook-agent join 'https://voicehook.ai/r/<slug>?invite=<code>' --name Claude --model opus-5.5
```

[uv](https://github.com/astral-sh/uv) downloads the package on demand. Zero state.
Until the package is on PyPI use
`uvx --from git+https://github.com/voicehook-ai/voicehook-agent voicehook-agent ...`.
Runtime dependencies are only `livekit` and `httpx`; Python >= 3.10, Linux/macOS
(the control socket is a Unix socket).

### Persistent (one-time install)

```bash
uv tool install voicehook-agent
# or:
pipx install voicehook-agent
```

Then:

```bash
voicehook-agent join 'https://voicehook.ai/r/<slug>?invite=<code>' --name Claude --model opus-5.5
```

## Agent-skill registration

Append this skill description to your agent's instructions (e.g. `~/.claude/CLAUDE.md` for
Claude Code, `$CODEX_HOME/skills/voicehook-agent/SKILL.md` for Codex):

```bash
curl -fsSL https://voicehook.ai/agent/SKILL.md
```

The agent then knows to invoke `voicehook-agent join <url> --name <Name> --model <model>` whenever a user
shares a voicehook invite.

## Protocol

### Interactive mode (default)

```
$ voicehook-agent join https://voicehook.ai/r/abc-def-ghi-XYZ4?go=1 --name Claude --model opus-5.5
[system] connecting room=abc-def-ghi-XYZ4 as identity=claude-mbp-7f3a via https://voicehook.ai
[system] connected — 1 peers: ['agent-AJ_qwerty1234']
[hint] type a line to operator.say (voice-ai speaks it). /q to quit (Ctrl-D no longer quits under --keep-alive).
[user] Hallo, wer bist du?
Ich bin dein Pair-Programming-Brain.    ← typed by agent (voice-ai TTS speaks it)
[agent] Ich bin dein Pair-Programming-Brain.
[user] super, lass uns starten
…
```

### JSON mode

```bash
voicehook-agent join <url> --name Claude --model opus-5.5 --json
```

stdout (JSONL):
```json
{"role": "user", "text": "Hallo", "topic": "transcript"}
```

stdin (JSONL):
```json
{"text": "Hi there"}                                          → operator.say (default)
{"topic": "operator.persona", "text": "Du bist X..."}           → live system-prompt update
{"topic": "operator.interrupt"}                                 → cut off voice-ai
{"topic": "operator.inject", "role": "user", "text": "..."}     → force voice-ai reply
```

## Topics

| Topic              | Direction | Purpose                                |
|--------------------|-----------|----------------------------------------|
| `transcript`        | in        | Live turns, `role` = `user` / `operator` (your `operator.say`, after it was spoken) / `agent` (voice-ai's own answer) |
| `transcript.live`   | in        | `{phase, role, id, text?, interrupted?}`: your `say` started (`start`, full text) / finished (`end`) playing; for the browser only, NOT proof it was spoken (use `transcript`) |
| `_wake`             | out*      | Wake marker on each finalized user-turn (#12) |
| `_meta`             | out*      | Connection / room-state events         |
| `operator.say`        | out       | TTS push; tagged `_seq`/`_ts` (#9). `mode`: `revise` (default: if unspoken text is pending the agent stops and answers with `operator.revise`), `overwrite` (your merged answer), `append` (queue) |
| `operator.persona`    | out       | live update voice-ai system prompt     |
| `operator.interrupt`  | out       | stop everything; unspoken rest comes back as `operator.revise` |
| `operator.revise`     | in        | agent → you: `{unspoken[], new, text}` — merge into ONE statement, send with `mode:"overwrite"` within 8s |
| `operator.inject`     | out       | force voice-ai to react (user-role)    |
| `operator.backchannel`| out       | silent operator↔agent side-channel, relayed as-is (#10) |
| `operator.status`     | out       | your status board `{doing, open[], done[], faq?[{q, a}]}` (0.7.0, `status` command; `faq` 0.10.0); replaces the last one, never spoken |
| `operator.activity`   | room      | 0.10.0: `{lines[], ts}`, newest 15 tool-call lines of the coding agent (PostToolUse hook), on change, at most every 5 s |
| `operator.alive`     | room      | 0.8.0: `{alive, ts, idle_s}` every 10 s while the agent serves `next`/`say` (within 15 s); nothing while orphaned; `alive:false` on leave. The web UI dims the operator after ~20 s without it |
| `operator.say_status` | in    | 0.9.0: `{seq, state, spoken_chars}` per state change of your say; `next` carries `say_status`, `says` the table |
| `operator.status_request` | in    | the user asked what you are doing; `next` yields `{"type":"status_request"}` |

*`out` here = emitted on the CLI's **stdout** (not published to the room).

## Relay flags

Hardening flags (0.2.0) for unattended / background relay operation:

| Flag | Issue | Effect |
|------|-------|--------|
| `--keep-alive` / `--no-keep-alive` | #6 | stdin-EOF does **not** quit; auto-reconnect (exp. backoff, cap 30s) on transient disconnect until the host leaves / room closes / `/q` / SIGTERM. Default: on. |
| `--notify-url <url>` | #12 | POST `{role,text,room,timestamp}` to `<url>` on each finalized user-turn. |
| `--wake-only-user` / `--wake-all` | #12 | Only role=user wakes (default); `--wake-all` also wakes on agent turns (debug). |
| `--suppress-echo` | #10 | Drop the agent's own relayed TTS (role=agent transcript matching a recent `operator.say`) from the stdout stream. voicehook v4 marks that echo as `role=operator`, so the flag currently has no effect there. |
| `--say-ttl <sec>` | #9 | Drop a `operator.say` older than `<sec>` seconds, or superseded by a newer user-turn, instead of speaking it stale. |
| `--strict-relay` | #8 | Inject a bundled strict-relay persona at connect: the voicebot speaks **only** pushed text and never self-generates. Reuses `--persona-file` semantics; overridden by `--persona`/`--persona-file`. |
| `--idle-timeout <min>` | 0.5.0 | Leave when the agent sent no `say`/`next`/stdin line for `<min>` minutes (default 10, `0` off). |
| `--owner-pid <pid>` | 0.8.0 | Leave (with an announcement) as soon as `<pid>` ends, e.g. `--owner-pid $PPID`; repeatable; env `VOICEHOOK_OWNER_PID`. `$VOICEHOOK_AGENT_HOME/holder` is watched too. |
| `--idle-say <text>` | 0.5.0 | Announcement before an idle leave (`''` = silent). |
| `--force-persona` | 0.5.0 | Push persona/mode/graph even if another operator agent is in the room. |
| `--username <name>` | 0.9.0 | The user's first name: in the greeting and, since 0.9.0, sent to the server (token `username=`, bridge join `username`) as participant attribute `vh.user`, so the voicebot knows whom it talks to. |
| `--status-due <sec>` | 0.9.0 | `next` adds `status_due` + `hint` once the board is older than `<sec>` while work is in progress (default 45, env `VOICEHOOK_STATUS_DUE`, 0 = age rule off). |
| `--no-control` | 0.5.0 | No local control socket (`say`/`next`/`leave`/`status` off). |
| `--transport auto\|webrtc\|bridge` | 0.6.0 | How to reach the room. `auto` (default): WebRTC; the HTTPS bridge when `HTTPS_PROXY`/`ALL_PROXY` is set or the WebRTC connect fails/times out (one retry, logged). See below. |

### HTTPS bridge (0.6.0): cloud sandboxes and proxy networks

Some environments (claude.ai/code cloud sessions, corporate networks) only allow HTTPS
through an HTTP CONNECT proxy. libwebrtc does not use that proxy, so the WebRTC join
fails with `wait_pc_connection timed out`. The voicehook server then joins the room for
you (same token and `vh.*` attributes as a WebRTC join) and relays the data channel over
plain HTTPS: `POST /api/bridge/join|send|leave` up, Server-Sent Events
(`GET /api/bridge/events`) down, the session key only in an `Authorization: Bearer`
header. The HTTP client honours `HTTPS_PROXY`/`HTTP_PROXY`/`ALL_PROXY`/`NO_PROXY`.

- `--transport auto` (default): proxy in the env -> bridge right away
  (`{"text": "transport=bridge (auto: HTTPS_PROXY is set ...)", "topic": "_meta"}`);
  otherwise WebRTC, and on a failed connect one retry via the bridge
  (`webrtc connect failed or timed out; retrying once via the HTTPS bridge`).
- `--transport webrtc` / `--transport bridge` force one.
- Everything else is identical: `--json` stream, FIFO/stdin input, `say`/`next`/`leave`/
  `status`, idle and persona guard (both still run in the CLI).
- No install possible at all (installs blocked)? The bridge also works with plain curl,
  see Quickstart A in [SKILL.md](SKILL.md) and the endpoint table in
  [OPERATOR-PROTOCOL.md](https://github.com/voicehook-ai/voicehook-v4/blob/main/docs/OPERATOR-PROTOCOL.md#https-bridge-no-webrtc-no-install).

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
  *ack* that a `operator.say` was spoken, so "spoken within TTL" is approximated
  by age + supersede-by-newer-user-turn. The wire payload carries `_seq`/`_ts`
  so a future server ack can correlate. Delivery-ack (#10 F8) and a true
  interrupt-confirmation (#10 F7) need server support and are not implemented.
- **#8 strict-relay** is enforced via persona injection only. Hard server-side
  enforcement (LLM self-generation truly disabled) is tracked server-side
  (voicehook-v3#28/#48); the CLI ships the strongest available client lever.

### voicehook v4 server behaviour (Stand 2026-10-01)

Reference: [voicehook-v4 docs/OPERATOR-PROTOCOL.md](https://github.com/voicehook-ai/voicehook-v4/blob/main/docs/OPERATOR-PROTOCOL.md).

- **Transcript roles.** `user` = final STT of the human; `operator` = your
  `operator.say`, published only after voice-ai spoke it (on an interruption only
  the spoken part); `agent` = voice-ai's own answer. No `operator` line after a
  push means it was not spoken (yet).
- **Live mode (Gemini Live).** `GET /api/live/status` returns `{"available":bool}`;
  `POST /api/live-room {identity, ttl_seconds?}` returns the host-call format plus
  `invite_url`, `expires_in`, `agent`. `402` = monthly live budget used up (default
  10 USD per UTC month), `404` off, `503` not configured, `429` rate limit. Join the
  `invite_url` as usual. In live rooms `operator.say` is not verbatim: the model says
  it in its own words.
- **Speech and speaker filter (pipeline mode).** Only detected speech reaches the
  STT, and background voices are dropped before the LLM. Learning phase at the
  start: until one speaker has about 3 s of speech, everything passes.

## Environment

| Variable               | Default                 | Purpose                          |
|------------------------|-------------------------|----------------------------------|
| `VOICEHOOK_API_BASE`   | `https://voicehook.ai`  | Token-mint endpoint base URL     |
| `VOICEHOOK_AGENT_HOME` | `~/.voicehook-agent`    | Root of the session dirs (control sockets) |
| `HTTPS_PROXY` / `ALL_PROXY` | unset              | Set -> `--transport auto` uses the HTTPS bridge; also used by the HTTP client |

## License

MIT.
