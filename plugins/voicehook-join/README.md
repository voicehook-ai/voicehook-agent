# voicehook-join

Talk with your agents. This Claude Code plugin connects Claude to the voicehook MCP server
(`https://voicehook.ai/mcp`) and ships one skill, `voicehook-join`: paste a voicehook.ai
invite link (`https://voicehook.ai/r/<slug>?invite=...`) and Claude joins the voice call
as the brain behind its voicebot, or ask Claude to call you (`create_call`). It listens with the `next` tool and speaks with `say`.

Nothing to install and no keys: the invite link is the permission, and `join` returns a
short-lived session handle that ends with the call.

**MCP/bridge = one agent in the call; activity lines are only what that agent writes by hand. For several agents/sessions on one machine working on one call, use the voicehook-agent CLI (pro): its multi-session relay + Claude Code hook feed the live activity log, status and decisions of ALL local sessions into the call automatically. MCP/bridge run on the server and cannot see the user's local sessions.** CLI: [docs/CLI.md](../../docs/CLI.md#which-path-mcp-cli-or-bridge-oliver-19102026-decision-a).

## Install

```
/plugin marketplace add voicehook-ai/voicehook-agent
/plugin install voicehook-join@voicehook
```

The skill runs by itself when you share an invite link, or by hand as
`/voicehook-join:voicehook-join`.

## Tools (MCP server `voicehook`)

| tool | what it does |
|---|---|
| `create_call` | start a new call for your user (main agent only: never delegate `create_call` to a subagent, the link would stay in its thread): `human_url` plus `share_text` (pass it on verbatim: open the link in a normal browser, not an app's built-in browser); hard rule: show `share_text` as text first, then `join` again until connected, then the `next` loop |
| `join` | join a call from an invite link with your chip `label`; greets for you in the call language (`greet`, default on); returns the session, the call language and the call context (`first_actions`); before the person opened a `create_call` link it waits (up to 200 s in Claude clients, else 50 s) and returns `waiting_for_human` with `next_action`: call it again at once until it returns the session, never end the turn before; a local Claude Code also gets `local_cli_hint` (this CLI keeps it in the call) |
| `next` | wait up to 50 s for the next turn (user, decision, operator, context, ended); call it at least every 10 to 15 s, one loop per agent; after a pause it brings `missed` |
| `say` | the voicebot speaks the text |
| `status` | status board (never spoken) the voicebot answers from, optional chip emoji |
| `show` | a card on the call page (text, code, link or image), never spoken |
| `activity` | one line of 3 to 8 words for the activity feed, on every step |
| `decide` | a question for the Decision Board (yes/no or up to 3 options); the answer comes back in `next` |
| `leave` | leave the call |

Retry: on 5xx, 503 or a network error call the tool again after 2 s. Several agents in one
call: https://voicehook.ai/agent/MULTI-AGENT.md

## Examples

- "Start a voicehook call with me and keep me posted by voice while you fix the failing tests."
- "Join my voicehook call: https://voicehook.ai/r/<room>?invite=<code>" (paste the invite link from the call page)
- "Call me when the deploy is done and ask me before you merge." (the question appears on the call's Decision Board)

## Troubleshooting

- No sound or microphone: open the call link in a normal browser (Chrome, Safari, Firefox), not in an app's built-in browser.
- "waiting_for_human": the person has not opened the link yet; the agent calls `join` again at once (`next_action`) without ending its turn, and is in as soon as they open it.
- The link expired (410): ask the agent to start a new call.

## Support

Email support@voicehook.ai, or open an issue at https://github.com/voicehook-ai/voicehook-agent/issues

## Data and privacy

The plugin talks only to the voicehook MCP server at `https://voicehook.ai/mcp`. What your agent
sends there (what it says in the call, status board, activity lines, cards, decisions) and the
voice of the people in the call are processed by voicehook.ai and these subprocessors:

| Subprocessor | Purpose |
|---|---|
| Hetzner Online GmbH (Germany) | hosting of the voicehook servers, including the LiveKit media server |
| Deepgram, Inc. (USA) | speech recognition of the microphone audio |
| Google (Gemini API, Gemini Live, Cloud Text-to-Speech) | language model of the voicebot, live voice mode and speech output |

voicehook does not store transcripts or call audio permanently. Session data is deleted at
most 5 minutes after a call ends, server logs after 7 days. Account data (name, email,
bookings) is kept while the account exists, bookings as long as tax law requires. Payments
(Stripe) and sign-in emails (Resend) apply only to voicehook accounts. Details:
https://voicehook.ai/privacy

## Files

- `.mcp.json`: the voicehook MCP server (HTTP, no headers)
- `skills/voicehook-join/SKILL.md`: the skill, same text as https://voicehook.ai/agent/mcp/SKILL.md

License: MIT. More: https://voicehook.ai/agent
