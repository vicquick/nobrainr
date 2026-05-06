# Mastra Observational Memory — proposal for nobrainr

**Status:** PROPOSAL · NOT IMPLEMENTED
**Date:** 2026-05-06
**Effort:** L (2-3 days focused)
**Priority:** Medium-low for nobrainr's actual usage pattern (see "Does it apply?" below)

## What it is

Mastra OM ([mastra.ai/research/observational-memory](https://mastra.ai/research/observational-memory),
Feb 2026) is a chat-memory architecture that hits **84.23% on
LongMemEval-S with gpt-4o** and **94.87% with gpt-5-mini** — the current
public SOTA, beating even the oracle baseline by 2 points.

The mechanism:

1. **Observer agent** — runs after each chat turn. Reads the latest
   exchange and writes a single, dense observation (~50-100 tokens):
   *"User wants to migrate from Postgres 13 → 16. They've already drafted
   the pg_upgrade plan but are worried about the trigger O→D rename
   issue. They mentioned this is for the bimavo project."*
2. **Reflector agent** — runs periodically. Reads the observation log
   and consolidates: merges duplicates, lifts long-term facts to a
   stable "core" log, ages out noise.
3. **Retrieval** — at query time, the system pulls **from the
   observation log**, not from raw turns or RAG. The observation log
   is short (~5-10K tokens), stable, and prefix-cacheable.

## Why it wins on LongMemEval

LongMemEval is a **chatbot session memory** benchmark. Questions ask
"what did I tell you in session 3?" across hundreds of sessions. The
correct answer requires perfect recall of one specific message buried
in a haystack.

Traditional RAG: chunks the haystack, embeds, retrieves top-k. Misses
when the question's phrasing diverges from the original message.

Observational Memory: the Observer paraphrases each session into the
log immediately. The query then matches the *paraphrase*, which the
Observer wrote to be queryable. This is why it beats oracle: the
oracle has the raw text, OM has been pre-distilling for retrieval.

## Why it might not apply to nobrainr

**nobrainr is not primarily a chatbot.** It's a knowledge harness:

- 90% of queries are ad-hoc semantic search across ALL of history,
  via MCP from Claude Code or the dashboard
- The "session" concept barely exists — memories are flat by design
- ChatPanel (the actual chat UI in the dashboard) is a thin wrapper
  over MCP search, not a long-running threaded conversation

OM's prompt-cache benefit lands when:

- A single user has a long-running thread (>20 turns)
- The chat client respects Anthropic prompt caching
- The retrieval context grows with each turn

This describes **OpenWebUI as a frontend to nobrainr**, not nobrainr's
core flow. If we ever go all-in on OpenWebUI as the primary UI, OM
becomes high-value. Today, the win is smaller.

## What we already have that overlaps

- **chatgpt_distill** scheduler job — distills bulk-imported conversations
  into "learnings". This is essentially Reflector for historical data.
- **synthesis** scheduler job — produces higher-level insights from
  entity clusters. This is essentially Reflector for the entity graph.
- **community_summaries** — LLM summaries of entity communities, used
  by the GraphRAG branch in retrieval. Overlaps with OM's "core log".

What we don't have:

- **Per-session observation logs** — we have `conversations_raw` and
  per-memory `metadata.conversation_id`, but no live observation log
  that a chat client can read.
- **Cache-friendly retrieval** — every query rebuilds 5 RRF branches
  fresh. No prefix-stable injection.

## Minimal implementation if we choose to build

### New table: `observation_logs`

```sql
CREATE TABLE observation_logs (
    id          uuid DEFAULT uuidv7() PRIMARY KEY,
    thread_id   text NOT NULL,
    body        text NOT NULL,
    embedding   vector(1024),
    created_at  timestamptz DEFAULT now(),
    superseded_by uuid,
    metadata    jsonb DEFAULT '{}'::jsonb
);
CREATE INDEX idx_observation_thread_active
    ON observation_logs(thread_id, created_at DESC)
    WHERE superseded_by IS NULL;
```

### New scheduler job: `observation_consolidate`

Every 30 min: read recent un-consolidated observations per thread, ask
the LLM to merge near-duplicates, mark the originals `superseded_by`
the merged version. Scoped to threads with >5 fresh observations.

### MCP tool: `observation_log(thread_id, message_pair)`

Called by the chat client after each turn. Generates the observation
via a 200-token LLM call (system prompt: "summarize this exchange in
≤80 tokens, focusing on facts the user might ask about later").

### New retrieval mode: `chat_recall(thread_id, query)`

For a chat client mid-conversation:
1. Load the thread's full observation log (always cached — same
   thread_id always returns same prefix)
2. Inject as the first message context, paraphrasing the system
   prompt to make Anthropic see it as a stable cache prefix
3. Append the query
4. The LLM answers from the observation log directly

Falls back to `memory_search` if the observation log is empty (new
thread).

### Frontend integration

Two paths:

- **Quick:** `/api/chat` already exists in the dashboard. Wire
  ChatPanel.vue to call `observation_log` after each turn.
- **Right:** expose `chat_recall` as a new MCP tool. OpenWebUI/Claude
  Code can use it via existing MCP plumbing.

## Concrete cost / benefit

| Metric | Current | With OM |
|--------|---------|---------|
| ChatPanel cold query | 4-7s (full RAG pipeline each turn) | 4-7s first turn, <1s subsequent |
| Anthropic prompt cache hits | 0% (RAG context changes every turn) | 80%+ (observation log stable per thread) |
| Cost per chat turn | ~$0.02 (full context re-embed + RAG) | ~$0.005 (cached prefix, only delta tokens billed) |
| LongMemEval-S recall@1 | TBD (script ready, not run) | Theoretical SOTA per Mastra |
| Multi-turn coherence | Low (every turn fresh-retrieves) | High (observations track user's running mental model) |

## Decision

**Do not build now.** Build:

1. After the LongMemEval-S baseline run is published (so we can measure
   the lift from OM, not just trust the Mastra paper).
2. After we decide whether OpenWebUI becomes the primary chat UI or we
   keep ChatPanel as a thin MCP wrapper.
3. As a single 2-3 day focused session — the design above is enough to
   start; the real cost is the prompt engineering for the Observer and
   Reflector and the dashboard wiring.

## References

- Mastra blog post: https://mastra.ai/research/observational-memory
- Mastra runner code: github.com/mastra-ai/mastra/tree/main/packages/memory/src/processors/observational-memory
- DeepWiki overview: https://deepwiki.com/mastra-ai/mastra/7.9-observational-memory-system
- LongMemEval paper: arXiv:2410.10813
