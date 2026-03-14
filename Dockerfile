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
USER nobrainr

# Expose MCP SSE port
EXPOSE 8420

# Health check — /api/stats returns JSON (fast, non-streaming)
HEALTHCHECK --interval=30s --timeout=10s --retries=3 --start-period=20s \
    CMD curl -sf http://localhost:8420/api/stats > /dev/null

CMD ["nobrainr-mcp"]
