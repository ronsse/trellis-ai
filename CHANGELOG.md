# Changelog

All notable changes to Trellis will be documented in this file.

## [Unreleased]

## [0.8.0] - 2026-05-12

First wave of the **self-improvement program** scoped in [`docs/design/plan-self-improvement-program.md`](docs/design/plan-self-improvement-program.md). Five PRs landed in one batch.

### Added

- **`EXTRACTION_FAILED` event type + `emit_extraction_failure()` helper** ([`src/trellis/extract/telemetry.py`](src/trellis/extract/telemetry.py)) — sampling cap (10 per `(extractor_id, prompt_hash, failure_kind)` cluster per 10-minute window, env-tunable), PII redaction (email / UUID / SSN-shape) bounded at 200 chars. Replaces silent JSON-parse swallows in `LLMExtractor.extract()` and `trellis_workers.learning.miner._parse_candidates` with emit-then-raise. `ExtractionDispatcher` is the *one* legitimate degrader — catches the new raises, emits a `tier_fallback` event with the original failure_kind on `error_class`, continues. ADR: [`docs/design/adr-extraction-failure-telemetry.md`](docs/design/adr-extraction-failure-telemetry.md). Item 4 of the self-improvement program. ([#110](https://github.com/ronsse/trellis-ai/pull/110))
- **Well-known promotion loop** ([`src/trellis/learning/schema_evolution.py`](src/trellis/learning/schema_evolution.py)) — `WELL_KNOWN_CANDIDATE` event type + `analyze_well_known_candidates()` analyzer. Surfaces open-string `node_type` / `edge_kind` values that meet promotion thresholds (count, distinct extractors, distinct domains, signal quality, time window). **Surface-only**: never auto-mutates `well_known.py`. Promotion is human-gated via ADR amendment. Includes `trellis analyze schema-evolution` + `trellis admin draft-promotion-adr <candidate_id>` CLI subcommands. Cooldown + recurrence handling deduplicates re-emission on growth / threshold-cross. Filters out `extractor_id startswith "trellis_meta_"` so future dogfooding writes won't feed back into promotion counts. ADR: [`docs/design/adr-well-known-promotion-loop.md`](docs/design/adr-well-known-promotion-loop.md). Item 5. ([#111](https://github.com/ronsse/trellis-ai/pull/111))
- **Self-improvement program docs** — umbrella plan + 5 ADR/plan pairs (Items 1, 4, 5, 6, 7) + plan-only entries for Items 2 + 3 + 2 cleanup tracks + 9-axis program-level eval spec + follow-on `adr-graph-shape-constraints.md` (lightweight SHACL-inspired declarative validation, scoped after this program). All in [`docs/design/`](docs/design/). ([#108](https://github.com/ronsse/trellis-ai/pull/108))
- **Silent-fallback audit script + 2026-05 baseline report** ([`scripts/audit_silent_fallbacks.py`](scripts/audit_silent_fallbacks.py), [`audit/silent_fallbacks_2026-05.md`](audit/silent_fallbacks_2026-05.md)) — AST-based deterministic scanner, classifies each `except` clause into DEFECT / GRACEFUL-DEGRADATION / GUARD / TEST-ONLY. 153 sites flagged, 112 DEFECT (73%). Surfaced an invisible retention-drift bug at `retention.py:169` (silently masks `datetime.fromisoformat` errors) tracked as a standalone P0 fix. Pre-audit speculation about embedder / policy-gate concentration turned out wrong; actual top files are `mcp/server.py` (31 DEFECT), `stores/registry.py` (16 DEFECT), `migrate/graph_migrator.py` (9), `retrieve/pack_builder.py` (9). C2 Phase 0. ([#112](https://github.com/ronsse/trellis-ai/pull/112))
- **`trellis admin init-learning-params` subcommand** ([`src/trellis_cli/admin.py`](src/trellis_cli/admin.py)) — seeds `~/.config/trellis/learning_params.yaml` with the recommended noise / promote thresholds so `trellis analyze learning-candidates` stops WARNing about defaulted values.

### Changed (breaking — POC stage)

- **`analyze_learning_observations()` now requires a `registry: ParameterRegistry` kwarg.** Calling without it raises `TypeError`. A registry that lacks the four required keys (`noise_success_threshold`, `noise_retry_threshold`, `promote_success_threshold`, `promote_retry_threshold`) raises `KeyError` naming the missing key + remediation command. Removes the silent-fallback path to hard-coded module constants. Item 3. ([#109](https://github.com/ronsse/trellis-ai/pull/109))
- **`LLMExtractor.extract()` and `PrecedentMiner._parse_candidates()` now raise `ExtractionFailureError`** on parse / validation failure instead of returning empty results. The dispatcher catches and degrades explicitly via a `tier_fallback` event; direct callers must do the same if they want graceful degradation. ([#110](https://github.com/ronsse/trellis-ai/pull/110))
- **`LEARNING_*_KEY` + `LEARNING_SCORING_COMPONENT` + `REQUIRED_LEARNING_PARAMETER_KEYS` exported from `trellis.learning`** as the single source of truth for the registry-key strings (previously duplicated across `scoring.py`, `analyze.py`, and test fixtures). ([#109](https://github.com/ronsse/trellis-ai/pull/109))

### Removed

- **`_NOISE_SUCCESS_THRESHOLD`, `_NOISE_RETRY_THRESHOLD`, `_PROMOTE_SUCCESS_THRESHOLD`, `_PROMOTE_RETRY_THRESHOLD`** hard-coded module constants from [`src/trellis/learning/scoring.py`](src/trellis/learning/scoring.py). Values now live in the operator-facing `LEARNING_PARAMETER_SEED_DEFAULTS` in `trellis_cli/analyze.py` (CLI seed) and in the ParameterRegistry the library requires. ([#109](https://github.com/ronsse/trellis-ai/pull/109))

### Cleanup

- Per-file simplify pass over each of the four code PRs in this wave: −104/+46 (#109), −42/+19 (#110), −18 net (#111), −125 (#112). Dead-code removals plus a POC-directive violation caught **inside the audit script itself** (a bogus `except Exception` around `ast.unparse`).

### Notes for adopters

POC directives now apply across this surface: no silent fallbacks, no backwards-compat shims, loud on misuse, no half-finished implementations. See [`docs/design/plan-self-improvement-program.md`](docs/design/plan-self-improvement-program.md) §2 for the full spec; the four cleanup tracks ([`plan-cleanup-dead-code.md`](docs/design/plan-cleanup-dead-code.md), [`plan-cleanup-silent-fallbacks.md`](docs/design/plan-cleanup-silent-fallbacks.md)) sequence the broader sweep.

## [0.7.0] - 2026-05-11

**ArcadeDB becomes the blessed graph + vector substrate** for self-hosted AWS deployments (Apache 2.0, Bolt + openCypher 25 at 97.8% TCK, native HNSW via jVector).

### Added

- **ArcadeDB graph backend** ([`src/trellis/stores/arcadedb/`](src/trellis/stores/arcadedb/)) — thin adapter over a shared [`BoltOpenCypherGraphStore`](src/trellis/stores/bolt_opencypher/graph.py) base class. Neo4j now subclasses the same base; ~1000 LOC of Cypher payload + SCD-2 logic shared between the two backends. (commits `ae410aa`, `5d85a27`)
- **ArcadeDB vector backend** — SQL-over-HTTP path with `LSM_VECTOR` index + `vectorNeighbors` function. Graph and vector see the same `(:Node)` rows but use different protocols. (commit `08714f3`)
- **ADR: [`adr-arcadedb-blessed-substrate.md`](docs/design/adr-arcadedb-blessed-substrate.md)** documenting the substrate decision (replaces LanceDB; preserves Neo4j as a supported alternative).
- **[`docs/deployment/recommended-config.yaml`](docs/deployment/recommended-config.yaml)** — three blessed shapes: local Neo4j + SQLite, cloud AuraDB + Postgres, ArcadeDB + Postgres. Smoke test pins the per-block backend contract.

### Removed

- **LanceDB substrate** ([commit `29175d3`](https://github.com/ronsse/trellis-ai/commit/29175d3)) — removed in favor of ArcadeDB for the blessed self-hosted graph + vector path. LanceDB worked but pinned a non-standard wire format; ArcadeDB's Bolt + openCypher matches the rest of the stack.

## [0.6.0] - 2026-05-11

Two themes ship together: the v0.5.x deprecation window finally closes (Phase 6 PR 2 removals), and the cold-start / Reading-B story lands as the spec + supporting code surface a green-field user needs to feed Trellis from scratch.

### Added — cold-start specification + supporting code

- **Cross-database routing properties** on dataset-shaped entities ([`src/trellis/schemas/well_known.py`](src/trellis/schemas/well_known.py)). New canonical convention: `source_system`, `connection_ref`, `database_name`, `schema_name`, `physical_uri`. Populated automatically by `DbtManifestExtractor` (from manifest `metadata.adapter_type`) and `OpenLineageExtractor` (from namespace URI scheme). Query-engine agents now read routing from the entity properties rather than getting it from their prompt or out-of-band config.
- **`"dataset"` → `Dataset` canonical alias** in [`src/trellis/schemas/well_known.py`](src/trellis/schemas/well_known.py). OpenLineage's lowercase output now buckets correctly with the canonical Dataset type at retrieval.
- **`sources.yaml` schema + loader** ([`src/trellis/extract/sources.py`](src/trellis/extract/sources.py)). Declarative source registry: one entry per upstream system, path-or-endpoint XOR, env-var-only credential refs (never inline secrets), unique-name validation, optional `enabled` and `tier_override` fields. Consumed by the new refresh CLI; ad-hoc per-source invocations still work without it.
- **`trellis extract refresh` CLI** ([`src/trellis_cli/extract_refresh.py`](src/trellis_cli/extract_refresh.py)). Two invocation forms: `--source <name>` (looks up `sources.yaml`) or `--type <type> --path <path>` (one-shot). For each entity touched, computes a property-level diff against the prior state and emits a `TAGS_REFRESHED` event with the structured before/after payload. Wires cleanly into cron / GitHub Actions / Airflow / K8s CronJob — Trellis remains the substrate, your scheduler runs the loop.
- **Demo migration** ([`src/trellis_cli/demo.py`](src/trellis_cli/demo.py) + [`examples/cold-start-fixture/`](examples/cold-start-fixture/)). `trellis demo load` now also runs a dbt + OpenLineage fixture through the *real* extractor + governed mutation pipeline alongside the legacy hand-coded narrative content. Same code path a production deployment uses — kills drift between "demo" and "real ingestion." Fixture is hand-editable for drift-detection demos.
- **Sample query-engine agent + Makefile** ([`examples/docker-demo/`](examples/docker-demo/)). `make -C examples/docker-demo demo` runs an annotated end-to-end script in under 60 seconds: seeds the cold-start fixture in-process, prints the routing properties on dataset entities, sketches the closing of the feedback loop. No Docker required for v1; the in-memory ASGI shim does the job.

### Added — cold-start documentation (four cornerstone guides)

- **[`docs/agent-guide/modeling-guide.md`](docs/agent-guide/modeling-guide.md)** — extended with five new sections: the four-store mental model (graph / document / blob / vector), reference-vs-summary decision matrix, cross-database routing properties contract, a third worked example covering curated knowledge derivation from SQL query logs (`JoinPattern` / `AccessPattern` / `HotDataset`), and the freshness-signals model (`valid_from` / `importance_scored_at` / `TAGS_REFRESHED` / `Lifecycle.state`).
- **[`docs/agent-guide/source-modeling-cookbook.md`](docs/agent-guide/source-modeling-cookbook.md)** — new doc. Per-source recipes for Markdown docs, Jira, Confluence, SQL query logs, Unity Catalog, and git repos. Entity types, edges, reference-vs-summary tradeoffs, recommended curated derivations, refresh cadence.
- **[`docs/agent-guide/extractor-authoring.md`](docs/agent-guide/extractor-authoring.md)** — new doc. The `Extractor` Protocol contract, tier semantics (`DETERMINISTIC` / `HYBRID` / `LLM`), purity rule, idempotency keys, entry-point plugin registration, telemetry contract, annotated walks of the dbt + OpenLineage reference implementations, a MVP skeleton.
- **[`docs/agent-guide/freshness-and-curation.md`](docs/agent-guide/freshness-and-curation.md)** — new doc. The two refresh modes (periodic pull vs pushed events), `trellis extract refresh` CLI walkthrough, scheduler patterns (cron / GHA / Airflow / K8s CronJob), curator workflows, lifecycle transitions, the variation → selection loop.
- **[`docs/agent-guide/quickstart-query-agent.md`](docs/agent-guide/quickstart-query-agent.md)** — new doc. Install → seed → CLI verify → run sample agent → MCP integration → drift test. The "5-minute from `git clone` to working query-engine agent" walkthrough.

### Removed

- **Flat `StoreRegistry` properties** — `trace_store`, `document_store`, `graph_store`, `vector_store`, `event_log`, `blob_store`. Use `registry.knowledge.<store>` (graph, vector, document, blob) or `registry.operational.<store>` (trace, event_log). Deprecated since v0.4.0.
- **Flat `stores:` config block** in `~/.trellis/config.yaml`. Use `knowledge:` / `operational:` plane blocks. Deprecated since v0.4.0.
- **`TRELLIS_PG_DSN` env-var fallback.** Set `TRELLIS_KNOWLEDGE_PG_DSN` and `TRELLIS_OPERATIONAL_PG_DSN` instead (both can point at the same DSN). Deprecated since v0.4.0.
- **`trellis admin migrate-config` CLI.** The flat → plane-split migrator was a one-shot helper for the deprecation window; with the flat block gone there is nothing to migrate.
- **`trellis_api/models.py` re-export shim** and **`trellis_api/deprecation.py`** infrastructure (the `DeprecationNotice` DTO and `ROUTE_DEPRECATIONS` registry that drove `Sunset` / `Deprecation` response headers on legacy routes). All API DTOs now live in `trellis_wire.dtos`; legacy route paths are gone.
- **`PACK_PUBLISH` / `PACK_INVALIDATE` mutation operations.** Both were declared in the operation enum but had no handlers and no callers — dead surface.

## [0.5.1] - 2026-04-29

### Added

- **`PgVectorStore` dim-mismatch fail-fast** ([#64](https://github.com/ronsse/trellis-ai/pull/64)). On `_init_schema`, after `CREATE TABLE IF NOT EXISTS vectors` no-ops against an existing table, the store reads the actual column dim from `pg_attribute` and raises `ValueError` if it doesn't match `self._dimensions`. Pre-fix the store silently inherited the old dim and crashed on the first upsert with `DataException: expected N dimensions, not M`. Error message offers two resolutions — pass the matching dim, or DROP TABLE.
- **AuraDB vector-index cohabitation documentation** ([#64](https://github.com/ronsse/trellis-ai/pull/64)). New section in [`docs/deployment/neo4j-auradb.md`](docs/deployment/neo4j-auradb.md) covering the "one vector index per `(label, property)` pair" constraint, what each consumer (unit tests, eval scenarios, loader) does, and the recommendation to use separate AuraDB Free instances. Two new troubleshooting rows.
- **Scenario 5.4 — agent-loop convergence** ([`eval/scenarios/agent_loop_convergence/`](eval/scenarios/agent_loop_convergence/scenario.py)). Synthetic agent runs N rounds of build-pack → grade-coverage → record-feedback. Periodic effectiveness + advisory fitness loops tag noise items and score advisories. Convergence delta = mean useful-fraction on last quarter minus first quarter. Default 30 rounds × 3 domains × 4 traces / domain on SQLite completes in ~1.4s. Plan §5.4.
- **Scenario 5.5 — multi-backend feedback loop** ([`eval/scenarios/multi_backend_feedback/`](eval/scenarios/multi_backend_feedback/scenario.py)). Runs the convergence loop scenario 5.4 measures against three handles (sqlite / postgres / neo4j_op_postgres) and diffs loop counters + convergence deltas. `vector_store` + `document_store` pinned to SQLite across all handles so cross-backend drift is attributable to the feedback path under test (event_log + trace + graph). Live 3-handle run on Neon + AuraDB Free showed identical loop counters across all three. Plan §5.5.2 row 3.
- **EventLog → learning.scoring promote bridge** ([`src/trellis/learning/observations.py`](src/trellis/learning/observations.py)). `build_learning_observations_from_event_log` joins `PACK_ASSEMBLED` + `FEEDBACK_RECORDED` events on `pack_id` and produces the observation shape `analyze_learning_observations` consumes. Closes the §5.5.2 row 2 gap where the dual-loop's *promote* half was implementation-only with zero callers in the source tree. The file-only JSONL variant is logged in TODO.md as a deferred ADR-shaped item — `PackFeedback` carries no per-item shape so a JSONL bridge would need either a schema extension or a sibling `pack_assembly.jsonl`.
- **Live-backend wipe orchestrator** ([`eval/_live_wipe.py`](eval/_live_wipe.py)). Single `wipe_live_state(registry)` call that dispatches by store type so scenarios 5.1, 5.3, and 5.5 all share one hygiene path. SQLite is a no-op via type-name short-circuit. Replaces three handle-name-coupled helpers in 5.5 and adds wipe to 5.1 + 5.3 (which previously had none and were silently contaminated by stale rows on the shared Neon + AuraDB test DBs).
- **Regime-shift demo mode for scenario 5.4** — `regime_shift_round` + `advisory_min_sample_size` kwargs make the advisory suppression branch fire end-to-end on a controlled corpus (3 anti-pattern advisories suppressed at the pre-row-3 corpus baseline). Restoration is unit-test-only by architectural fence — see TODO.md "Advisory restoration unreachable in scenario context".
- **`helpful_item_ids`-driven `usage_rate` in `analyze_effectiveness`** ([`src/trellis/retrieve/effectiveness.py`](src/trellis/retrieve/effectiveness.py)). Switches noise tagging from pack-level success rate to per-item agent reference signal when the corpus carries it; back-compat fallback to the old success-rate heuristic when `helpful_item_ids` is absent. Flipped scenario 5.4's `convergence.useful_delta` from -0.131 to +0.652 on the baseline run.

### Changed

- **Bulk fast paths for `upsert_nodes_bulk` + `upsert_edges_bulk` across all three graph backends** ([#60](https://github.com/ronsse/trellis-ai/pull/60), [#62](https://github.com/ronsse/trellis-ai/pull/62), [#63](https://github.com/ronsse/trellis-ai/pull/63)). Pre-fix: the bulk paths looped per-row `upsert_node` / `upsert_edge` with per-row commits; on managed Postgres + AuraDB the round trips dominated wall time. Post-fix: pre-validate, bulk-fetch existing rows once, close priors in a single statement, INSERT all new versions via bulk syntax, single commit at end. Same atomicity story (one transaction wraps the batch, strictly stronger than the prior per-row commit loop). Measured: SQLite **32 → 33,464 nodes/sec** on fresh-bulk (~1000×); Postgres **1–5 → 1794 nodes/sec** on Neon (~300–1000×); Neo4j **45 → 3643 nodes/sec** on AuraDB Free (~80×) via a CREATE-only branch when the role-immutability pre-fetch returns empty.
- **Eval scenarios 5.1 + 5.3 use `vector_store.upsert_bulk`** ([#61](https://github.com/ronsse/trellis-ai/pull/61)). Both `populated_graph_performance` and `multi_backend_equivalence` were doing per-row `vector_store.upsert()` in Python loops — 200 round trips at ~70ms each on AuraDB Free dominated each scenario's ingest metric. Switched to the bulk method: `ingest_nodes_per_sec.neo4j` in scenario 5.3 climbed from 40 to 219.86 and the scenario reports `pass` for the first time.
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
