# nobrainr

[![License: MIT](https://img.shields.io/github/license/youruser/nobrainr)](LICENSE)
[![CI](https://img.shields.io/github/actions/workflow/status/youruser/nobrainr/ci.yml?label=CI)](https://github.com/youruser/nobrainr/actions)
[![Python 3.12+](https://img.shields.io/badge/Python-3.12+-green)](https://python.org)
[![MCP](https://img.shields.io/badge/MCP-compatible-blue)](https://modelcontextprotocol.io)

**Your AI agents forget everything between sessions. nobrainr fixes that.**

Every time you start a new Claude Code session, your agent starts from zero. It doesn't remember what it debugged yesterday, what architecture decisions were made last week, or what patterns it discovered across your projects. You lose hours re-explaining context.

nobrainr is a self-hosted memory service that gives your AI agents persistent, searchable memory across sessions, machines, and projects. Agents store what they learn. Next session — on any machine — they recall it instantly.

### What it actually does

- Agent fixes a tricky Docker networking issue on your laptop? That knowledge is available on your server too.
- Agent discovers a project convention? Every future session starts with that context.
- Import your ChatGPT history? All 2000 conversations become searchable agent memory.
- A knowledge graph builds itself in the background — entities, relationships, and insights extracted automatically.

```python
# Agent stores a learning
memory_store(content="pg_dump ignores --schema when used with --table",
             tags=["postgresql", "backup"], category="gotchas")

# Any agent, any machine, any session — finds it instantly
memory_search(query="postgres backup gotcha")
```

### Architecture

```mermaid
graph TB
    subgraph Agents
        A1[Claude Code<br/>Machine A]
        A2[Claude Code<br/>Machine B]
        A3[Cursor / Windsurf<br/>Machine C]
    end

    A1 & A2 & A3 -->|MCP over HTTPS| NB

    subgraph nobrainr [nobrainr server :8420]
        NB[FastMCP + JSON API]
        NB --> EMB[Ollama<br/>nomic-embed-text]
        NB --> EXT[Ollama<br/>qwen3:8b<br/>Entity Extraction]
        NB --> CHAT[Ollama<br/>gemma3:12b<br/>Chatbot + LoRA]
        NB --> PG[(PostgreSQL 18<br/>+ pgvector)]
    end

    subgraph Storage [Knowledge]
        PG --> MEM[Memories<br/>vector similarity]
        PG --> KG[Knowledge Graph<br/>entities + relations]
        PG --> EVT[Events + Feedback]
    end

    NB --> DASH[Vue 3 Dashboard<br/>Graph · Memories · Timeline]

    style nobrainr fill:#1a1a2e,stroke:#16213e,color:#e6e6e6
    style Agents fill:#0d1117,stroke:#30363d,color:#e6e6e6
    style Storage fill:#0d1117,stroke:#30363d,color:#e6e6e6
```

Fully local. No API keys. No cloud. Your data stays on your hardware. Built on PostgreSQL + pgvector for storage, Ollama for free local embeddings, and MCP as the standard interface.

## Quick start

### Docker (recommended)

```bash
git clone https://github.com/youruser/nobrainr.git
cd nobrainr
cp .env.example .env

# Edit .env — at minimum, set a real POSTGRES_PASSWORD
$EDITOR .env

# Start everything
docker compose up -d

# Wait for Ollama to pull the embedding model (~270MB, first run only)
docker compose logs -f ollama-init

# Verify
curl -sf http://localhost:8420/api/stats | jq .total_memories
```

The extraction model (`qwen3:8b`, ~5.2GB) is also pulled on first start. If you don't need automatic entity extraction (knowledge graph), set `NOBRAINR_EXTRACTION_ENABLED=false` in `.env` to skip it.

For chatbot features (support assistant, domain-specific LoRA adapters), `gemma3:12b` (~8-9GB) is recommended. Pull it manually: `docker exec ollama ollama pull gemma3:12b`.

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

## Connect your AI client

Replace `<your-server>` with your nobrainr host IP or domain. For remote access, use a reverse proxy with TLS (see [Deployment — Security](docs/deployment.md#security)).

<details>
<summary><b>Claude Code</b></summary>

Add to `~/.claude/mcp.json`:
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

For local-only access (same machine):
```json
{
  "mcpServers": {
    "nobrainr": {
      "url": "http://localhost:8420/mcp"
    }
  }
}
```

Or follow the [Claude Code setup guide](docs/claude-code-setup.md) for full integration with hooks and scripts.
</details>

<details>
<summary><b>Claude Desktop</b></summary>

Add to `claude_desktop_config.json` (Settings > Developer > Edit Config):
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
</details>

<details>
<summary><b>Cursor</b></summary>

Settings > MCP > Add Server:
- **Type:** HTTP
- **URL:** `https://<your-domain>/mcp`
</details>

<details>
<summary><b>Windsurf / Cline / any MCP client</b></summary>

HTTP (recommended):
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

SSE (legacy, still supported):
```json
{
  "mcpServers": {
    "nobrainr": {
      "type": "sse",
      "url": "https://<your-domain>/sse"
    }
  }
}
```
</details>

> **Security note:** Never expose nobrainr directly to the internet without TLS and access control. Use a reverse proxy with HTTPS, and restrict access via VPN, IP allowlist, or authentication.

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
| Summarize | 1h | Auto-summarize memories that lack summaries |
| Consolidation | 2h | Merge near-duplicate memories (cosine > 0.88) |
| Synthesis | 4h | Generate insights from entity clusters |
| Entity enrichment | 2h | Improve entity descriptions |
| Insight extraction | 1h | Extract learnings from agent events |
| ChatGPT distillation | 6min | Distill imported ChatGPT conversations into memories |
| Contradiction detection | 4h | Find and flag contradicting memories |
| Cross-machine insights | 6h | Discover patterns across machines |
| Extraction quality | 4h | Validate entity extractions, prune bad links |
| Memory decay | 24h | Archive low-value, never-accessed old memories |

All jobs are configurable via environment variables. See `.env.example`.

These jobs require an Ollama model with structured output support. On CPU-only servers, expect ~60-120s per LLM call — the scheduler handles this with sequential processing and cooldowns.

## Hooks & Skills (optional)

The `scripts/` directory contains Claude Code integrations:

**Auto-load on session start** — A hook queries nobrainr for relevant memories and injects them as startup context.

**Auto-save on session end** — A hook detects substantial code changes and stores a session summary.

**`/remember`** — Slash command that reviews the session and stores key insights.

**`/recall <query>`** — Slash command that searches memories.

See the [Claude Code setup guide](docs/claude-code-setup.md) for full setup instructions.

## Stack

| Component | Version | Purpose |
|-----------|---------|---------|
| PostgreSQL | 18 | Storage (UUIDv7 native) |
| pgvector | HNSW index | Similarity search |
| Ollama | nomic-embed-text | Local embeddings (768d, free, no API costs) |
| Ollama | qwen3:8b | Entity extraction + autonomous learning (optional) |
| Ollama | gemma3:12b | Chatbot base model — support + domain LoRA adapters (optional) |
| FastMCP | HTTP + SSE | MCP server |
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
| `NOBRAINR_EXTRACTION_MODEL` | `qwen3:8b` | Ollama model for extraction |
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

### Behind a reverse proxy (recommended for multi-machine)

For accessing nobrainr from multiple machines, put it behind a reverse proxy with TLS. Never expose port 8420 directly — MCP traffic includes memory content in plaintext.

nobrainr serves MCP (HTTP + SSE) and a JSON API on port 8420:

- Route `/mcp` to the backend (HTTP transport — recommended)
- Route `/sse` and `/messages/*` to the backend (SSE transport — legacy, don't buffer)
- Route `/api/*` to the backend
- Route everything else to the dashboard

Restrict access via VPN subnet, IP allowlist, or authentication at the proxy layer. See [Deployment — Security](docs/deployment.md#security) for Traefik and nginx examples with TLS.

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
