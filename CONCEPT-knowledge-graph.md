# CONCEPT — Self-Updating Knowledge-Graph for the voicehook Voice-Agent

Status: draft v1 — 2026-05-18
Owner: voicehook-agent CLI + voicehook-v2 agent.py
Predecessor spike: `/Users/pwrunltd/voicehook-call-starten/examples/memory/` (graph.jsonl, curator.sh, stable_persona.txt — 17.05.2026)

---

## 1. Goals & non-goals

**Goals**
- **≥ 80 % in-prompt answer-rate**: Voice-Agent (Gemini 2.5 Flash on Hetzner, agent.py L132) answers from its loaded system-prompt without calling the Senior (Claude via `senior.say`).
- **≤ 1 / 5 fallback** to Claude on `senior.*` topics; Senior remains the strategic brain, not the FAQ-bot.
- **Self-updating**: nodes refresh from repo state, chat history, and explicit `/kg add` writes without human curation per node.
- **Multi-server portable**: same KG runs against `127.0.0.1:7400` (v2 dev) and `https://voicehook.ai/api` (prod), driven by `VOICEHOOK_CONTROL_URL`.
- Stay inside the prompt-cache budget (< 15 k tokens, see `latency` node in graph.jsonl L10).

**Non-goals**
- Not a RAG store for end-user documents. Scope = voicehook product knowledge only.
- No external vector-DB infra (Pinecone, Weaviate). On-disk JSONL + in-process embeddings.
- Not a replacement for `web_search` (agent.py L39) — that tool stays for genuinely fresh facts.

---

## 2. Schema — extended JSONL node

The spike's 5 fields (id, domain, triggers, name, summary) are kept; six fields added:

```json
{
  "id": "agent-py",
  "domain": "architecture",
  "lang": ["de", "en"],
  "name": "agent.py Zwei-Prozess-Architektur",
  "triggers": ["agent.py", "architektur", "ipc", "fastapi"],
  "embeddings": {"model": "text-embedding-3-small", "vec_ref": "vecs/agent-py.f32"},
  "summary": "agent.py spawnt zwei FastAPI Server: Parent auf 7400 …",
  "priority": 0.85,                          // 0.0–1.0, decays w/ age + boosts w/ hit-rate
  "recency": "2026-05-18T14:07:00Z",         // last source-of-truth touch
  "xref": ["persona", "inject", "say"],      // related node ids
  "source": {"kind": "file", "path": "voicehook-v2/agent.py", "lines": "267-349", "sha": "ab12cd"},
  "hit_count": 17,                           // how often this node was injected
  "answer_count": 12                         // how often answer was satisfactory (no Senior fallback)
}
```

`answer_count / hit_count` is the **per-node fallback rate** — drives priority decay (§4).

---

## 3. Build pipeline (bootstrap)

A one-shot Python script `voicehook-agent kg build` (new sub-command in `src/voicehook_agent/`) runs four passes:

1. **Repo scan** (~30 min effort): walks `voicehook/`, `voicehook-v2/`, `voicehook-animations/`, `voicehook-docs/` looking for:
   - `README.md`, `HANDOFF-*.md`, `SKILL.md` → 1 node per H2 section.
   - `*.py` modules with docstrings → 1 node per public route/class (e.g. agent.py L223 `senior.persona` → node `persona`).
   - `ROADMAP.md`, GitHub issue titles via `gh issue list --json` → 1 node per open issue.

2. **Chat-history harvest** (~1 h effort): parses `messages.log` (agent.py L101 `log_message`) for user-questions where the next agent-turn began with hedging phrases (`"Das weiß ich nicht"`, `"einen Moment"`). Each cluster of similar questions → candidate node, escalated to Claude for summarization.

3. **LLM curation pass** (~2 h effort): feeds each candidate to Gemini 2.5 Flash with the prompt *"Compress to ≤ 280 chars, German, conversational, no markdown. Output triggers as a comma list."* Output saved as draft node.

4. **Human accept gate**: `voicehook-agent kg review` shows a TUI diff; operator presses `y/n/edit`. Accepted nodes append to `graph.jsonl`.

Initial yield target: **80–120 nodes** ≈ 30–40 kB JSONL, ≈ 8 k tokens compressed.

---

## 4. Update loop

Three independent triggers, all hitting the same writer (`kg_writer.py`):

| Trigger | Cadence | Where it runs |
|---|---|---|
| **A. File-watch** on `voicehook*/` repos | inotify / fswatch, debounced 30 s | Hetzner VM next to agent.py (systemd unit `voicehook-kg-watch.service`) |
| **B. Conversation-mining** of messages.log | every 5 min, batched | Same VM, cron `*/5 * * * *` |
| **C. Explicit writes** via `POST /kg/upsert` | on demand | agent.py parent_app (new endpoint, ~40 LOC after L270) |

The conversation-miner (B) is the self-updating heart:
- Scans last 5 min of `messages.log` for turns followed by `senior.say` from Claude.
- Treats every such Claude reply as **a missed-knowledge signal**: extracts the user question, asks Gemini Flash to compress Claude's reply into a node, lowers `priority` of the node that should have answered (if any), and queues the new node for the §3-step-4 review gate.
- After 7 d without a hit, `priority -= 0.1`; after `hit_count > 5 && answer_count/hit_count < 0.4`, node is flagged for rewrite (likely too vague).

Orchestrator runs on Hetzner because (a) it shares the messages.log file with agent.py, (b) the cron pattern from curator.sh L18-129 is already proven there.

---

## 5. Predictive pre-injection

Per-conversation hot-layer selection (replacing curator.sh L62-72 naive keyword match):

1. **Embed last 3 user-turns** with `text-embedding-3-small` (cached, 5 ms locally).
2. **Cosine-rank** against precomputed node embeddings (in `vecs/*.f32`, mmap'd, ~50 µs for 100 nodes).
3. **Boost** nodes whose `triggers` keyword-match (legacy signal, weight 0.3) and whose `xref` appears in the last 2 turns (graph-walk, depth 1, weight 0.2).
4. **Intent classifier** (one-shot Gemini Flash call, 80 ms, gated to once per 4 user-turns): outputs `{intent: "deploy|pricing|architecture|smalltalk", confidence}`. Restricts the candidate pool to nodes with matching `domain`.
5. **Token budget**: pick top-K nodes until the running token sum hits the dynamic-suffix budget (§6). Typical K = 6-10.

Cold-start (no user-turns yet): inject the 8 highest-`priority` nodes — matches curator.sh "no Senior dilution" rule (L69-72).

---

## 6. System-prompt assembly & token budget

Gemini 2.5 Flash sweet spot: **prefix ≤ 12 k tok, suffix ≤ 3 k tok** → TTFT < 100 ms (graph.jsonl `latency` L10).

```
┌─ STABLE PREFIX (≈ 11 k tok, cached, identical for every /persona POST) ─┐
│  • stable_persona.txt content (mission, identity, style, cmd parser)    │
│  • ALL high-priority nodes (priority ≥ 0.8) — pre-rendered             │
│  • Hash-stamped: prefix_sha=abc123 → cache-key                          │
├─ DYNAMIC SUFFIX (≈ 2-3 k tok, changes per conversation) ───────────────┤
│  • Top-K hot-layer nodes from §5                                       │
│  • RECENT USER-TURNS echo (last 3, ≤ 200 chars each — curator.sh L83)  │
│  • Optional "MISSED-KNOWLEDGE HINT" if conversation-miner flagged one  │
└─────────────────────────────────────────────────────────────────────────┘
```

Per-node budget = `floor((suffix_budget - echo_size) / K)` ≈ 250-300 tokens. Nodes longer than that are auto-truncated at the last sentence-boundary before re-injection (one-time fix, not stored).

---

## 7. Cache strategy

- **Prefix immutable per release**: rebuilt only when `kg build` runs or a `priority ≥ 0.8` node mutates. SHA written to `prefix.sha`. Gemini's 5-min cache-TTL (graph.jsonl `persona` L6 — "75 % discount") means a 10-min idle conversation still cache-hits if no high-priority churn.
- **Suffix never cached** — that's fine, suffix is only 2-3 k tokens.
- **Invalidation rules**: prefix rebuild on (a) `kg build` finishes, (b) any node with `priority ≥ 0.8` changes `summary`, (c) `stable_persona.txt` mtime changes. Otherwise suffix-only POST `/persona` (which agent.py L223 `senior.persona` handles in-place via `agent_obj.update_instructions`).
- **Multi-room**: each LiveKit room gets its own suffix; prefix shared across rooms (same Gemini account, same cache).

---

## 8. Fallback path

When Voice-Agent doesn't know:

1. Voice-Agent recognises its own ignorance via the existing rule (agent.py L65): *"Wenn du etwas wirklich nicht weißt … sag das ehrlich kurz."*
2. New behaviour: the **agent emits a marker turn** `"[FALLBACK] <user-question>"` into the chat-context via `senior.inject role=system`. The conversation-miner (§4-B) sees that marker within 5 s, triggers a **fast path** (1 s cadence instead of 5 min) that POSTs to Claude's `senior.say` queue.
3. Senior CLI (`voicehook-agent join`, SKILL.md L84-93) is already polling — its tmux pane sees the marker and Claude responds via `senior.say`.
4. **Latency cost**: ~1.2 s extra (marker → miner → Claude → TTS) on top of the normal 700 ms (graph.jsonl L10). UX cover: Voice-Agent says one bridge phrase first ("Einen Moment, ich check das kurz." — already exists, L63), buying perceptual headroom.

Target: this path fires on ≤ 20 % of user-turns. Above 25 % for > 1 h → alert, conversation-miner rewrites bottom-quartile nodes.

---

## 9. Deployment

Files & where they live:

| File | Path | Status |
|---|---|---|
| `kg/graph.jsonl` | `/var/lib/voicehook-kg/graph.jsonl` (Hetzner) | new |
| `kg/vecs/*.f32` | `/var/lib/voicehook-kg/vecs/` | new |
| `kg/stable_persona.txt` | `/var/lib/voicehook-kg/stable_persona.txt` | copy from spike |
| `kg_writer.py` + `kg_select.py` | inside voicehook-v2 repo `kg/` | new ~400 LOC |
| `/kg/upsert`, `/kg/select` endpoints | append to `agent.py` after L270 | ~60 LOC |
| `voicehook-kg-watch.service` | `/etc/systemd/system/` | new unit |
| `voicehook-agent kg {build,review,stats}` | extend `src/voicehook_agent/` | ~250 LOC |
| `SKILL.md` update | `/Users/pwrunltd/voicehook-agent/SKILL.md` + mirror | append §"KG fallback" |

agent.py changes are minimal: a `before_session_start` hook that POSTs current selection to its own `/persona` and a 60-s refresher (replaces curator.sh L18-129 — same logic, in-process, no bash).

Effort: bootstrap script + schema **1 day**, watch+miner loops **1 day**, agent.py wiring + endpoints **0.5 day**, CLI subcommands **0.5 day**. Total **3 days** for a working v1.

---

## 10. Test plan — proving the 1/5 fallback rate

1. **Eval-set**: 100 curated voice-questions split 60/40 across (real messages.log historical) + (adversarial Claude-generated edge cases). Stored at `voicehook-agent/tests/eval_set.jsonl`.
2. **Harness**: `voicehook-agent kg eval` opens a headless LiveKit room, injects each question via `senior.inject role=user`, captures the agent reply + whether `[FALLBACK]` marker fired.
3. **Pass criteria**: ≥ 80 / 100 answered without fallback marker AND human-graded "correct enough" (3-tier rubric: correct / acceptable / wrong). Re-graded by Claude with a fixed grader-prompt to avoid grader-drift.
4. **Adversarial pass**: 20 questions designed to look in-domain but require fresh facts (e.g. *"Wie viel Stripe-Umsatz haben wir gestern gemacht?"*). KG **must** fallback for these — fallback-rate is not a uniform 20 %, it's question-class-conditional.
5. **CI**: nightly cron on Hetzner runs the eval, posts result to a GitHub issue if pass-rate drops below 75 %.
6. **Production telemetry**: aggregate `hit_count / answer_count` per node, weekly report — that's the live ground-truth, eval-set is just the canary.

---

## Open questions

- Embedding provider: OpenAI `text-embedding-3-small` vs. local `bge-small-de` — cost call.
- Should KG also drive `web_search` query rewriting? Likely yes.
- Remove human-accept gate after 200 nodes? Keep it for `priority ≥ 0.8` only.
