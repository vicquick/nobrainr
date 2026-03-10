# nobrainr — Collective Agent Memory Service

## What This Is
Persistent shared memory for AI coding agents. If you're an agent reading this:
you can store learnings with `memory_store`, search past knowledge with `memory_search`,
and explore the knowledge graph with `entity_search` / `entity_graph`. Everything you
store is available to every other agent instance connected to this server.

Provides hybrid search (vector + full-text RRF) by default, context-enriched embeddings,
chunked document ingestion with overlapping context, optional cross-encoder reranking,
chunk-aware retrieval, automatic entity extraction, on-write dedup, embedding versioning,
and a Vue 3 dashboard with interactive graph visualization.

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
- **Ollama + snowflake-arctic-embed2** — local embeddings (1024 dimensions, 8K context, Matryoshka MRL)
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
├── services/
│   ├── memory.py          # store_memory_with_extraction(), store_document_chunked()
│   ├── chunking.py        # Text chunking with configurable overlap
│   └── reranker.py        # Cross-encoder reranking via flashrank (optional)
├── embeddings/
│   └── ollama.py          # Ollama API client (embed_text, embed_batch)
├── extraction/
│   ├── models.py          # Pydantic: ExtractedEntity, ExtractedRelationship, ExtractionResult
│   ├── extractor.py       # Ollama /api/chat with structured output
│   ├── dedup.py           # Memory dedup (vector + LLM merge decision)
│   ├── llm.py             # Shared Ollama chat helper
│   └── pipeline.py        # Full pipeline: extract → dedup → store → link
├── crawler/
│   ├── client.py          # Shared Crawl4AI HTTP client (crawl4ai_request, crawl4ai_job, crawl4ai_deep, discover_sitemap_urls, bm25_markdown_generator)
│   └── knowledge.py       # Scheduled knowledge crawler (seed URLs, link discovery, freshness)
├── dashboard/
│   ├── app.py             # Parent ASGI app: create_app(), lifespan
│   └── api.py             # Pure JSON API endpoints
├── mcp/
│   └── server.py          # FastMCP server with all MCP tools
├── scheduler.py           # Asyncio periodic task runner (LLM lock, staggered starts)
├── scheduler_jobs.py      # 9 autonomous learning jobs (see Scheduler Jobs section)
└── importers/
    ├── chatgpt.py         # ChatGPT conversations.json parser
    ├── claude.py          # Claude .claude/ directory scanner
    └── documents.py       # Universal document importer (PDF, images, DOCX, text) with vision OCR

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
- `/api/memories/{id}/history` — GET full version audit trail
- `/api/memories/{id}/restore` — POST restore to a previous version (body: `{"version": N}`)

## MCP Tools
| Tool | Purpose |
|------|---------|
| `memory_store` | Store memory with auto-embedding, dedup check, async entity extraction |
| `memory_store_document` | Store long documents with chunked ingestion (overlapping chunks, document linking) |
| `memory_search` | Hybrid search (vector + FTS via RRF) with chunk-aware retrieval and optional reranking |
| `memory_query` | Structured filter (tags, category, source, machine) |
| `memory_get` | Retrieve specific memory by ID (tracks access) |
| `memory_update` | Update memory (re-embeds if content changes, versioned) |
| `memory_delete` | Delete a memory (snapshots before deletion) |
| `memory_history` | Get full version audit trail for a memory |
| `memory_restore` | Restore a memory to a previous version |
| `memory_stats` | Database + knowledge graph + embedding model + chunk statistics |
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
| `crawl_page` | Crawl a URL with PruningContentFilter (or BM25 query-aware filtering), supports css_selector, screenshot, network capture |
| `crawl_and_store` | Crawl a URL + chunked ingestion with content filtering (no more truncation, full content preserved) |
| `deep_crawl` | Multi-page deep crawl (BFS/DFS) with optional store — crawls linked pages from a starting URL |
| `discover_sitemap` | Discover page URLs from sitemap.xml and robots.txt for targeted crawling |
| `memory_import_documents` | Import documents from a directory (PDF, images, DOCX, markdown) with optional vision OCR |
| `distill` | Compress text via local LLM — extract only essential info, 90-99% token savings |
| `distill_search` | Search memories + auto-compress results into a focused answer |
| `code_index` | Index a codebase by extracting code symbols (functions, classes, methods) via AST |
| `code_search` | Search indexed code symbols by name/kind — symbol-level retrieval without reading files |

## Search & Retrieval Pipeline

```
query → embed(query) → HNSW vector search + FTS keyword search
                          ↓                    ↓
                     vector results         FTS results
                          ↓                    ↓
                     Reciprocal Rank Fusion (RRF, k=60)
                          ↓
                   [optional] cross-encoder reranking (flashrank)
                          ↓
                   [optional] chunk context expansion (fetch adjacent chunks)
                          ↓
                     final results (with similarity, relevance, rrf_score)
```

- **Hybrid search** is on by default — every `memory_search` combines vector similarity + full-text matching
- **Embedding model safeguard** — search only matches memories embedded with the current model (prevents garbage from mixed embeddings during migration)
- **Context-enriched embeddings** — memories are embedded as `{category}. {tags}. {content}`, not raw text
- **Chunk-aware retrieval** — when a search hit is part of a chunked document, adjacent chunks are automatically fetched for context continuity

### Chunked Document Ingestion

Long documents (>4000 chars) are automatically split into overlapping chunks:
- **Max chunk size:** 3000 chars (configurable: `NOBRAINR_CHUNK_MAX_CHARS`)
- **Overlap:** 300 chars between consecutive chunks (`NOBRAINR_CHUNK_OVERLAP_CHARS`)
- **Splitting:** Prefers paragraph → line → sentence boundaries
- **Linking:** All chunks share a `document_id` in metadata with `chunk_index` and `chunk_total`
- **Dedup skipped:** Individual chunks skip the write-path dedup check

Used by: `memory_store_document`, `crawl_and_store`, `memory_import_documents`, knowledge crawler.

### Embedding Versioning

Each memory and entity tracks which embedding model generated its vector in the `embedding_model` column.
This enables safe model migration via `nobrainr re-embed --model <new-model>`.

### Reranking (optional)

When `NOBRAINR_RERANKER_ENABLED=true`, search overfetches 3x results and reranks with a cross-encoder
(flashrank, ONNX, CPU-only, ~100ms for 30 docs). Install with `pip install nobrainr[reranker]`.

## Memory Versioning (Audit Trail)

Every memory mutation is tracked in the `memory_versions` table. This provides:
- **Full audit trail**: who changed what, when, and why
- **Point-in-time restore**: revert any memory to a previous version
- **Corruption recovery**: if an LLM-driven job corrupts a memory, the pre-mutation state is always recoverable

### What Gets Versioned
| Mutation | change_type | changed_by |
|----------|-------------|------------|
| New memory created | `created` | `system` |
| Dedup merges new info into existing | `dedup_update` | `mcp` |
| Dedup replaces outdated memory | `dedup_supersede` | `mcp` |
| Manual update via MCP tool | `manual_update` | `mcp` |
| Manual delete via MCP tool | `manual_delete` | `mcp` |
| Scheduler consolidation merge | `consolidation` | `scheduler:consolidation` |
| Scheduler auto-summarize | `auto_summarize` | `scheduler:auto_summarize` |
| Restore to previous version | `restore` | `manual` |

### How It Works
- **Snapshot-on-write**: Before any mutation, the memory's current state (content, tags, category, confidence, metadata) is captured
- **Version numbers**: Sequential per-memory (0 = creation, 1 = first change, etc.)
- **Provenance**: `source_memory_id` and `similarity_score` track dedup/consolidation origins
- **No data loss**: Even deleted memories retain their version history

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

The scheduler runs 22 autonomous jobs (3 SQL + 2 system + 17 LLM). LLM jobs use a configurable
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

### Self-Improvement (inspired by autoresearch + OpusDelta)
| Job | Interval | Type | Purpose |
|-----|----------|------|---------|
| `system_pulse` | 24h | LLM | Generate daily health transmission — memory stats, growth, search quality, anomalies |
| `auto_optimize` | 12h | LLM | Analyze search feedback patterns, suggest improvements, store optimization insights |

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
- `snowflake-arctic-embed2` — embeddings (1024d, ~1.2 GB VRAM, always loaded)
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

`MAX_LOADED_MODELS=2` ensures only one LLM is loaded at a time alongside snowflake-arctic-embed2,
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
- Container: `crawl4ai` on `mcp` network, port 11235 (v0.8.0)
- CPU only (no GPU), 4GB RAM, 4 CPUs, `--shm-size=2g` for Chromium
- Connects to Ollama via `http://ollama:11434` for LLM-based extraction
- Env vars: `NOBRAINR_CRAWL4AI_URL=http://crawl4ai:11235`, `NOBRAINR_CRAWL4AI_API_TOKEN`
- **Shared client**: All crawl operations go through `nobrainr.crawler.client` which applies
  `PruningContentFilter` by default (80% noise reduction) and wraps sync/async/deep crawl APIs
- **API format**: v0.8.x requires `crawler_config` dict wrapping (NOT flat params) to activate
  content filtering. The shared client handles this automatically.
- **Content filters**: PruningContentFilter (default, boilerplate removal) or BM25ContentFilter
  (query-aware, extracts only relevant sections — used by entity_web_research, interest_expansion)
- **Async job API**: Scheduler jobs use POST `/crawl/job` + polling to avoid HTTP timeouts
- **Deep crawl**: BFS/DFS strategies via `crawl4ai_deep()`, exposed as `deep_crawl` MCP tool

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

# Install with reranker support
uv sync --extra reranker
```

## Configuration Reference (Retrieval)

| Env Var | Default | Purpose |
|---------|---------|---------|
| `NOBRAINR_EMBEDDING_MODEL` | `snowflake-arctic-embed2` | Ollama embedding model |
| `NOBRAINR_EMBEDDING_DIMENSIONS` | `1024` | Vector dimensions (must match model) |
| `NOBRAINR_CHUNK_MAX_CHARS` | `3000` | Max characters per chunk |
| `NOBRAINR_CHUNK_OVERLAP_CHARS` | `300` | Overlap between consecutive chunks |
| `NOBRAINR_CHUNK_THRESHOLD` | `4000` | Content above this length gets chunked |
| `NOBRAINR_CHUNK_CONTEXT_WINDOW` | `1` | Adjacent chunks fetched around search hits |
| `NOBRAINR_RERANKER_ENABLED` | `false` | Enable cross-encoder reranking |
| `NOBRAINR_RERANKER_MODEL` | `ms-marco-MiniLM-L-12-v2` | flashrank model name |
