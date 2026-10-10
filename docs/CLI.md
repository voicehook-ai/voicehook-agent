# voicehook-agent CLI

Zero-install CLI that lets any LLM agent (Claude Code, Cursor, ZeroClaw, Hermes,
Codex, …) join a [voicehook.ai](https://voicehook.ai) voice call as a second
participant. No SDK and no learning curve: a plain stdin/stdout protocol. An MCP
connector for claude.ai and Claude Desktop is coming soon.

Back to the [README](../README.md). Wire protocol, topics and flags: [PROTOCOL.md](PROTOCOL.md).

## TL;DR

```bash
uvx voicehook-agent join 'https://voicehook.ai/r/<slug>?invite=<code>' --name <your-own-name> --model <your-model-name>
```

- **`--name` + `--model` are mandatory** (since 0.4.0): your self-chosen display
  name and the exact model you run on. Name your real model and vendor, never
  'Claude' if you are not Claude. Without both, `join` exits with code 2
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
voicehook-agent join 'https://voicehook.ai/r/<slug>?invite=<code>' --name <your-own-name> --model <your-model-name> --json \
  > ~/.voicehook-agent/call.log 2>&1 &

voicehook-agent say "Hi, I have joined the call."       # speak one line
voicehook-agent next --timeout 60                       # blocks until the user said something
# {"ok": true, "type": "user", "role": "user", "text": "How are you?", "ts": 1.0, "pending": 0}
voicehook-agent say "Fine, thanks. What are we working on?"
voicehook-agent next --timeout 60
voicehook-agent leave --say "See you soon."             # clean exit
```

| Command | Output (one JSON line) | Exit |
|---|---|---|
| `say <text> [--mode append\|overwrite\|revise] [--urgent]` | `{"ok":true,"seq":3}` | 0 ok, 1 failed |
| `next [--timeout SEC]` | `{"type":"user","text":...}`, `{"type":"revise","text":...,"unspoken":[...],"new":...}` (your `--mode revise` replaced own not-started says; if any still matters, send one merged `say --mode overwrite`), `{"type":"timeout"}`, `{"type":"ended"}` | 0, 3 on `ended` |
| `says` (0.9.0) | `{"type":"says","says":[{"seq":3,"state":"spoken","spoken_chars":12,"age_s":4.1,"text":"..."}]}`: last state of each own say (`sent` until the voicebot's first receipt) | 0 |
| `leave [--say TEXT]` | `{"type":"leaving"}` | 0 |
| `status` | room, identity, connected, pending events, idle seconds, peers | 0 |
| `status [TEXT] [--doing T] [--open T]... [--done T]... [-f board.json]` (0.7.0) | `{"type":"board","board":{...}}`: sends your status board | 0 ok, 1 failed |

- `next` returns ONE event, oldest first; `pending` says how many more are queued.
- **Several operators (0.11.0):** a running say is never cut, except by your own
  `operator.interrupt` or `--urgent`. `say` defaults to `--mode append`: queued at the
  end, it starts right after the running one without a gap (the server prefetches the
  TTS). `--mode overwrite` replaces only YOUR says that have not started yet (state
  `queued`), at their place in the queue; if there are none it is simply appended (a
  `requeued` rest counts as started). Never another operator's says. `--mode revise` =
  overwrite plus `operator.revise` to you only, listing the replaced texts (`unspoken`)
  and `new`; nothing is held, nothing cut, and no revise event when nothing was replaced.
  `--urgent` (`priority:"urgent"`) interrupts whoever is speaking and goes first; use
  sparingly.
  `join --voice Puck` (Google Chirp3-HD name, sent as `vh.voice`) picks your own voice;
  without it the server assigns a fixed voice per identity, never Delta's (pipeline mode).
  `operator.revise` / `operator.say_status` reach only the say's owner (`owner` field);
  transcript lines carry `speaker` (display name) and `op` (operator identity), printed
  in `--json` output and on `next` user events when the server sends them.
- **Say receipts (0.9.0):** the voicebot reports each say as `operator.say_status
  {seq, state, spoken_chars}` (`seq` = the `seq` that `say` returned; states `queued`,
  `spoken`, `interrupted`, `requeued`, `replaced`, live `covered`). `next` carries the changes since the
  last `next` as `"say_status": [{"seq":3,"state":"spoken"}]` (`spoken_chars` only for
  `interrupted`/`requeued`); a say stuck in `queued`/`requeued` for more than 20 s adds
  `"say_hint"` (do not push more; if outdated, `say --mode overwrite` a short version).
  `says` shows the last state of every say. Live mode (0.12.0): `covered` (final) =
  your say arrived while the user had the floor, went to the model as context, and
  Delta already said it in his answer; it carries `"note": "Info steckt schon in
  Deltas Antwort, nicht nochmal senden"` (German: "info is already in Delta's answer,
  do not send it again"). Do not send it again. `no_audio` = no audio
  within 4 s (voice engine silent), the voicebot retries once; `dropped` (final) = the
  retry was silent too, the say was never spoken: resend it if it still matters. Both
  carry a `note`; `reason` is passed through (`interrupted` + `max_duration` = the
  voicebot's emergency brake cut a say that never reported finished).
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
  board: `voicehook-agent status --doing "building the fix" --open "Tests" --done
  "Analysis"` (or `-f board.json` with `{doing, open[], done[]}`); `status ""` when
  finished. It replaces the last board at a fixed place in the voicebot's instructions
  (never spoken, server budget 600 chars, at most one update per 5 s applied). The
  voicebot answers "what is Claude doing right now?" from it and calls you by your `--name`.
  When the user asks, `next` yields `{"type":"status_request"}`: send `status` at once.
  `next` adds `"status_stale": true` when your board is older than 5 min and the user
  spoke since.
- **Keep the board fresh (0.9.0):** the voicebot (Delta) answers the user from your
  board while you work in the background; a stale board makes it answer wrong. `next`
  therefore adds `"status_due": true`, `"status_reason"`, `"board_age_s"` and a `"hint"`
  with the exact command when
  - the board is empty or was never set (`empty`),
  - the user asked for the status (`status_request`; that event is also put first in
    line, carries a `"hint"` that the user asked for the status, with the command to
    update the board, and stays due until your next board),
  - the board is older than `--status-due SEC` (default 45, env
    `VOICEHOOK_STATUS_DUE`, 0 = off) while `doing`/`open` is set, or the user spoke
    after it (`stale`). A finished board (only `done`) does not nag by age.

  A `say` that reports progress ("done", "live", "deployed", "merged" ...) without a
  board push since your previous `say` returns `"status_reason": "say_progress"` and
  the same `hint`. Set the board on every request, delegation, result and deploy step;
  `doing` may hold the interim state and an ETA ("deploying the worker, ETA 2 min"), but the
  worker keeps at most 120 chars per entry. When finished, send `--done` items instead
  of clearing (an empty board counts as due). The hint texts are printed in German.
  Example (the `hint` says: board is stale, Delta will answer wrong otherwise, update it
  now with this command; which 3 questions will the user ask next, answer them in advance
  with `--faq`):

  ```json
  {"ok": true, "type": "timeout", "pending": 0, "status_due": true,
   "status_reason": "stale", "board_age_s": 61.2,
   "hint": "Board veraltet, Delta antwortet sonst falsch: jetzt aktualisieren: voicehook-agent status --doing \"<Zwischenstand, ETA>\" --open \"<offen>\" --done \"<erledigt>\". Welche 3 Fragen stellt der Nutzer wahrscheinlich als Nächstes? Beantworte sie vorab per --faq \"Frage::Antwort\""}
  ```
- **FAQ on the board (0.10.0):** on EVERY board update also predict the user's next
  likely questions and answer them in advance, so Delta can answer without asking you:
  `voicehook-agent status --doing "deploying the worker, ETA 2 min" --faq "When is it
  live?::in about 2 minutes" --faq "Do the tests pass?::yes, all green"`. `--faq` is
  repeatable and split on the first `::` (both halves stripped; an item with an empty
  half is skipped with a warning on stderr); at most 6 pairs, question and answer capped
  to 200 chars each. Payload: `{"doing": "...", "open": [], "done": [], "faq": [{"q":
  "When is it live?", "a": "in about 2 minutes"}]}`. Without `--faq` the field is
  omitted. Every `status_due` hint now also asks which 3 questions the user will
  probably ask next, to be answered in advance with `--faq`.
- **Board items stand alone:** every `--open`/`--done` item says WHAT concretely (thing
  and place), readable without context. Decisions waiting for the user come FIRST in
  `--open`, as a question with options and your recommendation. One item per thing, never
  bundles; at most 10 items per list (200 chars each): if more, keep the most important,
  the rest goes to `--faq`. `--doing` names the concrete current step.
  Good: `--open "iOS-Hinweis kurz oder lang? (Empf.: kurz)"`, `--open "PR #250: Überlappung
  mit Gate prüfen"`, `--doing "Prüfe PR #250 auf Überlappung"`.
  Bad: `--open "6 Entscheidungen von Oliver"`, `--open "diverse Fixes"`,
  `--doing "arbeite an PRs"`.

> Since 0.2.0, `--keep-alive` is the default: **stdin-EOF no longer quits** and
> transient room-disconnects auto-reconnect. Run with a closed stdin in the
> background without the FIFO sleep-holder hack. See [Relay flags](PROTOCOL.md#relay-flags).

## How to speak in the call

Everything you send with `say` or `--greet` is read aloud to a person on the phone. **Local style
modes (terse, caveman, telegram style, bullet points) do NOT apply to `say` text:** speak whole,
natural sentences with articles and verbs, like a good radio host or hotline agent.

- **Language:** the user's (German by default); switch only when the user switches.
- **Short:** 1-2 sentences per `say`, each one breath (about 8-12 words, <60 chars). One statement per sentence.
- **Most important first:** result first, then the reason. Active verbs: "I deployed the fix", not "The fix was deployed".
- **Speakable:** round numbers ("almost half", "about eight hundred"); codes and phone numbers in digit groups. No
  abbreviations, symbols, paths, URLs, code, markdown, lists or emoji: say what it means ("the login endpoint").
- **Signal and repeat:** announce longer answers ("Two points. First …"), repeat the core once at the end.
- **Confirm, then ask:** read a task back in one sentence ("Okay, I am deploying the worker."); end with one clear question
  when you need a decision. Pauses come from full stops, not from comma chains.

| Chat style (wrong) | Phone style (right) |
|---|---|
| Deploy green. Tests 812/812. CI ok. | The deploy went through, and all tests are green. |
| PR #161 open → waiting for review. | I opened the pull request. It is waiting for your review. |
| 403 on /api/join, token stale. | Joining does not work because the key has expired. Shall I renew it? |

Newer voicehook servers (v4, Oct 2026) also strip leftover markdown, backticks, emoji and link prefixes and read arrows as "then", but never reword you.

## Activity feed (operator.activity, 0.10.0)

Delta knows what the coding agent is doing in the background, without asking. Claude
Code hooks write ONE short line per tool call into `activity.log` of the running join's
session dir (0.13.0: `PreToolUse` writes it when the tool starts, `PostToolUse` only for
a call Pre did not log); the join publishes the newest 15 lines (oldest first) as
`operator.activity` `{"lines": ["17:12:03 Bash: Run the tests", "17:12:09 Edit: relay.py"],
"ts": 1759418000.0}`, only on change and at most once per 5 s (a change inside the
window goes out when it ends, last one wins).

Install once (merges idempotently into `~/.claude/settings.json`, keeps all other
keys and hooks, refuses to touch invalid JSON):

```bash
voicehook-agent hook install              # or: --settings PATH
voicehook-agent hook print                # the snippet, to paste by hand
```

```json
{"hooks": {
  "PreToolUse": [{"matcher": "*", "hooks": [
    {"type": "command", "command": "voicehook-agent-hook pre-tool-use || true", "timeout": 5}]}],
  "PostToolUse": [{"matcher": "*", "hooks": [
    {"type": "command", "command": "voicehook-agent-hook post-tool-use", "timeout": 5}]}]}}
```

`voicehook-agent-hook` is a light console script (no livekit import, starts fast);
`voicehook-agent hook pre-tool-use|post-tool-use` does the same. The hook always exits 0
and prints nothing (`|| true`: a PreToolUse hook exiting 2 would block the tool, and an
older `voicehook-agent-hook` < 0.13 exits 2 on the unknown `pre-tool-use`). 0.13.0:
`hook install` on an older Post-only install adds the Pre hook, idempotently. Pre and
Post log the same call once (dedupe by `tool_use_id`, kept in `activity.ids` next to the
log, never published).

- **A line contains:** local time, the tool name (`[A-Za-z0-9_.:-]`, max 40) and the
  tool's own `description` if it has one (Bash, Agent/Task), max 120 chars, taken 1:1
  (Claude Code writes 3-8 words anyway); file tools without one (Read, Edit, Write,
  MultiEdit, NotebookEdit) log the file's basename (0.13.0); Grep/Glob and others only
  the tool name: `HH:MM:SS Tool: description`, `HH:MM:SS Read: relay.py` or `HH:MM:SS Tool`.
- **A line never contains:** command text, arguments, full paths, search patterns, file
  contents or tool output. The description runs through a secret scrubber (API keys like `sk_`,
  `rk_`, `re_`, `whsec_`, `vhw_`, `ghp_`, `github_pat_`, `xox?-`, `AKIA`, `AIza`,
  `Bearer ...`, JWTs, `key=`/`token=`/`password=`/`secret=` values, long base64/hex
  strings become `[redacted]`); the join scrubs again before publishing.
- **Which call:** `VOICEHOOK_SESSION=<slug>/<identity>` wins; otherwise the one live
  join on this machine. With zero or several live joins nothing is written, so one
  Claude session never leaks into another call. The file is cleared when a join starts
  and ends, mode 0600, trimmed to the last 50 lines above 200.

### Activity log: keep it filled (activity_due, 0.13.0)

Why: in speech pauses Delta reads only fresh entries (younger than 60 s) aloud, "what is
happening right now". An empty or old log means Delta has nothing to say.

- **Claude Code (recommended):** `voicehook-agent hook install` once. Every tool call logs
  its own short description 1:1 when it starts; nothing else to do.
- **Agents without hooks** (any other LLM agent): send your own short status line 1:1,
  3-8 words, do not rephrase it:

  ```bash
  voicehook-agent activity "Running database migrations"
  # {"ok": true, "type": "activity", "line": "17:14:02 note: Running database migrations"}
  ```

  The running join appends `HH:MM:SS note: <text>` to its `activity.log` (same scrubber,
  max 120 chars; no paths, secrets or personal data). `--session` / `--wait` as for `say`;
  exit 3 = no running join, 1 = empty text.
- **`next` reminds you:** while work is in progress (`doing`/`open` set on the board, or you
  spoke / sent a board in the last 5 min) and no new line came for `--activity-due SEC`
  (default 60, env `VOICEHOOK_ACTIVITY_DUE`, 0 = off), `next` adds the following (the
  `activity_hint` is printed in German and says: activity log silent while you work, send
  `voicehook-agent activity "<3-8 words>"`):

  ```json
  {"ok": true, "type": "timeout", "pending": 0, "activity_due": true, "activity_age_s": 61.0,
   "activity_hint": "Aktivitätslog still, während du arbeitest: ... voicehook-agent activity \"<3-8 Wörter>\" ..."}
  ```

  Silent while hook lines arrive (any hook line in the last 10 min); at most one hint per
  SEC. Its own key `activity_hint`, so a `status_due` `hint` in the same reply stays intact.
- **Automatic flow:** `join` writes a pointer `~/.voicehook/joins/<pid>.json` (0600), so the hook
  finds the join even when it runs with its own `VOICEHOOK_AGENT_HOME` (Quickstart B wrapper);
  several live joins still mean nothing is written. For a curl bridge join (Quickstart A)
  `voicehook-agent bridge-session --save join.json --base https://voicehook.ai` (or the
  Quickstart's own line) stores only `{base, session}` in `~/.voicehook/bridge-session.json`
  (0600; `--clear`, leave/ended and an HTTP 401/404/410 remove it). The hook then POSTs the tool's
  description to `<base>/api/bridge/activity` `{"text"}` with the bridge Bearer token: 2 s
  timeout, never blocking (exit 0), at most one POST per 5 s, the latest line wins (a detached
  flusher sends it when the window ends). `VOICEHOOK_STATE_DIR` moves `~/.voicehook`.
- **`activity_now` / `board_now`:** every `next` shows what Delta currently knows about you:
  `"activity_now": {"text": "Bash: Sending deploy to JEV", "age_s": 12.0}` (newest line last
  published as `operator.activity`, without its time) and `"board_now": {"doing": "...", "age_s": 12.0}`;
  `null` before the first.
- **`stale_error` (no rate limit):** while you are active (`doing`/`open` set, a `say` in the
  last 5 min, or no board yet and the join older than SEC) and your board or your log is
  older than `--stale-error SEC` (default 60, env `VOICEHOOK_STALE_ERROR_S`, 0 = off), EVERY
  `next` carries the following (the `message` is printed in German and says: ERROR, status
  board not updated for 3 min, activity log for 1 min 35 s, run these two commands now):

  ```json
  "stale_error": {"status_age_s": 187, "activity_age_s": 95, "message": "FEHLER: Statusboard seit 3 Min nicht aktualisiert, Aktivitätslog seit 1 Min 35 s. Jetzt: voicehook-agent status --doing \"…\" und voicehook-agent activity \"…\""}
  ```

  An age is `null` when that part is fresh and the message names only what is overdue (same
  shape as the server's bridge `next`); never set = age since the join; while hook lines arrive only
  the board counts. A finished/empty board without a `say` for 5 min stays silent. Plain
  (non `--json`) join output prints `!! stale: <message>` after every user turn.
  `status_due`/`activity_due` stay unchanged.

## Shapes in the ring (`show`)

An agent can draw into the ring of the call UI while it explains something
by voice. Same session resolution as `say`; WebRTC join -> data-channel topic
`operator.visual`, bridge join -> `POST /api/bridge/visual`. Body:
`{"shape": <shape>, "emotion"?: {"label", "valence", "arousal"}}`.

```bash
voicehook-agent show --preset check --emotion joy                     # preset
voicehook-agent show --polygon "0.2,0.9 0.2,0.45 0.5,0.15 0.8,0.45 0.8,0.9" --label House
voicehook-agent show --label "Before, after" --json '{"type":"multi","items":[
  {"type":"polygon","points":[[0.1,0.4],[0.3,0.4],[0.3,0.6],[0.1,0.6]]},
  {"type":"path","d":"M0.38,0.5 L0.62,0.5"},
  {"type":"polygon","points":[[0.7,0.25],[0.9,0.25],[0.9,0.75],[0.7,0.75]]}]}'
voicehook-agent say "Test first, then roll out." --shape-preset arrow_right
```

| Flag | Meaning |
|------|---------|
| `--preset NAME` | `arrow_up arrow_right check cross question loop split3 scale heart bolt one two three` |
| `--polygon "x,y x,y ..."` | own polygon, closed (`--open` = line) |
| `--path D` | SVG path, only `M L Q C Z`, numbers 0..1 |
| `--json SHAPE` | full shape (`preset`, `polygon`, `path`, `multi` with `items`) |
| `--label TEXT` / `--hold-ms N` / `--emotion LABEL` | caption <= 24 chars / 800..8000 ms / ring tint |

Several `--preset/--polygon/--path` become one `multi` (max 4 items). Coordinates 0..1,
(0,0) top left, max 200 points, a path `d` max 2 KB, the whole shape max 8 KB; label max
24 chars without control characters, emoji or `<>`. The CLI validates like the server:
invalid input prints `{"ok":false,"type":"invalid","error":...}` and exits 2, nothing is
sent. At most one shape per 2 s per operator: faster returns `{"type":"rate_limited"}`
(bridge HTTP 429), exit 1. `say --shape-preset NAME | --shape-json J` and `--emotion L` add the fields `shape`
and `emotion` to that `operator.say`; the shape is drawn when the say is spoken.

When to draw: a sequence or loop, a comparison, a structure, yes or no, a count of one
to three. Not with every sentence, at most every few turns, never instead of speaking;
say texts stay whole, natural, phone-ready sentences.

## Install

### One-shot (per call, recommended)

```bash
uvx voicehook-agent join 'https://voicehook.ai/r/<slug>?invite=<code>' --name <your-own-name> --model <your-model-name>
```

[uv](https://github.com/astral-sh/uv) downloads the package from PyPI on demand. Zero state.
To pin a version: `uvx voicehook-agent@0.14.2 join ...`.
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
voicehook-agent join 'https://voicehook.ai/r/<slug>?invite=<code>' --name <your-own-name> --model <your-model-name>
```

Install and update in one line (what the skill does; always upgrades):

```bash
R=git+https://github.com/voicehook-ai/voicehook-agent
uv tool install -q --upgrade $R || pip install -q --upgrade --user $R
```

### Self-update (0.12.0)

The server names `cli_min` and `cli_latest` in every join answer (token mint and bridge
join; the CLI sends its version as `X-VH-CLI`).

- `cli_latest` newer than this CLI: `join` updates in place and restarts itself with the
  same arguments and the same identity (`os.execv`). uv tool installs run
  `uv tool upgrade voicehook-agent`, everything else
  `python -m pip install --upgrade [--user] git+https://github.com/voicehook-ai/voicehook-agent`
  (plus `--break-system-packages` on a PEP 668 system python). Only on the first connect,
  never during a running call, and at most once per join (env marker `VOICEHOOK_SELF_UPDATED`).
  A `next` waiting during the restart gets `{"type":"restarting"}`: call `next` again.
- Update fails: a clear warning, the join goes on with the old version (it is still >= `cli_min`).
- HTTP 426 (this CLI is below `cli_min`): prints the upgrade command, tries one self-update,
  else exits with code 7.
- Off: `join --no-self-update` or `VOICEHOOK_NO_SELF_UPDATE=1` (only an info line then).
- By hand: `voicehook-agent self-update` (`--dry-run` prints the command).
  `voicehook-agent --version` adds `neue Version verfügbar: x.y.z` (German: "new version
  available") when the last server answer named a newer one.

## Claude Code plugin

This repository is also a Claude Code plugin marketplace (`voicehook`) with one plugin,
`voicehook-join` in `plugins/voicehook-join/`. Since 1.2.0 it connects Claude to the
voicehook MCP server (`https://voicehook.ai/mcp`, `.mcp.json`, no headers, no keys) and ships
the MCP variant of the voicehook-join skill (`plugins/voicehook-join/skills/voicehook-join/SKILL.md`,
byte-identical copy of https://voicehook.ai/agent/mcp/SKILL.md): Claude joins a call as soon
as you paste an invite link (`https://voicehook.ai/r/<slug>?invite=...`).

In a Claude Code session:

```
/plugin marketplace add voicehook-ai/voicehook-agent
/plugin install voicehook-join@voicehook
```

Or from your shell:

```bash
claude plugin marketplace add voicehook-ai/voicehook-agent
claude plugin install voicehook-join@voicehook
```

On Claude Code 2.1.275 or later, one step does both:
`/plugin install voicehook-join --marketplace voicehook-ai/voicehook-agent`.

The skill runs by itself when you share an invite link, or by hand as
`/voicehook-join:voicehook-join`. `claude plugin update voicehook-join@voicehook` fetches a
new version, or turn on auto-update for the `voicehook` marketplace under **Marketplaces**
in `/plugin`.

The plugin is the MCP server entry plus the skill; the CLI and its activity hook
(`voicehook-agent hook install`) are separate and not part of the plugin (see [Activity feed](#activity-feed-operatoractivity-0100)). Releasing the plugin:
[DEVELOPMENT.md](DEVELOPMENT.md#plugin-releases).

## Agent-skill registration

Append this skill description to your agent's instructions (e.g. `~/.claude/CLAUDE.md` for
Claude Code, `$CODEX_HOME/skills/voicehook-agent/SKILL.md` for Codex):

```bash
curl -fsSL https://voicehook.ai/agent/SKILL.md
```

The agent then knows to invoke `voicehook-agent join <url> --name <Name> --model <model>` whenever a user
shares a voicehook invite.
