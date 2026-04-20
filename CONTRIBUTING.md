# Contributing to Trellis

Thanks for your interest in contributing! This guide covers the essentials.

## Development Setup

```bash
# Clone and install dev deps + register git hooks in one shot
git clone https://github.com/ronsse/trellis-ai.git
cd trellis-ai
make setup

# Initialize stores (needed for integration-style tests)
trellis admin init
```

`make setup` does two things: installs `[dev,vectors]` extras (mirrors CI, so the default SQLiteVectorStore tests run locally) and registers the pre-commit git hook. After that, every `git commit` runs ruff format, ruff lint, mypy, and a handful of safety checks locally — catching anything CI would flag before the push round-trip. See [`.pre-commit-config.yaml`](.pre-commit-config.yaml) for the full list.

### Hooks reference

| `make` target | What it does |
|---|---|
| `make setup` | First-time: install `[dev,vectors]` + register hooks. Idempotent. |
| `make hooks` | Register (or re-register) the pre-commit git hook. |
| `make hooks-run` | Run every hook across the whole repo (not just staged files). |
| `make fix` | Auto-fix what ruff can, surface what it can't. |

### For agent contributors (Claude Code, etc.)

The repo ships a project-scoped `.claude/settings.json` with a `PostToolUse` hook that auto-formats Python files after any Edit/Write tool call ([scripts/claude_postedit_lint.py](scripts/claude_postedit_lint.py)). No setup required — just open the repo in Claude Code and the hook activates automatically. Per-user overrides go in `.claude/settings.local.json` (gitignored).

If you're contributing via another agent, point it at `make fix` after any edit to get the same behavior.

## Quality Checks

The pre-commit hook catches most issues at commit time. If you want to run the checks manually before opening a PR:

```bash
make format      # Auto-format with ruff
make lint        # Lint check
make typecheck   # mypy strict type checking
make test        # Full test suite
make check       # All of the above
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
