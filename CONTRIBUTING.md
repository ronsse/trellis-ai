# Contributing to Trellis

Thanks for your interest in contributing! This guide covers the essentials.

## Development Setup

```bash
# Clone and install with dev dependencies
git clone https://github.com/ronsse/trellis-ai.git
cd trellis-ai
uv pip install -e ".[dev]"

# Initialize stores (needed for integration-style tests)
trellis admin init
```

## Quality Checks

Run all checks before submitting a PR:

```bash
make format      # Auto-format with ruff
make lint        # Lint check
make typecheck   # mypy strict type checking
make test        # Full test suite
```

Or run them individually:

```bash
ruff format src/ tests/          # Format
ruff check src/ tests/           # Lint
mypy src/                        # Type check
pytest tests/ -v                 # Tests
pytest tests/unit/stores/ -v     # Single directory
```

## Project Structure

```
src/
  trellis/          # Core library (schemas, stores, mutations, retrieval, MCP)
  trellis_cli/      # CLI (trellis command)
  trellis_api/      # REST API (FastAPI)
  trellis_sdk/      # Client SDK (local or remote)
  trellis_workers/  # Background workers (classification, ingestion)
tests/
  unit/             # Mirrors src/ layout
docs/
  agent-guide/      # Operational reference for AI agents
```

## How to Add a Store Backend

1. Create `src/trellis/stores/<backend>/` with implementations for the ABCs in `stores/base/` (TraceStore, DocumentStore, GraphStore, VectorStore, EventLog, BlobStore).
2. Register the backend in `stores/registry.py` so `StoreRegistry.from_config_dir()` can instantiate it from config.
3. Add tests in `tests/unit/stores/` using `tmp_path` fixtures.

## How to Add a Search Strategy

1. Subclass `SearchStrategy` in `src/trellis/retrieve/strategies.py` (or a new module under `retrieve/`).
2. Implement `name` property and `search()` method returning `list[PackItem]`.
3. Wire it into `build_strategies()` or document how users can add it to `PackBuilder`.

## How to Extend Classification

1. Implement the `Classifier` protocol in `src/trellis/classify/`.
2. Register it in `ClassifierPipeline` — deterministic classifiers run inline at ingest time.
3. Add tests with synthetic items covering each classification decision.

## Pull Request Process

1. Branch from `main`. Use descriptive branch names (`feat/reranker-protocol`, `fix/session-dedup`).
2. All PRs run lint, typecheck, and test workflows automatically.
3. Keep PRs focused — one feature or fix per PR.
4. Write tests for new functionality. Target the existing test style: unit-scoped, `tmp_path` for stores, `MagicMock(spec=...)` for protocols.
5. Update `docs/agent-guide/` if your change affects the agent-facing API.

## AI-Assisted Development

This project uses Claude Code for development. The `CLAUDE.md` file at the repo root provides context and rules for AI-assisted work. PRs authored or co-authored by AI are welcome — just include the `Co-Authored-By` trailer.

## Code Style

- **Type hints** on all public APIs.
- **`structlog`** for logging (never `print()` in library code).
- **`extra="forbid"`** on all Pydantic models (via `TrellisModel` base).
- **Ruff** for formatting and linting — the pre-commit hook enforces this.

## Questions?

Open a [GitHub Discussion](https://github.com/ronsse/trellis-ai/discussions) or file an issue.
