# nobrainr — Collective Agent Memory Service

## What This Is
A shared memory service accessible by all Claude instances across the VPN (10.0.0.0/24).
Provides semantic search over accumulated knowledge from ChatGPT, Claude, and manual entries.

## Architecture
- **PostgreSQL 18 + pgvector** — storage + vector similarity search (HNSW)
- **Ollama + nomic-embed-text** — local embeddings (768 dimensions, free)
- **Python MCP Server (FastMCP)** — SSE transport on port 8420
- **Deployed via Coolify** on myserver (10.0.0.2)

## Project Layout
```
src/nobrainr/
├── config.py              # Settings via env vars (NOBRAINR_ prefix)
├── cli.py                 # CLI: serve, status, search, import-chatgpt, import-claude
├── db/
│   ├── pool.py            # asyncpg connection pool with pgvector
│   ├── schema.py          # DDL: memories, conversations_raw, memory_relations
│   └── queries.py         # All database operations
├── embeddings/
│   └── ollama.py          # Ollama API client (embed_text, embed_batch)
├── mcp/
│   └── server.py          # FastMCP server with all tools
└── importers/
    ├── chatgpt.py         # ChatGPT conversations.json parser
    └── claude.py          # Claude .claude/ directory scanner
```

## MCP Tools
| Tool | Purpose |
|------|---------|
| `memory_store` | Save a new memory with auto-embedding |
| `memory_search` | Semantic search (natural language queries) |
| `memory_query` | Structured filter (tags, category, source, machine) |
| `memory_get` | Retrieve specific memory by ID |
| `memory_update` | Update memory (re-embeds if content changes) |
| `memory_stats` | Database statistics |
| `memory_import_chatgpt` | Import ChatGPT export JSON |
| `memory_import_claude` | Import Claude memory files |

## Database
- Uses UUIDv7 (PG18 native) for time-sortable primary keys
- HNSW index on embeddings for fast approximate nearest neighbor
- GIN indexes on tags and full-text search
- All env vars prefixed with `NOBRAINR_`

## Key Commands
```bash
nobrainr serve              # Start MCP server
nobrainr status             # Check DB + model status
nobrainr search "query"     # CLI semantic search
nobrainr import-chatgpt conversations.json --distill
nobrainr import-claude /root/.claude --machine myserver
```

## Client Connection
Any machine on the VPN adds to its Claude MCP config:
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
uv run pytest               # Run tests
```
