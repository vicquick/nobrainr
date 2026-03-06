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

## Behind a reverse proxy

nobrainr serves both MCP SSE and a JSON API on port 8420. Behind nginx/Traefik/Caddy:

- `/sse` and `/messages/*` → backend (SSE — disable buffering)
- `/api/*` → backend (regular HTTP)
- `/*` → dashboard (static files)

### nginx example

```nginx
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
    proxy_pass http://localhost:3000;
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
