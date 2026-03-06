# Autonomous Learning

nobrainr runs background scheduler jobs that continuously improve the knowledge base without manual intervention.

## Jobs

| Job | Default Interval | Type | Description |
|-----|-----------------|------|-------------|
| **Maintenance** | 6h | SQL | Recompute importance scores, decay stability for stale memories |
| **Feedback integration** | 12h | SQL | Integrate agent feedback into memory scoring |
| **Summarize** | 4h | LLM | Auto-generate summaries for memories that lack them |
| **Consolidation** | 8h | LLM | Merge near-duplicate memories (cosine similarity > 0.88) |
| **Synthesis** | 24h | LLM | Generate cross-cutting insights from entity clusters |
| **Entity enrichment** | 12h | LLM | Improve entity descriptions using related memories |
| **Insight extraction** | 6h | LLM | Extract learnings from patterns in agent events |
| **ChatGPT distillation** | 30min | LLM | Distill imported ChatGPT conversations into structured memories |

## How it works

1. **SQL jobs** (maintenance, feedback) are lightweight and run on any hardware
2. **LLM jobs** require Ollama with a model that supports structured output (e.g., `qwen2.5:7b`)
3. Jobs run sequentially with cooldowns to prevent resource contention
4. Each job logs its activity as an agent event, visible in the dashboard scheduler view

## Hardware considerations

!!! tip "CPU-only servers"
    On CPU-only hardware, LLM calls take ~60-120s each. The scheduler handles this gracefully with sequential processing. Expect ~15 operations per hour.

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
