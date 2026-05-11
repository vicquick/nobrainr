# Getting Started

## Docker (recommended)

```bash
git clone https://github.com/youruser/nobrainr.git
cd nobrainr
cp .env.example .env

# Edit .env — at minimum, set a real POSTGRES_PASSWORD
$EDITOR .env

# Start everything
docker compose up -d

# Wait for the model stack to load on first start
docker compose logs -f llama-swap

# Verify
curl -sf http://localhost:8420/api/stats
```

The reference deployment runs three on-GPU llama-server processes via a single `llama-swap` container:
**Qwen3.6-27B-IQ4_XS** (main LLM, ~15 GB VRAM at 32K ctx, parallel=2), **Qwen3-Embedding-0.6B** (~700 MB, 1024-dim), and **bge-reranker-v2-m3** (~600 MB). Total ~18 GB / 20 GB on an RTX 4000 Ada. If your GPU is smaller, drop the reranker first (`NOBRAINR_RERANKER_ENABLED=false`), then drop to a smaller LLM (Qwen3.5-9B or gemma3:4b). For CPU-only, set `NOBRAINR_EXTRACTION_ENABLED=false` — embedding-only mode still gives you semantic search.

## pip install

```bash
pip install nobrainr
```

You'll need PostgreSQL with pgvector and an LLM inference server running separately. The reference uses `llama-swap` + three `llama-server` (llama.cpp) processes, but any OpenAI-compatible endpoint works:

```bash
# Start PostgreSQL with pgvector
docker run -d --name nobrainr-db \
  -e POSTGRES_DB=nobrainr -e POSTGRES_USER=nobrainr -e POSTGRES_PASSWORD=changeme \
  -p 5432:5432 pgvector/pgvector:pg18

# Point nobrainr at any OpenAI-compatible LLM endpoint
# (llama-server, vLLM, Ollama with /v1/ proxy, etc.)
export NOBRAINR_LLM_SERVER_URL=http://localhost:5803
export NOBRAINR_DATABASE_URL=postgresql://nobrainr:changeme@localhost:5432/nobrainr
nobrainr serve
```

## Local development

```bash
# Start only the infrastructure
docker compose up -d postgres llama-swap

# Run the backend
uv sync
uv run nobrainr serve

# Run the dashboard (optional)
cd dashboard && npm install && npm run dev
```

## Verify it works

```bash
# Check status
curl http://localhost:8420/api/stats

# Connect with Claude Code
# See Client Setup for configuration
```

## Next steps

- [Connect your AI client](client-setup.md)
- [Configure nobrainr](configuration.md)
- [Explore MCP tools](mcp-tools.md)
