# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in nobrainr, please report it responsibly:

1. **Do NOT open a public issue.**
2. Use [GitHub Security Advisories](https://github.com/vicquick/nobrainr/security/advisories/new) to report privately.
3. Include steps to reproduce, affected versions, and potential impact.

We aim to acknowledge reports within 48 hours and provide a fix within 7 days for critical issues.

## Supported Versions

| Version | Supported |
|---------|-----------|
| 0.2.x   | Yes       |
| < 0.2   | No        |

## Security Considerations

nobrainr stores AI agent memory in PostgreSQL. When deploying:

- **Always change default database passwords** (`POSTGRES_PASSWORD` in `.env`)
- **Bind MCP port to localhost** or use a reverse proxy with TLS — the default `docker-compose.yml` already does this
- **Network isolation** — keep PostgreSQL and Ollama on internal networks only
- The import tools validate file paths to prevent path traversal
- Full-text search uses parameterized queries to prevent SQL injection
