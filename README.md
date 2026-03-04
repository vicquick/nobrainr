# nobrainr

Shared memory for AI agents. Store learnings, search them later, across machines and sessions.

PostgreSQL + pgvector for storage, Ollama for local embeddings, MCP (SSE) for the interface. Every Claude Code instance on your network gets the same memory.

## How it works

```
┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│  workserver   │     │   myserver     │     │  any machine  │
│  Claude Code  │     │  Claude Code │     │  Claude Code  │
└──────┬───────┘     └──────┬───────┘     └──────┬───────┘
       │    MCP (SSE)       │                     │
       └────────────────────┼─────────────────────┘
                            │
                    ┌───────▼────────┐
                    │   nobrainr     │
                    │   :8420/sse    │
                    ├────────────────┤
                    │ PG18+pgvector  │
                    │ Ollama embeddings
                    └────────────────┘
```

Agents store what they learn. Next session, any agent on the network can recall it.

## Quick start

```bash
# Clone and start everything
git clone https://github.com/youruser/nobrainr.git
cd nobrainr
docker compose up -d

# Wait for Ollama to pull the embedding model (~270MB, first run only)
docker logs -f nobrainr-ollama-init

# Verify
curl -sf http://localhost:8420/sse  # Should hang (SSE stream) — Ctrl+C
```

## Connect Claude Code

Add to `~/.claude/mcp.json`:

```json
{
  "mcpServers": {
    "nobrainr": {
      "type": "sse",
      "url": "http://<host>:8420/sse"
    }
  }
}
```

Add `"nobrainr"` to `enabledMcpjsonServers` and `"mcp__nobrainr"` to `permissions.allow` in `~/.claude/settings.json`.

## MCP Tools

| Tool | What it does |
|------|-------------|
| `memory_store` | Save a memory (auto-embeds) |
| `memory_search` | Semantic search with natural language |
| `memory_query` | Filter by tags, category, machine, source |
| `memory_get` | Get one memory by ID |
| `memory_update` | Update a memory (re-embeds if content changes) |
| `memory_delete` | Delete a memory by ID |
| `memory_stats` | Counts by category, machine, source, top tags |
| `memory_import_chatgpt` | Import from ChatGPT export |
| `memory_import_claude` | Import from `.claude/` directory |

### Example calls

```
# Store
memory_store(content="Traefik needs container DNS names, not IPs",
             tags=["traefik", "docker"], category="gotchas",
             source_machine="myserver")

# Search
memory_search(query="how did we fix the Docker networking issue")

# Filter
memory_query(source_machine="workserver", category="architecture", limit=20)
```

## Hooks & Skills (optional)

The `scripts/` and `commands/` directories in this repo contain Claude Code integrations:

**Auto-load on session start** — A hook queries nobrainr for relevant memories based on your current project directory and injects them as startup context.

**Auto-remind on session end** — A hook detects if you made substantial code changes and reminds you to save learnings.

**`/remember`** — Slash command that reviews the session and stores key insights.

**`/recall <query>`** — Slash command that searches memories. No argument = show recent.

See [`CLAUDE.md`](CLAUDE.md) for installation details.

## Stack

| Component | Version | Purpose |
|-----------|---------|---------|
| PostgreSQL | 18 | Storage (UUIDv7 native) |
| pgvector | HNSW index | Similarity search |
| Ollama | nomic-embed-text | Local embeddings (768d, free) |
| FastMCP | SSE transport | MCP server |
| Python | 3.12+ | Runtime |

## Configuration

All via environment variables with `NOBRAINR_` prefix:

| Variable | Default | Description |
|----------|---------|-------------|
| `NOBRAINR_DATABASE_URL` | `postgresql://nobrainr:nobrainr@localhost:5432/nobrainr` | PostgreSQL connection |
| `NOBRAINR_OLLAMA_URL` | `http://localhost:11434` | Ollama API endpoint |
| `NOBRAINR_EMBEDDING_MODEL` | `nomic-embed-text` | Ollama model for embeddings |
| `NOBRAINR_HOST` | `0.0.0.0` | Server bind address |
| `NOBRAINR_PORT` | `8420` | Server port |

## Project layout

```
src/nobrainr/
├── mcp/server.py      # MCP tools (the API)
├── db/queries.py      # All database operations
├── db/schema.py       # DDL (auto-creates tables)
├── db/pool.py         # asyncpg connection pool
├── embeddings/ollama.py  # Embedding client
├── importers/         # ChatGPT + Claude importers
├── config.py          # Pydantic settings
└── cli.py             # CLI: serve, status, search
```

## CLI

```bash
nobrainr serve              # Start MCP server
nobrainr status             # Check DB + embedding model
nobrainr search "query"     # Semantic search from terminal
nobrainr import-chatgpt conversations.json --distill
nobrainr import-claude ~/.claude --machine myhost
```

## Development

```bash
uv sync
uv run nobrainr status
uv run pytest
uv run ruff check src/
```

## License

MIT
