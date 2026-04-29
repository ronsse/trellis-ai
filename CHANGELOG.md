# Changelog

All notable changes to Trellis will be documented in this file.

## [Unreleased]

### Added

- **Scenario 5.4 — agent-loop convergence** ([`eval/scenarios/agent_loop_convergence/`](eval/scenarios/agent_loop_convergence/scenario.py)). Synthetic agent runs N rounds of build-pack → grade-coverage → record-feedback. Periodic effectiveness + advisory fitness loops tag noise items and score advisories. Convergence delta = mean useful-fraction on last quarter minus first quarter. Default 30 rounds × 3 domains × 4 traces / domain on SQLite completes in ~1.4s. Plan §5.4.
- **Scenario 5.5 — multi-backend feedback loop** ([`eval/scenarios/multi_backend_feedback/`](eval/scenarios/multi_backend_feedback/scenario.py)). Runs the convergence loop scenario 5.4 measures against three handles (sqlite / postgres / neo4j_op_postgres) and diffs loop counters + convergence deltas. `vector_store` + `document_store` pinned to SQLite across all handles so cross-backend drift is attributable to the feedback path under test (event_log + trace + graph). Live 3-handle run on Neon + AuraDB Free showed identical loop counters across all three. Plan §5.5.2 row 3.
- **EventLog → learning.scoring promote bridge** ([`src/trellis/learning/observations.py`](src/trellis/learning/observations.py)). `build_learning_observations_from_event_log` joins `PACK_ASSEMBLED` + `FEEDBACK_RECORDED` events on `pack_id` and produces the observation shape `analyze_learning_observations` consumes. Closes the §5.5.2 row 2 gap where the dual-loop's *promote* half was implementation-only with zero callers in the source tree. The file-only JSONL variant is logged in TODO.md as a deferred ADR-shaped item — `PackFeedback` carries no per-item shape so a JSONL bridge would need either a schema extension or a sibling `pack_assembly.jsonl`.
- **Live-backend wipe orchestrator** ([`eval/_live_wipe.py`](eval/_live_wipe.py)). Single `wipe_live_state(registry)` call that dispatches by store type so scenarios 5.1, 5.3, and 5.5 all share one hygiene path. SQLite is a no-op via type-name short-circuit. Replaces three handle-name-coupled helpers in 5.5 and adds wipe to 5.1 + 5.3 (which previously had none and were silently contaminated by stale rows on the shared Neon + AuraDB test DBs).
- **Regime-shift demo mode for scenario 5.4** — `regime_shift_round` + `advisory_min_sample_size` kwargs make the advisory suppression branch fire end-to-end on a controlled corpus (3 anti-pattern advisories suppressed at the pre-row-3 corpus baseline). Restoration is unit-test-only by architectural fence — see TODO.md "Advisory restoration unreachable in scenario context".
- **`helpful_item_ids`-driven `usage_rate` in `analyze_effectiveness`** ([`src/trellis/retrieve/effectiveness.py`](src/trellis/retrieve/effectiveness.py)). Switches noise tagging from pack-level success rate to per-item agent reference signal when the corpus carries it; back-compat fallback to the old success-rate heuristic when `helpful_item_ids` is absent. Flipped scenario 5.4's `convergence.useful_delta` from -0.131 to +0.652 on the baseline run.

### Changed

- **`eval/generators/graph_generator.py` default `embedding_dim` 16 → 3** to align with the pgvector contract suite's `DIMS=3` constant. The shared Neon test DB has a single `vectors` table; PR #64 added the construction-time fail-fast on dim mismatch but didn't align defaults — eval scenarios at default settings would always trip the new check. Cosine similarity at dim=3 still surfaces cross-backend equivalence drift; vector quality is not what 5.1 measures.
- **Scenario 5.4 corpus generator anchors required entities** in trace sampling, and `DOMAIN_TEMPLATES.query_intent` strings rewritten to mention every required entity by name. Levels per-domain `success_rate` from skewed (`software_engineering=0.0`, `data_pipeline=1.0`, `customer_support=0.0`) to uniform `1.0`. Pivots scenario 5.4's primary convergence gate from `weighted_delta` to `useful_delta` (the post-fix corpus is uniform enough that the breadth-weighted score under-credits successful noise tagging).
- **Scenario 5.4 advisory wiring** — `PackBuilder` now receives `advisory_store` so attached advisories show up in `PACK_ASSEMBLED.advisory_ids`, and advisories generate only on the first periodic pass so IDs stay stable for presentation accumulation. Production gates (`_ADVISORY_MIN_PRESENTATIONS = 3`, `_MIN_SAMPLE_SIZE = 5`) **were not changed** — the original symptom was scenario-driven, not threshold-driven.

### Fixed

- **`eval/runner.py` UTF-8 encoding** — `Path.write_text` defaulted to cp1252 on Windows and crashed on Unicode characters in finding / decision text. Reports are machine artifacts; UTF-8 is the only sane wire format.

### Removed

- **`EvalQuery.expected_categories` field** in [`eval/generators/trace_generator.py`](eval/generators/trace_generator.py). Defined but never set or read — scenarios pass `expected_categories=["entity_summary"]` directly to `EvaluationScenario` at score time. Surfaced by the live-data revisit's dead-code audit.

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
