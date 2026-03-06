# MCP Tools

nobrainr exposes 16 tools via the Model Context Protocol.

## Memory operations

### `memory_store`
Store a new memory with automatic embedding, deduplication check, and async entity extraction.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `content` | string | yes | The knowledge to remember |
| `summary` | string | no | One-line summary |
| `tags` | list[str] | no | Tags for categorization |
| `category` | string | no | High-level category |
| `source_type` | string | no | Source: "manual", "chatgpt", "claude", "agent" |
| `source_machine` | string | no | Which host generated this |
| `source_ref` | string | no | Reference to original source |
| `confidence` | float | no | Reliability score 0.0-1.0 (default 1.0) |
| `metadata` | dict | no | Additional structured data |

### `memory_search`
Semantic search across all memories, ranked by relevance (similarity + recency + importance).

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `query` | string | yes | Natural language search query |
| `limit` | int | no | Max results (default 10, max 100) |
| `threshold` | float | no | Minimum similarity 0.0-1.0 (default 0.3) |
| `tags` | list[str] | no | Filter to memories with these tags |
| `category` | string | no | Filter by category |
| `source_type` | string | no | Filter by source |
| `source_machine` | string | no | Filter by host |
| `hybrid` | bool | no | Also apply text search (default false) |

### `memory_query`
Structured query with filters. No semantic search, just filtering.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `tags` | list[str] | no | Filter by tags |
| `category` | string | no | Filter by category |
| `source_type` | string | no | Filter by source |
| `source_machine` | string | no | Filter by host |
| `text_query` | string | no | Full-text search on content |
| `limit` | int | no | Max results (default 50) |
| `offset` | int | no | Pagination offset |

### `memory_get`
Retrieve a specific memory by UUID.

### `memory_update`
Update an existing memory. Re-embeds automatically if content changes.

### `memory_delete`
Delete a memory by UUID.

## Knowledge graph

### `entity_search`
Semantic search on knowledge graph entities.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `query` | string | yes | Natural language query |
| `entity_type` | string | no | Filter by type (person/project/technology/concept/file/config/error/location/organization) |
| `limit` | int | no | Max results (default 10) |

### `entity_graph`
Traverse the knowledge graph from a named entity. Returns connected entities and relationships.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `entity_name` | string | yes | Starting entity name |
| `depth` | int | no | Hops to traverse (default 2, max 5) |

## Feedback & learning

### `memory_feedback`
Report whether a search result was useful. Improves future ranking.

### `memory_reflect`
Batch-save session learnings. More efficient than individual `memory_store` calls.

### `log_event`
Record agent activity (session starts, decisions, completions, errors).

## Maintenance

### `memory_maintenance`
Recompute importance scores and decay stability for stale memories.

### `memory_extract`
Manually trigger entity extraction for a specific memory.

### `memory_stats`
Database statistics: counts by source, category, machine, top tags, entity/relation counts.

## Import

### `memory_import_chatgpt`
Import conversations from a ChatGPT export file (`conversations.json`).

### `memory_import_claude`
Import memory files from a `.claude/` directory.
