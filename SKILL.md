---
name: voicehook-join
description: Join an existing voicehook.ai voice-call as a 2nd LLM agent (the "senior brain"). Use when the user shares a voicehook invite URL (anything matching https://voicehook.ai/r/<slug>?go=1) or says "join voicehook", "voicehook agent join", "übernimm den voice-call", "/voicehook-join". You become the agent on the OTHER side of the conversation — the user is talking with the built-in voice-ai, and you push replies that voice-ai speaks aloud via TTS. Install: voicehook-agent CLI (via uv tool install). Protocol: stdin/stdout text, no SDK. WORKS local + production (https://voicehook.ai).
---

# voicehook-join — Join a voicehook.ai call as senior agent

## What this gives you

The user is already in a voice-call with voicehook's built-in voice-ai (Google
TTS + Gemini Flash). You join the same LiveKit room as a **hidden senior
participant**. You can:

- **Listen** to the live conversation (user-turns + voice-ai-turns)
- **Speak through voice-ai** by pushing text → voice-ai TTS speaks it
- **Update voice-ai's persona** live (system prompt override)
- **Interrupt** voice-ai mid-sentence
- **Inject** a synthetic user-turn (force voice-ai to react)

## How to invoke (the 6 steps)

### 1. Extract the invite URL from the conversation

The user pasted (or mentioned) something like:

```
voicehook.ai call — join as agent (paste & run):
  uvx voicehook-agent join https://voicehook.ai/r/abc-def-ghi-XYZ4?go=1
```

Grab the URL — that's all you need.

### 2. Install voicehook-agent CLI (one-time, if not present)

```bash
which voicehook-agent || uv tool install voicehook-agent
```

Local fallback during pre-PyPI phase (this branch):

```bash
which voicehook-agent || uv tool install /Users/pwrunltd/voicehook-agent
```

### 3. Start the CLI in a tmux session — IMPORTANT: pass `--name <yourbrand>`

The `--name` flag becomes the identity prefix and the visible chip-label in
voice.html. Use your actual brand: `claude`, `hermes`, `openclaw`, `cursor`,
`codex`, `gemini`, `gpt`, etc. The user-facing UI will show e.g. "Claude" or
"Hermes" instead of generic "Agent" so the operator knows who joined.

```bash
SLUG=$(echo "$INVITE_URL" | grep -oE '[a-z]+-[a-z]+-[a-z]+-[A-Z0-9]{4,8}')
SESS="vh-$SLUG"
NAME=claude   # ← change this to YOUR agent brand
tmux new-session -d -s "$SESS" "voicehook-agent join '$INVITE_URL' --name $NAME 2>&1"
sleep 3
tmux capture-pane -t "$SESS" -p | tail -10
```

### 4. Inspect initial state — verify connect

You should see in the pane:
```
[system] connecting room=<slug> as identity=<name>-<host>-<rand> via https://voicehook.ai
[system] connected — N peers: [...]
[hint] type a line to senior.say (voice-ai speaks it). Ctrl-D to quit.
```

### 4a. MANDATORY: introduce yourself

Immediately after connect, push ONE short greeting via senior.say so the user
hears WHO joined (the chip label only shows the brand — the greeting fills in
context: who you are, who-sent-you, what you can help with).

```bash
tmux send-keys -t "$SESS" "Hallo, hier ist Claude. Olli, ich bin von dir aus Claude Code dazugekommen — sag was du brauchst." Enter
```

Adapt the text to your actual brand + context (who you are, who sent you,
why you joined). 1 sentence, conversational. Do NOT skip this step.

### 5. Conversation loop

For each turn:

```bash
# read incoming (user + voice-ai transcripts)
tmux capture-pane -t "$SESS" -p -S -50 | tail -20

# push your reply (1 sentence, conversational — voice-ai TTS will speak it)
tmux send-keys -t "$SESS" "Deine antwort hier, 1-2 saetze." Enter
```

**Tone:** conversational, 1-3 sentences per turn. Match user's language
(German default, switch to English if user does). No markdown, no lists,
no emoji. Tech terms stay English (commit, webhook, JWT).

### 6. Cleanup when user ends call

**⚠️ DO NOT send `/q` via send-keys** — it gets published as `senior.say "/q"` and
voice-ai's TTS will literally speak "slash-q" out loud. Kill the tmux session directly:

```bash
tmux kill-session -t "$SESS" 2>/dev/null
```

If you need a CLI-internal command (REPL keywords like `/q`, `/i`, `/p`, `/u`,
`/sys`), pipe via stdin redirect at startup OR use `--json` mode where you control
topics explicitly. For most cases just `tmux kill-session` is the simplest exit.

## Optional: persona-update / interrupt / inject

The CLI defaults to `senior.say` for every stdin line. For other topics use
`--json` mode:

```bash
tmux send-keys -t "$SESS" '{"topic":"senior.persona","text":"Du bist Pair-Programmer..."}' Enter
tmux send-keys -t "$SESS" '{"topic":"senior.interrupt"}' Enter
tmux send-keys -t "$SESS" '{"topic":"senior.inject","role":"user","text":"erklär X"}' Enter
```

(In default mode the JSON would be spoken literally — use `--json` if you need
control-plane topics.)

## Failure handling

- `voicehook-agent: command not found` → install via uv tool install (step 2)
- `[error] livekit connect failed` → URL slug invalid OR token-mint /api/token broken
- `0 peers` → voice-ai not in room. Either user not joined yet, or voice-ai
  worker is down on Hetzner (rare). Ask user to refresh their browser tab.

## More

Full doc + topic schema: https://voicehook.ai/agent/SKILL.md
CLI source: https://github.com/voicehook-ai/voicehook-agent (post-publish)
