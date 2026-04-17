# ADR: Dual-Loop Evolution — System Self-Improvement and Agent Advisory

**Status:** Proposed
**Date:** 2026-04-13
**Deciders:** Trellis core
**Related:**
- [`./adr-deferred-cognition.md`](./adr-deferred-cognition.md) — Deterministic writes, deferred intelligence
- [`../research/memory-systems-landscape.md`](../research/memory-systems-landscape.md) — Landscape comparison
- [Imbue: Darwinian Evolver](https://imbue.com/research/2026-02-27-darwinian-evolver/) — Population-based evolutionary optimization via LLM-guided mutation and fitness selection
- [`./context-economy-strategy.md`](./context-economy-strategy.md) — Retrieval budgeting

---

## 1. Context

### The landscape gap

Every memory graph in the landscape comparison delivers context to agents in one direction: the agent asks, the graph answers. The agent's only lever is to phrase a better query. If the agent repeatedly fails at a task category, the graph has no mechanism to tell it "agents that succeeded here did X instead of Y."

Trellis already has a partial feedback loop: `PACK_ASSEMBLED` events record what context was served, `FEEDBACK_RECORDED` events capture outcomes, and `apply_noise_tags()` demotes items that correlate with failure. But this loop is **unidirectional** — it improves the graph's curation, not the agent's behavior. The graph gets smarter silently; the agent never knows *why* its context changed or *what it should do differently*.

### The Darwinian insight

Imbue's Darwinian Evolver demonstrates that population-based evolutionary optimization — maintain a population of solution variants, select parents by fitness, mutate via LLM reasoning, evaluate against a scoring function, let the best survive — is a universal optimizer for any problem where solutions can be understood by an LLM and quality can be scored.

The key architectural elements that map to Trellis:

| Darwinian Evolver Concept | Trellis Analog |
|---|---|
| **Organism** | An agent's approach to a task category (captured in traces) |
| **Population** | The set of traces/precedents for a given domain+intent |
| **Fitness function** | `FEEDBACK_RECORDED` success/failure correlation |
| **Failure cases** | Traces with negative feedback + their pack contents |
| **Learning log** | Event log entries linking pack contents to outcomes |
| **Mutation** | Advisory hints that suggest behavioral changes |
| **Selection pressure** | Noise demotion (remove what fails) + advisory (amplify what works) |

The critical insight: **Trellis doesn't mutate agents directly** (we don't rewrite their code or prompts). Instead, we apply selection pressure through two complementary mechanisms — improving what the graph serves (Flow 1) and advising agents on what to do differently (Flow 2). Together, these create the Darwinian stair-step: each generation of agent runs benefits from the accumulated fitness signal of all prior runs.

### What's missing

Today only Flow 1 exists. Flow 2 — the advisory channel — has no schema, no generation mechanism, no delivery path:

```
                    TODAY                              TARGET

Agent ──request──> Graph                Agent ──request──> Graph
Graph ──context──> Agent                Graph ──context──> Agent
Agent ──feedback─> Graph                Graph ──advisory─> Agent   ← NEW
Graph ──(silent)─> better curation      Agent ──feedback─> Graph
                                        Graph ──(silent)─> better curation
```

## 2. Decision

**Trellis implements two complementary improvement loops. Flow 1 (System Self-Improvement) curates the graph. Flow 2 (Agent Advisory) advises agents on how to improve their behavior. Both are deterministic at read time and outcome-driven.**

### 2.1 Flow 1: System Self-Improvement (exists, to be extended)

The graph improves its own retrieval quality based on outcome data. This flow already works:

```
PACK_ASSEMBLED → FEEDBACK_RECORDED → analyze_effectiveness()
    → apply_noise_tags()       (demote what fails)
    → promote precedents       (amplify what works)
    → re-tag via enrichment    (reclassify based on outcomes)
```

**Extensions in this ADR:**

- **Rejected-candidate tracking.** Record *why* items were excluded from packs (budget overflow, dedup, structural filter, noise filter) in the `PACK_ASSEMBLED` event. This is essential data for the fitness function.
- **Strategy-level provenance.** Tag each `PackItem` with the strategy that found it (`strategy_source` field). This enables per-strategy fitness scoring — "graph search finds 3x more successful items than keyword search for this domain."
- **Budget consumption trace.** Record the running token count at each selection step so we can see whether budget cuts are removing high-value items.

### 2.2 Flow 2: Agent Advisory (new)

The graph generates deterministic, outcome-backed suggestions and delivers them alongside context packs. Advisories answer: "Based on what worked and didn't work for past agents facing similar tasks, here's what you should consider."

#### 2.2.1 Advisory schema

```python
class Advisory(VersionedModel):
    """A single actionable suggestion for an agent."""
    advisory_id: str
    category: AdvisoryCategory  # approach, scope, entity, anti_pattern, query
    confidence: float           # 0.0-1.0, derived from sample size + effect size
    message: str                # Human/agent-readable suggestion
    evidence: AdvisoryEvidence  # What data backs this up
    scope: str                  # domain, intent pattern, or entity type this applies to

class AdvisoryCategory(str, Enum):
    APPROACH = "approach"          # "Successful agents queried schema metadata first"
    SCOPE = "scope"                # "Narrowing to these 3 entities improved success rate by 40%"
    ENTITY = "entity"              # "Entity X appears in 80% of successful traces for this domain"
    ANTI_PATTERN = "anti_pattern"  # "Agents that skipped validation failed 73% of the time"
    QUERY = "query"                # "Try including 'deployment' in your context query"

class AdvisoryEvidence(VersionedModel):
    """Statistical backing for an advisory."""
    sample_size: int              # Number of traces analyzed
    success_rate_with: float      # Success rate when pattern is present
    success_rate_without: float   # Success rate when pattern is absent
    effect_size: float            # Difference (with - without)
    representative_trace_ids: list[str]  # Example successful traces
```

#### 2.2.2 Advisory generation (deterministic)

Advisories are **not generated by an LLM at read time**. They are computed deterministically from outcome data by an `AdvisoryGenerator` that runs as a post-analysis step (same cadence as `apply_noise_tags()`):

1. **Entity correlation.** For each domain, compute which entities appear disproportionately in successful vs failed traces. Entities with high positive effect size become `ENTITY` advisories.

2. **Step-pattern mining.** Analyze `TraceStep` sequences in successful vs failed traces. Steps that consistently appear in successes but are absent in failures become `APPROACH` advisories.

3. **Scope analysis.** Compare the `target_entity_ids` breadth in successful vs failed packs. If narrower scope correlates with success, emit a `SCOPE` advisory.

4. **Anti-pattern detection.** Identify step patterns or entity combinations that appear disproportionately in failed traces. These become `ANTI_PATTERN` advisories.

5. **Query improvement.** When `PACK_ASSEMBLED` events show that certain query terms consistently lead to high-scoring packs, emit `QUERY` advisories.

Each advisory carries its statistical evidence (`sample_size`, `effect_size`) so the consuming agent can weight it appropriately. **Low-confidence advisories (small sample, weak effect) are suppressed.** The system errs toward silence over noise.

#### 2.2.3 Advisory delivery

Advisories are embedded in pack responses — not as separate calls. When an agent calls `get_context`, `get_objective_context`, or `get_task_context`, the response includes an `advisories` section alongside the context items:

**MCP output:**
```markdown
## Context
[... normal pack items ...]

## Advisories (3 suggestions based on 47 past traces)
1. **[approach]** Agents that validated schema metadata before generating SQL
   succeeded 82% vs 34% without. (n=47, effect=+48pp)
2. **[entity]** Entity `uc://catalog.prod.customers` appears in 91% of
   successful traces for this domain. Consider including it.
3. **[anti_pattern]** Skipping the dry-run step correlated with 3x failure
   rate. (n=23, effect=-44pp)
```

**SDK/API output:**
```python
pack = await client.assemble_pack("generate SQL for customer report")
pack["advisories"]  # List of Advisory dicts with full evidence
```

**Key constraint: advisories are pre-computed, not generated at request time.** The `AdvisoryGenerator` runs periodically (or triggered by feedback events), writes advisories to a lightweight store, and `PackBuilder` retrieves matching advisories at assembly time based on domain/intent overlap. This keeps the read path deterministic and fast.

### 2.3 The evolutionary stair-step

The two flows create a ratchet effect. Each generation of agent runs produces outcome data that both improves the graph (Flow 1) and sharpens the advice given to the next generation (Flow 2):

```
Generation 0: Agent runs with no history
    → Records traces, feedback
    → System learns: items A, B are noise; item C correlates with success
    → Advisory generated: "include entity X" (confidence: 0.6)

Generation 1: Agent runs with improved packs + advisory
    → Better starting context (noise removed)
    → Agent follows advisory → higher success rate
    → System learns: advisory was effective, confidence rises to 0.8
    → New advisory: "also validate schema before writing" (confidence: 0.5)

Generation 2: Agent runs with further-improved packs + stronger advisories
    → Compounding improvement
    → Advisories that didn't help get demoted (same fitness mechanism)
    → New patterns emerge from the richer outcome data
```

This is the Darwinian selection mechanism applied without modifying agent code:
- **Fitness function:** `FEEDBACK_RECORDED` success rate
- **Selection pressure:** Noise demotion removes losing strategies; advisories amplify winning ones
- **Mutation:** Each new advisory is a behavioral suggestion the agent may or may not adopt
- **Population:** The set of advisory+context combinations that agents have tried
- **Survival:** Advisories that improve outcomes persist and gain confidence; those that don't are quietly dropped

### 2.4 Advisory fitness tracking

Advisories themselves are subject to the same evolutionary pressure:

1. When a pack is assembled with advisories, the `PACK_ASSEMBLED` event records which `advisory_ids` were included.
2. When feedback arrives, the advisory-outcome correlation is computed alongside item-outcome correlation.
3. Advisories whose inclusion correlates with *worse* outcomes are demoted (confidence reduced, eventually suppressed).
4. Advisories whose inclusion correlates with *better* outcomes gain confidence and are surfaced more prominently.

This prevents the advisory system from calcifying — bad advice is pruned by the same mechanism that prunes bad context items.

## 3. Consequences

### Positive

- **Bidirectional improvement.** The graph improves itself (Flow 1) *and* improves agents (Flow 2). No other system in the landscape does both.
- **Deterministic advisory generation.** Advisories are computed from statistical analysis of outcome data, not from LLM inference at read time. Consistent with the deferred-cognition ADR.
- **Self-correcting.** Advisories that hurt outcomes are automatically demoted. The system cannot permanently give bad advice.
- **Observable.** Every advisory carries its evidence chain. Agents (and operators) can inspect *why* a suggestion was made and *how strong* the evidence is.
- **Composable with enrichment.** The enrichment agent (from deferred-cognition ADR) can consume advisory effectiveness data to inform its own tagging and edge-creation decisions.
- **Graceful cold start.** With no outcome history, no advisories are generated. The system doesn't guess — it waits until it has statistical evidence. Agents receive pure context packs until the advisory generator has enough data.
- **Domain-portable.** The advisory generator is domain-agnostic. It finds patterns in whatever traces and feedback exist. A new domain starts producing advisories as soon as it accumulates enough outcome data.

### Negative / trade-offs

- **Requires feedback.** Both flows depend on `FEEDBACK_RECORDED` events. Agents (or their orchestrators) that don't record feedback get no improvement. This is already true for Flow 1 but becomes more visible with Flow 2.
- **Statistical lag.** Advisories require a meaningful sample size before they're surfaced. In low-traffic domains, this may take weeks. The `min_sample_size` threshold must be tuned per deployment.
- **Advisory adoption is voluntary.** The graph suggests; the agent decides. If agents ignore advisories, Flow 2 has no effect. The advisory format should be designed to be easy for agents to consume and act on.
- **Advisory storage is a new persistence concern.** Lightweight (JSON file or dedicated table), but it's another store to manage.
- **Correlation is not causation.** The advisory generator finds statistical patterns, not causal mechanisms. An advisory like "agents that checked schema first succeeded more" could reflect confounders (e.g., more careful agents both check schema and write better code). The evidence fields make the statistical basis transparent, but operators should understand this limitation.

### Neutral

- **No LLM in the advisory generation path.** Advisory text is template-generated from statistical findings. An enrichment agent *could* later rephrase advisories using an LLM for clarity, but this is optional and deferred.
- **No change to the write path.** Traces, entities, and documents are ingested exactly as before. Advisory data is read-only from the agent's perspective.

## 4. Implementation approach

### Phase 1: Decision trail (pre-requisite)

Extend `PACK_ASSEMBLED` telemetry with the data Flow 2 needs to compute advisories:

- `PackItem.strategy_source`: which search strategy found this item
- `PACK_ASSEMBLED` payload gains `rejected_items` (list of `{item_id, reason}`)
- `PACK_ASSEMBLED` payload gains `budget_trace` (running token count at each selection step)

### Phase 2: Advisory schema and generator

- Add `Advisory`, `AdvisoryCategory`, `AdvisoryEvidence` schemas
- Implement `AdvisoryGenerator` with the five deterministic analysis methods
- Store advisories in a lightweight `AdvisoryStore` (JSON file or SQLite table)
- Add `trellis analyze generate-advisories` CLI command
- Add `POST /api/v1/advisories/generate` API endpoint

### Phase 3: Advisory delivery

- Extend `PackBuilder.build()` to attach matching advisories to pack output
- Extend `Pack` schema with `advisories: list[Advisory]`
- Update MCP tools to include advisories section in markdown output
- Update SDK methods to return advisories in pack responses
- Record `advisory_ids` in `PACK_ASSEMBLED` events for fitness tracking

### Phase 4: Advisory fitness loop

- Implement advisory-outcome correlation in `analyze_effectiveness()`
- Add confidence adjustment: advisories that correlate with success gain confidence; those that correlate with failure lose it
- Add advisory suppression: advisories below a confidence threshold are no longer surfaced
- Add `trellis analyze advisory-effectiveness` CLI command

## 5. Minimum viable advisory

The smallest useful advisory is an entity correlation: "Entity X appears in 82% of successful traces for domain Y." This requires only:

1. `PACK_ASSEMBLED` events with `item_ids`
2. `FEEDBACK_RECORDED` events with `success: true/false`
3. Correlation analysis (already exists in `analyze_effectiveness`)
4. A template: `"Entity {name} appears in {pct}% of successful traces (n={n})"`

This can be implemented incrementally and delivers value before the full advisory schema is in place.

## 6. What this is NOT

- **Not prompt rewriting.** We don't modify agent prompts or code. We provide advisory metadata that agents can choose to act on.
- **Not reinforcement learning.** There is no gradient, no policy network, no reward model. The mechanism is statistical correlation + selection pressure.
- **Not LLM-generated advice.** Advisory text is template-based from statistical findings. LLM rephrasing is an optional enrichment step, not a core mechanism.
- **Not prescriptive.** Advisories are suggestions with evidence, not commands. An agent with domain-specific knowledge may correctly ignore an advisory.

## 7. Open questions

- **Advisory granularity.** Should advisories target (domain), (domain + intent pattern), or (domain + specific entity)? Finer granularity means more relevant advice but requires more data to reach statistical significance.
- **Advisory TTL.** Should advisories expire? The underlying patterns may shift as the codebase or team changes. A sliding-window analysis (last N days) naturally handles this, but explicit TTL may also be useful.
- **Cross-domain transfer.** Can advisories from one domain inform another? E.g., "validate before writing" might be universal. The current design is domain-scoped; cross-domain advisory is a future extension.
- **Multi-agent advisory.** Should advisories distinguish between agent types? A data-pipeline agent and a code-review agent might need different advice even in the same domain. The `agent_id` field on traces enables this, but the generator doesn't use it yet.
- **Advisory delivery format for non-MCP agents.** MCP tools return markdown. SDK returns structured data. What about agents that consume context via plain-text files (e.g., Claude Code CLAUDE.md)? Advisory injection into skill files is a potential path.

## 8. References

- [Imbue: LLM-based Evolution as a Universal Optimizer](https://imbue.com/research/2026-02-27-darwinian-evolver/) — Population-based evolutionary optimization framework
- [Imbue: Darwinian Evolver (GitHub)](https://github.com/imbue-ai/darwinian_evolver) — Open-source implementation
- `src/trellis/retrieve/effectiveness.py` — Existing effectiveness analysis and noise feedback loop
- `src/trellis/retrieve/pack_builder.py` — Pack assembly and telemetry emission
- `src/trellis/stores/base/event_log.py` — Event types for `PACK_ASSEMBLED` and `FEEDBACK_RECORDED`
- `docs/design/adr-deferred-cognition.md` — Deterministic write path constraint this ADR builds on
