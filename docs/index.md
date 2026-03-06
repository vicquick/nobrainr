# nobrainr

**Persistent memory and knowledge graph for AI agents via MCP.**

Your AI agents forget everything between sessions. nobrainr fixes that.

## What it does

nobrainr is a self-hosted memory service that gives AI coding agents persistent, searchable memory across sessions, machines, and projects. Agents store what they learn — next session, on any machine, they recall it instantly.

- **Cross-session memory** — agents remember past debugging sessions, architecture decisions, and patterns
- **Cross-machine sharing** — knowledge from your laptop is available on your server
- **Knowledge graph** — entities and relationships extracted automatically in the background
- **Import existing knowledge** — bring in your ChatGPT history and Claude memories
- **Autonomous learning** — background jobs consolidate, synthesize, and enrich knowledge over time

## Quick example

```python
# Agent stores a learning
memory_store(
    content="pg_dump ignores --schema when used with --table",
    tags=["postgresql", "backup"],
    category="gotchas"
)

# Any agent, any machine, any session — finds it instantly
memory_search(query="postgres backup gotcha")
```

## How it connects

Any MCP-compatible AI client (Claude Code, Claude Desktop, Cursor, Windsurf) connects via SSE:

```json
{
  "mcpServers": {
    "nobrainr": {
      "type": "sse",
      "url": "http://your-server:8420/sse"
    }
  }
}
```

## Stack

| Component | Purpose |
|-----------|---------|
| PostgreSQL 18 + pgvector | Storage, vector similarity, knowledge graph |
| Ollama + nomic-embed-text | Local embeddings (768d, free, no API keys) |
| Ollama + qwen2.5:7b | Entity extraction + autonomous learning (optional) |
| FastMCP (SSE) | MCP server transport |
| Vue 3 + Vuetify | Dashboard with interactive graph visualization |

Ready to get started? Head to [Getting Started](getting-started.md).
