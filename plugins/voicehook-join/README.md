# voicehook-join

Talk with your agents. This Claude Code plugin connects Claude to the voicehook MCP server
(`https://voicehook.ai/mcp`) and ships one skill, `voicehook-join`: paste a voicehook.ai
invite link (`https://voicehook.ai/r/<slug>?invite=...`) and Claude joins the voice call
as the brain behind its voicebot. It listens with the `next` tool and speaks with `say`.

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
| `join` | join the call from an invite link, returns the session handle |
| `next` | wait up to 50 s for the next turn of the user |
| `say` | the voicebot speaks the text |
| `status` | status board (never spoken) the voicebot answers from |
| `activity` | one line for the activity feed |
| `leave` | leave the call |

## Files

- `.mcp.json`: the voicehook MCP server (HTTP, no headers)
- `skills/voicehook-join/SKILL.md`: the skill, same text as https://voicehook.ai/agent/mcp/SKILL.md

License: MIT. More: https://voicehook.ai/agent
