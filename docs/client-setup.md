# Client Setup

nobrainr connects to any MCP-compatible AI client via SSE transport.

## Claude Code

Add to `~/.claude/mcp.json`:

```json
{
  "mcpServers": {
    "nobrainr": {
      "type": "sse",
      "url": "http://your-server:8420/sse"
    }
  }
}
```

Or use the automated setup script:

```bash
NOBRAINR_HOST=your-server bash scripts/setup-client.sh
```

This also installs optional hooks and slash commands:

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
      "type": "sse",
      "url": "http://your-server:8420/sse"
    }
  }
}
```

## Cursor

Go to Settings > MCP > Add Server:

- **Type:** SSE
- **URL:** `http://your-server:8420/sse`

## Windsurf

Add to your MCP configuration:

```json
{
  "mcpServers": {
    "nobrainr": {
      "type": "sse",
      "url": "http://your-server:8420/sse"
    }
  }
}
```

## Any MCP client

Any client that supports the MCP SSE transport can connect using the URL pattern:

```
http://your-server:8420/sse
```

!!! note
    Replace `your-server` with the IP or hostname where nobrainr is running. If running locally, use `localhost`.

!!! warning
    The MCP port (8420) is bound to localhost by default in `docker-compose.yml`. If connecting from other machines, either:

    - Use a reverse proxy with TLS (recommended)
    - Change the port binding to `0.0.0.0:8420:8420` (not recommended without a firewall)
