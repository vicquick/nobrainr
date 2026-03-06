# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [0.2.0] - 2026-03-06

### Added
- Knowledge graph: entity extraction, relationships, graph traversal
- Vue 3 + Vuetify dashboard with interactive graph visualization
- Scheduler with autonomous learning jobs (summarize, consolidate, synthesize)
- ChatGPT and Claude memory import pipelines
- Memory feedback loop (was_useful tracking, importance recomputation)
- Full-text search (hybrid semantic + keyword)
- Agent event logging
- CLI: `nobrainr serve`, `status`, `search`, `entities`, `extract-backfill`, `import-chatgpt`, `import-claude`
- GitHub Actions CI (lint + test)
- Docker Compose with PostgreSQL 18 + pgvector, Ollama, auto model pull

### Security
- Path traversal protection on import tools
- SQL injection fix in full-text search (websearch_to_tsquery)
- Input bounds on all limit/offset/confidence parameters
- Non-root Docker user
- MCP port bound to localhost in docker-compose
- Connection pool race condition fix

## [0.1.0] - 2026-03-04

### Added
- Initial MCP server with memory_store, memory_search, memory_query
- PostgreSQL + pgvector storage with HNSW indexing
- Ollama nomic-embed-text embeddings (768 dimensions)
- Composite relevance scoring (similarity + recency + importance + stability)
- Starlette ASGI app with JSON API
- Basic configuration via environment variables
