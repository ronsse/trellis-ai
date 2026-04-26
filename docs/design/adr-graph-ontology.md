# ADR: Graph Ontology — schema.org for Entities, PROV-O for Provenance

**Status:** Phase 0 fully landed (Proposed for later phases)
**Date:** 2026-04-24 (Phase 0 docs completed 2026-04-25)
**Deciders:** Trellis core
**Related:**
- [`./adr-tag-vocabulary-split.md`](./adr-tag-vocabulary-split.md) — same "well-known defaults vs open extension" axis applied to content tags
- [`./adr-terminology.md`](./adr-terminology.md) — canonical term map; this ADR adds entity/edge naming
- [`../../src/trellis/schemas/enums.py`](../../src/trellis/schemas/enums.py) — `EntityType`, `EdgeKind`, `NodeRole` (today: 10 entity types, 11 edge kinds)
- [`../../src/trellis/schemas/graph.py`](../../src/trellis/schemas/graph.py) — `Edge`, `CompactionReport` (note: `edge_kind` is already documented as an open string)
- [`../../src/trellis/extract/`](../../src/trellis/extract/) — `JSONRulesExtractor`, dispatcher; produces entity/edge drafts whose names land in the type system
- [`../../CLAUDE.md`](../../CLAUDE.md) — extension-point policy: domain-specific types live in their own packages

---

## 1. Context

### What exists today

`EntityType` is a `StrEnum` with 10 values: `person`, `system`, `service`, `team`, `document`, `concept`, `domain`, `file`, `project`, `tool`. `EdgeKind` is a `StrEnum` with 11 values grouped into trace edges (4), entity edges (3), evidence edges (2), and precedent edges (2). Both are documented as **well-known defaults, not closed sets** — the storage and API layers accept any string.

The grouping reveals the design intent: edges encode **agent provenance** (who/what produced this, what they used, what they touched, what they promoted) more than they encode domain semantics. That intent is exactly right for a graph-of-agent-experience. The names are not.

### What is biased about the current names

The values were minted ad-hoc for early agent-trace work:

- `person`, `system`, `service` — lowercase, locally invented; collide with no recognized vocabulary.
- `trace_used_evidence`, `trace_produced_artifact`, `trace_touched_entity` — describe the right relationships but in a vocabulary nobody else uses.
- `entity_related_to`, `entity_part_of`, `entity_depends_on` — `related_to` is a known SKOS term, `part_of` is universal, `depends_on` is generic; the `entity_` prefix is noise.
- `domain` as a `StrEnum` value collides with `ContentTags.domain` (the classification facet) and with the colloquial English meaning. Three concepts, one word.

The cost is not aesthetic. Concrete failures we expect as adoption grows:

1. **Interoperability with SDKs.** LlamaIndex's `PropertyGraphIndex` defaults to `PERSON / ORGANIZATION / PRODUCT / EVENT / LOCATION` — schema.org-aligned. A Trellis adopter wiring up LlamaIndex extraction has to write a translation table by hand because Trellis spells `person` and the SDK spells `Person`, and Trellis has no `Organization`.
2. **Provenance reasoning.** Tools like Microsoft GraphRAG, Mem0, and Glean all converge on PROV-O-shaped vocabulary (`Activity`, `Entity`, `Agent`, `wasGeneratedBy`, `used`, `wasDerivedFrom`). Trellis re-invents the same shape under different names, so these tools can't read Trellis graphs without a custom adapter.
3. **Agent-written code.** Agents writing extractors / mutations against the graph rely on intuition and existing knowledge of standards. Schema.org is in their training data; `trellis_used_evidence` is not. Made-up names increase the rate at which agents emit subtly wrong types.

### What's missing

The current vocabulary names are anchored in the trace/agent side. A Trellis graph also captures **business and operational entities** — organizations, products, events, places, datasets — and there's no first-class home for them today. They land as `concept` (lossy) or as a custom string per integration (fragments). Adopters in non-software domains feel this immediately.

### What we already do right (and don't want to lose)

- **Open-string extensibility.** The storage and API layers accept any entity type / edge kind string. Domain-specific integrations define their own types in their own packages. We are not changing this.
- **Three-role distinction.** `NodeRole` (`structural | semantic | curated`) is orthogonal to entity type and stays as-is. The ontology decision is about naming the entity types and edge kinds, not the roles.
- **Edges as first-class records.** Every edge already carries `edge_id`, `source_id`, `target_id`, `edge_kind`, `properties`, `valid_from`, `valid_to`, plus the SCD-2 versioning. Adding provenance fields (e.g., `source_trace_id`, `confidence`, `agent_id`) is purely additive.
- **EventLog as the audit journal.** `MutationExecutor` emits `MUTATION_EXECUTED` events for every write. This is the authoritative provenance log; the graph stores the *current shape* of the world, the EventLog stores *how we got here*. We are not splitting the audit story across two stores.

### The research that informed this ADR

A literature/standards survey was performed (see `TODO.md` — "Graph ontology survey + alignment ADR"; the survey was conducted with web access disabled, so it relied on canonical, stable specs rather than freshly fetched 2025-2026 papers). It evaluated **schema.org**, **FIBO**, **Wikidata's data model**, **PROV-O**, **CIDOC CRM**, **OpenLineage**, **GoodRelations** (now part of schema.org), plus how **Microsoft GraphRAG**, **Mem0**, **MemGPT**, **LlamaIndex**, **Zep / Graphiti**, and **Glean** structure their entity/edge vocabularies.

Three findings drove this decision:

1. **The industry is converging on schema.org for entities and PROV-O for provenance** — not formally, but de facto. LlamaIndex defaults, Glean's public docs, and the temporal-KG-for-LLM literature all land in this neighborhood.
2. **The industry is moving *away* from rigid ontologies for agent memory.** GraphRAG discovers types per corpus. Mem0 uses a 3-type minimal schema. MemGPT has no typed graph at all. Heavy ontologies (FIBO, CIDOC) are liabilities, not assets, for agent context graphs.
3. **A small, well-known typed core plus open-string extensibility is the dominant pattern.** This is exactly Trellis's current shape. The fix is renaming the small core to align with what everyone else uses, not changing the architecture.

### The decision to make

Do we:
- **(A)** Keep the locally-invented names and add a separate `schema_alignment` hint field for interop
- **(B)** Realign the well-known defaults with schema.org (entities) and PROV-O (provenance edges), keeping the existing names as deprecated aliases with no on-disk migration
- **(C)** Adopt RDF/OWL fully — IRIs as identifiers, formal ontology, triplestore-shaped reads
- **(D)** Do nothing — let adopters bring their own vocabulary

---

## 2. Decision

**Option B: Realign on schema.org + PROV-O. Additive only. Existing names stay as deprecated aliases.**

The well-known defaults shift to:
- **schema.org names for entity types** — `Person`, `Organization`, `SoftwareApplication`, `Dataset`, `CreativeWork`, `Product`, `Event`, `Place`, plus `Agent` and `Activity` from PROV-O for the agent/trace abstractions.
- **PROV-O verbs for trace/provenance edges** — `used`, `wasGeneratedBy`, `wasDerivedFrom`, `wasInformedBy`, `wasAttributedTo`, `wasAssociatedWith`. Plus `partOf`, `dependsOn`, `relatedTo` for entity edges (these align with both schema.org and SKOS and are universal enough to keep).

`NodeRole` is unchanged. Open-string extensibility is unchanged. Storage shape is unchanged.

### 2.1 Why schema.org for entities

- **In agents' training data already.** Schema.org is the most-trained-on entity vocabulary in existence (it powers the open web's structured data). Agents writing extractors will tend to emit it correctly without prompting.
- **Cheap to adopt.** No RDF, no IRIs, no JSON-LD machinery. Just take the class names. The full schema.org class hierarchy is irrelevant to us — we are using ~10 top-level types as labels, not as a formal type system.
- **Covers the cross-industry vocabulary gap.** `Organization`, `Product`, `Event`, `Place`, `CreativeWork` are the names regulated enterprises, retailers, and media companies already use. Closes the "no business entities" gap for free.
- **GoodRelations is already absorbed.** No separate commerce ontology to bolt on.

### 2.2 Why PROV-O for provenance / trace edges

- **It is *the* provenance vocabulary.** W3C standard, vocabulary-only (no reasoning required), and its core triad — `Entity`, `Activity`, `Agent` — maps almost one-to-one onto Trellis's trace model. A Trace is an Activity, an agent or human is an Agent, an evidence item is an Entity.
- **Industry alignment.** GraphRAG's relationships, Glean's activity graph, and the temporal-KG-for-LLM line of work all use PROV-O-shaped verbs. Trellis becomes legible to those tools without a translator.
- **No reasoning baggage.** PROV-O can be adopted as plain naming, exactly like schema.org. We don't need PROV-Constraints, PROV-N, or any inference engine.

### 2.3 Why we are *not* adopting the others

| Standard | Why not |
|---|---|
| **FIBO** | Excellent for finance; irrelevant for cross-domain agent context. Importing even a subset drags in OWL DL hierarchies hundreds deep and the LCC foundational ontologies. Domain-specific FIBO mappings can live in a separate finance integration package, never in core. |
| **CIDOC CRM** | Event-centric philosophy is good (PROV-O carries the same idea with better ergonomics). Vocabulary is heavyweight (90+ classes, cryptic E-numbers) and only adopted in museum/archive tooling. Borrow the framing, ignore the names. |
| **Wikidata identifiers** | Q-IDs and P-IDs are great for an encyclopedic KG; minting them for agent traces makes no sense. **Borrow the statement-with-qualifiers shape** — every claim reified with confidence/time/source — but not the IDs. Trellis's existing edge schema is already this shape; we just need to add provenance fields explicitly. |
| **OpenLineage** | Excellent and already partially integrated (`trellis_workers` ships an extractor). It is spiritually a domain-specific refinement of PROV-O — `Run` ↔ `prov:Activity`, `Dataset` ↔ `prov:Entity`, `Job` ↔ `prov:Plan`. **Stays as a domain extension.** Do not put `Job` / `Run` / `Dataset` (OpenLineage flavor) in the core well-known defaults — `Dataset` lands via schema.org, the rest stays in `trellis_workers`. |
| **Full RDF / OWL** | Triplestore semantics, IRI-as-identifier, formal ontology authoring. Massive complexity multiplier on the storage and query layers for benefits that don't materialize at our scale or use case. The entire industry moved away from this for AI agent KGs, and so do we. |

---

## 3. Concrete vocabulary

### 3.1 Entity types — well-known defaults

Naming convention: **PascalCase, schema.org or PROV-O class names verbatim.**

| New canonical | Aliases (back-compat) | Source standard | Use for |
|---|---|---|---|
| `Person` | `person` | schema.org | Individual humans (employees, customers, contacts) |
| `Organization` | — | schema.org | Companies, teams, departments, vendors, regulators |
| `Team` | `team` | schema.org subset of `Organization` | Internal teams; alias retained because "team" is universally used |
| `SoftwareApplication` | `system`, `service`, `tool` | schema.org | Services, systems, tools, applications |
| `Dataset` | — | schema.org | Tables, views, files-as-data, embeddings, model artifacts |
| `CreativeWork` | `document` | schema.org (supertype) | Documents, articles, ADRs, runbooks, knowledge-base entries |
| `Product` | — | schema.org | Sellable / catalog products |
| `Event` | — | schema.org | Incidents, deployments, meetings, business events |
| `Place` | — | schema.org | Physical or virtual locations (regions, datacenters, URLs as locations) |
| `File` | `file` | schema.org `MediaObject` (subtype) | Generic files where `Dataset` / `CreativeWork` doesn't fit |
| `Project` | `project` | (no exact schema.org match) | Projects / initiatives — Trellis-specific but semantically clear |
| `Concept` | `concept` | (no exact schema.org match) | Abstract concepts; a deliberate "I don't know what to call this yet" bucket |
| `Agent` | — | PROV-O | Anything that *acts*: human user, automated agent, system actor. **Distinct from `Person`** — a `Person` may also be an `Agent` when acting; system actors are `Agent` only. |
| `Activity` | — | PROV-O | A unit of work: a trace, a task, a workflow run. The trace itself, as a graph node. |

**Removed from defaults:** `domain` (the lowercase enum value). Reason: collides with the `ContentTags.domain` classification facet and with the English meaning. Domain-as-a-grouping-of-knowledge is a tagging concern, not an entity type. Existing data using `entity_type="domain"` keeps working as an open string; we just stop suggesting it.

### 3.2 Edge kinds — well-known defaults

Naming convention: **camelCase, PROV-O verbs verbatim where they apply.**

| New canonical | Aliases (back-compat) | Source standard | Semantics |
|---|---|---|---|
| `used` | `trace_used_evidence` | PROV-O `prov:used` | Activity (trace) consumed an Entity (evidence/document) as input |
| `wasGeneratedBy` | `trace_produced_artifact` (inverse direction) | PROV-O `prov:wasGeneratedBy` | An Entity was created by an Activity. **Note:** PROV-O's direction is `entity → activity`. Trellis's existing edge points trace→artifact. We keep the existing direction for back-compat and document it; the alias retains the original meaning. |
| `wasInformedBy` | `trace_touched_entity` | PROV-O `prov:wasInformedBy` | A trace's outcome was influenced by another entity / trace, even if not directly consumed |
| `wasDerivedFrom` | `trace_promoted_to_precedent`, `precedent_derived_from` | PROV-O `prov:wasDerivedFrom` | One entity is derived from another (precedents from traces, summaries from sources, generated nodes from `generation_spec` inputs) |
| `wasAttributedTo` | (new) | PROV-O `prov:wasAttributedTo` | Entity is attributable to a responsible Agent (author, owner, responsible party) |
| `wasAssociatedWith` | (new) | PROV-O `prov:wasAssociatedWith` | Activity was associated with an Agent (the agent that ran the trace) |
| `partOf` | `entity_part_of` | schema.org `isPartOf` (camelCased) | Compositional containment. Universal. |
| `dependsOn` | `entity_depends_on` | (universal) | Functional dependency between entities |
| `relatedTo` | `entity_related_to` | SKOS `related` / schema.org | Catch-all symmetric relationship; use only when no more specific edge fits |
| `attachedTo` | `evidence_attached_to` | (Trellis-specific, kept) | Evidence row attached to an entity row. No PROV-O equivalent that fits cleanly; retained as a Trellis-specific verb. |
| `supports` | `evidence_supports` | (Trellis-specific, kept) | Evidence supports a claim. Distinct from `attachedTo` because support is about *epistemic backing*, not membership. |
| `appliesTo` | `precedent_applies_to` | (Trellis-specific, kept) | A precedent applies to a class of situations |

### 3.3 The "this, not that" table for the renamed edges

Conflations the new vocabulary lets us avoid:

| Edge | Answers | Don't confuse with |
|---|---|---|
| `used` | What did this Activity *consume* as input? | `wasInformedBy` (influenced by, not consumed) |
| `wasGeneratedBy` | What Activity *produced* this Entity? | `wasDerivedFrom` (derived from another Entity, no Activity in the middle) |
| `wasInformedBy` | What other thing *influenced* this Activity's outcome? | `used` (direct input) |
| `wasDerivedFrom` | This Entity is a transformation of that Entity. | `wasGeneratedBy` (Activity produced it) |
| `wasAttributedTo` | Who is the responsible Agent for this Entity? | `wasAssociatedWith` (which Agent ran the Activity, not the responsible party) |
| `wasAssociatedWith` | Which Agent ran this Activity? | `wasAttributedTo` (responsible party for an Entity) |
| `partOf` | Compositional containment | `dependsOn` (functional, not structural) |
| `attachedTo` | This evidence row is *bound to* an entity | `supports` (this evidence *backs the claim*) |
| `supports` | This evidence justifies the claim | `attachedTo` (mere binding) |

### 3.4 Optional `schema_alignment` metadata field

For interop with downstream tooling that wants strict standard URIs, every entity and edge carries an optional `schema_alignment` field on its `metadata` / `properties` map:

```python
{
    "node_type": "Person",
    "metadata": {"schema_alignment": "schema.org/Person"},
}
{
    "edge_kind": "used",
    "properties": {"schema_alignment": "prov:used"},
}
```

- Empty / missing for domain-specific types (e.g., `dbt_model_references` has no schema.org equivalent).
- Populated automatically by the alias resolver (§ 4.2) when a canonical value is used.
- Consumed by future export tooling (e.g., a JSON-LD or RDF exporter) — not consumed by core retrieval or policy code.

This gives us the "interop with anything that speaks RDF" story without paying for it inside the core hot path.

---

## 4. Provenance model — what changes, what doesn't

### 4.1 Edges remain first-class reified records

Every edge already carries `edge_id`, source/target, kind, properties, and SCD-2 timestamps. **We do not change this shape.** Provenance fields are added as conventional keys inside `properties`, all optional:

| Property | Type | Set by | Meaning |
|---|---|---|---|
| `source_trace_id` | `str` | Extractor | Which trace/activity produced this edge |
| `agent_id` | `str` | Extractor | Which agent (human or software) made the assertion |
| `confidence` | `float` (0..1) | Classifier / extractor | Confidence in the assertion |
| `evidence_ref` | `str` (item_id) | Extractor | Link to the evidence row that supports this edge |
| `extractor_tier` | `"deterministic" \| "hybrid" \| "llm"` | Dispatcher | Which extraction tier produced it |

These are conventions, not schema enforcement. A future ADR can promote any of them to first-class columns if a policy consumer needs to gate on them — same promotion path as `ContentTags` flex fields.

### 4.2 Alias resolution

A small module — provisional name `schemas/well_known.py` — owns:

1. The list of canonical names (entity types, edge kinds).
2. The alias-to-canonical map (legacy lowercase → PascalCase / camelCase).
3. A `canonicalize(value: str) -> str` function: returns the canonical form if `value` is a known alias, otherwise returns `value` unchanged.
4. Predicates: `is_canonical(value)`, `is_alias(value)`, `is_known(value)`.

`canonicalize` is **not** called by the storage layer (storage stays on whatever string was written). It is used by:

- **Extractors** when they emit a draft, so new extractions land on canonical names.
- **Retrieval and analytics** when they want to bucket "person-ish" entities together.
- **The optional `schema_alignment` populator.**

This is intentionally conservative: we don't rewrite history, we don't break existing reads, we just make new writes land on the canonical names and make retrieval cross-bucket-aware.

### 4.3 EventLog stays as the audit journal

The EventLog is *not* a parallel provenance store. Graph edges describe the **current shape of the world** (with SCD-2 history). The EventLog describes **what mutations happened, in what order, by whom**. Both are needed; they answer different questions. This ADR does not propose changing either.

---

## 5. Guardrails

The decision is only safe if the migration is genuinely additive and the open-string contract remains intact.

### 5.1 No on-disk migration

Existing rows in `nodes`, `edges`, `entity_aliases` are not rewritten. Every legacy value (`person`, `service`, `trace_used_evidence`, …) keeps working as an open string. Retrieval code that reads `node_type` does not need to know about the canonical / alias distinction unless it specifically wants to bucket across both.

### 5.2 No new validation

The storage layer continues to accept any string. The extractors prefer canonical names but will accept anything. There is no "must be a known type" enforcement anywhere. Adopters who want validation layer it on themselves via a custom `Classifier` or mutation policy.

### 5.3 Aliases are documented as deprecated, not removed

A warning in the docstring of every legacy enum value points at the canonical replacement. The value itself never disappears — removal would be a breaking change with no upside. Adopters who keep using `person` get a cleaner graph if they migrate, but pay no penalty if they don't.

### 5.4 The `EntityType` and `EdgeKind` StrEnums grow, they don't shrink

New canonical values are added as new StrEnum members. Old values are kept (optionally annotated with deprecation). A reader checking `EntityType("person")` keeps getting `EntityType.PERSON` forever; a reader checking `EntityType("Person")` now gets `EntityType.PERSON_CANONICAL` (or whatever we name the new member). Both serialize to their respective strings.

### 5.5 The terminology ADR gets updated, not replaced

[`adr-terminology.md`](./adr-terminology.md) is the canonical term map. This ADR adds a section to it: a one-sentence reference per canonical name, pointing back here for the full table.

---

## 6. Scope — Phase 0 only

This ADR commits only to Phase 0. Later phases are listed for context but require their own decisions.

### 6.1 What Phase 0 ships (landed 2026-04-24)

| Deliverable | Status | Footprint |
|---|---|---|
| This ADR | Landed | ~500 lines of markdown |
| `schemas/well_known.py` — canonical names + alias maps + `canonicalize_*()` + `is_*()` helpers | Landed | 195 lines |
| Tests for the alias map (round-trip, idempotence, naming conventions, drift guards) | Landed | 41 tests |
| Docstring pointers on legacy `EntityType` / `EdgeKind` enums to `well_known.py` | Landed | ~30 lines |
| `docs/agent-guide/schemas.md` — extended `EntityType` / `EdgeKind` tables with canonical names + ADR cross-reference | Landed | ~50 lines |
| `adr-terminology.md` — new §2.5 "Graph ontology" subsection | Landed | ~20 lines |

**Note on form:** The original deliverable list proposed adding new `EntityType` / `EdgeKind` ``StrEnum`` members for the canonical names (e.g., `PERSON_CANONICAL = "Person"`). That created two confusing enum members per logical type for collision cases (`PERSON` + `PERSON_CANONICAL`, `TEAM` + `TEAM_CANONICAL`, …). The actual implementation puts canonical names as module-level constants in `well_known.py` instead. The legacy enums stay unchanged as the back-compat registry; `well_known.py` is the canonical registry. `canonicalize_entity_type()` / `canonicalize_edge_kind()` bridge the two. Same outcome as the ADR's decision (§2), cleaner namespace.

Total landed: ~225 lines of code + ~600 lines of docs / ADR. No migrations, no extractor changes, no retrieval changes, no policy gates, no CLI, no MCP tools, no SDK re-exports.

### 6.2 What Phase 0 does *not* ship

- Storage migrations (none needed).
- Default values for new fields on existing data (additive only — no defaults to backfill).
- Changes to extractors. They keep emitting whatever they emit today. (Phase 1.)
- `schema_alignment` auto-population. The field is documented; nothing writes it yet. (Phase 1.)
- `canonicalize()` calls in retrieval / pack builder. The function exists, it's not yet wired in. (Phase 2.)
- A JSON-LD or RDF exporter. (Phase N, gated on demand.)

### 6.3 The litmus test

A new user installing Trellis today and running `trellis admin init` should not encounter the words "schema.org" or "PROV-O" in any user-facing surface. The names show up in the agent-guide docs and in extractor output, not in CLI help text or error messages. If Phase 0 changes the getting-started flow, it over-shipped.

### 6.4 Later phases (informational only, not approved here)

| Phase | Scope | Gating signal |
|---|---|---|
| **Phase 1** | Switch built-in extractors (`JSONRulesExtractor`) to emit canonical names; auto-populate `schema_alignment` on extractor drafts. | Phase 0 has been live for a release and no breakage observed. |
| **Phase 2** | `PackBuilder` and analytics buckets cross-canonical-and-alias values via `canonicalize()`. | Phase 1 observed across multiple deployments. |
| **Phase 3** | First-class provenance fields (`source_trace_id`, `agent_id`, `confidence`) promoted from edge `properties` to dedicated columns. | A policy or retrieval consumer wants to gate on them. |
| **Phase 4** | JSON-LD / RDF export tooling using the populated `schema_alignment` field. | A design partner wants to consume the graph from RDF tooling. |

---

## 7. Consequences

### 7.1 What this preserves

- **No painful migration later.** The canonical names are minted now; any extension we ever want to add is also additive.
- **Open-string extensibility.** Domain integrations continue to define their own types in their own packages. Nothing in this ADR closes the type system.
- **The POC story.** Phase 0 adds zero surface to the getting-started flow, README, or CLI.
- **Existing data works forever.** Aliases are not deprecated removals — they are permanent reverse-lookups.

### 7.2 What this costs

- **A permanent alias map.** Adding a new alias is cheap; reusing a canonical name for something else is forbidden once published. The canonical names listed here are a one-way commitment.
- **Two ways to spell every type for one cycle.** Until Phase 1 lands, extractors will produce a mix of legacy and canonical names. `canonicalize()` is the single source of truth for which is which. (This is what the `schemas/well_known.py` module is for.)
- **A new ADR every time a canonical name is added.** Same as the tag-vocabulary-split policy — additions go through a tiny ADR amendment, not a code-only change. This keeps the canonical list small and intentional.

### 7.3 What this forecloses

- **Adopting RDF/OWL fully.** This decision says "schema.org names, no IRIs, no triplestore." A future ADR could revisit, but the migration cost would be substantial — by the time we'd want to, most of the industry would have to have moved that way too. We are betting the industry stays where it is.
- **Inventing more local vocabulary.** New entity types and edge kinds proposed for the well-known defaults must come from schema.org / PROV-O / SKOS first. Domain-specific names live in domain-specific packages, never in core.

---

## 8. Alternatives considered

### 8.1 Option A — Keep local names + add a `schema_alignment` hint field

Lower-cost in the short term: no enum changes, just a metadata field. Rejected because it leaves the *primary* surface (the type strings agents read and write) using locally-invented names. The interop benefit only shows up for tooling that specifically reads `schema_alignment`. Agents writing extraction code don't read metadata fields; they pattern-match on names. Also doesn't address the `domain` collision.

### 8.2 Option C — Adopt RDF / OWL

IRIs as identifiers, formal ontology authoring, triplestore-shaped reads. Rejected because:
- Massive complexity multiplier on storage (triplestores) and query (SPARQL).
- The agent-memory / agent-context-graph industry has explicitly rejected this direction. Adopting it makes Trellis less compatible with where the field is going, not more.
- We get the interop benefits with `schema_alignment` URIs without any of the runtime cost.

### 8.3 Option D — Do nothing

Rejected because the cost of the locally-invented names compounds with adoption. Renaming is cheaper now (when the system has zero external adopters) than later (when every adopter has built their own translation layer).

### 8.4 Adopting FIBO / CIDOC / Wikidata identifiers

Each rejected for reasons documented inline in § 2.3. Summary: too narrow (FIBO), too heavyweight (CIDOC), wrong granularity (Wikidata IDs).

### 8.5 Closing the type system

Rejected emphatically. The "open string + well-known defaults" pattern is one of Trellis's core strengths and matches where the industry has converged. Closing it would also break every domain extension package today.

---

## 9. References

- **schema.org** — [https://schema.org/](https://schema.org/) — Core entity vocabulary; classes used: `Person`, `Organization`, `SoftwareApplication`, `Dataset`, `CreativeWork`, `Product`, `Event`, `Place`, `MediaObject`.
- **PROV-O** — W3C Recommendation, [https://www.w3.org/TR/prov-o/](https://www.w3.org/TR/prov-o/) — Provenance vocabulary; classes used: `Entity`, `Activity`, `Agent`. Properties used: `used`, `wasGeneratedBy`, `wasDerivedFrom`, `wasInformedBy`, `wasAttributedTo`, `wasAssociatedWith`.
- **SKOS** — [https://www.w3.org/TR/skos-reference/](https://www.w3.org/TR/skos-reference/) — Source of `related` (via schema.org's `relatedTo`).
- **OpenLineage** — [https://openlineage.io/](https://openlineage.io/) — Stays as a domain extension; mappings live in `trellis_workers`.
- Industry survey conducted 2026-04-24 covering Microsoft GraphRAG, Mem0, MemGPT, LlamaIndex `PropertyGraphIndex`, Zep / Graphiti, Glean. Survey notes archived in the conversation that produced this ADR; no separate document committed (the findings are folded into § 1's "research that informed this ADR" subsection).
