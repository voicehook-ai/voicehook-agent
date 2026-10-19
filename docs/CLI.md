# voicehook-agent CLI

Zero-install CLI that lets any LLM agent (Claude Code, Cursor, ZeroClaw, Hermes,
Codex, …) join a [voicehook.ai](https://voicehook.ai) voice call as a second
participant. No SDK and no learning curve: a plain stdin/stdout protocol. In claude.ai
and Claude Desktop, add the custom connector `https://voicehook.ai/mcp` instead (no key,
no install): [guide](https://voicehook.ai/docs/mcp).

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

## Which path: MCP, CLI or bridge? (Oliver 19.10.2026, decision "A")

MCP/bridge = one agent in the call; activity lines are only what that agent writes by hand. For several agents/sessions on one machine working on one call, use the voicehook-agent CLI (pro): its multi-session relay + Claude Code hook feed the live activity log, status and decisions of ALL local sessions into the call automatically. MCP/bridge run on the server and cannot see the user's local sessions.

| path | role | use it when |
|---|---|---|
| **MCP** (`voicehook.ai/mcp`) | **Entry + distribution.** One click as connector or plugin, nothing to install, only path with `create_call`, cards, `decide`, `data`. | Default for a single agent in a call; every cloud/sandbox session; newcomers; whenever you must start the call yourself. |
| **CLI** (`voicehook-agent`) | **Pro tool for multi-agent work.** One machine, many sessions, one call: the multi-session relay (0.15+) and the Claude Code hook feed the activity log from every session automatically; helpers report `decide`/`status` through the relay (0.16+, in review). Base of the "Leitstelle". Also direct WebRTC, persona/mode flags. | Local machine with install rights AND several agents/sessions on the same call, or the human wants the live activity log of all sessions. |
| **Bridge** (`/api/bridge/*`, Quickstart A) | **Protocol underneath + fallback.** Plain HTTPS/REST for any agent. | No MCP client and no CLI; or a feature the other path lacks right now. |

- **Why the CLI stays:** MCP runs on the server and cannot see the sessions on the user's machine. Only a local process knows which Claude Code sessions run, what each does and when one waits for a decision, and feeds that into the call without hand-written notes.
- **Cards from the CLI (0.21.0+):** `cli show --card text|code|link|image` puts the same cards on the call page as bridge/MCP `show` ("Cards on the call page" below); older CLIs only draw ring shapes.
- **CLI install without git** (git blocked, e.g. Xcode license on macOS; PyPI lags), pinned to the reviewed commit, only with the user's OK: `uv tool install --force https://github.com/voicehook-ai/voicehook-agent/archive/75a29390504df7dad491af42b6cb68826b5c6c0f.zip`. Check `voicehook-agent --version` before every call.

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
| `next [--timeout SEC]` | `{"type":"user","text":...}`, `{"type":"revise","text":...,"unspoken":[...],"new":...}` (your `--mode revise` replaced own not-started says; if any still matters, send one merged `say --mode overwrite`), `{"type":"timeout"}`, `{"type":"ended"}`; 0.21.0 `{"type":"voicebot_left","room","text"}` / `{"type":"voicebot_back","room","seconds","text"}` (the voicebot dropped out, e.g. a server update: tell your user in chat, keep calling `next`, never leave; back: `say` works again. A takeover sends neither; also a JSONL line in `--json` mode) | 0, 3 on `ended` |
| `says` (0.9.0) | `{"type":"says","says":[{"seq":3,"state":"spoken","spoken_chars":12,"age_s":4.1,"text":"..."}]}`: last state of each own say (`sent` until the voicebot's first receipt) | 0 |
| `leave [--say TEXT]` | `{"type":"leaving"}` | 0 |
| `status` | room, identity, connected, pending events, idle seconds, peers | 0 |
| `status [TEXT] [--doing T] [--open T]... [--done T]... [-f board.json]` (0.7.0) | `{"type":"board","board":{...}}`: sends your status board | 0 ok, 1 failed |
| `decide --question T [--option T]... [--recommend N] [--context T] [--recent T]` (0.14.4) | `{"ok":true,"id":"d...","number":1}`: a question ONLY the human can decide (Decision Board, not the status board); the answer comes back in `next` as `{"type":"decision","verdict":"yes"\|"no"\|"option"\|"other"\|"unclear","option":N?,"text":"..."?}` | 0 ok, 1 failed |
| `ui open\|close\|min\|medium\|large\|max <window>`, `ui layout <preset>`, `ui move <window> <slot> [--preset P]`, `ui focus <project\|all>` (0.21.0) | one line `ui: window log large`: the humans' call page changes like on their own click (see [Page control](#page-control-ui-0210)) | 0 ok, 1 failed, 2 invalid |
| `notify-me --reason done\|blocked\|decision [--message T]`, `call-me --reason ... [--message T]` | the server's answer, e.g. `{"ok":true,"type":"notify_me","status":"sms_sent",...}` or `{"ok":true,"type":"call_me","status":"refused","why":"no_account"}`: reach the human outside the call (see [Notify the human](#notify-the-human-notify-me-call-me)) | 0 ok, 1 failed, 2 invalid |

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
- **Decisions (0.14.4):** EVERY question the human decides (yes/no, A/B, an approval,
  picking a value) ALWAYS goes to `decide`, plus one short `say` with its number; never only
  `say`, never into the status board (progress only). Yes/no: `voicehook-agent decide --question "Deploy the fix now?"`;
  options: `voicehook-agent decide --question "Which color?" --option Teal --option Grey
  --recommend 0`. The person answers by voice ("yes", "number two", or their own words);
  the result arrives in `next` as `{"type":"decision","verdict":"yes"|"no"|"option"|"other",
  "option":N?,"text":"..."?}`. Confirm it in one short `say`, then act. Change or drop an
  open one with `--id d... [--question/--option/--recommend]` or `--id d... --delete`.
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

### Decision background (0.21.0)

`decide --context "<2-4 plain sentences: what, why, what happens on yes, what on no>"`
(max 1500 chars) and `--recent "<last turn of the asking session>"` (max 600) go into the
`vh.decide` RPC / `POST /api/bridge/decide` (and over the relay for helper sessions) as
`context` / `recent`. The human never sees them; Delta explains the question from them when
asked "what is meant by number 2?". Whitespace is collapsed; longer: error, nothing sent.
With `--id` they change an open decision (`--context ''` clears). The CLI does not fill
`recent` by itself: its hooks never keep the text of a turn. Without any background Delta
may answer `{"type":"decision","verdict":"unclear","text":"... versteht #2 nicht, bitte
einfacher neu stellen"}` (the question stays open): delete it (`decide --id ... --delete`)
and ask again more simply, with `--context`. For a helper session it lands in its inbox
(`relay-next`) like every answer; the main agent's copy (`helper_decision`) carries `text`.

### Page control: `ui` (0.21.0)

```bash
voicehook-agent ui large decisions                 # ui: window dec large
voicehook-agent ui close log                       # ui: window log close
voicehook-agent ui layout dreiteiler               # ui: layout thirds
voicehook-agent ui move aktivität links --preset thirds   # ui: move log l thirds
voicehook-agent ui focus sales engine              # ui: focus salesengine
```

| Part | Canonical (aliases) |
|---|---|
| state | `open` `close` `min` `medium` `large` `max` |
| window | `log` (activity, aktivitaet, aktivität, feed), `dec` (decisions, entscheidungen), `board` (status, statusboard), `wrap` (transcript, transkript, chat), `cards` (karten) |
| preset | `halves` (haelften, hälften), `quarters` (viertel), `thirds` (dreiteiler, drittel), `bigmid` (gross-mitte, big), `bar` (leiste) |
| slot | `l` (left, links), `m` (middle, mitte), `r` (right, rechts), `b` (bottom, unten), `tl` `tr` `bl` `br` `main` `side` |
| project | `voicehook`, `salesengine` (sales engine, se), `kunden` (customers), `sonstiges` (other), `all` (alles) |

Case and spaces/`-`/`_` do not matter. Unknown value or wrong argument count: exit 2,
nothing sent. WebRTC: data packet topic `delta.ui`
`{"v":2,"cmd":"window","win":"log","state":"large","via":"agent"}` (likewise `layout`,
`move`, `focus`), reliable, only to the humans in the room (not to operator agents, not to
the voicebot); without a human nothing is sent (`(no human in the call, nothing shown)`).
Bridge: `POST /api/bridge/ui` with the same body without `v`/`via`; a 422 of the server
exits 1 with its message.

### Silent Mode: human state (0.21.0)

The server sets `vh.mic_muted`, `vh.speaker_muted`, `vh.away` ("1"/"0") on the human
participants. Every `next` carries `"human_state": {"mic_muted", "speaker_muted", "away"}`
(true only when it holds for every human; `speaker_muted` also when away; no human: all
false), and a change comes as `{"type":"human_state","role":"system","mic_muted":..,
"speaker_muted":..,"away":..,"ts":..}`. While `speaker_muted` or `away` the human cannot
hear: says are queued and not spoken, Delta sums up when they come back; put what matters
on the board, cards or `decide`. `mic_muted` alone: Delta still speaks, but expect no spoken
answer. Bridge transport: the same, from the participant attributes the bridge streams.

### Project tag (0.21.0)

`status ... --project NAME` and `activity ... --project NAME` (1-40 chars) send the field
`project` in `operator.status` / `operator.activity` (helper sessions: in the relay body of
`status`). The server uses it instead of the project it derives from names and paths, for
the focus filter (`ui focus`). `activity --project` stays on for the later activity packets
of this join (hook lines too); `status --project` needs a board (TEXT or a flag).

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
voicehook-agent doctor                    # 0.21.0: installed + runnable without ~/.local/bin?
```

0.21.0: the commands carry the **absolute path** of `voicehook-agent-hook` (PATH at install
time, else next to this Python; a symlink such as uv tool's `~/.local/bin` link is kept, so
`uv tool upgrade` does not break it; `$VOICEHOOK_HOOK_BIN` overrides). Claude Code runs hooks
with `/bin/sh` and the session's PATH; a session started by a service or `claude --resume`
from a unit often has no `~/.local/bin`, and a bare `voicehook-agent-hook` was "not found" on
every tool call. Re-running `hook install` rewrites an older bare (or moved) command in
place, never twice. `voicehook-agent doctor` checks every hook of ours under `/bin/sh` with
`PATH=/usr/bin:/bin`: `ok` / `FAIL` per event, exit 1 with the fix (`hook install`).
The snippet below shows the bare form.

```json
{"hooks": {
  "PreToolUse": [{"matcher": "*", "hooks": [
    {"type": "command", "command": "voicehook-agent-hook pre-tool-use || true", "timeout": 5}]}],
  "PostToolUse": [{"matcher": "*", "hooks": [
    {"type": "command", "command": "voicehook-agent-hook post-tool-use", "timeout": 5}]}],
  "PostToolUseFailure": [{"matcher": "*", "hooks": [
    {"type": "command", "command": "voicehook-agent-hook post-tool-use-failure || true", "timeout": 3}]}],
  "UserPromptSubmit": [{"hooks": [
    {"type": "command", "command": "voicehook-agent-hook user-prompt-submit || true", "timeout": 3}]}],
  "Stop": [{"hooks": [
    {"type": "command", "command": "voicehook-agent-hook stop || true", "timeout": 3}]}],
  "Notification": [{"matcher": "*", "hooks": [
    {"type": "command", "command": "voicehook-agent-hook notification || true", "timeout": 3}]}],
  "PermissionRequest": [{"matcher": "*", "hooks": [
    {"type": "command", "command": "voicehook-agent-hook permission-request || true", "timeout": 3}]}]}}
```

PostToolUseFailure, UserPromptSubmit and Stop (0.15.0) only feed the multi-session relay
below; without `~/.voicehook/relay.json` they do nothing. Notification and
PermissionRequest (0.18.0) bring a session that waits for the human into the call (see
"Sessions that wait for you" below). `hook install` adds them to an older install,
idempotently.

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
  Claude session never leaks into another call. 0.15.0: only the Claude Code session
  that started the join (`owner_session` in its `session.json`, in `bridge-session.json`
  for a curl bridge join) writes tool lines there; every other session reaches the call
  labeled through the multi-session relay below, never as the joining agent's own lines. The file is cleared when a join starts
  and ends, mode 0600, trimmed to the last 50 lines above 200.

### Multi-session relay: every Claude Code session of a machine in the call (0.15.0)

One session joins, all other Claude Code sessions on the same machine (Mac, crown, ...)
show up in the call's activity feed, each under its own name. Delta tells it as
"mein Helfer ...". Needs `voicehook-agent hook install` once per machine (the hooks
live in `~/.claude/settings.json`, so every session has them) and a server with the
activity relay (`POST /api/bridge/activity/relay-token`) for bridge joins; a WebRTC join
needs nothing on the server.

**Token distribution.** The joining session mints ONE relay token and writes
`~/.voicehook/relay.json` (atomic tmp + rename, file 0600, dir 0700):
`{url, token, room, expires_at, owner_pid, owner_session, written_at}`.

- `join` over the HTTPS bridge (`--transport bridge`, or `auto` when it falls back):
  right after connect, with its bridge session. `owner_pid` = the join.
- WebRTC `join` (0.17.0, server with `/api/helper/credential`): right after connect the
  join gets the helper credential from the server with its LiveKit token and writes it
  like a bridge join (token kind); it runs the helper pump for the whole join (see
  "Helper access for every main agent" below). An older server: the LOCAL relay.
- WebRTC `join` (0.15.0/0.16.0, or an older server): a LOCAL relay,
  `{kind: "local", dir, label, room, expires_at, owner_pid, owner_session, written_at}`
  without any token. The hooks append `HH:MM:SS relay: Helfer <machine>: <session>: <text>`
  to the join's own `activity.log` (only when its `session.json` names `owner_pid`), the
  join publishes it as `operator.activity`, the same line the server builds for a relay
  POST. No network from the hook. A `relay` `{token, url, expires_in}` in the join answer
  still wins; a bridge join whose mint fails falls back to the local relay too.
- curl or MCP bridge join: `voicehook-agent bridge-session --save join.json` mints it too
  (`--no-relay` skips it). `owner_pid` = `$VOICEHOOK_OWNER_PID`, else `$CLAUDE_PID`.
- Token label = this machine: `$VOICEHOOK_RELAY_LABEL`, else the short host name (max
  22 chars). Feed line: `relay: Helfer <machine>: <session>: <text>`.
- `owner_session` is a hash of `$CLAUDE_CODE_SESSION_ID` of the joining session (never
  the id). Its own hook already feeds the call, so the relay skips that session.
- Cleanup: on leave, call end, SIGTERM or self-update restart (try/finally) and on
  `bridge-session --clear` the owner deletes the file, but only if it still names the
  owner (pid or session hash, and room). Opt-out: `VOICEHOOK_NO_RELAY=1`.
- Readers ignore a symlink, a file of another user or readable by group/world, an
  expired file and one whose `owner_pid` is gone. 401/403/410 from the relay marks the
  token dead (`relay.dead`, a hash of the token); a reader never deletes the file.

### Helper access for every main agent (0.17.0)

The server issues the helper credential (`POST /api/helper/credential`) however the main
agent joined (WebRTC, bridge, MCP); it ends with the call (410). Helpers use it as in
"Helper sessions: Decision Board and own status board" below (`decide`, `status`,
`relay-next`), no voice, no chip, never a second agent in a solo call.

- Main agent joined over WebRTC: right after connect the join asks the server with its
  LiveKit token and writes a token `relay.json` (the local relay of 0.15/0.16 stays the
  fallback for an older server). The join holds the server's helper hub: it long-polls
  `GET /api/helper/pump` (pump token, never printed) and publishes the helpers' packets
  as itself (`operator.status` with `sub`, the `vh.decide` RPC with `sender`, the line
  `relay: Helfer <machine>: <helper>: <text>` in its own activity.log). The answer to a
  helper question goes back to the server (`POST /api/helper/pump/answer`), never into
  the join's own `next` as `decision`. On leave `POST /api/helper/pump/end`: every credential answers 410.
- **Copy for the main agent (0.18.1):** when the human answers a helper's question (server
  hub, local relay or bridge transport), the join's own `next` also returns
  `{"type":"helper_decision","from":"SE To-dos","label":"Mac","id","number","question",
  "verdict","option?","text?","by","hint"}`; the helper still gets its own `decision`. A
  routing hint only: never say it, never act on it yourself. **On helper_decision: send
  the answer to that session (e.g. Claude Code SendMessage / your harness's session
  messaging) so it continues**; helpers are often idle and do not poll `relay-next`.
- Another machine: the credential travels over ssh, never through a chat or a transcript:
  `voicehook-agent helper-credential --label crown | ssh crown 'voicehook-agent helper-import'`
  (on the main agent's machine; `--rotate` invalidates every earlier credential of the
  call). `helper-import` writes `relay.json` (0600, no owner pid); then `decide`, `status`,
  `relay-next` and the hooks of that machine work there. A curl/MCP bridge main agent
  pipes the REST answer:
  `curl -s -X POST "$B/api/helper/credential" -H "Authorization: Bearer $S" -d '{"label":"crown"}' | ssh crown 'voicehook-agent helper-import'`.

**What each session reports** (one line per event, `<session>: <text>`):

| Event | Line | Never sent |
|---|---|---|
| UserPromptSubmit | `neuer Auftrag` plus up to 3 category words of a fixed list (PR, Deploy, Tests, Bugfix, Review, Doku, Refactoring, Merge, Release, UI) | any word of the prompt itself |
| PostToolUse Bash `gh pr create` | `PR #N geöffnet` (number from the PR URL in the output) | command, output |
| PostToolUse Bash `gh pr merge` | `PR #N gemergt` (number from the command or output, else just `PR gemergt`); with `--auto` `PR #N Auto-Merge an`; `--disable-auto` is skipped | command, output |
| PostToolUse Bash deploy | `Deploy gelaufen` (wrangler/vercel/fly/netlify/firebase/supabase ... deploy, `npm run deploy`, `make deploy`, `*deploy*.sh`, kubectl apply, helm upgrade); `gh workflow run *deploy*` says `Deploy angestoßen`. Segments starting with grep/cat/git/echo/shellcheck/... do not count, nor `--dry-run` or `bash -n` | command, output |
| GitHub MCP create/merge pull request | `PR #N geöffnet` / `PR #N gemergt` | tool input/output |
| PostToolUseFailure, `is_error` | `Fehler bei <Tool>`, `Deploy fehlgeschlagen`, `PR #N Merge fehlgeschlagen`; Esc (is_interrupt) is skipped | the error text, the tool's description |
| Stop | `Turn fertig` | |

Other tool calls are not relayed (the joining session's own feed has them). Heredoc
bodies and anything in quotes are removed before matching, so a commit message such as
`fix: run ./deploy.sh only via CI` never reads as a deploy.

**Session name** (label in front of the text): `$VOICEHOOK_SESSION_LABEL`, else the last
`custom-title` of the transcript (the name set with `/rename`), else its last `ai-title`
(the name Claude Code generates), else the label seen before for this session, else the
basename of `cwd` plus 4 hex chars of a hash of the session id (`oliver 3f2a`), so two
sessions in the same folder stay apart. Scrubbed, `|` and `:` removed, max 40 chars.
The `ai-title` is generated from the conversation and lands in the call feed: rename the
session or set `VOICEHOOK_SESSION_LABEL` if that says too much.

**Throttle:** the same line of one session at most once per 120 s; turn and stop lines
at most one per 5 s per session; all sessions together at most 8 lines per 10 s (PR,
deploy and error lines up to 16; the server allows 20 per token). The hook decides
locally (`relay-state.json`, 0600) and hands the POST to a detached child
(2 s timeout, Bearer token, never in the URL or argv; started with `python -I` and cwd
`~/.voicehook`, so no module of the session's project dir is ever imported; a local
relay appends directly, no child). The hook never waits on the
network, prints nothing, always exits 0 and gives up after 3 s.

### Helper sessions: Decision Board and own status board (0.16.0)

A helper session is a Claude Code session of this machine that is not itself in the call
while another one is (`~/.voicehook/relay.json` exists). `decide` and `status
--doing/--open/--done` of a helper go over that relay automatically, under its session
name (`from`, same rule as the hook: `$VOICEHOOK_SESSION_LABEL`, `/rename`, `ai-title`,
fallback). The question shows the helper as sender, the answer comes back only to it, and
every helper has its own card on the status board.

```bash
voicehook-agent decide --question "PR #81 mergen?" --option Ja --option Nein --recommend 0
# {"ok": true, "id": "d0a1b2c3d", "number": 3, "type": "decide", "via": "relay"}
voicehook-agent relay-next --timeout 60      # one JSON line per answer, else {"type":"timeout"}
# {"type": "decision", "id": "d0a1b2c3d", "number": 3, "verdict": "option", "option": 0, "text": "Ja", "by": "Oliver", "via": "relay"}
voicehook-agent decide --question "Deploy?" --wait --timeout 300
# {"ok": true, "id": "d...", "number": 4, "type": "decide", "via": "relay", "decision": {...}}  (or "decision": null, "timeout": true)
voicehook-agent status --doing "Tests fixen" --open "PR öffnen" --done "Repro"
# {"ok": true, "type": "board", "via": "relay", "board": {"doing": "Tests fixen", "open": ["PR öffnen"], "done": ["Repro"]}}
voicehook-agent status ""                    # all empty: removes the helper's card
```

| | |
|---|---|
| Routing | `--session` or `VOICEHOOK_NO_RELAY=1`: never the relay. Session that started the join (its hash in relay.json or a live join's session.json): its own join, as before. Any other Claude Code session: the relay. No Claude Code session id: the relay only when no join control socket answers. `status` without flags always asks the join |
| `--wait` | `decide --wait` alone waits for this answer (up to `--timeout`, default 120 s); `--wait SEC` keeps its old meaning (wait for the join socket). Answers to other questions that arrive meanwhile stay queued for `relay-next` (`~/.voicehook/relay-inbox-<hash>.jsonl`, 0600). With an own join, `--wait` only adds a hint: the answer comes as a `next` event |
| `--faq` | not sent over the relay (warning on stderr) |
| `--context`, `--recent`, `--project` (0.21.0) | sent over the relay too (decide body `context`/`recent`, status body `project`) |
| Errors | `{"ok": false, "status": N, "error": ..., "via": "relay"}`, exit 1 (422/403/409/410/503 as on `/api/bridge/decide`, 504 = the local join did not answer in 20 s). Only 401 marks the token dead |
| Never | the token in a URL, argv or output; question or board text on stderr |

**Board reminder (0.21.0).** With the hooks installed, a helper session whose own board is
older than 3 min, or that has none in this call, gets one short line from its hook while it
works (PostToolUse, UserPromptSubmit), as Claude Code `hookSpecificOutput.additionalContext`
(never a `decision`: nothing is blocked), at most once per 3 min per session:

```
voicehook-Call: Dein Board ist 7 Min alt. Jetzt `vh-helper status "SE Gate" "<Schritt + ETA>"` setzen (oder leeren mit `vh-helper status "SE Gate" ""`, wenn du nichts tust). / voicehook call: your status board is 7 min old. ...
```

- Board age is tracked locally (`~/.voicehook/board-state.json`, 0600: session hash, name,
  room, timestamps): a Bash call of `vh-helper status "<from>" ...` or `voicehook-agent
  status TEXT|--doing|--open|--done|-f` that did not fail counts as set (`""` / all empty =
  cleared). Quoted text and heredocs never count, the command is never sent.
- Name: the `from` the session already used, else its session label (same as the
  activity relay and `voicehook-agent status`). The hint names `vh-helper` when it is on
  PATH (remote relay), else `voicehook-agent status`.
- Idle: after a Stop one detached sleeper per session waits; 15 min without new work it
  clears that session's board (`POST <url>/status {from}` / local relay request). New work
  cancels it.
- Silent for the joining session (its main board has `status_due`), without a usable
  relay.json, with `VOICEHOOK_NO_RELAY=1`; `VOICEHOOK_BOARD_REMIND=0` turns the reminder
  off, `VOICEHOOK_BOARD_IDLE_CLEAR=0` only the idle clear.

**Raw credential in relay.json (0.21.1).** A `helper-credential` answer copied into
`relay.json` by hand (instead of piping it into `helper-import`) made every hook silent (no
`expires_at`). It now counts for `expires_in` from the file's mtime, without an owner.

**Subagents (0.21.1).** Hook events of a subagent (Agent tool) carry the parent's
session_id plus `agent_id`. Each subagent counts as its own helper, also inside the joining
session: own board name (the `from` it used with `vh-helper status`, else `<agent_type> <4
hex>`), the board reminder only after 3 min of work without a board (a short Explore
subagent never sees it), its state (`relay/state`) only once it has a board.

**Answers delivered by the hook (0.21.0).** After a helper session asked a question
(`vh-helper decide "<from>" ...`, `voicehook-agent decide ...`, or a permission prompt /
`AskUserQuestion` that notify.py put on the board), its hook fetches the answers itself:
`GET <url>/next?from=<from>&timeout=0` (1.2 s timeout; local relay: the answer file; the
local inbox of `decide --wait` first) on UserPromptSubmit, Stop, SessionStart and at most
every 20 s on PostToolUse, for 6 h after the last question. A new answer goes into the
session once (deduped by a hash of id + verdict):

```
voicehook-Call: Antwort von Oliver auf deine Frage #3 ("PR #81 mergen?"): Ja. Handle jetzt danach. / Answer from Oliver to your question #3: Ja. Act on it now.
```

as `additionalContext` (PostToolUse, UserPromptSubmit, SessionStart). On Stop a new answer
returns `{"decision": "block", "reason": ...}`, so an idle session goes on with it; without
a new answer Stop never blocks (no loop, also with `stop_hook_active`). `hook install` adds
the `SessionStart` hook (re-run it after the upgrade).

**Session state from the hooks (0.21.0).** The hook reports each helper session's state,
never what the agent writes: PreToolUse / PostToolUse / UserPromptSubmit `arbeitet`,
Notification / PermissionRequest / AskUserQuestion `frage`, Stop `leerlauf`, 3 failed tools
in a row `problem`. **0.21.2:** a tool call that only waits is `wartet`: Bash of `vh-helper
next`, `voicehook-agent next|relay-next`, `sleep`/`wait`, `gh run watch`, `gh pr checks
--watch`, a relay long poll by curl, or an until/while loop whose other commands only look
(curl, grep, echo, ...); the tools Monitor, TaskOutput, BashOutput and an MCP `next`. Board
bookkeeping (`vh-helper status|activity`, `voicehook-agent status|activity`) keeps `wartet`;
anything else (Edit, Write, build, test, unknown commands) stays `arbeitet`. A server without
`wartet` (422) gets `leerlauf` instead. Only on change (same state again at most every 10 min), detached `POST
<url>/state {from, state}`; the server merges it into the helper's card (`operator.status`
with `sub` and `state`). Remote relay only; an older server (404) is ignored.
`VOICEHOOK_SESSION_STATE=0` turns it off.

**Remote relay** (relay.json with `url` + `token`; `url` = `<base>/api/activity/relay`),
Bearer relay token: `POST <url>/decide {from, question?, options?, recommend?, id?,
delete?, context?, recent?}` -> `{ok, id, number}`; `POST <url>/status {from, doing?, open?,
done?, project?}` ->
`{ok}`; `GET <url>/next?from=<label>&timeout=<0..25>` -> `{events: [...]}` (longer
`--timeout` = several calls).

**Local relay** (`kind: local`, WebRTC join): no network, files in the join dir (checked
like relay.json: a regular file of ours, 0600, never a symlink, and the dir's
`session.json` names the relay's `owner_pid`):

- `relay-req.jsonl`: the helper appends one line per request under flock,
  `{"v":1, "rid":"<16 hex>", "from":"<label>", "op":"decide"|"status", "body":{...}, "ts":<unix>}`
  (max 8 KB per line).
- The join drains it every 0.3 s and runs the `vh.decide` RPC with
  `sender = "<machine>: <from>"`, or `operator.status` with `sub = "<machine>: <from>"`
  (its own board stays untouched, a helper request never resets its idle timer).
- `relay-ans-<hash of from>.jsonl` (written by the join, capped at 256 KB): one
  `{"type":"result", "rid":..., "ok":..., "id"?, "number"?, "status"?, "error"?}` per
  request, and every later `operator.decision` event whose `sender` is that helper's
  (instead of the join's own `next`). The helper takes the lines it reads (flock).
- Both files are deleted on join start and end.

### Sessions that wait for you: permission prompts and questions (0.18.0)

A Claude Code session that waits for the human (permission dialog, `AskUserQuestion`,
MCP elicitation, idle at the prompt) used to show that only as a desktop push. With the
hooks installed (`voicehook-agent hook install`, re-run it after the upgrade) it lands in
the call, for the joining session and for every helper session:

| Hook event | Decision Board | Feed line |
|---|---|---|
| `PermissionRequest` | yes/no `Freigabe für Bash: Remove node_modules directory?` (the tool's own description, a file tool's basename, else only the tool name) | `wartet auf Freigabe: Bash` |
| `PreToolUse` of `AskUserQuestion` | the question itself, its option labels as options (2-3; more: yes/no); `(+N weitere Fragen in der Session)` for more questions | `hat eine Frage` |
| `Notification` `permission_prompt` | yes/no with the notification text (`Claude needs your permission to use Bash`), only when no more specific question is open | `wartet auf Freigabe: Bash` |
| `Notification` `elicitation_dialog`, `elicitation_url_dialog`, `agent_needs_input` | yes/no with the notification text | `hat eine Frage` |
| `Notification` `idle_prompt` | nothing | `wartet auf Eingabe` |
| `Notification` `elicitation_complete`, `elicitation_response` | removes the open question | |
| others (`auth_success`, `agent_completed`, `quota_*`) | nothing | nothing |

Without `notification_type` (older Claude Code) the message decides: a permission text
asks, a "waiting for your input" text is a feed line.

- **Who asks:** the session that started the join asks over its own control socket (the
  answer arrives in its `next` as a `decision` event; feed line `HH:MM:SS Notification: ...`
  in its own log). Every other session asks as a helper over `relay.json` with `from` =
  its session name (same rule as the relay); the answer goes to that helper's inbox
  (`voicehook-agent relay-next`) and, on servers with `helper_decision`, as a copy to the
  main agent.
- **Never automatic:** the hook prints nothing, so Claude Code's permission flow stays
  exactly as it was; the human still approves in the session. An answer in the call is
  information for the agents, not an approval.
- **No spam:** one open question per session; the same question again while it is open,
  or a generic push while a specific question is open, is dropped; a generic push within
  15 s after the session went on is stale. At most one new question per 10 s per session
  and 6 per minute for all sessions together.
- **The question leaves the board** when the session goes on: `PostToolUse` /
  `PostToolUseFailure` of the tool that asked (any tool for a generic push),
  `UserPromptSubmit`, `Stop`. A delete before the decide answer came is done by the
  child as soon as it has the id.
- **Never sent:** paths (only the basename), URLs beyond the host, secrets (same
  scrubber), command text. Max 160 chars per question, 40 per option.
- State: `~/.voicehook/notify-state.json` (0600, flock): per session hash the open
  question's id, a hash of its text and the session name, never the question text or a
  session id. The decide runs
  in a detached child (`python -I`, cwd `~/.voicehook`, job on stdin); the hook never
  waits, always exits 0, gives up after 3 s.

Follow-up (not built): answering a permission prompt FROM the call (a PermissionRequest
hook that waits for the call's answer and returns `allow`/`deny`). It needs an explicit
design first: who may approve which tool, timeouts, and that a wrong voice answer never
runs a destructive command.

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

## Cards on the call page (`show --card`, 0.21.0)

`voicehook-agent show --card text|code|link|image` puts the same card on the call page as
bridge `POST /api/bridge/show` and MCP `show`: never spoken, a new card shows up as a small
preview top left. Same session resolution as `say`.

```bash
voicehook-agent show --card code --title "Fix" --text "git push origin main"
voicehook-agent show --card text --file notes.md --panel cards
voicehook-agent show --card link --text https://voicehook.ai
voicehook-agent show --card image --title "Ablauf" --file swimlane.svg --large
voicehook-agent show --panel board          # only open a panel
```

| Flag | Meaning |
|------|---------|
| `--card KIND` | `text` (default look), `code` (monospace, copy button), `link` (exactly one http(s) URL), `image` |
| `--title T` | card heading, max 80 chars |
| `--text T` / `--file PATH` | the content (`-` = stdin); text/code/link: UTF-8 text max 4000 chars; image: svg (max 300 KB), png, jpeg or webp (max 1 MB), type from the magic bytes |
| `--panel P` | open on the humans' page: `cards` (card view), `board`, `log`, `decisions`; `none` closes all |
| `--large` | open the card view big in the middle with this card at full size (implies `--panel cards`); for diagrams such as swimlanes with many steps, arrows and labels |

SVG may draw anything, but never run or load anything: no `<script>`, `<foreignObject>`,
`on*` attributes, entities or external references (only `#id` and `data:image`). Invalid
input: exit 2, nothing sent, never cut. Not combinable with the shape flags below.

Transport: WebRTC join -> the CLI publishes topic `operator.card`
`{id, seq, kind, title, text, at, url?, mime?}` itself (the page takes cards from every agent
participant; the voicebot keeps the last 20 for a reload) and the panel as `delta.ui`
`{panel, open, via: "agent", large?, id?}` only to the humans. An image goes to the server
first: `POST /api/card/image {image}` with the join's own LiveKit token as Bearer (after it
expired: the helper pump token; no new credential, max 10 per minute), the answer
`{id, url, mime}` (signed, room-bound URL) goes into the card. Bridge join ->
`POST /api/bridge/show` with the same body. Answer:
`{"ok":true,"type":"card","via":"datachannel"|"bridge","id","seq","kind","panel"?,"large"?}`;
an older server without `/api/card/image` answers image cards with a clear error (text, code
and link still work).

## Speech recognition vocabulary (`vocab add`, 0.21.0)

The call keeps mishearing a word (a product, a name, jargon)? Teach it:

```sh
voicehook-agent vocab add "Trello" --wrong "Erzähli"   # --wrong is repeatable
voicehook-agent vocab add "Zapmail"                    # plain word, no known mishearing
```

The running join POSTs `/api/bridge/vocab` (server voicehook-v4) with its own operator
credential (WebRTC: its LiveKit token, bridge transport: its session), never as the user.
The server adds to the vocabulary of the account that pays for the call: add only, the
agent can neither read nor delete the list (the user manages it on https://voicehook.ai/konto).
Max 20 entries per request, 100 per call, 50 characters each. Output: one JSON line
`{"ok": true, "type": "vocab", "added": {"terms", "fixes"}, "total", "call_left", "reloaded"}`;
errors `{"ok": false, "type": "vocab", "status", "error"}` (409 = guest call without an
account, 429 = limit, 410 = call ended). Corrections apply right away; in normal mode the
word list reaches the speech recognition at the next pause, in live mode from the next call.

## Notify the human (`notify-me`, `call-me`)

The human left the call page and you are done, blocked or need a decision:

```sh
voicehook-agent notify-me --reason decision --message "Merge PR 92?"   # SMS, max 120 chars
voicehook-agent call-me --reason blocked --message "CI red, need you"  # phone call, max 200 chars
```

`notify-me` sends an SMS with a single-use 10-minute link back into the call (login or PIN);
if it is not opened within 2 minutes the server phones as a follow-up. `call-me` phones the
human who owns the call on their own confirmed number and blocks until the call ends. The
number never comes from the agent; the account's rules (enabled, reasons, quiet hours, budget,
cooldown) decide. The running join POSTs `/api/cli/notify_me` / `/api/cli/call_me` (server
voicehook-v4) with its own LiveKit token (expired: the helper pump token), bridge transport
`/api/bridge/notify_me` / `/api/bridge/call_me` with its session. Output: one JSON line, the
server's answer plus `ok`/`type`: `{status: refused, why}` or `{status: sms_sent|sms_failed|
insufficient, phone_call_id, billed_eur, link_valid_s, fallback_call_in_s}` (notify-me),
`{status: answered|no_answer|busy|failed|insufficient, phone_call_id, billed_eur}` (call-me);
errors `{"ok": false, "type", "status", "error"}` (422 bad reason/message, 429 more than 5 per
minute, 410 call ended).

## Shapes in the ring (`show`)

`voicehook-agent show` without `--card` draws a shape into the ring (the MCP tool `show`
puts cards, like `--card` above; see [/docs/mcp](https://voicehook.ai/docs/mcp)).

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

Without git (git blocked, e.g. Xcode license on macOS; PyPI lags): the same pinned commit as a zip,
`uv tool install --force https://github.com/voicehook-ai/voicehook-agent/archive/<40-hex sha>.zip`;
check `voicehook-agent --version` before every call.

Pinned (multi-session relay needs 0.15.0 or newer, helper decide/status 0.16.0). Releases are tagged `cli-vX.Y.Z`
(see DEVELOPMENT.md); the tag `cli-v0.16.0` is set on the merge commit after the merge.
Skills that must stay byte-stable (voicehook-v4 `REFERENCE.md`) pin the 40-hex commit
sha of that tag instead of the tag name:

```bash
R=git+https://github.com/voicehook-ai/voicehook-agent@cli-v0.16.0
uv tool install -q --force $R || pip install -q --user --force-reinstall $R
voicehook-agent hook install
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

## Other coding agents (0.20.0)

The automation above was Claude-Code-only. `voicehook-agent hook install --agent codex`
brings it to the **OpenAI Codex CLI**; other agents get the local MCP server and the
manual commands (`activity`, `status`, `decide`, `relay-next`).

| capability | Claude Code | Codex CLI | Gemini/Antigravity CLI | opencode | Cursor | pi |
|---|---|---|---|---|---|---|
| line per tool call | hook | hook (`PreToolUse`/`PostToolUse`) | hooks (BeforeTool/AfterTool), no adapter yet | plugin (`tool.execute.*`), no adapter yet | hooks (IDE; cursor-agent fires few), no adapter yet | extension (`tool_execution_*`), no adapter yet |
| turn start/end, error | hook | hook (`UserPromptSubmit`, `Stop`) + `notify` | BeforeAgent/AfterAgent | `session.idle`/`session.error` | `stop`, `postToolUseFailure` | `agent_settled`, `turn_end` |
| text into the session | `additionalContext`, Stop block | same JSON (`additionalContext`, Stop `decision: block`) | `additionalContext` | `session.prompt` (noReply) | `additional_context`, `followup_message` | `sendUserMessage` |
| permission prompt -> Decision | `PermissionRequest`, `Notification` | `PermissionRequest` (no question event) | Notification (observe only) | `permission.asked` | ask on shell/MCP only | none built in |
| MCP tools | remote MCP | `voicehook-agent-hook mcp` | `mcpServers` | `mcp` | `.cursor/mcp.json` | `mcp.json` |

### Codex CLI

```bash
voicehook-agent hook install --agent codex      # $CODEX_HOME (default ~/.codex)
voicehook-agent hook print --agent codex        # only show what it would write
```

Writes, each changed file copied to `<file>.voicehook-backup-<UTC time>` first (invalid
JSON/TOML: nothing changed):

- `hooks.json`: PreToolUse, PostToolUse, UserPromptSubmit, Stop, PermissionRequest,
  SessionStart -> `voicehook-agent-hook codex <event> || true`. **Codex runs new hooks only
  after you trust them once:** start `codex`, type `/hooks`, trust the voicehook entries.
- `config.toml`: `notify = ["voicehook-agent-hook", "codex-notify"]` (only when no `notify`
  is set; yours stays) and `[mcp_servers.voicehook-local]` (`voicehook-agent-hook mcp
  --agent codex`, env_vars `VOICEHOOK_*` passed through).

What works in Codex:

- Activity line per tool call. Codex sends no tool `description`: Bash shows a category of a
  fixed list (`Bash: Tests`, `Git`, `Build`, `Install`, `Lint`, `PR`, `Deploy`), never the
  command; `apply_patch` shows `Edit: <basename>`.
- Helper sessions (another session owns the join): `neuer Auftrag`, `Turn fertig`, PR
  opened/merged, deploy, `Fehler bei Bash` (exit code != 0) over the relay, like Claude Code.
- Own join (Codex ran `join` from its shell): `Codex: neuer Auftrag` / `Codex: Turn fertig`
  in its own activity log. Session id = `CODEX_THREAD_ID` (hooks: `session_id`).
- Turn end also without trusted hooks (`notify`); Stop hook and notify share a turn list,
  one line per turn.
- Permission prompt -> a decision on the Decision Board (`Freigabe für Bash: <Codex's
  reason>?`); the hook never answers it, you still decide in Codex.
- board.py features (session state, board reminder, answers delivered into the session,
  0.21.0) run in the Codex adapter as well when this CLI has them; their JSON is what Codex
  reads (`additionalContext`, Stop `decision: block`).
- Not available in Codex: questions (`request_user_input`) have no hook event, there is no
  error event besides a failed tool.

### Local MCP server (any MCP client)

`voicehook-agent-hook mcp [--agent NAME]` (also `voicehook-agent mcp`) speaks MCP over
stdio with four tools: `status` {doing, open[], done[]}, `decide` {question, options[2-3]?,
recommend?, wait_s?}, `next` {timeout_s?} and `activity` {text}. A session with its own join
talks to that join's control socket; every other session is a helper over
`~/.voicehook/relay.json`. Helper name: `$VOICEHOOK_SESSION_LABEL`, else the name the
agent's hook used last in that directory, else `<dir> <agent>`. Nothing is spoken, the
token never appears in tool output. No call on the machine: an error result, nothing sent.

## Claude Code plugin

This repository is also a Claude Code plugin marketplace (`voicehook`) with one plugin,
`voicehook-join` in `plugins/voicehook-join/`. Since 1.2.0 it connects Claude to the
voicehook MCP server (`https://voicehook.ai/mcp`, `.mcp.json`, no headers, no keys) and ships
the MCP variant of the voicehook-join skill (`plugins/voicehook-join/skills/voicehook-join/SKILL.md`,
byte-identical copy of https://voicehook.ai/agent/mcp/SKILL.md): Claude joins a call as soon
as you paste an invite link (`https://voicehook.ai/r/<slug>?invite=...`), or calls you
itself with `create_call`. Since 1.3.0 the skill also covers `show`, `decide`, explanatory
images, the call context (`context`, multi-agent rules), `share_text` and retry on 5xx/503.

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
