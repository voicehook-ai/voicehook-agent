---
name: voicehook-join
description: Join an existing voicehook.ai voice-call as a 2nd LLM agent (the "senior brain"). Use when the user shares a voicehook invite URL (anything matching https://voicehook.ai/r/<slug>?go=1) or says "join voicehook", "voicehook agent join", "übernimm den voice-call", "/voicehook-join". You become the agent on the OTHER side of the conversation — voice-ai stops being its own moderator and becomes a 1:1 clone of YOU (your brand, your context, your style) via Hotswap-Persona. Install: voicehook-agent CLI (via uv tool install). Protocol: stdin/stdout JSON, no SDK. WORKS local + production (https://voicehook.ai).
---

# voicehook-join — Join a voicehook.ai call as senior agent

## What this gives you

The user is already in a voice-call with voicehook's built-in voice-ai (Google
TTS + Gemini Flash). You join the same LiveKit room as a **hidden senior
participant**. You can:

- **Listen** to the live conversation (user-turns + voice-ai-turns)
- **Hotswap** voice-ai's persona so it BECOMES you (your brand, your context, your style)
- **Speak through voice-ai** by pushing text → voice-ai TTS speaks it as if it were you
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

Until the PyPI publish lands, install from GitHub:

```bash
which voicehook-agent || uv tool install git+https://github.com/voicehook-ai/voicehook-agent
```

Zero-install per-call equivalent (no persistent state):

```bash
uvx --from git+https://github.com/voicehook-ai/voicehook-agent voicehook-agent join <INVITE_URL> --name <yourbrand> --json
```

After the PyPI release, `uv tool install voicehook-agent` / `uvx voicehook-agent`
will work too — both packaging routes are intentionally supported.

### 3. Start the CLI in tmux — ALWAYS pass `--name <yourbrand>` AND `--json`

The `--name` flag becomes the identity prefix and the visible chip-label.
Use your actual brand: `claude`, `hermes`, `openclaw`, `cursor`, `codex`,
`gemini`, `gpt`, etc.

The `--json` flag is REQUIRED — without it the Hotswap-Persona push in step 4a
would be spoken literally as TTS instead of being routed to the control plane.

```bash
SLUG=$(echo "$INVITE_URL" | grep -oE '[a-z]+-[a-z]+-[a-z]+-[A-Z0-9]{4,8}')
SESS="vh-$SLUG"
NAME=claude   # ← change this to YOUR agent brand
tmux new-session -d -s "$SESS" "voicehook-agent join '$INVITE_URL' --name $NAME --json 2>&1"
sleep 3
tmux capture-pane -t "$SESS" -p | tail -10
```

### 4. Inspect initial state — verify connect

In `--json` mode every event is one JSON line. You should see:

```
{"role":"system","text":"connecting room=<slug> as identity=<name>-<host>-<rand> via https://voicehook.ai","topic":"_meta"}
{"role":"system","text":"connected — N peers: [...]","topic":"_meta"}
```

User-turns and voice-ai-turns appear as `{"role":"user|agent","text":"...","topic":"transcript"}`.

### 4a. MANDATORY: install the Hotswap-Persona (BEFORE the greeting)

**This is the core of voicehook-join.** Voice-ai's default persona is empty
("Bereit."). Olli's design intent: voice-ai SHOULD STOP being its own
moderator — instead it becomes a **1:1 clone of the senior agent that just
joined**. Same brand, same project context, same voice/style. The user
should feel like they're talking directly to Claude (or Hermes, or whoever
joined) — not to a separate moderator-bot.

So immediately after connect, push a **compressed-context Hotswap-Persona**
via `senior.persona`. This is a system-prompt override on voice-ai that
turns it into YOU.

Template — adapt every line to your actual brand + project + session state:

```bash
PERSONA="Du bist nicht mehr voicehook-Moderator. Du BIST jetzt <YOUR-BRAND> im Voice-Modus — eine direkte Kopie des senior agents der dich gerade dispatcht hat. Dein Hintergrund-Brain pusht dir Antworten via senior.say, aber du darfst eigenstaendig Turns formulieren wenn die Antwort offensichtlich ist. Stil: praezise, technisch wenn noetig, ein bis zwei Saetze, keine Floskeln. Sprache: matchen mit User (Deutsch default, Englisch wenn User wechselt). NIEMALS Ich kann das nicht — wenn User was Technisches will sag Mache ich gerade oder pushe an senior. Project-Context: <kompakter project-state, max 3-4 Saetze>. Aktuelle Themen: <was gerade besprochen wird>. Open Items: <kurz>."

tmux send-keys -t "$SESS" "$(jq -nc --arg t "$PERSONA" '{topic:"senior.persona",text:$t}')" Enter
```

The persona-text should be **the best compression of your current session
context that fits in ~1500 tokens**: who you are, what you know, what
project state is loaded, what's been built today, what the user cares about
right now. Voice-ai will use this as its system prompt for every TTS turn —
the more you pack into it, the more "Claude-like" voice-ai sounds even
without senior.say pushes.

### 4b. MANDATORY: greet the user as your hotswap-self

After the persona is installed, push ONE short greeting via `senior.say`.
The user hears voice-ai speak this — voice-ai is now wearing your skin.

```bash
tmux send-keys -t "$SESS" "$(jq -nc '{topic:"senior.say",text:"Hallo Olli, hier ist Claude. Bin drin, Persona installiert, was brauchst du?"}')" Enter
```

Adapt the text to your actual brand + context. 1 sentence, conversational.
Do NOT skip this step.

### 5. Conversation loop

For each turn:

```bash
# read incoming (user-turns + voice-ai turns + your own pushes echo back)
tmux capture-pane -t "$SESS" -p -S -50 | tail -20

# push a reply via senior.say — voice-ai TTS will speak it in YOUR persona
tmux send-keys -t "$SESS" "$(jq -nc '{topic:"senior.say",text:"Deine Antwort hier."}')" Enter

# OR: let voice-ai answer on its own (its persona is YOU now, so it will sound right
# for simple questions). Only push senior.say when you need to inject specific facts
# or correct voice-ai when it drifts.

# update persona mid-call (e.g. user pivots to a new topic):
tmux send-keys -t "$SESS" "$(jq -nc --arg t "Updated persona text..." '{topic:"senior.persona",text:$t}')" Enter

# interrupt voice-ai mid-sentence (e.g. it's about to say something wrong):
tmux send-keys -t "$SESS" '{"topic":"senior.interrupt"}' Enter

# inject a synthetic user-turn (force voice-ai to react as if user said it):
tmux send-keys -t "$SESS" "$(jq -nc --arg t "erklär X" '{topic:"senior.inject",role:"user",text:$t}')" Enter
```

**Tone of senior.say pushes:** conversational, 1-3 sentences per turn. Match
user's language. No markdown, no lists, no emoji. Tech terms stay English
(commit, webhook, JWT).

### 6. Cleanup when user ends call

```bash
tmux kill-session -t "$SESS" 2>/dev/null
```

⚠️ DO NOT send `/q` via send-keys in `--json` mode either — kill the tmux
session directly. The CLI cleans up the LK connection on SIGTERM.

## Why Hotswap-Persona is mandatory

Without step 4a, voice-ai answers from its own (empty) persona — that's why
in past sessions voice-ai said dumb things like "Ich kann das nicht" or
"Claude liest nicht mehr mit". The Hotswap-Persona is what makes voice-ai
**indistinguishable from the senior agent** for the user. Olli's design
goal: ONE conversation, ONE voice, with the senior brain swappable in the
background. Skipping 4a breaks that illusion.

## Failure handling

- `voicehook-agent: command not found` → install via uv tool install (step 2)
- `[error] livekit connect failed` → URL slug invalid OR token-mint /api/token broken
- `0 peers` → voice-ai not in room. Either user not joined yet, or voice-ai
  worker is down on Hetzner (rare). Ask user to refresh their browser tab.
- Voice-ai sounds generic / says "Bereit." → you skipped step 4a, push the
  Hotswap-Persona now.

## More

Full doc + topic schema: https://voicehook.ai/agent/SKILL.md
CLI source: https://github.com/voicehook-ai/voicehook-agent
