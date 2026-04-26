# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> **Picking up implementation work?** Read [`docs/design/implementation-roadmap.md`](docs/design/implementation-roadmap.md) first — it's the live, single-page hand-off doc with the state of the project, recommended execution order across all open ADR phases, and the live-test credential format for the AuraDB instance.

## What This Is

A structured experience store for AI agents. Agents record traces of their work, build a shared knowledge graph of entities and evidence, and retrieve context packs before starting new tasks. The system provides governed mutations, immutable audit logging, and policy-based access control.

## Terminology

See [`docs/design/adr-terminology.md`](docs/design/adr-terminology.md) for the canonical term map. Highlights:

- **Tagging pipeline** = `src/trellis/classify/` (the module name stays, but prose calls it the tagging pipeline).
- **`ContentTags`** = retrieval-shaping tags (open vocabulary). **`DataClassification`** = access policy (closed, policy-relevant). **`Lifecycle`** = staleness state. All three co-exist in `src/trellis/schemas/classification.py`.
- **Enrichment** means the LLM-backed pipeline mode and the `EnrichmentService` class — nothing else. Use *tag* / *annotate* / *label* for generic prose.
- **Knowledge Plane** = agent-facing stores (graph, vector, document, blob). **Operational Plane** = Trellis-internal stores (trace, event log).
- **Substrate** = the blessed default backend per plane (one per store). **Backend** = any implementation class in `_BUILTIN_BACKENDS`. They are not synonyms.
- **Feedback loop** = the EventLog-authoritative + JSONL-file-based dual-path system (see below). **"Self-learning"** is not a project term.

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

All packages depend on `trellis` (core library) and share configuration via `StoreRegistry.from_config_dir()` reading `~/.config/trellis/config.yaml` or env vars.

| Package | Entry Point | Access Pattern |
|---------|-------------|----------------|
| `trellis` | (library) | Schemas, stores, mutation executor, retrieval, MCP server |
| `trellis_cli` | `trellis` | Direct imports + StoreRegistry |
| `trellis_api` | `trellis-api` | StoreRegistry in FastAPI lifespan + `Depends()` injection |
| `trellis_sdk` | (library) | **Dual-mode**: local (lazy imports trellis directly) or remote (httpx to REST API) |
| `trellis_workers` | (library) | Direct imports + SDK client; submits Commands to MutationExecutor |

### Governed Mutation Pipeline (`src/trellis/mutate/`)

Every write flows through `MutationExecutor` in 5 stages: validate → policy check → idempotency check → execute → emit event. Handlers and policy gates are Protocol-based (injected, not hardcoded). Batch execution supports `SEQUENTIAL`, `STOP_ON_ERROR`, and `CONTINUE_ON_ERROR` strategies.

### Store Abstraction (`src/trellis/stores/`)

Six ABCs in `stores/base/`: TraceStore, DocumentStore, GraphStore, VectorStore, EventLog, BlobStore. `StoreRegistry` uses `importlib` for late-binding dynamic module loading — config determines which backend class to instantiate at runtime.

**Contract test suites** in `tests/unit/stores/contracts/` define the shared semantics every backend must honour. New `GraphStore` backends subclass `GraphStoreContractTests` (49 tests covering CRUD, SCD-2, `as_of`, query, subgraph, aliases, deletion, counts, role validation, document_ids, temporal reads); new `VectorStore` backends subclass `VectorStoreContractTests` (25 tests covering CRUD, metadata round-trip, similarity ordering, top_k, metadata filters). See [`docs/design/adr-canonical-graph-layer.md`](docs/design/adr-canonical-graph-layer.md) for the rationale and the deliberate deviation for `Neo4jVectorStore` (shape #2 — vectors are properties on graph nodes, not an independent store). The contract suites are the authoritative spec — prose docstrings on the ABCs are not.

| Store | Default | Cloud |
|-------|---------|-------|
| Trace/Document/EventLog | `sqlite` | `postgres` (`TRELLIS_PG_DSN`) |
| Graph | `sqlite` | `postgres` or `neo4j` (Bolt URI + credentials) |
| Vector | `sqlite` | `pgvector`, `lancedb`, or `neo4j` (HNSW on `:Node.embedding`) |
| Blob | `local` | `s3` (`TRELLIS_S3_BUCKET`) |

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

### Tiered Extraction (`src/trellis/extract/`)

Raw sources → `EntityDraft`/`EdgeDraft` records routed through `MutationExecutor`. Extractors are pure (no store writes). The `ExtractionDispatcher` routes by tier with priority `DETERMINISTIC > HYBRID > LLM` and `allow_llm_fallback=False` as the default — deterministic paths are first-class, LLM paths are opt-in additions, never silent substitutions. Core ships `JSONRulesExtractor` (field-reference and ancestor edges); `trellis_workers.extract` ships `DbtManifestExtractor` and `OpenLineageExtractor`. See [TODO.md — Tiered Extraction Pipeline — Phase 2 Plan](TODO.md#tiered-extraction-pipeline--phase-2-plan).

### LLM Client Abstraction (`src/trellis/llm/`)

Provider-agnostic protocols: `LLMClient`, `EmbedderClient`, `CrossEncoderClient`. Reference implementations for OpenAI / Anthropic live in `trellis.llm.providers` behind `[llm-openai]` / `[llm-anthropic]` optional extras so core stays dependency-free. See [`docs/design/adr-llm-client-abstraction.md`](docs/design/adr-llm-client-abstraction.md).

### Two feedback paths — EventLog (authoritative) vs JSONL (file-based)

Context curation runs a variation → selection loop: extraction produces candidate context items, feedback grades them, the advisory + learning loops propagate or suppress. Feedback reaches the analytics layer through **two paths** that do **not** duplicate each other — they serve different deployment contexts:

| Path | Wire format | Persistence | Consumer | Role |
|---|---|---|---|---|
| EventLog | `FEEDBACK_RECORDED` event | store backend | `AdvisoryGenerator`, `effectiveness.analyze_*`, `run_advisory_fitness_loop` | **Authoritative.** Automated continuous loops, MCP-driven agent flows. Auto-suppresses noise. |
| JSONL | `PackFeedback` dataclass | `pack_feedback.jsonl` on disk | `compute_item_effectiveness`, `learning.scoring` (human-reviewed promotions) | File-based workflows extracted from fd-poc. Batch analysis, human-in-the-loop precedent promotion. |

Bridge: `PackFeedback.to_event_payload()` plus the optional `event_log` / `pack_id` kwargs on `record_feedback()` let a file-based capture also emit into the authoritative EventLog. Dual-loop demotes; `learning.scoring` promotes — complementary halves of the same loop, not duplicates.

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
| [pack-quality-evaluation.md](docs/agent-guide/pack-quality-evaluation.md) | Assembly-time pack scoring (5 dimensions), profiles, scenario fixtures, optional `PackBuilder(evaluator=...)` hook |
