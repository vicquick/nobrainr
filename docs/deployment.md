# Deployment

## Docker Compose (recommended)

The included `docker-compose.yml` runs everything: PostgreSQL 18 + pgvector, Ollama, and the nobrainr server.

```bash
git clone https://github.com/youruser/nobrainr.git
cd nobrainr
cp .env.example .env
# Edit .env with your settings
docker compose up -d
```

### Dashboard

The Vue 3 dashboard is a separate container:

```bash
cd dashboard
docker build -t nobrainr-dashboard .
docker run -d -p 3000:80 nobrainr-dashboard
```

## Security

nobrainr stores your AI agents' accumulated knowledge — treat it like a database containing proprietary information.

### Recommendations

- **Never expose port 8420 directly to the internet.** Use a reverse proxy with TLS.
- **Use HTTPS for all client connections.** MCP traffic includes memory content in plaintext.
- **Restrict access** via VPN, IP allowlist, or authentication at the proxy layer.
- **Set a strong `POSTGRES_PASSWORD`** in `.env` — the default is insecure.
- **Back up regularly** — your memories are valuable.

### Example: Traefik with TLS + IP allowlist

```yaml
# traefik dynamic config
http:
  middlewares:
    vpn-only:
      ipAllowList:
        sourceRange:
          - "10.0.0.0/8"       # your VPN subnet
          - "127.0.0.1/32"
    redirect-to-https:
      redirectScheme:
        scheme: https
        permanent: true
  routers:
    nobrainr-http:
      rule: "Host(`mcp.example.com`)"
      entryPoints: [http]
      middlewares: [redirect-to-https]
      service: noop@internal
    nobrainr-api:
      rule: "Host(`mcp.example.com`) && (PathPrefix(`/api`) || PathPrefix(`/sse`) || PathPrefix(`/messages`) || PathPrefix(`/mcp`))"
      entryPoints: [https]
      middlewares: [vpn-only]
      service: nobrainr-backend
      tls:
        certResolver: letsencrypt
      priority: 100
    nobrainr-dashboard:
      rule: "Host(`mcp.example.com`)"
      entryPoints: [https]
      middlewares: [vpn-only]
      service: nobrainr-frontend
      tls:
        certResolver: letsencrypt
      priority: 1
  services:
    nobrainr-backend:
      loadBalancer:
        servers:
          - url: "http://nobrainr:8420"
    nobrainr-frontend:
      loadBalancer:
        servers:
          - url: "http://nobrainr-dashboard:80"
```

Clients then connect via HTTPS:
```json
{
  "mcpServers": {
    "nobrainr": {
      "type": "http",
      "url": "https://mcp.example.com/mcp"
    }
  }
}
```

## Behind a reverse proxy

nobrainr serves MCP (HTTP + SSE) and a JSON API on port 8420. Behind nginx/Traefik/Caddy:

- `/mcp` → backend (HTTP transport — recommended)
- `/sse` and `/messages/*` → backend (SSE transport — legacy, disable buffering)
- `/api/*` → backend (regular HTTP)
- `/*` → dashboard (static files)

### nginx example

```nginx
server {
    listen 443 ssl;
    server_name mcp.example.com;

    ssl_certificate     /path/to/cert.pem;
    ssl_certificate_key /path/to/key.pem;

    # Optional: restrict to VPN subnet
    # allow 10.0.0.0/8;
    # deny all;

    location /mcp {
        proxy_pass http://localhost:8420;
        proxy_http_version 1.1;
        proxy_set_header Connection '';
        proxy_buffering off;
        proxy_cache off;
    }

    location /sse {
        proxy_pass http://localhost:8420;
        proxy_http_version 1.1;
        proxy_set_header Connection '';
        proxy_buffering off;
        proxy_cache off;
    }

    location /api/ {
        proxy_pass http://localhost:8420;
    }

    location /messages/ {
        proxy_pass http://localhost:8420;
    }

    location / {
        proxy_pass http://localhost:3000;  # dashboard
    }
}
```

## With Coolify

nobrainr works with [Coolify](https://coolify.io/) — connect your Git repo, set environment variables, and deploy. The `Dockerfile` and `dashboard/Dockerfile` are ready to use.

## Backups

```bash
# Backup
docker exec nobrainr-db pg_dump -U nobrainr nobrainr | gzip > nobrainr-$(date +%Y%m%d).sql.gz

# Restore
gunzip -c nobrainr-20260306.sql.gz | docker exec -i nobrainr-db psql -U nobrainr nobrainr
```

!!! warning
    The PostgreSQL volume contains all your memories. Set up regular backups before relying on nobrainr in production.
