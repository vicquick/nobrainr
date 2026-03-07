# Client Setup

nobrainr supports two MCP transports: **HTTP** (recommended) and **SSE** (legacy).

For remote access across machines, always use HTTPS via a reverse proxy (see [Deployment — Security](deployment.md#security)).

## Claude Code

Add to `~/.claude/mcp.json`:

```json
{
  "mcpServers": {
    "nobrainr": {
      "type": "http",
      "url": "https://your-domain/mcp"
    }
  }
}
```

For local-only access (same machine), you can use `http://localhost:8420/mcp` without the `type` field.

Or follow the [Claude Code setup guide](claude-code-setup.md) which sets up everything via a single prompt. This installs hooks and slash commands:

- **Auto-load on session start** — queries nobrainr for relevant memories
- **Auto-save on session end** — stores session summaries
- **`/remember`** — review and store key session insights
- **`/recall <query>`** — search memories from the command line

## Claude Desktop

Open Settings > Developer > Edit Config, then add:

```json
{
  "mcpServers": {
    "nobrainr": {
      "type": "http",
      "url": "https://your-domain/mcp"
    }
  }
}
```

## Cursor

Go to Settings > MCP > Add Server:

- **Type:** HTTP
- **URL:** `https://your-domain/mcp`

## Windsurf

Add to your MCP configuration:

```json
{
  "mcpServers": {
    "nobrainr": {
      "type": "http",
      "url": "https://your-domain/mcp"
    }
  }
}
```

## Any MCP client

Any client that supports MCP HTTP transport can connect:

```
https://your-domain/mcp
```

For clients that only support SSE (legacy), use:

```
https://your-domain/sse
```

!!! note
    Replace `your-domain` with the domain or hostname where nobrainr is accessible. For local-only use, `localhost:8420` works without TLS.

!!! warning
    Never expose port 8420 directly to the internet. MCP traffic includes memory content in plaintext. For multi-machine setups:

    - Use a reverse proxy with TLS (required)
    - Restrict access via VPN or IP allowlist (recommended)
    - See [Deployment — Security](deployment.md#security) for examples
