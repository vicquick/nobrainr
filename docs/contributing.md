# Contributing

Contributions are welcome! Here's how to get set up.

## Development setup

```bash
git clone https://github.com/vicquick/nobrainr.git
cd nobrainr

# Start infrastructure
docker compose up -d postgres ollama ollama-init

# Install dependencies
uv sync --extra dev

# Run the backend
uv run nobrainr serve

# Run the dashboard (optional)
cd dashboard && npm install && npm run dev
```

## Code style

We use [ruff](https://docs.astral.sh/ruff/) for linting:

```bash
uv run ruff check src/
uv run ruff format src/  # auto-format
```

## Testing

```bash
uv run pytest
```

## Pull request process

1. Fork the repo and create a feature branch
2. Make your changes
3. Ensure `ruff check` and `pytest` pass
4. Submit a PR with a clear description of what changed and why

## Project structure

See the [README](https://github.com/vicquick/nobrainr#project-layout) for the full project layout.
