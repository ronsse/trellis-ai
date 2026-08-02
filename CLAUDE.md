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
- **All mutations go through the governed pipeline.** Validate, policy check, idempotency check, execute, emit event. No direct store writes.
- **Use `--format json` for machine output.** All CLI commands support it. Parse JSON output, not human-readable text.
- **Extra fields are forbidden.** All schemas use `extra="forbid"` (via `TrellisModel` base). Unrecognized fields cause validation errors.
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

Every write flows through `MutationExecutor` in 5 stages: validate → policy check → idempotency check → execute → emit event. Handlers and policy gates are Protocol-based (injected, not hardcoded). Batch execution supports `SEQUENTIAL`, `STOP_ON_ERROR`, and `CONTINUE_ON_ERROR` strategies.

**Sanctioned exception — eval-scenario seeding.** Eval scenarios (in the separate `trellis-evals` repo since 2026-07-12) may synthesize audit events directly via `event_log.emit(...)` when seeding test data (e.g., `_seed_extraction_failures`, `_populate_entity_documents`). The pipeline's per-row policy + idempotency checks are uneconomical at the volume eval scenarios produce, and the events the pipeline *would* have emitted are reproducible from the synthetic seed. This is scenario-local; **production code paths must use `MutationExecutor`**.

### Write Provenance & Write-Behaviour Config (`src/trellis/core/`)

The same database is written concurrently by several builds (host editable install, container images of varying age), so a write has to say which code produced it. Every event emitted through `EventLog.emit` carries `metadata["write_provenance"]` — build version + git sha + `env_flags` + a short `env_flags_digest`. It rides the free-form `Event.metadata`, so it is additive: payload models keep `extra="forbid"`, rows written before it existed still parse, and no emitter is required to supply one. Resolved once per process ([`write_provenance.py`](src/trellis/core/write_provenance.py)).

Two things it deliberately is **not**. `env_flags` is the write-behaviour *environment the process was launched with*, not a per-write record — `memory_extraction` is ANDed with a caller's `--extract`, so `true` means permitted, not performed. And the version ([`version.py`](src/trellis/core/version.py)) comes from installed distribution metadata, which `hatch-vcs` populates from git at install/build time: an editable install carries a sha, but the Docker build context excludes `.git`, so **build images with `make docker-build`** (it passes `TRELLIS_BUILD_VERSION` through to `SETUPTOOLS_SCM_PRETEND_VERSION`). An image built any other way reports `version_source: "fallback-version"`, `commit: null` — honestly unidentifiable rather than falsely identified.

[`write_config.py`](src/trellis/core/write_config.py) is the **one home** for the ingest-time knobs (`TRELLIS_ENABLE_{CLASSIFY_ON_INGEST,EMBED_ON_INGEST,MEMORY_EXTRACTION,RECONCILE_ON_WRITE,TRACE_EXTRACTION}`, `TRELLIS_TRACE_EXTRACTION_MIN_CONFIDENCE`, `TRELLIS_RECONCILE_MODEL`, `TRELLIS_RECONCILE_TIMEOUT_S`). Add a new write-behaviour knob **there**, with an `ENV_VAR_BY_FIELD` entry — the per-module readers (`classify_on_ingest_enabled()` and friends) delegate to it and stay the call-site-facing names. Ask a process what it is applying with `trellis admin write-config --format json`; ask a running API container with `GET /api/version` (the stamp there is gated on the `/readyz` ops-detail posture).

### Store Abstraction (`src/trellis/stores/`)

Six ABCs in `stores/base/`: TraceStore, DocumentStore, GraphStore, VectorStore, EventLog, BlobStore. `StoreRegistry` uses `importlib` for late-binding dynamic module loading — config determines which backend class to instantiate at runtime.

**Contract test suites** in `tests/unit/stores/contracts/` define the shared semantics every backend must honour. New `GraphStore` backends subclass `GraphStoreContractTests` (49 tests covering CRUD, SCD-2, `as_of`, query, subgraph, aliases, deletion, counts, role validation, document_ids, temporal reads); new `VectorStore` backends subclass `VectorStoreContractTests` (25 tests covering CRUD, metadata round-trip, similarity ordering, top_k, metadata filters). See [`docs/design/adr-canonical-graph-layer.md`](docs/design/adr-canonical-graph-layer.md) for the rationale and the deliberate deviation for `Neo4jVectorStore` (shape #2 — vectors are properties on graph nodes, not an independent store). The contract suites are the authoritative spec — prose docstrings on the ABCs are not.

| Store | Default | Cloud |
|-------|---------|-------|
| Trace/Document/EventLog | `sqlite` | `postgres` (`TRELLIS_KNOWLEDGE_PG_DSN` / `TRELLIS_OPERATIONAL_PG_DSN`) |
| Graph | `sqlite` | **`arcadedb` (blessed)**, `postgres`, or `neo4j` (Bolt URI + credentials) |
| Vector | `sqlite` | **`arcadedb` (blessed)**, `pgvector`, or `neo4j` (HNSW on `:Node.embedding`) |
| Blob | `local` | `s3` (`TRELLIS_S3_BUCKET`) |

**ArcadeDB** is the blessed graph + vector substrate for self-hosted AWS deployments (Apache 2.0, Bolt + openCypher 25 at 97.8% TCK, native HNSW via jVector — see [`docs/design/adr-arcadedb-blessed-substrate.md`](docs/design/adr-arcadedb-blessed-substrate.md)). The graph backend is a thin adapter over a shared [`BoltOpenCypherGraphStore`](src/trellis/stores/bolt_opencypher/graph.py) base class that Neo4j also subclasses; ~1000 LOC of Cypher payload + SCD-2 logic is shared between the two backends. The vector backend uses ArcadeDB's SQL-over-HTTP path (`LSM_VECTOR` index + `vectorNeighbors` function) — graph and vector see the same `(:Node)` rows but use different protocols.

The Neo4j vector store attaches embeddings as an *optional* property on the
graph store's `(:Node)` rows (shape #2) — same database, same nodes, no
parallel `:VectorItem` label. This means the vector store's `item_id` is the
graph store's `node_id`, embeddings are skipped by the index when absent
(zero cost on structural nodes), and updating a node creates a new version
without inheriting the prior embedding (callers must re-embed). Requires
the `[neo4j]` optional extra and Neo4j 5.11+.

GraphStore implements SCD Type 2 temporal versioning (`valid_from`/`valid_to`) for time-travel queries via `as_of` parameter. Use `get_node_history()` for full audit trail.

**Type extensibility:** Entity types and edge types are **any string** at the storage and API layers. The `EntityType`/`EdgeKind` enums in `schemas/enums.py` are well-known defaults for agent-centric use, not a closed set. Domain-specific integrations (data platforms, infrastructure, etc.) define their own types in their own packages — do not add domain-specific types to the core enums.

### Classification Layer (`src/trellis/classify/`)

`ClassifierPipeline` runs in two modes configured by whether an LLM classifier is provided. Ingestion mode is deterministic-only (inline, microseconds). Enrichment mode adds LLM fallback (async, only fires when deterministic confidence < threshold). Four deterministic classifiers conform to the `Classifier` Protocol: `StructuralClassifier`, `KeywordDomainClassifier`, `SourceSystemClassifier`, `GraphNeighborClassifier`. `LLMFacetClassifier` wraps `EnrichmentService` for the LLM path.

Items are tagged with `ContentTags` (4 flat facets: `domain`, `content_type`, `scope`, `signal_quality`). Tags stored in metadata JSON, filtered via `json_extract`/`json_each` in SQLite. `PackBuilder` accepts `tag_filters` for pre-filtering before similarity scoring. Noise items (`signal_quality="noise"`) excluded by default. `compute_importance()` combines tags with LLM base scores. `apply_noise_tags()` closes the feedback loop from effectiveness analysis.

### Retrieval & Pack Builder (`src/trellis/retrieve/`)

`PackBuilder` orchestrates pluggable `SearchStrategy` protocols (keyword, semantic, graph), deduplicates by `item_id`, then enforces two-stage budgets: `max_items` then `max_tokens` (estimated at ~4 chars/token). Emits `PACK_ASSEMBLED` events with full telemetry for effectiveness analysis.

Excerpt hygiene lives in [`src/trellis/retrieve/excerpts.py`](src/trellis/retrieve/excerpts.py). `truncate_excerpt()` is the single boundary-aware truncator every strategy uses (sentence boundary → word boundary → hard cut, ellipsis-marked, never longer than the 500-char cap it replaced). A boundary is only honoured if it retains at least half the budget — both kinds, so an early `"Note. "` cannot gut the excerpt. The **content floor** demotes substance-free items — fewer than 5 distinct words in the excerpt, i.e. the name-only graph stubs — by multiplying their relevance score by `0.35`. It is a *penalty, not an exclusion*, by default: a legitimately terse memory (a one-line gotcha) must never be silently dropped. Item types whose excerpt is structured rather than prose are exempt (`exempt_item_types`, `observation` by default — a Measurement excerpt is `"row_count = 41823"` by construction). `PackBuilder(content_floor=ContentFloorConfig(mode="exclude" | "off"))` switches it. Every decision is observable — `PACK_ASSEMBLED.payload["content_floor"]` plus `content_floor_penalty` / `content_floor_substance_words` on the item's `score_breakdown`.

### Tiered Extraction (`src/trellis/extract/`)

Raw sources → `EntityDraft`/`EdgeDraft` records routed through `MutationExecutor`. Extractors are pure (no store writes). The `ExtractionDispatcher` routes by tier with priority `DETERMINISTIC > HYBRID > LLM` and `allow_llm_fallback=False` as the default — deterministic paths are first-class, LLM paths are opt-in additions, never silent substitutions. Core ships `JSONRulesExtractor` (field-reference and ancestor edges); `trellis_workers.extract` ships `DbtManifestExtractor` and `OpenLineageExtractor`. See [TODO.md — Tiered Extraction Pipeline — Phase 2 Plan](TODO.md#tiered-extraction-pipeline--phase-2-plan).

### LLM Client Abstraction (`src/trellis/llm/`)

Provider-agnostic protocols: `LLMClient`, `EmbedderClient`. Reference implementations for OpenAI / Anthropic live in `trellis.llm.providers` behind `[llm-openai]` / `[llm-anthropic]` optional extras so core stays dependency-free. See [`docs/design/adr-llm-client-abstraction.md`](docs/design/adr-llm-client-abstraction.md).

### Feedback path — EventLog authoritative, JSONL audit log

Context curation runs a variation → selection loop: extraction produces candidate context items, feedback grades them, the advisory + learning loops propagate or suppress. The **EventLog is the single authoritative path** for that loop. `trellis.feedback.recording.record_feedback()` always appends a `PackFeedback` row to `pack_feedback.jsonl` and, when given an `event_log` kwarg, also emits a `FEEDBACK_RECORDED` event; the file is durable, the event is what drives behavior.

**Not every feedback surface calls that function**, so the JSONL file is not a record of all feedback. Two families of surface exist, and they emit different `FEEDBACK_RECORDED` payloads:

| Surface | Route | JSONL row | Event payload |
|---|---|---|---|
| MCP `record_feedback` (#287), REST `POST /packs/{pack_id}/feedback`, `TrellisClient.record_feedback` (which posts to that route) | `PackFeedback.from_agent_signal` → `recording.record_feedback` | **yes** | Full `PackFeedback.to_event_payload()` — `feedback_id`, `rating`, `success`, `helpful_item_ids` / `unhelpful_item_ids` / `followed_advisory_ids`, `intent_family` |
| CLI `trellis curate feedback`, REST `POST /feedback` | `Command(FEEDBACK_RECORD)` → `MutationExecutor` → `FeedbackRecordHandler` | **no** | `{target_id, rating, comment}` only — no `feedback_id`, no item attribution |

Before #287 the MCP tool was in the second family, which is why `trellis admin reconcile-feedback` had nothing to reconcile on a deployment whose only grader was Claude Code. It is now in the first: one call writes both sinks, and the emit fails soft so a sink outage degrades to a file row the reconcile replays (the `record_feedback` tool in `src/trellis/mcp/server.py`).

`recording.record_feedback` is idempotent against the EventLog by `feedback.feedback_id` — a replayed call appends the JSONL row again but skips the emit and reports `event_log_skipped_as_duplicate`. The governed path has no such key.

| Path | Wire format | Persistence | Consumer | Role |
|---|---|---|---|---|
| EventLog | `FEEDBACK_RECORDED` event | store backend | `AdvisoryGenerator`, `effectiveness.analyze_*`, `run_advisory_fitness_loop`, `build_learning_observations_from_event_log` → `analyze_learning_observations` | **Authoritative.** Drives both demote (auto-suppress) and promote (human-reviewed `learning.scoring`) halves of the loop. |
| `pack_feedback.jsonl` | `PackFeedback` dataclass | on disk per run | `compute_item_effectiveness` (ad-hoc), `reconcile_feedback_log_to_event_log` (backfill into EventLog) | **Audit log only.** Durable file record of every pack signal. Not a second decision path. |

`PackFeedback.to_event_payload()` shapes the file row into the event payload; `reconcile_feedback_log_to_event_log()` replays rows missing from the EventLog. A file-only promote path was considered and **rejected** (see [`adr-dual-loop-evolution.md`](docs/design/adr-dual-loop-evolution.md) §8) — `PackFeedback` does not carry the per-item `item_type` / `source_strategy` / `category` fields `analyze_learning_observations` needs, so promotion runs strictly off the EventLog join in `learning/pack_observations.py`. Those fields are read from the **pack** side of the join — `PACK_ASSEMBLED.injected_items[]`, which since #285 also carries `title` / `category` / `domain_system` so a promotion candidate is legible to a human reviewer instead of a bare `item_id`. Flat packs only: `build_sectioned` emits no `injected_items[]`, so sectioned packs contribute zero per-item rows to the join.

### Test Structure

Tests live in `tests/unit/` mirroring source layout. All tests are unit-scoped using `tmp_path` fixtures for SQLite stores and `MagicMock(spec=...)` for protocols. `pytest-asyncio` with `asyncio_mode = "auto"` handles async tests. CLI tests suppress structlog output via `conftest.py`.

## Agent Guide

Detailed operational reference lives in `docs/agent-guide/`:

| Document | What It Covers |
|----------|----------------|
| [trace-format.md](docs/agent-guide/trace-format.md) | Constructing and ingesting valid trace JSON |
| [schemas.md](docs/agent-guide/schemas.md) | All Pydantic schemas with fields, types, and examples |
| [operations.md](docs/agent-guide/operations.md) | Full CLI, REST API, MCP, and Python mutation API reference |
| [playbooks.md](docs/agent-guide/playbooks.md) | Step-by-step procedures for common tasks |
| [pack-quality-evaluation.md](docs/agent-guide/pack-quality-evaluation.md) | Assembly-time pack scoring (6 dimensions, one opt-in via `expected_shapes`), profiles, scenario fixtures, optional `PackBuilder(evaluator=...)` hook |

## Product docs

- `docs/PRD.md` — product thesis, adopter profiles, component disposition
- `docs/ROADMAP-EDITS-2026-07-11.md` — proposed edit-set for `docs/design/implementation-roadmap.md` (which stays the authoritative roadmap)
