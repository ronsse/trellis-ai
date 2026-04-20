# Changelog

All notable changes to Trellis will be documented in this file.

## [Unreleased]

## [0.4.0] - 2026-04-20

### Added

- **`trellis serve` CLI subcommand** — runs the REST API + UI with configurable `--host`, `--port`, `--config-dir`. Replaces the hardcoded `0.0.0.0:8420` in `trellis_api.app.main` and configures structured logging before uvicorn starts. Suitable for container ENTRYPOINTs.
- **`/healthz` and `/readyz` probe endpoints** — liveness (never touches stores) and readiness (calls `registry.operational.event_log.count()`, returns 503 until initialized). Wired for ECS, Kubernetes, and ALB target-group health checks. Unversioned, outside `/api/v1`.
- **Structured JSON logging** (`trellis_api.logging.configure_logging()`) controlled by `TRELLIS_LOG_FORMAT=json|console` and `TRELLIS_LOG_LEVEL`. JSON is the container default for CloudWatch / container log-driver ingestion.
- **Multi-stage Dockerfile** — `python:3.12-slim` base, `uv` builder, non-root runtime user, `[cloud,llm-openai]` extras, container-level `HEALTHCHECK` on `/healthz`. Plus `.dockerignore`.
- **Local `docker-compose.yml`** — offline rehearsal of the AWS ECS + RDS path. Boots the API container against `pgvector/pgvector:pg16` with the same code paths the cloud deployment uses. Exercises `trellis_knowledge` + `trellis_operational` schemas via the committed [`deploy/init-db.sql`](deploy/init-db.sql) init script and a mounted [`deploy/config.compose.yaml`](deploy/config.compose.yaml).
- **Cloud deployment documentation** — [`docs/deployment/aws-ecs.md`](docs/deployment/aws-ecs.md) runbook (ECR push, RDS + pgvector, S3 + VPC gateway endpoint, Secrets Manager, full task-definition JSON, bastion + MCP-stays-local note, backups), [`docs/deployment/local-compose.md`](docs/deployment/local-compose.md) smoke-test runbook, and [`docs/deployment/config.yaml.aws.example`](docs/deployment/config.yaml.aws.example) as a reference production config.
- **Client-repo starter scaffold** ([`examples/client_starter/`](examples/client_starter/)) — complete extract → ingest → retrieve loop showing the recommended layout for a consumer integrating Trellis from a separate Python repo. Demonstrates namespaced entity/edge types, a wrapped `TrellisClient` factory (remote or in-memory), a pure-function `DraftExtractor`, evidence ingestion, and pack retrieval. Verified end-to-end locally.

### Changed

- `trellis_api.app.main()` now accepts `host` and `port` parameters and configures logging before starting uvicorn (preserves the `DEFAULT_HOST = "0.0.0.0"`, `DEFAULT_PORT = 8420` behavior for existing callers).

### Resolved

- **SurrealDB BSL-1.1 license question** (previously tracked as an open item in [TODO.md](TODO.md)). SurrealDB 3.0 is BSL 1.1 with Change Date 2030-01-01 → auto-converts to Apache 2.0. The Additional Use Grant forbids only offerings "that enable third parties to create, manage, or control schemas or tables" — i.e. competing DBaaS products. Trellis consumers embedding SurrealDB as a hidden backend are allowed. If SurrealDB is picked, it must ship behind a `[surrealdb]` optional extra with the DBaaS carve-out documented in the backend-selection ADR.

## [0.3.2] - 2026-04-17

### Fixed

- Publish workflow's `publish` job failed at `actions/checkout` with "repository not found" because the explicit `permissions: id-token: write` block implicitly set `contents: none`. Added `contents: read` alongside the OIDC token permission.

## [0.3.1] - 2026-04-17

### Fixed

- `mypy` error in [`src/trellis_sdk/async_client.py`](src/trellis_sdk/async_client.py) that blocked the `test` job in the publish workflow. The `type: ignore[arg-type]` was on the wrong line inside a multi-line `httpx.AsyncClient(...)` call. The initial `v0.3.0` tag never produced a PyPI artifact — this is the first actual release.

## [0.3.0] - 2026-04-17

### Breaking changes

- **Removed `trellis-mcp-legacy` entry point** and deleted `src/trellis/mcp_server.py`. The current MCP server lives at `src/trellis/mcp/server.py` and is exposed as `trellis-mcp`. Anyone invoking `trellis-mcp-legacy` should switch to `trellis-mcp`.
- **Removed `[langgraph]` optional extra.** The LangGraph integration is no longer shipped in the wheel — it lives in [`examples/integrations/langgraph/`](examples/integrations/langgraph/) as a copy-paste reference template. Install `langgraph` and `langchain-core` directly in your project and copy `tools.py` in.
- **Moved `integrations/` to `examples/integrations/`.** None of the integrations (LangGraph, Obsidian, OpenClaw) ship in the wheel. They are reference templates you copy into your project. Test imports updated from `integrations.obsidian.*` to `examples.integrations.obsidian.*`.

### Added

- **PyPI publishing pipeline**: trusted-publisher (OIDC) workflow, `make build`/`verify-wheel`/`publish-check` targets, `workflow_dispatch` re-run path, `twine check` step, [RELEASING.md](RELEASING.md) runbook.
- **Examples directory** ([`examples/`](examples/)): SDK local + remote demos, retrieve→act→record loop, custom extractor, custom classifier, LangGraph agent, batch ingest script, and an MCP-from-Claude-Code walkthrough.
- **Skill templates** ([`skills/`](skills/)): drop-in Claude Code skills for `retrieve-before-task`, `record-after-task`, `link-evidence`.
- **MCP setup guides** for Claude Code, Cursor, and Claude Desktop in [`docs/getting-started/`](docs/getting-started/).
- **GitHub repo hygiene**: issue templates (bug, feature, config), PR template.
- **Python 3.13 support** added to CI matrix and PyPI classifiers.
- **`py.typed` markers** for `trellis_cli`, `trellis_sdk`, `trellis_api`, `trellis_workers` so type checkers see them as typed (`trellis` already had one).

### Changed

- **MCP server documentation now lists 11 tools, not 8.** The three sectioned-context tools (`get_objective_context`, `get_task_context`, `get_sectioned_context`) were already in the server but missing from every doc surface. Updated [docs/agent-guide/operations.md](docs/agent-guide/operations.md), [examples/integrations/openclaw/SKILL.md](examples/integrations/openclaw/SKILL.md), [README.md](README.md), and the IDE setup guides.
- **README links rewritten to absolute URLs** so they render correctly on PyPI.

## [0.2.0] - 2026-04-01

### Added

- **Classification Layer**: Hybrid deterministic + LLM tagging pipeline for all ingested content
  - Four orthogonal tag facets: `domain`, `content_type`, `scope`, `signal_quality`
  - Four deterministic classifiers: `StructuralClassifier`, `KeywordDomainClassifier`, `SourceSystemClassifier`, `GraphNeighborClassifier`
  - `LLMFacetClassifier` for async enrichment of ambiguous items (fires only when confidence < threshold)
  - `ClassifierPipeline` with two modes: ingestion (deterministic-only, microseconds) and enrichment (+ LLM fallback)
  - `compute_importance()` combining tags with LLM base scores for relevance ranking
  - `apply_noise_tags()` feedback loop: effectiveness analysis flags low-value items as noise, excluding them from future packs
  - Tag-based pre-filtering in `PackBuilder` (noise items excluded by default)

- **Web UI Foundation**: Dashboard served at `/ui` when running `trellis admin serve`
  - Live store stats (traces, documents, nodes, edges, events)
  - Store health status
  - Placeholder views for Graph Explorer, Evolution, Traces, and Precedents
  - Static files bundled in the PyPI wheel — no separate install needed

- **UI Design Documents**: Comprehensive design for full interactive UI
  - Graph Explorer with force-directed layout and time-travel slider
  - Evolution View: learning curve chart, pack composition drift, item lifecycle, domain generations
  - Trace Timeline, Improvement Dashboard, Precedent Library
  - ASCII wireframes, data flow diagrams, backend schema proposals
  - Demo scenario specification (8-week improvement arc from 40% to 85% success rate)

- **Package extras**: `all` convenience extra (`pip install trellis-ai[all]`)

### Changed

- FastAPI app version bumped to 0.2.0
- Fallback version updated to 0.2.0

### Fixed

- 22 code review issues across correctness, efficiency, and quality
  - `json_each` JOIN for multi-label domain filtering in SQLite stores
  - `get_node_history` ordering (DESC by `valid_from` for newest-first)
  - `StoreRegistry.close()` safety for partially initialized registries
  - `_emit_telemetry` exception handling in PackBuilder
  - Bounded idempotency cache (10K max) in MutationExecutor
  - `Content-Type` validation in API ingest routes
  - Defensive keyword extraction in `KeywordDomainClassifier`
  - `classification_version` default set to `"1"` in ContentTags

## [0.1.0] - 2025-12-15

### Added

- Initial release
- Core library (`trellis`): schemas, stores, mutation executor, retrieval, MCP server
- CLI (`trellis`): admin, ingest, retrieve, curate, analyze commands
- REST API (`trellis-api`): FastAPI server on port 8420
- Python SDK (`trellis_sdk`): dual-mode client (local or remote via httpx)
- Background workers (`trellis_workers`): ingestion (dbt, OpenLineage), maintenance
- Six store ABCs: TraceStore, DocumentStore, GraphStore, VectorStore, EventLog, BlobStore
- SQLite default backends with PostgreSQL cloud backends
- SCD Type 2 temporal versioning on graph nodes (time-travel via `as_of`)
- 13 edge types for entity relationships
- Governed mutation pipeline: validate, policy check, idempotency, execute, emit
- Context pack builder with keyword, semantic, graph, and recency search strategies
- Token-budgeted retrieval with two-stage limits (max_items, max_tokens)
- MCP server with 8 macro tools for Claude and other MCP clients
- OpenClaw skill for Claude Code integration
- LangGraph integration
- Obsidian vault indexer
- Effectiveness analysis and feedback loop
- Token usage tracking and telemetry
