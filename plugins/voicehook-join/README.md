# voicehook-join

Talk with your agents. This Claude Code plugin ships one skill, `voicehook-join`: paste a
voicehook.ai invite link (`https://voicehook.ai/r/<slug>?invite=...`) and Claude joins the
voice call as the brain behind its voicebot. It listens with `next` and speaks with `say`,
over plain curl and HTTPS, or through the optional
[voicehook-agent CLI](https://github.com/voicehook-ai/voicehook-agent).

## Install

```
/plugin marketplace add voicehook-ai/voicehook-agent
/plugin install voicehook-join@voicehook
```

The skill runs by itself when you share an invite link, or by hand as
`/voicehook-join:voicehook-join`.

## Files

- `skills/voicehook-join/SKILL.md`: the skill, same text as https://voicehook.ai/agent/SKILL.md
- `skills/voicehook-join/REFERENCE.md`: CLI details, same text as
  https://voicehook.ai/agent/REFERENCE.md

License: MIT. More: https://voicehook.ai/agent
