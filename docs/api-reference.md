# API Reference

nobrainr exposes a JSON API alongside the MCP server on the same port.

## Endpoints

### Memories

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/memories` | Search/list memories |
| GET | `/api/memories/{id}` | Memory detail + entities |
| POST | `/api/memories/{id}` | Update memory (JSON body) |
| DELETE | `/api/memories/{id}` | Delete memory |
| POST | `/api/memories/{id}/feedback` | Record feedback |

**Query parameters for `/api/memories`:**

| Param | Type | Description |
|-------|------|-------------|
| `q` | string | Semantic search query (triggers embedding) |
| `category` | string | Filter by category |
| `source_machine` | string | Filter by machine |
| `tags` | string | Comma-separated tag filter |
| `limit` | int | Max results (default 50) |
| `offset` | int | Pagination offset |

### Knowledge graph

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/graph` | Full graph data (Cytoscape.js format) |
| GET | `/api/node/{id}` | Entity detail + connections + related memories |

### Timeline

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/timeline` | Memories ordered by date |

### Utilities

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/stats` | Database statistics + feedback counts |
| GET | `/api/scheduler` | Scheduler status, jobs, recent events |
| GET | `/api/recall` | Fast full-text search (no embedding, uses PostgreSQL FTS) |
| GET | `/api/categories` | Unique categories |
| GET | `/api/tags` | Unique tags |
| GET | `/api/events` | SSE stream for real-time dashboard updates |
