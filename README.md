# nobrainr

**Your AI agents forget everything between sessions. nobrainr fixes that.**

Every time you start a new Claude Code session, your agent starts from zero. It doesn't remember what it debugged yesterday, what architecture decisions were made last week, or what patterns it discovered across your projects. You lose hours re-explaining context.

nobrainr is a self-hosted memory service that gives your AI agents persistent, searchable memory across sessions, machines, and projects. Agents store what they learn. Next session — on any machine — they recall it instantly.

### What it actually does

- Agent fixes a tricky Docker networking issue on your laptop? That knowledge is available on your server too.
- Agent discovers a project convention? Every future session starts with that context.
- Import your ChatGPT history? All 2000 conversations become searchable agent memory.
- A knowledge graph builds itself in the background — entities, relationships, and insights extracted automatically.

### How it works

```
┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│  machine A   │     │  machine B   │     │  machine C   │
│  Claude Code │     │  Claude Code │     │  Claude Code  │
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

Fully local. No API keys. No cloud. Your data stays on your hardware. Built on PostgreSQL + pgvector for storage, Ollama for free local embeddings, and MCP (SSE) as the standard interface.

## Quick start

### Docker (recommended)

```bash
git clone https://github.com/vicquick/nobrainr.git
cd nobrainr
cp .env.example .env

# Edit .env — at minimum, set a real POSTGRES_PASSWORD
$EDITOR .env

# Start everything
docker compose up -d

# Wait for Ollama to pull the embedding model (~270MB, first run only)
docker compose logs -f ollama-init

# Verify
curl -sf http://localhost:8420/sse  # Should hang (SSE stream) — Ctrl+C
```

The extraction model (`qwen2.5:7b`, ~4.7GB) is also pulled on first start. If you don't need automatic entity extraction (knowledge graph), set `NOBRAINR_EXTRACTION_ENABLED=false` in `.env` to skip it.

### Local development

```bash
# Start only the infrastructure
docker compose up -d postgres ollama ollama-init

# Run the backend locally
uv sync
uv run nobrainr serve

# Or run the dashboard too
cd dashboard && npm install && npm run dev
```

## Connect Claude Code

Add to `~/.claude/mcp.json`:

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

Add `"nobrainr"` to `enabledMcpjsonServers` and `"mcp__nobrainr"` to `permissions.allow` in `~/.claude/settings.json`.

There's a setup script that automates this:

```bash
# On the machine where Claude Code runs:
NOBRAINR_HOST=<your-server> bash scripts/setup-client.sh
```

## MCP Tools

| Tool | What it does |
|------|-------------|
| `memory_store` | Save a memory (auto-embeds, dedup check, async entity extraction) |
| `memory_search` | Semantic search with natural language (relevance-ranked) |
| `memory_query` | Filter by tags, category, machine, source |
| `memory_get` | Get one memory by ID |
| `memory_update` | Update a memory (re-embeds if content changes) |
| `memory_delete` | Delete a memory by ID |
| `memory_stats` | Counts by category, machine, source, top tags + knowledge graph stats |
| `entity_search` | Semantic search on knowledge graph entities |
| `entity_graph` | Recursive graph traversal from a named entity |
| `memory_maintenance` | Recompute importance scores + decay stability |
| `memory_extract` | Manually trigger entity extraction for a memory |
| `memory_feedback` | Report whether search results were helpful (improves ranking) |
| `memory_reflect` | Batch-save learnings from a session |
| `log_event` | Record agent activity (session starts, decisions, completions) |
| `memory_import_chatgpt` | Import from ChatGPT export |
| `memory_import_claude` | Import from `.claude/` directory |

### Example calls

```
# Store
memory_store(content="Traefik needs container DNS names, not IPs",
             tags=["traefik", "docker"], category="gotchas",
             source_machine="my-server")

# Search
memory_search(query="how did we fix the Docker networking issue")

# Filter
memory_query(source_machine="my-laptop", category="architecture", limit=20)
```

## Autonomous Learning

nobrainr runs background scheduler jobs that continuously improve the knowledge base:

| Job | Interval | What it does |
|-----|----------|-------------|
| Maintenance | 6h | Recompute importance scores, decay stale memories |
| Summarize | 4h | Auto-summarize memories that lack summaries |
| Consolidation | 8h | Merge near-duplicate memories (cosine > 0.88) |
| Synthesis | 24h | Generate insights from entity clusters |
| Entity enrichment | 12h | Improve entity descriptions |
| Insight extraction | 6h | Extract learnings from agent events |
| ChatGPT distillation | 30min | Distill imported ChatGPT conversations into memories |

All jobs are configurable via environment variables. See `.env.example`.

These jobs require an Ollama model with structured output support. On CPU-only servers, expect ~60-120s per LLM call — the scheduler handles this with sequential processing and cooldowns.

## Hooks & Skills (optional)

The `scripts/` directory contains Claude Code integrations:

**Auto-load on session start** — A hook queries nobrainr for relevant memories and injects them as startup context.

**Auto-save on session end** — A hook detects substantial code changes and stores a session summary.

**`/remember`** — Slash command that reviews the session and stores key insights.

**`/recall <query>`** — Slash command that searches memories.

Run `scripts/setup-client.sh` on each machine to install these automatically.

## Stack

| Component | Version | Purpose |
|-----------|---------|---------|
| PostgreSQL | 18 | Storage (UUIDv7 native) |
| pgvector | HNSW index | Similarity search |
| Ollama | nomic-embed-text | Local embeddings (768d, free, no API costs) |
| Ollama | qwen2.5:7b | Entity extraction + autonomous learning (optional) |
| FastMCP | SSE transport | MCP server |
| Python | 3.12+ | Runtime |
| Vue 3 | Vuetify + Cytoscape.js | Dashboard (optional, separate container) |

## Configuration

All via environment variables with `NOBRAINR_` prefix:

| Variable | Default | Description |
|----------|---------|-------------|
| `NOBRAINR_DATABASE_URL` | `postgresql://nobrainr:nobrainr@localhost:5432/nobrainr` | PostgreSQL connection |
| `NOBRAINR_OLLAMA_URL` | `http://localhost:11434` | Ollama API endpoint |
| `NOBRAINR_EMBEDDING_MODEL` | `nomic-embed-text` | Ollama model for embeddings |
| `NOBRAINR_HOST` | `0.0.0.0` | Server bind address |
| `NOBRAINR_PORT` | `8420` | Server port |
| `NOBRAINR_EXTRACTION_ENABLED` | `true` | Enable entity extraction (knowledge graph) |
| `NOBRAINR_EXTRACTION_MODEL` | `qwen2.5:7b` | Ollama model for extraction |
| `NOBRAINR_SOURCE_MACHINE` | `<hostname>` | Machine name for scheduler-created memories |
| `NOBRAINR_SCHEDULER_ENABLED` | `true` | Enable background scheduler jobs |

See `.env.example` for the full list including scheduler intervals.

## Project layout

```
src/nobrainr/
├── mcp/server.py          # MCP tools (the API)
├── db/
│   ├── queries.py         # All database operations
│   ├── schema.py          # DDL (auto-creates tables on startup)
│   └── pool.py            # asyncpg connection pool
├── embeddings/ollama.py   # Embedding client
├── extraction/
│   ├── extractor.py       # Entity/relationship extraction via Ollama
│   ├── pipeline.py        # Full pipeline: extract → dedup → store → link
│   ├── dedup.py           # Memory dedup (vector + LLM merge)
│   ├── llm.py             # Shared Ollama chat helper
│   └── models.py          # Pydantic models for extraction
├── dashboard/
│   ├── app.py             # ASGI app with lifespan
│   └── api.py             # JSON API endpoints
├── importers/
│   ├── chatgpt.py         # ChatGPT export parser
│   └── claude.py          # Claude memory scanner
├── scheduler.py           # APScheduler setup
├── scheduler_jobs.py      # Autonomous learning jobs
├── config.py              # Pydantic settings
└── cli.py                 # CLI: serve, status, search, import

dashboard/                  # Vue 3 frontend (optional, separate build)
├── Dockerfile
├── nginx.conf
├── src/
│   ├── views/             # Graph, Memories, Timeline, Scheduler
│   ├── composables/       # Vue composables for each view
│   └── components/        # Reusable UI components
└── ...
```

## Deployment

### Plain Docker

The included `docker-compose.yml` is self-contained. It runs PostgreSQL, Ollama, and the nobrainr server. Just `docker compose up -d`.

For the dashboard, build and run it separately:

```bash
cd dashboard
docker build -t nobrainr-dashboard .
docker run -d -p 3000:80 nobrainr-dashboard
```

### Behind a reverse proxy

nobrainr serves both the MCP SSE endpoint and a JSON API on the same port (default 8420). If you put it behind a reverse proxy (nginx, Traefik, Caddy):

- Route `/sse` and `/messages/*` to the backend (SSE — don't buffer these)
- Route `/api/*` to the backend (regular HTTP)
- Route everything else to the dashboard (static files)

Example nginx snippet:

```nginx
location /sse {
    proxy_pass http://localhost:8420;
    proxy_http_version 1.1;
    proxy_set_header Connection '';
    proxy_buffering off;
    proxy_cache off;
}

location /api/ {
    proxy_pass http://localhost:8420;
}

location /messages/ {
    proxy_pass http://localhost:8420;
}

location / {
    proxy_pass http://localhost:3000;  # dashboard
}
```

### With Coolify

nobrainr works well with [Coolify](https://coolify.io/) — connect your Git repo, set the environment variables, and deploy. The `Dockerfile` and `dashboard/Dockerfile` are ready to use.

### Backups

The PostgreSQL volume contains all your memories. Back it up regularly:

```bash
docker exec nobrainr-db pg_dump -U nobrainr nobrainr | gzip > nobrainr-backup-$(date +%Y%m%d).sql.gz
```

To restore:

```bash
gunzip -c nobrainr-backup-20260306.sql.gz | docker exec -i nobrainr-db psql -U nobrainr nobrainr
```

## CLI

```bash
nobrainr serve              # Start MCP server
nobrainr status             # Check DB + embedding model
nobrainr search "query"     # Semantic search from terminal
nobrainr import-chatgpt conversations.json
nobrainr import-claude ~/.claude --machine my-laptop
```

## License

[MIT](LICENSE)
