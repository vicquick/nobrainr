# Contributing to nobrainr

Thanks for your interest in nobrainr! Here's how to get started.

## Development Setup

```bash
git clone https://github.com/youruser/nobrainr.git
cd nobrainr

# Backend
uv sync
uv run nobrainr status   # requires PG + Ollama running

# Frontend
cd dashboard
npm install
npm run dev              # dev server with hot reload
```

You need PostgreSQL 18 with pgvector and Ollama running locally, or use Docker:

```bash
docker compose up -d postgres ollama ollama-init
uv run nobrainr serve
```

## Code Style

- Python: [ruff](https://docs.astral.sh/ruff/) with line length 100
- TypeScript: standard Vue/Vuetify conventions
- Run `uv run ruff check src/` before committing

## Testing

```bash
uv run pytest
```

## Pull Requests

1. Fork the repo and create a feature branch from `main`
2. Make your changes
3. Run linting and tests
4. Open a PR with a clear description of what changed and why

Keep PRs focused — one feature or fix per PR.

## Reporting Issues

Open an issue on GitHub. Include:
- What you expected vs what happened
- Steps to reproduce
- Your environment (OS, Docker version, Python version)

## Architecture Overview

See [CLAUDE.md](CLAUDE.md) for a detailed breakdown of the codebase. That file is designed to be readable by both humans and AI agents.

## License

By contributing, you agree that your contributions will be licensed under the [MIT License](LICENSE).
