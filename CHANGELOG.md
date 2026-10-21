# Changelog

voicehook-agent CLI. Older versions: the GitHub releases (`cli-v*`) and the version notes in
[docs/CLI.md](docs/CLI.md).

## 0.21.10 (2026-10-21)

Live (Oliver, call 21.10. morning): the router put screenshots into the call with
`show --card image --file <png>`, the CLI said ok, Oliver saw nothing.

- **Real screenshots work:** the join's control socket read one request line with asyncio's
  default limit of 64 KB. A screenshot (base64 in that line) overran it, the join dropped the
  connection and `show` printed `no-session` ("session ended?"). Now up to 4 MB, and a too
  large request gets an answer instead of a dropped connection.
- **Never ok without the image in the browser:** every call page loads an image card and
  answers on `vh.card_ack` `{id, ok}`. `show --card image` answers `ok:true` with `shown` only
  after at least one page loaded it; otherwise `ok:false` with `type` `not_shown` (did not load,
  e.g. the server restarted, or no page answered) or `no_human`, exit 1.
- **Cards from helper sessions:** `show --card ...` of a session without its own join goes over
  the helper relay (`POST /api/activity/relay/show`); the card names the helper as sender. Same
  checks and limits, image only as file data, no `--panel`/`--large`. A WebRTC main agent gets
  such cards over its helper pump (kind `card`), publishes them as itself and confirms them like
  its own.

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
