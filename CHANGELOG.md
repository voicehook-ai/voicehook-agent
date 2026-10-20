# Changelog

voicehook-agent CLI. Older versions: the GitHub releases (`cli-v*`) and the version notes in
[docs/CLI.md](docs/CLI.md).

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
