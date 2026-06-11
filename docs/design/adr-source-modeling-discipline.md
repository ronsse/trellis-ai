# ADR: Source Modeling Discipline — Columns as the Worked Example

**Status:** Proposed (Track G Wave 1)
**Date:** 2026-05-18 (reconciled 2026-06-11 with [`adr-column-leaf-modeling-guardrails.md`](./adr-column-leaf-modeling-guardrails.md))
**Deciders:** Trellis core
**Policy authority:** [`./adr-column-leaf-modeling-guardrails.md`](./adr-column-leaf-modeling-guardrails.md) is THE columns-as-properties policy (default rule, exception criteria, node requirements, maintenance-cost argument). This ADR is its implementation companion: it records the gated follow-up decision that the guardrails ADR §8 anticipates. The guardrails ADR committed to a docs-only guardrail and explicitly left the advisory-warning options 2–4 as "a follow-up plan; none are claimed as implemented", each gated "on its own decision". **This ADR is that decision for the advisory-warning path** — it authorizes the warn-only, opt-in-flagged validator at extraction time (Track G G1), plus the searchability recipe (`column_names` + the DSL `contains` operator) and the no-name-match lineage rule.
**Related:**
- [`./adr-column-leaf-modeling-guardrails.md`](./adr-column-leaf-modeling-guardrails.md) — resolves [#221](https://github.com/ronsse/trellis-ai/issues/221); the policy authority this ADR implements. Its §4 exception criteria and §5 node requirements govern when column nodes are justified; this ADR's two-signal opt-in (§2.5) is the machine-checkable surface for that exception.
- [`./adr-graph-ontology.md`](./adr-graph-ontology.md) — canonical vocabulary; `NodeRole` (`structural` / `semantic` / `curated`) is the role-axis this ADR governs at extraction time.
- [`./adr-canonical-graph-layer.md`](./adr-canonical-graph-layer.md) — §4 (operator scope) and §8.2: nested-path traversal beyond one level is deferred until a consumer pushes; that deferral still holds. The one-level list-membership `contains` operator (Track G G3, [#188](https://github.com/ronsse/trellis-ai/pull/188)) is the minimal extension §2.2 relies on instead.
- [`./adr-planes-and-substrates.md`](./adr-planes-and-substrates.md) — Knowledge Plane shape; this ADR pins extractor behaviour upstream of the substrate decisions.
- [`../agent-guide/modeling-guide.md`](../agent-guide/modeling-guide.md) — the "Column and leaf metadata policy" section (line 227, added by the guardrails ADR), the Schema explosion anti-pattern (lines 344-354), and Worked Example 1: Database catalog ingestion (lines 415-553).
- [`../agent-guide/source-modeling-cookbook.md`](../agent-guide/source-modeling-cookbook.md) — Recipe 5 (Unity Catalog) line 445 already says "Do NOT emit a node per column."
- [`../../src/trellis_sdk/extract/__init__.py`](../../src/trellis_sdk/extract/__init__.py) — lines 9-46: the SDK extractor example that already recommends columns-as-property.
- [`../../src/trellis/extract/json_rules.py`](../../src/trellis/extract/json_rules.py) — `EntityRule` shape; the unguarded surface this ADR governs.
- [`../../src/trellis/schemas/well_known.py`](../../src/trellis/schemas/well_known.py) — canonical registry; the blocklist constant lands here.

---

## 1. Context

### 1.1 The gap

The modeling guide and the SDK extractor docstring already say the right thing: columns are properties on the parent `Dataset` (or `UC_TABLE`) node, not nodes themselves. The four-question test in [`modeling-guide.md`](../agent-guide/modeling-guide.md) gives "zero yesses" for `UC_COLUMN`; the cookbook's UC recipe states the rule outright at line 445. None of this is enforced. `JSONRulesExtractor` ([`src/trellis/extract/json_rules.py`](../../src/trellis/extract/json_rules.py)) accepts an `EntityRule` with `entity_type="column"` and a nested wildcard path (`["tables", "*", "columns", "*"]`) and emits one `EntityDraft` per column, with `node_role` defaulting to `SEMANTIC`. Several tests in [`tests/unit/extract/test_json_rules.py`](../../tests/unit/extract/test_json_rules.py) (the column-walking test at line 87, the field-reference column-to-table edge test at line 219, the ancestor-edge test at line 369, the missing-ancestor test at line 466, the canonical-alias columns test around line 728) exercise that path. The extractor does not warn, the rule bundle does not surface a hint, and there is no canonical signal that says "you have crossed into the schema-explosion anti-pattern."

### 1.2 What the user flagged

The trigger was a Unity Catalog extractor proposal. Quoting the user concern that motivated this ADR: columns should be searchable, but they should not be nodes — making them nodes "will introduce too many too many changes to the graph, make it harder to maintain" — and importantly, "lineage between, like, columns isn't necessarily, um, name related." Two distinct concerns:

1. **Graph inflation** — a 10K-table catalog becomes a 500K+ node graph, with most leaves being structural plumbing. The cost is real (SCD-2 history per column, retrieval token budget burnt on columns instead of tables, traversal fanout).
2. **Unreliable name-match lineage** — "`table_a.user_id` flows to `table_b.user_id` because the column names match" is a tempting but wrong heuristic. The false-positive rate is unbounded; the same column name across two tables more often coincides than co-references.

### 1.3 Why preventive, not curative

No Unity Catalog extractor exists in the codebase today. `trellis_workers.extract` ships `DbtManifestExtractor` and `OpenLineageExtractor`; neither emits column nodes. The SDK extractor example already recommends the right shape. **The footgun is unbuilt.** The policy itself is codified in [`adr-column-leaf-modeling-guardrails.md`](./adr-column-leaf-modeling-guardrails.md); what that ADR deliberately stopped short of is enforcement — it shipped the docs-only guardrail and left advisory warnings as a gated follow-up (its §8, options 2–4). This ADR takes that gate: it decides the advisory-warning path before the first UC extractor lands, so the wrong shape, if attempted, produces a warning that points back at the policy.

### 1.4 What this ADR is *not*

- Not the columns-as-properties policy. [`adr-column-leaf-modeling-guardrails.md`](./adr-column-leaf-modeling-guardrails.md) owns the default rule, the exception criteria (its §4), the node requirements (its §5), and the maintenance-cost argument (its §6). Where the two documents could be read to disagree, the guardrails ADR wins; this ADR specifies mechanisms, not policy.
- Not a UC extractor specification — that belongs in `trellis_workers.extract` or a customer plugin, gated on this ADR.
- Not a re-litigation of the `node_role=structural` carve-out documented in the modeling guide and formalized as the guardrails ADR's exception criteria. Columns that meet those criteria still earn node status; the existing escape hatch is preserved by §2.5.
- Not a rejection mechanism. The validator in G1 warns; it does not refuse. POC-stage discipline is to make the wrong shape visible, not unbuildable — the same advisory-only stance the guardrails ADR §8 requires ("Advisory only — never a rejection").

---

## 2. Decision

The codified discipline is six rules, implementing the default rule of [`adr-column-leaf-modeling-guardrails.md`](./adr-column-leaf-modeling-guardrails.md) §2 at extraction time. Columns are the canonical worked example; the rules generalise to other structural-leaf anti-patterns the project encounters in the future.

### 2.1 Default rule — columns are properties, not nodes

Extractors targeting catalog-shaped sources emit one entity per table-equivalent (`Dataset` / `UC_TABLE` / `dbt_model`) and place columns into the table's `properties` map in two forms:

- `properties.columns` — a structured list of column records `[{"name": ..., "type": ..., "nullable": ..., "comment": ..., "tags": [...]}, ...]`. This is the canonical column metadata payload; consumers read from here.
- `properties.column_names` — a flat list of strings `["user_id", "order_total", ...]` derived from `properties.columns`. This duplicates a field already present in the structured list and exists for one reason only: exact-match search via the canonical graph DSL (see §2.2).

The shape mirrors what [`modeling-guide.md`](../agent-guide/modeling-guide.md) §"Worked example 1: Database catalog ingestion" already shows in code (lines 460-509) and what the SDK example at [`src/trellis_sdk/extract/__init__.py`](../../src/trellis_sdk/extract/__init__.py) lines 24-32 already recommends. This ADR makes the shape explicit and gives it a name: **columns-as-property**.

### 2.2 Searchability — denormalized `column_names` + the `contains` operator

The canonical query DSL ([`src/trellis/stores/base/graph_query.py`](../../src/trellis/stores/base/graph_query.py)) supports `eq`, `in`, `exists`, `lt`/`lte`/`gt`/`gte`, and `contains` over one-level property paths (`properties.<key>`). Nested-path traversal beyond one level — `properties.columns[*].name` — is explicitly out of scope per [`adr-canonical-graph-layer.md`](./adr-canonical-graph-layer.md) §4 and §8.2, deferred until a consumer pushes. **This ADR does not push on nested paths.**

Instead, the extractor pays a small denormalization cost: every column name also lives in a flat `properties.column_names` list. Agents searching for `user_id` use the `contains` operator (Track G G3, landed via [#188](https://github.com/ronsse/trellis-ai/pull/188)):

```python
FilterClause("properties.column_names", "contains", "user_id")
```

`contains` asks list-membership — "the scalar value is an element of the list-typed property at `field`" — which is exactly the shape this query needs. Note the inversion relative to `in`: `in` asks "is the property's *scalar* value in this tuple of scalars?", so `FilterClause("properties.column_names", "in", ("user_id",))` would compare the whole list against the scalar `"user_id"` and match nothing. An earlier draft of this ADR made precisely that mistake; the `contains` operator is the correct (and now landed) mechanism. If the property is scalar, missing, or any non-list value, the predicate evaluates `False` for that row — no exception. The operator works on every backend the DSL supports (SQLite via a `json_each` EXISTS subquery, Postgres via JSONB `@>` containment with an array-type guard, the Bolt-path backends via the client-side predicate over the JSON properties payload); the `GraphStoreContractTests` suite covers it. No nested-path compiler logic; no new ABC method.

Vector search over column-name embeddings is a complement, not a substitute. Exact-match on `column_names` answers "find every table that has a column called `user_id`"; semantic search answers "find tables that probably have a user-identifier column." Both are useful; this ADR pins exact-match because the user concern was structured searchability, not similarity.

### 2.3 Enforcement — warn, with an opt-in flag

This is the gated follow-up decision [`adr-column-leaf-modeling-guardrails.md`](./adr-column-leaf-modeling-guardrails.md) §8 anticipated: an advisory warning in the spirit of its options 2–4, applied at the extraction validator stage, advisory-only as that ADR requires ("never a rejection"). Track G G1 ([#187](https://github.com/ronsse/trellis-ai/pull/187)) implements it.

G1 adds `allow_structural_leaf: bool = False` to both `EntityRule` (in [`src/trellis/extract/json_rules.py`](../../src/trellis/extract/json_rules.py)) and `EntityDraft` (in [`src/trellis/schemas/extraction.py`](../../src/trellis/schemas/extraction.py)). When the flag is `False` and the rule's (or draft's) `entity_type` matches the blocklist below, the extractor emits a structlog `WARNING` at the validator stage. The warning carries the rule name, the offending `entity_type`, and a link back to this ADR.

The validator **does not raise**. It does not reject the entity. It does not block the mutation. The shape continues to extract and persist; the warning is the only signal. Rationale in §3.2.

Operators with a legitimate need (the guardrails ADR's exception criteria, surfaced through §2.5) opt out by setting `allow_structural_leaf=True` on the rule. The flag is the explicit "I have read the policy and I have earned the exception" marker.

### 2.4 Blocklist — column variants only, in v1

The constant lives at [`src/trellis/schemas/well_known.py`](../../src/trellis/schemas/well_known.py), in the same module as the canonical entity-type registry:

```python
ENTITY_TYPE_ANTI_PATTERNS: Final[frozenset[str]] = frozenset(
    {"Column", "column", "TableColumn", "table_column"}
)
```

Four spellings cover the casing variants an extractor author is likely to reach for. The helper `validate_entity_type_not_anti_pattern(entity_type, *, allow_structural_leaf) -> warnings` is the lookup point; G1 owns both the constant and the helper.

The blocklist is deliberately tight. The user concern that motivated this ADR is column nodes specifically; the modeling guide names function parameters, file lines, and config keys as adjacent anti-patterns; the principle generalises. **The blocklist does not generalise speculatively.** Future anti-patterns extend the set via ADR amendment — same gating policy as `adr-tag-vocabulary-split.md` uses for reserved tag namespaces. Adding `Parameter` / `FileLine` / `ConfigKey` to the blocklist without observed misuse is the speculative-design failure mode this project has rejected before.

### 2.5 Exception mechanics — the two-signal opt-in

When column nodes ARE justified is decided by [`adr-column-leaf-modeling-guardrails.md`](./adr-column-leaf-modeling-guardrails.md): its §4 lists the five exception criteria (traversal requirement, cross-parent queries, independent evidence/policy, independent lifecycle, regulated/high-risk field) and its §5 lists the requirements an admitted column node must satisfy (`node_role="structural"`, excluded from default retrieval, source identifiers + freshness, a retention/compaction strategy). This ADR does not define a competing exception scheme — it defines the **machine-checkable surface** for the guardrails ADR's exception. The modeling-guide regulated-column carve-out ([`modeling-guide.md`](../agent-guide/modeling-guide.md) lines 522-552) is the same exception, criterion 5. The signal that an operator has invoked the exception is the **combination** of:

1. `allow_structural_leaf=True` on the `EntityRule` (or `EntityDraft`).
2. `node_role=NodeRole.STRUCTURAL` on the same rule.

Either flag alone is not enough. `allow_structural_leaf=True` with `node_role=SEMANTIC` should still warn (the operator opted out of the policy but did not declare the column structural — almost certainly a mistake, and a violation of the guardrails ADR §5 requirement that admitted column nodes are never `semantic`). `node_role=STRUCTURAL` with `allow_structural_leaf=False` should also still warn (the operator declared the column structural but did not assert they have read the policy). The combination is the explicit two-signal acknowledgement that one of the guardrails ADR's §4 exception criteria applies. The flags assert the *role* half of the §5 requirements mechanically; the remaining §5 requirements (retrieval exclusion, source identifiers + freshness, retention strategy) stay the operator's responsibility — the validator cannot check them at extraction time.

The validator MAY emit a softer informational log when both signals are set, confirming the exception is recognised. G1 owns that policy choice.

### 2.6 Column-level lineage policy — no name-match

Column-level lineage edges between tables are governed by one rule: **the extractor must possess stable column identifiers — `(table_id, column_position)` or `(table_id, column_name)` where column names are guaranteed unique within the table AND the extractor has access to a stable mapping that survives source schema changes.** Without such a mapping, column-level lineage at the cross-table granularity is forbidden.

**Name-match across tables is forbidden.** "`table_a.user_id` flows from `table_b.user_id` because they share a name" is the canonical false-positive shape; you cannot distinguish a real co-reference from a coincidental name collision without source-system evidence. The unbounded false-positive rate is the load-bearing argument — see §3.4.

For v1, column-level lineage stays as **table-level edges with column-pair annotations** on the edge `properties`:

```python
# the table-to-table lineage edge carries the column mapping inline
{
    "edge_kind": "wasDerivedFrom",
    "source_id": "dataset:.../fct_orders",
    "target_id": "dataset:.../raw_orders",
    "properties": {
        "column_pairs": [
            {"from": "user_id", "to": "user_id"},
            {"from": "amount_cents", "to": "amount"},
        ],
    },
}
```

The column-pair annotation captures the per-column dependency without minting per-column nodes and without minting per-column edges. Agents inspecting the lineage edge see the column mapping; the graph cardinality stays at the table level.

Separate column nodes connected by column-to-column lineage edges are **not forbidden** — that shape is governed by [`adr-column-leaf-modeling-guardrails.md`](./adr-column-leaf-modeling-guardrails.md), which explicitly allows structural `Column` nodes when a genuine traversal requirement exists (its §2.1 placement table, §4 criterion 1, and the enterprise-graph (EG) reconciliation in §7). An extractor whose workload meets one of the §4 exception criteria may mint column nodes and column-level lineage edges, provided every admitted node satisfies the §5 requirements (`node_role=structural`, excluded from default retrieval, source identifiers + freshness, a retention strategy) and the opt-in is declared through the two-signal mechanism in §2.5. What this ADR adds on top is the *default* (table-level edges with `column_pairs` annotations, for extractors that do not meet the exception criteria) and the *evidence rule*: even under the exception, **name-match is never the evidence** — column-level edges require source-system evidence and stable identifiers regardless of whether the endpoints are tables or column nodes.

This rule binds extractors. It does not bind retrieval; downstream consumers reading the `column_pairs` annotation are free to render column-level lineage in their UI.

---

## 3. Why this shape

### 3.1 Why the denormalized `column_names` list

Three alternatives were considered for the searchability mechanism: (a) extend the canonical DSL to support nested-path traversal (`properties.columns[*].name`); (b) lean on vector search over column-name embeddings; (c) denormalize a flat list and query it with the one-level `contains` operator. The denormalization is the cheapest move on every axis:

- The nested-path DSL extension is heavy: every backend compiler gains a new path-walk codepath, the contract suite gains operator/path combinations, and the operator-vocabulary commitment is permanent. [`adr-canonical-graph-layer.md`](./adr-canonical-graph-layer.md) §8.2 explicitly defers nested-path traversal "until a consumer pushes." This ADR does not push on it. The `contains` operator (Track G G3, [#188](https://github.com/ronsse/trellis-ai/pull/188)) is the deliberately minimal extension instead: one list-membership operator over the existing one-level `properties.<key>` paths — no path-walking, no nested traversal.
- Vector search alone fails exact-match queries that the user concern centred on ("find every table with a column called `user_id`").
- Denormalization costs roughly the size of a column-name list per table, in JSON, on disk. For a 10K-table catalog with average 20 columns per table that is ~200K extra strings — negligible against the alternative of 500K extra graph nodes plus 500K edges plus 500K SCD-2 histories.

The duplication between `properties.columns[*].name` and `properties.column_names` is the explicit price. Both fields stay synchronised at extraction time; agents querying via the DSL read `column_names` with `contains`, agents reading structured column metadata read `columns`.

### 3.2 Why warn, not reject

Rejecting an entity at the validator stage breaks the four existing tests in [`tests/unit/extract/test_json_rules.py`](../../tests/unit/extract/test_json_rules.py) that exercise column-typed entities (lines 87, 200, 219, 369 — see also the missing-ancestor test at 466 and the column-walking test at 728). The tests cover the path-walking and edge-emission machinery, not the column-as-node anti-pattern; rejecting columns at the validator would force the tests to switch to a synthetic entity type, which loses test coverage on the real shape extractors hit.

More importantly, the project is POC-stage. The discipline this ADR codifies is novel for the codebase; the blocklist may evolve as we learn what real extractors hit. A warning surfaces the problem to the operator without breaking their build; rejection makes the warning impossible to ignore but also impossible to roll forward against without code changes. The opt-in flag in §2.3 *is* the mechanism for the guardrails ADR's §4 exception criteria — there is no need to add a second escape hatch in the form of "the validator can be turned off." Warn + flag is the smallest mechanism that does the job.

A future ADR amendment can promote the warning to a rejection once the discipline is established and the blocklist is stable. The path from warn to reject is shorter than the path from reject to warn.

### 3.3 Why the blocklist is tight

The user concern was columns. The modeling guide names columns first, function parameters second, file lines third — but the only signal we have is column misuse. Adding `Parameter` / `FileLine` / `ConfigKey` to the blocklist now would catch hypothetical misuse the project has not seen; if a customer ships a code-search extractor that legitimately models public-API functions as `node_role=structural` (as the modeling guide line 641 carves out), an over-broad blocklist would warn on the legitimate shape. The blocklist starts at column variants and grows by amendment when a new anti-pattern is observed in the wild.

### 3.4 Why no name-match lineage

The unbounded false-positive rate is the load-bearing argument, and it does not soften with sampling or filtering. Two tables in the same warehouse legitimately share `user_id` columns when one is derived from the other; two tables in different domains legitimately share `user_id` columns because both domains track users independently. From a column name alone you cannot tell which case applies. The cost of being wrong is structural: a false lineage edge propagates through every traversal that touches it.

Source-system evidence — a `SELECT user_id FROM raw_orders` in the query log, a dbt manifest column-level `lineage` block, an OpenLineage `columnLineage` facet — distinguishes the real case from the coincidence. Until the extractor has that evidence, the only honest answer is "no edge." The table-level edge with `column_pairs` annotation (§2.6) preserves the information the extractor *does* have without claiming evidence it does not.

The forbid-by-default stance is a one-way commitment: opening name-match lineage later requires an ADR. The reverse (closing a previously-allowed name-match path) would break consumers, so the project errs on the side of forbidding it now.

---

## 4. Guardrails — what this ADR does *not* do

- **The validator does not reject.** It emits a structlog WARNING with the rule context and a pointer to this ADR. The entity continues to extract and persist.
- **The validator does not override the policy's exception path.** Column nodes admitted under [`adr-column-leaf-modeling-guardrails.md`](./adr-column-leaf-modeling-guardrails.md) §4 remain a fully supported shape — the open-string type contract is untouched. The two-signal opt-in (§2.5) is how an extractor declares the exception; it does not narrow it.
- **The validator does not bind edges.** Edge kinds, edge directions, and edge property shapes are governed by [`adr-graph-ontology.md`](./adr-graph-ontology.md) and (when [`adr-graph-shape-constraints.md`](./adr-graph-shape-constraints.md) lands) by SHACL-flavoured shape rules. This ADR is the entity-shape policy at extraction time; it does not duplicate edge-shape governance.
- **The validator does not run at read time.** Existing column nodes — if a customer has them today, or if a sanctioned-exception extractor minted them — keep working at query time. There is no read-path filter, no retrieval-time degradation, no migration.
- **The column-lineage policy does not forbid in-table column-level lineage.** If an extractor has stable `(table_id, column_position)` identifiers for both ends of an in-table dependency and a clear definition of what the edge means, the policy is silent — only the cross-table name-match case is forbidden. Cross-table column-level lineage is allowed only when the extractor possesses stable identifiers; see §2.6.
- **The blocklist does not generalise speculatively.** v1 covers column variants. Future anti-patterns extend the blocklist via ADR amendment, not via prose updates.

---

## 5. Consequences

### 5.1 What this enables

- **The first Unity Catalog extractor lands with the right shape.** When someone writes `trellis_unity_catalog.reader` (or an internal equivalent), the policy is visible in `well_known.py` and the SDK example, and the warning catches the boundary mistakes.
- **A clean target for a future relationship-discoverer skill.** The Phase F skill harness ships a graph-skill abstraction; a future "find table relationships" skill knows that table relationships live at the table level with column-pair annotations, not in a separate column-node layer.
- **Pre-empts the schema-explosion debugging cost.** The modeling guide already documents the pattern; this ADR moves the discipline from "you should read the docs" to "your extractor will warn at you."

### 5.2 What this does not change

- **No existing extractor changes.** `DbtManifestExtractor` and `OpenLineageExtractor` do not emit column nodes today; they continue not to.
- **No existing data migrates.** Customers with `entity_type="column"` rows in their graph keep them. The warning fires at extraction time, not at read time.
- **No new ABC method, no nested-path compiler logic, no new policy gate stage.** The `contains` operator this ADR relies on landed separately as Track G G3 ([#188](https://github.com/ronsse/trellis-ai/pull/188)); the validator is one function in G1; the constant is one frozenset in `well_known.py`.

### 5.3 What this costs

- **Modest denormalization.** `properties.column_names` duplicates a field already present in `properties.columns`. The cost is a small JSON list per table; the per-deployment storage impact is well under the cost of one column node per column.
- **An ADR amendment is required to grow the blocklist.** Same policy as `adr-tag-vocabulary-split.md`; same lightweight overhead.
- **Operators must remember the two-signal opt-in.** Setting only `allow_structural_leaf=True` (without `node_role=STRUCTURAL`) or only `node_role=STRUCTURAL` (without `allow_structural_leaf=True`) still warns. The two-signal requirement is intentional friction.

---

## 6. Alternatives considered

### 6.1 Vector search over column-name embeddings, instead of denormalization

Rejected. Vector search answers "find tables with user-identifier-like columns"; the user concern is "find every table with a column called `user_id` so an agent can plan a join." The exact-match query is not soft-similarity, and the denormalized list answers it on the existing DSL with no new machinery. Vector search remains useful as a complement — but not as the primary mechanism.

### 6.2 Hard reject on column entity types

Rejected. POC-stage, breaks four existing tests in `test_json_rules.py` (lines 87, 200, 219, 369; plus the related cases at 466 and 728), and leaves the exception path without a clean opt-in. The warn + opt-in flag combination achieves the same discipline while preserving extractor behaviour. [`adr-column-leaf-modeling-guardrails.md`](./adr-column-leaf-modeling-guardrails.md) §3 rejected write-time enforcement on the same grounds ("too blunt ... enforcement belongs in advisory tooling, not the hot path"); this ADR honours that constraint. A future amendment can promote warn → reject once the blocklist is stable.

### 6.3 Stop at docs-only — rely on the guardrails ADR's guide section alone

Rejected as an end state, though it was the deliberate *first* step: [`adr-column-leaf-modeling-guardrails.md`](./adr-column-leaf-modeling-guardrails.md) committed to the docs-only guardrail as its deliverable and explicitly deferred advisory tooling to a follow-up decision. The modeling guide says "do not create column nodes"; the cookbook says it at line 445; the SDK example shows the right shape at lines 24-32. The discipline survives in documentation. What it does not survive is the boundary case — an extractor author who skims the docs, picks `EntityRule(entity_type="column", path=["tables", "*", "columns", "*"])` because that path-shape works, and ships. Docs-only does not catch that. A warning at validator time does — which is exactly why the guardrails ADR left the advisory-warning option open rather than closing it.

### 6.4 Extend the canonical DSL — nested-path traversal vs. `contains`

Two different extensions hide under "extend the DSL", and they get opposite verdicts:

- **Nested-path traversal (`properties.columns[*].name`) — rejected.** [`adr-canonical-graph-layer.md`](./adr-canonical-graph-layer.md) §4 / §8.2 defers nested-path traversal until a consumer pushes; that deferral was deliberate. Reopening it for one consumer would commit a permanent path-walk codepath in every backend compiler and a combinatorial growth of the contract suite.
- **One-level list-membership (`contains`) — accepted, and landed.** Track G G3 ([#188](https://github.com/ronsse/trellis-ai/pull/188)) added `contains` to the canonical operator vocabulary in [`graph_query.py`](../../src/trellis/stores/base/graph_query.py): "the scalar value is a member of the list-typed property at the given path." An earlier draft of this ADR claimed no DSL extension was needed because `in` could serve — that was wrong (`in` compares the property's *scalar* value against a tuple of scalars; it never matches a list-typed property), and the minimal correct extension was judged worth its cost. `contains` stays within one-level `properties.<key>` paths, so the nested-path deferral above is undisturbed.

Denormalizing `column_names` plus `contains` solves the searchability requirement in eight lines of extractor code per source plus one narrowly-scoped operator.

### 6.5 Forbid column-level lineage altogether (no `column_pairs` annotation)

Rejected. Some extractors — dbt with `columns.lineage`, OpenLineage with `columnLineage` — have stable column-level evidence the operator legitimately wants to preserve. The `column_pairs` annotation on the table-level edge captures the information without minting node-level structure for it. The *default* shape is "table-level edge with the column mapping inline"; "two column nodes connected by a lineage edge" is reserved for extractors that meet the [`adr-column-leaf-modeling-guardrails.md`](./adr-column-leaf-modeling-guardrails.md) §4 exception criteria (see §2.6). This preserves the evidence the extractor has while keeping the graph cardinality at the table level for the common case.

---

## 7. References

- [`./adr-column-leaf-modeling-guardrails.md`](./adr-column-leaf-modeling-guardrails.md) — **the policy authority.** Default rule (§2), exception criteria (§4), node requirements (§5), maintenance-cost argument (§6), EG reconciliation (§7), and the §8 follow-up plan this ADR's advisory-warning decision takes the gate on.
- [`./adr-graph-ontology.md`](./adr-graph-ontology.md) — `NodeRole` is the role axis this ADR operates on; the `structural` value is the legitimate-exception signal.
- [`./adr-canonical-graph-layer.md`](./adr-canonical-graph-layer.md) §4, §8.2 — nested-path traversal deferral. The reason `column_names` is denormalised and queried with the one-level `contains` operator ([`src/trellis/stores/base/graph_query.py`](../../src/trellis/stores/base/graph_query.py), Track G G3 / [#188](https://github.com/ronsse/trellis-ai/pull/188)) instead of a nested path.
- [`./adr-planes-and-substrates.md`](./adr-planes-and-substrates.md) — Knowledge Plane partitioning; extractors sit upstream of the substrate decisions this ADR pins.
- [`./adr-graph-shape-constraints.md`](./adr-graph-shape-constraints.md) — shape rules for canonical types; this ADR governs entity *creation* shape, that one governs *post-write* shape validation. Complementary, not overlapping.
- [`./adr-tag-vocabulary-split.md`](./adr-tag-vocabulary-split.md) — precedent for the "small core, gated growth" pattern that §2.4 follows.
- [`../agent-guide/modeling-guide.md`](../agent-guide/modeling-guide.md) — the four-question test (lines 47-65), the "Column and leaf metadata policy" section (line 227), the Schema explosion anti-pattern (lines 344-354), Worked Example 1 (lines 415-553) with the regulated-column exception (lines 522-552).
- [`../agent-guide/source-modeling-cookbook.md`](../agent-guide/source-modeling-cookbook.md) — Recipe 5 Unity Catalog (line 445: "Do NOT emit a node per column"); G2 ships the column-search cookbook addendum.
- [`../../src/trellis_sdk/extract/__init__.py`](../../src/trellis_sdk/extract/__init__.py) lines 24-32 — the SDK example already showing columns-as-property; G2 ships the parallel `column_names` denormalization.
- [`../../src/trellis/extract/json_rules.py`](../../src/trellis/extract/json_rules.py) — `EntityRule`; G1 adds `allow_structural_leaf`.
- [`../../src/trellis/schemas/extraction.py`](../../src/trellis/schemas/extraction.py) — `EntityDraft`; G1 adds `allow_structural_leaf`.
- [`../../src/trellis/schemas/well_known.py`](../../src/trellis/schemas/well_known.py) — G1 adds `ENTITY_TYPE_ANTI_PATTERNS` constant and `validate_entity_type_not_anti_pattern()` helper.
- [`../../tests/unit/extract/test_json_rules.py`](../../tests/unit/extract/test_json_rules.py) — column-typed tests at lines 87, 200, 219, 369, 466, 728; G1 updates these to pass `allow_structural_leaf=True` so the warning surfaces the policy without breaking the path-walking coverage.
