# Client Setup

nobrainr supports two MCP transports: **Streamable HTTP** (recommended) and **SSE** (legacy).

## Claude Code

Add to `~/.claude/mcp.json`:

```json
{
  "mcpServers": {
    "nobrainr": {
      "type": "streamable-http",
      "url": "http://your-server:8420/mcp"
    }
  }
}
```

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
      "type": "streamable-http",
      "url": "http://your-server:8420/mcp"
    }
  }
}
```

## Cursor

Go to Settings > MCP > Add Server:

- **Type:** Streamable HTTP
- **URL:** `http://your-server:8420/mcp`

## Windsurf

Add to your MCP configuration:

```json
{
  "mcpServers": {
    "nobrainr": {
      "type": "streamable-http",
      "url": "http://your-server:8420/mcp"
    }
  }
}
```

## Any MCP client

Any client that supports MCP Streamable HTTP can connect:

```
http://your-server:8420/mcp
```

For clients that only support SSE (legacy), use:

```
http://your-server:8420/sse
```

!!! note
    Replace `your-server` with the IP or hostname where nobrainr is running. If running locally, use `localhost`.

!!! warning
    The MCP port (8420) is bound to localhost by default in `docker-compose.yml`. If connecting from other machines, either:

    - Use a reverse proxy with TLS (recommended)
    - Change the port binding to `0.0.0.0:8420:8420` (not recommended without a firewall)
