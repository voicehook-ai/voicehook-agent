# voicehook-agent protocol

How `voicehook-agent join` talks to the agent (stdin/stdout) and to the call (LiveKit data
channel or HTTPS bridge). Back to the [README](../README.md). Commands and install:
[CLI.md](CLI.md). Server side: [OPERATOR-PROTOCOL.md](https://voicehook.ai/agent/OPERATOR-PROTOCOL.md).

## Interactive mode (default)

```
$ voicehook-agent join https://voicehook.ai/r/abc-def-ghi-XYZ4?go=1 --name <your-own-name> --model <your-model-name>
[system] connecting room=abc-def-ghi-XYZ4 as identity=claude-mbp-7f3a via https://voicehook.ai
[system] connected — 1 peers: ['agent-AJ_qwerty1234']
joined via voicehook.ai · Talk with your agents · https://voicehook.ai/?utm_source=agent-join&utm_campaign=viral   ← stderr, once
[hint] type a line to operator.say (voice-ai speaks it). /q to quit (Ctrl-D no longer quits under --keep-alive).
[user] Hello, who are you?
I am your pair-programming brain.    ← typed by agent (voice-ai TTS speaks it)
[agent] I am your pair-programming brain.
[user] great, let's start
…
```

## JSON mode

```bash
voicehook-agent join <url> --name <your-own-name> --model <your-model-name> --json
```

stdout (JSONL):
```json
{"role": "user", "text": "Hello", "topic": "transcript"}
```

Join hint (0.14.0): once per join, right after the `connected` event, one `_meta` event for
YOUR user (pass it on once, never `say` it; plain mode prints it as one stderr line instead):
```json
{"role": "system", "topic": "_meta", "_meta": "hint", "text": "joined via voicehook.ai · Talk with your agents", "url": "https://voicehook.ai/?utm_source=agent-join&utm_campaign=viral"}
```
Off: `--quiet` or `VOICEHOOK_QUIET=1`. A reconnect does not repeat it.

stdin (JSONL):
```json
{"text": "Hi there"}                                          → operator.say (default)
{"topic": "operator.persona", "text": "You are X..."}           → live system-prompt update
{"topic": "operator.interrupt"}                                 → stop your own running say
{"topic": "operator.inject", "role": "user", "text": "..."}     → force voice-ai reply
```

## Topics

| Topic              | Direction | Purpose                                |
|--------------------|-----------|----------------------------------------|
| `transcript`        | in        | Live turns, `role` = `user` / `operator` (an `operator.say`, after it was spoken; `op` = whose) / `agent` (voice-ai's own answer); `speaker` = display name (v4, 0.11.0) |
| `transcript.live`   | in        | `{phase, role, id, text?, interrupted?}`: your `say` started (`start`, full text) / finished (`end`) playing; for the browser only, NOT proof it was spoken (use `transcript`) |
| `_wake`             | out*      | Wake marker on each finalized user-turn (#12) |
| `_meta`             | out*      | Connection / room-state events         |
| `operator.say`        | out       | TTS push; tagged `_seq`/`_ts` (#9). `mode`: `append` (default since 0.11.0: queue at the end), `overwrite` (replaces only your own unspoken says), `revise` (your own unspoken says are stopped, `operator.revise` comes back to you only); `priority:"urgent"` interrupts whoever speaks |
| `operator.persona`    | out       | live update voice-ai system prompt     |
| `operator.interrupt`  | out       | stop your own output (the only way, besides `urgent`, to cut a running say); your unspoken rest comes back as `operator.revise` |
| `operator.revise`     | in        | agent → you only: `{unspoken[], new, text, owner}`: your `mode:"revise"` replaced these not-started says; if any still matters, send ONE merged say with `mode:"overwrite"` |
| `operator.inject`     | out       | force voice-ai to react (user-role)    |
| `operator.backchannel`| out       | silent operator↔agent side-channel, relayed as-is (#10) |
| `operator.status`     | out       | your status board `{doing, open[], done[], faq?[{q, a}]}` (0.7.0, `status` command; `faq` 0.10.0); replaces the last one, never spoken |
| `operator.activity`   | room      | 0.10.0: `{lines[], ts}`, newest 15 tool-call lines of the coding agent (Pre/PostToolUse hook, 0.13.0 also `voicehook-agent activity` notes), on change, at most every 5 s |
| `operator.alive`     | room      | 0.8.0: `{alive, ts, idle_s}` every 10 s while the agent serves `next`/`say` (within 15 s); nothing while orphaned; `alive:false` on leave. The web UI dims the operator after ~20 s without it |
| `operator.say_status` | in    | 0.9.0: `{seq, state, spoken_chars}` per state change of your say; `next` carries `say_status`, `says` the table |
| `operator.status_request` | in    | the user asked what you are doing; `next` yields `{"type":"status_request"}` |
| `operator.visual`    | room      | `{shape, emotion?}`: draw a shape in the ring (`show`; bridge: `POST /api/bridge/visual`) |

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
| `--no-human-timeout <min>` | 0.10.1 | Leave when no human has been in the room for `<min>` minutes (default 2.5, just above the server grace of 120 s; `0` off); a reconnect does not reset it. Server call end (`call_end`, `ROOM_DELETED`, HTTP 410) always ends the join, no reconnect even with `--keep-alive`. 0.10.2: HTTP 409 at join (no human in the room yet) prints a clear message and retries every 5 s for up to 2 min (then exit code 6); 410 exits at once. |
| `--owner-pid <pid>` | 0.8.0 | Leave (with an announcement) as soon as `<pid>` ends, e.g. `--owner-pid $PPID`; repeatable; env `VOICEHOOK_OWNER_PID`. `$VOICEHOOK_AGENT_HOME/holder` is watched too. |
| `--idle-say <text>` | 0.5.0 | Announcement before an idle leave (`''` = silent). |
| `--force-persona` | 0.5.0 | Push persona/mode/graph even if another operator agent is in the room. |
| `--username <name>` | 0.9.0 | The user's first name: in the greeting and, since 0.9.0, sent to the server (token `username=`, bridge join `username`) as participant attribute `vh.user`, so the voicebot knows whom it talks to. |
| `--status-due <sec>` | 0.9.0 | `next` adds `status_due` + `hint` once the board is older than `<sec>` while work is in progress (default 45, env `VOICEHOOK_STATUS_DUE`, 0 = age rule off). |
| `--activity-due <sec>` | 0.13.0 | `next` adds `activity_due` + `activity_age_s` + `activity_hint` once no new `activity.log` line came for `<sec>` while work is in progress (default 60, env `VOICEHOOK_ACTIVITY_DUE`, 0 = off); silent while hook lines arrive. |
| `--stale-error <sec>` | 0.13.0 | `next` adds `stale_error` `{status_age_s?, activity_age_s?, message}` on every output while you are active and board or activity log are older than `<sec>` (default 60, env `VOICEHOOK_STALE_ERROR_S`, 0 = off). |
| `--no-control` | 0.5.0 | No local control socket (`say`/`next`/`leave`/`status` off). |
| `--quiet` | 0.14.0 | No join hint (the one line `joined via voicehook.ai ...` for you, never spoken); env `VOICEHOOK_QUIET=1`. |
| `--no-self-update` | 0.12.0 | Do not update when the server names a newer `cli_latest` (see [Self-update](CLI.md#self-update-0120)). HTTP 426 then exits 7 with the upgrade command. |
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
- Server restart / deploy (0.11.0): the server keeps the bridge session (same token).
  A dropped stream, a 502/503/504 or `"reconnect": true` is retried with the SAME token
  for ~30 s (`reconnecting` / `reconnected` in the output); a `say` sent meanwhile is
  delivered once the server is back. Only after that the join loop rejoins.
- Everything else is identical: `--json` stream, FIFO/stdin input, `say`/`next`/`leave`/
  `status`, idle and persona guard (both still run in the CLI).
- No install possible at all (installs blocked)? The bridge also works with plain curl,
  see Quickstart A in [SKILL.md](../plugins/voicehook-join/skills/voicehook-join/SKILL.md) and the endpoint table in
  [OPERATOR-PROTOCOL.md](https://voicehook.ai/agent/OPERATOR-PROTOCOL.md) (section "HTTPS bridge").

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

### voicehook v4 server behaviour (as of 2026-10-01)

Reference: [OPERATOR-PROTOCOL.md](https://voicehook.ai/agent/OPERATOR-PROTOCOL.md).

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
| `VOICEHOOK_NO_SELF_UPDATE` | unset             | `1` = never self-update on `join` (same as `--no-self-update`) |
| `VOICEHOOK_QUIET`      | unset                   | `1` = no join hint (same as `join --quiet`) |
