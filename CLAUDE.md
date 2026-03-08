# nobrainr — Collective Agent Memory Service

## What This Is
Persistent shared memory for AI coding agents. If you're an agent reading this:
you can store learnings with `memory_store`, search past knowledge with `memory_search`,
and explore the knowledge graph with `entity_search` / `entity_graph`. Everything you
store is available to every other agent instance connected to this server.

Provides relevance-ranked semantic + hybrid (RRF) search, context-enriched embeddings,
automatic entity extraction, on-write dedup, and a Vue 3 dashboard with interactive
graph visualization.

### Canonical Categories
Use one of these when storing memories — freeform categories are auto-normalized:
`architecture`, `debugging`, `deployment`, `infrastructure`, `patterns`, `tooling`,
`security`, `frontend`, `backend`, `data`, `business`, `documentation`, `session-log`,
`insight`, `_archived`

The system learns autonomously: it summarizes, consolidates duplicates, synthesizes
cross-entity insights, detects contradictions, validates its own extractions, discovers
cross-machine patterns, and archives stale knowledge — all on scheduled LLM-powered jobs.

## Architecture
- **Backend** — Python ASGI: FastMCP (HTTP + SSE) + pure JSON API (Starlette)
- **Frontend** — Vue 3 + Vuetify + Cytoscape.js (separate container, nginx)
- **PostgreSQL 18 + pgvector** — storage, vector similarity, knowledge graph
- **Ollama + nomic-embed-text** — local embeddings (768 dimensions)
- **Ollama + gemma3:12b** — entity/relationship extraction, scheduler jobs (structured output)
- **Crawl4AI** — web crawling service (CPU-only container on `mcp` network, port 11235)

### Routing (when using a reverse proxy)
| Path | Target |
|------|--------|
| `/mcp`, `/api/*`, `/sse`, `/messages/*` | nobrainr backend (port 8420) |
| `/*` (catch-all) | dashboard (nginx, port 80) |

## Project Layout
```
src/nobrainr/              # Python backend
├── config.py              # Settings via env vars (NOBRAINR_ prefix)
├── cli.py                 # CLI: serve, status, search, extract-backfill, entities, imports
├── db/
│   ├── pool.py            # asyncpg connection pool with pgvector
│   ├── schema.py          # DDL: memories, entities, entity_memories, entity_relations, etc.
│   └── queries.py         # All database operations (memory + entity + graph)
├── embeddings/
│   └── ollama.py          # Ollama API client (embed_text, embed_batch)
├── extraction/
│   ├── models.py          # Pydantic: ExtractedEntity, ExtractedRelationship, ExtractionResult
│   ├── extractor.py       # Ollama /api/chat with structured output
│   ├── dedup.py           # Memory dedup (vector + LLM merge decision)
│   ├── llm.py             # Shared Ollama chat helper
│   └── pipeline.py        # Full pipeline: extract → dedup → store → link
├── dashboard/
│   ├── app.py             # Parent ASGI app: create_app(), lifespan
│   └── api.py             # Pure JSON API endpoints
├── mcp/
│   └── server.py          # FastMCP server with all MCP tools
├── scheduler.py           # Asyncio periodic task runner (LLM lock, staggered starts)
├── scheduler_jobs.py      # 9 autonomous learning jobs (see Scheduler Jobs section)
└── importers/
    ├── chatgpt.py         # ChatGPT conversations.json parser
    └── claude.py          # Claude .claude/ directory scanner

dashboard/                  # Vue 3 frontend (separate build)
├── Dockerfile             # node:20-alpine build → nginx:alpine serve
├── nginx.conf             # SPA routing, gzip, cache
├── package.json
├── vite.config.ts
└── src/
    ├── main.ts            # App entry: Vue + Vuetify + Router + Pinia
    ├── App.vue
    ├── api/client.ts      # Axios instance (same-origin)
    ├── plugins/vuetify.ts # Dark theme
    ├── router/index.ts    # /graph, /memories, /timeline, /scheduler
    ├── stores/stats.ts    # Global stats (Pinia)
    ├── types/index.ts     # TypeScript interfaces
    ├── composables/       # useMemories, useGraph, useTimeline, useScheduler
    ├── views/             # GraphView, MemoriesView, TimelineView, SchedulerView
    └── components/        # AppBar, MemoryCard, MemoryDetail, EntityBadge, GraphSidePanel
```

## API Endpoints (pure JSON)
- `/api/graph` — Full graph data (Cytoscape elements format)
- `/api/memories` — Search/list memories (query: q, category, source_machine, tags, limit, offset)
- `/api/memories/{id}` — GET detail (+entities), POST update (JSON body), DELETE
- `/api/timeline` — Memories by date (query: category, source_machine, limit, offset)
- `/api/node/{id}` — Entity detail + connections + related memories
- `/api/stats` — Statistics + feedback
- `/api/scheduler` — Scheduler status + events + feedback
- `/api/entities` — List entities (query: type, limit, offset)
- `/api/recall` — Fast text-only search (PostgreSQL FTS, no embedding)
- `/api/categories`, `/api/tags` — Filter values

## MCP Tools
| Tool | Purpose |
|------|---------|
| `memory_store` | Store memory with auto-embedding, dedup check, async entity extraction |
| `memory_search` | Relevance-ranked semantic search (similarity + recency + importance) |
| `memory_query` | Structured filter (tags, category, source, machine) |
| `memory_get` | Retrieve specific memory by ID (tracks access) |
| `memory_update` | Update memory (re-embeds if content changes) |
| `memory_delete` | Delete a memory |
| `memory_stats` | Database + knowledge graph statistics |
| `entity_search` | Semantic search on knowledge graph entities |
| `entity_graph` | Recursive graph traversal from a named entity |
| `entity_list` | List entities with optional type filter |
| `entity_memories` | Get all memories linked to a specific entity |
| `memory_maintenance` | Recompute importance + decay stability |
| `memory_extract` | Manually trigger entity extraction for a memory |
| `memory_feedback` | Report whether search results were helpful |
| `memory_reflect` | Batch-save learnings from a session |
| `log_event` | Record significant agent activity |
| `memory_import_chatgpt` | Import ChatGPT export JSON |
| `memory_import_claude` | Import Claude memory files |
| `crawl_page` | Crawl a URL and return cleaned markdown content via Crawl4AI |
| `crawl_and_store` | Crawl a URL and store the content as a memory with entity extraction |

## Client Connection

**HTTP transport (recommended — used by Claude Code):**
```json
{
  "mcpServers": {
    "nobrainr": {
      "type": "http",
      "url": "https://<your-domain>/mcp"
    }
  }
}
```

**SSE (legacy, still supported):**
```json
{
  "mcpServers": {
    "nobrainr": {
      "type": "sse",
      "url": "http://<your-server>:8420/sse"
    }
  }
}
```

## Scheduler Jobs

The scheduler runs 18 autonomous jobs (3 SQL + 15 LLM). LLM jobs use a configurable
semaphore (`NOBRAINR_SCHEDULER_LLM_CONCURRENCY`, default 3) with 1s inter-request delay
between batch LLM calls for live request coexistence. LLM retry: 5 attempts with
exponential backoff on empty/malformed JSON responses. Structured labeling jobs use
`think=False` for ~10x speed.

### Knowledge Lifecycle
| Job | Interval | Batch | Type | Purpose |
|-----|----------|-------|------|---------|
| `chatgpt_distill` | 6m | 20 | LLM | Convert raw ChatGPT conversations → structured memories |
| `auto_summarize` | 1h | 20 | LLM | Generate 1-line summaries for unsummarized memories |
| `insight_extraction` | 1h | 30 | LLM | Extract reusable learnings from agent events |
| `consolidation` | 2h | 10 | LLM | Find near-duplicates (>88% similar) and merge via LLM |
| `entity_enrichment` | 2h | 20 | LLM | Generate descriptions for underdescribed entities |
| `synthesis` | 4h | 5 | LLM | Cross-entity insight generation from memory clusters |

### Web Intelligence (NEW — knowledge crawl evolution)
| Job | Interval | Batch | Type | Purpose |
|-----|----------|-------|------|---------|
| `knowledge_crawl` | 6h | 3 | LLM | Crawl seed URLs + queued URLs, store with entity extraction, discover links |
| `entity_web_research` | 12h | 3 | LLM | Find underdescribed entities, LLM suggests docs URL, crawl + store |
| `freshness_recrawl` | 24h | 3 | LLM | Re-crawl stale pages (>30 days), update if content changed |
| `interest_expansion` | 24h | 3 | LLM | Research hot topics based on agent search/crawl interest signals |

### Quality & Integrity
| Job | Interval | Batch | Type | Purpose |
|-----|----------|-------|------|---------|
| `extraction_quality` | 4h | 20 | LLM | Validate entity extractions, update confidence, prune bad links |
| `contradiction_detection` | 4h | 10 | LLM | Find semantically similar memories that contradict each other |
| `cross_machine_insights` | 6h | 5 | LLM | Discover patterns across different machines/agents |

### Maintenance
| Job | Interval | Type | Purpose |
|-----|----------|------|---------|
| `maintenance` | 6h | SQL | Recompute importance scores, decay stability |
| `feedback_integration` | 12h | SQL | Adjust importance based on feedback |
| `memory_decay` | 24h | SQL | Archive low-value, never-accessed memories >30 days old |

### Knowledge Growth Loop
```
Agent sessions → interest signals (search queries, manual crawls)
    ↓
interest_expansion (24h) → targeted web research based on hot topics
    ↓
knowledge_crawl (6h) → crawl seed URLs + queued links
    ↓
Link discovery → queue interesting links from crawled pages
    ↓
entity_web_research (12h) → LLM picks underdescribed entities → suggests docs → crawls
    ↓
freshness_recrawl (24h) → re-crawl stale pages → update if changed
    ↓
Knowledge graph grows → entities enriched → new relations discovered
```

## Deployment Notes

### Ollama Configuration
Two models are required:
- `nomic-embed-text` — embeddings (~0.6 GB VRAM, always loaded)
- `gemma3:12b` — entity extraction + scheduler (~11.4 GB VRAM, keep_alive=5m)

Additionally, `qwen3.5-nothink:9b` is available for external consumers (Affine copilot).

Recommended Ollama env vars for production:
- `OLLAMA_FLASH_ATTENTION=1` — reduces VRAM, speeds inference
- `OLLAMA_KV_CACHE_TYPE=q8_0` — halves KV cache memory per slot
- `OLLAMA_NUM_PARALLEL=6` — concurrent inference slots
- `OLLAMA_KEEP_ALIVE=5m` — unload idle models after 5 minutes
- `OLLAMA_MAX_LOADED_MODELS=2` — embedding + one LLM at a time
- `OLLAMA_NUM_CTX=4096` — context window per slot
- `OLLAMA_NUM_GPU=999` — offload all layers to GPU

`MAX_LOADED_MODELS=2` ensures only one LLM is loaded at a time alongside nomic-embed-text,
preventing VRAM exhaustion when multiple apps share the GPU (nobrainr + Affine + Speaches).

### Extraction Performance
- `ollama_chat()` uses `"think": false` for entity extraction (structured labeling doesn't need reasoning)
- Scheduler jobs (consolidation, synthesis, dedup) keep `think=True` — they benefit from reasoning
- gemma3:12b is ~2x faster than qwen3.5 for structured output and more reliable at producing valid JSON
- Backfill: `nobrainr extract-backfill --batch-size 50`
- Retry logic: 404s from Ollama (model loading contention) are retried 5x with exponential backoff
- LLM retry: 5 attempts with exponential backoff on empty/malformed JSON responses

### Network Aliases (Coolify)
Coolify redeploys create new containers with random suffixes. Traefik routes to stable
hostnames (`nobrainr`, `brain-dashboard`) via Docker network aliases on the `mcp` network.

The `nobrainr-network.service` (systemd) watches `docker events` and auto-applies aliases
when containers restart. No manual post-deploy needed.

```
# Manual fix if needed:
docker network disconnect mcp <container> && docker network connect --alias nobrainr mcp <container>
```

### Crawl4AI Configuration
- Container: `crawl4ai` on `mcp` network, port 11235
- CPU only (no GPU), 4GB RAM, 4 CPUs, `--shm-size=2g` for Chromium
- Connects to Ollama via `http://ollama:11434` for LLM-based extraction
- Env vars: `NOBRAINR_CRAWL4AI_URL=http://crawl4ai:11235`, `NOBRAINR_CRAWL4AI_API_TOKEN`

### TLS Certificates
Uses Traefik `letsencrypt-dns` resolver (Cloudflare DNS challenge) — works behind VPN
where HTTP challenge can't reach port 80. Config: `/data/coolify/proxy/dynamic/nobrainr.yaml`.

## Development
```bash
# Backend
uv sync && uv run nobrainr serve

# Frontend (local dev with proxy to backend)
cd dashboard && npm install && npm run dev

# Lint
uv run ruff check src/

# Test
uv run pytest
```
