FROM python:3.12-slim

WORKDIR /app

# Install curl (health checks) and gh CLI (GitHub sync scheduler job)
RUN apt-get update && apt-get install -y --no-install-recommends curl gpg && \
    curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      -o /usr/share/keyrings/githubcli-archive-keyring.gpg && \
    chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      > /etc/apt/sources.list.d/github-cli.list && \
    apt-get update && apt-get install -y --no-install-recommends gh && \
    rm -rf /var/lib/apt/lists/*

# Install uv for fast dependency management
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy project files
COPY pyproject.toml .
COPY README.md .
COPY src/ src/

# Install dependencies
RUN uv pip install --system --no-cache .

# Run as non-root user
RUN useradd --create-home --shell /bin/bash nobrainr

# Pre-download the cross-encoder reranker into the image layer. Without
# this the 560MB weights download at first-search time, and because
# the reranker runs under an asyncio semaphore (see services/reranker),
# every concurrent search queues behind the download. Observed on
# 2026-04-19: fresh container → MCP memory_search hung for minutes
# right after rebuild, not because of code but because of HF download.
# Baking the weights makes rebuild→search-ready deterministic at the
# cost of a ~560MB image layer.
ENV HF_HOME=/opt/hf_cache \
    SENTENCE_TRANSFORMERS_HOME=/opt/hf_cache \
    HF_HUB_DISABLE_TELEMETRY=1
RUN mkdir -p /opt/hf_cache && \
    python -c "from sentence_transformers import CrossEncoder; CrossEncoder('BAAI/bge-reranker-v2-m3', max_length=512, device='cpu')" && \
    chown -R nobrainr:nobrainr /opt/hf_cache

USER nobrainr

# Expose MCP SSE port
EXPOSE 8420

# Health check — /api/stats returns JSON (fast, non-streaming)
HEALTHCHECK --interval=30s --timeout=10s --retries=3 --start-period=20s \
    CMD curl -sf http://localhost:8420/api/stats > /dev/null

CMD ["nobrainr-mcp"]
