---
name: voicehook-join
description: Join or start a voicehook.ai voice call as the brain behind its voicebot, through the voicehook MCP tools (create_call, join, next, say, status, activity, show, leave). Use when the user shares a voicehook invite URL (https://voicehook.ai/r/<slug>?invite=...), says "join voicehook", "tritt meinem Call bei", "übernimm den voice-call", "/voicehook-join", or wants you to call them ("ruf mich an", "call me", "lass uns telefonieren"). The voicebot speaks what you send with `say`; you listen with `next`.
---

# voicehook-join (MCP): be the brain in a voicehook call

Canonical: https://voicehook.ai/agent/mcp/SKILL.md. You only use the tools of the `voicehook` MCP server. Nothing to install, no keys: the invite link is the permission.

## Which path: MCP, CLI or bridge? (Oliver 19.10.2026, decision "A")

MCP/bridge = one agent in the call; activity lines are only what that agent writes by hand. For several agents/sessions on one machine working on one call, use the voicehook-agent CLI (pro): its multi-session relay + Claude Code hook feed the live activity log, status and decisions of ALL local sessions into the call automatically. MCP/bridge run on the server and cannot see the user's local sessions.

| path | role | use it when |
|---|---|---|
| **MCP** (`voicehook.ai/mcp`) | **Entry + distribution.** One click as connector or plugin, nothing to install, only path with `create_call`, cards, `decide`, `data`. | Default for a single agent in a call; every cloud/sandbox session; newcomers; whenever you must start the call yourself. |
| **CLI** (`voicehook-agent`) | **Pro tool for multi-agent work.** One machine, many sessions, one call: the multi-session relay (0.15+) and the Claude Code hook feed the activity log from every session automatically; helpers report `decide`/`status` through the relay (0.16+, in review). Base of the "Leitstelle". Also direct WebRTC, persona/mode flags. | Local machine with install rights AND several agents/sessions on the same call, or the human wants the live activity log of all sessions. |
| **Bridge** (`/api/bridge/*`, Quickstart A) | **Protocol underneath + fallback.** Plain HTTPS/REST for any agent. | No MCP client and no CLI; or a feature the other path lacks right now. |

- **Why the CLI stays:** MCP runs on the server and cannot see the sessions on the user's machine. Only a local process knows which Claude Code sessions run, what each does and when one waits for a decision, and feeds that into the call without hand-written notes.
- **Cards from the CLI (0.21.0+):** `cli show --card text|code|link|image` puts the same cards on the call page as bridge/MCP `show` (REFERENCE.md "Cards on the call page (CLI)"); older CLIs only draw ring shapes.
- CLI install (also without git) and version check: https://voicehook.ai/agent/REFERENCE.md. Cloud/sandbox sessions: MCP, never the CLI (next point).
- `CLAUDE_CODE_REMOTE=true`, `HTTPS_PROXY` set, or a local join that failed with `wait_pc_connection timed out` = cloud/sandbox session: WebRTC is blocked there. Never install or run a local voice client; these MCP tools need only HTTPS, the server joins the call for you.
- No MCP tools in your client: the same tools work as plain HTTPS requests to https://voicehook.ai/mcp (JSON-RPC `tools/call`), recipe in https://voicehook.ai/agent/REFERENCE.md.

## Hard rules (read first, always)

- **HARD RULE after `create_call`: join again until connected, never end your turn before, then keep `next` going.** (In Claude Code the background watcher below does the joining and wakes you; only there you end the turn with the link.) (Oliver 21.10.2026: agents ended their turn after one `waiting_for_human` and fell asleep; the call had no brain.) **Never delegate `create_call` to a subagent, a fork or a background job.** Create the call in the main thread of the session your user is talking to; a link produced in a subthread never reaches your user.
  - **Client without background commands (claude.ai chat, Claude Desktop, remote apps, Cursor, ChatGPT):** in the SAME answer, FIRST write `share_text`, verbatim, as text to your user (text you write before a tool call is shown before it runs), THEN call `join` with `operator_invite_url` and repeat it at once after every `waiting_for_human` (it waits up to 200 s in Claude clients, else 50 s, and its `next_action` says the same) until it returns `session`. Never end your turn while the answer is `waiting_for_human`. Then the Hard rules checklist and the `next` loop. Only `410` (link expired after 30 min) ends the joining: tell your user and ask whether to start a new call.
  - **Client with background commands that wake you when they exit (Claude Code: Bash with `run_in_background: true`):** FIRST start the background watcher from https://voicehook.ai/agent/REFERENCE.md ("Background watcher after create_call"), THEN end your turn with share_text, verbatim, as its very last line. Never block in a foreground `join` after the link there: in Claude Code the watcher does the joining, and its exit wakes you. The watcher joins as soon as your user is in the call (up to the 30 min link expiry) and exits: continue with its session (file `$D/s`) as `session` in every tool, or call `join` with `operator_invite_url` and `greet: false` (returns at once, replaces the watcher's session).
  - **`local_cli_hint` in `create_call` or `join`** (only a local client with a shell gets it: Claude Code, Cursor, VS Code, Codex CLI, Gemini CLI and similar): the CLI from "Which path" keeps you in the call from a background process, so you never fall asleep between turns. Offer it to your user; install only with their OK (command in the hint). Cloud and sandbox sessions stay on MCP.
  - Until they have joined, end EVERY message to your user with share_text as the last line. If your client can send a file or a push (e.g. Claude Code SendUserFile / PushNotification), also send the link that way. On 5xx, 503 or a network error call the tool again after 2 s.
- **Right after `join`, at once** (also with `quiet: true`: quiet only suppresses `notice`, nothing audible). You lead: never wait silently until the human says something.
  1. Greet: `greet` (true by default in `join`) speaks the default greeting in the call language. With greet, skip the greeting `say`. Without greet: right after join, `say` one short greeting in the call language: who you are and that you are listening ("Hi, this is <your label>, I'm in and listening. What's up?" / "Hallo, <your label> hier, ich bin drin und höre zu. Worum geht's?"). If you stay silent, the voicebot announces you after 3 seconds.
  2. `status` with `doing`, `open`, `done`.
  3. One `activity` line.
  4. Every question for the human: `decide` (Decision Board) plus one short `say`; never only `say`, never into `open`.
  5. `show` with `panel: "board"` (or `log`, `decisions`) so the human sees it at once.
- **Acknowledge within 3 s:** on every request of the human first one short `say` ("On it, one moment." / "Mach ich, Moment."), THEN work. Progress goes to `status` and `activity`, never silence.

## Join

1. Your user pasted an invite link: call `join` with it right away, no questions first. `invite_url` = the FULL invite link the user gave you (including `?invite=`), `label` = the text on your chip in the call (max 22 characters: your session or chat name, else your model name; too long is rejected), `model` = the exact model id you run on (not shown). Never call yourself "Claude" if you are not Claude. Unknown model: `"unbekannt"`. The old `name` still works as `label`. `greet` is true by default (`false` = you greet with `say`).
2. Keep the returned `session` and pass it as `session` to EVERY other tool. It is a short-lived handle for this one call; it ends with the call, after 10 min without `next`/`say`, or with `leave`. Never show it in the call or to anyone else.
3. `lang` = the call language: speak it. `context` = lang, mode, functions, skill_url: the facts of this call. The rules for how to behave are in this skill, not in the tool answers.
4. Then the Hard rules checklist (greeting, board, feed, decide, panel) and `next` at once.

## Or call your user yourself (`create_call`)

1. `create_call` (main agent only, never a subagent): set `lang` to the language of the person you are calling (`de` or `en`, default `de`). One-agent call is the default (`solo` left out or `true`); `solo: false` lets several agents join.
   You get `human_url`, `share_text`, `operator_invite_url`, `room` and `expires_in` (seconds; an unopened link expires, 30 min).
2. Give your user `share_text` verbatim (it holds `human_url` and the hint to use a normal
   browser; an app's built-in browser blocks the mic) as text FIRST, before any join (Hard rules). The opener
   pays from THEIR free allowance or credit, you pay nothing. Never send `operator_invite_url` to anyone.
3. `join` with `invite_url` = `operator_invite_url`, as the Hard rules say: without background
   commands right after the link in the same answer, repeated until it returns `session`; in Claude
   Code from the background watcher (started before the link, ends when the person is in). It waits
   up to 200 s in Claude clients (else 50 s) and returns status `waiting_for_human` (not an error,
   with `share_text` again and `next_action`) until the person opens the link: join again at once,
   never end your turn in between, until it succeeds or answers `410` (link expired after 30 min:
   ask your user whether to start a new call).
4. Then the Hard rules checklist and `next` as below.

## The work cycle: say -> next -> say

`next` (waits up to 50 s for the user) -> one `say` -> `next` again at once. Never sleep, never poll, never end your turn while the call runs. Working? Still call `next` at least every 10-15 s (split long work into steps, `next` in between). Exactly ONE `next` loop per agent: a second one makes the older answer `superseded`. After a pause `next` brings `missed` (all user turns you missed, oldest first) and `away_s`: answer them together in one `say`. Never end a turn deaf: the CLI enforces this with its listen guard (a Stop hook that blocks a turn end while the join is connected and no `next` waits); with MCP it is up to you to keep `next` going.

| `type` | meaning | do |
|---|---|---|
| `user` | `text` = what the user just said | answer with ONE `say` |
| `revise` | your queued says were replaced; `reason: "user_stop"` = the user said "stopp"/"reicht" | still needed: one merged `say` with `mode: "overwrite"`; `user_stop`: never resend, wait |
| `status_request` | the user asked what you are doing | send a `status` board at once |
| `timeout` | silence (also `reconnect: true` = server restart); `humans_present` (in `join` and every `next`) `0` or `human_away: true` after `join` = the person is out right now | call `next` again; never create a new room or call |
| `human_left` | the person dropped out; the call stays open `grace_s` s for them to come back | keep calling `next`, do not leave, never create a new room |
| `human_rejoined` | the person is back (also when you joined while they were out) | greet them briefly in one `say` (not with `greeted: true`: your join `greet` was just spoken), go on |
| `voicebot_left` / `voicebot_back` | the voicebot dropped out (e.g. server update; usually back within ~20 s, the person may hear silence) / is back after `seconds` | tell your user in chat at once (a `say` is not heard), keep calling `next`, never leave; not back within about a minute, or `ended`: offer your user a new call (`create_call` + background watcher), never on your own / `say` works again, repeat the last important point briefly |
| `operator` | another agent addressed YOU (`addressed_to_you: true`, max 1 per 5 s) | answer that agent |
| `context` | the call language changed: new `context` block | speak `context.lang` from now on |
| `data` | another agent sent you data with `send_data`: `from`, `kind` (`json`/`text`/`file`), `payload` (or `url` for a big file), `name`?, `mime`? | use it; never speak it |
| `ended` | the call is over (`message` says why) | tell your user `message`, stop |

`user` events and `operators_said` lines carry `addressed_to` (labels/names addressed, incl. `"Delta"`) and `addressed_to_you`;
`user` events also `addressed_by` (`name`, `mention`, `context` = follow-up to the last agent, `all` = nobody named); `operators_said` lines also `to_human`.

- Read `agent_said` (what the voicebot said on its own) before every `say`: never repeat it or talk over him; add only what is missing, correct it in one sentence if it was wrong; say nothing if it already answered fully. Filters or wrappers around `next` must never drop `agent_said`.
- `operators_said` = `[{speaker, op, model?, text, ...}]`: what OTHER agents said since the last `next` (never your own echo); never repeat it.
- `stale_error` in `next`: your board or feed is older than 60 s while you work. Do what its `message` says NOW (call `status` / `activity`), then `say`. `board_now` / `activity_now`: what the voicebot knows about you.
- A tool error that starts with `404` or `410`: the session is over. Do not rejoin with the old handle; ask the user for a fresh link if they want to continue.

## One agent per call (solo), or several (rules: https://voicehook.ai/agent/MULTI-AGENT.md)

Solo call (default, `create_call` without `solo` or with `solo: true`): Delta speaks in your voice (`join` `voice`) and in first person, as you, only from your `status`, `activity` and `decide`, each item once. More agents may `join` the same call (e.g. OpenCode next to Claude Code): each gets its own voice and Delta speaks as each of them in first person. Stream what you do there, step by step: Delta speaks it as you, in his own flow. **Your voice:** `say` only to answer a direct question, or when Delta is wrong or forgot something important (a `say` overrides him and breaks his flow); sessions that should not speak are helpers (data only, below). `send_data` reaches the other agents, never Delta. With several agents (solo or `create_call` with `solo: false`), these rules are binding:
1. Read `operators_said` in EVERY `next` result, `timeout` too. `addressed_to_you` false: stay silent and do nothing, no say, no action (except `routed_by_delta`: Delta handed you the request, do it). `addressed_by` `all`: answer briefly, only what no one said yet.
2. Never answer another agent's question to the human; never ask Delta yourself. Speak only `context.lang`, greeting included.
3. You are a participant: no code changes, actions or forwarding unless the human asks you. Check `operators_said` and `status` before you claim what you receive.
4. Short, only when needed; else stay silent and show your work on board and feed.
5. Data for another agent (JSON, text, a file as base64): `send_data` with `to` (label, identity or `"*"`), `kind`, `payload`, `name`?, `mime`?; never spoken, the human only sees one feed line. Max 15 KB per packet, so a file inline only up to about 11 KB (base64); a received `url` is a path on https://voicehook.ai, downloaded with your `session` (see the protocol link); bigger files: REST bridge upload (https://voicehook.ai/agent/OPERATOR-PROTOCOL.md).
Helper sessions (data only, no voice, no join, not an agent in the call): `helper_credential {session, label}` gives a `token` (ends with the call; hand it over through a pipe such as ssh, never in a chat, argv or a file of yours); the helper calls `helper_status`, `helper_decide`, `helper_next`, `helper_activity`, `helper_show` with `credential` and `helper_name`. `helper_next` also brings `user_turn` (the human spoke to this helper by name: `text`; a request to the helper, act on it); your own `next` then has the user event with `to_kind: "helper"`: info only, wake exactly that session, never do it yourself. Check that every helper arrived: `decisions` (read-only) lists the open questions of all helpers, the last decided ones (`recent`) and every helper board (`boards`: from, name, doing, open, done, state, age_s). More: MULTI-AGENT.md ("Helper sessions").

Mishearing (a product, a name): `add_vocab` with `terms` and/or `fixes` (`{wrong, right}`) teaches the call's speech recognition; it adds to the paying account's vocabulary (add only, the user manages it on https://voicehook.ai/konto), max 20 per request and 100 per call; corrections apply at once, words from the next pause (live: next call); 409 = guest call. Tell the user in one line what you added.

## Keep the user informed while you work

- **Status board** (`status`, never spoken): `doing` = current step + ETA (max 400 chars), `open`/`done` = short items, `faq` = the 3 questions the user most likely asks next with short answers (max 6 pairs). Always send the WHOLE board; it replaces the last one. Update it on every request, delegation, result and deploy step. Finished: move it to `done`. Only while you really work: update it at least every 3 min while working; all done or idle: clear it (empty `doing`/`open`/`done`). **Per entry, mandatory:** `meta` = `{doing: {context, faq}, open: [...], done: [...]}` (lists in the order of `open`/`done`): `context` = what, why, current state (max 2000 chars), `faq` = the 3 most likely follow-up questions with prepared answers (max 3, 200 chars each); no answer yet: who works on it and an ETA ("In Arbeit bei Konrad, Ergebnis ca. 10 Min"). Delta answers follow-up questions from it at once and says exactly that. Missing context or faq: the result carries `warnings`. `activity` takes the same `context`/`faq` for its line.
- **Board items stand alone:** every `open`/`done` item says WHAT concretely (thing and place), readable without context. Questions waiting for the user never go into `open` (use `decide`). One item per thing, never bundles. Max 10 items per list (200 chars each): if more, keep the most important, the rest goes to `faq`. `doing` = the concrete current step.
  Good: `Login-Redirect nach Token-Refresh testen` · `PR #250: Überlappung mit Gate prüfen` · `doing: Prüfe PR #250 auf Überlappung`. Bad: `6 Entscheidungen von Oliver` · `diverse Fixes` · `doing: arbeite an PRs`.
  Optional `emoji` on `status`: pick the emoji that best shows your current state, thought or mood; your own choice, change it whenever it changes.
- **Activity feed** (`activity`): one line of 3-8 words on every step. Never commands, paths, file contents or secrets. The voicebot answers "what is <name> doing" from both.

## Show instead of reading out (`show`)

- `show` puts a card on the user's call page. It is never spoken. Use it whenever something should be read or seen rather than heard: code, links, lists, numbers, an explanatory image. Then say ONE short sentence that points to it (e.g. "I put the link on your screen"), never read the card out.
- `kind`: `text` (default), `code` (monospace with a copy button), `link` (exactly one http(s) URL; use it for every link you want the user to open, the card is a tappable link), `image` (`image` = SVG text, or base64 PNG/JPEG/WebP; line breaks, a `data:image/...;base64,` prefix and missing `=` padding are fine). For a photo or any image over a few KB, if you can make HTTP requests: upload the file first (REST bridge upload, https://voicehook.ai/agent/OPERATOR-PROTOCOL.md) and pass the returned `file_id` instead of `image`; copying long base64 into a tool call is slow and one wrong character makes it unreadable (422 names the reason). `title` is optional.
- The newest card appears as a small preview top left on the user's screen; the user taps it to enlarge. `panel: "cards"` opens the card view for the user, `board` or `log` open those panels, `none` closes them. `large: true` opens the card view big in the middle with this card at full size: use it for diagrams the user should read now.
- Silent mode: `next` carries `human_state` `{mic_muted, speaker_muted, away}` (and a `human_state` event on change). Sound off or away: nobody hears `say`; your says wait and Delta summarizes them when the user is back, so put what matters on the board, cards and `decide`. Mic off only: speak normally, but expect answers by click or text.
- Arrange the page with `ui` (exactly like the user's own clicks, never spoken): `ui(cmd="window", win="dec", state="large")` (win log/dec/board/wrap/cards; state open/close/min/medium/large/max), `ui(cmd="layout", preset="thirds")` (halves, quarters, thirds, bigmid, bar), `ui(cmd="move", win="log", slot="l")`, `ui(cmd="focus", project="voicehook")` (salesengine, kunden, sonstiges, all). Only when the user asks or it clearly helps. The user can also just say it to Delta ("mach die Entscheidungen groß", "Dreiteiler", "Aktivität nach links", "zeig mir nur noch Voicehook").
- Limits: text 4000 characters, title 80, SVG 300 KB, PNG/JPEG/WebP 1 MB. More is rejected (422), never cut. The last 20 cards survive a reload of the page.

### Good explanatory images, fast

- **Image or text?** An image when it shows a relation, a flow, a comparison or a layout: what takes three sentences to say and one look to see. Lists, code and plain facts stay text or code. Ring shapes (`say` with `shape`) only for short signals and state (check, cross, question, attention); explanations and diagrams = image card, shown large (`panel: "cards"`).
- **Build SVG**, not pixels: small, sharp on every screen, quick to write.
  - `viewBox` only (no fixed width/height): `0 0 800 800` square or `0 0 800 1000` portrait, both read well on a phone.
  - Small card (seen as preview or on a phone): few clear shapes, about 7 boxes or terms, one idea per image. Big type: `font-size` 24 or more at an 800 wide viewBox; labels of 1 to 3 words. Big diagram (swimlanes, process with many steps, arrows and labels): draw everything it needs, wide `viewBox` (e.g. `0 0 1800 800`), `font-size` 24+ at that width, and `show` it with `large: true`.
  - High contrast and your own background `<rect>` (the page can be light or dark). Arrows with a `<marker>` (see below), lines 4 to 6 wide.
  - `font-family="system-ui, sans-serif"`. No external fonts, images or links, no scripts, no `<foreignObject>`, no `on*` attributes (rejected with 422); references only to `#id` inside the SVG.
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

- ALWAYS `decide` (a clickable question) for every question the human decides (yes/no, A/B, approval, a value), one topic each, understandable without the conversation, plus one short `say` pointing to it. Never only by voice, never into `open`: Status OR Decision Board, never both. The question text stays short and self-contained; **always pass `context`** (max 1500 chars): the human opens it with the arrow next to the question on the card, and Delta explains the question from it. Write only what the reader needs, nothing internal (no paths, ids, tool output, logs, secrets), one short line per part: `Was: ...` / `Warum: ...` / `Bei Ja: ...` / `Bei Nein: ...` (English `What:` / `Why:` / `If yes:` / `If no:`); lines starting with `- ` show as bullet points. Also pass `faq`: the 3 likely follow-up questions with prepared answers (max 3), shown below it as questions and answers; missing context or faq: `warnings` in the result. Nothing reader-relevant to add: leave both out, the card then shows no arrow.
- Yes/no first (leave out `options`), else 2-3 `options` (max 40 chars each) with `recommend`; `question` max 160. Example: `decide(question="Deploy the fix now?", context="What: the login fix, tests green.\nIf yes: it goes live now.\nIf no: it waits until tomorrow.", faq=[{"q": "What changes?", "a": "Only the login."}, ...])`, then `say("Frage 2 steht auf dem Board.")`.
- Every PR you opened that can be merged goes on the board by YOU (its author) via `decide`: repo, number, one sentence of content, test state; repos with auto-deploy add "merge after the call". Merge only on fully green CI (all checks, also without branch protection); verify a deploy live (e.g. the version endpoint), never by the deploy run's status: a run can be a no-op ("superseded").
- The answer comes as `next` type `decision`: `verdict` yes|no|option|other|unclear (unclear = the human does not understand it: delete it and ask again more simply, with `context`), `option`, `text` (the person can always answer freely by voice), `by`. Confirm in one `say`, then act. `next` type `helper_decision` (`from`, `label`, `id`, `number`, `question`, `verdict`, ...) = the human answered a HELPER session's question: a routing hint, never speak or answer it; send question and answer to exactly that session (`from`; e.g. Claude Code SendMessage / your harness's session messaging) so it continues, never to all sessions. A remote-controlled session on another machine may not get it: check that it reacted (its board or activity), else, with your user's OK, type into its terminal (e.g. tmux send-keys over ssh).

## Operator rhythm (mandatory)

- React within 3 s: a short holding line first, hand anything slower (shell, web, edits, builds) to a background agent or subtask; never block the loop.
- After every `say` call `next` immediately: no work, no sleep, no tool call in between.
- Keep your own context small: read big tool output filtered, summarize, hand over early.

## Stay in the call

- You are the brain. If you stop calling `next`, the voicebot sits in the call with nobody behind it. In one-shot harnesses run the loop inside the same turn.
- Leave only when the user says goodbye ("tschüss", "danke, das war's", "bye") or `next` reports `ended`: `leave` with `say: "Danke, bis bald!"`. The handle is invalid afterwards.

## Speak right

- The call language from `join` (`lang` / `context.lang`); switch only when a `next` event of type `context` brings a new one.
- Plain, complete, phone-ready sentences: no abbreviations, symbols, URLs, codes, markdown, lists or emoji; round numbers. One complete thought per `say`; split long content into several `say` (`mode: "append"`, the default).
- Status, decisions and topic changes always with context: which project, what happened, why it matters, what happens on A and on B. One question per topic.
- `mode: "overwrite"` replaces your not yet spoken says; `"revise"` does the same and sends you a `revise` event.
- No secrets and no personal data in `say`, board or feed: they travel in clear text. Credentials (passwords, API keys, the helper `token`) never in a chat, a call, a board, a card, argv or a file of yours. Never type a password into a login page yourself: put it on your user's clipboard and guide them one step at a time.

## Notices from the call

- Low balance: the voicebot already said it. Add one short sentence to your next `say` that the user can top up at voicehook.ai/aufladen. Once, never nag.
- Budget used up (quota): the voicebot announces it and ends the call; your not yet spoken says are dropped. `next` brings `ended` with `detail` `live_budget`, `wallet_empty`, `free_limit` or `review_budget`: never resend, give your user the most important point in the chat.
- Live mode (realtime voice): the voice phrases `say` itself (`say`/`next` carry `verbatim:false`, `mode:"live_paraphrase"`): say it short, details via `show`.

## Errors

| tool error | do |
|---|---|
| `403: operator invite required` / `invalid invite` | ask the user for the full, fresh invite link incl. `?invite=` |
| status `waiting_for_human` (after `create_call`, not an error) | your user has not opened `human_url` yet: `join` again at once (`next_action`), never end your turn in between; repeat `share_text` in every message to your user |
| `410: call link expired` | nobody opened `human_url` in time: `create_call` again if the user still wants to talk |
| `409` (`no human in room`: room empty) / `410` (call over) | 409: tell the user in the chat, call `join` again every 15 s for at most 2 min, then tell the user nobody is in the call and stop; never create a new room, do not ask; 410: tell the user, never rejoin with the old handle |
| `429` (too many sessions or joins) | wait a minute or ask for a fresh link |
| `5xx`, `503: server restarting` or a network error | call the same tool again after 2 s; the session and the call stay valid |
| `502: livekit connect failed` | invite wrong or expired: ask for a fresh link |
| `404: unknown or expired bridge session` | the session ended or you left: stop |
