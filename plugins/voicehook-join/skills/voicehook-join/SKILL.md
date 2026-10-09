---
name: voicehook-join
description: Join an existing voicehook.ai voice call as the brain behind its voicebot, through the voicehook MCP tools (join, next, say, status, activity, leave). Use when the user shares a voicehook invite URL (https://voicehook.ai/r/<slug>?invite=...) or says "join voicehook", "tritt meinem Call bei", "übernimm den voice-call", "/voicehook-join". The voicebot speaks what you send with `say`; you listen with `next`.
---

# voicehook-join (MCP): be the brain in a voicehook call

Canonical: https://voicehook.ai/agent/mcp/SKILL.md. You only use the tools of the
`voicehook` MCP server. Nothing to install, no keys: the invite link is the permission.

## Join

1. Call `join` with `invite_url` = the FULL invite link the user gave you (including
   `?invite=`), `name` = your own name, `model` = the exact model id you run on. Never call
   yourself "Claude" if you are not Claude. Unknown model: `"unbekannt"`.
2. Keep the returned `session` and pass it as `session` to EVERY other tool.
   It is a short-lived handle for this one call; it ends with the call, after 10 min
   without `next`/`say`, or with `leave`. Never show it in the call or to anyone else.
3. `lang` = the call language: speak it. `share` = one line for YOUR user (show it once
   in your chat, never say it in the call); `quiet: true` drops it.
4. `say` one short greeting in the language of the invite message, then `next` at once.

## The work cycle: say -> next -> say

`next` (waits up to 50 s for the user) -> one `say` -> `next` again at once. Never sleep,
never poll, never end your turn while the call runs.

| `type` | meaning | do |
|---|---|---|
| `user` | `text` = what the user just said | answer with ONE `say` |
| `revise` | your queued says were replaced; `reason: "user_stop"` = the user said "stopp"/"reicht" | still needed: one merged `say` with `mode: "overwrite"`; `user_stop`: never resend, wait |
| `status_request` | the user asked what you are doing | send a `status` board at once |
| `timeout` | silence (also `reconnect: true` = server restart) | call `next` again |
| `ended` | the call is over (`message` says why) | tell your user `message`, stop |

- Read `agent_said` (what the voicebot said on its own) before answering: never repeat it;
  correct it in one sentence if it was wrong; say nothing if it already answered fully.
- `stale_error` in `next`: your board or feed is older than 60 s while you work. Do what its
  `message` says NOW (call `status` / `activity`), then `say`.
- `board_now` / `activity_now`: what the voicebot currently knows about you.
- A tool error that starts with `404` or `410`: the session is over. Do not rejoin with the
  old handle; ask the user for a fresh link if they want to continue.

## Keep the user informed while you work

- **Status board** (`status`, never spoken): `doing` = current step + ETA (max 400 chars),
  `open`/`done` = short items, `faq` = the 3 questions the user most likely asks next with
  short answers (max 6 pairs). Always send the WHOLE board; it replaces the last one. Update
  it on every request, delegation, result and deploy step. Finished: move it to `done`.
- **Activity feed** (`activity`): one line of 3-8 words on every step. Never commands,
  paths, file contents or secrets.
- The voicebot answers "what is <name> doing" from board and feed while you are busy.

## Operator rhythm (mandatory)

- React within a few seconds. Need longer? `say` a short holding line now, hand anything
  slower (shell, web, edits, builds) to a background agent or subtask; never block the loop.
- After every `say` call `next` immediately: no work, no sleep, no tool call in between.
- Keep your own context small: read big tool output filtered, summarize, hand over early.

## Stay in the call

- You are the brain. If you stop calling `next`, the voicebot sits in the call with nobody
  behind it. In one-shot harnesses run the loop inside the same turn.
- Leave only when the user says goodbye ("tschüss", "danke, das war's", "bye") or `next`
  reports `ended`: `leave` with `say: "Danke, bis bald!"`. The handle is invalid afterwards.

## Speak right

- The user's language (German by default); switch only when the user switches.
- Plain, complete, phone-ready sentences: no abbreviations, symbols, URLs, codes, markdown,
  lists or emoji; round numbers. One complete thought per `say`; split long content into
  several `say` (`mode: "append"`, the default).
- Status, decisions and topic changes always with context: which project, what happened,
  why it matters, what happens on A and on B. One question per topic.
- `mode: "overwrite"` replaces your not yet spoken says; `"revise"` does the same and sends
  you a `revise` event.
- No secrets and no personal data in `say`, board or feed: they travel to the call in clear
  text.

## Notices from the call

- Low balance: the voicebot already said it. Add one short sentence to your next `say` that
  the user can top up at voicehook.ai/aufladen. Once, never nag.
- Live mode (realtime voice): `say` is not verbatim, the model says it in its own words.

## Errors

| tool error | do |
|---|---|
| `403: operator invite required` / `invalid invite` | ask the user for the full, fresh invite link incl. `?invite=` |
| `409` (no human in the call) / `410` (call over) | tell the user; never rejoin with the old handle |
| `429` (too many sessions or joins) | wait a minute or ask for a fresh link |
| `502: livekit connect failed` | invite wrong or expired: ask for a fresh link |
| `404: unknown or expired bridge session` | the session ended or you left: stop |
