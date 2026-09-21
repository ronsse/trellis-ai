# ADR: Referent Identity — How a Node Names Something in Another System

**Status:** Proposed
**Date:** 2026-09-20
**Deciders:** Trellis core
**Related:**
- [`./adr-alias-resolution.md`](./adr-alias-resolution.md) — **Accepted (2026-05-05).** Establishes `(source_system, raw_id)` as the natural key of `entity_aliases`, pins the SCD-2 rebind semantic, and reserves `LOCAL_SOURCE_SYSTEM = "local"`. This ADR extends that key to a class of referent it did not cover.
- [`./adr-graph-ontology.md`](./adr-graph-ontology.md) — §5.1 "No on-disk migration" and §5.2 "No new validation", both binding here.
- [`../../src/trellis/schemas/entity.py`](../../src/trellis/schemas/entity.py) — `EntityAlias`.
- [`../../src/trellis/stores/base/graph.py`](../../src/trellis/stores/base/graph.py) — `upsert_alias` / `bind_alias_if_absent` / `resolve_alias` / `get_aliases` ABC.
- [`../../src/trellis/extract/entity_resolution.py`](../../src/trellis/extract/entity_resolution.py) — the display-name index (`NAME_ALIAS_SOURCE_SYSTEM = "name"`), and the authoritative note on per-backend uniqueness enforcement.
- [`../../src/trellis/schemas/well_known.py`](../../src/trellis/schemas/well_known.py) — `DATASET_PROP_PHYSICAL_URI` / `DATASET_ROUTING_PROPERTIES`.
- [`../../src/trellis_cli/stores.py`](../../src/trellis_cli/stores.py) — `LOCAL_SOURCE_SYSTEM = "local"`. Note it is a **CLI-module** constant, not a core one.

---

## 1. Context

Trellis's graph is a **provenance map**, not a subject-matter knowledge graph. Nodes are agents, traces, tools, artifacts and gotchas; edges record who produced what. The map is meant to connect *outward* — to file stores, repositories, databases, services and other knowledge graphs — so that a memory about a thing can be joined to the thing.

The outward half does not work yet. Measured on the reference deployment, 2026-09-20:

**991 nodes denote something that lives in another system.**

| `node_type`         | count |
|---------------------|-------|
| SoftwareApplication |  851  |
| File                |  125  |
| Dataset             |    7  |
| Device              |    4  |
| API                 |    1  |
| Command             |    1  |
| SystemdUnit         |    1  |
| Wrapper             |    1  |

**Zero of those 991 carry any locator property at all.** Zero of the 125 `File` nodes carry either a locator or a link to a source document.

**Path-like node names: 133.** 103 relative, 30 absolute. Four of the 133 carry a repository or machine property. **Zero of the 30 absolute paths carry a machine or host.**

Locator-ish keys present anywhere in the graph, by occurrences and by distinct values:

| key          | occurrences | distinct values |
|--------------|-------------|-----------------|
| path-as-name | 137 | — |
| `repo`       |  60 | 9 |
| `machine`    |  41 | 2 |
| `host`       |  15 | 1 |
| `file`       |   9 | 9 |
| `endpoint`   |   4 | 2 |
| `url`        |   1 | 1 |

There is no convention about which key is authoritative: `host` and `machine` share exactly one value between them, and 23 nodes carry more than one locator key at once.

### 1.1 The defect is ambiguity, not divergence

An earlier gap analysis framed this as spelling drift — "the same file mentioned by two activities is two string spellings that happen to match, or happen not to." Measured, that is largely **not** what is happening:

- **Zero collisions by exact path string** across the 133 path-like nodes. This is not luck. For the `artifact:` namespace the minted id *is* the verbatim name, so two producers that type the same string converge by construction.
- Divergence that does exist is small: 4 suffix pairs (one file under both a relative and an absolute spelling), 5 basename groups carrying more than one spelling, 8 redundant ids by basename, 14 redundant ids across all 1,945 distinct current names.

The real defect is that **the key that converges is under-qualified.** The same relative path in two different repositories, or the same absolute path on two different machines, silently collapses to one node today — and no property anywhere on the node would let a reader tell that it had.

This has already happened in a neighbouring form. From [`entity_resolution.py`](../../src/trellis/extract/entity_resolution.py):

> Every mention fell through to the LLM residue stage, which is how the live graph accumulated seven separate `hermes` nodes with an empty `entity_aliases` table.

### 1.2 One producer today, several tomorrow

127 of the 133 path-like nodes carry `source_trace_id` / `agent_id` / `extractor_tier` — they were minted by trace extraction. Essentially one producer mints outward referents today, which is why exact-string convergence has been sufficient so far. That is a property of the monoculture, not of the scheme. A second producer is already in flight (an agent's saved note naming a referent by name), and the stated goal is many producers federating onto one map.

**So this decision is forward-looking by design.** It is not repairing a live corruption; it is choosing the key before there is more than one thing minting against it.

### 1.3 The primitive already exists

`EntityAlias` is a cross-system identifier bound to a canonical entity. It is in the schema, it is on the `GraphStore` ABC as `upsert_alias` / `bind_alias_if_absent` / `resolve_alias` / `get_aliases`, and it is exercised by the contract suite every backend must pass.

On the reference deployment the `entity_aliases` table holds **zero rows across all versions** — but that deployment predates the governed display-name index, which populates it under `source_system = "name"`. On `main` the table has its first tenant. **Nothing has ever written an outward referent into it.**

---

## 2. Decision

**Bind an `EntityAlias` row per `(source_system, raw_id)` for every referent that lives in another system. Identity is a lookup, not a string-equality accident.**

A producer that means a referent resolves it first (`resolve_alias(source_system, raw_id)`) and mints a node only on a miss, then binds the alias. A second producer meaning the same referent resolves to the entity the first one minted.

### 2.1 What goes in each field

- **`source_system` names the *kind* of external system** — `git`, `host`, `warehouse`, `graph:<name>` — and is an open string, as it already is.
- **`raw_id` carries the system-local identifier, qualified so that it is unique under that `source_system`.** A file in a repository is `<repo-slug>/<repo-relative-path>`. A file on a machine is `<machine>:<absolute-path>`. A table is the fully-qualified name its warehouse already uses.

Qualification lives in `raw_id` because that is exactly the contract the accepted alias ADR states (§2.1):

> **The storage contract is: callers pick a `raw_id` that is unique under their chosen `source_system`, or they accept the SCD-2 rebind semantic.**

and it is the pattern that ADR already recommends inline (`svc:foo`, `team:foo`) wherever uniqueness across kinds is not guaranteed.

That ADR **rejected** namespacing as option (a) — but the rejection is specific to `LOCAL_SOURCE_SYSTEM`, and its stated cost does not apply here:

> **Cost:** UX regression for `retrieve entity user-api`; users would type `retrieve entity svc:user-api`. **Rejected** unless we can resolve unprefixed lookups by trying every type prefix (brittle, surface area grows linearly with `EntityType`).

No user types a file path at a CLI expecting bare-label resolution. Outward referents are resolved by producers, not by humans recalling a label, so the unprefixed-lookup problem that sank (a) does not arise.

### 2.2 Reserved `source_system` values

`"name"` (the governed display-name index) and `"local"` (`LOCAL_SOURCE_SYSTEM`, the CLI's in-instance label convention) are **taken**. An outward referent must not be bound under either. This is the one place where the open-string posture needs a written-down convention rather than enforcement, per §5.2 of the ontology ADR.

### 2.3 Pair it with rebind detection

The accepted ADR's §2.3 option (c) — emit an event when a rebind repoints an existing `(source_system, raw_id)` at a different `entity_id` — is called "likely the right first step" there and is still unbuilt (verified 2026-09-20: no `ALIAS_REBIND` event type, no `AliasCollisionError`, no `allow_repoint` anywhere in `src/`). It becomes load-bearing here. For a display label a silent rebind is usually a deliberate relabel. For an outward referent it is the ambiguity failure becoming permanent: two different files claiming one binding, with the loser silently unreachable through the alias path, and no error, no warning and no event.

**This ADR proposes (c) as a precondition of adopting §2.2, not as a follow-up.**

---

## 3. What this decision does *not* do

This is the part most likely to be misread, so it is stated first among the consequences.

**Option B supplies the convergence mechanism. It does not supply the qualifier.** Uniqueness is scoped to `(source_system, raw_id)`, so a qualifier only disambiguates if it is *inside* one of those two fields. Producers do not capture one today — zero of 991 outward nodes carry any locator, and four of 133 path-like nodes carry a repo or machine. Binding aliases from the identifiers producers currently emit would bind under-qualified `raw_id`s and reproduce the collapse one layer down, now with a serialization point making it durable.

**A producer-side change that captures the qualifier at mint time is therefore a prerequisite, not a follow-up.** Adopting §2 without it is worse than the status quo, because it converts an ambiguous name into an authoritative binding.

This is also why **option C — folding the qualifier into the minted id itself** — is rejected rather than merely not chosen. C is lookup-free and would be the cheaper mechanism if the inputs existed; the measurement says they do not (0/991). C additionally strands every id already minted under the unqualified form, and the ontology ADR forbids rewriting them.

**Option A — a scheme-qualified `physical_uri` property on the node** — is rejected as the *identity* answer because it provides no convergence: two producers can write two different URIs for one referent, or the same URI onto two nodes, and nothing reconciles them. It remains useful as *evidence* a later reconciliation pass can group on, and the existing `physical_uri` convention for dataset-typed nodes is unaffected.

---

## 4. Constraints honoured

- **No on-disk migration.** No existing row in `nodes`, `edges` or `entity_aliases` is rewritten. The 991 already-minted unqualified nodes stay exactly as they are; they simply carry no alias until something binds one. Per ontology ADR §5.1.
- **The alias *mechanism* is not deprecated.** Ontology ADR §5.3 is titled "Aliases are documented as deprecated, not removed", and it is easy to read that as retiring `EntityAlias`. It is not: §5.3 is about legacy **type-name** enum values (`person` → `Person`), and says nothing about `entity_aliases` rows. §5.1 names that table among the ones it explicitly preserves.
- **No new validation.** `source_system` and `raw_id` stay open strings; §2.1 and §2.2 are conventions a producer follows, not rules the storage layer enforces. Per ontology ADR §5.2.
- **Traces stay immutable**, and every alias write already flows through the governed pipeline (`bind_alias_if_absent` from the mutation handlers), so no new ungoverned write path is introduced.
- **The claim floor is preserved.** Naming a referent is not a claim to possess or use it. Fresh mints keep `extraction_status="unconfirmed"` / `epistemic_status="mentioned"` and stay gated out of retrieval by default. An alias binding attests *that two producers mean the same thing*, and nothing more.

---

## 5. Known weaknesses

**Per-backend uniqueness is not uniform, and the blessed substrate is on the weaker side.** From [`entity_resolution.py`](../../src/trellis/extract/entity_resolution.py):

> SQLite and Postgres carry a real partial unique index (`idx_aliases_current ON entity_aliases(source_system, raw_id) WHERE valid_to IS NULL`), so a duplicate current binding is *impossible*. The two Bolt backends (Neo4j, ArcadeDB — neither overrides `SCHEMA_STATEMENTS`) index the lookup … but constrain uniqueness only on `version_id`; one-current-per-pair there rests on `upsert_alias`'s close-then-insert, not on DDL.

ArcadeDB is the blessed graph substrate. A scheme that leans on `(source_system, raw_id)` as an identity key therefore leans on application-level serialization there, not on the database. That is survivable — the resolver reads a single row and re-validates the binding before use — but it must not be described as a database-enforced invariant.

**Latency on the inline write path.** Resolution costs one indexed read per referent per write, on a path an agent waits on. It is an indexed lookup on `(source_system, raw_id)`, so the expected cost is small, but it is not zero and it has not been measured.

**Legacy nodes diverge from new mints until something reconciles them.** For a window, a referent may exist both as an unaliased legacy node and as a newly bound one. This ADR deliberately does not propose the reconciliation pass; it proposes the key that would make one possible.

---

## 6. What would change this decision

- **Evidence that producers can capture repo/host qualifiers at mint time for the large majority of events.** That would make option C viable and lookup-free, and C is cheaper than a per-write read. Today's measurement (0/991, 4/133) is what rules it out; re-measure rather than trusting this sentence.
- **A measured write-latency regression** attributable to alias resolution on the inline save path.
- **A second, independent identity index arriving for outward referents** — at which point the question becomes which one is authoritative, not whether to have one.

---

## 7. How this decision was made

A cross-lab decision panel (litellm `deep` / Kimi, and NVIDIA Nemotron) was given the measurements in §1, the three options, the constraints in §4, and the prior attempts quoted verbatim. Both panelists chose **B** independently — at confidence 0.72 and 0.85 — with non-overlapping reasoning: one on the grounds that B fails safe (ambiguity yields mergeable duplicates rather than silent collapse) while C demands qualifiers producers measurably do not capture; the other on the grounds that B requires no schema change to `nodes` and no migration.

Their risks were then verified against the code, and both survived: the under-qualified-`raw_id` collapse is §3 of this ADR, and the inline-lookup latency is §5.

**The panel did not have the accepted alias ADR** — it was found afterwards, during verification. That ADR had already established the natural key, already named the collision footgun, and had already considered and rejected namespacing for a different surface. The panel's verdict survives that discovery, but §2.1's reasoning is drawn from the ADR rather than from the panel, and the reader should weigh it accordingly.
