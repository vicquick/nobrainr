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

## Extraction (knowledge graph)

| Variable | Default | Description |
|----------|---------|-------------|
| `NOBRAINR_EXTRACTION_ENABLED` | `true` | Enable automatic entity extraction |
| `NOBRAINR_EXTRACTION_MODEL` | `qwen2.5:7b` | Ollama model for extraction |

!!! tip
    On CPU-only servers, extraction takes ~60-120s per memory. Set `NOBRAINR_EXTRACTION_ENABLED=false` if you don't need the knowledge graph, or if your server doesn't have enough RAM for the extraction model.

## Scheduler

| Variable | Default | Description |
|----------|---------|-------------|
| `NOBRAINR_SCHEDULER_ENABLED` | `true` | Enable background learning jobs |
| `NOBRAINR_SOURCE_MACHINE` | `<hostname>` | Machine name for scheduler-created memories |
| `NOBRAINR_SCHEDULER_LLM_MODEL` | `qwen2.5:7b` | Model for scheduler LLM jobs |

### Job intervals

| Variable | Default | Description |
|----------|---------|-------------|
| `NOBRAINR_MAINTENANCE_INTERVAL_HOURS` | `6.0` | Importance recomputation + stability decay |
| `NOBRAINR_FEEDBACK_INTERVAL_HOURS` | `12.0` | Integrate feedback into memory scores |
| `NOBRAINR_SUMMARIZE_INTERVAL_HOURS` | `4.0` | Auto-summarize unsummarized memories |
| `NOBRAINR_CONSOLIDATION_INTERVAL_HOURS` | `8.0` | Merge near-duplicate memories |
| `NOBRAINR_SYNTHESIS_INTERVAL_HOURS` | `24.0` | Generate insights from entity clusters |
| `NOBRAINR_ENTITY_ENRICHMENT_INTERVAL_HOURS` | `12.0` | Improve entity descriptions |
| `NOBRAINR_INSIGHT_EXTRACTION_INTERVAL_HOURS` | `6.0` | Extract learnings from agent events |
| `NOBRAINR_CHATGPT_DISTILL_INTERVAL_HOURS` | `0.5` | Distill imported ChatGPT conversations |

## Search defaults

| Variable | Default | Description |
|----------|---------|-------------|
| `NOBRAINR_DEFAULT_SEARCH_LIMIT` | `10` | Default result limit for searches |
| `NOBRAINR_DEFAULT_SIMILARITY_THRESHOLD` | `0.3` | Minimum similarity for search results |

## Example `.env`

See [`.env.example`](https://github.com/vicquick/nobrainr/blob/main/.env.example) for a complete template.
