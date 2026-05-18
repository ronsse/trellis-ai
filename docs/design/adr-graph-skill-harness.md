# ADR: Graph-skill harness — bounded agent loops against Trellis data

**Status:** Proposed
**Date:** 2026-05-18
**Deciders:** Trellis core
**Related:**
- [`./adr-coding-agent-loop.md`](./adr-coding-agent-loop.md) — closest sibling harness; the proposals-only loop whose security model this ADR adopts and adapts
- [`./adr-coding-agent-loop-cohort2-amendment.md`](./adr-coding-agent-loop-cohort2-amendment.md) — autonomous-spawn controls; precedent for the kill-switch + budget envelope pattern
- [`./plan-self-improvement-program.md`](./plan-self-improvement-program.md) — program-level framing for Items 1–7 (what shipped) and §5.9 (security baseline)
- [`./adr-llm-client-abstraction.md`](./adr-llm-client-abstraction.md) — the `LLMClient` Protocol this harness consumes
- [`./adr-planes-and-substrates.md`](./adr-planes-and-substrates.md) — Knowledge / Operational plane split that constrains where a skill may read and where its telemetry lands
- [`./adr-inner-curation-loop.md`](./adr-inner-curation-loop.md) — sibling ADR being authored in parallel (Wave 1 Unit B); the curator that consumes this harness in F2

---

## 1. Context

The Self-Improvement Program (Items 1–7, shipped 2026-05-11 → 2026-05-16) produced a system that **observes itself well**: extraction failures cluster into `EXTRACTION_FAILED` events, open-string types graduate via `WELL_KNOWN_CANDIDATE`, advisory fitness lands `ADVISORY_DRIFT_DETECTED`, dogfooded analyses leave `Activity` nodes via `record_meta_analysis()`, and Item 7's `ProposalGenerator` turns those signals into reviewable markdown. The retrieval-side knobs (`ParameterRegistry`, score tuning, advisory restoration) are wired and converging on the eval scenarios.

What none of those items do — and what Phase F (inner agent loop / curation loop) needs — is **run a bounded LLM-backed loop against the graph itself**. Item 7's `code_authoring/` spawns Claude Code SDK against a markdown proposal; that path is filesystem-and-git shaped (read files, edit files, `git diff`, open a draft PR). It works for "draft a code change for human review." It does not work for "read the entity, look at its neighbors, decide whether to write a `hasObservation` edge through `MutationExecutor`." Claude Code's tool surface is filesystem + git; the curator's tool surface is `read_node` + `search_graph` + `propose_mutation`.

The four follow-on Phase F units (F2 curator, F3 retrieval lazy enrichment, F4 outcome feedback, F5 score-based evolver) each need to **dispatch a small LLM-backed agent against a triggering event, give it bounded access to the graph, and capture what it did**. They each need the same substrate. This ADR defines that substrate — the harness for running bounded *graph skills* against Trellis data, sibling to but distinct from `code_authoring/`.

Building it badly creates two distinct failure modes: an unbounded harness loops on every event and balloons cost; an under-secured harness lets a skill bypass `MutationExecutor` and write directly to a store, ducking policy + idempotency. The design below addresses both by making the tool surface a fixed allowlist and the only write path the same governed pipeline every other Trellis writer uses.

## 2. Decision

Introduce `src/trellis_workers/agent/` — a new worker package, peer to `enrichment/`, `extract/`, and `code_authoring/` — that runs bounded "graph skills" against Trellis data. The package ships the harness; concrete skills live in `src/trellis_workers/agent/skills/` and are loaded by `skill_id`. The harness is consumed by F2–F5; this ADR does not ship a skill.

### 2.1 Package layout

```
src/trellis_workers/agent/
    __init__.py          # re-exports SkillHarness, Skill, SkillResult, the five tool callables
    harness.py           # SkillHarness — the loop driver
    skill.py             # Skill dataclass: parsed frontmatter + body + resolved LLMClient
    tools.py             # the five tool callables (read_node, read_document, …) and the allowlist enforcer
    budget.py            # cumulative-spend ledger over EventLog (mirrors code_authoring/budget.py shape)
    telemetry.py         # event-emission helpers for the five SKILL_* event types
    skills/
        __init__.py      # discovers *.md files; no skill ships with this PR
```

Mirrors `enrichment/`, `extract/`, and `code_authoring/` — same package shape, same `__init__.py` re-export discipline, same `telemetry.py` boundary. New imports are kept inside `trellis_workers.agent`; the harness consumes `trellis.llm.LLMClient`, `trellis.stores.registry.StoreRegistry`, and `trellis.mutate.executor.MutationExecutor` through their existing public surfaces.

### 2.2 Skill artifact — markdown with YAML frontmatter

A skill is a single markdown file. Frontmatter is the machine-readable contract; the body is the system prompt the harness ships to the LLM. Mirrors the proposal-as-markdown discipline from [`adr-coding-agent-loop.md`](./adr-coding-agent-loop.md) §2.2 — markdown is human-readable, diff-friendly, version-controllable, and degrades gracefully when the schema grows.

```markdown
---
skill_id: curator.attach_observation
version: 1
triggers:
  - observation.recorded
  - measurement.recorded
tools_allowed:
  - read_node
  - read_document
  - search_graph
  - propose_mutation
  - emit_event
budget_cents: 5
max_steps: 8
success_criteria: |
  Either (a) a LINK_CREATE for the appropriate hasObservation edge has
  been proposed and accepted, or (b) the harness emits SKILL_COMPLETED
  with outcome="deferred" naming the missing precondition.
model: claude-sonnet-4-7  # optional; harness resolves via LLMClient if absent
---

## When to act

[system prompt body — the operator-authored guidance the LLM sees first]

## Examples

[few-shot examples; optional]

## When to defer

[explicit "do not act" conditions — what makes the skill emit deferred]
```

Frontmatter fields are fixed in v1:

| Field | Type | Required | Meaning |
|---|---|---|---|
| `skill_id` | str | yes | Stable identity. Used for dispatch lookup, telemetry, and budget accounting. Must match the filename stem. |
| `version` | int | yes | Monotonic per `skill_id`. The harness records the version on every `SKILL_DISPATCHED` event. |
| `triggers` | list[str] | yes | Event type values (e.g. `observation.recorded`) that wake this skill. The dispatcher matches against `EventType` enum values exactly. |
| `tools_allowed` | list[str] | yes | Subset of the five tool names in §2.3. Anything outside the set is a load-time error. Empty list is a load-time error (skill with no tools is misconfigured, not "passive"). |
| `budget_cents` | int | yes | Per-invocation cap on `TokenUsage`-derived cost. The harness aborts mid-loop on overflow. |
| `max_steps` | int | yes | Hard cap on planner iterations. Reached → `SKILL_FAILED` with `reason="max_steps_exceeded"`. |
| `success_criteria` | str | yes | Free-form terminal condition. The harness shows this to the LLM each step and uses it to decide whether to set `outcome` on `SKILL_COMPLETED`. Not parsed. |
| `model` | str | no | Provider-agnostic model hint. The harness resolves via `LLMClient`; absent means "harness default." |

The schema is enforced at load time. A skill that fails to parse is a load-time error; no silent skip (per the program-wide POC directive in `plan-self-improvement-program.md` §2).

### 2.3 Tool surface — exactly five tools, allowlist-enforced per skill

The harness exposes a closed set of five tools in v1. A skill can call only the tools named in its `tools_allowed` frontmatter list. The harness rejects an out-of-allowlist call as a hard error and emits `SKILL_FAILED` with `reason="tool_disallowed"`.

| Tool | Signature | Backed by | Notes |
|---|---|---|---|
| `read_node` | `read_node(node_id: str) -> NodeView \| None` | `GraphStore.get_node()` via `registry.knowledge.graph_store` | Reads the current SCD-2 row. `as_of` is intentionally absent in v1 — time-travel reads are F-follow-up scope. |
| `read_document` | `read_document(document_id: str, max_tokens: int = 2000) -> DocumentView` | `DocumentStore.get()` via `registry.knowledge.document_store` | Truncates `content` to `max_tokens` (estimated at ~4 chars/token, matching `PackBuilder`); records the truncation in the returned view. |
| `search_graph` | `search_graph(query: NodeQuery) -> list[NodeView]` | `GraphSearch` strategy + the canonical `NodeQuery` DSL in `trellis.stores.base.graph_query` | Phase 1 + 2 operators only (`eq` / `in` / `exists` / `lt` / `lte` / `gt` / `gte`). No regex; no FTS — `read_document` is the path for content lookup. |
| `propose_mutation` | `propose_mutation(command: Command) -> CommandResult` | `MutationExecutor.execute()` | Submits `ENTITY_CREATE` / `ENTITY_UPDATE` / `LINK_CREATE` / `LINK_REMOVE` / `LABEL_ADDED` / … through the governed pipeline. **The harness NEVER bypasses `MutationExecutor`.** Policy + idempotency + audit emission are non-negotiable. The skill sees the `CommandResult`, including the `status` discriminator (`SUCCESS` / `REJECTED` / `DUPLICATE` / `FAILED`), and decides how to proceed. |
| `emit_event` | `emit_event(event_type: str, payload: dict) -> None` | `EventLog.emit()` on the operational plane | Restricted to event types in a static allowlist (curator-domain `OBSERVATION_*` + `SKILL_OUTCOME_RECORDED`; the harness's own `SKILL_DISPATCHED` / `SKILL_STEP` / `SKILL_COMPLETED` / `SKILL_FAILED` are emitted by the harness itself, not by the skill). The skill cannot forge a `MUTATION_EXECUTED` event. |

The signatures are *the* contract. v1 will not grow this list. Adding a sixth tool — `read_trace`, `read_blob`, anything — requires an ADR amendment. The closure is deliberate; see §3.3.

`NodeView` and `DocumentView` are immutable, narrowed views over their store-layer counterparts (no methods, no back-references) so a skill cannot mutate state by mutating a returned object. They are defined in `trellis_workers.agent.tools`.

### 2.4 Concurrency — single skill per trigger in v1

Multiple skills may register the same event type in `triggers`. v1's dispatcher fires the **highest-`version` skill per `skill_id` for each matching trigger**, **one at a time** per event — single skill per trigger, not multi-skill chaining. Concurrent dispatch across distinct events is unbounded; the existing `MutationExecutor` policy + idempotency pipeline handles write conflicts. The skill author writes idempotency-safe code (idempotency keys on every `propose_mutation`) and assumes their work may race with another skill's response to a different trigger.

Multi-skill-per-trigger chaining (orchestrated pipelines, fan-out, conditional gating) is an F2.x follow-up — a curator-level concern, not a harness primitive. Punching it down into the harness now would force every skill author to reason about ordering semantics that the curator can give them declaratively later.

### 2.5 Security — adopts the program-wide model, narrows it for skills

The harness inherits the security baseline from [`plan-self-improvement-program.md`](./plan-self-improvement-program.md) §5.9 and the operational-spend envelope from [`adr-coding-agent-loop.md`](./adr-coding-agent-loop.md) §2.4 + §2.6:

- **Closed tool surface.** No skill may register a tool that bypasses `MutationExecutor`. The five tools in §2.3 are the v1 surface. Expanding it requires an ADR amendment. This is the load-bearing constraint.
- **Allowlist enforcement at every step.** A skill calling a tool not in its `tools_allowed` is rejected before the call dispatches. The check is not advisory.
- **Env-var secret scrubbing.** The harness, before each LLM call, drops any env var matching `*_KEY|*_SECRET|*_TOKEN|*_PASSWORD` from the subprocess + tool environment. Mirrors the coding-agent loop's spawn-time scrub but applies per-step inside the harness loop. (The harness does not spawn subprocesses, but the `propose_mutation` path can reach `LLMClient` providers that read env vars — the scrub is belt-and-suspenders against accidental leakage.)
- **Cumulative weekly LLM-spend cap.** `TRELLIS_LLM_BUDGET_CENTS_WEEK` is the existing envelope; the harness honors it as an additional upper bound across **all skill invocations** in the trailing 7-day window, computed from `BUDGET_CONSUMED` events. Skill-local `budget_cents` is the per-invocation cap; the week cap is the global cap; the spawn is rejected if either is exhausted. No silent skip.
- **Kill switch.** `TRELLIS_GRAPH_SKILLS_ENABLED` defaults to `false`. The harness refuses to dispatch when the flag is unset. Mirrors the Cohort 2 amendment's `TRELLIS_AUTONOMOUS_SPAWN_ENABLED` pattern; one binary switch per autonomous-feature surface.

The skill surface intentionally has **no filesystem write tool, no `git` invocation, no subprocess spawn**. A skill that wants to author code files calls the `code_authoring/` proposal generator, which is a separate worker with a separate security model. The two systems do not merge.

### 2.6 LLM client integration — `LLMClient` Protocol, no new abstraction

The harness consumes the existing [`LLMClient`](../../src/trellis/llm/protocol.py) Protocol — same one `EnrichmentService` and `LLMExtractor` use today. Skills declare an optional `model` in frontmatter; absent, the harness uses the registry-resolved default (`StoreRegistry.build_llm_client()`). Both Anthropic and OpenAI work from day one because both ship implementations in `trellis.llm.providers`; a third provider works by registering a `LLMClient` implementation, no harness change.

The harness loop is, in shape:

```python
async def run(self, skill: Skill, trigger_event: Event) -> SkillResult:
    self._emit_skill_dispatched(skill, trigger_event)
    for step in range(skill.max_steps):
        plan = await self._llm.generate(messages=self._build_messages(skill, history))
        self._record_tokens(plan.usage)
        if self._budget_exhausted():
            return self._fail(skill, reason="budget_exhausted")
        tool_call = self._parse_tool_call(plan.content)
        if tool_call.name not in skill.tools_allowed:
            return self._fail(skill, reason="tool_disallowed")
        observation = await self._dispatch_tool(tool_call)
        self._emit_skill_step(skill, step, tool_call, observation)
        history.append((plan, tool_call, observation))
        if self._satisfies_success_criteria(skill, plan):
            return self._complete(skill)
    return self._fail(skill, reason="max_steps_exceeded")
```

This is sketch, not contract; the implementation PR (F1) will land the typed shapes. The contract is: one LLM call per loop iteration, one tool dispatch per loop iteration, one `SKILL_STEP` per loop iteration, the loop terminates on success-criteria match, `max_steps` exhaustion, budget exhaustion, or tool-disallowed (the four terminal states).

### 2.7 Telemetry — five reserved event types

The harness emits five event types from the operational `EventLog`. They are **reserved** at this ADR — the enum values land in `trellis.stores.base.event_log.EventType`, no consumer wiring ships with this ADR. F1 wires the harness; F4 wires the consumer (outcome feedback). The reservation prevents naming collisions across the four parallel Phase F units.

| Event type | Enum value | When | Payload schema |
|---|---|---|---|
| `SKILL_DISPATCHED` | `skill.dispatched` | The harness has selected a skill for a trigger event and is about to run the loop. | `{skill_id, skill_version, trigger_event_id, trigger_event_type, started_at}` |
| `SKILL_STEP` | `skill.step` | One loop iteration completed (planner call + tool dispatch + observation). | `{skill_id, skill_version, step_index, tool_name, tool_args_hash, observation_summary, tokens_consumed, dollars_estimated}` |
| `SKILL_COMPLETED` | `skill.completed` | The skill loop terminated by satisfying success criteria. | `{skill_id, skill_version, steps_taken, total_tokens, total_dollars, outcome}` (`outcome` ∈ `{"acted", "deferred"}`) |
| `SKILL_FAILED` | `skill.failed` | The skill loop terminated by budget exhaustion, max-steps, tool-disallowed, or unexpected error. | `{skill_id, skill_version, steps_taken, total_tokens, total_dollars, reason, error_excerpt}` |
| `SKILL_OUTCOME_RECORDED` | `skill.outcome_recorded` | F4 (outcome feedback, downstream of this ADR) records whether the skill's action was upheld or reverted. Reserved here; not emitted by the harness itself — emitted by the outcome-feedback consumer. | `{skill_id, skill_version, dispatched_event_id, completion_event_id, verdict, evidence_event_ids}` (`verdict` ∈ `{"upheld", "reverted", "no_signal"}`) |

`SKILL_STEP` granularity is **one event per loop iteration**, not per LLM call or per tool call. The loop's invariant ("one planner call + one tool dispatch + one observation per iteration") makes this the right granularity — finer-grained events would explode the EventLog for no consumer benefit. `tool_args_hash` is a SHA-256 over the canonicalized args so an analyzer can detect "skill is calling `read_node(X)` on every step" without retaining the args themselves.

`BUDGET_CONSUMED` events (`EventType.BUDGET_CONSUMED`, already reserved by Item 7 / E3) are emitted once per skill run, **always** — including when the run aborts on budget exhaustion. `source="trellis_workers.agent.harness"` discriminates from other `BUDGET_CONSUMED` emitters (the eval scenario, the coding-agent loop).

## 3. Why this shape

### 3.1 Why a custom harness, not Claude Code SDK

Claude Code SDK is built for one shape of work: read files, modify files, run shell commands, commit. Item 7's `code_authoring/` uses it correctly — the deliverable there is a draft PR with a diff. Phase F's deliverable is the opposite: a graph mutation, an emitted event, a tagged entity. The tools are `read_node` / `search_graph` / `propose_mutation`, not `Read` / `Edit` / `Bash`. Forcing graph-shaped work through a filesystem-shaped tool surface would either (a) materialize every graph operation as a file write that a separate process reads back, doubling the I/O and losing the typed `Command` shape, or (b) extend the SDK with custom tools, at which point we have built a custom harness and shipped a bigger dependency to host it.

The Anthropic + OpenAI SDKs both expose tool-use APIs at the level we need (a planner returns a tool call; the harness dispatches; the observation goes back as a message). `trellis.llm.LLMClient` is already the abstraction. Building a small loop driver over it is ~300 LOC; importing Claude Code SDK pulls a much larger surface for a non-fit use case. The decision is "use the right tool for the shape of the work," not "Claude Code SDK is bad."

### 3.2 Why markdown artifact

Three reasons, in order of weight:

1. **Diffability.** Skill iteration is the load-bearing flywheel for F5 (score-based evolver). Operators will read, compare, and edit skill files manually. A markdown body next to a YAML header reads like every other Trellis design doc; a JSON schema is opaque.
2. **Graceful schema growth.** v1's frontmatter has eight fields. v3's will have more. Markdown frontmatter degrades — old skills still parse with new fields as `None`, new skills don't break the loader on missing optional fields. A JSON or Pydantic-only artifact has the same property but reads worse.
3. **Precedent.** [`adr-coding-agent-loop.md`](./adr-coding-agent-loop.md) §2.2 made the same call for the same reasons. Two systems with the same artifact shape reduce the operator's mental load.

### 3.3 Why this fixed tool surface — not "extensible by skill"

A skill that can register its own tools is a skill that can register `write_to_disk` or `shell_out`. The whole security argument collapses. The fixed five-tool surface is the load-bearing constraint that makes the rest of the security model coherent: a skill can do exactly the things in §2.3 and no others. The five tools are sufficient for the F2–F5 use cases by construction — they were chosen *because* they cover those use cases without enabling a sixth.

Expanding the surface in a future ADR is correct and expected. Letting a skill expand it itself, by registering an extension hook, is the foothold an attacker (or a buggy skill) uses to escape. The pattern matches `MutationExecutor`'s closed operation registry — handlers are injected by code, not by data.

### 3.4 Why opt-in dispatch by default

`TRELLIS_GRAPH_SKILLS_ENABLED=false` as the default mirrors the conservative posture every other autonomous-feature surface in Trellis adopts (`TRELLIS_LLM_BUDGET_CENTS_WEEK=0` from [`adr-coding-agent-loop.md`](./adr-coding-agent-loop.md) §2.6, `TRELLIS_AUTONOMOUS_SPAWN_ENABLED=false` from the Cohort 2 amendment §2.1). The harness lands in tree, the F1 unit tests cover dispatch, but no deployment runs skills unless an operator explicitly flips the flag. This is the project's house pattern; deviating would create asymmetric defaults across the worker packages.

### 3.5 Why `MutationExecutor` as the only write path

`MutationExecutor` does five things every Trellis write needs: validate against the operation registry, run the policy gate, check idempotency (in-memory + persisted), execute the handler with typed exception routing, emit a `MUTATION_EXECUTED` / `MUTATION_REJECTED` audit event. Replicating any of those in a skill-local write path means either (a) the skill does it worse, or (b) the skill silently bypasses it. Both are unacceptable. The skill submits a `Command`, the executor decides; the skill sees the `CommandResult` and reacts.

This also means a skill cannot author a write the policy gate would reject — the rejection lands as a `REJECTED` `CommandResult`, the skill sees it, the audit event captures the attempt. The "attempted write" trail is itself signal for F4's outcome feedback.

## 4. Guardrails

The security model from §2.5 in concrete operational terms:

- **Diff-level write surface:** the only diff the harness can produce against any Trellis store is via a `Command` submitted to `MutationExecutor`. There is no other write path. A skill that imports `GraphStore` directly is rejected at module-load time (see §4.1).
- **Per-step budget check:** before each LLM call, the harness sums `BUDGET_CONSUMED` events emitted in the trailing 7 days with `source="trellis_workers.agent.harness"`. If `sum + estimated_cost_of_next_call > TRELLIS_LLM_BUDGET_CENTS_WEEK`, the harness aborts with `SKILL_FAILED(reason="weekly_budget_exhausted")`. Per-invocation `budget_cents` is checked at the same point against the cumulative spend in the *current* invocation only.
- **Tool-call validation:** the harness parses the LLM's tool-call output, looks up the tool name in `tools_allowed`, validates the args against the tool's signature, and only then dispatches. A parser failure is a `SKILL_FAILED(reason="malformed_tool_call")`. The skill never sees raw store handles.
- **Event-emission allowlist:** `emit_event` rejects any event type outside a static allowlist (the curator-domain events plus `SKILL_OUTCOME_RECORDED`). The harness's own `SKILL_*` events are emitted by the harness, not by the skill, so a malicious skill cannot forge a "completed" event.
- **Read-only `read_*` tools:** `read_node` / `read_document` / `search_graph` return `NodeView` / `DocumentView` / `list[NodeView]` — immutable narrowed views. They expose no mutation methods and no back-references to live store objects.

### 4.1 New guardrails this ADR adds

- **No skill may import from `trellis.stores.*`, `trellis.mutate.executor`, or `trellis.llm.providers.*`.** Enforced via an import lint rule in `tests/unit/agent/test_skill_imports.py` that runs `ast` over every skill module under `skills/` and rejects offending imports. Skills consume tools through `trellis_workers.agent.tools` — not the underlying stores. The lint rule is the load-bearing check; the runtime check (a skill that somehow imports a store and dies on attribute access) is belt-and-suspenders.
- **`tools_allowed` is the *complete* permission grant.** A skill with `tools_allowed: [read_node]` cannot call `read_document` even if it constructs the call correctly. There is no implicit "all read tools are allowed because the skill is read-only" rule; the allowlist is the surface.
- **`skill_id` namespace reservation.** Skill IDs must match `^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$` (e.g. `curator.attach_observation`). The prefix before the dot is the *skill domain* and is curator-controlled (F2 reserves `curator.*`, F3 reserves `retrieval.*`, F4 reserves `outcome.*`, F5 reserves `evolver.*`). This prevents two unrelated Phase F units from shipping conflicting skills with the same name.

## 5. Consequences

### 5.1 What this enables

- **F1 lands.** The harness substrate is in tree; F2 (curator), F3 (retrieval lazy enrichment), F4 (outcome feedback), and F5 (score-based evolver) can each ship a concrete skill against a stable contract.
- **Skills compose naturally with the existing program.** A curator skill that emits `propose_mutation(ENTITY_UPDATE)` triggers the existing `MUTATION_EXECUTED` event the rest of the system already consumes — the dogfooded analyses, the advisory generator, the effectiveness loop. No new bridges.
- **Dual-provider day one.** Both Anthropic and OpenAI skills work because both `LLMClient` implementations already ship. A team running Anthropic in prod and OpenAI in dev needs zero harness changes.
- **The "what is the agent doing right now" question becomes queryable.** `SKILL_STEP` events, joined to `SKILL_DISPATCHED`, give operators a per-loop trace of every autonomous run. This is the substrate the eval scenario in `eval/scenarios/skill_loop_convergence/` (Wave 1 Unit D, parallel) needs.

### 5.2 What this does not do

- **Does not ship a skill.** F1 ships the harness + the import lint + the unit tests. F2 ships the first concrete skill. The two are deliberately separated so the contract is reviewed without a use case to drift it.
- **Does not enable a skill to author code.** Code authoring lives in `code_authoring/` with its own security model. A skill that wants to draft a code change emits an event the `ProposalGenerator` consumes.
- **Does not chain skills.** v1 is one skill per trigger event. Multi-skill workflows (one skill's `SKILL_COMPLETED` triggering another skill) are an F2.x curator-level concern, not a harness primitive.
- **Does not introduce a new LLM abstraction.** `LLMClient` is the only Protocol the harness depends on; no `SkillClient`, no `PlannerClient`.

### 5.3 What this costs

- **LLM spend per invocation.** Estimated $0.001–$0.05 per skill run depending on `max_steps`, model, and tool-call density. The weekly cap envelope is `TRELLIS_LLM_BUDGET_CENTS_WEEK`; default zero means the system is dry-run-only until an operator opts in. POC consistency with the rest of the program.
- **EventLog volume.** Five new event types, with `SKILL_STEP` firing potentially `max_steps` times per run. A skill at `max_steps=8` dispatched 100 times/day produces 800 `SKILL_STEP` rows/day. For SQLite-local deployments this is negligible; for Postgres-operational deployments the existing retention story applies. No change in storage architecture is required.
- **Operator attention.** A new "skills are running and here's their step log" surface enters the operator's review workflow. Mitigated by the kill switch defaulting off — operators opt in by feature, not by deployment.

## 6. Alternatives considered

- **Use Claude Code SDK.** Rejected for the tool-shape reason in §3.1. Claude Code's tool surface is filesystem + git; the harness needs graph-internal tools. Wrapping graph operations as filesystem calls would double the I/O and lose typed `Command` shape, and customizing the SDK to expose graph tools is "we built our own harness" by another name.
- **Build on `src/trellis_workers/engine/`.** That package exists today and ships `WorkflowEngine` + `ThinkingPolicy` + `TierConfig`. It is **not** what Phase F needs — `WorkflowEngine` is a tier-routing policy for cognition (escalate from FAST to DEEP), not a loop driver with a tool surface. Whether `engine/` is retained for other uses or retired is the scope of `docs/research/workflow-engine-disposition.md` (Wave 1 Unit C, parallel). This ADR does not pre-empt that decision: even if `engine/` is kept, it solves a different problem and is not the substrate for `agent/`. If `engine/` is retired, the harness lands without absorbing any of its surface.
- **Adopt LangGraph (or a similar agent framework).** Rejected. The dependency surface is large, the abstractions overlap awkwardly with `MutationExecutor` and `LLMClient` (LangGraph wants to own the state machine, we already have a governed pipeline), and the project's pattern is Protocol-in-core + thin implementations. A LangGraph-shaped harness would either expose LangGraph's idioms to skill authors (leaking a heavy dep into the artifact contract) or wrap it (the wrap is the harness, and at that point the underlying framework is gratuitous).
- **Bare `LLMClient.generate()` call sites in each F-unit consumer.** Rejected. Each consumer would reinvent: tool-call parsing, allowlist enforcement, budget accounting, telemetry emission, max-step loops. The result is four near-duplicate implementations with subtle drift, none of which has been security-reviewed once let alone four times. The harness is the consolidation.

## 7. References

- [`./adr-coding-agent-loop.md`](./adr-coding-agent-loop.md) — sibling harness, proposals-only, file/git tool surface
- [`./adr-coding-agent-loop-cohort2-amendment.md`](./adr-coding-agent-loop-cohort2-amendment.md) — autonomous-spawn controls precedent
- [`./plan-self-improvement-program.md`](./plan-self-improvement-program.md) §5.9 — security baseline
- [`./adr-llm-client-abstraction.md`](./adr-llm-client-abstraction.md) — `LLMClient` Protocol
- [`./adr-planes-and-substrates.md`](./adr-planes-and-substrates.md) — Knowledge / Operational plane discipline
- [`./adr-inner-curation-loop.md`](./adr-inner-curation-loop.md) — sibling Phase F Wave 1 Unit B (parallel; F2 curator consumes this harness)
- [`../../src/trellis/mutate/executor.py`](../../src/trellis/mutate/executor.py) — the write path `propose_mutation` calls
- [`../../src/trellis/llm/protocol.py`](../../src/trellis/llm/protocol.py) — the `LLMClient` Protocol the harness consumes
- [`../../src/trellis/stores/base/event_log.py`](../../src/trellis/stores/base/event_log.py) — the `EventType` enum where the five `SKILL_*` values land
