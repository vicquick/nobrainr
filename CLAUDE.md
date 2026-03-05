# nobrainr — Collective Agent Memory Service v3

## What This Is
A shared memory service with knowledge graph for all Claude instances across the VPN (10.10.10.0/24).
Provides relevance-ranked semantic search, automatic entity extraction, on-write dedup,
and a Vue 3 dashboard with interactive graph visualization.

## Architecture
- **Backend** — Python ASGI: FastMCP SSE + pure JSON API (Starlette)
- **Frontend** — Vue 3 + Vuetify + Cytoscape.js (separate Coolify app, nginx)
- **PostgreSQL 18 + pgvector** — storage, vector similarity, knowledge graph
- **Ollama + nomic-embed-text** — local embeddings (768 dimensions)
- **Ollama + qwen2.5:7b** — entity/relationship extraction (structured output)
- **Deployed via Coolify** on bimavo (10.10.10.12)
- **Domain:** mcp.budinic.art (VPN-only via Traefik ipAllowList)

### Routing (Traefik path-based on mcp.budinic.art)
| Path | Target | Priority |
|------|--------|----------|
| `/api/*`, `/sse`, `/messages/*` | nobrainr backend (port 8420) | 100 |
| `/*` (catch-all) | brain-dashboard (nginx, port 80) | 1 |

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
│   └── pipeline.py        # Full pipeline: extract → dedup → store → link
├── dashboard/
│   ├── app.py             # Parent ASGI app: create_app(), lifespan
│   └── api.py             # Pure JSON API endpoints
├── mcp/
│   └── server.py          # FastMCP server with 14 MCP tools
└── importers/
    ├── chatgpt.py         # ChatGPT conversations.json parser
    └── claude.py          # Claude .claude/ directory scanner

dashboard/                  # Vue 3 frontend (separate Coolify app)
├── Dockerfile             # node:20-alpine build → nginx:alpine serve
├── nginx.conf             # SPA routing, gzip, cache
├── package.json
├── vite.config.ts
└── src/
    ├── main.ts            # App entry: Vue + Vuetify + Router + Pinia
    ├── App.vue
    ├── api/client.ts      # Axios instance (same-origin)
    ├── plugins/vuetify.ts # Dark theme (#0d1117, #161b22, #58a6ff)
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
- `/api/categories`, `/api/tags` — Filter values

## MCP Tools (14 total)
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
| `memory_maintenance` | Recompute importance + decay stability |
| `memory_extract` | Manually trigger entity extraction for a memory |
| `memory_import_chatgpt` | Import ChatGPT export JSON |
| `memory_import_claude` | Import Claude memory files |

## Client Connection
```json
{
  "mcpServers": {
    "nobrainr": {
      "type": "sse",
      "url": "http://10.10.10.12:8420/sse"
    }
  }
}
```

## Development
```bash
# Backend
uv sync && uv run nobrainr serve

# Frontend (local dev with proxy to backend)
cd dashboard && npm install && npm run dev
```
