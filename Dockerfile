FROM python:3.12-slim

WORKDIR /app

# Install curl for health checks
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

# Install uv for fast dependency management
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy project files
COPY pyproject.toml .
COPY src/ src/

# Install dependencies
RUN uv pip install --system --no-cache .

# Expose MCP SSE port
EXPOSE 8420

# Health check — /sse is a streaming endpoint so curl always "times out" (exit 28).
# Exit 0 or 28 = healthy (server responded), anything else = unhealthy.
HEALTHCHECK --interval=30s --timeout=10s --retries=3 --start-period=15s \
    CMD curl -sf --max-time 2 -o /dev/null http://localhost:8420/sse; rc=$?; [ $rc -eq 0 ] || [ $rc -eq 28 ]

CMD ["nobrainr-mcp"]
