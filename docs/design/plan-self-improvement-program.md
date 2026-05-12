# Plan: Self-improvement program

**Status:** proposed 2026-05-11
**Owner:** rotating (swarm-pickable per sub-plan)
**Self-contained:** yes — but read [`implementation-roadmap.md`](./implementation-roadmap.md) first for the live state of the project.

## 1. Premise

Trellis today **demotes** well (effectiveness → noise tags → importance → filtering; advisory fitness → suppression/restoration) but does not **promote**, does not **observe itself**, and does not **act on its own degradation**. The system has a feedback loop *capability* but no closed loop on:

* Empirical observations about entities (query patterns, profiling stats, access frequency) — no first-class home in the graph.
* Provenance of edges (source trace, agent, confidence) — buried in JSON properties, unqueryable.
* Parameter learning thresholds — hard-coded in `learning/scoring.py` despite a registry being plumbed.
* Extraction failures — silently swallowed in `LLMExtractor` and worker miners.
* Schema evolution — open-string types accumulate forever with no graduation path.
* Dogfooding — the system records traces of *user* work, never of *its own* analysis.
* Code authoring against itself — no surface that converts learned signal into a draft PR.

This program scopes seven additive features and two cleanup tracks that close those gaps. Each item is sized for a single swarm unit (one PR, one reviewer cycle).

## 2. Hard rules for every sub-plan (POC directive)

The project is in POC stage with no live users. The directive for every item below:

1. **No silent fallbacks.** Every `try: X except: pass` is a defect. Every `except SomeError: log.warning(...); return None` is a defect unless the code path is *explicitly* documented as graceful-degradation with a stated reason. Default behavior on unexpected input or state is **raise**.
2. **No backwards-compat shims.** No alias maps, no "if old shape, fall through to new shape." Greenfield writes; deprecate old shapes via deletion, not via compatibility layers. Existing on-disk data without the new fields fails loud at read time and surfaces an explicit migration.
3. **Loud on misuse.** Misconfigured registries, missing required env vars, ambiguous extractor outputs, ungated schema additions — all emit warnings (or raise) at the earliest possible point, never at use-site.
4. **No half-finished implementations.** A feature ships with its analyzer, its CLI surface, and its eval scenario, or it doesn't ship.
5. **Type extensibility preserved.** Open-string entity types and edge kinds stay open. New canonical types are *additive* to `well_known.py`.

These rules supersede defaults in `CLAUDE.md` *only on the POC dimension* — soft-fail behavior elsewhere (e.g., reading partial advisories) is unchanged.

## 3. Program inventory

Eight sub-plans, two cleanup tracks. Each is independently shippable, with named dependencies.

| # | Item | Plan | ADR | Depends on |
|---|---|---|---|---|
| 1 | Observation / Measurement entity vocabulary | [`plan-observation-entity-type.md`](./plan-observation-entity-type.md) | [`adr-observation-entity-type.md`](./adr-observation-entity-type.md) | — |
| 2 | Provenance columns (Phase 3 of graph-ontology) | [`plan-provenance-columns.md`](./plan-provenance-columns.md) | already-decided in [`adr-graph-ontology.md`](./adr-graph-ontology.md) §6.4 | Item 1 (consumer signal) |
| 3 | Parameter-registry wiring (wake dead code) | [`plan-parameter-registry-wiring.md`](./plan-parameter-registry-wiring.md) | none (sub-ADR scale) | — |
| 4 | Extraction-failure telemetry + analyzer | [`plan-extraction-failure-analyzer.md`](./plan-extraction-failure-analyzer.md) | [`adr-extraction-failure-telemetry.md`](./adr-extraction-failure-telemetry.md) | Cleanup 2 (silent-fallback audit) |
| 5 | Well-known promotion loop (schema evolution) | [`plan-well-known-promotion-loop.md`](./plan-well-known-promotion-loop.md) | [`adr-well-known-promotion-loop.md`](./adr-well-known-promotion-loop.md) | — |
| 6 | Dogfooding meta-traces | [`plan-dogfooding-meta-traces.md`](./plan-dogfooding-meta-traces.md) | [`adr-dogfooding-meta-traces.md`](./adr-dogfooding-meta-traces.md) | Item 1 (Observation type), Item 2 (provenance columns) |
| 7 | Coding-agent self-improvement loop | [`plan-coding-agent-loop.md`](./plan-coding-agent-loop.md) | [`adr-coding-agent-loop.md`](./adr-coding-agent-loop.md) | Item 4 (extraction-failure events), Item 5 (well-known candidates), Item 6 (dogfooding) |
| C1 | Cleanup: dead-code removal | [`plan-cleanup-dead-code.md`](./plan-cleanup-dead-code.md) | — | Item 3 (frees one block) |
| C2 | Cleanup: silent-fallback hardening | [`plan-cleanup-silent-fallbacks.md`](./plan-cleanup-silent-fallbacks.md) | — | — |

## 4. Recommended execution order

A fresh swarm can pick any item whose deps are satisfied. The natural ordering:

1. **C2** (silent-fallback hardening) — establishes the POC discipline everything else assumes. Standalone, no deps.
2. **Item 1** (Observation entity vocabulary) — unblocks Items 2 and 6, defines the empirical-data home.
3. **Item 3** (parameter-registry wiring) — wakes existing dead code, smallest unit (~150 LOC). Standalone.
4. **Item 4** (extraction-failure telemetry) — small, generates signal Item 7 will consume.
5. **Item 5** (well-known promotion loop) — small, generates signal Item 7 will consume.
6. **C1** (dead-code removal) — runs in parallel; cleans up the slack from Item 3 wiring and the JSONL bridge.
7. **Item 2** (provenance columns) — schedules now because Item 1 needs the consumer signal.
8. **Item 6** (dogfooding meta-traces) — depends on Items 1 + 2.
9. **Item 7** (coding-agent loop) — capstone; depends on Items 4 + 5 + 6.

Items 3, 4, 5, C1, C2 can run **in parallel** as five independent swarm units.

## 5. Considerations the program-level plan owns

These are concerns that span multiple sub-plans and are addressed once, here:

### 5.1 Eval scenario coverage per item

Every sub-plan must ship an eval scenario in `eval/scenarios/` that demonstrates convergence or correctness. Without this, "the loop closes" is a vibes claim. Required scenarios:

| Item | Scenario shape |
|---|---|
| 1 | Observation ingestion + retrieval — synthetic table with 1000 query-log entries → observations attached → PackBuilder pulls observations alongside structural neighbors |
| 2 | Provenance round-trip — write edge with `confidence=0.6`, query `WHERE confidence < 0.7`, verify column-shaped predicate works on all backends |
| 3 | Parameter registry pass-through — feed observations with registry overriding `noise_success_threshold`; verify recommendation matches override, not hard-coded default |
| 4 | Extraction-failure clustering — inject 50 malformed-JSON failures across 3 prompt-hash buckets; verify analyzer reports 3 clusters with correct counts |
| 5 | Schema-evolution candidate emergence — synthetic ingest of 1000 nodes with `node_type="metric"` (high signal); verify candidate report surfaces "metric" with N=1000 |
| 6 | Meta-trace round-trip — run `trellis analyze context-effectiveness`; verify Activity node + Observations land in graph; PackBuilder can retrieve "what analyses touched this entity" |
| 7 | Proposal generation — inject one failure cluster; verify proposal markdown is produced with stable identity (re-run → same proposal_id, no duplicate) |

### 5.2 Plane discipline

* **Operational plane:** new event types (`EXTRACTION_FAILED`, `WELL_KNOWN_CANDIDATE`, `META_ANALYSIS_RECORDED`, `PROPOSAL_DRAFTED`). They are events about Trellis, not about user content.
* **Knowledge plane:** new entity types (`Observation`, `Measurement`), new edges (`hasObservation`, `derivedFromQueryLog`). They are content the agent reads back.
* **Cross-plane reads:** Item 6's dogfooding meta-traces explicitly cross planes (read operational events, write knowledge entities). This is a sanctioned crossing — document it in the plan, do not generalize the pattern.

### 5.3 Cost and budget

Items 5, 6, and especially 7 will eventually invoke LLMs. Each plan must:

* Specify per-invocation token estimate and dollar estimate.
* Specify rate limit (max calls per hour / per day).
* Specify cumulative budget envelope (max spend per week).
* Surface a `TRELLIS_LLM_BUDGET_CENTS` env knob honored by the affected workers.

POC default: budgets default to **zero** (loops dry-run by default). Operator opts in by setting the env var.

### 5.4 Idempotency of self-modifying loops

Items 5 and 7 produce proposals that may re-fire on every run. Each must:

* Compute a stable `proposal_id` (hash of the underlying cluster signature).
* Persist the proposal_id in the EventLog (`PROPOSAL_DRAFTED` event).
* Skip emission if the same `proposal_id` was emitted within a cooldown window (default 7 days).
* On state change (cluster grows, signal flips), re-emit with `PROPOSAL_UPDATED` event referencing the prior `proposal_id`.

### 5.5 Privacy / data classification

Items 1 (observations on entities) and 6 (meta-traces) may surface data that the underlying entity is classified to restrict. Each plan must honor existing `DataClassification` enforcement at the retrieval layer — observations inherit the access policy of their attached entity. **No new classification logic in scope.** If existing enforcement is incomplete (it is — `SensitivityGate` is gated on a design partner), the plan documents that as a known limitation, does not bypass.

### 5.6 Versioning the canonical registry

Items 1 and 5 mutate `src/trellis/schemas/well_known.py`. The canonical-registry contract:

* Adding a new canonical name is a minor version bump of `well_known.py` (track at module level: `WELL_KNOWN_VERSION = "1.2.0"`).
* Removing or repurposing a canonical name is forbidden (per `adr-graph-ontology.md` §5.4) — a one-way commitment.
* The promotion ADR template (Item 5) is the required artifact for any addition. No code-only canonical additions.

### 5.7 MCP / SDK surface

Items 1 and 6 add new operations agents need to call directly:

* `record_observation(entity_id, kind, value, evidence_ref, window=...)` — SDK + MCP tool.
* `query_observations(entity_id, kind=None, since=None)` — SDK + MCP tool.
* `record_meta_analysis(...)` — internal-only; not exposed in MCP.

Each plan that adds graph data agents should read or write specifies the SDK + MCP surface explicitly. Plans that don't surface in SDK/MCP justify why.

### 5.8 Contract test extension

New entity types do not require contract test changes. New *edge semantics* (Item 1's `hasObservation`, Item 2's provenance columns) do — the affected plan adds tests to `tests/unit/stores/contracts/graph_store_contract.py` that every backend must pass.

### 5.9 Security model for code authorship (Item 7 only)

The coding-agent loop is the highest-blast-radius item. Required guardrails:

* Branch isolation: every proposal targets a fresh branch under `agent-proposals/<proposal_id>`. Never push to `main` or any active dev branch.
* No auto-merge, no auto-rebase. The proposal is a draft PR or local commit only.
* Sandboxed execution: Claude Code spawn is restricted to read access on `src/`, write access on `agent-proposals/` worktree only.
* Secret scrubbing: pre-spawn check rejects proposals whose context includes any env var matching `*_KEY|*_SECRET|*_TOKEN|*_PASSWORD`.
* Explicit allowlist of modifiable files: only `src/trellis/schemas/well_known.py`, `docs/design/adr-*.md`, and the calling extractor's source. Anything else needs a separate ADR.

These are stated here so Item 7's plan can reference rather than restate.

## 6. Other considerations flagged for the user (raised 2026-05-11)

The user explicitly asked what they might be missing. Beyond §5 above:

* **Backpressure on the dogfooding loop (Item 6).** If every `analyze` CLI call writes back to the graph, scheduled-task setups can balloon the graph with low-value meta-traces. The plan needs a sampling rate or a "only record when something changed" filter.
* **Observation freshness decay.** Stats derived from a query log six months ago are stale. The `Importance score freshness` ADR ([`adr-importance-score-freshness.md`](./adr-importance-score-freshness.md)) shape applies; Item 1's plan must cite it and decide whether observations decay on the same curve.
* **What happens when Observation conflicts with stored property?** E.g., `column.nullable=false` (structural) but `Observation(kind="null_rate", value=0.03)` (empirical) implies it's effectively nullable in practice. The retrieval layer needs a precedence rule. Item 1's plan owns this.
* **Trellis is recording its own ADR generation.** Item 6's dogfooding loop, taken to its conclusion, means *this very plan document* would generate Activity + Observation nodes when it runs. That's correct, not weird — but it raises a measurement question: do we filter out "Trellis-authored" content from agent context packs by default? Item 6's plan must answer.
* **The system never learns to *trust less*.** Today everything trends toward demotion (noise tags) or promotion (precedent). There's no mechanism for "we used to trust this, the world changed, trust it less." Advisory drift is the closest analogue. The program does not add a "regress trust" mechanism; flagging here for visibility — if it matters, it's a separate ADR.
* **Cross-loop interaction.** Item 4's extraction-failure analyzer and Item 5's well-known promotion loop will both want to look at the same `MUTATION_EXECUTED` event stream. They must not double-count or cross-contaminate. The plans share an event-iteration helper.
* **Why not just use OpenLineage events end-to-end?** OpenLineage is already a domain integration for lineage. Could it carry the observation payload too? The Item 1 ADR explicitly considers and rejects this — Observations are a graph concern (PackBuilder retrieves them), OpenLineage is a wire-format concern (extractors emit it). They map onto each other but they are not the same artifact.

## 7. Success criteria for the program as a whole

The program is "done" when:

* Items 1–7 have landed and each has at least one green eval scenario in `eval/scenarios/`.
* Cleanup tracks C1 + C2 have run; no `except: pass` patterns remain in `src/`; no dead-code blocks named in C1 remain.
* `trellis analyze schema-evolution` and `trellis analyze extraction-health` produce non-empty reports against synthetic seed data.
* A complete dry-run of Item 7's proposal generator on one injected failure cluster produces a valid proposal markdown — but does **not** spawn Claude Code without an explicit operator command.
* `TODO.md` has the seven items marked `[x]` with commit references.

Until then: the system has a self-improvement *architecture* but not a self-improvement *practice*. That's the gap this program closes.
