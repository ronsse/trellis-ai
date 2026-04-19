# TODO — Growth & Adoption

## Recently Completed (2026-04-13 → 2026-04-15)

The dual-loop evolution sprint landed Phases 1–4 plus follow-ons. Items previously open under Tiered Retrieval, Graph Modeling, and Advisory work are now live:

- **Dual-loop Phases 1–4:** decision trail on `PACK_ASSEMBLED`, `Advisory`/`AdvisoryStore`/`AdvisoryGenerator`, advisories embedded in pack responses, `run_advisory_fitness_loop()` with confidence adjustment and suppression.
- **Tiered Context Retrieval Phases 1–3:** `RetrievalAffinity` enum + `ContentTags.retrieval_affinity`, `SectionRequest` / `PackSection` / `SectionedPack`, `PackBuilder.build_sectioned()`, `TierMapper` with default heuristics, `format_sectioned_pack_as_markdown()`, `get_objective_context` / `get_task_context` MCP tools, SDK methods + `POST /api/v1/packs/sectioned`, tiered-retrieval + classification adoption guides.
- **Graph Modeling Phase 2 (node_role):** `NodeRole` enum (STRUCTURAL/SEMANTIC/CURATED), `node_role` + `GenerationSpec` on `Entity` with model validator, mutation handler validation, PackBuilder filter for structural nodes.
- **Cross-cutting:** configurable retrieval budgets per tool/domain (`BudgetConfig`), `pack_id` header in pack markdown, `item_id` / `advisory_id` citation footer, `record_feedback` accepting `helpful_item_ids` / `unhelpful_item_ids` / `followed_advisory_ids` for element-level fitness attribution.
- **Sprint A — Close the feedback loop (2026-04-15):** exponential recency decay in retrieval strategies, `save_memory` content-hash dedup + `EventType.MEMORY_STORED`, session-aware dedup in `get_context` / `PackBuilder` with `session_id` propagated through MCP tools and `PACK_ASSEMBLED` payload, `trellis analyze pack-sections` CLI + `trellis.retrieve.pack_sections` module.
- **Sprint B — Unblock platform integration (2026-04-15):** `POST /api/v1/ingest/bulk` endpoint, entity/edge types relaxed to open `str`.
- **Sprint D — Credibility signals (2026-04-15):** split `ci.yml` into `lint.yml`/`typecheck.yml`/`tests.yml` + README badges (lint, typecheck, tests, MIT license), `py.typed` marker, `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `SECURITY.md`, `make check` aggregate target.
- **Sprint E — Deterministic capability (2026-04-15):** `Reranker` protocol + `RRFReranker` + `MMRReranker` integrated into `PackBuilder`, `MinHashIndex` (128-perm LSH) for fuzzy dedup in `save_memory`, `trellis admin graph-health` diagnostic command with role/type/leaf/orphan analysis + CI exit codes.

See commits `ed3e2fd`, `f5760f6`, `66320ae`, `aaf7ccd`, `837dde6`, `d0bfd40`, `4cb78b9`.

## Next Up (re-prioritized 2026-04-15)

Grouped by coherent sprint, ordered by ROI given current state. Each item links into its detailed entry below.

### Sprint A — Close the feedback loop (finish dual-loop sprint cleanly) — COMPLETE (2026-04-15)

The advisory fitness loop just shipped but we have no operational visibility and the upstream signal quality is still weak. Close these before opening new fronts.

- [x] **`trellis analyze pack-sections` CLI** — observability for the sectioned packs and advisories that just shipped. Reads `PACK_ASSEMBLED` events with `entity_type="sectioned_pack"`, reports per-section packs-count / avg-items / unique-items / empty-rate, flags frequently-empty sections. Module: `trellis.retrieve.pack_sections`.
- [x] **Recency decay in PackBuilder scoring (P0)** — exponential decay with floor (`base * (floor + (1-floor) * 0.5^(age/half_life))`) in `KeywordSearch` / `SemanticSearch` / `GraphSearch`; half-life 30 days, floor 0.3.
- [x] **`save_memory` dedup + event emission (P0)** — content-hash dedup via `DocumentStore.get_by_hash`; emits new `EventType.MEMORY_STORED` with doc_id, content_length, content_hash, metadata.
- [x] **Session-aware dedup in `get_context` (P0)** — `session_id` plumbed through MCP tools and `PackBuilder.build{,_sectioned}()`; items served within 60-minute window are excluded (flat pack tracks as `RejectedItem(reason="session_dedup")`). `session_id` is now a first-class field on `Pack` / `SectionedPack` and appears in `PACK_ASSEMBLED` payload.
- [~] ~~**`trellis analyze advisory-effectiveness` CLI**~~ — already shipped in commit `66320ae` (see `src/trellis_cli/analyze.py:295`). Struck from Sprint A.

### Sprint B — Unblock platform integration

- [x] **`POST /api/v1/ingest/bulk` endpoint (P0)** — shipped 2026-04-15. Accepts `entities` + `edges` + `aliases` in one request; entities and edges flow through `MutationExecutor` (audit events, per-item idempotency key); aliases route directly to `graph_store.upsert_alias` (no alias mutation operation exists yet). Strategies: `continue_on_error` (default, backfill-friendly), `stop_on_error` (halts across groups), `sequential`. Processing order entities → edges → aliases. Response reports per-group counts (`total`/`succeeded`/`failed`/`rejected`/`duplicates`/`skipped`) + per-item results. See [BulkIngestRequest](src/trellis_api/models.py) and [ingest_bulk()](src/trellis_api/routes/ingest.py). Unblocks trellis-platform EKS deploy.
- [x] **EntityType/EdgeKind enum role clarification** — resolved 2026-04-15. `Entity.entity_type` and `Edge.edge_kind` relaxed from StrEnum to open `str` (previously silently rejected domain-specific values like `uc_table`, `dbt_model`, contradicting CLAUDE.md's "any string" claim). Enums retained in `schemas/enums.py` as named constants for well-known agent-centric values. Locked in by `TestEntityTypeIsOpenString` and `TestEdgeKindIsOpenString`. Docs updated: `schemas.md`, `playbooks.md`, `client-layer-inventory.md`.

### Sprint C — LLM abstraction decision (ADR, not code) — COMPLETE (2026-04-15)

- [x] **ADR: LLM Client Abstraction** — decided Option B: Protocol in core, reference implementations behind optional extras. `LLMClient` protocol (replaces `LLMCallable`) with `Message`/`LLMResponse`/`TokenUsage` types in `trellis.llm`. `EmbedderClient` protocol for future async paths. `CrossEncoderClient` protocol for reranking. Reference implementations for OpenAI and Anthropic SDKs behind `[llm-openai]`/`[llm-anthropic]` optional extras. **Clean cut:** `LLMCallable` had no production consumers — deleted outright; `EnrichmentService`/`PrecedentMiner` migrated; `EnrichmentResult` gained `usage: TokenUsage | None`. Phases 1–3 of the ADR are implemented; Phase 4 (Sprint F features) remains. See [`docs/design/adr-llm-client-abstraction.md`](docs/design/adr-llm-client-abstraction.md).

### Sprint D — Credibility signals (interstitial, low cognitive load)

Do in parallel with Sprints A–C; none of these block or conflict. See [Repository Polish & Credibility Signals](#repository-polish--credibility-signals) for the full list. Start with:

- [x] Split `ci.yml` into `lint.yml` / `typecheck.yml` / `tests.yml` + badges — done 2026-04-15.
- [x] `py.typed` marker + License badge — done 2026-04-15.
- [ ] Enable GitHub Discussions + topics
- [x] CONTRIBUTING.md + Code of Conduct + SECURITY.md — done 2026-04-15.

### Sprint E — Deterministic capability (safe to start pre-LLM-decision) — COMPLETE (2026-04-15)

- [x] **Reranker protocol + RRF/MMR** — `Reranker` ABC in `retrieve/rerankers/base.py`, `RRFReranker` (reciprocal rank fusion across strategy lists), `MMRReranker` (maximal marginal relevance with text-overlap diversity). PackBuilder accepts optional `reranker` parameter, applied after dedup/filters, before budget enforcement. Telemetry includes reranker name. 23 tests.
- [x] **MinHash/LSH deterministic dedup stage** — `MinHashIndex` in `classify/dedup/minhash.py` with configurable permutations (128), LSH banding (16 bands), Jaccard threshold (0.85), entropy filter. Integrated into `save_memory` MCP tool as stage 2 dedup (after exact hash, before store). 16 tests.
- [x] **`trellis admin graph-health` diagnostic** — role distribution, top entity types, leaf-node analysis, orphan detection. Warning signals for structural dominance, type imbalance, semantic leaves, missing curated nodes. Supports `--entity-type`, `--role`, `--format json`. Exit codes 0/1/2 for CI. 10 tests.

### Sprint F — Unblocked (Sprints A + C complete)

- **LLM Client Protocols + Reference Implementations (Phases 1–3 of ADR)** — DONE. `trellis.llm` package shipped: `LLMClient`, `EmbedderClient`, `CrossEncoderClient` protocols + types + OpenAI/Anthropic providers + `EnrichmentService`/`PrecedentMiner` migrated. `LLMCallable` deleted (no production callers). See [`docs/design/adr-llm-client-abstraction.md`](docs/design/adr-llm-client-abstraction.md).
- **Pack Quality Evaluation Framework** — cleaner to build now that Sprint A observability reveals where real gaps are.
- **Tiered Extraction Pipeline — Phase 1 DONE (2026-04-15).** Shipped `trellis.extract` core (`Extractor` Protocol, `ExtractorTier`, `ExtractionContext`, `ExtractionDispatcher` with event telemetry, `ExtractorRegistry`, `JSONRulesExtractor`) + `EntityDraft`/`EdgeDraft`/`ExtractionResult` schemas + `EXTRACTION_DISPATCHED` event. `DbtManifestExtractor` and `OpenLineageExtractor` ported to `trellis_workers.extract` (pure — no I/O, no store writes). Old `IngestionWorker` base + `dbt.py` + `openlineage.py` deleted. CLI `trellis ingest dbt-manifest` / `openlineage` now route through `MutationExecutor` (governed writes).
- **Tiered Extraction Pipeline — Phase 2 DONE (2026-04-15 → 2026-04-16).** Steps 1–9 all shipped. See [Tiered Extraction Pipeline — Phase 2 Plan](#tiered-extraction-pipeline--phase-2-plan) below for the per-step record. Scope broadened beyond the original LLM-only framing to lead with deterministic wins first — preserved the "deterministic options for the write" differentiator (LLM-tier extractors are additive, opt-in (`allow_llm_fallback=False` by default), never a silent replacement for rules). Future Tiered-Extraction work (graduation tracking, LLM-assisted dedup, self-learning classification) should branch off its own plan section.
- **Graph Modeling Phase 4 (`trellis curate list/show/regenerate/edit/diff`)** — note: `trellis curate` namespace exists but hosts mutation commands (promote/link/label/entity/feedback), not the curation-workflow commands in the Phase 4 plan. Either rename one or the other; decide before building.

### Deprioritized / deferred

- ~~**`LLMExtractor`, `SaveMemoryExtractor`, `HybridJSONExtractor`**~~ — **SHIPPED** 2026-04-16 in Phase 2 Steps 4–6 (composition won — factory `build_save_memory_extractor` instead of a dedicated class).
- **LLM-based rerankers + `CrossEncoderClient`** — [unblocked: `CrossEncoderClient` protocol ships today — add implementations alongside rerank features]
- ~~**Prompt library pattern**~~ — **SHIPPED (minimal)** 2026-04-16 in Phase 2 Step 3 — `PromptTemplate` + `render()` + two templates. Full Jinja2-based registry still deferred; three prompts against `str.format` is below the complexity threshold.
- **LLM-assisted dedup for `save_memory`** — [unblocked: `EmbedderClient` protocol ships today; needs async embedding path before wiring]
- **Self-Learning Classification** — depends on sufficient `ENRICHMENT_COMPLETED` volume; revisit once LLM tier is live.
- **Native graph backend (Neo4j/Memgraph)** — P3, only when graphs exceed ~100K edges and need 4+ hop traversal.
- **More Framework Integrations (CrewAI, AutoGen)** — defer until LangGraph integration validates the pattern.

### Housekeeping

- [x] Normalize `trellis` → `trellis` / `trellis` throughout this TODO (done 2026-04-15; badge URLs use `trellis-ai` as the PyPI package name).
- [x] Fold "Observation Pipeline" into "Tiered Extraction Pipeline" — done 2026-04-15 via cross-reference note on the Observation Pipeline section.

---

## Tiered Extraction Pipeline — Phase 2 Plan

Started 2026-04-15. **Deterministic-first sequencing** — the core value proposition is that writes have deterministic options; LLM paths are additive, never substituted silently. All extractors route drafts through `MutationExecutor` (governed writes). The dispatcher already enforces tier priority `DETERMINISTIC > HYBRID > LLM` with `allow_llm_fallback=False` as the default.

### Step 1 — `JSONRulesExtractor` ancestor edges — DONE (2026-04-15)

Closes the Phase 1 promise (*"Nested / ancestor-tracking edges will follow in Phase 2"* from [json_rules.py](src/trellis/extract/json_rules.py)).

- `EdgeRule` has two modes: `source_field` (field-reference, existing) or `via_ancestor=True` (new). `@model_validator` enforces exactly one.
- `_walk` now yields `(item, trail)` where trail is the tuple of wildcard-matched ancestors, outer→inner.
- `_apply_ancestor_edge_rule` uses object-identity matching on the trail; closest matching ancestor wins.
- Internal `_EntityMatch` dataclass carries the trail alongside each draft.
- 5 new tests: column→table nesting, closest-ancestor selection, missing-ancestor skip, both validator failure modes. All 41 extract tests pass, ruff + mypy clean.

### Step 2 — `AliasMatchExtractor` (deterministic, `save_memory` path) — DONE (2026-04-16)

Shipped in [`src/trellis/extract/alias_match.py`](src/trellis/extract/alias_match.py). Tier=`DETERMINISTIC`, supported_sources=`["save_memory"]`.

- Default mention pattern matches `@word` (configurable via `mention_pattern`).
- Injected `alias_resolver: Callable[[str], list[str]]` keeps `trellis.extract` decoupled from `GraphStore`. The MCP wiring layer (Step 7) provides the concrete hookup.
- Accepts either plain `str` or `dict` with `{"text", "doc_id"}`. Without a `doc_id`, no edges are emitted.
- **No `EntityDraft` for matches** — the resolver returns IDs of entities that already exist; emitting drafts with `entity_type="unknown"` would clobber real metadata. Only `EdgeDraft` (`<doc_id>-[mentions]->(<entity_id>)`) is produced.
- **Ambiguous mentions (resolver returns >1) skip silently** — deterministic extractor refuses to guess; the mention goes to residue for the LLM-tier fallback.
- Residue strategy: no mentions → full text; partial resolution → `{"text", "unmatched_mentions": [...]}`; all resolved → `None`.
- 18 tests cover contract, input shapes, resolution paths, customization, metadata. All 59 extract tests pass.
- Resolved the open question on the resolver signature: stayed with `Callable[[str], list[str]]` (per-alias lookup, extractor does the parsing) rather than `(text) -> list[(id, span)]` (resolver does both). The simpler signature composes better with whatever name-lookup the MCP layer builds.

### Step 3 — Prompt scaffolding — DONE (2026-04-16)

Shipped in [`src/trellis/extract/prompts/`](src/trellis/extract/prompts/):

- `prompts/base.py` — `PromptTemplate` (frozen dataclass: `name`, `version`, `system`, `user_template`) + `render()` helper that normalizes optional inputs (`entity_type_hints`, `edge_kind_hints`, `domain`, `source_system`) into template variables so templates can reference them unconditionally.
- `prompts/extraction.py` — `ENTITY_EXTRACTION_V1` (generic entity+edge extraction) and `MEMORY_EXTRACTION_V1` (mention-focused, no-edges mode for `save_memory`).
- No Jinja2 dependency — plain `str.format` as planned. `expected_schema` embedded in the system prompt text instead of as a separate field (YAGNI). 13 tests.

### Step 4 — `LLMExtractor` — DONE (2026-04-16)

Shipped in [`src/trellis/extract/llm.py`](src/trellis/extract/llm.py). Tier=`LLM`.

- Consumes `LLMClient.generate()`; defaults to `ENTITY_EXTRACTION_V1` but accepts any `PromptTemplate`.
- Budget-aware: `context.max_llm_calls == 0` short-circuits before any network call, returning `llm_calls=0`, `tokens_used=0`, `unparsed_residue=text`.
- Tolerant JSON parsing via `_parse_json_tolerant`: strips markdown fences (``` / ```json), tries full-string parse, falls back to widest-brace-span extraction for models that emit prose around the JSON. Bare arrays are lifted to `{"entities": [...], "edges": []}`. Failure → `entities=[], edges=[], unparsed_residue=response.content, overall_confidence=0.0` — never raises.
- Populates `llm_calls=1` and `tokens_used=response.usage.total_tokens if response.usage else 0`.
- Per-draft validation: entities without `entity_type` or `name` are dropped; edges without all three of `source_id`/`target_id`/`edge_kind` are dropped; confidences are clamped to `[0.0, 1.0]`; non-dict `properties` defaults to `{}`. Malformed individual entries don't tank the whole result.
- All drafts get `node_role=NodeRole.SEMANTIC` (LLM output is never structural — enforces the graph-modeling invariant).
- 23 tests.

### Step 5 — `HybridJSONExtractor` — DONE (2026-04-16)

Shipped in [`src/trellis/extract/hybrid.py`](src/trellis/extract/hybrid.py). Tier=`HYBRID`. Composes any two `Extractor` instances.

- **Deterministic-first is load-bearing.** Skips the LLM stage entirely when `det.overall_confidence >= threshold` and `det.unparsed_residue is None`.
- **Budget gates are explicit.** Because HYBRID tier isn't gated by `allow_llm_fallback` at the dispatcher, the wrapper enforces it itself. `allow_llm_fallback=False`, `max_llm_calls=0`, and missing context all skip LLM with a structlog warning (never silent).
- **Deterministic wins in merges.** Entity dedup key = `(entity_type, entity_id or name)`; edge dedup key = `(source_id, target_id, edge_kind)`. Confidence = min of the two stages when both contribute.
- **Residue selector composes cleanly.** Default selector promotes string residue to `{doc_id, text}` when the original input had a `doc_id`, preserves dict residue with `doc_id` merged in, and falls through to `raw_input` for unknown shapes. Overridable via `residue_selector: Callable[[Any, ExtractionResult], Any]`.
- **Provenance preserved.** `extractor_used` and `provenance.extractor_name` both become `"hybrid(<det_name>+<llm_name>)"` so effectiveness analysis can attribute output correctly.
- 15 tests covering short-circuit, LLM-for-residue (including doc_id preservation), all three budget gates, entity/edge dedup, confidence math, provenance composition.

### Step 6 — Decide: `SaveMemoryExtractor` class vs composed `HybridJSONExtractor(AliasMatch, LLM)` — DONE (2026-04-16)

**Decision: composition wins.** No new class. Shipped [`src/trellis/extract/save_memory.py`](src/trellis/extract/save_memory.py) — a single `build_save_memory_extractor()` factory that returns a preconfigured `HybridJSONExtractor(AliasMatchExtractor, LLMExtractor)` with `MEMORY_EXTRACTION_V1` prompt and `max_tokens=400` default.

Why composition was enough:
- `AliasMatchExtractor` already had `supported_sources=["save_memory"]`, configurable `edge_kind`, and produces a residue shape (`{"text", "unmatched_mentions"}`) that `HybridJSONExtractor`'s default selector handles cleanly — doc_id injection included.
- `LLMExtractor` already accepts a custom `PromptTemplate`, `model`, and `max_tokens`. `MEMORY_EXTRACTION_V1` slots in with zero new code.
- `HybridJSONExtractor`'s merge, budget gates, and provenance work as-is.

Everything memory-specific was config, not behavior. Promoting to a dedicated class would have added inheritance without new capability.

Side cleanup during Step 6: dropped `ClassVar` annotations on `tier` attributes across `AliasMatchExtractor`, `LLMExtractor`, and `HybridJSONExtractor` so they structurally conform to the `Extractor` Protocol (mypy surfaced the mismatch when the factory composed them). 8 new tests; 118 extract tests total.

### Step 7 — Wire extraction into MCP `save_memory` — DONE (2026-04-16)

Feature-flagged via environment variable `TRELLIS_ENABLE_MEMORY_EXTRACTION` (accepts `1`/`true`/`yes`/`on`). Default is off — no behavior change for existing deployments. Shipped sync-in-process for v1; the deferred EXTRACTION_REQUESTED event + worker path is still on the table if latency becomes an issue.

What landed:

- **`trellis.extract.commands.result_to_batch`** — new public helper that converts `ExtractionResult` drafts into a `CommandBatch` with `ENTITY_CREATE` and `LINK_CREATE` commands. Replaces the private `_result_to_batch` that lived in `trellis_cli/ingest.py`; both CLI and MCP now share the same code path, so the "drafts never touch a store" invariant has exactly one enforcement point.
- **`trellis/mcp/server.py` `_get_memory_extractor()`** — lazy, cached builder that checks the env flag, constructs an `LLMClient` from env (OpenAI preferred via `OPENAI_API_KEY`, Anthropic fallback via `ANTHROPIC_API_KEY`), builds a name-based alias resolver against the graph store, and assembles the extractor via `build_save_memory_extractor`. Failure on any step (flag off, no key, SDK not installed, etc.) returns `None` and caches that result so we don't retry on every save.
- **`save_memory` hook** — after the document is stored and `MEMORY_STORED` is emitted, calls `_run_memory_extraction()` which runs the extractor, converts drafts via `result_to_batch`, and executes the batch through a `MutationExecutor` with `create_curate_handlers(registry)` + `requested_by="save_memory_extractor"`. All failures are logged at debug level and swallowed — `save_memory` success never depends on extraction.
- **Dispatch context** — `ExtractionContext(allow_llm_fallback=True, max_llm_calls=1, max_tokens=400)` per plan. `source_hint="save_memory"` so the dispatcher picks this extractor when the factory registers multiple.

Tests:

- 8 tests for `result_to_batch` (entity conversion, edge conversion, ordering, strategy, requested_by propagation)
- 4 feature-flag tests for the MCP path: flag off (default), flag on without LLM client, flag on with injected fake LLM (extractor builds + LLM fires once), extraction failure non-fatal
- CLI `ingest_dbt_manifest` / `ingest_openlineage` refactored to use the new `result_to_batch`; 17 CLI tests still green
- Full run: 191 tests pass across mcp + extract + cli/ingest scopes. Lint + mypy clean on the changed scope.

### Step 8 — `LLMClient` construction hook in `StoreRegistry` — DONE (2026-04-16)

Split into four sub-agent briefs; all shipped. See [`docs/plans/2026-04-16-phase2-step8-subtasks.md`](docs/plans/2026-04-16-phase2-step8-subtasks.md) for the full work orders.

- **8A — DONE.** `StoreRegistry.build_llm_client()` + `build_embedder_client()` read an `llm:` block from `~/.config/trellis/config.yaml`. Returns `None` when unconfigured; never raises. Lazy SDK imports keep core dependency-free. Masked-key logging. Supports `api_key_env` (preferred) or `api_key` literal with the documented fallback: if both are set and the env var is unset, the literal is used.
- **8B — DONE.** MCP `save_memory` now calls `_build_llm_client(registry)` which prefers `registry.build_llm_client()` and falls back to `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` env vars for backward compat. Instance-level `monkeypatch` of `registry.build_llm_client` is the supported test-injection pattern.
- **8C — DONE.** `trellis admin check-extractors [--format json]` diagnostic reports extractor readiness with CI-friendly exit codes: `0` READY, `1` WARN (non-fatal suboptimal states), `2` BLOCKED (flag on but no LLM obtainable — the case where extraction would silently skip).
- **8D — DONE.** Docs: config template extension in `trellis admin init`, new "Configuring LLM extraction" playbook entry, ADR Phase 4 close-out, TODO close-out (this change).

### Step 9 — Docs + ADR + TODO close-out — DONE (2026-04-16)

Folded into 8D — single commit covers the config template, playbook, ADR, and TODO updates.

- [x] "Configuring LLM extraction" added as Playbook 12 in [docs/agent-guide/playbooks.md](docs/agent-guide/playbooks.md).
- [x] Phase 4 of [adr-llm-client-abstraction.md](docs/design/adr-llm-client-abstraction.md) lists shipped items + remaining open work.
- [x] Matching items struck from "Deprioritized / deferred" above.

### Deliberately out of scope for Phase 2

- Jinja2 prompt library with versioning (str.format is enough for three prompts).
- Streaming LLM responses (ADR §2.4).
- Structured output / `response_model` (ADR §2.4).
- `CrossEncoderClient` implementations (separate backlog item).
- Graduation tracking (LLM→Hybrid→Deterministic auto-promotion) — needs effectiveness data we don't have yet.
- `SCD Type 2` for extracted entities — out of scope; existing graph versioning applies once drafts land.

### Resumption checklist

When picking this back up:
1. Re-read this section end-to-end.
2. `git log --oneline origin/main..HEAD` — confirms which commits have / haven't been pushed.
3. Next action: **Phase 2 is complete.** Steps 1–9 all shipped (2026-04-15 → 2026-04-16). Future Tiered-Extraction work — LLM-assisted dedup, self-learning classification, graduation tracking, LLM-based rerankers — should branch off their own plan section rather than extending this one.
4. Run `pytest tests/unit/extract/ tests/unit/llm/ tests/unit/feedback/ tests/unit/cli/test_admin.py tests/unit/mcp/ -q` to confirm the Phase 2 surface is still green before editing.

---

## Client Boundary & Extension Contracts — Phase 1 Plan

Started 2026-04-17. **Problem:** `trellis_sdk` is a thin facade over direct core imports — 11+ `from trellis import ...` calls in [client.py](src/trellis_sdk/client.py) force client codebases (Unity Catalog integration, dbt sync, any domain-specific extractor package) to reach past the SDK into core types. API DTOs inherit `TrellisModel` from core, so schema changes ripple to the wire. No version handshake, no static OpenAPI, no plugin discovery for runtime extensions. Migration work is harder than it needs to be and scaling to N client packages is constrained by the tight coupling.

**Goal:** Establish four narrow contracts — versioned HTTP API, frozen wire DTOs, HTTP-only SDK with a client-side `extract` module, and an entry-points plugin loader — so client packages can ship their own data models and extensions without importing `trellis.*`.

### Step 1 — API version handshake + static OpenAPI in CI — DONE (2026-04-17)

Cheap, unblocks everything else. No new business logic.

- [x] `GET /api/version` returning `{api_major: 1, api_minor: 0, api_version, wire_schema, sdk_min, package_version, deprecations: []}`. Route mounted at [src/trellis_api/routes/version.py](src/trellis_api/routes/version.py) — deliberately outside the `/api/v1` prefix because it describes which major is running. No store access, safe to keep public when auth lands.
- [x] `Deprecation` + `Sunset` + `Link` response headers (RFC 9745 + RFC 8594). Registry-based emitter at [src/trellis_api/deprecation.py](src/trellis_api/deprecation.py) — add a deprecation by inserting one entry into `ROUTE_DEPRECATIONS`; both the headers *and* the `/api/version` payload pick it up automatically. Handlers call `apply_deprecation_headers(response, path)` unconditionally (no-op when not deprecated).
- [x] `docs/api/v1.yaml` — static OpenAPI spec committed at [docs/api/v1.yaml](docs/api/v1.yaml). Regenerated by [scripts/generate_openapi.py](scripts/generate_openapi.py). `make openapi` regenerates locally; `make openapi-check` is the CI drift-check. New workflow [.github/workflows/openapi.yml](.github/workflows/openapi.yml) fails PRs that change the live schema without updating the committed spec.
- [x] `api_major`, `api_minor`, `wire_schema`, `sdk_min` constants in [src/trellis/api_version.py](src/trellis/api_version.py) — single source of truth imported by the route AND the CLI. `trellis admin version [--format json]` added in [src/trellis_cli/admin.py](src/trellis_cli/admin.py).
- 11 new tests (`tests/unit/api/test_version.py`, `tests/unit/cli/test_admin_version.py`); full api + sdk suites (66 tests) green; ruff + mypy clean on the changed scope.

### Step 2 — `trellis_wire` package (frozen wire DTOs) — DONE (2026-04-18)

Prerequisite for clean Step 3/4. Translation layer at the API edge, not in every route.

- [x] **New package [src/trellis_wire/](src/trellis_wire/)** — zero dependencies on `trellis.*` core. Enforced by a structural test (`TestWirePackageIsolation`) that AST-walks every file in the package and fails if any `import trellis` or `from trellis.*` appears.
- [x] **Wire DTOs for every request/response** — all 27 DTOs from the old `trellis_api/models.py` ported to [src/trellis_wire/dtos.py](src/trellis_wire/dtos.py). Class names preserved for drop-in compatibility. Request DTOs inherit `WireRequestModel` (`extra="forbid"` + `frozen=True`); response DTOs inherit `WireModel` (`extra="forbid"`, mutable). **Frozen-response scope note:** `trellis_api/routes/ingest.py` currently builds bulk-ingest responses by incrementing counters on nested models (`response.entities.skipped += 1`). Fully freezing responses requires refactoring the route's accumulator pattern — tracked as a follow-up, not a blocker for Step 2. The critical contract guarantee (`extra="forbid"`) ships now universally.
- [x] **Translators:** [src/trellis/wire/translate.py](src/trellis/wire/translate.py) — `trellis.wire` is the core-adjacent module that knows about both sides; `trellis_wire` stays pure. Only enum translators exist for now (DTOs are wire-shape natively); DTO-level translators will be added when a route needs the same conversion twice.
- [x] **Parity tests** — [tests/unit/wire/test_parity.py](tests/unit/wire/test_parity.py) — 17 tests verify (a) same members, (b) same string values, (c) round-trip equality via translators, (d) structural isolation (no core imports in wire package). Parametrized across both `BatchStrategy` and `NodeRole`.
- [x] **Backward-compat shim:** [src/trellis_api/models.py](src/trellis_api/models.py) is now a pure re-export of the 24 DTOs from `trellis_wire`. Zero route changes needed (6 route files untouched). New code should `from trellis_wire import ...` directly; shim stays indefinitely.
- [x] **Packaging:** `pyproject.toml` wheel packages updated to ship `src/trellis_wire` + `py.typed` marker. The wire package is now independently importable by client code.
- **Decision captured:** codegen wire DTOs deferred. Hand-written keeps schema changes deliberate. Revisit only if the DTO count grows past ~30 or if multi-language clients appear.

**Verification:** 17 new wire parity tests pass; full `tests/unit/api/ + tests/unit/sdk/ + tests/unit/wire/` suites (83 tests) green; ruff + mypy clean on all new scope; OpenAPI spec regenerates identically (the one `test_delete_policy` flake and 5 Windows-path integration failures all confirmed pre-existing on `main`).

### Step 3 — HTTP-only SDK + `trellis.testing` in-memory shim — DONE (2026-04-18)

Drop dual-mode. Single code path for all clients.

- [x] **Local-mode branch removed.** `base_url=None` no longer instantiates a `StoreRegistry` in-process. `TrellisClient` / `AsyncTrellisClient` constructors require `base_url=` OR an injected `http=` client (for tests); passing neither raises `ValueError`. Full rewrite at [src/trellis_sdk/client.py](src/trellis_sdk/client.py) and [src/trellis_sdk/async_client.py](src/trellis_sdk/async_client.py).
- [x] **SDK has zero `trellis.*` imports.** Enforced structurally by [tests/unit/sdk/test_isolation.py](tests/unit/sdk/test_isolation.py) which AST-walks every `.py` in `src/trellis_sdk/` and fails on any `from trellis.*` / `import trellis` it finds. Skill formatters ported to [src/trellis_sdk/_format.py](src/trellis_sdk/_format.py) (~80 pure-string lines, no Advisory Pydantic model required); the core `trellis.retrieve.formatters` stays in place for MCP use.
- [x] **`trellis.testing` shim.** New package [src/trellis/testing/](src/trellis/testing/) with `in_memory_client()` / `in_memory_async_client()` context managers. Sync variant uses Starlette's `TestClient` (an `httpx.Client` subclass with ASGI-portal support); async variant uses raw `httpx.AsyncClient` + `httpx.ASGITransport`. Drops the network entirely — microsecond round-trips. Migrated all `tests/unit/sdk/*` fixtures to use it.
- [x] **Typed exception hierarchy.** [src/trellis_sdk/exceptions.py](src/trellis_sdk/exceptions.py) ships `TrellisError` → `TrellisAPIError`, `TrellisRateLimitError` (parses `Retry-After` header as seconds-int or HTTP-date, clamps past dates to 0), `TrellisVersionMismatchError`. 404s deliberately *don't* raise — many SDK methods translate to `None`; the caller decides.
- [x] **Version handshake.** `TrellisClient` / `AsyncTrellisClient` hit `GET /api/version` lazily on first request. Mismatched `api_major` or SDK below `sdk_min` raises `TrellisVersionMismatchError`; server minor older than SDK logs a warning (features may be missing, but safe to continue). Disable via `verify_version=False` — the testing shim does this by default so tests don't need to mock `/api/version`.
- [x] **Bounded concurrency.** `AsyncTrellisClient` ships `asyncio.Semaphore(max_concurrency)` (default 16) guarding every request. `max_concurrency` is a constructor kwarg and exposed as a property. Handshake uses a separate `asyncio.Lock` so parallel first-calls don't all hit `/api/version`.
- [x] **Example updated.** [examples/sdk_local_demo.py](examples/sdk_local_demo.py) now uses `trellis.testing.in_memory_client` — the lightest path without a separate server process.

**Tests:**

- 9 sync client tests (ingest, retrieve, curate, pack, construction)
- 12 async client tests (same + concurrency + max_concurrency propagation)
- 7 skill tests (unchanged behaviour, new fixture)
- 14 HTTP helper tests (5 × `raise_for_status`, 6 × Retry-After parsing, 4 × handshake, 2 × integration)
- 1 structural isolation test
- Totals: **43 SDK tests; 101 pass across SDK + API + wire suites; full `tests/unit/` is 1352 passing** (the 1 failure is the pre-existing Windows `\var\` path issue on `main`).

**Out-of-scope issues flagged as follow-ups:**

- `StoreRegistry._get()` has a thread-safety bug — two concurrent cache-miss threads can both instantiate the same store. Discovered when stress-testing concurrent reads through `in_memory_async_client`; workaround is `max_concurrency=1` in one test. Fix = wrap cache access in `threading.Lock`. Separate task spawned.
- Response DTO freezing (from Step 2 carry-over) still deferred.
- Skills module still has utility — if clients want to build their own, they can import `trellis_sdk._format` directly (private but stable).

### Step 4 — `trellis_sdk.extract` module + `POST /api/v1/extract/drafts` — DONE (2026-04-18)

Client-side extractor contract. Unity Catalog, dbt, etc. live in their own packages and submit drafts over HTTP.

- [x] **Wire DTOs:** [src/trellis_wire/extract.py](src/trellis_wire/extract.py) ships `ExtractorTier`, `EntityDraft`, `EdgeDraft`, `ExtractionBatch`, `DraftSubmissionRequest`, `DraftSubmissionResult`. Request DTOs frozen per the Step 2 convention. `generation_spec` deliberately omitted from wire `EntityDraft` — curated-node provenance flows through a dedicated curation path, not extraction.
- [x] **SDK surface:** [src/trellis_sdk/extract/__init__.py](src/trellis_sdk/extract/__init__.py) re-exports the wire DTOs plus a `DraftExtractor` Protocol (structural — no inheritance required). Client packages depend on `trellis_sdk` alone and get everything transitively.
- [x] **Client method:** `TrellisClient.submit_drafts(batch, *, strategy, requested_by, idempotency_key)` and async variant. Sends `Idempotency-Key` header when set; header wins over `batch.idempotency_key` when both are present.
- [x] **Server route:** [src/trellis_api/routes/extract.py](src/trellis_api/routes/extract.py) at `POST /api/v1/extract/drafts`. Wire batch → core `ExtractionResult` (via new translator in [src/trellis/wire/translate.py](src/trellis/wire/translate.py)) → `result_to_batch` → `MutationExecutor`. Zero new mutation logic; same bridge the CLI ingest commands and MCP `save_memory` use. Audit trail records `f"{extractor_name}@{extractor_version}"` in `requested_by` and `provenance.source_hint = batch.source`.
- [x] **Idempotency stamping:** server stamps `{key}:{i}` on every command in the batch so per-entity dedup survives replays at the executor layer.
- [x] **Draft translators:** `entity_draft_to_core`, `edge_draft_to_core`, `extraction_batch_to_core_result` in `trellis.wire.translate`. Three translators cover the wire → core direction; reverse direction not needed yet (responses don't include draft payloads).
- [x] **Testing shim updated:** [src/trellis/testing/inmemory.py](src/trellis/testing/inmemory.py) wires the new router into `_build_app` so the extract route is reachable from in-process tests.
- [x] **Example client package:** [examples/trellis_example_extractor/](examples/trellis_example_extractor/) with `reader.py`, `types.py`, `sync.py`, `README.md`. Demonstrates namespaced types (`example.widget`, `example.contains`), `idempotency_key` derived from a snapshot ID, and two submission modes (real server vs. in-memory shim). `PYTHONPATH=src python -m examples.trellis_example_extractor.sync` runs end-to-end against an in-memory fixture and prints the submission report.
- [x] **Playbook:** "Playbook 13: Building a client extractor package" in [docs/agent-guide/playbooks.md](docs/agent-guide/playbooks.md) — fork instructions, namespace-choice guidance, the 8-step shape (types → extractor → submit → idempotency → operational shape → testing → when to graduate to a server plugin).
- [x] **OpenAPI spec updated:** `docs/api/v1.yaml` now includes the `/api/v1/extract/drafts` route + 6 new schemas. Grew from 52KB → 60KB.

**Tests:** 35 new tests across three files (`tests/unit/wire/test_extract.py` — 13; `tests/unit/sdk/test_extract.py` — 7; `tests/unit/api/test_extract_route.py` — 15). Full boundary suite is 136 green (was 101 after Step 3). SDK isolation test still passes — `trellis_sdk.extract` only depends on `trellis_wire`.

**Decisions captured:**
- **Chose a new route (`/extract/drafts`) over reusing `/ingest/bulk`.** The new route captures extractor identity + version in the audit trail automatically via `source_hint` + `requested_by`; `/ingest/bulk` would require the caller to wire that up by convention. Small duplication, clean telemetry — the right call per the original plan.
- **Schema registry deferred.** `properties: dict[str, Any]` stays as the escape hatch for domain-specific fields. Revisit once ≥3 client extractor packages exist and the patterns are visible.
- **No reverse (core → wire) draft translator yet.** Responses don't return draft payloads; if a future `GET /api/v1/extract/batches/{id}` surfaces submitted drafts, add the reverse translator then.

### Step 5 — Entry-points plugin loader (runtime extensions)

Only for things that *must* run in the API process: custom store backends, classifiers, rerankers, policy gates, search strategies.

- [ ] `trellis.plugins.discover(group) -> dict[name, (module, class)]` helper using `importlib.metadata.entry_points`. One function, used by all registries.
- [ ] Extend [StoreRegistry._BUILTIN_BACKENDS](src/trellis/stores/registry.py) with discovered plugins, merged at init. Plugin shadowing of a built-in logs a warning and built-in wins (override via `TRELLIS_PLUGIN_OVERRIDE=1`).
- [ ] Mirror pattern in `ExtractorRegistry`, `ClassifierPipeline`, reranker/policy/strategy registries.
- [ ] Entry-point groups defined and documented:
    - `trellis.stores.{trace,document,graph,vector,event_log,blob}`
    - `trellis.extractors`
    - `trellis.classifiers`
    - `trellis.rerankers`
    - `trellis.policies`
    - `trellis.search_strategies`
    - `trellis.llm.providers` — `LLMClient` implementations (e.g. `trellis-llm-bedrock`, `trellis-llm-vertex`, `trellis-llm-vllm-native`). Config selects by name: `llm.provider: bedrock`. **Note:** OpenAI-compatible OSS servers (Ollama, vLLM, LiteLLM, LM Studio, TGI) already work today by pointing the built-in `openai` provider at a local `base_url` — no plugin needed. This group is for wire protocols the OpenAI SDK can't speak.
    - `trellis.llm.embedders` — `EmbedderClient` implementations (same rationale; e.g. `trellis-embed-instructor`, `trellis-embed-cohere`).
- [ ] `trellis admin check-plugins [--format json]` — lists discovered plugins, status (LOADED / BLOCKED / SHADOWED), reason on failure. Exit codes 0/1/2 matching `check-extractors`.
- [ ] Plugin authors declare a supported `trellis-abi` version in their package metadata; loader warns on mismatch. (Actual enforcement: defer — use `py.typed` + Protocol contracts as the real guarantee for now.)
- [ ] ADR: [`docs/design/adr-plugin-contract.md`](docs/design/adr-plugin-contract.md) — covers groups, ABI policy, shadowing rules, deprecation process.

### Step 6 — MCP as a separate narrower contract

MCP ≠ REST. Keep MCP the ~8 agent-shaped tools; REST stays the programmatic surface.

- [ ] `mcp_tools_version` field added to `/api/version` response, versioned independently from `api_major`.
- [ ] MCP tools import from `trellis_wire`, not `trellis.schemas.*`. Formatters (`trellis.retrieve.formatters`) either move to wire or get a wire-level wrapper.
- [ ] Document which capabilities are MCP-only vs. REST-only vs. both.

### Deliberately out of scope for Phase 1

- Schema registry (Option B from the extension discussion) — use `EntityDraft.properties: dict[str, Any]` escape hatch for now. Revisit once we have ≥3 client extractor packages and can see the patterns.
- Codegen'd wire DTOs or OpenAPI-generated client SDKs (TypeScript, Go). Unblock later if multi-language clients appear.
- Plugin marketplace / discovery site. Just `pip install trellis-unity-catalog` for now.
- Remote extractors (extractor-as-a-microservice). Client-side extraction + `submit_drafts` covers the current use cases.

### Ordering rationale

1 → 2 → (3 ∥ 5) → 4 → 6. Step 1 is cheap and standalone. Step 2 is a prerequisite for clean 3 and 4. Steps 3 and 5 are independent and can be parallelized across two contributors. Step 4 depends on Step 3's SDK and Step 2's wire DTOs being in place. Step 6 is a cleanup pass after 2 and 5 land.

### Resumption checklist

When picking this back up:
1. Re-read this section end-to-end.
2. `git log --oneline origin/main..HEAD` — confirms pushed/unpushed commits.
3. Next action: start with Step 1 (API version handshake + OpenAPI in CI). It's the cheapest piece and unblocks review of later structural changes.
4. Before editing, run `pytest tests/unit/ -q` and confirm current green baseline.

---

## In Progress

### PyPI Publishing
- [x] Rename package to `trellis-ai` in pyproject.toml
- [x] Add classifiers, keywords, authors, project URLs
- [x] Add hatch-vcs fallback version
- [x] Verify `python -m build` succeeds
- [ ] Create GitHub repo at `trellis-ai/trellis-ai` (or update URLs to actual repo)
- [ ] Tag `v0.1.0` and push
- [ ] Publish to PyPI: `python -m twine upload dist/*`
- [ ] Verify `pip install trellis-ai` works from a clean venv
- [ ] Set up GitHub Actions for automated PyPI publishing on tag push

### Framework Integration (LangGraph)
- [ ] Create `integrations/langgraph/` with XPG as a tool provider
- [ ] Wrap MCP tools as LangGraph `Tool` instances
- [ ] Add example: agent retrieves context → executes → saves trace
- [ ] Write README with setup instructions
- [ ] Open PR upstream to LangGraph's integrations/community tools

## Backlog

### Demo & Content
- [ ] Record 60-second demo GIF showing the retrieve → act → record feedback loop
- [ ] Write blog post: "Why AI agents keep making the same mistakes" (target HN, r/LocalLLaMA)
- [ ] Create starter knowledge pack (common deployment patterns, debugging playbooks) so new users get value on day one

### Discoverability
- [ ] Submit to awesome-mcp-servers list
- [ ] Submit to awesome-llm-agents / awesome-ai-tools lists
- [ ] Publish ClawHub skill: `clawhub publish integrations/openclaw/`

### Developer Experience
- [ ] Add hosted playground (Streamlit or Gradio) showing graph visualization and search
- [ ] Write persona-targeted docs: "XPG for platform teams", "XPG for solo devs with Claude Code"

### Research — Agent & Compaction Improvements
- [x] Review https://barazany.dev/blog/claude-codes-compaction-engine for compaction strategies
- [x] Review https://github.com/instructkr/claude-code for agent orchestration and memory patterns
- [x] Write up findings: [docs/research/compaction-and-agent-patterns.md](docs/research/compaction-and-agent-patterns.md)
- [x] **P0:** Add recency decay to PackBuilder relevance scoring (`retrieve/strategies.py`) — shipped 2026-04-15 (Sprint A)
- [x] **P0:** Session-aware dedup in `get_context` (track recently served items per session_id) — shipped 2026-04-15 (Sprint A)
- [x] **P0:** Dedup check + event emission in `save_memory` (`mcp/server.py`) — shipped 2026-04-15 (Sprint A) with `EventType.MEMORY_STORED`
- [ ] **P1:** Wire `save_memory` into enrichment pipeline (`DocumentEnrichmentWorker`)
- [ ] **P1:** `DocumentPromotionWorker` — route high-value docs to precedents/graph
- [ ] **P1:** TTL metadata + `DocumentRetentionWorker` for auto-expiry
- [ ] **P1:** Tier 1 compaction worker — strip old tool outputs from traces >30 days
- [ ] **P2:** `get_detail(item_id)` MCP tool for lazy/deferred content loading
- [ ] **P2:** Tier 2 compaction — fuzzy document dedup via similarity threshold
- [ ] **P3:** Tier 3 compaction — LLM-driven trace consolidation with summary merging
- [ ] **P3:** SCD Type 2 versioning for documents (matching graph store pattern)

### Tiered Context Retrieval — Sectioned Pack Assembly

> **Problem discovered (2026-04-05):** In fd-poc's multi-agent pipeline, each agent constructs a narrow, task-scoped retrieval intent ("SQL generation for layer casino_sessions"). This misses strategic context (who owns this data? what already exists? what failed before?) because keyword retrieval only surfaces content with vocabulary overlap to the technical task. The user's business objective gets decomposed into a structured spec before context retrieval happens, so no agent ever asks the *strategic* question.
>
> **Solution:** Tiered pack assembly. The graph provides sectioned retrieval where each section targets a different *kind* of knowledge with its own budget and retrieval strategy. Applications define which sections each agent phase needs. Some sections (objective/domain) are assembled once and shared across all phases; others (tactical/entity) are assembled per-step.

#### The Four Retrieval Tiers

| Tier | What It Provides | When Assembled | Content Sources |
|------|-----------------|----------------|-----------------|
| **Objective** | Business intent, domain conventions, ownership, governance, what already exists | Once per workflow, from user's original request | Precedents, constraints, ownership entities, domain_intent docs |
| **Strategic** | Patterns, prior art, design decisions, materialization rules | Once during planning, enriched after discovery | Patterns, procedures, successful traces, decisions |
| **Tactical** | Column schemas, code examples, known pitfalls for this exact task | Per-step, uses outputs from prior steps | Entity metadata, code docs, error-resolutions, recent traces |
| **Reflective** | Quality constraints, compliance rules, comparison against original objective | After execution, before validation | Constraints, failed traces, precedent applicability |

#### Implementation: Three-Layer Classification

The tier isn't a label on the content — it's a filter on the retrieval. Classification happens at three layers:

**Layer 1 — Deterministic (ingest time).** Already exists via `StructuralClassifier`, `KeywordDomainClassifier`, `SourceSystemClassifier`. Tags `content_type`, `scope`, `signal_quality`, `domain`.

**Layer 2 — Heuristic rules (retrieval time).** New. Maps content properties to tier eligibility:
- Objective: `scope IN (universal, org)` AND `content_type IN (constraint, decision, documentation)`, or entity_type is OWNER/TEAM/PRECEDENT
- Strategic: `content_type IN (pattern, procedure, decision)`, or entity is DBT_MODEL/PIPELINE_DEFINITION, or successful trace with matching domain
- Tactical: `content_type IN (code, configuration, error-resolution)`, or entity is UC_TABLE/UC_COLUMN
- Reflective: `content_type IN (constraint, discovery)`, or failed traces, or precedent with applicability match

These rules are **configurable per application** — shipped as defaults, overridable.

**Layer 3 — LLM-enriched (ingest time, async).** New `retrieval_affinity` classification facet on `ContentTags`:
```python
class RetrievalAffinity(StrEnum):
    DOMAIN_KNOWLEDGE = "domain_knowledge"     # business concepts, ownership, governance
    TECHNICAL_PATTERN = "technical_pattern"    # how-to, SQL patterns, conventions
    OPERATIONAL_CONTEXT = "operational"        # traces, incidents, error history
    REFERENCE_DATA = "reference"              # entity metadata, schemas, configs
```
Classified by the existing `LLMFacetClassifier` pipeline when deterministic confidence is low. Content can have multiple affinities (a "lookback windows" doc is both domain_knowledge and technical_pattern).

#### Implementation Tasks

##### Phase 1: Schemas & Classification (P0) — ✅ DONE (2026-04)

> Landed in `src/trellis/schemas/classification.py` and the classifier pipeline. Kept for historical reference; see [Recently Completed](#recently-completed-2026-04-13--2026-04-15).

- [x] **Add `retrieval_affinity` to `ContentTags`** (`schemas/classification.py:52`)
- [x] **Add `RetrievalAffinity` enum** (`schemas/classification.py:28-33`)
- [x] **Extend deterministic classifiers** to populate `retrieval_affinity`
- [x] **Extend `LLMFacetClassifier`** to classify ambiguous content into affinities
- [x] **Tests**: unit tests for each classifier extension, round-trip serialization of new field

##### Phase 2: Sectioned Pack Assembly (P0) — ✅ DONE (2026-04)

> Landed in `src/trellis/schemas/pack.py`, `src/trellis/retrieve/pack_builder.py`, and `src/trellis/retrieve/tier_mapping.py`. Kept for historical reference.

- [x] **`SectionRequest` schema** (`schemas/pack.py:92-107`)
  ```python
  class SectionRequest(TrellisModel):
      name: str                                    # e.g., "domain", "patterns", "entities"
      retrieval_affinities: list[RetrievalAffinity] | None = None  # filter by affinity
      content_types: list[ContentType] | None = None               # filter by content type
      scopes: list[Scope] | None = None                            # filter by scope
      entity_ids: list[str] | None = None                          # seed entities for graph search
      max_tokens: int = 2000                                       # per-section budget
      max_items: int = 10                                          # per-section cap
      strategies: list[str] | None = None                          # override which strategies run
  ```

- [x] **`SectionedPack` schema** (`schemas/pack.py:121-150`)
- [x] **`PackBuilder.build_sectioned()`** method (`retrieve/pack_builder.py:189-268`)
- [x] **Tier-mapping rules / `TierMapper`** (`retrieve/tier_mapping.py:54-146`, defaults at `:26-51`)
- [x] **Tests**: sectioned pack assembly with mock strategies, per-section budgeting, cross-section dedup, tier mapping rules

##### Phase 3: Formatted Output & MCP Tools (P1) — ✅ DONE (2026-04)

- [x] **`format_sectioned_pack_as_markdown()`** (`retrieve/formatters.py:278`)
- [x] **New `get_objective_context` MCP tool** (`mcp/server.py:525+`)
- [x] **New `get_task_context` MCP tool** (`mcp/server.py:631+`)
- [x] **SDK methods**: `assemble_sectioned_pack`, `get_objective_context`, `get_task_context` (sync + async) + `POST /api/v1/packs/sectioned` endpoint
- [ ] **Update flat `get_context` MCP tool** to accept `sections` param for backward-compatible sectioned retrieval. (Still flat; `get_objective_context` / `get_task_context` are the new entry points.)

##### Phase 4: CLI & Documentation (P1) — PARTIAL

- [x] **`trellis analyze pack-sections`** CLI command — shipped 2026-04-15 (Sprint A). Reports per-section packs-count / avg-items / unique-items / empty-rate from `PACK_ASSEMBLED` events, flags frequently-empty sections.
- [x] **Adoption guide**: [docs/agent-guide/tiered-context-retrieval.md](docs/agent-guide/tiered-context-retrieval.md)
- [x] **Classification guide**: [docs/agent-guide/enriching-for-retrieval.md](docs/agent-guide/enriching-for-retrieval.md)
- [ ] **Migration guide**: update existing `get_context` / `search` docs to reference sectioned retrieval as the recommended pattern for multi-agent workflows

### Pack Quality Evaluation Framework (interactive testing)

Generic pack evaluation that works on synthetic scenarios *and* real event log data. Extends the existing `effectiveness.py` / `token_usage.py` pattern with richer scoring dimensions.

#### Background: What We Built & Learned (fd-poc, 2026-04-05)

Built a project-specific pack analysis in `fd-data-architecture-poc/src/fd_poc/trellis/pack_analysis.py` (40 passing tests). The implementation:

1. **5 domain scenarios** (sportsbook, snowplow, casino, reg_reporting, reference) with synthetic entity fixtures and knowledge base items loaded from `trellis-platform/knowledge/`.
2. **5 scoring dimensions**: completeness (coverage checklist hit rate), relevance (mean PackItem.relevance_score), noise ratio (cross-domain items / total), coverage breadth (distinct content categories / expected), token efficiency (useful tokens / total).
3. **2 use-case weight profiles** — pipeline generation (technical depth) vs business domain context (organizational breadth) — same pack, different weights, meaningfully different scores.
4. **Synthetic pack assembly** — reads knowledge .md files (frontmatter + body), splits multi-section docs, loads precedent .yaml, builds entity fixtures, scores via keyword overlap (Jaccard + tag bonus), applies domain filter + budget. Mirrors `ContextProvider` logic without live stores.

**Key findings from initial analysis:**
- **Keyword retrieval misses ownership/governance docs** when intent is technical ("Generate SQL for dedup..."). The vocabulary gap between technical intents and organizational metadata means agents never see who owns the data. This validates the need for either (a) mandatory context injection for ownership metadata, (b) semantic search, or (c) structured pack sections by content_type.
- **Relevance scores are moderate (0.30-0.43)** even for correct items — pure keyword overlap between specific task intents and broad convention docs is inherently limited. Embedding-based semantic search would significantly improve recall.
- **Coverage breadth is weakest for simple domains** (reference: 0.60) — fewer domain-specific precedents and patterns available.
- **Noise ratio is 0.000 everywhere** — domain filter works well (knowledge items tagged `domain: all` pass through), but the metric only catches metadata-tagged cross-domain contamination, not topical irrelevance.
- **Pipeline gen scores higher (0.805) than business context (0.758)** — knowledge base is stronger on technical patterns than organizational context.

**What's domain-specific (stays in fd-poc):** scenario definitions, FanDuel entity fixtures, knowledge base loading, use-case weight profiles.

**What's generic (belongs here):** scoring dimensions, evaluator protocol, report structures, CLI surface, pack diff/replay, strategy ablation, event integration.

#### Implementation Plan

**Architecture:** Follow the `effectiveness.py` pattern exactly. New `evaluate.py` module in `retrieve/`, new `QualityReport` extending `TrellisModel`, new `trellis analyze pack-quality` CLI command.

##### P1: Core Evaluation Engine

- [ ] **`retrieve/evaluate.py`** — Generic pack quality scorer.

  ```python
  class QualityDimension(Protocol):
      name: str
      def score(self, pack: Pack, ground_truth: EvaluationScenario) -> float: ...

  class EvaluationScenario(TrellisModel):
      """Downstream projects define these; core library scores them."""
      intent: str
      domain: str | None
      seed_entity_ids: list[str]
      required_coverage: list[str]  # keywords that MUST appear in pack
      expected_categories: list[str]  # content types that should be present
      metadata: dict[str, Any]  # use-case-specific context

  class QualityReport(TrellisModel):
      """Extends the EffectivenessReport pattern."""
      scenario_name: str
      pack_id: str | None
      dimensions: dict[str, float]  # dimension_name -> score [0, 1]
      weighted_score: float
      missing_coverage: list[str]
      findings: list[str]  # actionable observations
  ```

  Built-in dimensions (all deterministic, no LLM):
  - `CompletenessScorer` — fraction of `required_coverage` keywords found in pack items
  - `RelevanceScorer` — mean `relevance_score` across included items
  - `NoiseScorer` — fraction of items with mismatched `domain_system` metadata
  - `BreadthScorer` — distinct content categories present vs `expected_categories`
  - `EfficiencyScorer` — token budget utilization (useful tokens / total)

  Extension point: implement `QualityDimension` protocol for custom scorers (e.g., LLM-as-judge for semantic relevance, embedding similarity to reference outputs).

- [ ] **`EvaluationProfile`** — Named weight sets for different use cases.

  ```python
  class EvaluationProfile(TrellisModel):
      name: str  # e.g., "code_generation", "domain_understanding", "investigation"
      weights: dict[str, float]  # dimension_name -> weight (must sum to 1.0)
  ```

  Ship 2 built-in profiles (from fd-poc learnings):
  - `code_generation`: completeness=0.35, relevance=0.25, noise=0.20, breadth=0.10, efficiency=0.10
  - `domain_context`: completeness=0.20, relevance=0.20, noise=0.15, breadth=0.30, efficiency=0.15

- [ ] **`trellis analyze pack-quality`** CLI command.

  Two modes:
  1. **Event log mode**: score packs from `PACK_ASSEMBLED` events in the event log. Joins with scenario metadata from a YAML fixture file.
  2. **Scenario mode**: load `EvaluationScenario` fixtures from a YAML file, assemble packs via `PackBuilder`, score them. No live event log needed.

  Output: Rich table (dimension scores per scenario) + JSON mode. Follow `analyze context-effectiveness` pattern.

##### P2: Interactive Pack Testing

- [ ] **Pack replay/diff** — `trellis analyze pack-diff --left <config_a.yaml> --right <config_b.yaml>`.

  Given two pack configurations (different budgets, strategies, domain filters, or scoring weights), assemble both packs for the same scenario and render:
  - Items only in left, only in right, in both (with rank delta)
  - Score deltas per dimension
  - Token budget utilization comparison

  This is the core "interactive testing" tool — lets you tune retrieval parameters and see exactly what changes.

- [ ] **Strategy ablation** — `trellis analyze strategy-ablation --scenario <file.yaml>`.

  For each scenario, assemble packs with:
  - keyword-only (KeywordSearch)
  - graph-only (GraphSearch)
  - semantic-only (SemanticSearch)
  - combined (all strategies)

  Report which strategy contributes the most unique high-value items per scenario type. Identifies where to invest retrieval improvements.

- [ ] **Live quality gating** — emit `PACK_QUALITY_SCORED` event alongside `PACK_ASSEMBLED`.

  When `PackBuilder.build()` runs, optionally evaluate the pack against a scenario (if one is registered for the current agent_id/skill_id) and emit quality scores as an event. Downstream consumers can alert on low-quality packs before they reach the agent.

  Integration point: `PackBuilder.__init__(evaluator=None)` — optional evaluator that runs post-assembly.

##### P3: Learning Loop Integration

- [ ] **Feedback-driven dimension calibration** — correlate quality dimension scores with `FEEDBACK_RECORDED` outcomes to learn which dimensions actually predict task success. If `noise_ratio` doesn't correlate with failure, reduce its weight. If `breadth` strongly predicts success, boost it.

- [ ] **Automatic scenario generation from traces** — mine `PACK_ASSEMBLED` + `FEEDBACK_RECORDED` event pairs to auto-generate `EvaluationScenario` fixtures. Successful traces become ground-truth scenarios; failed traces become regression tests.

#### Relationship to Existing Infrastructure

| Existing Module | Relationship | Integration Point |
|----------------|--------------|-------------------|
| `effectiveness.py` | Complementary. Effectiveness measures pack→outcome correlation over time. Quality evaluation measures pack properties at assembly time. | Share `QualityReport` → `EffectivenessReport` pipeline: quality scores become features for effectiveness analysis. |
| `token_usage.py` | Subset. Token efficiency dimension replaces ad-hoc budget tracking with a principled score. | `EfficiencyScorer` can consume `TOKEN_TRACKED` events for calibration. |
| `ClassifierPipeline` | Upstream. Classification quality directly affects pack content quality. | `BreadthScorer` depends on correct `content_type` tags from classification. Low breadth may indicate classification gaps, not retrieval gaps. |
| `importance.py` | Upstream. Importance scores feed `relevance_score` in strategies. | `RelevanceScorer` measures the downstream effect of importance weighting. Ablation can compare packs with/without importance boost. |
| `formatters.py` | Downstream. Formatters consume packs that evaluation scored. | Future: `format_pack_as_markdown()` could annotate items with their quality contribution (e.g., "this item covers the ownership gap"). |
| `PackBuilder` | Core dependency. Evaluation scores packs that PackBuilder assembles. | Optional `evaluator` parameter on `PackBuilder` for inline quality gating. |

### Unity Catalog Backfill & Platform Integration
- [x] **P0:** `POST /api/v1/ingest/bulk` endpoint accepting entities + edges + aliases in one request — shipped 2026-04-15 (see Sprint B entry above)
- [ ] **P1:** Clarify EntityType/EdgeKind enum role — either remove from schemas or document as "well-known types" (storage layer already accepts any string)
- [ ] **P2:** Add `GET /api/v1/sync/status` for recent backfill run tracking
- [ ] Reference: trellis-platform is at `fd-data-architecture-poc/trellis-platform/` — runners already work against GraphStore directly, need API mode for EKS deployment

### Storage Backend Guidance & 3-Layer Architecture
- [ ] Add a "Choosing Backends" section to README and/or agent guide that explains the 3-layer architecture and why each tool exists:
  - **PostgreSQL for graph** — recursive CTEs for transitive dependency queries (`WITH RECURSIVE deps AS ...`), ACID transactions for atomic entity+edge+alias upserts, SCD Type 2 temporal versioning (`valid_from`/`valid_to` with `as_of` queries), referential integrity on edges, MVCC for concurrent writers (API + backfill scripts simultaneously)
  - **S3/local for blobs** — large artifacts, embeddings, file content
  - **LanceDB (or pgvector) for vector search** — ANN similarity, document retrieval, semantic search only
- [ ] Document what LanceDB/SQLite CANNOT do and when to upgrade:
  - No transactions (crash mid-upsert of entity+edges = partial state)
  - No recursive CTEs (transitive graph traversal requires pulling edges into Python — works at ~170K edges, painful beyond)
  - No SCD Type 2 temporal queries (no `as_of` time-travel)
  - No referential integrity (dangling edge references)
  - No concurrent writer isolation (single-machine only)
  - SQLite is fine for dev/single-agent use; PostgreSQL is required for production multi-writer deployments
- [ ] Add a decision matrix to docs: "If you need X, use Y backend" table
- [ ] Consider adding a startup warning when graph store is SQLite but edge count exceeds a threshold (e.g., >50K) suggesting Postgres migration

### Configurable Ingestion Rules & Population Framework
Currently all content routing is hardcoded — each worker/tool decides where content goes with fixed metadata. There's no configurable framework for defining "source X → store Y with metadata Z and lifecycle W."

- [ ] **Design: IngestionRule schema** — declarative rules in `config.yaml` that control content routing. Each rule specifies:
  - `source` — where content comes from (used for matching)
  - `content_type` — what shape of content (entity, document, observation, trace)
  - `destination` — which store(s) receive it (graph_store, document_store, trace_store)
  - `metadata_defaults` — metadata injected on every item from this source
  - `lifecycle` — retention, staleness, refresh policies
  - `enrich` — whether to trigger ClassifierPipeline + EnrichmentService
  - `promote` — whether high-importance items auto-promote to precedents/graph
  - `filters` — conditions for accepting/rejecting content
- [ ] **Provide `config.yaml.sample`** with annotated examples and reasoning for each field — application-specific rules belong in consumer repos, not in the core library
- [ ] **Population surface guidelines** — document which store handles which content shape and WHY:
  - **GraphStore**: structured entities with typed relationships (nodes + edges). Use when content has identity, hierarchy, or dependency structure. Supports transitive queries, temporal versioning, referential integrity.
  - **DocumentStore**: searchable text content. Use for descriptions, observations, notes — anything retrieved by keyword or semantic search. Attach `content_type`, `domain`, `memory_type` metadata for lifecycle and retrieval filtering.
  - **TraceStore**: immutable execution records. Use for agent work with intent, steps, outcome. Append-only by design — never update, only add feedback.
  - **VectorStore**: embedding vectors for semantic search. Populated by enrichment workers, not directly by ingestion. Complements DocumentStore, not a replacement.
  - **EventLog**: append-only audit trail. Never populated directly — emitted by the system on mutations and feedback.
- [ ] **IngestionRouter class** — evaluates rules at ingestion time to determine destination(s), metadata injection, and lifecycle policy. Replaces hardcoded routing in individual workers.
- [ ] **Backfill vs incremental mode** — bulk loading needs different behavior:
  - Dedup strategy (skip-if-exists vs upsert vs version)
  - Batch size / transaction boundaries
  - Whether to trigger enrichment (skip during bulk load, enable after)
  - Progress tracking and resumability (checkpoint after N items)
- [ ] **Extend policy gates to cover ingestion** — currently policies only gate mutations. Add ingestion-level policies for accept/reject, redaction, and rate limiting.
- [ ] **Refactor existing workers** to use IngestionRouter instead of hardcoded paths

### Workflow Integration Hooks (context in, traces out, results back)

Proven patterns from trellis-platform/fd-poc that should be generalized into the core library. These enable any workflow engine (not just fd-poc) to integrate with the experience graph.

Reference implementation: `fd-data-architecture-poc/src/fd_poc/agents/{graph_context,trace_recorder,result_feedback}.py`

#### Context Injection (pre-dispatch)
- [ ] **`ContextInjector` class** — generic pre-dispatch hook that assembles graph context for a worker/agent. Takes entity IDs + intent, returns formatted context (markdown or structured).
  - Uses `PackBuilder.build()` internally (keyword + graph + trace search)
  - Configurable token budget and format (markdown for prompt injection, JSON for structured tools)
  - Falls back gracefully with WARNING when API unreachable (never blocks the pipeline)
  - Should support both SDK client mode (remote HTTP) and direct registry mode (local stores)
- [ ] **Example: `assemble_worker_context(entity_ids, intent, max_tokens) -> str`** in SDK
- [ ] **Config-driven**: which stores to query (graph, documents, traces, precedents) and token budget per store

#### Trace Recording (post-dispatch)
- [ ] **`WorkflowTraceRecorder` class** — generic post-dispatch hook that records execution traces.
  - Maps workflow step metadata (skill name, duration, status, artifacts) to the Trace schema
  - Records on success AND failure — failure traces are valuable for learning ("what went wrong when generating SQL for this table?")
  - Fire-and-forget with WARNING on failure (never blocks pipeline)
  - Should accept a `trace_template` for workflow-specific fields
- [ ] **Example: `record_step_trace(skill, run_id, status, duration_ms, entity_ids) -> trace_id`** in SDK
- [ ] **Feedback wiring**: after trace is recorded, enable `record_feedback(trace_id, success, notes)` to close the loop

#### Result Feedback (post-execution)
- [ ] **`ResultFeedbackLoop` class** — generic post-execution hook that records evidence linking results to entities.
  - On success: creates DOCUMENT entity + DESCRIBED_BY edge from target entity → result doc
  - On failure: trace alone captures the failure (no document created)
  - Supports storing full content in blob store (S3) with summary in document store
  - Fire-and-forget with WARNING on failure
- [ ] **Example: `record_result(target_entity, result_summary, full_content, success) -> None`** in SDK

#### MCP Server as Sidecar
- [ ] **Document the pattern**: run `trellis.mcp.server` alongside `trellis_api` so workers can query the graph at runtime via MCP tools
  - Workers get `get_context`, `search`, `get_graph`, `get_lessons` tools
  - `.mcp.json` entry: `{"trellis": {"command": "python3", "args": ["-m", "trellis.mcp.server"], "env": {"TRELLIS_PG_DSN": "..."}}}`
- [ ] **Consider**: should the API server also serve MCP (single process) or keep them separate?

### Observation Pipeline (wire save_memory into learning) — FOLDED INTO TIERED EXTRACTION

> **Consolidation note (2026-04-15):** This section's `save_memory` → enrichment → promotion flow overlaps with the "Tiered Extraction Pipeline" section below. The `DocumentEnrichmentWorker` described here is the consumer of `SaveMemoryExtractor` (Tiered Extraction Phase 2). Treat the items below as the **Phase 3 integration tail** of Tiered Extraction, not as a parallel workstream. The `dedup + event emission` item is split out to Sprint A because it does not depend on the LLM extractor path.

- [x] **[Sprint A, P0]** Dedup + event emission in `save_memory` — shipped 2026-04-15. Content-hash dedup via `DocumentStore.get_by_hash`; emits `EventType.MEMORY_STORED`.
- [ ] Design doc: `save_memory` as observation ingestion funnel (see `docs/research/compaction-and-agent-patterns.md`) — folded into Tiered Extraction Phase 5 docs.
- [ ] `DocumentEnrichmentWorker` (classify + enrich new documents) — [blocked: Tiered Extraction Phase 3 integration + LLMClient ADR]
- [ ] `DocumentPromotionWorker` (route high-value docs to precedents/graph) — promoted precedents are **curated nodes** (`node_role=CURATED`, `generation_spec.generator_name="precedent_promotion"`). Depends on Phase 4 `trellis curate regenerate` machinery.
- [ ] TTL metadata + `DocumentRetentionWorker` for auto-expiry

### Self-Learning Classification (reduce LLM cost over time)
- [ ] Investigate: train a lightweight model (logistic regression, small transformer, or decision tree) on accumulated LLM classification outputs to progressively replace LLM calls
- [ ] The ClassifierPipeline already has a deterministic-first / LLM-fallback architecture — the idea is to **grow the deterministic tier** by learning from LLM outputs over time
- [ ] Approach options:
  - **Scikit-learn model** trained on (content features → content_type, domain, signal_quality) from EnrichmentService outputs. Retrain periodically as a batch job. Simplest, most interpretable.
  - **Embedding + kNN classifier** using the vector store — classify new items by nearest-neighbor vote from previously LLM-classified items. Leverages existing infrastructure.
  - **Active learning loop** — only send items to the LLM when the deterministic classifier's confidence is below threshold (already how ClassifierPipeline works), but feed LLM results back to retrain the deterministic classifiers.
- [ ] Key metric: LLM call rate over time should decrease as the deterministic classifiers absorb learned patterns
- [ ] Data pipeline: `ENRICHMENT_COMPLETED` events → training set extraction → model fit → deploy as a new `LearnedClassifier` conforming to the `Classifier` Protocol
- [ ] Consider: could also apply to importance scoring (`auto_importance`) — learn what the LLM considers important from historical scores

### Tiered Extraction Pipeline (deterministic-first, LLM-augmented, grows over time)

> **Design insight (2026-04-11, in response to the Graphiti deep audit):** The Graphiti comparison initially framed "LLM-driven extraction" as strictly a weakness versus our "deterministic-first" approach. That framing is wrong. **LLM-based extraction is the correct default for unknown or evolving domains** where you haven't yet characterized the structure. **Deterministic extraction is the correct target state for known, stable domains** where the cost of LLM calls is unjustified by the marginal information gained. A mature platform needs **both**, with an explicit graduation path from one to the other.
>
> **The pattern already exists in the codebase — at the wrong layer.** Our `ClassifierPipeline` uses deterministic-first / LLM-fallback for **classification** (tagging content with `domain`, `content_type`, `signal_quality`, etc.), and the "Self-Learning Classification" section above plans to grow the deterministic tier by mining LLM outputs. That's the right pattern — but we've only been applying it one level deep. Apply it one layer up, to **extraction** itself: the process of turning raw input into entities and edges before classification even runs.
>
> **Why this matters:**
> - A new consumer spinning up against a domain they don't yet understand currently has to hand-write an ingestion runner before they can see any value from Trellis. That's a hard cold-start problem — exactly the cold-start problem Graphiti solved by defaulting to LLM extraction.
> - Our `save_memory` path already ingests unstructured agent observations, and currently punts all the "what entities are in this observation?" work to the caller. An LLM extractor in core would make `save_memory` actually useful without requiring consumers to build their own extraction layer.
> - The graduation path (LLM → hybrid → deterministic) gives consumers a clear cost-reduction story: *start expensive-but-universal, get cheaper as the domain crystallizes*.

#### The three extractor tiers

| Tier | When to use | Cost per item | Examples |
|---|---|---|---|
| **Deterministic** | Known source with stable schema | ~0 (parse cost only) | UC catalog runner, dbt manifest parser, OpenAPI spec ingester, GitHub repo walker, OpenLineage feed processor, language-server integrations, CSV/Parquet table readers |
| **Hybrid** | Partial structure + ambiguous relationships | 0-1 LLM calls per N items (amortized) | JSON with typed fields but unclear cross-field relationships; YAML with consistent shape but variable content semantics; log lines with common patterns + occasional surprises; markdown with frontmatter + free-text body |
| **LLM** | Unstructured input, unknown source, or exploratory phase | 2-5 LLM calls per item (Graphiti-style) | Raw text documents, conversational agent observations, screenshots + OCR, arbitrary user-pasted content, first-pass ingestion of a new source system, content from an unfamiliar domain |

The tiers are not mutually exclusive — a single ingestion run can use different tiers for different inputs. The **dispatcher** decides which tier applies based on the input's provenance hints, the domain's maturity, and the consumer's cost budget.

#### Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                 Raw input from any source                    │
│  (text, JSON, message, database row, file, observation,     │
│   API response, user paste, agent memory write, ...)         │
└────────────────────────────┬─────────────────────────────────┘
                             │
                             ▼
               ┌──────────────────────────┐
               │   ExtractionDispatcher   │
               │                          │
               │  Routes by:              │
               │   • source_hint          │
               │   • domain registry      │
               │   • content-type detect  │
               │   • consumer config      │
               │   • cost budget          │
               └────────────┬─────────────┘
                            │
        ┌───────────────────┼───────────────────┐
        │                   │                   │
        ▼                   ▼                   ▼
┌───────────────┐  ┌───────────────┐  ┌───────────────┐
│ Deterministic │  │    Hybrid     │  │      LLM      │
│  Extractor    │  │   Extractor   │  │   Extractor   │
│               │  │               │  │               │
│ Registered by │  │ Rules-based   │  │ Graphiti-like │
│ source system │  │ for structure │  │ entity+edge   │
│ (UC, dbt,     │  │ + LLM for     │  │ extraction    │
│ OpenAPI, ...)│  │ ambiguous     │  │ from raw text │
│               │  │ relationships │  │               │
└───────┬───────┘  └───────┬───────┘  └───────┬───────┘
        │                  │                  │
        └──────────────────┼──────────────────┘
                           ▼
              ┌──────────────────────────┐
              │     ExtractionResult     │
              │  (entities, edges,       │
              │   provenance, confidence,│
              │   extractor_used)        │
              └────────────┬─────────────┘
                           │
                           ▼
                [Existing mutation pipeline]
                 validate → policy → …
```

#### Graduation path

The graduation path is the key differentiator from a pure-LLM approach. Once an LLM extractor has been used enough against a new domain to stabilize patterns, those patterns get mined into a deterministic or hybrid extractor:

```
[LLM extractor used on domain X]
          │
          │  logs (input, extracted entities/edges) as ENRICHMENT_COMPLETED
          │  events with `extractor_used="llm"` tag
          ▼
[Pattern mining over accumulated extractions]
          │
          │  finds stable extraction rules:
          │   - "every item with field X.type = 'table'
          │      produces an entity with name = X.fqn"
          │   - "relationship Y always links A→B when
          │      A.kind = 'foo' and B.kind = 'bar'"
          ▼
[Generate a HybridExtractor with the rules]
          │
          │  rules handle the easy 80%
          │  LLM handles the ambiguous 20%
          ▼
[Register the HybridExtractor in the dispatcher]
          │
          │  LLM calls drop by ~80% on that domain
          ▼
[Continue mining; promote Hybrid → Deterministic
 as the LLM-fraction approaches 0]
```

This is the same pattern the "Self-Learning Classification" section describes for classification — just applied one stage earlier in the pipeline. The feedback loop is identical: LLM outputs become training data for deterministic replacements.

#### Phase 1: Core Protocol and Dispatcher (P1)

- [ ] **`Extractor` Protocol** (`src/trellis/extract/base.py`)
  ```python
  class Extractor(Protocol):
      name: str                       # unique registry key
      tier: ExtractorTier              # deterministic / hybrid / llm
      supported_sources: list[str]     # source_hint values this handles
      version: str                     # semver for graduation tracking

      async def extract(
          self,
          raw_input: Any,
          source_hint: str | None = None,
          context: ExtractionContext | None = None,
      ) -> ExtractionResult: ...
  ```

- [ ] **`ExtractorTier` enum** — `DETERMINISTIC`, `HYBRID`, `LLM`. Used for routing, telemetry, and graduation tracking.

- [ ] **`ExtractionResult` schema** (`src/trellis/schemas/extraction.py`)
  ```python
  class EntityDraft(TrellisModel):
      entity_id: str | None            # None = let the alias system assign
      entity_type: str
      properties: dict[str, Any]
      node_role: NodeRole = NodeRole.SEMANTIC
      confidence: float                # extractor's confidence in this entity

  class EdgeDraft(TrellisModel):
      source: str
      target: str
      kind: str
      properties: dict[str, Any] = {}
      confidence: float

  class ExtractionResult(TrellisModel):
      entities: list[EntityDraft]
      edges: list[EdgeDraft]
      extractor_used: str              # registry key of the extractor
      tier: ExtractorTier
      llm_calls: int                   # cost tracking
      tokens_used: int
      overall_confidence: float
      provenance: ProvenanceSpec       # source_id, timestamp, raw_input_hash
      unparsed_residue: Any | None = None  # content that couldn't be extracted
  ```

- [ ] **`ExtractionDispatcher` class** (`src/trellis/extract/dispatcher.py`)
  - Constructed with a registry of `Extractor` instances
  - `async dispatch(raw_input, source_hint, context) -> ExtractionResult`
  - Routing logic (in priority order):
    1. If `source_hint` matches a registered deterministic extractor → use it
    2. If `source_hint` matches a registered hybrid extractor → use it
    3. If `context.allow_llm_fallback=True` and an LLM extractor is registered → use it
    4. Otherwise raise `NoExtractorAvailable` (explicit failure, not silent passthrough)
  - Dispatcher emits `EXTRACTION_DISPATCHED` events with extractor used, tier, cost, confidence for effectiveness analysis

- [ ] **`ExtractionContext` schema** — carries the consumer's preferences and cost budget:
  ```python
  class ExtractionContext(TrellisModel):
      allow_llm_fallback: bool = False            # gate LLM usage explicitly
      max_llm_calls: int = 5                      # hard cap per extraction
      max_tokens: int = 8000                      # hard cap per extraction
      prefer_tier: ExtractorTier | None = None    # force a specific tier
      domain: str | None = None                   # hint for domain-specific extractors
      source_system: str | None = None            # hint for source-matched extractors
  ```

- [ ] **Registry mechanism** — extractors register via Python entry points (`trellis.extractors`) so consumer packages can ship their own. Core ships a minimal set; consumers extend.

- [ ] **Tests**: dispatcher routing, context-based gating, cost budget enforcement, explicit failure when no extractor matches

#### Phase 2: Reference Implementations (P1-P2)

- [ ] **`PassthroughExtractor` (tier=DETERMINISTIC, P1)** — For when the caller already has structured data and just wants the dispatcher's bookkeeping. Accepts `{"entities": [...], "edges": [...]}` and returns it wrapped in an `ExtractionResult`. Useful for existing runners (UC, dbt) to plug into the dispatcher without being rewritten. Zero cost. Acts as the migration bridge from the current "consumers upsert directly" model.

- [ ] **`JSONSchemaExtractor` (tier=DETERMINISTIC, P1)** — For JSON input with a registered schema. Walks the input using a declarative extraction rulebook (`{"entity_rules": [...], "edge_rules": [...]}`) and emits drafts. Covers the 80% case of "we have structured JSON and know what it means" without any LLM cost. Useful for OpenLineage feeds, audit logs, dbt manifest rows, UC column metadata, etc.

- [ ] **`LLMExtractor` (tier=LLM, P2)** — [blocked: LLMClient ADR (Sprint C)] Graphiti-style extraction from raw text or unknown-structure JSON. Pipeline:
  1. Call LLM with extraction prompt (entity types, relationship kinds to look for, examples from domain registry)
  2. Parse structured response (Pydantic `ExtractedEntities`, `ExtractedEdges`)
  3. Optionally call LLM again for deduplication if the dispatcher's pass identified potential collisions
  4. Return `ExtractionResult` with full cost accounting
  - Prompt templates live in `src/trellis/extract/llm_prompts/` using the same Jinja pattern as the prompt library TODO in the Graphiti section
  - Depends on the **`LLMClient` + `EmbedderClient` abstractions** TODO from the Graphiti section. That work is a prerequisite.
  - Reference: `graphiti/graphiti_core/utils/maintenance/node_operations.py` and `edge_operations.py` — their extraction pipeline is the template. We don't need to match it line-for-line; we need the basic "extract → dedup → return" loop.
  - Initial scope: one implementation using the `LLMClient` abstraction, works with any registered LLM provider. Do not ship multiple extractor implementations for different providers.

- [ ] **`HybridJSONExtractor` (tier=HYBRID, P2)** — [blocked: LLMClient ADR (Sprint C)] JSON with known structure for some fields, free-text for others. Applies deterministic rules first, then calls LLM only for the ambiguous portions. The prototype for all hybrid extractors.

- [ ] **`SaveMemoryExtractor` (tier=LLM, P2)** — [blocked: LLMClient ADR (Sprint C)] Dedicated extractor for the `save_memory` unstructured observation path. Knows the observation format (agent_id, intent, observation text) and extracts entities/edges specifically for the agent-memory domain. Cross-reference: the "Observation Pipeline" section's `DocumentEnrichmentWorker` uses this.

- [ ] **Tests**: each extractor exercised with domain fixtures; cost tracking verified; confidence scores sensible; graceful degradation when LLM unavailable

#### Phase 3: Integration with Existing Paths (P1-P2)

- [ ] **Wire dispatcher into `save_memory`** — currently `save_memory` creates a document and lets the enrichment pipeline classify it. Extend to optionally run the dispatcher first to extract entities/edges from the observation, then save the document *and* create the entities in one governed transaction. Gated by a config flag (`enable_llm_extraction: bool`) that defaults to False — LLM path is opt-in, not automatic.

- [ ] **Wire dispatcher into `ingest bulk` endpoint** — the bulk ingest endpoint (P0 in the Unity Catalog Backfill section) currently expects pre-structured entity/edge lists. Extend to accept a new format `{"raw_items": [...]}` where each item is routed through the dispatcher. Structured callers still use the pre-structured format; unstructured callers can now use the raw format with an explicit `source_hint` and `allow_llm_fallback=True`.

- [ ] **Wire dispatcher into `DocumentEnrichmentWorker`** — when a new document arrives via `save_memory`, the enrichment worker first classifies (existing path) and then optionally runs extraction. Extracted entities are created via the mutation pipeline; extracted edges link the entities to the document. This makes `save_memory` into a real "drop unstructured content and get structured graph updates" path, not just a document stash.

- [ ] **New event: `EXTRACTION_COMPLETED`** — emitted after every dispatch with: extractor_used, tier, cost, confidence, entities_created, edges_created, source_hint. Feeds into the graduation pipeline below and into effectiveness analysis.

#### Phase 4: Graduation Mechanism (P2-P3)

- [ ] **Pattern mining worker** (`src/trellis/extract/mining/`)
  - Scheduled job that reads `EXTRACTION_COMPLETED` events where `extractor_used` is an LLM extractor
  - For each (source_hint, domain) bucket, looks for stable patterns in the LLM's outputs:
    - "every input of shape X produces an entity of type Y with name = X.field_Z"
    - "relationship kind W always appears between entities of types A, B when condition C holds"
    - Simple frequency + consistency thresholds first; machine-learning later if needed
  - Outputs candidate `JSONSchemaExtractor` rules for human review

- [ ] **`trellis extract graduate` CLI command**
  - Lists mined patterns per domain
  - Shows: which rules are candidates, how much LLM cost they'd displace, confidence
  - Lets a human approve a rule set and deploy it as a new `HybridExtractor` in the registry
  - Approved rules are versioned and tracked — graduation is auditable

- [ ] **Graduation telemetry** — track per-domain:
  - LLM call rate over time (should decrease as graduation progresses)
  - Deterministic coverage % (fraction of items handled without LLM)
  - False-positive rate of deterministic rules (compared against periodic LLM re-extractions for sampling)
  - Domain "maturity score" = (deterministic coverage) × (1 - false-positive rate)

- [ ] **Graduation guardrails** — automatically revert a deterministic rule to the LLM path if:
  - Its false-positive rate (measured against periodic LLM sampling) exceeds a threshold
  - The source schema changes in a way that invalidates the rule (detected via schema diff)
  - A consumer explicitly flags an extraction as wrong and the flag points back to the rule

- [ ] **Tests**: pattern mining over synthetic fixtures, graduation CLI workflow, automatic reversion on rule degradation

#### Phase 5: Documentation & Positioning (P2)

- [ ] **`docs/agent-guide/extraction-pipeline.md`** — explains the three tiers, how to register a new extractor, how to use the dispatcher from a runner, how the graduation path works, when to opt into LLM fallback, and when to write a dedicated deterministic extractor.

- [ ] **Update `docs/agent-guide/modeling-guide.md`** — add a section noting that LLM extractors are a valid path for exploratory ingestion of new domains, and that the modeling guide's four-question test applies equally to LLM-extracted entities (the extractor is responsible for setting `node_role` correctly).

- [ ] **Update README positioning** — revise the value prop to include "starts LLM-powered for unknown domains, graduates to deterministic for stability." This is a genuinely differentiated story vs both Graphiti (LLM forever) and pure-deterministic systems (hand-write everything). It's the right synthesis.

- [ ] **Blog post**: "Why your ingestion pipeline should start expensive and get cheaper" — the tiered-extraction story as a thought-leadership piece. Positions Trellis as the mature answer to the cold-start / long-run tradeoff.

#### Connection to adjacent sections

- **Self-Learning Classification** (`### Self-Learning Classification`) — same pattern applied to tagging rather than extraction. Both feed into a "grow deterministic tier over time" philosophy.
- **Configurable Ingestion Rules & Population Framework** (`### Configurable Ingestion Rules & Population Framework`) — the `IngestionRule` schema there is a natural pairing: rules specify *where content goes*, the extractor dispatcher decides *how it becomes structured*. Both are consulted by the ingestion router.
- **Observation Pipeline** (`### Observation Pipeline`) — `save_memory` is the first real consumer of the LLM extractor path. The `DocumentEnrichmentWorker` and `SaveMemoryExtractor` are tightly coupled.
- **Graphiti comparison — LLM/Embedder abstractions** — Phase 2 depends on those abstractions landing first. Cannot implement `LLMExtractor` without an `LLMClient`.
- **Graph Modeling Guidance — node_role** — LLM extractors must respect `node_role` (default SEMANTIC; can explicitly produce STRUCTURAL or CURATED if the extraction prompt calls for it). Wire into the Phase 2 prompt design.

#### Open questions

- [ ] **Should the dispatcher support parallel extraction** (run multiple extractors on the same input and merge)? Argument for: hybrid approaches where a deterministic extractor handles the structured fields and an LLM handles the text body. Argument against: complexity, double-cost. Decision: start with single-extractor dispatch; add parallel as a Phase 3 feature if real need emerges.
- [ ] **Where does extraction cost get charged**? For LLM extraction in `save_memory`, the cost is incurred by whoever owns the Trellis deployment, not the agent calling `save_memory`. Consumers need visibility into cost budgets. Resolve via the `ExtractionContext.max_llm_calls` / `max_tokens` caps — callers opt in and declare their budget; exceeded budgets raise.
- [ ] **How does graduation interact with SCD Type 2 history?** When a rule changes, do we re-extract existing entities with the new rule and version them? Or only apply to new ingestions? Decision deferred to implementation; default leaning: only new ingestions (no retroactive rewrite), with a separate `trellis extract reingest <source>` command for explicit backfills.
- [ ] **Should the LLM extractor support streaming**? For very large raw inputs, Graphiti chunks and extracts incrementally. We likely want this eventually; defer to Phase 2 v2.

### Domain-Specific Entity Types & Per-Implementation Skill Curation

> **Principle:** The core library defines the *machinery* (stores, mutations, retrieval, classification) but never prescribes *what* a deployment should store. Each implementation develops its own entity types, categories, and knowledge curation patterns based on its domain and usage.

The current design already supports arbitrary string entity types and edge types at the storage layer. What's missing is guidance and structure around *how* implementations should evolve their own vocabularies.

- [ ] **Document the extensibility pattern** — a "Building Your Domain" guide in `docs/agent-guide/`:
  - How to introduce domain-specific entity types (e.g., `EXEMPLAR`, `UC_TABLE`, `PIPELINE`, `METRIC`) without modifying core enums
  - How to structure `knowledge/` directories (conventions, precedents, patterns) with frontmatter that drives classification
  - How domain categories evolve: start with `general`, split into `data_pattern`, `convention`, `architecture`, etc. as usage reveals natural groupings
  - How `retrieval_affinity` tags bridge domain-specific content to the generic retrieval tier system
  - When to add a new entity type vs. using metadata on an existing type (decision criteria: does it have its own identity? do agents query for it by type? does it participate in typed edges?)
  - Example: trellis-platform defines `UC_TABLE`, `DBT_MODEL`, `EXEMPLAR`, `PRECEDENT` — another deployment might define `API_ENDPOINT`, `RUNBOOK`, `INCIDENT`, `PLAYBOOK`

- [ ] **Per-implementation skill context** — how agents learn what entity types and categories exist in *their* deployment:
  - Agents should be able to discover available types via graph introspection (e.g., `trellis retrieve entity-types` → list of types with counts)
  - Skill definitions (MCP tool descriptions, agent system prompts) should be generated or enriched from the graph's actual content, not hardcoded
  - The graph teaches the agent what to ask for — if an agent sees `EXEMPLAR` entities exist, it can request exemplars; if they don't, it doesn't waste tokens asking

- [ ] **Starter templates** — provide a minimal `knowledge/` scaffold and sample `ingestion-rules.yaml` that new deployments can copy and customize, rather than starting from scratch or copying trellis-platform's domain-specific setup

### Graph Modeling Guidance, Node Roles & Curation

> **Problem discovered (2026-04-10):** A downstream data-architecture deployment was ingesting a Unity Catalog with ~500K columns and modeling **every column as its own graph node** with a `belongs_to` edge back to its parent table. The result: graph inflation of 10-100×, retrieval pollution (column nodes competing with tables for token budget), traversal pollution (fan-out through leaf-only column nodes), and no payoff — schema lineage is table-to-table, not column-to-column, and nobody ever queried for a column as an independent entity. This was a *runner-side* modeling error, not a core-library bug — Trellis faithfully stores whatever is handed to it — but Trellis has a **guidance gap** that let the error propagate: there is no documentation telling consumers how to decide what gets to be a node, no diagnostic that surfaces the smell, and no schema affordance for distinguishing "structural plumbing" from "things worth retrieving."
>
> **Core principle:** Trellis should be strict about **correctness** (schemas, mutations, audit, immutability) and flexible about **modeling** (what entity types exist, how granular the graph is). Modeling judgment is domain-specific — a pharma deployment may genuinely need molecule→atom decomposition, a code-search deployment may need function→parameter. The core library cannot pre-judge. But it **can** teach good modeling via documentation, **measure** graph health via diagnostics, and **provide affordances** (like `node_role`) that let consumers express intent without constraining the schema surface.

> **Three node roles, not two:** The design distinguishes three kinds of nodes:
> - **`structural`** — fine-grained, machine-generated, regenerated from source. Columns, function parameters, file lines. Retrieved only as part of their parent's context, never standalone.
> - **`semantic`** (default) — represents a real thing in the world, ingested with a source-of-truth. Tables, services, people, documents, precedents. Normal retrieval, standalone-discoverable.
> - **`curated`** — synthesized/derived from the graph itself. Domain rollups, community clusters, precedents, "popular entities" summaries. Regenerable from a generator spec, human-editable, boosted for broad/strategic queries.
>
> The curated tier is what unlocks iteration: ingested entities can't be freely edited (you'd drift from source-of-truth), but curated nodes are **meant** to be edited and re-run. Separating them in the schema means curation tooling can operate on curated nodes without risking corruption of ingested ground truth. This subsumes several existing TODO items (precedents, community detection, domain rollups) under one coherent framework.

#### Phase 1: Modeling Guide Documentation (P1, no code)

- [ ] **Create `docs/agent-guide/modeling-guide.md`** — the canonical "how to decide what gets to be a node" reference. This is the highest-leverage, lowest-risk artifact in this section — it's pure documentation, it prevents future consumers from making the column-explosion mistake, and it can be written and merged without touching any code.

  Contents (draft outline):

  1. **The four-question test for node-vs-property-vs-document.** A thing should be a **node** only if at least one is true:
     1. You traverse *from* it to other things (not just from its parent)
     2. You query *for* it directly across parents ("find all X of type Y")
     3. You attach evidence/observations/policies *to it independently* of its parent
     4. It has its own lifecycle (versioning, deprecation) separate from its parent

     If none are true, it's a **property** on the parent (JSON field). If it has substantial text content that benefits from full-text/semantic search, it's a **document** linked to the parent via an edge.

  2. **The three node roles** — `structural`, `semantic`, `curated` — with the full table from the section intro above, plus retrieval defaults, lifecycle expectations, and examples drawn from **multiple domains** (data platforms, codebases, infrastructure, knowledge management) so no single domain looks canonical.

  3. **Anti-patterns with named diagnoses:**
     - **Schema explosion** — modeling every leaf of a hierarchical structure (columns, params, file lines, config keys) as its own node
     - **Leaf-only nodes** — nodes that only ever appear as the target of one edge kind and never as a source
     - **Ingested-looking curated nodes** — synthetic summaries stored without `node_role=curated`, making them indistinguishable from ground truth and impossible to regenerate safely
     - **Property-envy documents** — information that belongs as structured properties on a node being shoved into freeform document text because "it's easier to ingest"
     - **Cardinality explosion from implicit joins** — creating a node for every (table, column) pair, every (file, line) pair, every (service, endpoint) pair

  4. **Temporal reinforcement paragraph** (one of the decision factors the guide should make explicit):
     > Every node in Trellis is a temporal entity. The graph store uses SCD Type 2 versioning (`valid_from`/`valid_to`) to track how each node's properties change over time, and `get_node_history()` returns the full audit trail. **Every node you create is a commitment to track its history forever.** If you model 500K columns as nodes, you now have 500K independent temporal histories — almost none of which you'll ever query by time. If columns are instead a JSON property on the table node, a schema migration shows up as *one* property-diff event on the table's history, which is usually what you actually want to see. When deciding node-vs-property, ask: *"do I want time-travel queries at this granularity?"* If no, it's a property.

  5. **Curated-node temporality paragraph** (separate because it has different semantics):
     > Curated nodes carry two kinds of history: (1) standard SCD Type 2 property history (edits to the summary are property changes), and (2) a `generation_spec` recording which algorithm/prompt/inputs produced them and when. Regenerating a curated node creates a new version via the standard SCD mechanism but additionally updates the `generation_spec` fields — this separates "someone edited the summary" from "we re-ran the clustering algorithm," which matters for curation workflows and for trusting curated content in downstream retrieval.

  6. **Worked example — database catalog ingestion.** Walk through a concrete UC ingest showing:
     - **The wrong shape:** `UC_TABLE` node + N `UC_COLUMN` nodes + N `belongs_to` edges per table. Graph inflates by 20-50×. Retrieval ranks individual columns against table descriptions. Traversal fans out through column leaves.
     - **The right shape:** one `UC_TABLE` node with `columns: [...]` as a JSON property, one `schema_description` document linked via `DESCRIBED_BY`, table-level `DEPENDS_ON` edges for lineage, and an `OWNED_BY` edge to a team entity. ~1 node per table, retrievable as a coherent unit.
     - **The legitimate exception:** when column-level lineage is a real requirement (regulated data products, dbt exposures with per-column grants), model only the columns that actually participate in lineage as `node_role=structural` nodes — not *all* columns. Everything else stays a property.

  7. **Worked example — code repository ingestion.** Show the same structural-vs-semantic-vs-curated split applied to a different domain (files, functions, imports, symbols) to reinforce that this is not database-specific.

  8. **Decision flowchart** (mermaid or ASCII) tying it all together.

- [ ] **Cross-link from `docs/agent-guide/schemas.md` and the Domain-Specific Entity Types section** so consumers find the modeling guide before they start designing their ingestion runners. The guide is useless if nobody reads it before they write the runner.

- [ ] **Cross-link from `README.md`** under the "How to use Trellis" section — a one-sentence pointer: "Before designing an ingestion runner, read the modeling guide to avoid graph-inflation anti-patterns."

#### Phase 2: `node_role` Schema Extension (P1, co-land with Tiered Retrieval Phase 1) — ✅ DONE (2026-04)

> Landed in `src/trellis/schemas/enums.py` and `src/trellis/schemas/entity.py`. Kept for historical reference.

- [x] **Add `NodeRole` enum** (`schemas/enums.py:22-40`)
- [x] **Add `node_role` to the `Node` schema** (`schemas/entity.py:59`, default `SEMANTIC`)
- [x] **Mutation handler validation** — `upsert_node` rejects invalid `node_role` / `generation_spec` combinations (model validator on `Entity`)
- [x] **Extend deterministic classifiers to populate `node_role`** (landed in the classifier pipeline; structural opt-in only)
- [x] **`PackBuilder` filter logic** — structural-node filtering + curated-node boost via `TierMappingConfig`
- [x] **Update `schemas.md` and `trace-format.md`** in the agent guide to document `node_role` and `generation_spec`
- [x] **Tests**: round-trip serialization, validator enforcement, PackBuilder filter behavior

#### Phase 3: `trellis admin graph-health` Diagnostic (P1, can ship independently)

- [ ] **New CLI command: `trellis admin graph-health`** (`src/trellis_cli/admin.py`)
  - Read-only analysis — never modifies the graph
  - Supports `--format json` for machine output and rich-table for humans
  - Optional `--entity-type` filter to scope analysis to one type at a time
  - Optional `--role` filter (`structural` / `semantic` / `curated`) for role-specific reports

- [ ] **Report: role distribution**
  - Total nodes by `node_role` (count + percentage)
  - **Warning signal:** `structural` > 70% of total nodes → likely over-modeling, point to the modeling guide
  - **Warning signal:** `curated` > 30% of total nodes → curation outpacing ingestion, may indicate duplicate generators or stale regeneration
  - **Warning signal:** zero `curated` nodes in a graph with >10K semantic nodes → curation infrastructure not being used; recommend running precedent promotion / community detection

- [ ] **Report: top entity types by count**
  - Ranked list with percentages
  - **Warning signal:** any single entity type is >70% of all nodes → type-imbalance, likely a dominant machine-generated type that should be reviewed for structural-vs-semantic classification

- [ ] **Report: leaf-node analysis by entity type and role**
  - For each entity type: fraction of nodes that are leaves (zero outbound edges) vs branches (≥1 outbound edge)
  - **Warning signal:** a `semantic` entity type with >90% leaves → those nodes are probably structural plumbing masquerading as semantic content. Suggest reclassification to `structural` or merging into a parent node's properties.
  - **Fine:** a `structural` entity type with ~100% leaves — that's expected and correct
  - Report distinguishes these so structural leaves don't trigger warnings

- [ ] **Report: edge fan-out distribution per edge kind**
  - For each edge kind: histogram of out-degree per source node
  - **Warning signal:** an edge kind whose source nodes have median fan-out of exactly 1 and whose targets are ≥90% leaves → likely a "structural glue" edge (e.g., `belongs_to`) that could be replaced by embedding the target as a property
  - **Warning signal:** an edge kind that only appears on one entity type → may indicate a type-specific shortcut that should be documented or refactored

- [ ] **Report: curated-node freshness**
  - For each `generator_name`: count of curated nodes, median age since `generated_at`, oldest and newest
  - **Warning signal:** a generator with curated nodes older than `staleness_threshold_days` (default 30, configurable) → regeneration overdue
  - **Warning signal:** a generator with zero runs in the last 90 days → may be abandoned; recommend removal or reactivation

- [ ] **Report: "suggested demotions"**
  - List entity types whose shape matches the structural profile (>90% leaves, single inbound edge kind, low-or-zero outbound edges) but are currently classified as `semantic`
  - For each, show: count, dominant inbound edge kind, example node IDs
  - Explicitly labeled "suggestion, not enforcement" — the runner's choice stands, this is advisory

- [ ] **Report: orphan detection**
  - Nodes with no edges in either direction (semantic islands)
  - Edges with dangling endpoints (references to nonexistent nodes)
  - Overlaps with the "consistency sweep" TODO in the Cross-Store Reference Model section — consider consolidating when both land

- [ ] **Output formatting**
  - Human mode: rich tables with color-coded warnings (green/yellow/red per threshold), final summary line (`3 warnings, 1 critical`)
  - JSON mode: structured report matching an `trellis.admin.GraphHealthReport` schema for programmatic consumption
  - Exit code: 0 if no warnings, 1 if warnings, 2 if critical (for CI integration)

- [ ] **Tests**:
  - Synthetic fixtures exercising each warning signal independently
  - Integration test against a realistic mixed graph (some semantic, some structural, some curated, some orphans)
  - JSON output matches schema
  - Exit codes match severity

- [ ] **Documentation**: `docs/agent-guide/graph-health.md` explaining each report, what the signals mean, and how to act on them. Cross-link from the modeling guide.

#### Phase 4: `trellis curate` CLI Namespace (P2, enables iteration loop)

> **Naming conflict:** An `trellis curate` namespace already exists at `src/trellis_cli/curate.py` but it hosts **mutation commands** (`promote`, `link`, `label`, `entity`, `feedback`) rather than curated-node-workflow commands. Before building out this phase, decide: rename the existing namespace (e.g., `trellis mutate`), or put these new commands under a different verb (e.g., `trellis curated` or `trellis synth`). The command list below assumes the curation-workflow semantics described in this section.

- [ ] **New CLI command namespace for curated-node workflows** — operations on curated nodes. Depends on Phase 2 (`node_role=CURATED` and `generation_spec`) being in place — ✅ Phase 2 is complete.

- [ ] **`trellis curate list`** — list curated nodes with filters
  - Flags: `--generator NAME` (filter by `generation_spec.generator_name`), `--stale [DAYS]` (older than threshold), `--entity-type TYPE`, `--domain DOMAIN`
  - Output: table with node_id, generator, generated_at, age, summary snippet
  - `--format json` for programmatic consumption

- [ ] **`trellis curate show <node_id>`** — full view of a curated node
  - Displays: properties, `generation_spec`, SCD Type 2 version history, inbound/outbound edges, source nodes listed in `generation_spec.source_node_ids`
  - Useful for "why does this domain summary say X?" investigations

- [ ] **`trellis curate regenerate <node_id>`** — re-run the node's generator
  - Loads `generation_spec`, dispatches to the registered generator by `generator_name`, captures new output, upserts a new version via the mutation pipeline
  - Old version preserved via SCD Type 2 — regeneration is never destructive
  - Flags: `--dry-run` (show what would change), `--force` (bypass freshness check)
  - Requires a **generator registry** — new module `src/trellis/curate/generators.py` with a `Generator` Protocol and a registry populated at startup. Each generator declares `name`, `version`, and `regenerate(node, spec) -> GenerationResult`.
  - Built-in generators (start with one or two, grow over time):
    - `precedent_promotion` (wraps the precedent promotion worker — see Observation Pipeline section)
    - `community_detection_louvain` (when community detection lands — see Graphiti section)
    - `domain_summary_llm` (when domain rollup lands)

- [ ] **`trellis curate edit <node_id>`** — human-edit a curated node's summary
  - Opens the node's `summary` / `content` property in `$EDITOR`
  - Saves as a property change (SCD Type 2 version bump) — `generation_spec` is untouched, because this is a human edit, not a regeneration
  - Adds a `human_edited: true` flag to the new version's metadata to distinguish edited versions from regenerated versions
  - This is the "iterate and curate easily" affordance — lets humans refine machine-generated summaries without losing the regeneration ancestry

- [ ] **`trellis curate regenerate-all`** — bulk regeneration with filters
  - Flags: `--generator NAME`, `--stale-days N`, `--dry-run`
  - Walks matching curated nodes, invokes regenerate on each, reports success/failure counts
  - For CI: `trellis curate regenerate-all --generator precedent_promotion --stale-days 7`

- [ ] **`trellis curate diff <node_id> --from <version> --to <version>`** — compare two versions of a curated node
  - Shows property-level diff between SCD versions
  - Distinguishes property changes (human edits) from `generation_spec` changes (regenerations)
  - Useful for reviewing whether a regeneration produced meaningfully different content

- [ ] **Generator registry entry-point mechanism** — consumer packages can register their own generators via Python entry points (`trellis.curate.generators`), so domain-specific curation lives in consumer repos, not core

- [ ] **Tests**:
  - End-to-end regenerate: create curated node → edit source nodes → regenerate → verify new version reflects changes and old version is preserved
  - Edit flow: edit → verify property change, `generation_spec` unchanged, `human_edited` flag set
  - Registry: custom generator registered via entry point is discoverable and invokable
  - Diff: shows meaningful output for both edits and regenerations

- [ ] **Documentation**: `docs/agent-guide/curation-workflows.md` — the iteration loop pattern, when to edit vs regenerate, how to write a custom generator, how to structure a curation CI job

#### Phase 5: Consolidation with Adjacent Work (P2, mostly docs and refactors)

This section folds existing TODO items into the curated-node framework. No new code surface — just reframing so the pieces compose.

- [ ] **Precedents as curated nodes** — the `DocumentPromotionWorker` TODO in the Observation Pipeline section should produce nodes with `node_role=CURATED` and `generation_spec.generator_name="precedent_promotion"`. Update the worker's design doc to reflect this. Migration: existing precedent nodes (if any) get backfilled with `node_role=CURATED` and a synthetic `generation_spec` carrying `generator_name="precedent_promotion_legacy"` to mark them as pre-framework.

- [ ] **Community detection as curated nodes** — when the Graphiti-comparison community detection feature lands, community cluster nodes should be `node_role=CURATED` with `generation_spec.generator_name="community_detection_louvain"` (or whichever algorithm). The generation_spec captures the algorithm parameters (resolution, random_seed, min_community_size) so regeneration is deterministic and reproducible. Update the Graphiti-comparison TODO entry to cross-reference this framework rather than proposing a parallel mechanism.

- [ ] **Domain rollups as curated nodes** — the "domain" entity concept discussed in the Domain-Specific Entity Types section should be implemented as curated nodes, not semantic nodes. A domain rollup is synthesized from the entities belonging to the domain; it's regeneratable, editable, and boosted in objective-tier retrieval. This also fits the "objective-tier content" slot in the Tiered Context Retrieval design (curated domain nodes are exactly what you want to assemble once per workflow and share across agent phases).

- [ ] **Workflow/saga entities** — the Graphiti comparison proposes promoting workflow runs from implicit (`workflow_id` on traces) to first-class entities. These are **semantic**, not curated — a workflow run is a real thing that happened, not a synthesis. Explicitly note this distinction in the Graphiti TODO entry so the two concepts don't get confused.

- [ ] **Update the Graphiti comparison section** to reference the curated-node framework as the umbrella mechanism for community detection, rather than describing community nodes as a novel concept. The framework is broader than community detection alone.

- [ ] **Effectiveness correlation with node role** — extend `effectiveness.py` to report outcome correlation **by node role of items included in the pack**. Hypothesis: curated nodes correlate more strongly with successful outcomes than raw semantic nodes, because they're pre-digested and higher signal density. If confirmed, this validates the curated-tier boost in PackBuilder. If not confirmed, it's a signal that current curation isn't adding value and the generators need tuning. This is a feedback loop closing between the tiered retrieval work and the curated-node framework.

#### Open Questions (to resolve before or during implementation)

- [ ] **Should `node_role` be mutable?** Current proposal: no — changing it requires delete+recreate. Rationale: role is a graph-structural invariant, not a property. Counter-argument: a semantic node that turns out to be pure plumbing might benefit from in-place demotion to structural. Decision deferred until a concrete case arises; default is immutable.

- [ ] **Should `structural` nodes emit SCD Type 2 history?** They're regenerated from source, so version history is arguably redundant (the history lives in the source system). Turning off SCD for structural nodes would massively reduce storage cost in high-cardinality deployments but breaks the uniformity of the graph store model. Decision: keep SCD on for now (uniformity > storage cost), revisit if storage becomes a pain point.

- [ ] **Should curated nodes have an explicit `refreshable: bool` flag?** Some curated nodes might be one-shot (e.g., an LLM summary of a specific trace at a specific point in time) that shouldn't be regenerated. Alternative: `generation_spec.parameters.refreshable = false` and have the regenerate command respect it. Decision: piggyback on parameters, no dedicated field.

- [ ] **How does `node_role` interact with the Cross-Store Reference Model's typed ID prefixes?** Proposal: IDs remain `ent:` regardless of role (the role is a property of the entity, not its identity). Curated nodes don't get their own prefix. Resolve this when the typed-ID work starts.

- [ ] **Generator registry discovery mechanism** — Python entry points vs config-file registration vs runtime registration via `MutationExecutor.register_generator()`. Entry points are cleanest for plugin distribution but add a dependency on `importlib.metadata`. Decide when implementing Phase 4.

### Cross-Store Reference Model & Source of Truth

> **Problem:** The system uses 6 independent stores with no referential integrity between them. The same concept (e.g., a UC table) can exist as a graph node, a document, a vector embedding, and a blob — each with a different ID. There is no unified way to ask "give me everything the system knows about entity X" without querying every store independently and hoping IDs match. As more backends are added (Neo4j, filesystem, Obsidian), this problem multiplies.

> **Core question:** What is the minimum reference architecture that lets stores stay independent while maintaining coherent cross-references without duplicating data or creating excessive management overhead?

#### Current State (gaps identified via codebase audit)

1. **No unified ID namespace.** All IDs are bare ULIDs with no type prefix. A string like `01ARZ3NDEKTSV4RRFFQ69G5FAV` could be a trace, document, node, edge, or event. `PackItem.item_id` is ambiguous without `item_type` metadata.

2. **No cross-store foreign keys.** Stores reference each other only through denormalized fields in payloads (`Evidence.attached_to`, `Trace.evidence_used`). These are soft pointers — no validation, no cascade, no reverse lookup.

3. **Same concept, different IDs.** An entity `node_abc` in GraphStore and its description `doc_xyz` in DocumentStore are not linked except by convention (both carry the entity name in metadata). PackBuilder's in-memory dedup catches exact ID collisions only — not "these two items describe the same thing."

4. **No `source_of_truth` in core schemas.** When the same entity is upserted from multiple sources (UC runner, dbt runner, manual creation), the last write wins. No merge logic, no conflict detection, no provenance chain.

5. **No cross-store atomicity.** Mutation handlers write to GraphStore then EventLog sequentially. If the second write fails, state is inconsistent with no rollback.

6. **No reverse lookups.** Graph knows about entities; DocumentStore knows about documents. Neither knows about the other. To find "all documents about entity X," you must iterate evidence `attached_to` fields or scan the event log.

#### Design Principles (proposed)

- [ ] **Design doc: `docs/design/cross-store-references.md`** — full treatment of the reference model. Key principles to evaluate:

  **P1: Graph as the directory.** GraphStore is the "index of everything." Every concept that exists in any store has a corresponding node (or at minimum, is reachable via an edge from a node). Other stores hold the content; the graph holds the relationships. You query the graph to discover what to fetch from other stores.

  **P2: Typed ID namespace.** Prefix IDs at the API boundary: `ent:`, `doc:`, `trc:`, `evt:`, `vec:`, `blob:`. Internal stores can use bare ULIDs, but any ID that crosses a store boundary carries its type. This makes `PackItem.item_id` self-describing and enables correct routing without relying on sidecar metadata.

  **P3: Content lives in exactly one authoritative store.** No duplicating document text as a graph node property AND as a DocumentStore entry. Store it once, reference it by typed ID. The graph node carries a pointer (`described_by: doc:xyz`), not a copy.

  **P4: Edges are the cross-store pointers.** GraphStore edges bridge stores: `ent:abc --[DESCRIBED_BY]--> doc:xyz`. The edge lives in the graph; the content lives in the document store. Retrieval follows the pointer. This is the same pattern the graph already uses for intra-store relationships — extend it to cross-store.

  **P5: Source of truth is always external (for ingested entities).** The graph is a cache/index. UC, dbt, GitHub are the real sources. A `source_of_truth` field + `staleness_days` on entities defines the refresh contract. For human-created entities (precedents, conventions), the graph IS the source of truth. The field captures both cases.

  **P6: Eventual consistency over distributed transactions.** Cross-store writes will sometimes fail partially. Design for idempotent retry (the mutation executor already has idempotency checks), not 2PC. Add periodic consistency sweeps that detect orphaned references, missing reverse links, and stale entries.

#### Implementation Tasks

- [ ] **Add `source_of_truth` to Entity schema** — `source_of_truth: str | None` indicating which external system is authoritative (e.g., `"unity_catalog"`, `"dbt_manifest"`, `"knowledge_base"`, `None` for graph-native). Informs staleness detection and conflict resolution.

- [ ] **Typed ID prefix convention** — define the prefix scheme (`ent:`, `doc:`, `trc:`, etc.). Implement as a `TypedId` value object that serializes as `"{prefix}:{ulid}"`. Introduce gradually: new IDs use typed format, existing bare ULIDs are accepted via backward-compatible parsing.

- [ ] **Cross-store dedup in PackBuilder** — extend dedup to group items by "canonical entity" not just by bare ID. When a graph node and a document both reference the same canonical entity, merge them into a single PackItem (or rank and keep the richer one). Requires the graph-as-directory pattern: look up which documents/evidence are linked to each entity node.

- [ ] **Reverse index on entities** — when a document or evidence references an entity (via `attached_to`), store the reverse pointer on the entity node's properties (or as an edge). Enables "give me everything about entity X" via a single graph query without scanning other stores.

- [ ] **Consistency sweep CLI** — `trellis admin check-integrity`: scan GraphStore edges, verify both endpoints exist, flag dangling references. Scan DocumentStore `attached_to` fields, verify target entities exist. Report orphans. Optionally repair (create missing reverse edges, archive orphaned documents).

- [ ] **Evaluate: graph-as-directory cost** — does requiring every concept to have a graph node create unacceptable overhead for high-volume stores (vectors, blob artifacts)? Or is it a negligible cost given that graph nodes are lightweight metadata? Determine the threshold: everything gets a graph node vs. only "important" things get graph nodes.

### Store-Per-Purpose Architecture & Backend Diversity

> **Principle:** Different retrieval and storage needs demand different backends. The right store depends on the operation — graph traversal, semantic similarity, full-text search, document storage, and blob archival each have optimal backends that may differ. The experience graph should compose these backends transparently.

The current `StoreRegistry` + ABC pattern already supports this — 6 store ABCs, lazy-loaded backends via config. But the *aspirational vision* is broader than the current 3-backend set (SQLite, PostgreSQL, LanceDB/pgvector).

**Landscape of relevant storage paradigms:**

| Need | Current Backend | Potential Backends | Why |
|------|----------------|-------------------|-----|
| Graph traversal (shallow) | PostgreSQL (recursive CTE) | PostgreSQL, SQLite | CTEs handle 2-3 hop traversal well |
| Graph traversal (deep) | PostgreSQL (limited) | Neo4j, Memgraph, Apache AGE | Native graph engines optimized for deep traversal (6+ hops), path finding, community detection |
| Semantic search | LanceDB / pgvector | LanceDB, pgvector, Qdrant, Pinecone | ANN index tuning, quantization, metadata filtering |
| Full-text search | SQLite FTS5 / PostgreSQL | PostgreSQL tsvector, Elasticsearch, Typesense | Ranking, stemming, fuzzy matching at scale |
| Document storage | SQLite / PostgreSQL | PostgreSQL, MongoDB, filesystem | Structured metadata + content |
| Blob/artifact storage | Local filesystem / S3 | S3, GCS, Azure Blob, local | Large files, binary artifacts, cost optimization |
| Local-first / offline | SQLite + LanceDB | SQLite, filesystem, Obsidian vault | Single-machine, no server, git-syncable |
| Markdown knowledge graph | (not supported) | Obsidian-style markdown + wikilinks, [mempalace](https://github.com/milla-jovovich/mempalace) | Human-readable, git-versioned, filesystem-native, existing ecosystem of graph visualization tools |

- [ ] **Audit current ABC surface area** — ensure each store ABC's interface is backend-agnostic enough that a native graph database (Neo4j), a dedicated search engine (Elasticsearch), or a filesystem-backed store (Obsidian-style markdown) could implement it without contorting the interface
  - GraphStore: does the interface assume SQL? Would a Cypher/Gremlin backend need adapter shims?
  - DocumentStore: could a filesystem backend (directory of markdown files with YAML frontmatter) implement the current interface?
  - VectorStore: is the interface ANN-library-agnostic?

- [ ] **Document the "store-per-purpose" design philosophy** — expand the existing "Storage Backend Guidance" section into a standalone design doc:
  - Why a single database (even PostgreSQL) isn't optimal for all operations
  - How the registry composes heterogeneous backends behind uniform ABCs
  - Trade-off matrix: latency, concurrency, traversal depth, semantic quality, operational complexity
  - When to add a new backend vs. when to upgrade an existing one
  - Cost profile: local-first (free) → managed PostgreSQL ($) → dedicated graph DB + vector DB ($$$)

- [ ] **Filesystem-backed stores for local-first use** — investigate feasibility:
  - GraphStore backed by markdown files with YAML frontmatter + wikilinks (Obsidian-compatible)
  - DocumentStore backed by a directory of text files with metadata sidecar
  - Trade-offs: no transactions, no concurrent writes, but human-readable, git-syncable, zero-infrastructure
  - This enables a "knowledge base as code" pattern where the graph *is* the repo

- [ ] **Native graph backend** (P3) — for deployments needing deep traversal:
  - Neo4j or Memgraph backend for GraphStore
  - When: graph exceeds ~100K edges and queries need 4+ hop traversal (community detection, impact analysis, full dependency chains)
  - The current PostgreSQL recursive CTE approach works for 2-3 hops but degrades with depth

### Research: Graphiti Comparison & Feature Adoption

> **Reference:** [github.com/getzep/graphiti](https://github.com/getzep/graphiti) — temporal knowledge graph engine by Zep. Similar purpose (agent memory), different architecture (single graph DB, LLM-heavy extraction). Initial scan 2026-04-08; **deep code-level analysis 2026-04-11** (cloned repo, audited `graphiti_core/` end-to-end).
>
> **Headline findings from the deep audit:**
> - Graphiti is production-grade and feature-complete for its target use case (conversational agent memory). ~34K LOC in `graphiti_core/`, 45% of which is driver abstraction (Neo4j/FalkorDB/Kuzu/Neptune).
> - 24.8K GitHub stars, 2.5K forks, published arXiv paper (2501.13956), active hiring. The notoriety and credibility gap is the largest single difference between the two projects — and is almost entirely presentation/marketing, not capability.
> - Their architecture is **edge-centric temporal, LLM-heavy, single-store graph-DB-locked**. Ours is **node-centric temporal, deterministic-first, multi-store with local-first support**. These are genuinely different design points — neither dominates the other — but Graphiti has shipped and polished choices we still have in TODO.
> - Several things I assumed Graphiti had are more primitive than expected: community detection is label-propagation (not Louvain/Leiden); dedup is 3-stage (exact → MinHash/LSH → LLM) rather than LLM-first; saga nodes exist but minimal orchestration; REST server has no auth.
> - Several things I assumed we had parity on, we don't: their reranker abstraction (`CrossEncoderClient` with OpenAI/Gemini/BGE implementations), their token usage tracker aggregated per-prompt-name, their Jinja2 prompt library with typed response models, their OpenTelemetry tracing, and their CI polish (ruff + pyright + per-database integration workflows).
> - Several things we have that they don't: structured trace schema with steps/tool calls/outcomes, immutable audit trail via EventLog, governed mutation pipeline (validate → policy → idempotency → execute → emit), policy-based access control, blob store abstraction, pre-compression of context into Packs with token budgets and telemetry, and the whole classification/tagging infrastructure (ContentTags, signal_quality, retrieval_affinity).

#### Architecture Comparison (deep-audit update)

| Dimension | Trellis (ours) | Graphiti | Implication / status |
|-----------|----------------|----------|---------------------|
| **Storage model** | 6 independent stores (graph, doc, vector, trace, event, blob) composed via StoreRegistry | Single graph database with vectors as node properties | Graphiti has zero cross-store reference problems but is locked into graph DBs. We trade cross-store complexity for backend diversity and SQLite-based local dev. Our Cross-Store Reference Model TODO addresses the complexity cost. |
| **Backend support** | SQLite (default), PostgreSQL, LanceDB, pgvector, local/S3 blob | Neo4j, FalkorDB, Kuzu, Amazon Neptune | Graphiti has zero SQLite story; they can't run local-first without Redis (FalkorDB) or embedded Kuzu. We're the only option for truly local-first agent memory. |
| **Temporal granularity** | SCD Type 2 on **nodes** (`valid_from`/`valid_to`). Edges are point-in-time. | Bi-temporal on **edges** (`valid_at`, `invalid_at`, `expired_at`, `reference_time`). Nodes are mutable with current summaries. | Graphiti captures "when did this relationship become true" — we capture "when did this entity change." Neither is strictly better; they're complementary. **Adopting edge-level temporality is still the single highest-value feature to port.** |
| **Fact contradiction** | None. Last-write-wins. | LLM-judged. Old edge gets `invalid_at`, new edge gets `valid_at`, both preserved. | Genuine capability gap — but only matters if you ingest contradictable unstructured facts. For structured ingestion (UC catalogs, dbt manifests), upsert semantics are usually correct. |
| **Entity extraction** | Explicit — consumers write deterministic runners; no core extraction path exists for unstructured input | LLM-driven extraction from text/JSON/messages (1-2 LLM calls per episode) | **More nuanced than I initially framed it.** Our approach is correct *for known, stable domains* — writing a UC or dbt runner once, running it millions of times, is 100× cheaper than an LLM per row. But for **unknown or evolving domains** (new source systems, unstructured agent observations, arbitrary user content) we have **no story at all** — consumers have to write their own extraction logic from scratch. Graphiti's LLM-extraction approach is the right default for that case. The right architecture is **both**: deterministic extractors for known domains, LLM extractors for unknown domains, with a graduation path as patterns stabilize. See the new "Tiered Extraction Pipeline" section below. |
| **Entity dedup** | Deterministic alias system (`source_system, raw_id → entity_id`) | **3-stage pipeline**: exact match → MinHash/LSH with 32 permutations, Jaccard ≥0.9, entropy filter → LLM resolution for ambiguous cases (`utils/maintenance/dedup_helpers.py`) | Surprisingly sophisticated on Graphiti's side. We should adopt the **fuzzy match stage** (MinHash/LSH, no LLM cost) for the `save_memory` unstructured path — it sits between our alias exact-match and an LLM fallback. |
| **Classification** | Deterministic-first pipeline with LLM fallback (`ClassifierPipeline`, 4 deterministic classifiers + 1 LLM classifier). Goal: reduce LLM cost over time. | All classification is LLM (no deterministic tier). | Our approach is strictly better for cost/scale. Graphiti has nothing to adopt here; we should document this as a genuine differentiator. |
| **Retrieval strategies** | Keyword + semantic + graph, sectioned pack assembly with per-section budgets, tag filtering | BM25 + vector + BFS graph traversal, unified hybrid search | Both hybrid. Our sectioned packs are more sophisticated for multi-agent workflows. Their unified search has richer reranking (see below). |
| **Reranking** | Importance-weighted relevance scores (single pass) | **`CrossEncoderClient` abstraction** (`graphiti_core/cross_encoder/`) with **OpenAI reranker**, **Gemini reranker**, and **BGE local cross-encoder** (sentence-transformers). Additional rerank strategies: RRF, MMR, node-distance, episode-mention. | **Capability gap.** Rerankers are cheap to add (one new abstraction, 2-3 implementations) and integrate cleanly with our existing strategy protocol. This should be a TODO. |
| **Community detection** | None | **Label propagation** (not Louvain/Leiden). Simple vote-tally algorithm, O(V+E) per iteration. LLM called only to summarize the resulting clusters. (`utils/maintenance/community_operations.py`) | Simpler than I assumed — label propagation, not a fancy community algorithm. Still a gap for us but cheap to port. Already covered by our curated-node framework. |
| **Narrative/workflow** | `parent_trace_id` + `workflow_id` (implicit) | `SagaNode` with `first_episode_uuid`, `last_episode_uuid`, `last_summarized_at`. `NEXT_EPISODE` edges. Minimal orchestration — no state machines, no hierarchical sagas. | Graphiti makes this first-class but their orchestration is thin. Our TODO for "Workflow/saga entities" is roughly at parity with what they actually ship. |
| **Multi-tenancy** | Domain-based filtering via metadata | `group_id` on every node/edge/episode | Similar mechanism, different naming. Both are partition-by-field, not row-level security. |
| **LLM cost per ingestion** | ~0 LLM calls for deterministic pipeline; 1 call per ambiguous item in enrichment | **2-5 LLM calls per episode** (extract_nodes → dedup → extract_edges → dedup → summarize). Heavy. | Our cost profile is strictly better **for known domains where a deterministic runner exists**. For **unknown domains**, our cost is effectively infinite — we can't ingest them at all without bespoke runner work. Graphiti's cost profile is the inverse: high but finite for unknown domains. Neither is universally better; they're appropriate for different points in the domain-maturity lifecycle. The "Tiered Extraction Pipeline" section below closes this gap on our side. |
| **LLM provider support** | None (we have no LLM client abstraction in core — enrichment callers provide their own) | **7 providers** with unified `LLMClient` ABC: OpenAI, AzureOpenAI, OpenAIGeneric, Anthropic, Gemini, Groq, GLiNEr2 (local NER). Structured outputs via `response_model=PydanticModel`. Token tracker per prompt name. Optional response caching. | **Real gap.** We outsource LLM calls to consumers, which means every consumer reimplements the same client, retry, token tracking, caching. Adding an `LLMClient` abstraction in core (optional dep) would be a big DX improvement and cost us nothing in our deterministic-first philosophy — the abstraction is invoked only when the LLM path fires. |
| **Embedder abstraction** | None in core (vector store is populated by consumers) | **`EmbedderClient` ABC** with OpenAI, AzureOpenAI, Voyage, Gemini implementations | Similar to LLM client — we punt to consumers. Worth adopting a minimal abstraction to reduce consumer boilerplate. |
| **MCP tools exposed** | **10 tools** (`get_context`, `save_experience`, `save_knowledge`, `save_memory`, `get_lessons`, `get_graph`, `record_feedback`, `search`, `get_objective_context`, `get_task_context`) | **16 tools** covering temporal queries, community access, manual graph curation, per-entity graph context BFS, edge/episode deletion | Close in count. Theirs is more CRUD-oriented; ours is more macro-composed. Worth auditing their MCP surface for operations we're missing (entity-graph BFS context, delete paths). |
| **REST API** | FastAPI with full surface (mutations, retrieval, admin, graph UI) | FastAPI with async worker queue for ingestion (POST /messages returns 202), direct save for entities/edges, search by node/edge/episode/community | Both FastAPI. Graphiti's async queue pattern is worth adopting for bulk ingestion endpoints. Neither has auth built-in. |
| **Trace/provenance** | Structured `Trace` schema with steps, tool calls, outcomes, evidence, feedback. Immutable. | `EpisodicNode` stores raw content blob + `source_description` + `valid_at`. Less structure. | **We're strictly richer here.** Traces are first-class in our model; episodes are closer to "messages that got ingested." Worth emphasizing as a differentiator. |
| **Audit / event log** | `EventLog` ABC with append-only guarantee, emitted by every mutation | None. Direct mutations. | **We're strictly richer here.** Governance, replayability, debugging are all stronger. |
| **Governance pipeline** | `MutationExecutor` with 5 stages (validate → policy → idempotency → execute → emit) and pluggable Protocol-based handlers/gates | Direct database writes, no pipeline | **We're strictly richer here.** Worth calling out as a core differentiator. |
| **Policy / access control** | `PolicyType` (mutation/access/retention/redaction) with `Enforcement` modes (enforce/warn/audit_only) | None visible beyond `group_id` partitioning | **We're strictly richer here.** Important for enterprise/regulated deployments. |
| **Blob store** | Dedicated abstraction (local fs or S3) for large artifacts | None. Content stored as node properties. | **We're strictly richer here.** Graphiti would need a complete new store for artifacts >few KB. |
| **Tests** | 70 test files, unit-scoped with `tmp_path` + mocks | ~36 test files, split unit/integration, integration runs real Neo4j + FalkorDB containers | Roughly comparable scale; their integration harness is more mature. Their CI runs DB-in-container tests; ours doesn't. |
| **Type checking** | `mypy strict=false`, `warn_return_any=true`, `disallow_untyped_defs=false` | **Pyright basic mode** for core, **pyright standard** for server, both in CI | Their type-checking is stricter and runs in CI per-module. We should tighten and switch to pyright (or at least match the enforcement rigor). |
| **Linting** | ruff in CI (good) | ruff + pyright in CI (good), plus `make check` aggregate | Parity modulo the pyright addition. |
| **Prompt management** | None in core | **`prompts/` library** — ~1665 LOC of Jinja2 templates wrapped in Python functions returning `list[Message]`. Typed Pydantic response models for structured outputs. Versioned and tracked per prompt name. | Capability gap if/when we add an LLM path. Not a blocker now; worth copying the pattern later. |
| **Telemetry / tracing** | structlog logging, basic event log | OpenTelemetry spans on major operations (add_episode, search), Datadog/Honeycomb/Jaeger compatible, disabled by default | Capability gap. OpenTelemetry is low-cost to add (optional dep, no-op tracer by default) and is a credibility signal for enterprise adopters. |
| **Concurrency/rate-limiting** | None explicit | `SEMAPHORE_LIMIT` env var (default 10) gates parallel LLM calls to prevent rate-limit errors. Async worker queue in REST server. | Only matters when LLM path exists. TODO for later. |

#### Strategic assessment

Graphiti is a **1-D specialist** (conversational memory with temporal facts, heavy LLM, single-store graph DB). Trellis is a **2-D generalist** (agent memory *and* structured knowledge ingestion, deterministic-first, multi-store with local-first). The two projects are **not in direct competition** — they target overlapping but distinct use cases.

**Where we lose:**
1. **Notoriety and credibility signals** — 24.8K stars vs ours. Paper, blog posts, community. This is the single biggest gap and is mostly addressable via presentation, not code. See the new "Repository Polish & Credibility" section below.
2. **Edge-level temporality** — genuine capability gap. Already the highest-value feature on our Graphiti adoption list.
3. **Reranking surface** — they have a clean `CrossEncoderClient` abstraction with multiple implementations; we have importance-weighted scores. Cheap to add.
4. **LLM/embedder abstractions in core** — they own the client layer; we punt to consumers. Adding minimal ABCs would help consumers and cost us nothing.
5. **MinHash/LSH dedup stage** — surprisingly good deterministic dedup for fuzzy string matching. Cheap to adopt for `save_memory`.
6. **Type-checking rigor** — pyright in CI, stricter than our mypy config.
7. **OpenTelemetry tracing** — enterprise credibility signal, low effort.

**Where we win (and should market hard):**
1. **Governed mutation pipeline** — validate → policy → idempotency → execute → emit. No equivalent in Graphiti.
2. **Immutable audit trail via EventLog** — enterprise compliance, debugging, replayability.
3. **Policy/access control system** — none in Graphiti.
4. **Multi-store with local-first** — SQLite-to-production path. Graphiti cannot do this.
5. **Deterministic-first classification with a grow-over-time plan** — 10-100× cheaper than LLM-only *for the domains you've characterized*. See "Self-Learning Classification" for the roadmap that turns LLM outputs into deterministic rules.
6. **Structured trace schema** — steps, tool calls, outcomes, evidence, feedback are all first-class. Graphiti's `EpisodicNode` is just a content blob with a timestamp.
7. **Blob store abstraction** — artifact-friendly from day one.
8. **Sectioned pack assembly** — multi-agent workflows with per-section budgets. Graphiti has unified search only.
9. **Contextual pack telemetry** — `PACK_ASSEMBLED` events with full retrieval report enable effectiveness analysis loops Graphiti can't do.

**Caveat on advantage #5:** deterministic-first is only an advantage *once you've written the deterministic extractors*. For a new consumer spinning up against a domain they don't yet understand, Graphiti's LLM-extraction path is strictly better — they can ingest raw content immediately. Our advantage kicks in after the domain has been characterized and a runner written. The "Tiered Extraction Pipeline" section below closes this gap by adding an LLM-extraction path to the core library with a graduation mechanism that converts it into deterministic rules over time.

**Where we're at parity or better but they ship it and we don't yet:**
- FastAPI REST surface (both)
- MCP server (both; theirs has 16 tools, ours has 10)
- Hybrid retrieval (both)
- CI lint + tests (both)
- Python packaging (both)

#### Features to Evaluate for Adoption

- [ ] **Temporal edge validity (P0, highest ROI)** — Add `valid_from`/`valid_to` to edges, matching the existing SCD Type 2 pattern on nodes. Enables: "when did this dependency start?", "what were the relationships at time T?", tracking when facts change without losing history. This is the highest-value Graphiti concept to adopt — it extends an existing pattern we already have on nodes. Implementation: mirror the node-side SCD Type 2 in `GraphStore.upsert_edge` / `get_edge_history` / `list_edges(as_of=...)`. Postgres and SQLite backends both need migrations. The schema change is additive (existing edges get `valid_from=created_at`, `valid_to=None`).

- [ ] **Contradiction detection on upsert** — When `upsert_node` receives properties that conflict with existing properties from a different `source_of_truth`, flag it rather than silently overwriting. Store both versions with temporal validity. Requires the `source_of_truth` field proposed in the cross-store reference model. Start simple: detect numeric/date/enum conflicts; ignore free-text differences.

- [ ] **Community detection** — Auto-cluster entities into domain groups using graph structure (Louvain, Leiden, or label propagation). Creates community cluster nodes with summary embeddings. Enables: "tell me about the sportsbook domain" → returns pre-computed cluster summary rather than individual entity lookups. Evaluate: does this provide value over manual `domain` metadata tags? Where does it discover structure that manual tagging misses? **Implementation note:** community nodes should use `node_role=CURATED` with `generation_spec.generator_name="community_detection_<algorithm>"` from the "Graph Modeling Guidance, Node Roles & Curation" framework — do not introduce a parallel `COMMUNITY` entity type or a separate regeneration mechanism. The curated-node framework already covers regeneration, freshness tracking, and retrieval boosting.

- [ ] **Workflow/saga entity** — Promote workflow runs from implicit (`workflow_id` on traces) to explicit first-class entities. A `WORKFLOW_RUN` node links to its traces via ordered edges. Enables: "show me all runs of the SQL generation workflow", "what was the last successful run?", "compare run 5 vs run 6." Natural fit for the multi-agent pipeline use case. **Role:** workflow runs are `node_role=SEMANTIC` (they are real things that happened, not synthesis) — do not confuse with curated nodes.

- [ ] **LLM-assisted dedup for unstructured observations** — [blocked: LLMClient ADR (Sprint C)] The `save_memory` path ingests unstructured agent observations. These don't have clean `(source_system, raw_id)` aliases. Evaluate Graphiti's LLM dedup approach for this path: before creating a new document/entity from `save_memory`, compare against recent similar items via embedding similarity + LLM confirmation. The deterministic alias system stays for structured ingestion; LLM dedup activates only for unstructured input.

- [ ] **Reranking strategies + `CrossEncoderClient` abstraction (P1)** — RRF + MMR are deterministic and safe to ship now (Sprint E). `BGECrossEncoder` and API-based cross-encoders are [blocked: LLMClient ADR (Sprint C)]. Graphiti offers RRF, MMR, cross-encoder (`graphiti_core/cross_encoder/` with OpenAI, Gemini, and BGE local implementations), node-distance, and episode-mention reranking. Trellis currently uses importance-weighted relevance scores only. Implementation plan:
  1. Add `Reranker` Protocol in `src/trellis/retrieve/rerankers/base.py` with `rank(query: str, candidates: list[PackItem]) -> list[RankedItem]`
  2. Ship three built-in rerankers: `RRFReranker` (deterministic, no LLM, combines heterogeneous score distributions from our keyword/semantic/graph strategies — highest ROI), `MMRReranker` (deterministic, adds diversity — useful against the "sectioned packs all returning similar items" problem), `BGECrossEncoder` (local sentence-transformers model, optional dependency via `[rerank]` extra)
  3. `PackBuilder` accepts optional `reranker: Reranker | None` parameter. Applied after strategy union + dedup, before budget enforcement.
  4. Ship `CrossEncoderClient` abstraction (mirroring our LLM client approach) only if/when we add the LLM-based rerankers
  5. Tests: per-reranker unit tests with synthetic candidates; integration test showing RRF improves pack quality on a fixture scenario
  6. Reference: Graphiti's `graphiti_core/cross_encoder/bge_reranker_client.py` is a ~80-line implementation worth copying as a starting point

- [ ] **MinHash/LSH deterministic dedup stage (P1, for `save_memory`)** — Graphiti's 3-stage dedup (`utils/maintenance/dedup_helpers.py`): exact match → MinHash with 32 permutations + LSH banding + Jaccard ≥0.9 + entropy filter → LLM resolution. The middle stage is **deterministic and LLM-free** and catches fuzzy duplicates (typos, casing, punctuation) without cost. We should adopt this for the `save_memory` unstructured observation path and for document ingestion — it sits between our existing exact-match alias system and a future LLM fallback. Implementation:
  1. New module `src/trellis/classify/dedup/minhash.py` with `MinHashIndex` class (configurable permutations, shingles size, similarity threshold)
  2. Integrate into `save_memory` path: before creating a new document, hash + check index, return existing doc ID if Jaccard ≥ threshold
  3. Index populated on ingestion; stored in a new `dedup_signatures` SQLite/Postgres table for persistence
  4. **Cost consideration**: index grows with document count; evaluate memory footprint at 10K/100K/1M scale. Optional LRU eviction based on recency.
  5. Tests: exact duplicate detection, fuzzy duplicate detection (typos), entropy filter (short/generic names skipped), false-positive rate measurement on a synthetic corpus
  6. Reference: `graphiti/graphiti_core/utils/maintenance/dedup_helpers.py` lines 39-200+

- [ ] **`LLMClient` + `EmbedderClient` abstractions in core (P2, optional dependency)** — **Sprint C ADR decides this.** Many other items block on it. — Graphiti owns the LLM and embedder client layer via ABCs (`graphiti_core/llm_client/client.py`, `graphiti_core/embedder/client.py`) with unified `async _generate_response(response_model=...)` and `async create(input_data)` interfaces, retry logic, token tracking, and optional response caching. Trellis currently punts this to consumers — every consumer reimplements retry, token tracking, caching. Plan:
  1. Add `src/trellis/llm/` module with `LLMClient` Protocol, `TokenUsageTracker`, optional `ResponseCache` (hashed by blake2b of messages), and one or two built-in implementations gated behind optional dependencies (`[llm-openai]`, `[llm-anthropic]`)
  2. Add `src/trellis/embeddings/` with `EmbedderClient` Protocol and built-in OpenAI implementation gated behind `[embed-openai]`
  3. These are **optional** — the core library never imports them at runtime; only the enrichment pipeline and (future) rerankers use them
  4. This is purely a DX improvement for consumers building enrichment workers; it does not change our deterministic-first philosophy
  5. Existing `LLMFacetClassifier` gets refactored to accept a `LLMClient` instance
  6. Reference: Graphiti's `llm_client/client.py` (~300 LOC) is the template — we don't need 7 provider implementations, one or two is enough for v1

- [ ] **Prompt library pattern (P2, only if LLM abstractions land)** — [blocked: LLMClient ADR (Sprint C)] — Graphiti ships `graphiti_core/prompts/` (~1665 LOC) as a library of Jinja2 templates wrapped in Python functions returning `list[Message]`. Each prompt has a typed Pydantic response model. Prompts are versioned and tracked per `prompt_name` for token usage aggregation. If we add the LLM client abstraction, adopt this pattern for the enrichment prompts rather than inline f-strings. Reference: `graphiti/graphiti_core/prompts/extract_nodes.py` as the template.

- [ ] **OpenTelemetry tracing (P2, credibility signal)** — Graphiti supports OTel spans on major operations (`add_episode`, `search`) with custom attributes (query length, reranker scores), compatible with Datadog/Honeycomb/Jaeger, disabled by default via a no-op tracer. Low implementation cost (optional dep, `[tracing]` extra), real enterprise credibility signal. Add spans to: `MutationExecutor.execute`, `PackBuilder.build` and `build_sectioned`, each `SearchStrategy.search`, `ClassifierPipeline.classify`, `MCP tool invocations`. Use standard OTel conventions (span kind, status codes). Document in `docs/agent-guide/observability.md`.

- [ ] **Async worker queue for bulk ingest (P2)** — Graphiti's REST server wraps `POST /messages` in an async worker queue (`AsyncWorker` class, FIFO, returns 202) to decouple request latency from ingestion processing. Adopt the same pattern for our bulk ingest endpoint (`POST /api/v1/ingest/bulk` — already on TODO in the Unity Catalog Backfill section). Reference: `graphiti/server/graph_service/routers/ingest.py`.

- [ ] **MCP tool parity audit (P2)** — Graphiti's MCP server exposes 16 tools; ours exposes 10. Audit which of their tools fill capability gaps for us:
  - `get_entity_graph_context(entity_id, depth)` — BFS neighbors with context. **Worth adding.** Enables agents to ask "show me everything connected to entity X out to 2 hops."
  - `list_facts_by_entity(entity_id)` — incoming + outgoing edges for one entity. **Worth adding.** Currently requires consumers to call `search` with awkward filters.
  - `delete_episode(uuid)` — cascade deletion. **Skip** — our traces are immutable by design.
  - `delete_entity_edge(uuid)` — edge removal. **Skip for now** — should go through the mutation pipeline, not a direct MCP tool.
  - `list_available_entity_types()` — enumerate types. **Worth adding** — matches our "Per-implementation skill context" TODO in the Domain-Specific Entity Types section.
  - `update_entity_summary(uuid, summary)` — manual curation. **Map to `trellis curate edit`** from the curated-node framework rather than a direct MCP call.
  - `retrieve_latest_episodes(n)` — time-windowed retrieval. **Worth adding** as `get_recent_traces(n, since)`.
  - Each addition lands in `src/trellis/mcp/server.py` and gets a test in `tests/unit/mcp/`.

- [ ] **Write-up: `docs/research/graphiti-comparison.md`** — Full analysis with code examples showing how each system handles the same scenario (e.g., "agent discovers a new table dependency, records it, later the dependency changes, then queries what existed at time T"). Clarify where Trellis's design is intentionally different vs. where it has gaps. Should become a permanent comparison doc (not just a one-time research note) since Graphiti is the most frequent competitive reference — potential adopters will ask about it. Cross-link from README in a "How does this compare to Graphiti?" FAQ section.

#### What NOT to Adopt (and why)

- **Single-store architecture** — Graphiti's choice eliminates cross-store problems but locks you into graph databases. XPG's multi-store design supports SQLite (local dev), PostgreSQL (production), LanceDB (semantic search) independently. The generalizability goal requires backend diversity. Solve cross-store references via the graph-as-directory pattern instead.

- **LLM-driven extraction for all ingestion** — Graphiti calls an LLM for every entity extraction, edge extraction, dedup check, and summarization. This is appropriate for conversational memory (few messages, high value per message). XPG ingests thousands of UC tables, dbt models, and metadata records — LLM cost would be prohibitive. Keep the deterministic-first pipeline; add LLM paths only where deterministic fails (unstructured input, ambiguous dedup).

- **Vectors-in-graph** — Storing embedding vectors as graph node properties works when you have one database. With separate stores, embedding storage belongs in the dedicated VectorStore (LanceDB, pgvector) which is optimized for ANN indexing. Cross-referencing via `item_id = doc_id` convention is sufficient.

### Repository Polish & Credibility Signals

> **Why this section exists:** The deep Graphiti audit (2026-04-11) made it clear that the biggest single difference between Trellis and Graphiti is not capability — it's **presentation, credibility, and discoverability**. Graphiti has 24.8K stars, a published arXiv paper, blog posts, a dedicated Discord, and polished CI badges. Trellis has none of these yet. Every item in this section is low-risk, reversible, and mostly doesn't touch code — but together they close the credibility gap that determines whether a skeptical engineer gives the project 30 seconds or 30 minutes of attention.
>
> **Priority framing:** These are **not nice-to-haves**. The order of operations for an open-source project being discovered by potential users is: see a README badge → trust the project enough to read the README → trust the README enough to try the quickstart → trust the quickstart enough to integrate. Each step has a 50-80% drop-off. Missing badges and an unlinted README cause the first drop, and you never get a second chance. Graphiti nailed this; we haven't yet.

#### Badges (README, top of file, first thing a visitor sees)

- [ ] **CI status badge** — `[![CI](https://github.com/OWNER/trellis-ai/actions/workflows/ci.yml/badge.svg)](...)`. Already have `ci.yml` — just needs the badge Markdown added to README. **5 minutes.**

- [ ] **Ruff lint badge** — either reuse the CI badge (if CI runs lint) or add a dedicated `lint` workflow with its own badge. Graphiti has a dedicated `lint.yml` workflow → dedicated badge. We should do the same: split `ci.yml` into `lint.yml`, `typecheck.yml`, `tests.yml` so each gets its own badge and its own green checkmark. Graphiti's pattern in `.github/workflows/lint.yml` is the template. **30 minutes.**

- [ ] **Type-check badge** — `[![Type Check](https://github.com/OWNER/trellis-ai/actions/workflows/typecheck.yml/badge.svg)](...)`. Requires splitting the `ci.yml` typecheck job into its own workflow. **15 minutes.**

- [ ] **Test status badge (with matrix)** — badge per Python version (3.11, 3.12, 3.13) or one aggregate badge. Prefer one aggregate; matrix badges are visually noisy. **10 minutes** after the split.

- [ ] **Coverage badge** — requires setting up coverage reporting. Options:
  - **Codecov** (`codecov.io`, free for open source) — standard, widely trusted badge, integrates via GitHub Action. Add `pytest --cov=src --cov-report=xml` to test workflow, upload via `codecov/codecov-action@v4`, add badge.
  - **Coveralls** — similar, slightly less common.
  - **No coverage badge** — if coverage is currently low or uneven, ship the badge after improving coverage. Do not ship a badge showing 40%.
  - Decision gate: measure current coverage first (`pytest --cov=src`), decide based on result. Target ≥80% before shipping the badge. **1-2 hours** including workflow changes.

- [ ] **PyPI version badge** — `[![PyPI](https://img.shields.io/pypi/v/trellis-ai.svg)](https://pypi.org/project/trellis-ai/)`. Gated on actually publishing to PyPI (already in the "In Progress — PyPI Publishing" section). **5 minutes** after publish.

- [ ] **Python versions supported badge** — `[![Python](https://img.shields.io/pypi/pyversions/trellis-ai.svg)]`. Auto-renders from PyPI classifiers. **5 minutes** after publish.

- [ ] **License badge** — `[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)`. **2 minutes.**

- [ ] **Downloads badge** — `[![Downloads](https://static.pepy.tech/badge/trellis-ai/month)](https://pepy.tech/project/trellis-ai)`. Gated on PyPI publish, shows momentum. Only add once downloads are non-embarrassing (e.g., after first announcement push). **5 minutes** when ready.

- [ ] **GitHub stars badge** (optional) — `[![GitHub stars](https://img.shields.io/github/stars/OWNER/trellis.svg?style=social)](...)`. Only useful after visible community activity. Do not add while the number is low.

- [ ] **"Powered by" / "Built with" badges** (optional, if we ship specific integrations) — MCP, Claude, OpenAI, etc. Use sparingly; too many badges is worse than too few.

#### Workflow file hygiene

- [ ] **Split `ci.yml` into per-concern workflows** — `lint.yml`, `typecheck.yml`, `tests.yml`. Each gets its own badge. Pattern from Graphiti: one workflow per concern, clear naming, each workflow has a single `name:` matching the badge. This also makes CI faster because failures in one concern don't block the others.

- [ ] **Add database integration test workflow** — Graphiti has `database_integration_tests.yml` that spins up Neo4j and FalkorDB in containers via `services:` in the workflow. We should do the same for PostgreSQL + pgvector to ensure the production backend is actually tested in CI, not just mocked. Currently our CI only exercises SQLite. Reference: `graphiti/.github/workflows/database_integration_tests.yml`.

- [ ] **Claude Code review workflow** — Graphiti has `.github/workflows/claude-code-review.yml` and `claude.yml` for AI-assisted PR review. We already have these (checked in). Confirm they're configured correctly and mention in CONTRIBUTING.md as a signal that AI-assisted development is first-class here.

- [ ] **CodeQL scanning** — Graphiti has `codeql.yml` for security scanning. Add ours. GitHub provides a one-click setup via the Security tab. **5 minutes** (no code needed).

- [ ] **Release workflow** — `release.yml` that builds and publishes to PyPI on tag push. Already in the PyPI Publishing TODO; just make sure it lands before the first release.

#### README polish

- [ ] **Hero tagline** — one-sentence value prop in italics under the title, before any prose. Current README opens with "A structured context graph and experience store for AI agents." That's good but buries the differentiator. Something like:
  > *Structured, governed, local-first memory for AI agents — immutable traces, policy-gated mutations, and token-budgeted context retrieval. Built for teams that need auditability and reproducibility, not just a vector store.*
  Target: make it obvious in 10 seconds what Trellis is and why it's different from a memory framework or a vector DB.

- [ ] **"How does this compare to X?" FAQ section** — a short table comparing Trellis to Graphiti, Mem0, Zep, LangChain Memory, Letta (formerly MemGPT), and plain pgvector/Chroma. Include the Graphiti comparison as the lead example. This is the question every skeptical adopter asks; answering it prominently converts skeptics into trial users. Cross-link to `docs/research/graphiti-comparison.md` for the long version.

- [ ] **Quickstart section tightening** — current README has diagrams but the "how to actually run this" path is not top-of-page. Pattern from Graphiti (which has a 600-line README, but the quickstart is up top): tagline → badges → 5-line install + first-run snippet → then the deep dive. First-run snippet should produce visible output in <60 seconds from a clean venv.

- [ ] **Sub-minute demo GIF** — already in the "Demo & Content" TODO. The deep audit reinforces this is important: Graphiti's README has architecture diagrams but no GIF, which is actually an opportunity — a GIF showing "retrieve → act → record → retrieve smarter next time" would be a unique differentiator in the space.

- [ ] **Link to paper / blog post** — Graphiti links to [arXiv:2501.13956](https://arxiv.org/abs/2501.13956) prominently. We don't have a paper (yet), but linking to a blog post explaining the design philosophy (governed mutations, immutable traces, why deterministic-first classification matters) would serve the same credibility function. **Gated on writing the blog post.** Deferred until there's content to link.

- [ ] **"Used by" or "Adopters" section** — even if it's just "used internally at [company]" or "built for [project]", showing that *someone* runs this in production is a credibility signal. Gated on having users willing to be named.

- [ ] **CONTRIBUTING.md** — Graphiti has one; we should too. Topics: how to run tests, how to add a new store backend, how to extend the classification pipeline, how AI-assisted development works (reference to CLAUDE.md), PR process. Should be short (≤200 lines) and actionable.

- [ ] **Code of Conduct** — Graphiti has `CODE_OF_CONDUCT.md`. Adopt the Contributor Covenant. **5 minutes** (standard template).

- [ ] **Security policy** — Graphiti has `SECURITY.md`. Ours should explain: supported versions, how to report a vulnerability (private channel), expected response time. **15 minutes** (template).

- [ ] **Changelog** — Graphiti has implicit changelog via releases. We should have `CHANGELOG.md` in Keep-a-Changelog format, updated on every release. Gated on the first release.

#### Documentation polish

- [ ] **Docs site (optional, P2)** — Graphiti has a docs site linked from the README. Options:
  - **mkdocs-material** — cheapest path, renders our existing `docs/` markdown into a site. Deploy via GitHub Pages. ~30 minutes setup.
  - **Docusaurus / Mintlify / Fumadocs** — prettier, more effort.
  - **Just keep `docs/` on GitHub** — fine for v1, less polished. Decision: start with mkdocs-material before the first public announcement, not before.

- [ ] **Architecture diagram in README** — current README has ASCII diagrams (good! unique!). Supplement with a rendered mermaid or SVG architecture diagram for visitors who skim. Place after the tagline, before the deep dive.

- [ ] **Type-check strictness increase** — Graphiti uses pyright in basic mode for core and standard mode for server. We use `mypy strict=false` with most warnings off. Tighten over time:
  1. Phase 1: enable `disallow_untyped_defs = true` for `src/trellis/schemas/`, `src/trellis/mutate/`, `src/trellis/stores/base/` (the parts with the clearest contracts)
  2. Phase 2: extend to `src/trellis/retrieve/` and `src/trellis/classify/`
  3. Phase 3: evaluate switching mypy → pyright (pyright is faster, has better error messages, and is what Graphiti uses — community signal that it's the modern default for new projects)
  4. Each phase is its own PR so contributors see incremental rigor

- [ ] **`py.typed` marker file** — ships typed stubs with the package so downstream users get type checking. Graphiti has this. One-line file in `src/trellis/py.typed` + `include` directive in `pyproject.toml`. **5 minutes.**

- [ ] **`Makefile` parity with Graphiti** — Graphiti has `make install`, `make format`, `make lint`, `make test`, `make check`. We have similar targets in our existing Makefile; confirm they all work and add `make check` as the aggregate (format + lint + typecheck + test) if not present.

#### Community & visibility

- [ ] **Discord or GitHub Discussions** — Graphiti has a Discord. GitHub Discussions is zero-setup and lives inside the repo. Enable Discussions and pin a welcome thread before any external announcement. **5 minutes.**

- [ ] **GitHub topics / tags** — add topics to the repo settings: `ai-agents`, `knowledge-graph`, `agent-memory`, `mcp`, `llm-memory`, `rag`, `temporal-graph`, `llm-tools`. Improves GitHub search discoverability. **2 minutes.**

- [ ] **Submit to awesome-* lists** — already in the Discoverability TODO. Key targets: `awesome-mcp-servers`, `awesome-llm-apps`, `awesome-agents`, `awesome-ai-agents`. Each one is a 1-2 line PR.

- [ ] **HN Show / Launch post** — when ready (post-PyPI publish, post-quickstart tightening, post-demo-GIF). Template: "Show HN: Trellis — governed, local-first memory for AI agents with auditable traces." Time it for a Tuesday or Wednesday morning US Eastern for best visibility. Have badges, demo, and docs ready before posting.

- [ ] **Blog post: the design philosophy** — one-shot thought-leadership post explaining: why deterministic-first classification matters, why immutable traces matter, why multi-store architecture matters. Serves as the "paper substitute" that lets us link serious content from the README. Target: Substack, dev.to, or a dedicated blog. ~2000 words.

- [ ] **Comparison blog post** — "Trellis vs Graphiti vs Mem0: choosing an agent memory system." Not a hit piece — a genuine "these tools are for different things" explainer. Cross-promotes all three projects, builds goodwill, converts people who are shopping specifically by comparing. Gated on the design philosophy post landing first.

- [ ] **Conference / meetup submissions** — once the project is public and there's at least one production user, submit talks to: PyCon, ML/AI meetups, LLM DevDay, FOSDEM. Prepared slides reusable across venues.

#### Ordering and dependencies

The suggested execution order for this section, front-loaded by ROI:

1. **Immediate (before any public announcement), ~4 hours total:**
   - Split `ci.yml` into lint/typecheck/tests workflows + add badges
   - Add license badge, Python versions badge (after PyPI publish)
   - Add `py.typed` marker
   - Enable GitHub Discussions
   - Add GitHub topics
   - Tighten README hero tagline

2. **Before first external announcement (HN/blog), ~1 day total:**
   - CONTRIBUTING.md + Code of Conduct + SECURITY.md
   - Coverage measurement + optional Codecov integration (only ship badge if ≥80%)
   - README "how does this compare to X?" section
   - Quickstart tightening (5-line install + 60-second first-run)
   - Rendered architecture diagram alongside the ASCII one
   - Demo GIF (from Demo & Content section)

3. **Before v1.0 release, ~2-3 days:**
   - Database integration test workflow (Postgres + pgvector in CI)
   - Type-check strictness phase 1 (schemas/mutate/stores)
   - Design philosophy blog post
   - Graphiti comparison doc (`docs/research/graphiti-comparison.md`)
   - CHANGELOG.md
   - Release workflow (PyPI auto-publish on tag)

4. **Post-v1.0, ongoing:**
   - Coverage improvement to ≥80%
   - Type-check strictness phases 2 and 3
   - Comparison blog post
   - mkdocs-material docs site
   - Submit to awesome-* lists
   - Conference submissions
   - Downloads badge, stars badge (once numbers are non-embarrassing)

### More Framework Integrations
- [ ] CrewAI integration (`integrations/crewai/`)
- [ ] AutoGen integration (`integrations/autogen/`)
- [ ] Contribute integration PRs upstream to each framework's repo
