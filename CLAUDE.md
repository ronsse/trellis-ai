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

**Contract test suites** in `tests/unit/stores/contracts/` define the shared semantics every backend must honour. New `GraphStore` backends subclass `GraphStoreContractTests` (100 tests covering CRUD, SCD-2, `as_of`, query, subgraph, aliases, deletion — pinned as a physical purge of all versions/edges/aliases, which `redaction.apply` relies on — counts, role validation, document_ids, temporal reads); new `VectorStore` backends subclass `VectorStoreContractTests` (34 tests covering CRUD, metadata round-trip, similarity ordering, top_k, metadata filters, bulk upsert, and the metadata-only re-upsert that `sync_vector_metadata` rests on). See [`docs/design/adr-canonical-graph-layer.md`](docs/design/adr-canonical-graph-layer.md) for the rationale and the deliberate deviation for `Neo4jVectorStore` (shape #2 — vectors are properties on graph nodes, not an independent store). The contract suites are the authoritative spec — prose docstrings on the ABCs are not.

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

**The LLM→deterministic ladder** ([`shadow.py`](src/trellis/classify/shadow.py) + [`tag_evolution.py`](src/trellis/learning/tag_evolution.py), #321). `DETERMINISTIC > LOCAL > FRONTIER` (`docs/PRD.md` §6) says judgment-avoidance should *dissolve* as evidence accrues — the LLM bootstraps a vocabulary, the deterministic layer inherits it, the LLM switches off for what was learned. Two halves, in strict order.

**Shadow mode** (`trellis classify shadow`) persists LLM verdicts to `metadata["content_tags_shadow"]`, never to `content_tags`. Safe against a production store because retrieval cannot see it, enforced twice independently: tag filters address `$.content_tags.<facet>` so a sibling top-level key is structurally unaddressable, and [`retrieve/servable.py`](src/trellis/retrieve/servable.py) strips it where `PackBuilder` collects every strategy's results — not inside the built-in strategies, since the strategy set is injected and open (a deny-list, so new metadata keys stay servable by default). The claim is scoped: shadow tags never reach a *pack*, not that they are confidential; `GET /api/v1/documents` returns them to a read-scoped caller, correctly, being the same access path as the content. The record is a `ShadowTags`, **not** a `ContentTags`, because the two paths use disjoint vocabularies — `ContentTags.content_type` is a closed `Literal` that rejects 9 of the 10 `DEFAULT_CLASSIFICATIONS` values, so coercing LLM output into it raises rather than refines. That disagreement is the measurement, so it is recorded verbatim and counted by `trellis classify shadow-report`. Each judged document emits `MEMORY_OP_JUDGED` (`JudgedOpType.CLASSIFICATION`) — this is #264's classify-layer instance, not a parallel channel — carrying digest, verdict label, confidence and a subject pointer, but **never** the open-vocabulary `domain` tags: those reveal subject matter, so they stay on the document behind the same access path as the content. Batch pass only; an LLM call has no business in the inline write path.

**Promotion** (`trellis classify tag-candidates`) mines the shadow corpus for keyword rules, modelled on `schema_evolution.py` and sharing its four constraints (read-only, `ParameterRegistry` thresholds that raise on a missing key, idempotent via the shared [`cooldown.py`](src/trellis/learning/cooldown.py), filters its own writes). Gated on support **and lift over the tag's base rate** — precision alone is satisfiable by a constant, since in a corpus dominated by one tag every keyword predicts it perfectly. `domain` is **surface-only**: the analyzer proposes a `classify.domain_keywords` fragment a human pastes into `config.yaml`; `apply_promotion` / `revoke_promotion` are pure inverse transforms over that map, so a promotion is revocable. It is never auto-applied because `domain` is the one facet that hard-excludes — a wrong keyword *hides* content (the #282 failure). Promotion is measured on **agreement with the LLM, not retrieval outcome**: this is a distillation step with a human gate, not yet a closed learning loop. Closing it needs the pack-feedback join.

### Retrieval & Pack Builder (`src/trellis/retrieve/`)

`PackBuilder` orchestrates pluggable `SearchStrategy` protocols (keyword, semantic, graph), deduplicates by `item_id`, then enforces two-stage budgets: `max_items` then `max_tokens` (estimated at ~4 chars/token). Emits `PACK_ASSEMBLED` events with full telemetry for effectiveness analysis.

Excerpt hygiene lives in [`src/trellis/retrieve/excerpts.py`](src/trellis/retrieve/excerpts.py). `truncate_excerpt()` is the single boundary-aware truncator every strategy uses (sentence boundary → word boundary → hard cut, marked `… [+2.3k chars]` so the consumer can judge whether the full document is worth fetching, never longer than the 500-char cap it replaced — the marker is invisible to the substance-word count). A boundary is only honoured if it retains at least half the budget — both kinds, so an early `"Note. "` cannot gut the excerpt. The semantic path is cut at *embed* time (`build_vector_row`), not at retrieval: the vector row's metadata excerpt is the last copy of the text a pack consumer sees, and only the embed hook still holds the full document to measure against. The same honesty rule covers prompt caps: `trellis.core.elision.elide_text()` marks any pre-LLM payload cut with an explicit `<elided chars=… reason=… />` tag (distillation + enrichment call it; #310). The **content floor** demotes substance-free items — fewer than 5 distinct words in the excerpt, i.e. the name-only graph stubs — by multiplying their relevance score by `0.35`. It is a *penalty, not an exclusion*, by default: a legitimately terse memory (a one-line gotcha) must never be silently dropped. Item types whose excerpt is structured rather than prose are exempt (`exempt_item_types`, `observation` by default — a Measurement excerpt is `"row_count = 41823"` by construction). `PackBuilder(content_floor=ContentFloorConfig(mode="exclude" | "off"))` switches it. Every decision is observable — `PACK_ASSEMBLED.payload["content_floor"]` plus `content_floor_penalty` / `content_floor_substance_words` on the item's `score_breakdown`.

**The noise boundary** ([`src/trellis/retrieve/noise.py`](src/trellis/retrieve/noise.py), #338). "Noise items excluded by default" was expressed only as a store-side predicate, and that predicate reached exactly one axis under exactly one calling convention: `SemanticSearch` *strips* `content_tags` from the filters it forwards (vector backends offer only hard-equality scalar filters — passing the facet bag through matches nothing, #254), and `_build_filters` returns early when `tag_filters is None`, which is what MCP `get_context` passes unless a `domain` is given. So the default was never constructed on that path and never reached the semantic axis on any path. It is now enforced at the **collect seam** beside `exclude_archived` — same reasoning as the serving and lifecycle boundaries: the strategy set is injected and open, so a rule applied inside the built-ins would not hold for a fourth added later. Default-pass (an untagged item is never hard-excluded) and identical to the document store's tag-filter semantics, which the pushdown still applies where it works — a row filtered in SQL never spends a strategy's `limit` budget. A caller-supplied `tag_filters={"signal_quality": {"in": ["noise"]}}` inverts it for curation tooling.

**Post-embed metadata write-through** ([`src/trellis/core/vector_metadata.py`](src/trellis/core/vector_metadata.py), #338). A vector row's metadata is a **snapshot taken at embed time**, and `SemanticSearch` builds its `PackItem` from that snapshot rather than from the document store — so a tag written after embedding is invisible to semantic retrieval. `apply_noise_tags` wrote only to the document store; production held 45 noise-tagged documents and not one whose vector row agreed. Post-embed writers now mirror `content_tags` / `auto_importance` onto the row through `sync_vector_metadata`, a **metadata-only re-upsert** that re-embeds nothing, fails soft (the document row is authoritative and already written), and treats a missing row as a no-op. The pair of keys is synced together deliberately: an `auto_importance` without the `importance_scored_at` stamp inside `content_tags` is the broken pair `_apply_importance` raises on. `trellis admin resync-vector-metadata` repairs rows that diverged before the write-through existed — idempotent, and needing no embedder, unlike `reindex-vectors --force`. #337 fixed the same root cause for `Lifecycle` on the retention path.

**Graduated disclosure** ([`src/trellis/retrieve/disclosure.py`](src/trellis/retrieve/disclosure.py), #359). `PackBudget.max_tokens` is documented as a ceiling and was implemented as a **quota**: the greedy walk kept admitting items while any budget remained, so **zero of 37 packs in a 30-day window served every candidate they found** — 17 ran out of tokens, 20 hit `max_items`. Nobody chose a 35-item pack; the number fell out of `max_tokens / ~90 tokens per excerpt`. Meanwhile the bottom fifth of a pack by rank carries 23.5% of its tokens and **1.9%** of its cited-helpful tokens (top fifth: 16.9% and 19.3%), so the last several hundred tokens of every fat pack bought almost nothing. A flat pack now serves the first `body_items` (default 12) excerpts and demotes the rest to one-line pointers carrying a label and the withheld size — *demotion, not exclusion*, the same commitment the content floor makes: the id stays in the pack, ranked, cited, and fetchable via `get_items`. Applied **after** the token-budget walk, never before — pricing the tail cheaper first just lets the greedy admit more of it. That is not a hypothesis, it is what the width lever does: `analyze replay --excerpt-max-chars 300` saves 4.1% and drives the fraction 0.088 → 0.066 by backfilling 116 ungraded items. Post-walk, `--body-items 12` replays at **-30.3% tokens, useful-token fraction 0.088 → 0.120, one of 35 cited-helpful servings demoted and none dropped**. Two things deliberately *not* done. Excerpt width is unchanged: **`useful_token_fraction` is a ratio, so a uniform cap scales numerator and denominator together and cannot move it** — suppress the refill and a 300-char cap shifts it 0.088 → 0.090. Width is a cost lever where the item count binds and never a precision lever; treat any future "just make excerpts shorter" proposal as answered. And index mode (#305) is **not** the default and is exempt (it is already all pointers): its compression is 2.7:1 not 10:1 (`item_id` averages 44 chars), its break-even is a fetch rate under ~63%, and it has fired on zero packs, so nothing measures the quantity that would justify it. A relative-*score* cut was tried and refused — post-RRF every item scores within 0.74-1.00 of its pack's top item (helpful median 0.968 vs unjudged 0.910), so rank is the only separating signal.

**Counterfactual replay** ([`pack_replay.py`](src/trellis/retrieve/pack_replay.py), `trellis analyze replay`, #359). A serving change affects only *future* packs, so a before/after across two windows compares two populations of packs graded by different callers and calls the difference the effect of the change. `PACK_ASSEMBLED.budget_trace[]` already records every candidate the walk saw **including the ones it rejected**, with each one's charge — so the walk is *re-run*, not modelled, over the same packs and the same citations with one variable changed. An empty policy must reproduce `analyze value` exactly; that identity is the property the whole method rests on and is pinned by test. Every arm reports what the policy **cost** beside what it saved: cited-helpful bodies withheld, cited-helpful servings dropped, and items the refill admitted that nobody ever graded. Counted per `(pack_id, item_id)` **serving**, never per distinct id — a memory bodied in one pack must not mask its being withheld in another, which understated a real cost as `0/25` before it was caught. Costs are priced from `budget_trace[].item_tokens` (what was charged), not `injected_items[].estimated_tokens` (which stays the *excerpt read cost* by design in index mode, and would over-charge such a pack ~3x). The window rolls, so re-derive rather than trusting a figure in prose: these were taken over the 30 days to 2026-08-27 (n=17 attributed packs) and the same conclusions held on the preceding n=15 window.

**Capture-health banner** ([`src/trellis/ops/capture_health.py`](src/trellis/ops/capture_health.py), #309). Every MCP pack surface prepends a warning block when a *write* surface has gone dark: at least `TRELLIS_CAPTURE_WARN_THRESHOLD` (default 3) rejections — boundary `WRITE_REJECTED` (#297) plus executor `MUTATION_REJECTED`, aggregated under one `mcp:<tool>` label, idempotency replays excluded — and **zero accepted writes for that same surface** in the trailing `TRELLIS_CAPTURE_WARN_WINDOW_HOURS` (default 24). Per-surface, not global: the motivating incident has a nightly ingest landing rows while every `save_*` call is rejected, so a global "zero accepted anywhere" rule would stay silent through exactly the outage it was built for. These are **read-side** knobs and deliberately do *not* live in `write_config.py`. The empty pack carries the banner too — that is where dark capture masquerades as greenfield — and the check fails soft (indeterminate presents as healthy), never blocking a pack.

### Tiered Extraction (`src/trellis/extract/`)

Raw sources → `EntityDraft`/`EdgeDraft` records routed through `MutationExecutor`. Extractors are pure (no store writes). The `ExtractionDispatcher` routes by tier with priority `DETERMINISTIC > HYBRID > LLM` and `allow_llm_fallback=False` as the default — deterministic paths are first-class, LLM paths are opt-in additions, never silent substitutions. Core ships `JSONRulesExtractor` (field-reference and ancestor edges); `trellis_workers.extract` ships `DbtManifestExtractor` and `OpenLineageExtractor`. See [TODO.md — Tiered Extraction Pipeline — Phase 2 Plan](TODO.md#tiered-extraction-pipeline--phase-2-plan).

**Memory-path draft policy** ([`draft_policy.py`](src/trellis/extract/draft_policy.py), #299/#300): both memory-extraction call sites (the CLI ingest hook and MCP `save_memory`) run every `ExtractionResult` through `apply_memory_draft_policy` before `result_to_batch`. Person-typed drafts naming a conversation participant are dropped (a conversation's speakers are its frame, not its subject matter), and every fresh mint is stamped with `document_ids=[source_doc]` plus the claim floor `extraction_status="unconfirmed"` / `epistemic_status="mentioned"` — extraction attests *mention*, never possession or use. `GraphSearch` excludes unconfirmed mints from packs by default (pass `include_unconfirmed=True` to surface them for review; confirming an entity is an `entity.update` that sets `extraction_status="confirmed"`).

**Deterministic evidence override** ([`evidence.py`](src/trellis/extract/evidence.py), #308): the same claim floor one level deeper — whenever a field is *verifiable from the source material*, the deterministic parse wins and an extractor-supplied value is at most additive. `parse_trace_evidence` reads a trace's `tool_call` payloads (Edit/Write/MultiEdit/NotebookEdit shapes, unified-diff `+++`/`---` hunks in patch args, shell `command` args) into a `TraceEvidence` record; `apply_trace_evidence` stamps it onto the Activity draft at the shared [`extract_trace_batch`](src/trellis/extract/trace_ingest_hook.py) seam — the one path both the live ingest hook and the `trellis extract traces` backfill route through — so the guarantee holds for *whatever extractor* produced the result, including a future LLM residue pass. Supplied values that the evidence does not attest are kept but demoted to a `<field>_unverified` companion property; they can never displace, reorder or delete an evidence value. `files_touched` is stricter still — the attested key carries **evidence only**, so a model's claim about what it modified lives under the companion key and nowhere else; `files_read` and `commands_run` take the union, with the companion naming which members are unattested. Deliberate limits: files are **not** inferred from shell commands (parsing shell for writes is guesswork wearing a deterministic badge), a diff header is only read as the full `---`/`+++`/`@@` triple (a lone marker matches hunk *body* lines — a removed `-- sql comment` arrives as `--- sql comment`), and exit codes wait for a `TraceStep.result` payload contract to read them from.

### LLM Client Abstraction (`src/trellis/llm/`)

Provider-agnostic protocols: `LLMClient`, `EmbedderClient`. Reference implementations for OpenAI / Anthropic live in `trellis.llm.providers` behind `[llm-openai]` / `[llm-anthropic]` optional extras so core stays dependency-free. See [`docs/design/adr-llm-client-abstraction.md`](docs/design/adr-llm-client-abstraction.md).

### Feedback path — EventLog authoritative, JSONL audit log

Context curation runs a variation → selection loop: extraction produces candidate context items, feedback grades them, the advisory + learning loops propagate or suppress. The **EventLog is the single authoritative path** for that loop. `trellis.feedback.recording.record_feedback()` always appends a `PackFeedback` row to `pack_feedback.jsonl` and, when given an `event_log` kwarg, also emits a `FEEDBACK_RECORDED` event; the file is durable, the event is what drives behavior.

**Not every feedback surface calls that function**, so the JSONL file is not a record of all feedback. Two families of surface exist, and they emit different `FEEDBACK_RECORDED` payloads:

| Surface | Route | JSONL row | Event payload |
|---|---|---|---|
| MCP `record_feedback` (#287), REST `POST /packs/{pack_id}/feedback`, `TrellisClient.record_feedback` (which posts to that route) | `PackFeedback.from_agent_signal` → `recording.record_feedback` | **yes** | Full `PackFeedback.to_event_payload()` — `feedback_id`, `rating`, `success`, `helpful_item_ids` / `unhelpful_item_ids` / `followed_advisory_ids`, `intent_family` |
| CLI `trellis curate feedback --pack-id`, REST `POST /feedback` | `Command(FEEDBACK_RECORD)` → `MutationExecutor` → `FeedbackRecordHandler` | **no** | `{target_id, rating, comment, success}` plus `pack_id` when the caller named one — no `feedback_id`, no item attribution |

Before #287 the MCP tool was in the second family, which is why `trellis admin reconcile-feedback` had nothing to reconcile on a deployment whose only grader was Claude Code. It is now in the first: one call writes both sinks, and the emit fails soft so a sink outage degrades to a file row the reconcile replays (the `record_feedback` tool in `src/trellis/mcp/server.py`).

The governed path forwards `pack_id` **and** derives `success` from `rating` (backlog A4). `POST /feedback` had accepted a `pack_id` since the wire DTO was written — "Link feedback to a context pack" — and the route always put it in `command.args`, but the handler emitted three fixed keys and dropped it, so the link the caller asked for was never made and `join_pack_feedback` (which reads `payload["pack_id"]` and skips events without it) could never see the event. `success` has to ride with it: `_join_one` resolves an absent `success` to `"failure"`, so forwarding the join key alone would have made a governed `rating=0.9` join as a *failed* delivery — a wrong signal reaching the loop, which is worse than the unjoinable silence it replaced. The derivation is `SUCCESS_RATING_THRESHOLD`, the same one `PackFeedback.from_agent_signal` applies, so the two families cannot disagree about what a rating means.

**Attribution is measured over the population that can carry it.** `attribution_rate` divides by every feedback event, and that denominator mixes a caller who named a pack and cited nothing (a real, fixable loss) with a caller grading work no pack informed (nothing to cite, because there is no pack on the other side of the join). Measured on the reference deployment over 30 days: 37 events, 12 attributed (0.32) — but 19 of the 25 unattributed graded work for which no pack had been assembled anywhere in the preceding six hours, and among the 13 that *did* name a pack the citation rate was 12/13 = 0.92. `ServeAttributionReport` therefore reports `pack_targeted_feedback` / `pack_targeted_attributed` / `pack_attribution_rate` / `untargeted_feedback` alongside the unchanged headline. `attribution_rate` keeps its original denominator on purpose: DoD-3 reads it, and a metric that improves because its denominator was quietly narrowed is the failure the decomposition exists to expose. `TRELLIS_REQUIRE_PACK_ATTRIBUTION` (default **off**, `write_config.py`) makes MCP `record_feedback` reject an uncited pack-targeted call and hand back the ids the pack actually served; it fails open whenever the pack resolves to nothing citable, and never touches trace-level feedback.

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

## Autonomous / swarm work

Picking up implementation work as an autonomous agent? Read
[`docs/design/swarm-handoff.md`](docs/design/swarm-handoff.md) — the autonomy contract,
the merge gate (**green against *current* `main`**), the traps that have already cost
time, and the dependency-ordered queue. Decisions taken and pending live in
[`docs/design/decision-ledger.md`](docs/design/decision-ledger.md); the work items are in
[`docs/design/autonomous-backlog.md`](docs/design/autonomous-backlog.md).

**Test-coverage caveat worth knowing before you trust a green run:** local `make test`
deselects 635 tests (`postgres`, `pgvector`, `neo`, `arcadedb`, `live`, `slow`), so
a green local run says nothing about any cloud backend. What CI actually covers:

- **On pull requests** (`tests.yml`): SQLite backends only. Every `postgres` / `pgvector` /
  `neo` / `arcadedb` test is deselected — for the *graph* and *vector* contracts alike.
- **On push to `main`** (`live-infra.yml`): the Postgres + Neo4j graph contracts, the
  Postgres document / trace / event-log contracts, and — since
  [#345](https://github.com/ronsse/trellis-ai/issues/345) — the **pgvector vector
  contract**, against `pgvector/pgvector:pg16` and `neo4j:2025.12` service containers.
- **Nowhere at all:** the ArcadeDB graph contract (`test_arcadedb_graph_contract.py`).
  ArcadeDB is the *blessed* graph + vector substrate and its contract has no service
  container in any workflow ([#351](https://github.com/ronsse/trellis-ai/issues/351)).
  Nor does anything under `tests/unit/stores/` outside `contracts/` — `live-infra.yml` names
  paths, not markers, so 59 Postgres-marked tests there are simply unwired (they pass; they
  have just never been run by CI). Sweeping the whole directory in does not work yet:
  `test_neo4j_vector.py::TestQuery` issues AuraDB-only Cypher that self-hosted
  `neo4j:2025.12` cannot parse, and unlike the e2e suite it has no capability probe
  ([#356](https://github.com/ronsse/trellis-ai/issues/356)).

Note the shape of the #345 defect, because it is the one this repo keeps producing: the
pgvector contract had *never executed anywhere*, because its fixture called `_conn` as an
attribute — which it stopped being when #84 pooled connections — so the one env combination
that would have run it (`TRELLIS_TEST_PG_DSN` **and** `TRELLIS_TEST_PGVECTOR=1`) errored
instead, and nobody ran that combination. `tests/unit/stores/test_pgvector.py` carried the
identical dead fixture. All 47 now pass, against pgvector 0.4.2 and 0.5.0 both.

Running them locally needs a Postgres whose database already has `CREATE EXTENSION vector`.
`PgVectorStore` cannot create it: `register_vector` is the pool's `on_connect` hook, so on a
database without the extension every pooled connection fails and `pool.wait()` raises
`PoolTimeout` after 30s — per test — long before `_init_schema`'s
`CREATE EXTENSION IF NOT EXISTS vector` is reached
([#350](https://github.com/ronsse/trellis-ai/issues/350)).

## Product docs

- `docs/PRD.md` — product thesis, adopter profiles, component disposition
- `docs/design/implementation-roadmap.md` — authoritative single-page roadmap; §3.H is the Productionization milestone (the 2026-07-11 edit-set has been applied into it and removed)
