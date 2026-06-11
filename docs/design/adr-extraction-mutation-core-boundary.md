# ADR: Extraction-to-mutation core-vs-integration boundary

**Status:** Proposed
**Date:** 2026-06-03
**Deciders:** Trellis core
**Resolves:** #185, #195, #196, #211, #215, #214, #224
**Related:**
- [`./adr-plugin-contract.md`](./adr-plugin-contract.md) — entry-point runtime extensions; what graduates to core vs ships as a wheel
- [`./adr-planes-and-substrates.md`](./adr-planes-and-substrates.md) — Knowledge vs Operational Plane, the two sanctioned cross-plane bridges, "EventLog authoritative"
- [`./adr-graph-ontology.md`](./adr-graph-ontology.md) — open-string types + well-known defaults; domain types live in domain packages
- [`./adr-observation-entity-type.md`](./adr-observation-entity-type.md) — general-purpose vs domain-specific entity-type test (the precedent for #224)
- [`./adr-column-leaf-modeling-guardrails.md`](./adr-column-leaf-modeling-guardrails.md) — *forthcoming* (#221) — sibling modeling-boundary decision (columns as node vs property)
- [`./adr-enterprise-ontology-capability-framing.md`](./adr-enterprise-ontology-capability-framing.md) — *forthcoming* (#217 umbrella) — the capability-framing umbrella this ADR sits under

---

## 1. Context

The `consumer-kg` integration package (an external consumer that populates a
Trellis Knowledge Plane from Unity Catalog metadata, Databricks query history, and a
Docusaurus docs repo) was the first heavy, real-world driver of the
extraction→mutation path. Building it surfaced seven behaviors that the package had to
hand-roll because core either lacked the seam or left the contract ambiguous. Each is
filed as an issue tagged `area:core-boundary`.

The recurring question is not *what* the behavior should do — the package already
proved each one works — but *where it belongs*: in core (`trellis`), or in the
integration package, or split as a core contract with a package-side adapter. Getting
this wrong in either direction is costly:

- **Too eager to promote.** Core absorbs domain-specific assumptions (Unity Catalog
  three-part table names, MDX chrome, Databricks notebook parsing) and the open-string,
  domain-packages-own-their-types posture from [`adr-graph-ontology.md`](./adr-graph-ontology.md)
  erodes.
- **Too reluctant to promote.** Every integration re-implements the
  `EntityDraft`/`EdgeDraft`→`Command` mapping slightly differently, silently dropping
  Trellis-owned fields (#185 is exactly this failure), and the "all mutations go through
  `MutationExecutor`" hard rule decays into "all mutations go through *a* hand-rolled
  executor."

What core already owns and we are *not* relitigating:

- The draft and command schemas: `EntityDraft` / `EdgeDraft` / `ExtractionResult`
  ([`src/trellis/schemas/extraction.py`](../../src/trellis/schemas/extraction.py)) and
  `Command` / `Operation`
  ([`src/trellis/mutate/commands.py`](../../src/trellis/mutate/commands.py)).
- The 5-stage governed pipeline (validate → policy → idempotency → execute → emit
  event) in [`src/trellis/mutate/executor.py`](../../src/trellis/mutate/executor.py).
- The `allow_dangling` flag on `EdgeDraft` and the `LinkCreateHandler` FK pre-flight
  ([`src/trellis/mutate/handlers.py`](../../src/trellis/mutate/handlers.py)).
- The SCD-2 edge upsert in
  [`src/trellis/stores/bolt_opencypher/graph.py`](../../src/trellis/stores/bolt_opencypher/graph.py)
  and the `GraphStore` ABC in
  [`src/trellis/stores/base/graph.py`](../../src/trellis/stores/base/graph.py).

This ADR rules on each issue and states one cross-cutting graduation principle (§4).

## 2. Per-issue decisions

Each subsection gives the ruling, the rationale, and a concrete shape. **This is a
design document; nothing below is claimed as implemented.**

### 2.1 #185 — canonical draft→command helper · **Promote to core**

**Ruling: Promote to core.** Trellis owns the draft and command schemas, so Trellis owns
the mapping between them. The `consumer-kg` `_draft_to_command()` copied
`entity_id` / `entity_type` / `name` / `properties` but dropped `node_role` and
`generation_spec`, silently demoting structural and curated nodes to the default
`NodeRole.SEMANTIC`. That is a *schema-fidelity* bug, and schema fidelity is a core
responsibility — every consumer that re-derives the mapping is one refactor away from
the same regression.

**Shape.** A pure conversion module in core (no store access, no I/O), e.g.
`trellis.mutate.from_drafts`:

```python
def entity_draft_to_command(draft: EntityDraft, *, requested_by: str) -> Command:
    """ENTITY_CREATE carrying every Trellis-owned field, not just the obvious four."""
    # args includes: entity_type, name, properties, node_role, generation_spec,
    # confidence, and entity_id when the draft pins one.

def edge_draft_to_command(draft: EdgeDraft, *, requested_by: str) -> Command:
    """LINK_CREATE preserving allow_dangling and properties."""

def extraction_result_to_commands(
    result: ExtractionResult, *, requested_by: str
) -> list[Command]:
    """Batch helper. Entity creates ordered before the edge creates that reference
    them, so a STOP_ON_ERROR batch resolves draft-local endpoint references in order."""
```

Field coverage is pinned against `EntityDraft` / `EdgeDraft` so adding a field to a
draft and forgetting to thread it through the helper is a test failure, not a silent
drop. Concretely the helper must round-trip:

- `EntityDraft(node_role=NodeRole.STRUCTURAL)` → `ENTITY_CREATE` with
  `args["node_role"] == NodeRole.STRUCTURAL`.
- `EntityDraft(node_role=NodeRole.CURATED, generation_spec=...)` → both fields present.
- `EdgeDraft(allow_dangling=True)` → `LINK_CREATE` with `args["allow_dangling"] is True`.

The helper does **not** weaken validation: the store/handler boundary still rejects
invalid role/provenance combinations exactly as today. The helper just guarantees the
fields *arrive* at that boundary. `requested_by` follows the existing
`<surface>:<verb>` convention from `Command`'s docstring (e.g. `worker:uc-ingest`).

Integration packages are then expected to call the helper instead of hand-rolling, and
the extractor-authoring docs (Playbook 13, referenced by
[`adr-plugin-contract.md`](./adr-plugin-contract.md)) point at it. `consumer-kg`
deletes its local `_draft_to_command()`.

### 2.2 #195 — edge dedup on re-ingest · **Core contract + completeness fix (bug)**

**Ruling: this is a core contract, and the gap is a core bug.** Idempotent re-ingest of
the same *logical* edge is a property the `GraphStore` contract must guarantee on every
backend — it is not something each integration can be asked to dedup for itself, because
the integration has no deterministic `edge_id` to dedup *on*.

Where dedup belongs: **the store's edge upsert**, expressed through SCD-2, *not* the
executor's idempotency stage. The idempotency stage keys on `idempotency_key` /
`command_id` (see `has_idempotency_key` in
[`src/trellis/stores/base/event_log.py`](../../src/trellis/stores/base/event_log.py))
and is about *command* replay, not *logical-edge* identity across independently
constructed runs. Two runs that legitimately re-assert the same edge produce different
commands; only the store knows that `(source_id, edge_kind, target_id)` already has a
current version.

The bolt/openCypher backend already does the right thing: `upsert_edge` runs
`OPTIONAL MATCH (s)-[old:EDGE {edge_type}]->(t) WHERE old.valid_to IS NULL`, closes the
prior version, and `coalesce`s the existing `edge_id` forward
([`graph.py:705-736`](../../src/trellis/stores/bolt_opencypher/graph.py)). Re-asserting
an edge updates in place rather than creating a parallel current version — so the
14→28 doubling reported in #195 is a backend that lacks this collapse, not a contract
that permits it.

**Shape.**

- **Canonical edge identity = `(source_id, edge_kind, target_id)` plus any
  qualifiers the backend already version-keys on.** Stated explicitly in the
  `GraphStore` ABC docstring for `upsert_edge` and enforced by the contract suite.
- **A `GraphStoreContractTests` case** asserting that upserting the same triplet twice
  leaves exactly one current edge and stable node + edge counts. This is the
  "re-ingest flow where node and edge counts remain stable" the issue's acceptance
  criteria ask for; adding it to the contract suite means every backend (SQLite,
  Postgres, ArcadeDB, Neo4j) is held to it, per the contract-suite-is-the-spec policy
  in [`adr-plugin-contract.md`](./adr-plugin-contract.md).
- Callers **may** still supply a deterministic identity via edge `properties`, but they
  are not required to — the triplet collapse is the default guarantee.

The within-batch duplicate rejection in
[`graph.py:642-669`](../../src/trellis/stores/bolt_opencypher/graph.py) stays as-is:
duplicates *inside one batch* are a caller error (the OPTIONAL MATCH sees the prior edge
only once), distinct from *across-run* re-assertion which is idempotent.

### 2.3 #196 — knowledge-plane-only executor without EventLog · **Promote to core, scoped to the Knowledge Plane**

**Ruling: Promote a supported builder path to core, with `event_log=None` as an
intentional, documented degradation — *bounded to Knowledge-Plane-only deployments*.**

This one brushes against the "EventLog is authoritative" hard rule, so the scoping is
load-bearing. `MutationExecutor` already *accepts* `event_log=None`: `_emit_event`
returns early when there is no log
([`executor.py:433-434`](../../src/trellis/mutate/executor.py)), and the idempotency
cache already logs `idempotency_cache_evicted_without_event_log` and degrades to
in-memory-only when no log is attached ([`executor.py:365-385`](../../src/trellis/mutate/executor.py)).
The *capability* is there; what is missing is a **sanctioned builder** —
`build_curate_executor` hardwires `registry.operational.event_log`
([`src/trellis/mutate/__init__.py:23-36`](../../src/trellis/mutate/__init__.py)), so a
graph-only consumer either hits a configuration error or hand-rolls
`MutationExecutor(event_log=None, handlers=...)` and bypasses the wiring — which is
exactly the monkey-patch `consumer-kg` was forced into for its first EKS ingest.

**Why this does not violate "EventLog authoritative."** Per
[`adr-planes-and-substrates.md`](./adr-planes-and-substrates.md) §2.1, the EventLog is
an **Operational Plane** store. A deployment that configures *no Operational Plane*
(graph + vector + document only, no trace, no event log) has no authoritative-audit
surface to be authoritative *over* — there is nothing to relax because the plane is
absent by configuration, not bypassed at runtime. The feedback loop, effectiveness
analysis, and promote/demote machinery that depend on `FEEDBACK_RECORDED` /
`MUTATION_EXECUTED` events all live in the Operational Plane and are simply **not
available** in this topology. The hard rule is preserved as: *whenever an EventLog is
configured, every mutation emits to it; the executor never writes around an EventLog
that exists.*

**Shape.**

- A builder, e.g. `build_knowledge_executor(registry)`, that wires the curate handlers
  but passes `event_log=None` **only when** `registry.operational` has no event log
  configured. If an Operational Plane *is* configured, this builder must refuse to drop
  it (no silent loss of audit) — it errors and points the caller at
  `build_curate_executor`.
- `event_log=None` documented as a **supported no-op** for emit, not an
  accident. The degradation note states plainly what is lost: no audit journal, no
  cross-restart idempotency (cache-only), no feedback/effectiveness loop.
- A **caveat on the `LinkCreateHandler`**: it currently emits `LINK_CREATED` directly to
  `registry.operational.event_log` ([`handlers.py:328-337`](../../src/trellis/mutate/handlers.py)),
  *not* via the executor's optional-log path. For a knowledge-only executor this handler
  must route its emit through the same null-tolerant seam (or skip it when no log
  exists) — otherwise the "graph-only" promise breaks on the first link. Resolving this
  is part of the #196 work, called out here so it is not missed.
- A contract test exercising a registry with **no operational stores configured**:
  entity + link mutations succeed, no emit is attempted, idempotency degrades to
  cache-only with the existing warning.

### 2.4 #211 + #215 — the missing-target-node / `allow_dangling` contract · **Core contract; auto-stubbing stays in integration**

These two issues are the same root cause: a curator/ingest path tries to write an edge
to a target table node that has no current version, and the bolt/HTTP graph adapter
refuses (`"... has no current version"`,
[`graph.py:730-735`](../../src/trellis/stores/bolt_opencypher/graph.py)). #215 already
worked around it in `consumer-kg` by **materializing a minimal `uc_table` stub**
before the edge write; #211 enumerates the three candidate contracts.

**Ruling: define ONE explicit core contract for what happens when an edge's referenced
node is missing — and that contract is `allow_dangling`-honoring, not auto-stubbing.**
Auto-stubbing a missing referenced node is **integration ingestion behavior** and stays
in the package.

The one enforced contract, in priority order:

1. **`allow_dangling=False` (default) → fail review-first.** The `LinkCreateHandler` FK
   pre-flight runs, the missing endpoint is reported by side (source/target/both) with
   the IDs attempted, and the executor emits `MUTATION_REJECTED` with `reason=orphan_edge`
   — no partial write ([`handlers.py:281-319`](../../src/trellis/mutate/handlers.py)).
   This is already the behavior; we are blessing it as *the* default and forbidding
   silent materialization underneath it.
2. **`allow_dangling=True` → honor it end-to-end.** When the draft says the edge may
   span outside the current batch, the *entire stack* must let the edge through —
   including the backend store. Today the handler skips its FK pre-flight under
   `allow_dangling`, but the bolt/HTTP `upsert_edge` still hard-requires both endpoints
   to have a current version, so the flag is honored at the handler and **silently
   re-imposed at the store**. That inconsistency is the core bug behind #211/#215.

**Shape.**

- **Make `allow_dangling` a real end-to-end contract.** Either (a) the `GraphStore`
  contract gains an explicit dangling-edge affordance the bolt/HTTP backend implements
  (an edge whose endpoint is not yet current is recorded as pending rather than
  rejected), or (b) the handler, under `allow_dangling`, is responsible for ensuring the
  endpoint exists before the store call. We pick **(b)** for core: core does not invent
  a new "pending edge" graph shape (that is a substantial SCD-2 semantics change), so
  `allow_dangling` at the store boundary means *the caller has guaranteed the endpoints*.
  The `GraphStore` contract documents that `upsert_edge` requires both endpoints to be
  current, full stop — and a `GraphStoreContractTests` case pins the
  `ValueError`/rejection so no backend quietly diverges.
- **Auto-stubbing stays in the integration package.** Synthesizing a `uc_table` stub
  from a parseable three-part table name is Unity-Catalog-specific domain knowledge: how
  to parse the name, which `node_type` and `node_role` the stub gets, whether to set
  `document_ids`, how to avoid clobbering an existing node. None of that belongs in
  core graph mutation. `consumer-kg` keeps `run_source_ingest`'s stub-creation
  pre-pass; it just emits the stub as a *first-class* `ENTITY_CREATE` through the same
  executor (so the stub is audited and idempotent) and then issues the
  `LINK_CREATE` with the endpoint now present — rather than relying on `allow_dangling`
  to paper over a missing node.
- **Retrieval warning (the #211 tail).** When a table anchor is supplied but no current
  graph node exists for that id, retrieval should warn rather than silently return an
  empty neighborhood. This is a core retrieval ergonomic (it is not domain-specific —
  any anchor can be absent), so it lands in core `PackBuilder` as a non-fatal warning on
  the assembled pack.

Net: **the core contract is "honor `allow_dangling`, and `allow_dangling=False` fails
review-first; the store never auto-materializes."** Materializing stubs is a deliberate
*integration* choice made loudly (a logged `ENTITY_CREATE`), never a silent side effect
of an edge write.

### 2.5 #214 — MDX/Docusaurus sanitizer · **Keep in integration; promote only the generic primitive**

**Ruling: Keep the MDX/Docusaurus-specific sanitizer in `consumer-kg`. Promote
nothing MDX-shaped to core now; *consider* later promoting a small, format-agnostic
text-sanitization primitive if (and only if) a second ingestion flow needs it.**

The two fixes in #214 are different in kind:

- **Stripping frontmatter / `import` lines / JSX component blocks (`PageHero`,
  `ServiceFacts`) / admonition markers** is knowledge of one documentation toolchain
  (Docusaurus/MDX). That is textbook domain-specific ingestion and belongs in the
  package, exactly like `DbtManifestExtractor` and `OpenLineageExtractor` live in
  `trellis_workers`, not core ([`adr-graph-ontology.md`](./adr-graph-ontology.md) §2.3
  keeps OpenLineage as a domain extension for the same reason).
- **The possessive bug** (`team's` → `team'?'s` because apostrophes inside words were
  treated as quoted literals) is a bug in the package's *literal-redaction* primitive,
  not an MDX concern. It is fixed in the package.

**Shape / recommendation.** No core change is justified by a single consumer. If a
second graph-enrichment flow later ingests prose that needs the same *bounded,
redacted, summary-truncated* treatment, the reusable piece is **not** "an MDX
sanitizer" but a tiny `trellis.document` helper that takes already-plain text and
applies the format-agnostic steps: redact emails/secrets/long numeric literals, collapse
whitespace, truncate to a token budget. The format-specific *un-chroming* (which markup
to strip) stays per-integration and feeds plain text into that helper. We record this as
the graduation trigger (§4) and do not pre-build it.

### 2.6 #224 — transformation_logic vs query usage · **Keep entity type in integration; the *separation principle* is a core-aligned convention**

**Ruling: Keep `transformation_logic` as an integration-defined entity type. Do not add
it to core well-known defaults.** The open-string type system
([`adr-graph-ontology.md`](./adr-graph-ontology.md) §4.1, §5.2;
[`adr-observation-entity-type.md`](./adr-observation-entity-type.md) §4.1) exists
precisely so integrations define their own types — `transformation_logic`,
`query_pattern`, `usage_summary`, `pipeline_evidence` are all domain-specific evidence
buckets for a data-platform integration.

Apply the [`adr-observation-entity-type.md`](./adr-observation-entity-type.md) litmus
test: a type goes in core only if it is *general-purpose across every domain Trellis
serves*. `Observation` / `Measurement` passed that test (query logs, error rates,
click-through all need them). `transformation_logic` does **not** — it is source-code
construction evidence for pipelines, meaningful only where there is pipeline source to
parse. It stays in the package.

**What is core-aligned, though, is the *principle* the issue is really about:**
*construction evidence (how a dataset is built) must be kept distinct from usage
evidence (how analysts query it), because they have different reliability profiles for
different table populations* (query history is strong for high-traffic BI tables, weak
for intermediate/low-volume tables; source code is the inverse). That distinction maps
cleanly onto the structural-vs-empirical separation already blessed in
[`adr-observation-entity-type.md`](./adr-observation-entity-type.md) §2.4 (both returned,
neither suppresses the other, the consuming agent reconciles).

**Shape / recommendation.**

- `transformation_logic` (entity type + retrieval bucket + "keep separate from
  `query_pattern`" warning) stays in `consumer-kg`.
- The integration is encouraged to express construction-vs-usage as the existing
  empirical-evidence shapes where they fit (`Observation` for narrative construction
  claims, `Measurement` for query-frequency scalars) so the separation rides on core
  semantics rather than a bespoke parallel taxonomy.
- The static-parse false positives (`import ... ` read as a table ref; `os.path.join`
  read as a SQL join) are integration parsing bugs with integration regression tests —
  no core surface.
- The privacy posture (no raw SQL/source bodies in graph properties, bounded sanitized
  fragments, path/hash/commit pointers, redaction) is the integration's responsibility
  and aligns with the classification-inheritance rule in
  [`adr-observation-entity-type.md`](./adr-observation-entity-type.md) §4.5.

## 3. Summary of rulings

| Issue | Ruling | Where the work lands |
|---|---|---|
| #185 draft→command helper | **Promote to core** | `trellis.mutate.from_drafts` + field-fidelity tests |
| #195 edge dedup on re-ingest | **Core contract + bug fix** | `GraphStore` upsert SCD-2 collapse on `(source, kind, target)`; contract test |
| #196 EventLog-less executor | **Promote to core, Knowledge-Plane-scoped** | `build_knowledge_executor`; documented `event_log=None` no-op |
| #211 + #215 missing-target / `allow_dangling` | **Core contract; stubbing stays integration** | Honor `allow_dangling` end-to-end; integration emits stubs as audited `ENTITY_CREATE` |
| #214 MDX sanitizer | **Keep in integration** | package keeps un-chroming; optional generic text helper later |
| #224 transformation_logic vs usage | **Keep type in integration; principle is core-aligned** | integration type; lean on `Observation`/`Measurement` separation |

## 4. Cross-cutting principle — when does an `consumer-kg` behavior graduate to core?

The seven rulings follow one test. A behavior discovered in an integration package
**graduates to core** only when *all* of the following hold:

1. **It is about a Trellis-owned schema or invariant, not a domain.** Preserving
   `node_role`/`generation_spec` across a draft→command conversion (#185), the identity
   of an edge (#195), and the meaning of `allow_dangling` (#211/#215) are all about
   *Trellis's own contracts*. Parsing Databricks notebooks (#224) or Docusaurus MDX
   (#214) is about someone else's format.
2. **It is general-purpose across domains.** The
   [`adr-observation-entity-type.md`](./adr-observation-entity-type.md) test: would a
   non-data-platform adopter need it? Edge dedup — yes. A `uc_table` stub —
   no.
3. **Leaving it in the package re-creates a correctness footgun for the next
   integration.** #185 is the canonical case: every consumer re-deriving the mapping
   re-introduces the silent field-drop. If the only cost of staying in the package is
   "this one integration carries some code," that is not a graduation reason.
4. **It does not require core to encode a domain assumption.** Auto-stubbing needs
   three-part-name parsing and a choice of `node_type`; promoting it would smuggle Unity
   Catalog semantics into core graph mutation. Failed test 4 ⇒ stays integration.

Conversely, a behavior **stays in the integration package** when it encodes a specific
format, vocabulary, or product (MDX, dbt, Databricks, Unity Catalog), even if a clean
*generic primitive* could be carved out later. We carve the primitive only when a
**second** consumer needs it (the #214 trigger) — the plugin contract's "don't pay the
consumer-wiring cost until the first consumer appears"
([`adr-plugin-contract.md`](./adr-plugin-contract.md)) applied to extraction helpers.

The split is the same one [`adr-plugin-contract.md`](./adr-plugin-contract.md) already
draws between data-only extensions (new types, new properties — client-side) and runtime
extensions (code that must run in-process). This ADR adds the extraction→mutation seam
to that picture: **core owns the draft schemas, the command mapping, the executor, and
the graph-write contract; integrations own how to turn their domain into drafts.**

## 5. Acceptance criteria

A future implementation of this ADR is complete when:

- **#185** — A core conversion helper round-trips `node_role`, `generation_spec`,
  `allow_dangling`, `properties`, and `entity_id` from drafts into `ENTITY_CREATE` /
  `LINK_CREATE` commands; a batch helper orders entity creates before dependent edge
  creates; tests fail if a new draft field is not threaded through. `consumer-kg`
  deletes its local `_draft_to_command()`.
- **#195** — The `GraphStore` contract documents `(source_id, edge_kind, target_id)` as
  canonical edge identity; a `GraphStoreContractTests` re-ingest case keeps node and
  edge counts stable across a repeated upsert on every backend.
- **#196** — A sanctioned `build_knowledge_executor`-style path exists; `event_log=None`
  is documented as a supported emit no-op scoped to Knowledge-Plane-only deployments;
  the `LinkCreateHandler` emit no longer hard-requires an Operational EventLog; a
  contract test runs mutations with no operational stores configured.
- **#211 + #215** — `allow_dangling` is honored consistently across handler and store
  (no silent re-imposition); `allow_dangling=False` fails review-first with a
  per-endpoint `orphan_edge` rejection and no partial write; retrieval warns when an
  anchor has no current node; the `uc_table` stub pre-pass remains in
  `consumer-kg` and emits stubs as audited `ENTITY_CREATE` commands.
- **#214** — MDX/Docusaurus un-chroming and the possessive-redaction fix remain in
  `consumer-kg`; the §4 graduation trigger for a generic text-sanitization
  helper is recorded but not pre-built.
- **#224** — `transformation_logic` remains an integration-defined type; core
  well-known defaults are unchanged; the construction-vs-usage separation is documented
  as expressible via core `Observation` / `Measurement` semantics.

## 6. Non-goals

- **No new graph shape for dangling edges.** This ADR explicitly declines to add a
  "pending edge" SCD-2 state to core (§2.4). `allow_dangling` means the caller
  guarantees the endpoints; the store still requires current endpoints.
- **No relaxation of "EventLog authoritative" inside a configured Operational Plane.**
  #196 is bounded to topologies with *no* Operational Plane (§2.3). Whenever an EventLog
  exists, every mutation emits to it.
- **No promotion of domain extractors, types, or sanitizers to core.** MDX (#214),
  `transformation_logic` (#224), and `uc_table` stub synthesis (#215) stay in the
  integration package.
- **No on-disk migration and no change to existing draft/command/event schemas’ wire
  shape.** The #185 helper reads the schemas as they are; it adds a conversion module,
  not a schema version bump.
- **No implementation in this document.** This is a Proposed design; the rulings here
  authorize the work, they do not perform it.
