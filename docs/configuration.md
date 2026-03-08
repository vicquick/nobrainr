# Configuration

All settings are configured via environment variables with the `NOBRAINR_` prefix.

## Core

| Variable | Default | Description |
|----------|---------|-------------|
| `NOBRAINR_DATABASE_URL` | `postgresql://nobrainr:nobrainr@localhost:5432/nobrainr` | PostgreSQL connection string |
| `NOBRAINR_OLLAMA_URL` | `http://localhost:11434` | Ollama API endpoint |
| `NOBRAINR_EMBEDDING_MODEL` | `nomic-embed-text` | Ollama model for embeddings |
| `NOBRAINR_EMBEDDING_DIMENSIONS` | `768` | Embedding vector dimensions |
| `NOBRAINR_HOST` | `0.0.0.0` | Server bind address |
| `NOBRAINR_PORT` | `8420` | Server port |
| `NOBRAINR_SOURCE_MACHINE` | *(empty)* | Machine name for scheduler/crawler-created memories. Set this to identify which machine produced a memory (e.g., `my-server`, `workstation`). Falls back to `unknown` if not set. |

## Security

| Variable | Default | Description |
|----------|---------|-------------|
| `NOBRAINR_CORS_ORIGINS` | `["*"]` | Allowed CORS origins |
| `NOBRAINR_MAX_CONTENT_LENGTH` | `50000` | Max memory content size in bytes |

## Extraction (knowledge graph)

| Variable | Default | Description |
|----------|---------|-------------|
| `NOBRAINR_EXTRACTION_ENABLED` | `true` | Enable automatic entity extraction |
| `NOBRAINR_EXTRACTION_MODEL` | `gemma3:12b` | Ollama model for extraction (must support structured output) |

!!! tip
    On CPU-only servers, extraction takes ~60-120s per memory. Set `NOBRAINR_EXTRACTION_ENABLED=false` if you don't need the knowledge graph, or if your server doesn't have enough RAM for the extraction model.

## Chat (RAG)

| Variable | Default | Description |
|----------|---------|-------------|
| `NOBRAINR_CHAT_ENABLED` | `true` | Enable the RAG chatbot endpoint |
| `NOBRAINR_CHAT_MODEL` | *(extraction model)* | Ollama model for chat. Defaults to extraction model if empty. |
| `NOBRAINR_CHAT_MAX_CONTEXT_MEMORIES` | `15` | Max memories injected as chat context |
| `NOBRAINR_CHAT_MAX_SOURCE_MEMORIES` | `50` | Max memories searched for context |
| `NOBRAINR_CHAT_MAX_MESSAGE_LENGTH` | `2000` | Max user message length (characters) |
| `NOBRAINR_CHAT_MAX_HISTORY_LENGTH` | `20` | Max conversation history turns |

## Crawl4AI (web crawling)

| Variable | Default | Description |
|----------|---------|-------------|
| `NOBRAINR_CRAWL4AI_URL` | `http://crawl4ai:11235` | Crawl4AI service URL |
| `NOBRAINR_CRAWL4AI_API_TOKEN` | *(empty)* | API token for Crawl4AI authentication |

## Scheduler

| Variable | Default | Description |
|----------|---------|-------------|
| `NOBRAINR_SCHEDULER_ENABLED` | `true` | Enable background learning jobs |
| `NOBRAINR_SCHEDULER_LLM_CONCURRENCY` | `3` | Max concurrent LLM jobs (leave slots for live requests) |
| `NOBRAINR_SCHEDULER_INTER_REQUEST_DELAY` | `1.0` | Seconds between LLM calls in batch jobs |
| `NOBRAINR_SCHEDULER_LLM_MODEL` | `gemma3:12b` | Model for scheduler LLM jobs |

### Knowledge lifecycle jobs

| Variable | Default | Description |
|----------|---------|-------------|
| `NOBRAINR_CHATGPT_DISTILL_INTERVAL_HOURS` | `0.1` | Distill imported ChatGPT/Claude conversations |
| `NOBRAINR_CHATGPT_DISTILL_BATCH_SIZE` | `20` | Conversations per cycle |
| `NOBRAINR_CHATGPT_DISTILL_MODEL` | `gemma3:12b` | Model for distillation |
| `NOBRAINR_SUMMARIZE_INTERVAL_HOURS` | `1.0` | Auto-summarize unsummarized memories |
| `NOBRAINR_SUMMARIZE_BATCH_SIZE` | `20` | Memories per cycle |
| `NOBRAINR_INSIGHT_EXTRACTION_INTERVAL_HOURS` | `1.0` | Extract learnings from agent events |
| `NOBRAINR_INSIGHT_EXTRACTION_BATCH_SIZE` | `30` | Events per cycle |
| `NOBRAINR_CONSOLIDATION_INTERVAL_HOURS` | `2.0` | Merge near-duplicate memories |
| `NOBRAINR_CONSOLIDATION_BATCH_SIZE` | `10` | Pairs per cycle |
| `NOBRAINR_ENTITY_ENRICHMENT_INTERVAL_HOURS` | `2.0` | Improve entity descriptions |
| `NOBRAINR_ENTITY_ENRICHMENT_BATCH_SIZE` | `20` | Entities per cycle |
| `NOBRAINR_ENTITY_MERGING_INTERVAL_HOURS` | `2.0` | Merge duplicate entities |
| `NOBRAINR_ENTITY_MERGING_BATCH_SIZE` | `15` | Pairs per cycle |
| `NOBRAINR_SYNTHESIS_INTERVAL_HOURS` | `4.0` | Generate insights from entity clusters |
| `NOBRAINR_SYNTHESIS_BATCH_SIZE` | `5` | Clusters per cycle |

### Quality & integrity jobs

| Variable | Default | Description |
|----------|---------|-------------|
| `NOBRAINR_QUALITY_SCORING_INTERVAL_HOURS` | `0.5` | LLM-assessed quality scores (0-1) |
| `NOBRAINR_QUALITY_SCORING_BATCH_SIZE` | `30` | Memories per cycle |
| `NOBRAINR_QUALITY_INTERVAL_HOURS` | `4.0` | Validate entity extractions |
| `NOBRAINR_QUALITY_BATCH_SIZE` | `20` | Memories per cycle |
| `NOBRAINR_CONTRADICTION_INTERVAL_HOURS` | `4.0` | Detect contradicting memories |
| `NOBRAINR_CONTRADICTION_BATCH_SIZE` | `10` | Pairs per cycle |
| `NOBRAINR_CROSS_MACHINE_INTERVAL_HOURS` | `6.0` | Discover patterns across machines |
| `NOBRAINR_CROSS_MACHINE_BATCH_SIZE` | `5` | Patterns per cycle |

### Maintenance jobs (SQL, no LLM)

| Variable | Default | Description |
|----------|---------|-------------|
| `NOBRAINR_MAINTENANCE_INTERVAL_HOURS` | `6.0` | Recompute importance + decay stability |
| `NOBRAINR_FEEDBACK_INTERVAL_HOURS` | `12.0` | Integrate feedback into memory scores |
| `NOBRAINR_DECAY_INTERVAL_HOURS` | `24.0` | Archive low-value, never-accessed memories |
| `NOBRAINR_DECAY_BATCH_SIZE` | `50` | Memories per cycle |

### Web intelligence jobs

| Variable | Default | Description |
|----------|---------|-------------|
| `NOBRAINR_KNOWLEDGE_CRAWL_ENABLED` | `true` | Autonomous web crawling from link queue |
| `NOBRAINR_KNOWLEDGE_CRAWL_INTERVAL_HOURS` | `6.0` | Crawl cycle interval |
| `NOBRAINR_KNOWLEDGE_CRAWL_BATCH_SIZE` | `3` | Pages per cycle |
| `NOBRAINR_KNOWLEDGE_CRAWL_DELAY` | `10.0` | Seconds between requests (politeness) |
| `NOBRAINR_LINK_DISCOVERY_ENABLED` | `true` | Queue interesting links from crawled pages |
| `NOBRAINR_LINK_DISCOVERY_MAX_PER_PAGE` | `5` | Max links to queue per page |
| `NOBRAINR_LINK_DISCOVERY_MIN_SCORE` | `0.4` | Minimum relevance score to queue |
| `NOBRAINR_ENTITY_RESEARCH_ENABLED` | `true` | Targeted crawling for underdescribed entities |
| `NOBRAINR_ENTITY_RESEARCH_INTERVAL_HOURS` | `12.0` | Research cycle interval |
| `NOBRAINR_ENTITY_RESEARCH_BATCH_SIZE` | `3` | Entities per cycle |
| `NOBRAINR_ENTITY_RESEARCH_MIN_MENTIONS` | `5` | Min mentions to trigger research |
| `NOBRAINR_ENTITY_RESEARCH_COOLDOWN_DAYS` | `14` | Don't re-research within N days |
| `NOBRAINR_FRESHNESS_ENABLED` | `true` | Re-crawl stale pages |
| `NOBRAINR_FRESHNESS_INTERVAL_HOURS` | `24.0` | Re-crawl cycle interval |
| `NOBRAINR_FRESHNESS_BATCH_SIZE` | `3` | Pages per cycle |
| `NOBRAINR_FRESHNESS_MAX_AGE_DAYS` | `30` | Re-crawl pages older than N days |
| `NOBRAINR_INTEREST_TRACKING_ENABLED` | `true` | Prioritize crawling by usage signals |
| `NOBRAINR_INTEREST_EXPANSION_INTERVAL_HOURS` | `24.0` | Interest research cycle |
| `NOBRAINR_INTEREST_EXPANSION_BATCH_SIZE` | `3` | Topics per cycle |
| `NOBRAINR_INTEREST_SIGNAL_DECAY_DAYS` | `30` | Signals older than N days lose weight |

## Search defaults

| Variable | Default | Description |
|----------|---------|-------------|
| `NOBRAINR_DEFAULT_SEARCH_LIMIT` | `10` | Default result limit for searches |
| `NOBRAINR_DEFAULT_SIMILARITY_THRESHOLD` | `0.3` | Minimum similarity for search results |

## Example `.env`

See [`.env.example`](https://github.com/youruser/nobrainr/blob/main/.env.example) for a complete template.
