# ADR: Alias Resolution — `(source_system, raw_id)` Uniqueness Invariant

**Status:** Proposed
**Date:** 2026-05-05
**Deciders:** Trellis core
**Related:**
- [`./adr-canonical-graph-layer.md`](./adr-canonical-graph-layer.md) — Graph DSL + per-backend compilers; the alias surface is one of the things the contract suite covers
- [`../../src/trellis/stores/base/graph.py`](../../src/trellis/stores/base/graph.py) — `upsert_alias` / `resolve_alias` / `get_aliases` ABC
- [`../../src/trellis/stores/sqlite/graph.py`](../../src/trellis/stores/sqlite/graph.py) — reference SCD-2 implementation (~line 540)
- [`../../src/trellis_cli/stores.py`](../../src/trellis_cli/stores.py) — `LOCAL_SOURCE_SYSTEM = "local"`
- [`../../src/trellis_cli/retrieve.py`](../../src/trellis_cli/retrieve.py) — `retrieve entity` falls back through `resolve_alias("local", entity_id)` when exact-id lookup misses

---

## 1. Context

`GraphStore.upsert_alias(entity_id, source_system, raw_id, ...)` lets external identifiers map onto canonical entity IDs. The schema is SCD-2: each `(source_system, raw_id)` pair has a single open row at any time, and re-upserting closes the prior version and inserts a new one. The pair `(source_system, raw_id)` is the natural key.

PR [#99](https://github.com/ronsse/trellis-ai/pull/99) introduced a CLI convention on top of this:

- **`LOCAL_SOURCE_SYSTEM = "local"`** — a sentinel `source_system` value reserved for aliases minted *inside* a Trellis instance (as opposed to "github", "dbt", external system identifiers).
- **Demo loader** seeds a `("local", entity.name)` alias for every entity it creates, so the README quickstart's `trellis retrieve entity user-api` resolves a memorable label to the underlying ULID.
- **`retrieve entity`** falls back to `resolve_alias("local", arg)` when the exact-id lookup misses.

This works cleanly for the demo. It introduces a footgun for users who follow the convention with their own data.

### The footgun

Two entities sharing the same `name` under `LOCAL_SOURCE_SYSTEM` collide on the `(source_system, raw_id)` natural key. The current SCD-2 path:

1. The first `upsert_alias("local", "user-api", entity_id="A", ...)` opens row `R1: alias_id=X, entity_id=A, valid_to=NULL`.
2. A second `upsert_alias("local", "user-api", entity_id="B", ...)` calls `resolve_alias` → finds `R1` → closes it (`valid_to=now`) → inserts `R2: alias_id=X (same), entity_id=B, valid_to=NULL`.

After step 2, `resolve_alias("local", "user-api")` returns entity B. Entity A is still in the graph but **silently unreachable through the alias path** — no error, no warning, no event. The behavior is identical to a deliberate alias rebind (e.g., the user moved a label from one entity to another), and the storage layer cannot tell the difference.

This is fine when the collision is intentional. It is dangerous when two unrelated entities happen to share a name — for example, a service called `frontend` and a project called `frontend` both seeded under `"local"`, where the second overwrite is an accident, not a relabel.

The demo loader avoids the collision by construction (every entity has a unique `name`), so the symptom does not surface in tests or in the quickstart. It does surface for users who adopt the same convention with their own data without auditing for cross-type name uniqueness.

---

## 2. Decision

This ADR records the **current** semantics and the **immediate** posture. It does not change code shape. It does narrow the design space for two follow-ups.

### 2.1 Document the natural-key invariant

The `(source_system, raw_id)` pair is the natural key of `entity_aliases`. **The storage contract is: callers pick a `raw_id` that is unique under their chosen `source_system`, or they accept the SCD-2 rebind semantic.** This is now stated explicitly:

- **Inline.** [`src/trellis_cli/stores.py`](../../src/trellis_cli/stores.py)'s `LOCAL_SOURCE_SYSTEM` block comment already calls out the rebind behavior and recommends namespacing (`svc:foo`, `team:foo`) when uniqueness across types is not guaranteed.
- **Contract test.** A new contract test in `tests/unit/stores/contracts/graph_store.py` will assert: re-upserting with the same `(source_system, raw_id)` and a *different* `entity_id` MUST close the prior row and open a new one pointing at the new entity. Pins the rebind semantic so backends can't drift to "ignore" or "raise" without a coordinated design change. (Not landed in this ADR; tracked in TODO under "Quickstart polish follow-ups".)

### 2.2 Demo loader stays raw-name-keyed

The demo loader keeps `raw_id = entity.name` rather than namespacing per type. The README example — `trellis retrieve entity user-api` — depends on the bare label. Switching to `svc:user-api` would degrade the quickstart UX; the demo data is hand-curated to have unique names, which is the simpler invariant.

For real users, the inline doc + the open `LOCAL_SOURCE_SYSTEM` constant are the primary signals. Heavy users with name-collision risk are expected to namespace (`svc:foo`, `team:foo`, …) or use a non-`"local"` `source_system`.

### 2.3 Future direction: opt-in collision guard

Three shapes considered for catching unintentional collisions; **none land in this ADR**:

- **(a) Namespacing in the convention.** Recommend `svc:`, `team:` prefixes for `LOCAL_SOURCE_SYSTEM` aliases. **Cost:** UX regression for `retrieve entity user-api`; users would type `retrieve entity svc:user-api`. **Rejected** unless we can resolve unprefixed lookups by trying every type prefix (brittle, surface area grows linearly with `EntityType`).
- **(b) Strict mode at upsert.** Add an `allow_repoint: bool = False` kwarg on `upsert_alias`. With `allow_repoint=False`, an existing alias pointing at a different `entity_id` raises `AliasCollisionError` instead of closing-and-reopening. With `allow_repoint=True` (legitimate rebind), keep current SCD-2 behavior. **Cost:** ABC change; every backend's `upsert_alias` needs the new branch; minimum bump from `[]` to `["alias-strict"]` opt-in extra to avoid breaking existing callers. **Promising; deferred** until a real user reports a collision.
- **(c) Detection-only.** Emit an `ALIAS_REBIND` event when the prior alias's `entity_id` differs from the new one. Operators can audit via `trellis analyze events --type alias_rebind`. **Cost:** trivial — one branch in `upsert_alias`, one new `EventType`. **Likely the right first step** if option (b) ever lands; the event payload is a precondition for any audit / strict-mode UX. Tracked separately.

**Decision:** Pick option (c) first when motivated. (b) only if a design partner reports unintentional rebinds in production. (a) is rejected unless the unprefixed-lookup story is solved.

---

## 3. Consequences

### What this changes today

Nothing in the running code. The ADR documents the invariant that PR #99 implicitly relied on, and pins the design space for future work.

### What it constrains

- **No silent behavior change to `upsert_alias`.** Backends MUST keep SCD-2 rebind as the default. Strict mode is opt-in if it ever lands; existing callers stay unaffected.
- **The demo loader's name-as-raw-id pattern is sanctioned.** Future demo additions or example integrations can copy it without prefixing.
- **`LOCAL_SOURCE_SYSTEM` stays in `trellis_cli`.** Promotion to `trellis.stores` is gated on REST/MCP adopting the same convention (separate TODO item).

### What it does not address

- No support for the same `raw_id` under different `source_system` values pointing at different entities. That is the existing, intended schema and works today.
- No semantics for entity merger flows ("entity A was merged into entity B; redirect all aliases of A onto B"). That would be a separate `redirect_aliases(from_entity, to_entity)` method, not a property of `upsert_alias`.
- No automated lint that scans `LOCAL_SOURCE_SYSTEM` aliases for cross-type name collisions. Could be added as a `trellis admin check-aliases` command if it becomes useful.

---

## 4. Status

Proposed. Becomes Accepted once the contract test from §2.1 lands.
