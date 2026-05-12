# ADR: Graph shape constraints — declarative validation for canonical types

**Status:** Proposed
**Date:** 2026-05-12
**Deciders:** Trellis core
**Related:**
- [`./adr-graph-ontology.md`](./adr-graph-ontology.md) — defines the canonical vocabulary this layer validates
- [`./adr-observation-entity-type.md`](./adr-observation-entity-type.md) — first concrete consumer (Observation required fields)
- [`./plan-self-improvement-program.md`](./plan-self-improvement-program.md) — program this follows after
- W3C SHACL spec (informational, not adopted wholesale): https://www.w3.org/TR/shacl/

---

## 1. Context

Trellis applies the W3C semantic web stack selectively:

| Layer | Status | Notes |
|---|---|---|
| RDF | **Not adopted** — Trellis is an LPG, not a triple store. Rejected in [`adr-graph-ontology.md`](./adr-graph-ontology.md) §8.2. | `schema_alignment` URI on properties is the optional bridge. |
| RDFS | **Partial** — canonical vocabulary in `well_known.py` + one-level alias bucketing in Phase 2 retrieval. | No multi-level subclass hierarchy; no domain/range; no inverse-property relations. |
| OWL | **Not adopted, deliberately.** | Industry has rejected formal ontologies for agent KGs. |
| **SHACL** | **Partial via Pydantic + policy gates.** This is the gap this ADR addresses. | Pydantic enforces field-level schemas at write; policy gates enforce permission. Graph-level shape rules are absent. |
| SPARQL | **Partial via canonical DSL.** | Multi-hop variable-bound patterns absent; future work, not in this ADR. |

The SHACL-shaped gap surfaces concretely across the self-improvement program:

- **Item 1 (Observation vocabulary)** declares `kind`, `window_start`, `window_end`, `method` as required. The plan handles this via extractor `raise` — *call-site enforcement, not declarative*. Every producer must remember to validate. Forget once, silent drift.
- **Item 2 (provenance columns)** promotes `source_trace_id` / `agent_id` / `confidence` / `evidence_ref` / `extractor_tier` to first-class columns. The shape claim "every edge with `wasAttributedTo` must have `target` pointing at an `Agent`" is *not expressible* today — only at write-time policy gates per call site.
- **Item 4 (extraction telemetry)** depends on every extractor stamping the same payload shape. There is no declarative contract; each emitter re-implements the dict.
- **Item 5 (well-known promotion loop)** discovers types but cannot detect *malformed instances* of those types. If 800 nodes have `kind="filter_rate"` but only 120 of them carry `window_start`, the promotion loop has no way to surface the inconsistency.
- **Referential integrity** is unchecked. An edge whose `source_id` points at a deleted node is invalid by intent but not by validation. SCD-2 versioning makes "deleted" fuzzy, but there is no shape rule that says "if this edge points at a node, that node's current version must exist."

This ADR proposes a lightweight shape layer over the existing Pydantic + policy gate machinery — **inspired by SHACL, not implementing SHACL**.

## 2. Decision

Introduce a `src/trellis/schemas/shapes.py` module that declares **shape constraints** per canonical entity type and edge kind. Constraints are evaluated at three call sites:

1. **Pre-mutation** (write path) — `MutationExecutor` runs shape validation as a stage between policy gates and execute. Failure → mutation rejected with `ShapeViolationError`.
2. **On-demand** (admin path) — `trellis admin validate-shapes [--type X]` walks the graph and reports violations. Operator-triggered; never auto-fired.
3. **On-read** (advisory only, no rejection) — retrieval emits a `SHAPE_VIOLATION_DETECTED` event when a returned node violates its declared shape. Pack delivery is *not* blocked — observability without enforcement at the read path.

### 2.1 Shape language — the subset adopted

A constraint binds to a target (entity type, edge kind, or property path) and declares one or more predicates. The predicates we adopt:

| SHACL term | Trellis name | Semantics |
|---|---|---|
| `sh:targetClass` | `target_type` | Apply this shape to every node with `node_type=X`. |
| `sh:targetObjectsOf` | `target_edge_kind` | Apply this shape to every edge with `edge_kind=X`. |
| `sh:minCount` / `sh:maxCount` | `min_count` / `max_count` | A property or related edge appears between min and max times. |
| `sh:datatype` | `value_type` | The value matches a Pydantic/Python type. |
| `sh:in` | `allowed_values` | Value is one of a fixed set (small frozenset). |
| `sh:pattern` | `value_pattern` | Value matches a regex (used sparingly — regex is footgun-prone). |
| `sh:class` | `target_node_type` | An edge's target node must have a specific type. (RDFS-style domain/range for *one* end of an edge.) |
| `sh:hasValue` | `equals` | Property equals a fixed value (uncommon; mostly for tag conventions). |
| custom | `referent_must_exist` | An edge's `source_id` / `target_id` must point at a current-version node. (Referential integrity — no SHACL equivalent; Trellis-specific because SCD-2 makes this nontrivial.) |

**Predicates NOT adopted:**

- `sh:not`, `sh:and`, `sh:or`, `sh:xone` — composition. Drops the simplicity payoff.
- `sh:closed` — schema closedness. Trellis open-string contract intentionally precludes this.
- `sh:property` paths beyond one level — multi-hop traversal in shape constraints. Out of scope.
- `sh:sparql` — embedded SPARQL constraints. Not relevant (no SPARQL endpoint).
- `sh:nodeShape` recursion / inheritance — shape hierarchies. Avoided to keep evaluation cheap.

### 2.2 Wire format

Shapes are Python objects, not RDF/Turtle. The wire-format payoff of SHACL (interop with external tooling) doesn't apply at our scale; the cost (Turtle parsing, IRI machinery) is real. We use a `@dataclass`-based DSL:

```python
# src/trellis/schemas/shapes.py

OBSERVATION_SHAPE = NodeShape(
    target_type="Observation",
    property_shapes=[
        PropertyShape(path="properties.kind", min_count=1, max_count=1, value_type=str),
        PropertyShape(path="properties.window_start", min_count=1, max_count=1, value_type=datetime),
        PropertyShape(path="properties.window_end", min_count=1, max_count=1, value_type=datetime),
        PropertyShape(path="properties.method", min_count=1, max_count=1, value_type=str),
        PropertyShape(path="properties.confidence", min_count=0, max_count=1, value_type=float),
    ],
)

WAS_ATTRIBUTED_TO_SHAPE = EdgeShape(
    target_edge_kind="wasAttributedTo",
    source_node_type=None,                 # any subject
    target_node_type="Agent",              # but the target must be an Agent
    referent_must_exist=True,
)
```

Shapes live alongside the canonical registry. Adding a canonical entity type or edge kind without a shape is allowed — shapes are *optional layers*, not requirements. A type with no declared shape passes validation trivially (open-string contract preserved).

### 2.3 Loud failure (POC directive)

- Pre-mutation validation: `ShapeViolationError` raises with the offending node_id + property + violation reason. No silent skip.
- `validate-shapes` CLI: reports violations with file:line shape references. Exit 1 if any violations + `--strict`; otherwise exit 0 with the count.
- Read path: emits `SHAPE_VIOLATION_DETECTED` event with `node_id`, `shape_id`, `violation`. Pack continues — read-path enforcement would break too much existing behavior. The event lets operators see violations without changing pack delivery.

## 3. Why this shape, not full SHACL

| Alternative | Reason rejected |
|---|---|
| **Adopt full SHACL with Turtle wire format** | RDF tooling baggage, IRI machinery, no consumer for the interop. The Python DSL is the right scale. |
| **Reuse Pydantic exclusively** | Pydantic validates within a single object. A shape like "every `wasAttributedTo` edge must target an `Agent`" is multi-object and structural. Pydantic can't express it cleanly. |
| **Extend the policy gate API** | Policy gates are per-mutation, not per-shape. A shape applies to many mutations; conflating the two would force every policy gate to know about every shape. |
| **Build a SPARQL-style query checker** | Too big. Shape validation is structural pattern matching with predicates; SPARQL is query language. Different problem. |
| **JSON Schema with `$ref` chains** | Same shape-level limitation as Pydantic — single-object. Plus drift between JSON Schema and Pydantic types is its own headache. |
| **Don't build this; keep call-site `raise`** | The cost compounds with every new canonical type. The Observation work in Item 1 already has the pattern; future types will replicate it. Centralize once, before the canonical registry grows further. |

## 4. Guardrails

### 4.1 Optional layer

Shapes are *additive* — a type without a shape passes trivially. Adopters who want strict typing layer shapes on; everyone else continues with open-string.

### 4.2 No automatic shape inference

The validator does *not* learn shapes from data. Shape declarations are explicit (in `shapes.py`) and reviewed via PR. Auto-inference would create the same auto-mutation risk that `adr-well-known-promotion-loop.md` Item 5 rejects.

### 4.3 No retroactive validation on read

Read-path validation only *emits an event* — it does not block delivery. Rationale: existing graph data accumulated before a shape was declared. Blocking on read would be a behavior change that breaks every prior agent. Operators run `validate-shapes` explicitly when they want to act on accumulated violations.

### 4.4 Shape evolution is additive

Once a shape is published, it can only **relax** (e.g., raising `max_count`, adding to `allowed_values`). Tightening a shape (lowering `max_count`, narrowing `value_type`) requires a new ADR — same one-way-commitment shape as canonical naming in `adr-graph-ontology.md` §5.4.

### 4.5 Shape definitions live with the registry

`shapes.py` sits beside `well_known.py`. Adding a canonical type and its shape in the same PR is the recommended pattern — keeps the vocabulary and its validation co-located.

### 4.6 Performance budget

Pre-mutation validation runs synchronously; budget is **< 1 ms per mutation** for a node with ≤ 10 declared property shapes. Edges with `referent_must_exist` add one `get_node` per edge endpoint — bounded to 2 lookups per edge. Above these caps, the shape is rejected at registration time.

## 5. Consequences

### 5.1 What this enables

- Declarative replacement for the call-site `raise` patterns in Item 1 (Observation), Item 4 (extraction-failure payload), and future types.
- Graph-level invariants checkable at admin time — "audit my graph for `wasAttributedTo` edges that don't point at Agents."
- Read-path observability of accumulated violations without breaking existing behavior.
- A natural seam for adopters who want stricter validation per their domain (the open-string types in `trellis_workers` can declare their own shapes in their own packages).

### 5.2 What this does *not* do

- Does not introduce SPARQL-shaped multi-hop pattern matching (separate ADR if/when a workload demands it).
- Does not introduce RDFS-style subclass inheritance. `Team` is still a sibling of `Organization` in the type system; shape rules on `Organization` do *not* automatically apply to `Team`.
- Does not introduce automatic shape inference from data.
- Does not change `well_known.py` semantics.
- Does not enforce on read.

### 5.3 What this costs

- One new module (`shapes.py`) + per-mutation validation cost (< 1 ms).
- A new event type (`SHAPE_VIOLATION_DETECTED`).
- New CLI subcommand (`trellis admin validate-shapes`).
- A small dataclass DSL with ~10 predicates.

## 6. Phases

| Phase | Scope | Gating |
|---|---|---|
| **0** | This ADR + `shapes.py` module + `ShapeViolationError` + dataclass DSL + 10 predicates + unit tests. **No** shapes declared yet. | Self-improvement program items 1, 2, 4, 5 have landed. |
| **1** | Pre-mutation validation stage in `MutationExecutor`. Default-on; opt-out env var for fast-path debugging. Shapes for `Observation`, `Measurement`, `Activity` (Items 1 + 6). | Phase 0 landed. |
| **2** | `validate-shapes` CLI + walking-the-graph evaluator. Read-path advisory events. | Phase 1 landed; operator wants to audit accumulated data. |
| **3** | Shape declarations for the remaining canonical types (`Person`, `Organization`, etc.) — additive PRs per type. | Adopter / partner asks for one. |
| **4** | Shape contributions from `trellis_workers` (domain-specific types declaring their own shapes in their own packages). | Domain integration ships. |

Phases 0–2 are the deliverable of this ADR. Phases 3+ are operator-/partner-gated.

## 7. References

- W3C SHACL spec (informational): https://www.w3.org/TR/shacl/
- Pydantic v2 docs (current write-path validator): https://docs.pydantic.dev/
- `adr-graph-ontology.md` §5.4 — one-way-commitment policy (applies to shapes too)
- `adr-tag-vocabulary-split.md` §5 — Phase 5 of that ADR proposes `CUSTOM_TAG_USED` telemetry; this ADR's read-path `SHAPE_VIOLATION_DETECTED` is the analogous mechanism for shapes
