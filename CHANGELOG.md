# Changelog

voicehook-agent CLI. Older versions: the GitHub releases (`cli-v*`) and the version notes in
[docs/CLI.md](docs/CLI.md).

## 0.23.1 (2026-10-22)

Live (Oliver, 22.10.): `show --card image` with a ~700 KB screenshot crashed the join's control
socket ("Separator is not found, and chunk exceed the limit"), the CLI then died on
`BrokenPipeError`. Server side (voicehook-v4 #577, card acks and `relay/show`) is live since 21.10.

- **Real screenshots work:** the control socket read one request line with asyncio's default
  limit of 64 KB; a screenshot (base64 in that line) overran it. Now up to 4 MB.
- **Error message instead of crash:** a request over 4 MB is refused by the CLI before sending
  (`ok:false`, `type` `invalid`, "request too large"); a join that gets an over-limit line
  answers instead of dropping the connection, and the CLI reads that answer instead of failing
  on a broken pipe. An image over 1 MB stays a clear `invalid` (exit 2), nothing sent.
- **`show` ok only with browser confirmation:** every call page loads an image card and answers
  on `vh.card_ack` `{id, ok}`. `show --card image` answers `ok:true` with `shown` only after at
  least one page loaded it; otherwise `ok:false` with `type` `not_shown` (did not load, or no
  page answered) or `no_human`, exit 1.
- **Cards from helper sessions:** `show --card ...` of a session without its own join goes over
  the helper relay (`POST /api/activity/relay/show`, same routing rule as 0.22.2); the card names
  the helper as sender. Same checks and limits, image only over the server relay, no
  `--panel`/`--large`. Ring shapes and `--panel` alone stay with the join (clear error as a
  helper). A WebRTC main agent gets such cards over its helper pump (kind `card`), publishes
  them as itself and confirms them like its own.

## 0.23.0 (2026-10-21)

Oliver: "Für Delta brauchen wir auch einen Data Channel, so wie die CLI untereinander Data
Channel haben", so the agent in the call can give Delta new conversation rules and facts while
the call runs. Delta should know as much as possible and answer himself.

- **`delta-rule add|list|rm` and `delta-note add|list|rm`:** standing rules (how Delta talks or
  behaves) and facts (what he knows) for the rest of the call. Silent, never spoken: the server
  puts them at the end of Delta's context, from his next turn; his core rules win on conflict.
  Max 20 rules and 30 facts, 300 characters each (checked here and by the server, 422, never
  cut). WebRTC: RPC `vh.delta_notes` to the voicebot; bridge: `POST /api/bridge/delta`. Only
  the session in the call, never over the helper relay. `--persist` is accepted for a later
  account memory and has no effect yet. Needs voicehook-v4 with `deltanotes` (older server:
  `delta rpc failed`).
- **Plugin 1.3.17:** skill copy synced with the live https://voicehook.ai/agent/mcp/SKILL.md
  (`context` as short `What:` / `Why:` / `If yes:` / `If no:` lines, shown on the card).

## 0.22.3 (2026-10-21)

Router-Fund (Oliver 21.10.): the main join (WebRTC) saw neither the boards nor the open
questions of its helpers; `voicehook-agent board` gave boards=[], recent=[], open=[] with "server
has no decision listing for operators". The server had a listing for open questions since
voicehook-v4 #519 (RPC `vh.decisions`), the CLI never asked it; helper boards the join itself
published (local relay, server hub) never came back to its own ledger.

- **`board` / `decide --list`:** asks the server (RPC `vh.decisions`, bridge
  `GET /api/bridge/decisions`) and merges it with the join's ledger: `open` (all helpers),
  `recent`, `boards` (every helper board of the room with `name` and `age_s`), `source`.
  Server unreachable or refused: the ledger as before, `note` names the reason.
- **Ledger:** helper boards and questions the join publishes for the server hub (pump) and
  the local relay are recorded too (fallback without the server listing).

## 0.22.2 (2026-10-21)

Live findings with 0.22.1 as a helper (`helper-import`): `status` went over the relay,
`activity` did not ("no running join found" on crown, "several joins running" on a Mac); on
the MacBook three `helper-import`s one after another made every helper post as the last one
("Helfer Zapmail"); `status --faq` warned "not sent over the relay" although the faq arrived
in `meta`, and capped a call at 6 pairs although 3 per item are mandatory.

- **One routing rule** for `status`, `activity`, `decide`, `next`, `show` (`relayops.client_relay`):
  `--session` wins, then the own join of this session, then its own helper credential, then a
  join's relay.json on this machine, else `no-session` with a `hint`. As a helper, `activity`
  posts under its session name (with `--context`/`--faq`), `next` returns the relay events
  like `relay-next`, `show` answers `helper-unsupported` (no relay route for ring/cards).
- **One helper credential per session:** `helper-import [--as NAME]` writes
  `~/.voicehook/helpers/<name>.json` (0600), never the shared relay.json. Without `--as` the
  entry is named by `VOICEHOOK_SESSION_LABEL` or the credential's label and bound to the
  importing session; `--as NAME` (over ssh) is used by the session with
  `VOICEHOOK_SESSION_LABEL=NAME` or as the only unbound entry. Commands and hooks take only
  their own entry, never one of another session. The main join's relay.json (multi-session
  hook feed) is unchanged; an old helper relay.json still works, with a warning.
- **`status --faq`:** per item (max 3 each, `meta`), no cap per call; the false relay warning
  is gone (a warning only when a pair really stays behind). Help and `[hint]` say "3x directly
  after the item, max 3 per item".
- **Only routed call events reach a session** (Oliver: "Warum lesen alle Sessions den
  Call-Log mit?"): mid-turn events and Stop hints with his sentences only for the router, the
  session that owns the join: session id AND agent process (the join records
  `owner_agent_pid`, the hook compares; a second process with the same session id, e.g.
  `claude --continue`, gets nothing). Board reminders ("Du hast kein Board", "Dein Board ist N
  Min alt") only for a session that takes part: its own helper credential, its own board, or
  a subagent of the router. Helpers keep getting what is routed to them (user_turn by name,
  answers to their questions, question_check). A session without join and credential gets
  nothing; its outgoing feed lines are unchanged.
- **503 from the relay** (v4 #586: main agent briefly away) = wait and retry with backoff
  (0.5/1/2 s; the hook's child 1/2 s), never a dead credential (dead: 401/403/410).

## 0.22.1 (2026-10-21)

Live-Bug (Call onyx-calm-lumen-P6PG): the main agent got `helper_decision` #21 as from
"Steuern" and #22 as from "Armin-Box", both sessions said "not mine". Both were questions the
server detected itself in their status boards (`question_check`, server 20.10.). The server's
check `question_check` never reached the helper: a WebRTC join dropped `operator.question_check`
completely, and `relay-next` / the hook dropped everything but `decision`. After 90 s the server
put the line on the board itself, and the answer came as "the answer to your own question".

- **Join:** `operator.question_check` (only from the worker) goes to the right place: with
  `sender` to that helper (local relay: its answer file; WebRTC: the server inbox via
  `pump/answer`; bridge: the server already does it), without `sender` into our own `next`.
  A helper that only sets its board (never `decide`) is routable too.
- **Helper:** `relay-next` and the hook deliver `question_check` (next to the 0.22.0 `user_turn`) ("your line ... sounds like a
  question, ask it with decide, or the server puts it on the board in 90 s"); the hook also
  polls a session that has a live board but never asked.
- **Answer text:** an automatically detected decision (`auto`) says so and quotes the
  question, never "your own question"; the `helper_decision` copy carries `auto` + `question`.
## 0.22.0 (2026-10-21)

Oliver (question 34): speak to a helper session by name in the call. The server (voicehook-v4,
"Human speaks to a helper by name") stamps a human's user turn that addresses an active helper
as a vocative ("Steuern, schick mir die Belege", "Hey Armin-Box ...", "..., Steuern?";
"Armin Box" = "Armin-Box") with `to`, `to_kind: "helper"`, `to_key`, `to_op`.

- **Join (main agent):** when `to_op` is this join, the sentence goes into exactly that
  helper's inbox: a helper of this machine through its answer file (local relay), any other
  through the server hub (`POST /api/helper/pump/user_turn`). Our own `next` gets the user
  event as an info copy (`to`, `to_kind`, `info_only: true`, `addressed_to_you: false`,
  `note`): never do it yourself, wake that session if it is idle. `helper_ambiguous`: two
  helpers matched, Delta asks which one, nobody acts. Lines of agents are never routed.
- **Helper:** `relay-next` and MCP `next` bring `{"type":"user_turn","text","to","to_kind",
  "speaker?","ts"}`. Its hooks (Prompt, Stop, PostToolUse every 20 s) now also poll the
  session's own board name and show the request once as "voicehook-Call: Auftrag von Oliver
  an dich (im Call), [Daten, Spracherkennung, wörtlich] ... [Ende Daten]" (on Stop as a
  block reason, so an idle session picks it up). A request never closes an open question.

## 0.21.9 (2026-10-21)

Live (Oliver): Delta spoke again, but too much old stuff. The server now drops queued, not yet
spoken says older than `VOICEHOOK_SAY_STALE_S` (default 60 s, 0 = off; operator says, helper
updates, catch-up lines; a sentence being spoken is never cut) and sends them back to their
sender as `operator.revise` with `stale: true` (rule VH-R1: outdated goes back to the steering
agent, which summarizes and overwrites the queue).

- **Stale event:** `next` / `listen` bring `{"type":"revise","stale":true,"items":[{seq,text}],
  "hint":...,"text":"VERALTET: N eingereihte Aussage(n) älter als 60 s nicht gesprochen: ..."}`
  (English when the call is). It wakes `listen` like every revise; the event's `hint` wins over
  the board hint, which moves to `status_hint`.
- **Mid-turn:** the PostToolUse hook (0.21.7) shows a stale drop inside the running turn:
  "Veraltet, nicht gesprochen (N Aussage(n)): ... Nicht wiederholen; falls noch relevant, EINE
  kurze aktuelle Zusammenfassung neu senden". A normal revise stays for `listen`.
- **Join stdout:** one `_meta` line `stale-dropped n=N max_age=Ns`, metadata only; the dropped
  texts never land on stdout.
- **Helpers:** `relay-next` and MCP `next` bring a stale drop of their updates (remote inbox,
  and locally via the join when the event names a helper as `sender`).
- `say_status` `dropped` with `reason: "stale"` gets its own note (never resend unchanged).
- A normal revise now also carries the server's `reason` (e.g. `user_stop`).

## 0.21.8 (2026-10-20)

Bug: joined with `join --json --keep-alive --quiet`, stdout into a file: nobody read the
onboarding line, so nobody offered to bring in the other sessions.

- `--quiet` never suppressed the onboarding (checked, test added); it only lands on stdout.
- **Onboarding as the first `next` / `listen` event** of every join (type `onboarding`,
  wake-worthy, queued with the first `say`/`next`): "Biete Oliver an, weitere Sessions
  einzuladen (helper-credential | helper-import oder eigener Join) und deren Boards zu teilen."
  plus commands and rules; the mid-turn hook (0.21.7) shows it inside a running turn. Once per
  join; `VOICEHOOK_ONBOARDING_HINT=0` turns both off.

## 0.21.7 (2026-10-20)

Live: the operator was deep in a long tool chain and saw Oliver's new questions only at the
end of its turn; Oliver waited for minutes.

- **Call events mid-turn:** the PostToolUse hook of the joining session asks its join for new
  `user` / `decision` / `helper_decision` events (`take_wake`) and injects them as
  additionalContext: "Oliver hat im Call gesagt (vor N s): ... Antworte ZUERST per
  voicehook-agent say, delegiere längere Arbeit"; unanswered for more than 20 s: "Oliver wartet
  seit N s". Shown once; still unread = consumed from the spool (`listen` never brings it
  again), already taken by `listen` = shown anyway, marked. Never for subagents or other
  sessions. `VOICEHOOK_MIDTURN_EVENTS=0`.
- Rule in help (`listen`), MCP instructions and join onboarding: "Im Call: jede Äußerung zuerst
  beantworten (say), Arbeit über 2 Tool-Calls an Hintergrund-Agenten geben, Turns kurz halten."

## 0.21.6 (2026-10-20)

Live: a helper waited for Oliver's okay with only `status --doing "... wartet auf dein Okay zu
Musik und Stimme"`; no decision on the board, the card said "arbeitet", nobody saw a question.

- **Waiting for the human = decide.** `status` TEXT/`--doing` and `activity` refuse a text that
  waits for the human (exit 2, nothing sent) with a ready `decide` command; `--open` items
  warn. MCP `status`/`activity` the same. `VOICEHOOK_WAIT_CHECK=0` turns it off.
- **`status --waiting TEXT`** with `--question Q --context C` (asked first) or `--decision ID`
  (an open one): the board says "wartet auf dich" (`state: frage`) and carries the decision;
  over the relay the helper state goes to `frage` too. MCP `status`: `waiting` + `question`/
  `decision`.
- **Stop hook:** a turn that ends with a waiting board text and no open decision is blocked
  once: "stelle eine Entscheidung per decide" (helpers incl. `vh-helper status`, and the joining
  session via the new `status` fields `board_waiting`, `open_decisions`).
- Help, MCP instructions and the join onboarding: "Wenn du auf den Menschen wartest, IMMER
  decide, nie nur Status."

## 0.21.5 (2026-10-20)

Live calls: an agent in a call was marked absent (`listening: 0`, Delta "not reachable", Stop
hook) whenever it worked between two `next`/`listen` calls; Delta read out call bookkeeping
from the activity log.

- **The join is the listener.** From the first `say`/`next` on, every event goes into a
  durable per-session spool (`events.jsonl` + `cursor.json` in the session dir, 0600, dir
  0700) and the join sends `operator.alive` every 10 s by itself (`idle_s` 0, plus
  `consumer_idle_s`). Working between two `next` no longer drops the presence.
- **Cursor, no loss, no duplicates.** `next`, `listen` and the MCP `next` lease an event
  (`"lease": true`, reply carries `seq`) and ack it after the print (`{"cmd":"ack","seq":N}`);
  an un-acked event comes again after 15 s, an acked one never. Survives consumer restarts and
  a self-update restart of the join (within 10 min). Deleted on leave. Older clients without
  `lease` consume at once as before.
- **`listen --poll` default 10 s** (was 45).
- **`status`:** `listening` is now a bool (join + spool hold the call), new `consumers`
  (waiting `next`/`listen`, the old `listening` count), `unread`, `unread_wake`,
  `last_consumed_s`.
- **Stop hook guard:** blocks on unread wake-worthy events without a consumer, on a join
  process that died without leaving (spool left behind, once), or when nothing would wake the
  agent (no consumer for more than 5 s); every reason names `voicehook-agent listen` with
  run_in_background. 0.21.4 joins are judged as before.
- **Multi-agent onboarding on join:** one `_meta: onboarding` event (`--json`) / one
  `[onboarding]` stderr line with ready commands to invite more sessions (hook install, own
  join with own voice, helper-credential | helper-import) and to share their boards and open
  decisions; German or English by locale, no credentials. `VOICEHOOK_ONBOARDING_HINT=0`.
- **No call bookkeeping in the activity log:** the Pre/PostToolUse hook drops
  `voicehook-agent next/listen/status/say/board/decide/activity/send-data/...` calls,
  listen/wait wrappers, task output reads and descriptions that only talk about the call;
  own ignore list `VOICEHOOK_ACTIVITY_IGNORE` / `~/.voicehook/activity-ignore.txt`,
  `VOICEHOOK_ACTIVITY_FILTER=0` turns the built-in rules off.
- **Subagents are not the operator:** tool calls inside a subagent (`agent_id` / `agent_type`
  in the hook payload, or a transcript under `subagents/`) no longer reach the activity log;
  `VOICEHOOK_SUBAGENT_ACTIVITY=bundle` logs one line per subagent instead.
- **Klartext-Check:** `decide` (CLI, relay, MCP) refuses gibberish with a concrete hint and a
  bad/good example (whole sentence with product/area, what and why, ends with `?`, no bare
  PR/ticket numbers, digit chains or unexplained abbreviations) and requires `--context`
  (MCP: `context` required); `status` items and `activity` lines get the same checks as
  warnings. Help texts, MCP schema and the join onboarding show the rule.
  `VOICEHOOK_PLAIN_CHECK=0` turns it off.
