# ADR: Deferred Cognition — LLM Enrichment Is Not in the Write Path

**Status:** Accepted
**Date:** 2026-04-11
**Deciders:** Trellis core
**Related:**
- [`../research/memory-systems-landscape.md`](../research/memory-systems-landscape.md) — Landscape comparison that surfaced this principle
- [`../research/client-layer-inventory.md`](../research/client-layer-inventory.md) — The "us" baseline this ADR governs
- [`./classification-layer.md`](./classification-layer.md) — Two-mode classifier pipeline already in code
- [`./context-economy-strategy.md`](./context-economy-strategy.md) — Retrieval budgeting and effectiveness loop

---

## 1. Context

Trellis is a governed experience graph for fleets of AI agents. Ingestion runs at the critical path of agent execution: every trace ingest, every entity upsert, every document put happens while an agent is waiting on a response. The landscape comparison (`memory-systems-landscape.md`) surfaced a sharp architectural split across comparable systems:

| System | LLM in write path? | What the LLM decides at write time |
|---|---|---|
| **Graphiti** (getzep) | Yes | Fact extraction, entity resolution, edge invalidation, temporal overwrite, community rebuild. ~4–5 LLM roundtrips per `add_episode`. |
| **Mem0** | Yes | Fact extraction + 4-op arbiter (`ADD`/`UPDATE`/`DELETE`/`NOOP`) over existing memories. |
| **Zep** | Yes | Dialogue-grounded fact extraction and semantic summarization. |
| **cognee** | Yes | "Cognify" stage applies LLM + optional ontology during ingest to produce DataPoints. |
| **Letta** | In-agent | The agent itself edits memory blocks via tool calls during its own turn. |
| **Cursor** | **No** | Deterministic tree-sitter + Merkle-diff + embed. |
| **Claude Code** | **No** | Files on disk; the human writes them. |
| **Trellis** | **No** (this ADR) | Deterministic classifiers only; all LLM inference is deferred. |

Systems that put an LLM in the write path pay a recurring tax: non-determinism on replay, unbounded cost per ingest, policy-evasion (the LLM decides what gets written), opaque auditability, and hard failure modes when the model provider is degraded or rate-limited. They gain one thing: richer structure on the very first read.

Trellis's non-functional requirements make the trade-off clear in the opposite direction:

- **Immutable traces** are a hard rule. Any LLM decision made during ingest becomes a non-reproducible part of the audit record.
- **Governed 5-stage pipeline** (`validate → policy → idempotency → execute → emit event`) requires that handlers be deterministic so idempotency keys and replay produce stable outcomes.
- **Fleets of agents** ingesting concurrently cannot tolerate an LLM call in every write. Cost is `O(writes × model_latency)`, which is the wrong scaling shape for a shared memory substrate.
- **Pluggable stores** must work in pure-SQLite local mode with no network dependencies at all.
- **Policy enforceability.** A write that went through an LLM has already made decisions the policy gate has no way to reason about.

We also observed that Trellis has two facilities that the other systems do not: an effectiveness feedback loop (`apply_noise_tags`) and a retrieval pack telemetry channel (`PACK_ASSEMBLED`). These give us something nobody else has — a way to learn from *outcomes* rather than from *inputs* — which only works if intelligence is applied after the fact, when outcomes are observable.

## 2. Decision

**Writes deposit structure. Intelligence is applied after the fact by enrichment agents operating through the same governed Command pipeline.**

Concretely:

1. **`MutationExecutor` handlers must be deterministic.** No LLM calls, no network inference, no stochastic decisions. Handlers may call deterministic classifiers (regex, keyword, graph-neighbor lookup) but must produce the same output for the same input on replay.
2. **Enrichment happens through the same `MutationExecutor`, just later.** Enrichment agents are consumers of event streams that emit Commands back into the pipeline. They are not a second write path — they are the same write path, invoked with later timestamps and richer inputs.
3. **Retrieval is the observation surface.** The enrichment agent's signal is `PACK_ASSEMBLED × FEEDBACK_RECORDED`, not raw traces. It learns from what worked and what didn't, not from guessing at ingest time.
4. **Cold-start quality comes from deterministic classifiers only.** Every classifier that runs in the write path must be reproducible, pure, and cheap (microsecond order). The LLM classifier is exclusively an enrichment-mode citizen.
5. **Structural provenance is captured, not inferred.** `node_role` (structural / semantic / curated) and `GenerationSpec` are set explicitly by the caller at write time. If a caller cannot commit to a role, the default is `semantic`. LLM-inferred roles only come via curated-node promotion in enrichment.

### Write-path classification policy

The classifier pipeline (`src/trellis/classify/`) already has two modes. This ADR freezes which classifiers are allowed in each mode:

| Classifier | Ingestion (write path) | Enrichment (deferred) | Why |
|---|---|---|---|
| `StructuralClassifier` | ✅ | ✅ | Pure deterministic regex/type lookup. No graph context, no network. |
| `SourceSystemClassifier` | ✅ | ✅ | Source URI → system tag. Pure string parsing. |
| `KeywordDomainClassifier` | ✅ *only for covered domains* | ✅ | Deterministic keyword match. If no rule hits, emit no tag and defer. Do **not** fall through to LLM inline. |
| `GraphNeighborClassifier` | ❌ | ✅ | Needs graph state at query time, which changes between ingest and enrichment. Running it inline bakes in a transient view. |
| `LLMFacetClassifier` | ❌ | ✅ | LLM call. Non-deterministic, non-reproducible, unbounded cost. Enrichment only. |

Cold-start retrieval quality from deterministic classifiers alone is deliberately "good enough to retrieve, not good enough to reason about." The enrichment agent closes the gap.

## 3. Consequences

### Positive

- **Deterministic replay.** Re-running any Command from the event log produces byte-identical state. Audit holds.
- **Bounded ingest cost.** Ingest throughput is governed by store I/O, not by a model provider's rate limit or pricing page.
- **Graceful degradation.** If the LLM provider is down or policy-blocked, ingestion continues. Only enrichment stalls, and it catches up when the provider recovers.
- **Policy-enforceable enrichment.** Enrichment agents act through `MutationExecutor`, so their writes go through `PolicyGate` like any other mutation. You can have a policy like "LLM-inferred edges require a curated-node parent before they are retrievable" and it actually takes effect.
- **Outcome-driven learning.** Because enrichment sees `PACK_ASSEMBLED` and `FEEDBACK_RECORDED`, it tunes classification toward what actually helps agents, not toward a priori guesses. This is a capability none of the other systems have.
- **Pure-local viability.** The minimal Trellis install (SQLite stores, no enrichment worker) still produces a functional experience graph. The LLM is an optional service, not a substrate.
- **Correct layering for fleets.** Many agents write; few enrichment workers read. The expensive step happens on the narrow side of the fan-out.

### Negative / trade-offs

- **Worse cold-start retrieval quality.** Until an item has been through enrichment, it is tagged only by what deterministic classifiers could see. First-read packs are less semantically dense than Graphiti's or Zep's. This is the trade-off.
- **Eventual consistency at the meaning layer.** A newly-ingested trace will appear in retrieval with only its structural/source tags; richer `content_tags`, curated nodes, and inferred edges arrive asynchronously. Agents must tolerate this.
- **Enrichment queue lag is a new operational concern.** We now have a backlog to monitor. If enrichment falls behind, quality silently degrades (it doesn't fail, it just stops getting better). Observability must cover enrichment lag.
- **Two-mode classifier pipeline complexity.** Operators must understand which classifiers run in which mode. The registration API and docs must make this obvious.
- **We forfeit one category of "magic" demo.** Graphiti's demo is "paste a chat, watch a graph appear." Ours is "paste a chat, watch a deterministic skeleton appear, come back in five minutes, see a richer graph." Less theatrical.

### Neutral

- **Retrieval latency is unchanged.** The LLM was never in the read path in the first place; it was only the write path this ADR removes it from.
- **Schema is unchanged.** `ContentTags`, `GenerationSpec`, and `node_role` already support the deferred model.

## 4. Enrichment agent design

An "enrichment agent" is a background service that consumes event streams and emits Commands into `MutationExecutor`. Its job is to turn the deterministic skeleton into a richer graph over time, using both LLM inference and graph traversal.

### 4.1 Four needs

An effective enrichment agent requires four things:

1. **Observation.** It must see the inputs (raw traces, documents) *and* the deterministic-layer outputs (initial tags, structural role).
2. **Outcome.** It must see which retrieved packs worked (`PACK_ASSEMBLED` ↔ `FEEDBACK_RECORDED` correlation).
3. **Correlation.** It must be able to connect observation and outcome, usually via `pack_id → item_ids → source_ids`.
4. **Decision.** It must be able to emit Commands back into `MutationExecutor` under a policy that gates LLM-originated writes.

### 4.2 Three mechanism options

| Mechanism | How it triggers | Strengths | Weaknesses |
|---|---|---|---|
| **Scheduled sweeps** | Cron-style job enumerates recent events and enriches newest-first | Simple, predictable cost envelope, easy to rate-limit | Latency can be minutes to hours; wasted work on items that will never be retrieved |
| **Triggered enrichment** | Subscribes to `TRACE_INGESTED`, `NODE_UPSERTED`, `PACK_ASSEMBLED` events and enriches reactively | Low latency, work is proportional to what agents actually do | More complex queueing; must handle retries and dedupe against scheduled sweeps |
| **Long-running Claude Code enrichment skill** | A `SKILL.md` defines the enrichment procedure; a claude-code agent wakes on a schedule or signal and runs the skill | Uses the exact same agent substrate we're optimizing for; lets humans read/override the procedure | Adds Claude Code as a runtime dependency; harder to reason about cost; wall-clock unpredictable |

Initial implementation should be **triggered enrichment on a bounded queue with a scheduled backstop sweep**. The Claude Code skill variant is a good fit for the "promote to curated" path where a human-readable procedure is an asset, not overhead.

### 4.3 Actions the enrichment agent can take

All of these flow through `MutationExecutor` as Commands. None of them bypass the pipeline.

- **Re-tag items.** Run `LLMFacetClassifier` on a document or entity that deterministic classifiers could not cover. Emits a `TAG_REVISED` event.
- **Create edges.** Add semantic or graph-inferred edges between existing nodes. Source is `generation_spec={"kind": "llm", "model": ..., "prompt_hash": ...}`.
- **Promote precedents.** When a pattern of successful retrieval repeats, promote the shared items into a curated precedent node with `node_role="curated"`.
- **Create curated nodes.** Synthesize summaries, cluster labels, or taxonomy nodes as `node_role="curated"` with full `GenerationSpec`. The role is immutable across later versions.
- **Emit `CLASSIFICATION_REVISED` events.** For downstream consumers that rebuild retrieval indexes.
- **Demote to noise.** Via `apply_noise_tags()`, mark items that consistently hurt retrieval quality with `signal_quality="noise"` so `PackBuilder` excludes them by default.

### 4.4 Boundaries the enrichment agent must respect

- It operates **only through `MutationExecutor`**. No direct store writes. The pipeline is the interface.
- It cannot mutate historical versions. It can only emit new versions (SCD Type 2) or new events (immutable log).
- It cannot change a node's `node_role`. To "upgrade" a node's role, it must *create a new curated node* and link to the original — role immutability is a hard rule.
- Its mutations must carry `generation_spec` when `node_role="curated"` so the provenance chain is complete.
- Its mutations must pass through `PolicyGate` like any other write. Operators can gate LLM-originated Commands behind explicit policies (e.g., "LLM-inferred edges require approval before they are visible to agents outside the `exploration` domain").
- It must emit `enrichment_worker_id` and `source_event_ids` on every Command so its work can be attributed and replayed.

## 5. What "reasonable cold start" looks like

A trace ingested at time `t0` with no enrichment yet applied should still be retrievable by at least one strategy, even if the resulting pack is thin. The deterministic write-path minimum is:

- `content_tags.content_type` filled via `StructuralClassifier` (trace, document, entity by schema).
- `content_tags.scope` filled if the caller provided it, otherwise defaulted.
- `content_tags.domain` filled **only if** `KeywordDomainClassifier` has a matching rule; otherwise left empty.
- `content_tags.signal_quality` defaulted to `"standard"`; only `apply_noise_tags()` changes this.
- `node_role` explicitly set by the caller (default `semantic`).
- `source_system` tag from `SourceSystemClassifier`.
- Raw text indexed in `DocumentStore` for keyword search.
- Embeddings written to `VectorStore` if the caller supplied them (embedding generation is a deterministic store-side call, not an LLM decision).

That set is retrievable by keyword and vector strategies from the moment it is committed. Graph-neighbor and LLM-tag-based retrieval improve as enrichment catches up.

## 6. Open questions

- **Enrichment Commands as a distinct event namespace?** Should enrichment emit `ENRICHMENT_*` events distinct from `USER_*` events so retrieval can be filtered to "only human-grounded" in high-trust contexts?
- **Per-domain policy defaults for LLM-originated writes?** A global "LLM writes require review" flag is probably too blunt. Per-domain feels right.
- **Enrichment SLO shape.** Is it "p95 enrichment lag under 5 minutes" or "p95 items-retrieved-before-first-enrichment under 10%"? The latter is more outcome-aligned.
- **Interaction with `trellis_workers`.** Enrichment agents live in `trellis_workers/enrichment/`, but the existing workers (`DbtManifestWorker`, etc.) are ingestion-side. We should decide whether enrichment workers share the worker base class or get a dedicated one.
- **Curated-node GC.** If a curated node's source evidence is later marked noise, does the curated node stay? It should stay (immutable history) but be flagged; the exact mechanism needs design.

## 7. References

- `src/trellis/classify/pipeline.py` — Two-mode classifier pipeline (ingestion vs enrichment).
- `src/trellis/mutate/executor.py` — The 5-stage governed pipeline the enrichment agent must go through.
- `src/trellis/schemas/entity.py` — `node_role` and `generation_spec` fields.
- `src/trellis/retrieve/effectiveness.py` — `apply_noise_tags()` feedback loop (today partial; Tier 1 recommendation is to automate it).
- `src/trellis/retrieve/pack_builder.py` — Emits `PACK_ASSEMBLED` telemetry the enrichment agent consumes.
- `docs/research/memory-systems-landscape.md §2` — Graphiti's write-path LLM pipeline (the canonical example of the approach we are rejecting).
- `docs/research/memory-systems-landscape.md §10` Tier 3 — The "do not put an LLM in the write path" recommendation this ADR makes binding.
