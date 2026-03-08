# Autonomous Learning

nobrainr runs background scheduler jobs that continuously improve the knowledge base without manual intervention.

## Jobs

| Job | Default Interval | Type | Description |
|-----|-----------------|------|-------------|
| **Maintenance** | 6h | SQL | Recompute importance scores, decay stability for stale memories |
| **Feedback integration** | 12h | SQL | Integrate agent feedback into memory scoring |
| **Memory decay** | 24h | SQL | Archive low-value, never-accessed old memories |
| **Summarize** | 1h | LLM | Auto-generate summaries for memories that lack them |
| **Consolidation** | 2h | LLM | Merge near-duplicate memories (cosine similarity > 0.88) |
| **Synthesis** | 4h | LLM | Generate cross-cutting insights from entity clusters |
| **Entity enrichment** | 2h | LLM | Improve entity descriptions using related memories |
| **Insight extraction** | 1h | LLM | Extract learnings from patterns in agent events |
| **ChatGPT distillation** | 6min | LLM | Distill imported ChatGPT conversations into structured memories |
| **Contradiction detection** | 4h | LLM | Find and flag contradicting memories |
| **Cross-machine insights** | 6h | LLM | Discover patterns across different machines |
| **Extraction quality** | 4h | LLM | Validate entity extractions, prune bad links |

## How it works

1. **SQL jobs** (maintenance, feedback, decay) are lightweight and run on any hardware
2. **LLM jobs** require Ollama with a model that supports structured output (e.g., `qwen3.5:9b`)
3. LLM jobs run with a concurrency limit (3 concurrent) and per-job timeout (30 min)
4. Jobs are staggered on startup to avoid overwhelming the LLM server
5. Each job logs its activity as a scheduler event, visible in the dashboard scheduler view

## Hardware considerations

!!! tip "CPU-only servers"
    On CPU-only hardware, LLM calls take ~60-120s each. The scheduler handles this gracefully with concurrency limits and timeouts. Expect ~15 operations per hour.

!!! tip "GPU servers"
    With a GPU, the same calls drop to ~1-2s. This enables much more aggressive intervals and potentially synchronous extraction on every `memory_store`.

## Configuration

All intervals are configurable via environment variables. See [Configuration](configuration.md#job-intervals).

To disable the scheduler entirely:
```bash
NOBRAINR_SCHEDULER_ENABLED=false
```

To disable only the LLM-dependent jobs (keep SQL maintenance):
```bash
NOBRAINR_EXTRACTION_ENABLED=false
```
