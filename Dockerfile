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
COPY uv.lock .
COPY README.md .
COPY src/ src/

# Install dependencies FROM THE LOCKFILE (2026-07-06). The old unlocked
# `uv pip install .` re-resolved on every build; a fresh numpy release
# made the resolver backtrack to numba 0.53/llvmlite 0.36 (py<=3.9
# relics that cannot build on 3.12) and every deploy failed. Export the
# frozen lock to requirements form, install pinned, then install the
# project itself without re-resolving.
RUN uv export --frozen --no-dev --no-emit-project --format requirements-txt -o /tmp/requirements.txt && \
    uv pip install --system --no-cache -r /tmp/requirements.txt && \
    uv pip install --system --no-cache --no-deps . && \
    rm /tmp/requirements.txt

# Run as non-root user
RUN useradd --create-home --shell /bin/bash nobrainr

# Reranker is a dedicated TEI sidecar (2026-04-19). The backend no
# longer bakes 560MB of BGE weights into its image — services/reranker
# calls the `reranker` app on the mcp Docker network via HTTP.
# sentence-transformers is kept as a dependency so the in-process
# fallback still works if the sidecar is unreachable; no weights are
# pre-downloaded, so the fallback path lazy-loads on demand.
ENV HF_HUB_DISABLE_TELEMETRY=1

USER nobrainr

# Expose MCP SSE port
EXPOSE 8420

# Health check — /api/stats returns JSON (fast, non-streaming)
HEALTHCHECK --interval=30s --timeout=10s --retries=3 --start-period=20s \
    CMD curl -sf http://localhost:8420/api/stats > /dev/null

CMD ["nobrainr-mcp"]
