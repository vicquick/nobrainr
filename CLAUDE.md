# nobrainr — Collective Agent Memory Service v2

## What This Is
A shared memory service with knowledge graph for all Claude instances across the VPN (10.0.0.0/24).
Provides relevance-ranked semantic search, automatic entity extraction, on-write dedup,
and a web dashboard with interactive graph visualization.

## Architecture
- **PostgreSQL 18 + pgvector** — storage, vector similarity, knowledge graph (entities + relations)
- **Ollama + nomic-embed-text** — local embeddings (768 dimensions)
- **Ollama + qwen2.5:7b** — entity/relationship extraction (structured output)
- **Python ASGI app** — FastMCP SSE + Starlette dashboard + HTMX + Cytoscape.js
- **Deployed via Coolify** on myserver (10.0.0.2)
- **Dashboard** at mcp.example.com (VPN-only via Traefik ipAllowList)

## Project Layout
```
src/nobrainr/
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
│   ├── app.py             # Parent ASSI app: create_app(), lifespan
│   ├── routes.py          # Page handlers (Jinja2 → HTMLResponse)
│   ├── api.py             # JSON + HTMX fragment endpoints
│   ├── auth.py            # BasicAuthMiddleware
│   ├── templates/         # Jinja2 templates (base, graph, timeline, memories, partials)
│   └── static/            # CSS + vendored JS (HTMX, Cytoscape.js) + custom JS
├── mcp/
│   └── server.py          # FastMCP server with 12 MCP tools
└── importers/
    ├── chatgpt.py         # ChatGPT conversations.json parser
    └── claude.py          # Claude .claude/ directory scanner
```

## MCP Tools (12 total)
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

## Database Tables
- **memories** — core knowledge entries + v2 columns (last_accessed_at, access_count, stability, importance, extraction_status)
- **entities** — knowledge graph nodes (name, type, canonical_name, embedding, mention_count)
- **entity_memories** — junction linking entities to memories (role, confidence)
- **entity_relations** — knowledge graph edges (source, target, relationship_type, confidence)
- **conversations_raw** — imported conversation archives
- **agent_events** — activity log
- **memory_outcomes** — feedback tracking

## Key Commands
```bash
nobrainr serve                           # Start server (MCP + dashboard)
nobrainr status                          # Check DB + models + graph stats
nobrainr search "query"                  # CLI semantic search (relevance-ranked)
nobrainr extract-backfill --batch-size 5 # Entity extraction on all unprocessed memories
nobrainr entities --type technology      # List extracted entities
nobrainr import-chatgpt conversations.json --distill
nobrainr import-claude /root/.claude --machine myserver
```

## Dashboard URLs
- `/dashboard` — Knowledge graph visualization (Cytoscape.js)
- `/memories` — Memory browser with HTMX search
- `/timeline` — Timeline view of memories

## API Endpoints
- `/api/graph` — Full graph data (Cytoscape elements format)
- `/api/memories` — Search/list (HTMX or JSON)
- `/api/memories/{id}` — Detail, update, delete
- `/api/timeline` — Memories by date
- `/api/node/{id}` — Entity detail + connections
- `/api/stats` — Statistics
- `/api/categories`, `/api/tags` — Filter values

## Client Connection
```json
{
  "mcpServers": {
    "nobrainr": {
      "type": "sse",
      "url": "http://10.0.0.2:8420/sse"
    }
  }
}
```

## Development
```bash
uv sync                     # Install deps
uv run nobrainr status      # Test connection
uv run nobrainr serve       # Run locally
uv run pytest               # Run tests
```
