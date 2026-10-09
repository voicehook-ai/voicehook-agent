# voicehook-join reference: CLI details

Companion to [SKILL.md](SKILL.md) (https://voicehook.ai/agent/SKILL.md). Read it when you
need one of the details below; the skill itself is enough to join and talk. `$D/vh` is the
wrapper from Quickstart A (SKILL.md) or from the optional CLI below. Protocol:
https://voicehook.ai/agent/OPERATOR-PROTOCOL.md.

## Optional: the voicehook-agent CLI (local machine)

Quickstart A in SKILL.md needs no install and is the default. The CLI is for a local machine
that wants more: a direct WebRTC connection, the hooks that keep status board and activity
log fresh on their own, persona and mode control at join, and self-update. It installs
software from GitHub: **install it only with the user's OK.** The install is pinned to one
reviewed commit (CLI 0.14.0).

```bash
R=git+https://github.com/voicehook-ai/voicehook-agent@20f7d851003add33cff4fd798aeb3d89857becc9; export PATH="$HOME/.local/bin:$PATH"
command -v voicehook-agent || uv tool install -q $R || pip install -q --user $R
D=$(mktemp -d /tmp/vh-XXXXXX); mkfifo $D/in; (setsid sleep 86400 >$D/in & echo $! >$D/holder)
printf '#!/bin/sh\nexport VOICEHOOK_AGENT_HOME=%s\n[ "$1" = leave ] || exec "%s" "$@"\n"%s" "$@"; rc=$?; kill $(cat %s/holder) 2>/dev/null; exit $rc\n' \
  $D "$(command -v voicehook-agent)" "$(command -v voicehook-agent)" $D >$D/vh; chmod +x $D/vh
(setsid nohup $D/vh join "{{INVITE_URL}}" --name "<dein-eigener-Name>" --model <dein-Modellname> --json --greet "Hallo, hier ist <dein-eigener-Name>. Worum geht's?" <$D/in >$D/out 2>&1 &)
$D/vh next --help >/dev/null 2>&1 && echo "D=$D ready" || echo "D=$D CLI too old: uv tool install --force $R"
```

- Same `next`/`say`/`leave`/`status` JSON as Quickstart A; `$D/vh` gives the join its own
  control socket (`VOICEHOOK_AGENT_HOME=$D`), so other agents on the machine never collide.
  The CLI switches to the HTTPS bridge on its own behind `HTTPS_PROXY` (`--transport auto|webrtc|bridge`).
- `--name` / `--model` are mandatory (exit 2 without them); `--voice`, `--username` as the
  bridge fields `"voice"`, `"username"`. `setsid` matters: without it the join dies with your tool call.
- Status board with flags: `$D/vh status --doing "deployt den Worker, ETA 2 min" --open "Tests"
  --done "Analyse" --faq "Wann live?::in 2 min"` (`--faq` repeatable, split on the first `::`). `next`
  adds `status_due: true` + `status_reason` (`empty`, `status_request`, `stale` = older than 45 s while
  `doing`/`open` is set; `--status-due SEC`, env `VOICEHOOK_STATUS_DUE`) + `hint` with the exact
  command: run it before your `say`. A progress `say` ("fertig", "live", "deploye") without a fresh
  board returns `status_reason: "say_progress"`. Finished: `--done "..."`, not `vh status ""` (empty = due).
- No `uv`: the `pip` fallback above (PEP 668: add `--break-system-packages`), or Quickstart A.
  The CLI updates itself on join (self-update); `--no-self-update` keeps the pinned commit.
- `$D/out` (JSON lines) shows within ~5 s `connected — N peers` and a `room-state` line with
  a peer of `"kind": "agent"` (the voicebot), then `greet auto-pushed` and your greeting as
  `"role": "operator"` once spoken. `peer-left: … (agent)` = the voicebot is gone: tell the
  user to reload the call tab.


## Activity feed (CLI 0.10.0, hooks 0.13.0)

Delta sees what you do, one line per tool call, and answers "what is <Name> doing" from it.

- Claude Code, **only with the user's explicit OK** (it edits the user's settings):
  `voicehook-agent hook install` once. It merges a PreToolUse and a PostToolUse
  hook (`voicehook-agent-hook pre-tool-use || true` / `voicehook-agent-hook post-tool-use`)
  into `~/.claude/settings.json`, idempotent; re-run it after an update to add the
  PreToolUse hook to an older install. `voicehook-agent hook print` shows the snippet.
- A line is `HH:MM:SS Tool: description`: the tool's own `description` 1:1, scrubbed for
  secrets, written when the tool starts. File tools without one log the basename
  (`Read: relay.py`). Never commands, arguments, full paths, patterns, file contents or
  output. So give every Bash/Agent call a short, speakable `description`.
- The join publishes the newest 15 lines as `operator.activity` `{lines[], ts}`, on change,
  at most every 5 s.
- Without hooks: on every step send your own status line 1:1, 3-8 words, never rephrased:
  `$D/vh activity "Running database migrations"` (appends `HH:MM:SS note: <text>`, scrubbed,
  max 120 chars, no paths, secrets or personal data). Works in Quickstart A and B.
- The hook finds a Quickstart B join through `~/.voicehook/joins/<pid>.json`, under any
  `VOICEHOOK_AGENT_HOME`. With zero or several live joins it writes nothing, so one session
  never leaks into another call (several joins: set `VOICEHOOK_SESSION=<slug>/<identity>`).

### Hook feed for a Quickstart A (curl bridge) join (CLI 0.13.0)

With the CLI installed next to a curl bridge join, the hook can feed that join too, **only
with the user's explicit OK**. Right after the Quickstart A block:

```bash
voicehook-agent bridge-session --save $D/join --base "$(cat $D/base)"
```

It stores only base + session in `~/.voicehook/bridge-session.json` (mode 600, never
printed). The hook then POSTs each line to `/api/bridge/activity` (2 s timeout, at most one
POST per 5 s, the latest line wins, never blocking). On leave run
`voicehook-agent bridge-session --clear`; a 401/404/410 answer removes the file by itself.

## `activity_due` and `stale_error` (CLI 0.13.0)

In speech pauses Delta reads only fresh lines (under 60 s) aloud: what happens right now.

- `next` adds `activity_due: true` + `activity_age_s` + `activity_hint` while work is in
  progress (`doing`/`open` set, or you spoke or sent a board in the last 5 min) and the log
  got no line for 60 s (`--activity-due SEC`, env `VOICEHOOK_ACTIVITY_DUE`, 0 = off). Run
  the command in `activity_hint`. Silent while hook lines arrive; at most one hint per 60 s.
- `stale_error` on EVERY `next` (no rate limit) while you are active and the board or the
  log is older than 60 s (`--stale-error SEC`, env `VOICEHOOK_STALE_ERROR_S`, 0 = off):
  `{status_age_s, activity_age_s, message}`, an age is `null` when that part is fresh. Fix
  it before your `say`. Plain (non `--json`) join output: `!! stale: <message>`.
- Every `next` carries `activity_now {text, age_s}` and `board_now {doing, age_s}`: what
  Delta knows about you (`null` = nothing sent yet).

## Join flags and lifecycle (CLI)

- `--username <Vorname>` (CLI 0.9.0, bridge join field `"username"`): sent to the server as
  `vh.user`, Delta addresses the user by name.
- `--owner-pid <pid>` (CLI 0.8.0, env `VOICEHOOK_OWNER_PID`): the join leaves as soon as
  that process ends, e.g. your agent session. The FIFO holder of Quickstart B
  (`$D/holder`) is watched on its own.
- Idle guard `--idle-timeout MIN` (default 10, 0 = off).
  `--no-human-timeout MIN`: the join leaves when no human is in the room that long.
- Join answered with HTTP 409 (no human in the room yet, CLI 0.10.2): the join says so and
  retries every 5 s for up to 2 min, then exits with code 6. Open the call in the browser.
- Call end (CLI 0.10.1): server `call_end`, room deleted or HTTP 410 = the join exits and
  never reconnects.

## Say receipts (CLI 0.9.0)

- `$D/vh say` returns `{"ok":true,"seq":N}`.
- `next` adds `say_status` `[{seq, state}, ...]` (changes since the last `next`) and
  `say_hint` when one of your says sits in `queued`/`requeued` for more than 20 s.
- `$D/vh says` prints the last state of every own say.

## Topics the CLI sends for you

| topic | payload | sent when |
|---|---|---|
| `operator.status` | `{doing, open[], done[], faq?[{q, a}]}` | `vh status`; replaces the last board, never spoken |
| `operator.activity` | `{lines[], ts}` | hook lines and `vh activity` notes, newest 15, at most every 5 s |
| `operator.alive` | `{alive, ts, idle_s}` | every 10 s while you serve `next`/`say`; `alive:false` on leave |
