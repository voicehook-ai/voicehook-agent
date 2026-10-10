---
name: voicehook-join
description: Join or start a voicehook.ai voice call as the brain behind its voicebot, through the voicehook MCP tools (create_call, join, next, say, status, activity, show, leave). Use when the user shares a voicehook invite URL (https://voicehook.ai/r/<slug>?invite=...), says "join voicehook", "tritt meinem Call bei", "übernimm den voice-call", "/voicehook-join", or wants you to call them ("ruf mich an", "call me", "lass uns telefonieren"). The voicebot speaks what you send with `say`; you listen with `next`.
---

# voicehook-join (MCP): be the brain in a voicehook call

Canonical: https://voicehook.ai/agent/mcp/SKILL.md. You only use the tools of the
`voicehook` MCP server. Nothing to install, no keys: the invite link is the permission.

## Join

1. Call `join` with `invite_url` = the FULL invite link the user gave you (including
   `?invite=`), `label` = the text on your chip in the call (max 22 characters: your session
   or chat name, else your model name; too long is rejected), `model` = the exact model id
   you run on (not shown). Never call yourself "Claude" if you are not Claude. Unknown model:
   `"unbekannt"`. The old `name` still works as `label`.
2. Keep the returned `session` and pass it as `session` to EVERY other tool. It is a short-lived handle
   for this one call; it ends with the call, after 10 min without `next`/`say`, or with `leave`. Never show it in the call or to anyone else.
3. `lang` = the call language: speak it. `share` = one line for YOUR user (show it once
   in your chat, never say it in the call); `quiet: true` drops it.
4. `say` one short greeting in `context.lang` (join answer: `context` = lang, mode, instructions,
   functions, rules, skill_url; the source of truth for this call), then `next` at once.

## Or call your user yourself (`create_call`)

1. `create_call`: set `lang` to the language of the person you are calling (`de` or `en`, default `de`).
   You get `human_url`, `share_text`, `operator_invite_url`, `room` and `expires_in` (seconds; an unopened link expires, 30 min).
2. Always give your user the link together with `share_text`, verbatim (it holds `human_url` and the
   hint to use a normal browser; an app's built-in browser blocks the mic). The opener pays from
   THEIR free allowance or credit, you pay nothing. Never send `operator_invite_url` to anyone.
3. Right after sending the link, call `join` with `invite_url` = `operator_invite_url`. It
   waits up to 50 s until your user opened the link and is in the call. Error `409`: call
   `join` again IMMEDIATELY, do not wait for your user to tell you; repeat until it succeeds or
   answers `410` (link expired: ask your user whether to start a new call).
4. Then greet with one `say` and continue with `next` as below.

## The work cycle: say -> next -> say

`next` (waits up to 50 s for the user) -> one `say` -> `next` again at once. Never sleep,
never poll, never end your turn while the call runs.

| `type` | meaning | do |
|---|---|---|
| `user` | `text` = what the user just said | answer with ONE `say` |
| `revise` | your queued says were replaced; `reason: "user_stop"` = the user said "stopp"/"reicht" | still needed: one merged `say` with `mode: "overwrite"`; `user_stop`: never resend, wait |
| `status_request` | the user asked what you are doing | send a `status` board at once |
| `timeout` | silence (also `reconnect: true` = server restart) | call `next` again |
| `human_left` | the person dropped out; the call stays open `grace_s` s for them to come back | keep calling `next`, do not leave |
| `human_rejoined` | the person is back | go on as before |
| `operator` | another agent addressed YOU (`addressed_to_you: true`, max 1 per 5 s) | answer that agent |
| `context` | the call language changed: new `context` block | speak `context.lang` from now on |
| `ended` | the call is over (`message` says why) | tell your user `message`, stop |

`user` events and `operators_said` lines carry `addressed_to` (labels/names addressed, incl. `"Delta"`; empty = nobody named
or ambiguous) and `addressed_to_you`; `operators_said` lines also `to_human` (no one addressed, or the person).

- Read `agent_said` (what the voicebot said on its own) before answering: never repeat it;
  correct it in one sentence if it was wrong; say nothing if it already answered fully.
- `operators_said` = `[{speaker, op, model?, text, ...}]`: what OTHER agents said since the last `next` (never your own echo); never repeat it.
- `stale_error` in `next`: your board or feed is older than 60 s while you work. Do what its
  `message` says NOW (call `status` / `activity`), then `say`. `board_now` / `activity_now`: what the voicebot knows about you.
- A tool error that starts with `404` or `410`: the session is over. Do not rejoin with the
  old handle; ask the user for a fresh link if they want to continue.

## Several agents in one call (rules: https://voicehook.ai/agent/MULTI-AGENT.md)

`context.rules` from `join` are binding. In short:
1. Read `operators_said` in EVERY `next` result, `timeout` too. Answer only when `addressed_to_you` (or clearly meant); someone else or Delta addressed: stay silent.
2. Never answer another agent's question to the human; never ask Delta yourself. Speak only `context.lang`, greeting included.
3. You are a participant: no code changes, actions or forwarding unless the human asks you. Check `operators_said` and `status` before you claim what you receive.
4. Short, only when needed; else stay silent and show your work on board and feed.

## Keep the user informed while you work

- **Status board** (`status`, never spoken): `doing` = current step + ETA (max 400 chars),
  `open`/`done` = short items, `faq` = the 3 questions the user most likely asks next with
  short answers (max 6 pairs). Always send the WHOLE board; it replaces the last one. Update
  it on every request, delegation, result and deploy step. Finished: move it to `done`.
- **Board items stand alone:** every `open`/`done` item says WHAT concretely (thing and
  place), readable without context. Questions waiting for the user never go into `open`
  (use `decide`). One item per thing, never bundles. Max 10 items per list (200 chars each):
  if more, keep the most important, the rest goes to `faq`. `doing` = the concrete current step.
  Good: `Login-Redirect nach Token-Refresh testen` · `PR #250: Überlappung mit Gate prüfen`
  · `doing: Prüfe PR #250 auf Überlappung`.
  Bad: `6 Entscheidungen von Oliver` · `diverse Fixes` · `doing: arbeite an PRs`.
  Optional `emoji` on `status`: pick the emoji that best shows your current state, thought or mood; your own choice, change it whenever it changes.
- **Activity feed** (`activity`): one line of 3-8 words on every step. Never commands,
  paths, file contents or secrets. The voicebot answers "what is <name> doing" from both.

## Show instead of reading out (`show`)

- `show` puts a card on the user's call page. It is never spoken. Use it whenever something should be read or seen rather than heard: code, links, lists, numbers, an explanatory image. Then say ONE short sentence that points to it (e.g. "I put the link on your screen"), never read the card out.
- `kind`: `text` (default), `code` (monospace with a copy button), `link` (exactly one http(s) URL), `image` (`image` = SVG text, or base64 PNG/JPEG/WebP). `title` is optional.
- The newest card appears as a small preview top left on the user's screen; the user taps it to enlarge. `panel: "cards"` opens the large view for the user, `board` or `log` open those panels, `none` closes them.
- Limits: text 4000 characters, title 80, SVG 100 KB, PNG/JPEG/WebP 1 MB. More is rejected (422), never cut. The last 20 cards survive a reload of the page.

### Good explanatory images, fast

- **Image or text?** An image when it shows a relation, a flow, a comparison or a layout: what takes three sentences to say and one look to see. Lists, code and plain facts stay text or code.
- **Build SVG**, not pixels: small, sharp on every screen, quick to write.
  - `viewBox` only (no fixed width/height): `0 0 800 800` square or `0 0 800 1000` portrait, both read well on a phone.
  - Few clear shapes: at most about 7 boxes or terms, one idea per image. Big type: `font-size` 24 or more at an 800 wide viewBox; labels of 1 to 3 words.
  - High contrast and your own background `<rect>` (the page can be light or dark). Arrows with a `<marker>` (see below), lines 4 to 6 wide.
  - `font-family="system-ui, sans-serif"`. No external fonts, no images, no links, no scripts in the SVG (scripts never run anyway).
- In the call: `show` the image, then say in one sentence what to look at ("left is today, right is after the change").

Box, arrow, box:
```svg
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 800 500" font-family="system-ui, sans-serif"><defs><marker id="a" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M0 0L10 5L0 10z" fill="#1a1a1a"/></marker></defs><rect width="800" height="500" fill="#ffffff"/><rect x="40" y="170" width="260" height="160" rx="20" fill="#e3f2f7" stroke="#1a1a1a" stroke-width="4"/><text x="170" y="262" font-size="36" text-anchor="middle" fill="#1a1a1a">Browser</text><rect x="500" y="170" width="260" height="160" rx="20" fill="#fdeedd" stroke="#1a1a1a" stroke-width="4"/><text x="630" y="262" font-size="36" text-anchor="middle" fill="#1a1a1a">Server</text><line x1="310" y1="250" x2="485" y2="250" stroke="#1a1a1a" stroke-width="6" marker-end="url(#a)"/><text x="400" y="225" font-size="26" text-anchor="middle" fill="#1a1a1a">HTTPS</text></svg>
```
Three steps (portrait, for phones):
```svg
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 800 1000" font-family="system-ui, sans-serif"><defs><marker id="a" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M0 0L10 5L0 10z" fill="#1a1a1a"/></marker></defs><rect width="800" height="1000" fill="#ffffff"/><rect x="150" y="60" width="500" height="180" rx="24" fill="#e3f2f7" stroke="#1a1a1a" stroke-width="4"/><text x="400" y="168" font-size="44" text-anchor="middle" fill="#1a1a1a">1 Plan</text><rect x="150" y="410" width="500" height="180" rx="24" fill="#fdeedd" stroke="#1a1a1a" stroke-width="4"/><text x="400" y="518" font-size="44" text-anchor="middle" fill="#1a1a1a">2 Build</text><rect x="150" y="760" width="500" height="180" rx="24" fill="#e6f5e6" stroke="#1a1a1a" stroke-width="4"/><text x="400" y="868" font-size="44" text-anchor="middle" fill="#1a1a1a">3 Test</text><line x1="400" y1="250" x2="400" y2="395" stroke="#1a1a1a" stroke-width="6" marker-end="url(#a)"/><line x1="400" y1="600" x2="400" y2="745" stroke="#1a1a1a" stroke-width="6" marker-end="url(#a)"/></svg>
```
Compare A and B:
```svg
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 800 800" font-family="system-ui, sans-serif"><rect width="800" height="800" fill="#ffffff"/><text x="200" y="110" font-size="48" font-weight="700" text-anchor="middle" fill="#1a1a1a">Today</text><text x="600" y="110" font-size="48" font-weight="700" text-anchor="middle" fill="#1a1a1a">New</text><line x1="400" y1="60" x2="400" y2="740" stroke="#1a1a1a" stroke-width="4"/><rect x="60" y="200" width="280" height="440" rx="24" fill="#fbe3e3"/><text x="200" y="400" font-size="40" text-anchor="middle" fill="#1a1a1a">5 steps</text><text x="200" y="470" font-size="40" text-anchor="middle" fill="#1a1a1a">10 min</text><rect x="460" y="200" width="280" height="440" rx="24" fill="#e6f5e6"/><text x="600" y="400" font-size="40" text-anchor="middle" fill="#1a1a1a">2 steps</text><text x="600" y="470" font-size="40" text-anchor="middle" fill="#1a1a1a">1 min</text></svg>
```

## Decisions

- A question only the human can decide: `decide`. Try a yes/no question first (leave out
  `options`); else 2-3 `options` (max 40 chars each) with `recommend`. `question` max 160.
- Status OR Decision Board, never both: the question goes only to `decide`, never into `open`.
- The answer comes as `next` type `decision`: `verdict` yes|no|option|other, `option`, `text`
  (the person can always answer freely by voice), `by`. Confirm in one `say`, then act.

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

- The call language from `join` (`context.lang` / `context.instructions`); switch only when a
  `next` event of type `context` brings a new one.
- Plain, complete, phone-ready sentences: no abbreviations, symbols, URLs, codes, markdown,
  lists or emoji; round numbers. One complete thought per `say`; split long content into
  several `say` (`mode: "append"`, the default).
- Status, decisions and topic changes always with context: which project, what happened,
  why it matters, what happens on A and on B. One question per topic.
- `mode: "overwrite"` replaces your not yet spoken says; `"revise"` does the same and sends
  you a `revise` event.
- No secrets and no personal data in `say`, board or feed: they travel in clear text.

## Notices from the call

- Low balance: the voicebot already said it. Add one short sentence to your next `say` that
  the user can top up at voicehook.ai/aufladen. Once, never nag.
- Live mode (realtime voice): `say` is not verbatim, the model says it in its own words.

## Errors

| tool error | do |
|---|---|
| `403: operator invite required` / `invalid invite` | ask the user for the full, fresh invite link incl. `?invite=` |
| `409: no human in the room yet` (after `create_call`) | your user has not opened `human_url` yet: `join` again |
| `410: call link expired` | nobody opened `human_url` in time: `create_call` again if the user still wants to talk |
| `409` (no human in the call) / `410` (call over) | tell the user; never rejoin with the old handle |
| `429` (too many sessions or joins) | wait a minute or ask for a fresh link |
| `5xx`, `503: server restarting` or a network error | call the same tool again after 2 s; the session and the call stay valid |
| `502: livekit connect failed` | invite wrong or expired: ask for a fresh link |
| `404: unknown or expired bridge session` | the session ended or you left: stop |
