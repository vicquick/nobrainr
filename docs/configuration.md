# Configuration

All settings are configured via environment variables with the `NOBRAINR_` prefix.

## Core settings

| Variable | Default | Description |
|----------|---------|-------------|
| `NOBRAINR_DATABASE_URL` | `postgresql://nobrainr:nobrainr@localhost:5432/nobrainr` | PostgreSQL connection string |
| `NOBRAINR_OLLAMA_URL` | `http://localhost:11434` | Ollama API endpoint |
| `NOBRAINR_EMBEDDING_MODEL` | `nomic-embed-text` | Ollama model for embeddings |
| `NOBRAINR_EMBEDDING_DIMENSIONS` | `768` | Embedding vector dimensions |
| `NOBRAINR_HOST` | `0.0.0.0` | Server bind address |
| `NOBRAINR_PORT` | `8420` | Server port |

## Security

| Variable | Default | Description |
|----------|---------|-------------|
| `NOBRAINR_CORS_ORIGINS` | `["*"]` | Allowed CORS origins |
| `NOBRAINR_MAX_CONTENT_LENGTH` | `50000` | Max memory content size in bytes |

## Extraction (knowledge graph)

| Variable | Default | Description |
|----------|---------|-------------|
| `NOBRAINR_EXTRACTION_ENABLED` | `true` | Enable automatic entity extraction |
| `NOBRAINR_EXTRACTION_MODEL` | `qwen3:8b` | Ollama model for extraction |

!!! tip
    On CPU-only servers, extraction takes ~60-120s per memory. Set `NOBRAINR_EXTRACTION_ENABLED=false` if you don't need the knowledge graph, or if your server doesn't have enough RAM for the extraction model.

## Scheduler

| Variable | Default | Description |
|----------|---------|-------------|
| `NOBRAINR_SCHEDULER_ENABLED` | `true` | Enable background learning jobs |
| `NOBRAINR_SOURCE_MACHINE` | `<hostname>` | Machine name for scheduler-created memories |
| `NOBRAINR_SCHEDULER_LLM_MODEL` | `qwen3:8b` | Model for scheduler LLM jobs |

### Job intervals

| Variable | Default | Description |
|----------|---------|-------------|
| `NOBRAINR_MAINTENANCE_INTERVAL_HOURS` | `6.0` | Importance recomputation + stability decay |
| `NOBRAINR_FEEDBACK_INTERVAL_HOURS` | `12.0` | Integrate feedback into memory scores |
| `NOBRAINR_SUMMARIZE_INTERVAL_HOURS` | `1.0` | Auto-summarize unsummarized memories |
| `NOBRAINR_CONSOLIDATION_INTERVAL_HOURS` | `2.0` | Merge near-duplicate memories |
| `NOBRAINR_SYNTHESIS_INTERVAL_HOURS` | `4.0` | Generate insights from entity clusters |
| `NOBRAINR_ENTITY_ENRICHMENT_INTERVAL_HOURS` | `2.0` | Improve entity descriptions |
| `NOBRAINR_INSIGHT_EXTRACTION_INTERVAL_HOURS` | `1.0` | Extract learnings from agent events |
| `NOBRAINR_CHATGPT_DISTILL_INTERVAL_HOURS` | `0.1` | Distill imported ChatGPT conversations |
| `NOBRAINR_CHATGPT_DISTILL_MODEL` | `qwen3:8b` | Model for ChatGPT distillation |
| `NOBRAINR_CONTRADICTION_INTERVAL_HOURS` | `4.0` | Detect contradicting memories |
| `NOBRAINR_CROSS_MACHINE_INTERVAL_HOURS` | `6.0` | Discover patterns across machines |
| `NOBRAINR_QUALITY_INTERVAL_HOURS` | `4.0` | Validate entity extractions |
| `NOBRAINR_DECAY_INTERVAL_HOURS` | `24.0` | Archive low-value, never-accessed memories |

## Search defaults

| Variable | Default | Description |
|----------|---------|-------------|
| `NOBRAINR_DEFAULT_SEARCH_LIMIT` | `10` | Default result limit for searches |
| `NOBRAINR_DEFAULT_SIMILARITY_THRESHOLD` | `0.3` | Minimum similarity for search results |

## Example `.env`

See [`.env.example`](https://github.com/youruser/nobrainr/blob/main/.env.example) for a complete template.
