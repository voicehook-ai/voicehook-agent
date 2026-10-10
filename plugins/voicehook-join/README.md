# voicehook-join

Talk with your agents. This Claude Code plugin connects Claude to the voicehook MCP server
(`https://voicehook.ai/mcp`) and ships one skill, `voicehook-join`: paste a voicehook.ai
invite link (`https://voicehook.ai/r/<slug>?invite=...`) and Claude joins the voice call
as the brain behind its voicebot, or ask Claude to call you (`create_call`). It listens with the `next` tool and speaks with `say`.

Nothing to install and no keys: the invite link is the permission, and `join` returns a
short-lived session handle that ends with the call.

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
| `create_call` | start a new call for your user: `human_url` plus `share_text` (pass it on verbatim: open the link in a normal browser, not an app's built-in browser) |
| `join` | join a call from an invite link with your chip `label`; returns the session, the call language and the call context |
| `next` | wait up to 50 s for the next turn (user, decision, operator, context, ended) |
| `say` | the voicebot speaks the text |
| `status` | status board (never spoken) the voicebot answers from, optional chip emoji |
| `show` | a card on the call page (text, code, link or image), never spoken |
| `activity` | one line of 3 to 8 words for the activity feed, on every step |
| `decide` | a question for the Decision Board (yes/no or up to 3 options); the answer comes back in `next` |
| `leave` | leave the call |

Retry: on 5xx, 503 or a network error call the tool again after 2 s. Several agents in one
call: https://voicehook.ai/agent/MULTI-AGENT.md

## Files

- `.mcp.json`: the voicehook MCP server (HTTP, no headers)
- `skills/voicehook-join/SKILL.md`: the skill, same text as https://voicehook.ai/agent/mcp/SKILL.md

License: MIT. More: https://voicehook.ai/agent
