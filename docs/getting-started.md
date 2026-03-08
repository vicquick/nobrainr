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

# Wait for Ollama to pull the embedding model (~270MB, first run only)
docker compose logs -f ollama-init

# Verify
curl -sf http://localhost:8420/api/stats
```

The extraction model (`qwen3.5:9b`, ~6.6GB) is also pulled on first start. If you don't need automatic entity extraction, set `NOBRAINR_EXTRACTION_ENABLED=false` in `.env`.

## pip install

```bash
pip install nobrainr
```

You'll need PostgreSQL with pgvector and Ollama running separately:

```bash
# Start PostgreSQL with pgvector
docker run -d --name nobrainr-db \
  -e POSTGRES_DB=nobrainr -e POSTGRES_USER=nobrainr -e POSTGRES_PASSWORD=changeme \
  -p 5432:5432 pgvector/pgvector:pg18

# Start Ollama and pull the embedding model
ollama pull nomic-embed-text

# Run nobrainr
export NOBRAINR_DATABASE_URL=postgresql://nobrainr:changeme@localhost:5432/nobrainr
nobrainr serve
```

## Local development

```bash
# Start only the infrastructure
docker compose up -d postgres ollama ollama-init

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
