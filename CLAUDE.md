# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> **Picking up implementation work?** Read [`docs/design/implementation-roadmap.md`](docs/design/implementation-roadmap.md) first — it's the live, single-page hand-off doc with the state of the project and the recommended execution order across all open ADR phases.

## What This Is

A memory system for AI agents. Agents save memories (documents, deduplicated and embedded on ingest), record traces of their work, build a shared knowledge graph of entities and evidence, and retrieve token-budgeted context packs before starting new tasks. Feedback attributes outcomes to the specific items served, closing a learning loop over retrieval. The system provides governed mutations, immutable audit logging, and policy-based access control.

## Terminology

See [`docs/design/adr-terminology.md`](docs/design/adr-terminology.md) for the canonical term map. Highlights:

- **Tagging pipeline** = `src/trellis/classify/` (the module name stays, but prose calls it the tagging pipeline).
- **`ContentTags`** = retrieval-shaping tags (open vocabulary). **`DataClassification`** = access policy (closed, policy-relevant). **`Lifecycle`** = staleness state. All three co-exist in `src/trellis/schemas/classification.py`.
- **Enrichment** means the LLM-backed pipeline mode and the `EnrichmentService` class — nothing else. Use *tag* / *annotate* / *label* for generic prose.
- **Knowledge Plane** = agent-facing stores (graph, vector, document, blob). **Operational Plane** = Trellis-internal stores (trace, event log).
- **Substrate** = the blessed default backend per plane (one per store). **Backend** = any implementation class in `_BUILTIN_BACKENDS`. They are not synonyms.
- **Feedback loop** = the EventLog-authoritative path described below. `pack_feedback.jsonl` is an on-disk audit log of the same signal, not a second promote/demote path. **"Self-learning"** is not a project term.

## Hard Rules

- **Traces are immutable.** Once ingested, a trace cannot be modified or deleted through normal operations.
- **The governed mutation boundary is partial.** Graph, trace, feedback, retention, redaction and agent-facing evidence creation route through `MutationExecutor`. The remaining direct document/vector writes are rostered in `tests/unit/test_governed_write_rule.py`, which may only shrink (decision-ledger T-3).
- **Use `--format json` for machine output.** All CLI commands support it. Parse JSON output, not human-readable text. **A command's exit code must not depend on `--format`**: put the exit below the format branch and derive the payload's `status` from the same flag (`tests/unit/test_format_exit_parity_rule.py`).
- **Extra fields are forbidden.** All schemas use `extra="forbid"` (via `TrellisModel` base). Unrecognized fields cause validation errors.
- **No copyable handle reaches a Rich renderer raw.** Rich turns `:name:` into an emoji and deletes `[tag]` text. Build consoles with `trellis_cli.output.build_console`, never a bare `Console()`, and wrap identifier- or path-shaped values in `rich.markup.escape`, or pass `markup=False` for a wholly untrusted line (`tests/unit/test_rich_id_markup_rule.py`).
- **Use `structlog` for logging.** Never use `print()` in library code.
- **Type hints on all public APIs.**

## Development Commands

```bash
# Setup
uv pip install -e ".[dev]"
trellis admin init

# Quality
make lint          # ruff check src/ tests/
make format        # ruff format + fix
make typecheck     # mypy src/
make test          # pytest tests/ -v

# Run a single test file or test
pytest tests/unit/stores/test_graph_store.py -v
pytest tests/unit/stores/test_graph_store.py::test_upsert_and_get_node -v
```

## Architecture

### Five Packages, One Core

All packages depend on `trellis` (core library) and share configuration via `StoreRegistry.from_config_dir()` reading `~/.trellis/config.yaml` (or `$TRELLIS_CONFIG_DIR/config.yaml`) or env vars.

| Package | Entry Point | Access Pattern |
|---------|-------------|----------------|
| `trellis` | (library) | Schemas, stores, mutation executor, retrieval, MCP server |
| `trellis_cli` | `trellis` | Direct imports + StoreRegistry |
| `trellis_api` | `trellis-api` | StoreRegistry in FastAPI lifespan + `Depends()` injection |
| `trellis_sdk` | (library) | **Dual-mode**: local (lazy imports trellis directly) or remote (httpx to REST API) |
| `trellis_workers` | (library) | Direct imports + SDK client; submits Commands to MutationExecutor |

### Governed Mutation Pipeline (`src/trellis/mutate/`)

Operations route through `MutationExecutor` in 5 stages: validate → policy check → idempotency check → execute → emit event. Handlers and policy gates are Protocol-based (injected, not hardcoded). Batch execution supports `SEQUENTIAL`, `STOP_ON_ERROR`, and `CONTINUE_ON_ERROR` strategies.

- **Stage 2 always runs.** Build executors with `build_curate_executor`, whose gate comes from [`policy_source.py`](src/trellis/mutate/policy_source.py). A gate is passed unconditionally rather than only when policies exist, so "is Stage 2 running?" has an observable answer instead of depending on file state. `tests/unit/test_policy_gate_rule.py` requires a real gate on every `MutationExecutor(...)`.
- Zero policies ship, and the empty gate must stay transparent, audit payload included (`tests/unit/mutate/test_policy_wiring.py::TestDefaultPostureIsTransparent`). `resolve_policy_path` locates `policies.json` for every surface.
- Enforcement fails closed: a corrupt or unreadable policy file raises `ConfigError` rather than loading as zero policies, and emits `WRITE_REJECTED` under `config:policy_file` so `trellis analyze health` sees it. `PolicyStore` (display) degrades on read and refuses writes while degraded or stale.
- Test file presence with `trellis.core.path_presence.path_is_present`, never `Path.exists()`, which reads `ELOOP`/`ENOTDIR` as absent; swap it in only where a legible failure path sits downstream.
- The CLI and REST boundaries catch only `TrellisError`: `ConfigError` exits `EXIT_STORE` (5) and answers HTTP 409. Resolution is deny-wins. See [`docs/agent-guide/operations.md` § Policy Commands](docs/agent-guide/operations.md#policy-commands).
- Add a health signal as an event, not a new probe. An `info` log line is invisible on the default CLI (`TRELLIS_LOG_LEVEL` is `WARNING`).
- **Sanctioned exception — eval-scenario seeding.** Eval scenarios (in the separate `trellis-evals` repo) may emit audit events directly via `event_log.emit(...)` when seeding test data. **Production code paths must use `MutationExecutor`**.

### Write Provenance & Write-Behaviour Config (`src/trellis/core/`)

- Every event carries `metadata["write_provenance"]` (build version, git sha, `env_flags`), resolved once per process ([`write_provenance.py`](src/trellis/core/write_provenance.py)). `env_flags` records what was permitted, not what was performed.
- **Build images with `make docker-build`**; otherwise the stamp reads `fallback-version` with `commit: null` — honestly unidentifiable rather than falsely identified. A stale editable install adds `stamp_stale` and never overwrites `commit`.
- An advisory step on the write path is wrapped whole, not guarded by a list of exception types, because one escaped exception fails every write; `resolve_stamp_staleness` is the model.
- Add a write-behaviour knob only in [`write_config.py`](src/trellis/core/write_config.py), with an `ENV_VAR_BY_FIELD` entry. Inspect a process with `trellis admin write-config --format json`, a running API with `GET /api/version`.
- Every document write goes through `put_document` ([`document_write.py`](src/trellis/core/document_write.py); routing pinned by `tests/unit/core/test_document_write_rule.py`). Its keyword-only `preserve_updated_at` has no default (`test_put_document_signature.py`), and no static rule checks the literal is right, so decide content write vs metadata-only deliberately.

### Store Abstraction (`src/trellis/stores/`)

Six ABCs in `stores/base/`: TraceStore, DocumentStore, GraphStore, VectorStore, EventLog, BlobStore. `StoreRegistry` uses `importlib` for late-binding dynamic module loading — config determines which backend class to instantiate at runtime.

- Backend-specific setup belongs on the store class behind `prepare_registry_params`; `registry.py` never branches on a Bolt backend (`tests/unit/test_registry_plugin_boundary_rule.py`, [`adr-plugin-contract.md`](docs/design/adr-plugin-contract.md#registry-preparation-hook-store-plugins)).
- The JSON file stores `PolicyStore` and `AdvisoryStore` share [`DegradableJsonStore`](src/trellis/stores/degradable_json_store.py): reads degrade, writes refuse. A third such file gets a subclass, never a copy.
- **Contract test suites** in `tests/unit/stores/contracts/` are the authoritative spec, not the ABC docstrings; a new backend subclasses `GraphStoreContractTests` or `VectorStoreContractTests` ([`adr-canonical-graph-layer.md`](docs/design/adr-canonical-graph-layer.md)).
- A shape-#2 vector store overrides the no-op `provision_storage` to create its backing node rather than weakening the contract (`test_provisioning_alone_stores_no_vector`). `scripts/check_live_floor.py` fails live-infra when a contract passes fewer cases, or skips more, than its `FLOORS` row allows.

| Store | Default | Cloud |
|-------|---------|-------|
| Trace/Document/EventLog | `sqlite` | `postgres` (`TRELLIS_KNOWLEDGE_PG_DSN` / `TRELLIS_OPERATIONAL_PG_DSN`) |
| Graph | `sqlite` | **`arcadedb` (blessed)**, `postgres`, or `neo4j` (Bolt URI + credentials) |
| Vector | `sqlite` | **`arcadedb` (blessed)**, `pgvector`, or `neo4j` (HNSW on `:Node.embedding`) |
| Blob | `local` | `s3` (`TRELLIS_S3_BUCKET`) |

**ArcadeDB** is the blessed graph + vector substrate ([`adr-arcadedb-blessed-substrate.md`](docs/design/adr-arcadedb-blessed-substrate.md)); its graph backend and Neo4j's share [`BoltOpenCypherGraphStore`](src/trellis/stores/bolt_opencypher/graph.py). The Neo4j vector store keeps embeddings on the graph's `(:Node)` rows, so updating a node drops its embedding and callers must re-embed.

GraphStore implements SCD Type 2 temporal versioning (`valid_from`/`valid_to`) for time-travel queries via `as_of` parameter. Use `get_node_history()` for full audit trail.

**Type extensibility:** Entity types and edge types are **any string** at the storage and API layers. The `EntityType`/`EdgeKind` enums in `schemas/enums.py` are well-known defaults for agent-centric use, not a closed set. Domain-specific integrations (data platforms, infrastructure, etc.) define their own types in their own packages — do not add domain-specific types to the core enums.

### Classification Layer (`src/trellis/classify/`)

`ClassifierPipeline` runs in two modes configured by whether an LLM classifier is provided. Ingestion mode is deterministic-only (inline, microseconds). Enrichment mode adds LLM fallback (async, only fires when deterministic confidence < threshold). Four deterministic classifiers conform to the `Classifier` Protocol: `StructuralClassifier`, `KeywordDomainClassifier`, `SourceSystemClassifier`, `GraphNeighborClassifier`. `LLMFacetClassifier` wraps `EnrichmentService` for the LLM path.

Items are tagged with `ContentTags` (4 flat facets: `domain`, `content_type`, `scope`, `signal_quality`). Tags stored in metadata JSON, filtered via `json_extract`/`json_each` in SQLite. `PackBuilder` accepts `tag_filters` for pre-filtering before similarity scoring. Noise items (`signal_quality="noise"`) excluded by default. `compute_importance()` combines tags with LLM base scores. `apply_noise_tags()` closes the feedback loop from effectiveness analysis.

- **Demotion requires evidence of unhelpfulness, never absence of evidence of helpfulness** ([`demotion_gate.py`](src/trellis/classify/demotion_gate.py)); `apply_noise_tags` stays a dumb writer.
- Shadow-mode LLM tags go to `metadata["content_tags_shadow"]`, never `content_tags`, and [`servable.py`](src/trellis/retrieve/servable.py) strips them before a pack. A mined `domain` keyword is only proposed for a human to apply (`trellis classify tag-candidates`), because a wrong one hides content.

### Retrieval & Pack Builder (`src/trellis/retrieve/`)

`PackBuilder` orchestrates pluggable `SearchStrategy` protocols (keyword, semantic, graph), deduplicates by `item_id`, then enforces two-stage budgets: `max_items` then `max_tokens` (estimated at ~4 chars/token). Emits `PACK_ASSEMBLED` events with full telemetry for effectiveness analysis.

- Every surface builds packs through [`builder_factory.py`](src/trellis/retrieve/builder_factory.py), the only module that constructs `PackBuilder` (`tests/unit/retrieve/test_builder_factory.py`). A missing axis is reported in the pack's `axes` block, never absorbed.
- Unseeded, the graph axis is a recency window that ignores the intent. The factory injects `NamespaceSeedExtractor` (`TRELLIS_GRAPH_SEEDING`, on by default), and each item records `metadata["graph_selection"]`.
- Document recency comes from `resolve_recency_stamp`: a usable source-clock stamp beats the row clock, and `metadata["recency_clock"]` records which won. Retention keeps the row clock, and chunks carry no source clock (decision-ledger T-6).
- A rule that must hold for every strategy runs at the collect seam, because the strategy set is open: noise ([`noise.py`](src/trellis/retrieve/noise.py)), archived and superseded. Supersession is pairwise ([`partition_superseded`](src/trellis/retrieve/lifecycle.py)): a loser is withheld only while its successor is a candidate, and it gates enabling `TRELLIS_ENABLE_RECONCILE_ON_WRITE`.
- Every gate records `rejected_items` via `RejectedItem.from_pack_item`, and the pack states what was withheld above its items ([`withholding.py`](src/trellis/retrieve/withholding.py)). A `debug` log line is not a record: no shipped configuration prints it.
- A held-out pack (`TRELLIS_PACK_HOLDOUT_RATE`, off by default) is blind by design, the one exception to that; an aggregate `PACK_ASSEMBLED` reader drops its rows with `drop_holdout` ([`pack_holdout.py`](src/trellis/core/pack_holdout.py)).
- Truncate with `truncate_excerpt`; mark pre-LLM cuts with `elide_text`. The content floor (`ContentFloorConfig`) and graduated disclosure (`body_items`, [`disclosure.py`](src/trellis/retrieve/disclosure.py)) demote rather than drop.
- A vector row's metadata is an embed-time snapshot, so post-embed writers go through `sync_vector_metadata`.
- Measure a serving change with `trellis analyze replay` ([`pack_replay.py`](src/trellis/retrieve/pack_replay.py)), counting per `(pack_id, item_id)` serving. Replay cannot evaluate a change to which candidates exist, such as seeding; run both arms.
- Several obvious retrieval changes were measured and refused, among them semantic seeding, a per-parent rollup, shorter excerpts and index mode by default. Check [`docs/design/claude-md-rationale.md`](docs/design/claude-md-rationale.md) before proposing one.
- Capture health ([`capture_health.py`](src/trellis/ops/capture_health.py)): every capture surface needs an accept event that can clear its banner, and `NON_CAPTURE_SURFACES` is a deny-list (`tests/unit/mcp/test_capture_surface_roster.py`).

### Tiered Extraction (`src/trellis/extract/`)

Raw sources → `EntityDraft`/`EdgeDraft` records routed through `MutationExecutor`. Extractors are pure (no store writes). The `ExtractionDispatcher` routes by tier with priority `DETERMINISTIC > HYBRID > LLM` and `allow_llm_fallback=False` as the default — deterministic paths are first-class, LLM paths are opt-in additions, never silent substitutions. Core ships `JSONRulesExtractor`; `trellis_workers.extract` ships `DbtManifestExtractor` and `OpenLineageExtractor`.

- Memory extraction passes through `apply_memory_draft_policy` ([`draft_policy.py`](src/trellis/extract/draft_policy.py)): extraction attests mention, never possession, and unconfirmed mints stay out of packs by default.
- Where a field is verifiable from the source, the deterministic parse wins ([`evidence.py`](src/trellis/extract/evidence.py), at `extract_trace_batch`); an extractor's unattested value survives only as a `<field>_unverified` companion.

### LLM Client Abstraction (`src/trellis/llm/`)

Provider-agnostic protocols: `LLMClient`, `EmbedderClient`. Reference implementations for OpenAI / Anthropic live in `trellis.llm.providers` behind `[llm-openai]` / `[llm-anthropic]` optional extras so core stays dependency-free. See [`docs/design/adr-llm-client-abstraction.md`](docs/design/adr-llm-client-abstraction.md). A reply rescued by a salvage heuristic emits `extraction.parse_salvaged` (digests and lengths only, not a `failure_kind`; [ADR §2.4](docs/design/adr-extraction-failure-telemetry.md)).

### Feedback path — EventLog authoritative, JSONL audit log

The **EventLog is the single authoritative path** for the feedback loop. `trellis.feedback.recording.record_feedback()` always appends a `PackFeedback` row to `pack_feedback.jsonl` and, when given an `event_log` kwarg, also emits a `FEEDBACK_RECORDED` event; the file is durable, the event is what drives behavior. `trellis admin reconcile-feedback` replays file rows the EventLog lacks; the file is never a second decision path ([`adr-dual-loop-evolution.md`](docs/design/adr-dual-loop-evolution.md) §8).

**Not every feedback surface calls that function**, so the JSONL file is not a record of all feedback. Two families of surface exist, and they emit different `FEEDBACK_RECORDED` payloads:

| Surface | Route | JSONL row | Event payload |
|---|---|---|---|
| MCP `record_feedback` (#287), REST `POST /packs/{pack_id}/feedback`, `TrellisClient.record_feedback` (which posts to that route) | `PackFeedback.from_agent_signal` → `recording.record_feedback` | **yes** | Full `PackFeedback.to_event_payload()` — `feedback_id`, `rating`, `success`, `helpful_item_ids` / `unhelpful_item_ids` / `followed_advisory_ids`, `intent_family` |
| CLI `trellis curate feedback --pack-id`, REST `POST /feedback` | `Command(FEEDBACK_RECORD)` → `MutationExecutor` → `FeedbackRecordHandler` | **no** | `{target_id, rating, comment, success}` plus `pack_id` when the caller named one — no `feedback_id`, no item attribution |

- Both families derive `success` from `rating` with `SUCCESS_RATING_THRESHOLD`, so they cannot disagree about a rating.
- `attribution_rate` keeps its original denominator (DoD-3 reads it), less feedback naming a held-out pack; `ServeAttributionReport` reports the pack-targeted rates beside it.
- Promotion reads per-item fields from `PACK_ASSEMBLED.injected_items[]`. Flat packs only: `build_sectioned` emits no `injected_items[]`, so sectioned packs contribute zero per-item rows to the join.

### Test Structure

Tests live in `tests/unit/` mirroring source layout. All tests are unit-scoped using `tmp_path` fixtures for SQLite stores and `MagicMock(spec=...)` for protocols. `pytest-asyncio` with `asyncio_mode = "auto"` handles async tests. CLI tests suppress structlog output via `conftest.py`.

- Read CLI output through the `tests/cli_output.py` helpers, and run `FORCE_COLOR=1 pytest tests/unit/cli` before merging, because CI does not. Never run it alongside a plain pass: concurrent runs delete each other's `tmp_path`s.
- **Writing a rule that walks the AST of `src/`? Start from [`tests/ast_rules.py`](tests/ast_rules.py)**: share its mechanical layer, not the rules, and take the population floor from a hand read (`assert_hand_read_floor`), never from the scan it guards.
- Prove a guard by running it against a deliberately defective subject; see [`docs/agent-guide/testing.md`](docs/agent-guide/testing.md#fields-copied-everywhere-and-asserted-nowhere).

## Agent Guide

Detailed operational reference lives in `docs/agent-guide/`:

| Document | What It Covers |
|----------|----------------|
| [trace-format.md](docs/agent-guide/trace-format.md) | Constructing and ingesting valid trace JSON |
| [schemas.md](docs/agent-guide/schemas.md) | All Pydantic schemas with fields, types, and examples |
| [operations.md](docs/agent-guide/operations.md) | Full CLI, REST API, MCP, and Python mutation API reference |
| [playbooks.md](docs/agent-guide/playbooks.md) | Step-by-step procedures for common tasks |
| [pack-quality-evaluation.md](docs/agent-guide/pack-quality-evaluation.md) | Assembly-time pack scoring (6 dimensions, one opt-in via `expected_shapes`), profiles, scenario fixtures, optional `PackBuilder(evaluator=...)` hook |

## Autonomous / swarm work

Picking up implementation work as an autonomous agent? Read
[`docs/design/swarm-handoff.md`](docs/design/swarm-handoff.md) — the autonomy contract,
the merge gate (**green against *current* `main`**), the traps that have already cost
time, and the dependency-ordered queue. Decisions taken and pending live in
[`docs/design/decision-ledger.md`](docs/design/decision-ledger.md); the work items are in
[`docs/design/autonomous-backlog.md`](docs/design/autonomous-backlog.md).

**Test-coverage caveat worth knowing before you trust a green run:** local `make test` deselects the `postgres`, `pgvector`, `neo`, `arcadedb`, `live` and `slow` markers, so a green local run says nothing about any cloud backend.

- On pull requests `tests.yml` runs SQLite backends only; cloud backends run in `live-infra.yml`, whose `on:` block and path list you should re-read rather than trust a summary of.
- A file runs only if some leg selects its path **and** sets every gating marker on it (`tests/unit/test_ci_coverage_rule.py`). Add live suites one reviewed path at a time, never by sweeping `tests/unit/stores/` in.
- Neo4j silently discards a second vector index on the same `(label, property)`; reuse the store's default index name. Local pgvector runs need `CREATE EXTENSION vector` first ([#350](https://github.com/ronsse/trellis-ai/issues/350)).

## Product docs

- `docs/ROADMAP.md` — a **router**, not a roadmap of its own: which of the five planning
  documents is authoritative for what, and the Now / Next / Later gates stated as
  acceptance checks rather than as an item list. Carries no queue and no Done section on
  purpose — the queue is the tracker and the open PRs, which no file in this repo can see.
- `docs/PRD.md` — product thesis, adopter profiles, component disposition
- `docs/design/implementation-roadmap.md` — authoritative single-page roadmap; §3.H is the Productionization milestone (the 2026-07-11 edit-set has been applied into it and removed)

## Editing this file

Every agent loads this file on every call, so each line has a standing cost. Keep it to rules and pointers: one line per rule, naming the test or module that enforces it. Reasoning, measurements and incident history go in [`docs/design/claude-md-rationale.md`](docs/design/claude-md-rationale.md), an ADR or the decision ledger, with at most one linking line here. `tests/unit/test_claude_md_budget.py` fails above the byte budget; make room before raising it.
