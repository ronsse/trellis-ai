# ADR: Inner curation loop — the curator skill

**Status:** Proposed
**Date:** 2026-05-18
**Deciders:** Trellis core
**Related:**
- [`./adr-graph-skill-harness.md`](./adr-graph-skill-harness.md) — substrate this ADR builds on (skill loader, allowlisted graph tools, telemetry envelope). Do not duplicate; cite.
- [`./adr-planes-and-substrates.md`](./adr-planes-and-substrates.md) §2.4 + Phase 4 — this ADR closes the deferred "what consumes `document_ids`" story.
- [`./adr-coding-agent-loop.md`](./adr-coding-agent-loop.md) — sibling self-improvement pattern; structural template and opt-in-default discipline mirrored here.
- [`./plan-self-improvement-program.md`](./plan-self-improvement-program.md) §5.9 — security model the harness adopts wholesale.
- [`../../src/trellis/stores/base/graph.py`](../../src/trellis/stores/base/graph.py) `validate_document_ids` (lines 118–155) — structural contract the curator dereferences.
- [`../../src/trellis_workers/enrichment/service.py`](../../src/trellis_workers/enrichment/service.py) — pre-existing summarization capability the curator deliberately does **not** wrap.
- [`../../src/trellis/ops/registry.py`](../../src/trellis/ops/registry.py) `ParameterRegistry` — where the under-population threshold lives.
- [`./adr-memory-layer-interop.md`](./adr-memory-layer-interop.md) — in the delegate variant, `document_ids` point at external Memory-Layer item IDs and `DocumentStore` becomes a read-through backend. The curator is unaffected structurally (it reads through the same `read_document` tool), but "read the documents" may then be a cross-system read, and refs dangling after Memory-Layer GC surface as the `documents_unreadable` failure mode in §2.6.

**Terminology note — three "curation" senses.** This ADR's *inner curation loop* (a graph skill populating sparse node descriptions) is distinct from the dual-loop feedback *curation* of pack content ([`adr-dual-loop-evolution.md`](./adr-dual-loop-evolution.md)) and from the Memory Layer's *chained curation* of stored items ([`adr-memory-layer-interop.md`](./adr-memory-layer-interop.md)). None of the three subsumes another.

---

## 1. Context

`adr-planes-and-substrates.md` §2.4 introduced `document_ids: list[str]` as an optional property on entity nodes — a structural link from a graph entity back to the `DocumentStore` rows that sourced it. Phase 4 of that ADR landed three things: the schema field, the store-boundary validator ([`validate_document_ids`](../../src/trellis/stores/base/graph.py)), and the ingestion-path population. It explicitly **deferred the consumption story**: §2.4 names the field, Phase 4 step 3 says "retrieval layer learns to follow `document_ids` when building packs," and that is the whole brief. Nothing fires on the back of "this node has documents but no description."

The current state is asymmetric. `EnrichmentService` ([`src/trellis_workers/enrichment/service.py`](../../src/trellis_workers/enrichment/service.py)) can summarize a single piece of content end-to-end — tags, classification, summary, importance — but its callers are all on the **classification path** (`LLMFacetClassifier`, batch enrichment of document records). No code path looks at a graph node, sees `document_ids=["doc-7", "doc-12"]` and `properties.description=""`, and runs a follow-up agent to populate the description from those documents.

The gap is small in code surface and large in product effect: graph traversal answers structural questions ("what depends on what") but cannot answer descriptive ones ("what is this thing") for any node whose description was sparse at ingest. Phase 4's Cypher-side join makes the source documents reachable; the missing piece is the agent that reads them and writes back.

This ADR designs that piece — the **curator skill** — and bounds its scope tightly to avoid drift into general extraction.

## 2. Decision

Introduce a `curator` graph skill at `src/trellis_workers/agent/skills/curator/`, running on the harness defined in [`adr-graph-skill-harness.md`](./adr-graph-skill-harness.md). The skill detects under-populated nodes, reads their referenced documents via the harness's `read_document` tool, and writes a `summary` + `description` back through `MutationExecutor`. Three trigger paths are layered behind opt-in defaults.

### 2.1 Curator as a skill, not a service

The curator is a markdown skill (instructions + few-shot examples) plus a thin Python glue layer that the harness loads. It is **not** a long-running service, **not** a new package, and **not** a subclass of `EnrichmentService`. Layout:

```
src/trellis_workers/agent/skills/curator/
  skill.md                # instructions the harness injects
  glue.py                 # under_population_filter, propose_mutation, hash
  tests/
```

The harness owns the loop: `dispatch_skill("curator", node_id) → SKILL_DISPATCHED → tool calls → SKILL_COMPLETED`. The curator's domain-specific events (§2.5) layer on top — they describe *what* the loop did, not *how* it was invoked.

### 2.2 Under-population heuristic

A node is **under-populated** when:

```python
def is_under_populated(
    node: dict,
    threshold: int = 80,
    excluded_roles: tuple[str, ...] = ("curated",),
) -> bool:
    if node.get("node_role") in excluded_roles:
        return False
    docs = node.get("document_ids") or []
    desc = (node.get("properties") or {}).get("description") or ""
    return len(docs) > 0 and len(desc) < threshold
```

Threshold default: **80 characters**. Lives in [`ParameterRegistry`](../../src/trellis/ops/registry.py) under scope `component_id="curator"`, key `under_population_threshold_chars`. Tuneable by Item 3's parameter-promotion flow without redeploys.

`excluded_roles` defaults to `("curated",)`. A node whose `node_role="curated"` was set deliberately by a human reviewer or a prior curator pass should not be re-curated on top — overwriting intentional output is a safety regression, not a feature. The exclusion list lives in the same `ParameterRegistry` scope under key `under_population_excluded_roles`. Adding `"semantic"` or other structural-only roles is a follow-up once feedback signal tells us whether the v1 list under- or over-fires.

The heuristic is otherwise deliberately crude. v1 does not look at `summary`, `properties` density, or edge count. Adding those is a follow-up once we have feedback signal (§2.5) telling us whether the simple rule under- or over-fires.

### 2.3 Three trigger paths

| Path | Phase | Default | Invocation |
|---|---|---|---|
| **Explicit** | F2 | always on | `trellis admin run-skill curator --filter "<DSL>"` — operator runs against a graph query (e.g. `--filter "node_type=Service AND has_documents=true"`) |
| **Detected** | F2 | **disabled** | `CurationScheduler` worker periodically scans for under-populated nodes, emits `curation.requested`. Opt-in via `TRELLIS_CURATION_SCHEDULER_ENABLED=1`. |
| **Lazy** | F3 | **disabled** | `PackBuilder` emits `curation.requested` for under-populated nodes in assembled packs. Async fan-out: the *next* pack benefits; the current pack returns unblocked. A sync mode behind a budget gate is available for eval scenarios. |

The opt-in default for Detected and Lazy mirrors `adr-coding-agent-loop.md` §2.6 (`TRELLIS_LLM_BUDGET_CENTS_WEEK=0` default disables the Claude Code spawn). Same shape, same reason: a worker that polls the graph and runs an LLM per under-populated node will spend money the operator did not authorize.

### 2.4 Mutation scope — summary + description only

The curator writes exactly two fields, via one mutation type:

| Field | Where | Notes |
|---|---|---|
| `properties.description` | `ENTITY_UPDATE` Command | Long form, multi-sentence narrative. Bounded at the prompt level (~500 chars) to keep storage predictable. |
| `properties.summary` | same `ENTITY_UPDATE` Command | Short form, 1 sentence. Used by retrieval shaping. |

The Command goes through `MutationExecutor` (`src/trellis/mutate/`) — same pipeline every other governed write uses. The curator's tool layer does **not** call `graph_store.upsert_node()` directly. Per the project hard rule ("All mutations go through the governed pipeline"), and per the harness contract that skill tools either are pure-read or proxy through the executor.

The curator does **not**:

- Invent new edges (`EDGE_UPSERT`). Edge proposals are a future relationship-discoverer skill.
- Create new entities (`ENTITY_CREATE`). The curator only curates what extraction already produced.
- Touch any property other than `description` and `summary`. No `importance`, no `auto_tags`, no `content_tags` — those have their own owners.
- Modify `node_role`, `generation_spec`, `document_ids`, or any structural property.

Hard-bounding the write surface is what makes the curator safe to enable by lazy trigger (§3.5). A skill that could silently mutate `node_role` or invent edges would need an entire policy gate; one that touches two well-known property keys can be enforced with a Pydantic schema on the Command payload.

### 2.5 Feedback contract

The curator emits one event on success, one on failure, and consumes one downstream signal:

| Event | When | Payload (highlights) |
|---|---|---|
| `node.curated` | After successful `ENTITY_UPDATE` | `node_id`, `pre_description_len`, `post_description_len`, `summary`, `document_ids_read`, `tokens_used`, `cost_cents`, `proposal_hash`, `skill_run_id` |
| `curation.deferred` | On any non-success exit | `node_id`, `reason_code` (one of `documents_unreadable`, `policy_rejected`, `idempotent_short_circuit`, `budget_exceeded`, `llm_error`), `error_excerpt`, `skill_run_id` |
| `curation.feedback_recorded` | Authored externally by F4 evaluator | `node_id`, `curation_event_id`, `appeared_in_pack` (bool), `pack_feedback_delta` (numeric), `coverage_delta`, `human_quality` (optional 0–1) |

Three signals drive the F5 score-based evolver:

1. **Pack reuse with positive feedback** — did a curated node appear in a later assembled pack, and was that pack rated positively? Joined via `node_id` against `pack.assembled` + `feedback.recorded` from the existing effectiveness loop.
2. **Coverage delta** — percentage of nodes meeting `is_under_populated()` that became populated in a rolling window. Computed from the EventLog.
3. **Sampled human quality** — optional CLI `trellis admin curation sample --n 20 --rate` for periodic human review. Default off; intended for evolver training cycles.

`curation.feedback_recorded` is the join surface for items 1–3. The evolver (F5) reads it; the curator does not.

### 2.6 Failure modes and idempotency

- **Documents unreadable** (S3 ACL denial, missing rows, blob backend down): emit `curation.deferred` with `reason_code="documents_unreadable"`, do not retry. The scheduler's next sweep will pick it up; transient backend issues self-heal.
- **Proposal rejected by policy or schema**: emit `curation.deferred` with `reason_code="policy_rejected"` and the executor's error excerpt.
- **Idempotent short-circuit**: the curator computes `proposal_hash = sha256(node_id || sorted(document_ids) || prompt_template_version || model_id)`. The Command carries it as `idempotency_key`. Re-running the curator on the same node with the same docs and the same prompt produces the same hash; `MutationExecutor`'s idempotency check returns the prior result and emits `curation.deferred` with `reason_code="idempotent_short_circuit"` — no LLM call, no new event.
- **Budget exceeded** (per-node or worker-level): emit `curation.deferred` with `reason_code="budget_exceeded"`; do not partially update.
- **LLM error** (model 5xx, timeout, parse failure): emit `curation.deferred` with `reason_code="llm_error"`; the harness's existing `EXTRACTION_FAILED` plumbing also fires from inside the LLM client. No double-handling.

A `curation.deferred` event is **not** a defect. It is the normal way the loop says "skip for now."

### 2.7 Budgets

Two layers, mirroring §2.6's pattern from `adr-coding-agent-loop.md`:

- **Per-curation:** `budget_cents=50` default per node, enforced at LLM call site. Above-budget runs are split into per-document sub-steps that each respect the cap; if even one document exceeds it alone, the node is deferred with `reason_code="budget_exceeded"`.
- **Worker-level:** `TRELLIS_CURATION_BUDGET_CENTS_DAY` (default **0** — scheduler disabled). Cumulative spend in the trailing 24-hour window must remain `< budget`. At the limit, the scheduler stops dispatching and emits `curation.deferred` for each node it would have picked. Same opt-in default as §2.3.

Both budgets live in `ParameterRegistry` so the F5 evolver can tune them without a redeploy.

## 3. Why this shape

### 3.1 Why a skill, not a service

A service would be a longer-lived process with its own state, its own retry policy, its own observability surface. We already have the harness — it owns the loop, the allowlist, the telemetry envelope. Curator-as-skill means we add one markdown file, one glue module, and one set of mutation-schema additions. If a future skill (relationship-discoverer, claim-verifier) wants the same shape, the marginal cost is another `skills/<name>/` directory. The skill model is the right abstraction for "agent that runs against the graph"; inventing a parallel service abstraction would split the surface area in two for no gain.

### 3.2 Why hard-bound write scope

A general-purpose curation agent that could write any property would drift. It would start with `description`, learn the model writes useful `importance` scores, gain a tag-rewrite mode, become a competing classifier, and eventually we'd be debating whether the curator or the tagging pipeline owns `content_tags`. v1 says: two property keys, one Command type, full stop. The policy gate enforcing this is one Pydantic validator on the `ENTITY_UPDATE` payload. Lifting the scope is a single ADR amendment later; locking it now prevents the drift and gives us a clean baseline for the F4 evaluator to score against.

### 3.3 Why opt-in scheduler default

Two precedents already in the codebase: classifier tier `allow_llm_fallback=False` default ([`src/trellis/extract/`](../../src/trellis/extract/) dispatcher), and Item 7's `TRELLIS_LLM_BUDGET_CENTS_WEEK=0` default. Both encode the same principle: cost-incurring background loops do not run unless an operator explicitly says yes. The Detected and Lazy triggers can both spend real money against an LLM provider. The Explicit trigger has a human in the loop on every invocation, so it is on by default. This is the same shape the rest of the project uses; deviating would surprise operators.

### 3.4 Why direct LLM access via the harness's `read_document` tool, not a wrapped `EnrichmentService.summarize()`

This is the pre-committed call worth defending. Two viable shapes:

**Option A (recommended, v1):** the curator skill is given the `read_document(doc_id) → str` tool from the harness's allowlist, plus the model directly. The skill's instructions tell it to read all referenced documents and produce a JSON `{summary, description}` payload in one call. The glue layer parses, validates, packages the `ENTITY_UPDATE`.

**Option B:** the curator iterates `document_ids`, calls `EnrichmentService.summarize()` per doc, then runs a second LLM call to merge per-doc summaries into a node-level description.

We pick A for three reasons:

1. **`EnrichmentService` shape mismatch.** Its output is `EnrichmentResult` — a classification-shaped record with `auto_tags`, `auto_class`, `auto_importance`, `auto_summary`. The curator wants `{summary, description}` for a *node*, not a *document*. Wrapping `EnrichmentService` means either ignoring 80% of its return value or pretending node-level curation is document-level enrichment.
2. **Coupling cost.** `EnrichmentService` is currently load-bearing for the classification path. Adding a node-curation caller doubles the set of consumers reading from its prompt + result schema; either we constrain `EnrichmentService` to keep the curator happy (slowing classification work) or we fork it (two summarization paths to maintain).
3. **Harness ergonomics.** The harness already exposes `read_document` as an allowlisted tool with telemetry. The model deciding *which* documents to read deeply and which to skim is exactly the kind of decision an LLM is good at and an iterator is not. Option B's per-doc loop loses that.

Option B's only advantage is that `EnrichmentService` is already there. That is a sunk-cost argument; the maintenance surface of Option A is one prompt template living next to the skill that uses it.

### 3.5 Why async lazy curation in F3

When `PackBuilder` notices an under-populated node mid-assembly, two designs are possible:

- **Sync:** block pack assembly while the curator runs. The returned pack reflects the curated description.
- **Async:** emit `curation.requested`, return the current pack unblocked, let the worker pick it up. The *next* pack against the same node benefits.

We default async. A sync curator runs for tens of seconds (LLM round-trip + document fetch); pack assembly is on the agent's hot path. Blocking would change the latency profile from "Trellis returns in under a second" to "Trellis returns whenever its LLM provider feels like it." Async preserves the latency contract, gives the curator the freedom to defer/retry/batch, and accepts that the first agent to encounter an under-populated node pays no benefit. Convergence to a fully-populated graph is the F4 metric; first-touch latency is not.

A sync mode behind an explicit budget gate (`PackBuilder.assemble(..., curate_inline=BudgetSpec(...))`) is available for eval scenarios that need deterministic behavior.

## 4. Guardrails

The curator must **never**:

- Invent new edges. `EDGE_UPSERT` is not on its Command allowlist.
- Create new entities. `ENTITY_CREATE` is not on its Command allowlist.
- Modify any property other than `description` and `summary`. The mutation-schema validator rejects extra keys.
- Bypass `MutationExecutor`. No direct `graph_store.upsert_node()`. The harness's tool allowlist does not expose store-level write methods (it only exposes governed-mutation submitters).
- Touch `document_ids`. The link is structural; the curator reads it, never writes it.
- Modify other nodes during a single run. A curator dispatch is scoped to exactly one `node_id`; cross-node writes are a different skill.
- Retry on its own. Retry is the scheduler's job. The skill exits clean and emits a `curation.deferred` event.

## 5. Consequences

### 5.1 What this enables

- **Closes the `adr-planes-and-substrates.md` Phase 4 deferred story.** `document_ids` stops being a structural link with no consumer; the curator is the consumer.
- **Gives F4/F5 the signal source for the evolver.** `node.curated` + `curation.feedback_recorded` is the joint surface against which the score-based evolver tunes the threshold, the budget, and the prompt-template version. Without this loop, F5 has nothing to score.
- **Reusable skill pattern.** Curator is the first concrete graph skill on the harness. Its shape — narrow write scope, opt-in scheduler, idempotent proposal hash — is the template future skills (relationship-discoverer, claim-verifier) follow.
- **Convergence-eval ready.** The parallel `eval/scenarios/skill_loop_convergence/` scenario can use the curator as its primary skill subject without further design work.

### 5.2 What it doesn't do

- **No extraction.** The curator does not parse documents to find new entities. That is the extraction pipeline's job.
- **No edge discovery.** The curator does not propose relationships. That is the future relationship-discoverer skill.
- **No tagging.** `auto_tags`, `content_tags`, classification — owned by the tagging pipeline. The curator stays clear.
- **No backfill at scale.** Phase 4 said "backfill is best-effort; leave `document_ids` empty for pre-existing entities." This ADR does not change that. The curator runs against current ingestion, not historical data.

### 5.3 What it costs

- **LLM spend.** One model call per curated node — estimated $0.01–$0.05 depending on document count and model. At the v1 default `budget_cents=50` and a reasonable node mix, a thousand-node graph costs a few dollars to fully curate once.
- **Storage.** `description` is bounded by prompt design (~500 chars); `summary` is bounded similarly (~120 chars). For a million-node graph this is under a gigabyte of property storage — negligible against existing trace and event storage.
- **Operator attention.** A new event-type family in dashboards (`curation.*`), a new opt-in env var (`TRELLIS_CURATION_SCHEDULER_ENABLED`), and a new CLI subcommand (`trellis admin run-skill curator`).

## 6. Alternatives considered

- **Call `EnrichmentService.summarize()` directly without a skill.** Rejected. No tool allowlist, no harness telemetry, no per-call budget, no idempotency hash. We would either be reinventing the harness inside the enrichment package or accepting that a load-bearing LLM caller has no observability. Both are worse than reusing the harness.
- **Auto-run the curator on every `ENTITY_CREATE`.** Rejected. At POC stage, an ingestion pipeline that fires an LLM on every new entity is a runaway-cost risk. Detected (scheduler) and Lazy (pack-time) gate curation on actual demand — only nodes that *are* under-populated *and* matter (to a scheduler scan or a real assembled pack) trigger work. Auto-on-create is the kind of decision that looks fine in isolation and turns into a postmortem when a backfill job creates a million entities.
- **Store the summary as a separate node, not a property.** Rejected. Doubles graph storage for no read-side benefit; every read of "what is this node" would need a one-hop traversal. The property-on-the-node shape matches what every retrieval surface already does.
- **Run the curator as a separate worker package, not a skill on the harness.** Rejected. Splits the agent-loop abstraction in two: the harness for "agents that read the graph," another package for "agents that curate the graph." The harness is general enough; forking is premature.
- **Skip `MutationExecutor`, write directly to the graph store.** Rejected outright. Project hard rule. Restated here so future skill authors see it in this ADR.

## 7. References

- [`./adr-graph-skill-harness.md`](./adr-graph-skill-harness.md) — substrate
- [`./adr-planes-and-substrates.md`](./adr-planes-and-substrates.md) §2.4, Phase 4 — the deferred story this resolves
- [`./adr-coding-agent-loop.md`](./adr-coding-agent-loop.md) — sibling pattern
- [`./plan-self-improvement-program.md`](./plan-self-improvement-program.md) §5.9 — security baseline
- [`../../src/trellis/stores/base/graph.py`](../../src/trellis/stores/base/graph.py) — `validate_document_ids`
- [`../../src/trellis_workers/enrichment/service.py`](../../src/trellis_workers/enrichment/service.py) — the summarization capability deliberately not wrapped
- [`../../src/trellis/ops/registry.py`](../../src/trellis/ops/registry.py) — `ParameterRegistry`; under-population threshold lives here
- [`../../src/trellis/mutate/commands.py`](../../src/trellis/mutate/commands.py) — `ENTITY_UPDATE` Command type
