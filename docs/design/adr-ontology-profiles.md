# ADR: Optional governed ontology profiles

**Status:** Proposed
**Date:** 2026-06-03
**Deciders:** Trellis core
**Resolves:** #219
**Related:**
- [`./adr-enterprise-ontology-capability-framing.md`](./adr-enterprise-ontology-capability-framing.md) — #217, the umbrella capability framing this profile concept sits under
- [`./adr-query-history-promotion.md`](./adr-query-history-promotion.md) — #218, candidate→accepted promotion of query-derived edges (consumes the `candidate_only` / `promotion_requires_review` profile fields defined here)
- [`./adr-egp-interop-bridge.md`](./adr-egp-interop-bridge.md) — #220, the EGP interop bridge whose example profile motivates §2
- [`./adr-column-leaf-modeling-guardrails.md`](./adr-column-leaf-modeling-guardrails.md) — #221, column-vs-dataset modeling guidance that a profile's `node_role_default` / `recommendation` fields encode
- [`./adr-plugin-contract.md`](./adr-plugin-contract.md) — existing; plugins may ship profiles (§6, Phase D)
- [`./adr-graph-ontology.md`](./adr-graph-ontology.md) — sets the "small well-known core + open-string extension" policy this ADR overlays without changing
- [`./adr-observation-entity-type.md`](./adr-observation-entity-type.md) — precedent: conventional-but-unenforced properties on a node type
- [`../../src/trellis/schemas/well_known.py`](../../src/trellis/schemas/well_known.py) — canonical names + `canonicalize_*()` helpers a profile validates against
- [`../../src/trellis/schemas/enums.py`](../../src/trellis/schemas/enums.py) — `EntityType` / `EdgeKind` / `NodeRole` (well-known defaults, **not** a closed set)

---

## 1. Context

### 1.1 What Trellis does today, and why it is right

Entity types and edge kinds are **open strings** at the storage and API layers. The `EntityType` and `EdgeKind` `StrEnum`s in [`src/trellis/schemas/enums.py`](../../src/trellis/schemas/enums.py) and the canonical constants in [`src/trellis/schemas/well_known.py`](../../src/trellis/schemas/well_known.py) are *well-known defaults*, not a closed vocabulary — the storage layer accepts any string, and [`adr-graph-ontology.md`](./adr-graph-ontology.md) §5.2 commits explicitly to "no new validation." Domain-specific integrations (`dbt_model`, `uc_table`, `metric`, `oncall_shift`, `owned_by`, …) define their own types in their own packages. This is the dominant pattern for agent-context knowledge graphs and we are not changing it.

That openness is necessary for a reusable library. But it is *insufficient* for a specific enterprise deployment. An enterprise graph builder needs a way to declare, for **their** deployment only:

- which entity types and edge kinds are allowed (and which are typos to reject),
- which source system is authoritative for which field,
- which properties are required / recommended / forbidden on a given type,
- what node-role default a type should carry,
- which edges are cross-domain and therefore require attribution,
- which inferred or LLM-curated facts are still `candidate` versus `accepted`,
- which projections (named cross-domain query paths) a node or edge participates in.

Without such a layer, an enterprise graph degrades into a bag of correctly-stored strings: structurally valid, semantically drifting. The issue (#219) frames this as the strongest borrowable idea from EGP — not the graph itself (Trellis already has one) but the *governed ontology overlay*.

### 1.2 The tension to resolve

The core must stay open. A deployment must be able to constrain. These are not in conflict if the constraint lives **outside** core as an opt-in overlay that core does not require, load, or know about by default. That overlay is the **Ontology Profile**.

### 1.3 Terminology grounding

Per the project term map ([`adr-terminology.md`](./adr-terminology.md)) and `CLAUDE.md`: entity/edge types are open strings; the enums are well-known defaults. A **profile** is a deployment-scoped governance document, not a change to the **Knowledge Plane** schema and not a new **substrate** or **backend**. It governs what *should* be written; it does not change what *can* be stored.

---

## 2. Decision

Introduce an **optional, governed Ontology Profile**: a declarative YAML document, validated against a published schema, that a deployment or integration package *may* apply as a validation/governance overlay. Trellis core continues to accept open strings with no profile loaded. A profile is opt-in at every phase (§6); the recommended starting point is a CLI linter with **no runtime behavior change**.

### 2.1 What a profile can express

A profile expresses exactly the constraints #219 enumerates, and no more:

| Capability | Field(s) | Meaning |
|---|---|---|
| Domains | `domains` | Named domains this deployment recognizes (`TECHNICAL`, `BUSINESS`, `AGENT`, `DATA_MODEL`, …). Open list; the profile *names* them. |
| Entity types | `entity_types.<Name>` | Which types are allowed for this deployment; everything not listed is a lint finding (unless the profile sets an open-default — §2.3). |
| Edge kinds | `edge_kinds.<kind>` | Which edge kinds are allowed, with their permitted source/target type sets. |
| Source authority | `source_authority` (per type/edge), per-field map | Which source system is authoritative for a given field (`structure: unity_catalog`, `ownership: workday_or_uc_tags`). |
| Required / recommended / forbidden properties | `required_properties`, `recommended_properties`, `forbidden_properties` | Property-presence expectations on a type. Required-missing is an error; recommended-missing is a warning; forbidden-present is an error. |
| Node-role defaults | `node_role_default` | Default `NodeRole` (`structural` / `semantic` / `curated`) for nodes of this type, matching [`enums.py`](../../src/trellis/schemas/enums.py) `NodeRole`. |
| Cross-domain requirements | `cross_domain`, `requires_declared_by` | An edge crossing domains must carry attribution (a `declared_by` provenance property). |
| Candidate vs accepted | `status: candidate_only`, `promotion_requires_review` | Facts (typically inferred / query-derived) that are not yet accepted; promotion is governed by #218. |
| Projection membership | `projections.<name>.allowed_paths` | Named cross-domain query shapes and the entity→edge→entity paths permitted to answer them. |

### 2.2 The profile shape (EGP-inspired example)

This is the file shape from #219, used as the worked example. It is illustrative of the schema, not a shipped artifact.

```yaml
profile_id: fanduel.egp.data-platform.v1
owner: data-architecture
description: Data-platform and enterprise graph construction profile.

domains:
  - TECHNICAL
  - BUSINESS
  - AGENT
  - DATA_MODEL

entity_types:
  Dataset:
    canonical: true
    node_role_default: semantic
    source_authority:
      structure: unity_catalog
      description: unity_catalog_or_docs
      ownership: workday_or_uc_tags
    required_properties:
      - source_system
      - physical_uri
    recommended_properties:
      - database_name
      - schema_name
      - table_type
    retrieval:
      default_include: true

  Column:
    canonical: false
    node_role_default: structural
    recommendation: "Prefer Dataset.properties.columns unless column-level traversal/lifecycle is required."
    retrieval:
      default_include: false

edge_kinds:
  owned_by:
    source_types: [Dataset, Metric, Pipeline]
    target_types: [Team, Person, Organization]
    cross_domain: true
    requires_declared_by: true
    source_authority: workday_or_uc_tags

  commonly_joined_with:
    status: candidate_only
    source_authority: query_history
    promotion_requires_review: true

projections:
  metric_accountability:
    allowed_paths:
      - Metric -> owned_by -> Team
      - Metric -> dependsOn -> Dataset -> owned_by -> Team
      - Dataset -> producedBy -> Pipeline -> owned_by -> Team
```

Note that the example references both canonical names from [`well_known.py`](../../src/trellis/schemas/well_known.py) (`Dataset`, `Team`, `Person`, `Organization`, `dependsOn`) and deployment-specific open strings (`Metric`, `Pipeline`, `owned_by`, `commonly_joined_with`, `producedBy`). A profile is precisely the place to *register* those deployment-specific strings — core never has to learn them. The `Dataset.required_properties` of `source_system` / `physical_uri` line up with the recommended `DATASET_ROUTING_PROPERTIES` already documented in [`well_known.py`](../../src/trellis/schemas/well_known.py); a profile lets a deployment *promote* a Trellis recommendation to a deployment requirement without changing core.

### 2.3 Optional by construction

The profile is opt-in at three levels:

1. **No profile → no behavior change.** With no profile configured, every code path behaves exactly as today: open strings everywhere, no validation. This is the default and the POC/quickstart experience.
2. **A profile may declare an open default.** A profile can set `unknown_types: allow | warn | deny` (default `warn` for the linter, `allow` is the permissive setting) so even an *applied* profile need not close the type system — it can govern the named subset and pass everything else through. This keeps the open-string contract intact even under a profile.
3. **Enforcement is a separate, later, per-deployment choice.** The linter (Phase B) never changes a write. Runtime enforcement (Phase C) is an opt-in `MutationExecutor` policy gate that a deployment installs deliberately; it is never on by default and never installed by `trellis admin init`.

---

## 3. The profile schema and validation surface

### 3.1 Where the schema lives

A new Pydantic model — provisional `OntologyProfile` in `src/trellis/schemas/ontology_profile.py` — defines the document shape, mirroring the existing `TrellisModel` `extra="forbid"` convention so a malformed profile fails loudly rather than silently dropping fields. The model is **data only**: parsing a profile has no side effects, touches no store, and is independent of `StoreRegistry`.

### 3.2 Two CLI validation commands

Validation is exposed through the existing Typer CLI ([`src/trellis_cli/main.py`](../../src/trellis_cli/main.py) registers command groups via `app.add_typer`). A new `validate` group adds:

| Command | Validates | Reads any store? |
|---|---|---|
| `trellis validate ontology-profile <path.yaml>` | The profile document against the `OntologyProfile` schema — well-formed YAML, no unknown fields, internally consistent (e.g., every type named in `projections` paths exists in `entity_types`; every edge `source_types` / `target_types` references a declared type). | No. Pure document lint. |
| `trellis validate graph --profile <path.yaml>` | A graph (or a region of it) against an applied profile — required-property presence, forbidden-property absence, allowed type/edge membership, cross-domain attribution, candidate/accepted status. | Yes, read-only via `StoreRegistry`. Emits a findings report; writes nothing. |

Both support `--format json` per the Hard Rules. `validate graph` follows the `check-plugins` / `check-extractors` exit-code convention noted in [`adr-plugin-contract.md`](./adr-plugin-contract.md): `0` clean, non-zero on findings, so it slots into CI.

Neither command mutates anything. `validate graph` is a **read + report** path; it does not auto-fix and does not write events.

---

## 4. Why intentionally lighter than RDF/OWL

[`adr-graph-ontology.md`](./adr-graph-ontology.md) §2.3 / §8.2 already rejected full RDF/OWL for the core vocabulary, and the same reasoning forecloses building the profile layer on a triplestore/reasoner. A profile is a **lint contract**, not a formal ontology:

| RDF/OWL would give | A profile deliberately does instead |
|---|---|
| IRIs as identifiers, JSON-LD machinery | Plain strings — the same open strings the graph already stores. No identifier rewrite. |
| `rdfs:subClassOf` hierarchies, OWL DL reasoning, inferred class membership | A flat list of allowed types. No inference; a node is the type it was written as. |
| `owl:Restriction` / SHACL cardinality with an inference engine | Required/recommended/forbidden property *presence* checks, evaluated by a linter in a single pass — no reasoner. |
| Global, ontology-wide truth | Deployment-scoped governance. Profile A's rules say nothing about Profile B's graph. |
| Open-world assumption with materialized entailments | Closed-document lint with an explicit `unknown_types` escape hatch (§2.3). |

The benefits enterprise builders actually want — "reject the typo `Datset`", "require `source_system` on every Dataset", "make cross-domain `owned_by` edges carry attribution" — are all presence/membership checks expressible in YAML and evaluable without a reasoner. The costs RDF/OWL imposes (SPARQL, triplestore semantics, IRI minting, an inference layer in the hot path) buy capabilities Trellis does not need at its scale or use case, and the agent-memory field has moved away from them ([`adr-graph-ontology.md`](./adr-graph-ontology.md) §1). A profile is the 90%-of-the-value, 5%-of-the-cost overlay.

---

## 5. Why optional, and what stays open

The open-string core is load-bearing, not incidental:

- **Domain extensions depend on it.** `trellis_workers.extract` ships `dbt_model` / `uc_table` / OpenLineage types as open strings. Closing the type system would break every extension package.
- **Greenfield ergonomics.** A new user running `trellis admin init` must not meet the word "profile" — the litmus test from [`adr-graph-ontology.md`](./adr-graph-ontology.md) §6.3 applies here too. A profile is an enterprise capability, invisible to the quickstart path.
- **Profiles are deployment artifacts, not core.** Two deployments of Trellis can run incompatible profiles against the same code. The profile lives in the deployment's config, or is shipped by an integration package (§6, Phase D) — never minted into core enums.

Therefore: core stays exactly as open as it is today. The profile *adds* an optional contract; it removes nothing.

---

## 6. Migration path

Four phases, each independently shippable and each strictly opt-in. **Recommendation: ship through Phase B and stop until a deployment asks for Phase C** — Phase B delivers the governance value with zero runtime behavior change.

| Phase | Deliverable | Runtime behavior change? | Gating signal |
|---|---|---|---|
| **A — Docs-only convention** | Document the profile concept and the recommended shape in the agent guide; deployments hand-maintain a profile as documentation. | None | None — this ADR + a doc page. Lowest cost; does not prevent drift (that's the point of moving to B). |
| **B — Profile schema + CLI linter** *(recommended landing zone)* | `OntologyProfile` Pydantic schema + `trellis validate ontology-profile` + `trellis validate graph --profile`. CI-runnable, JSON output. | **None.** Linting is read-only; no write path changes. | This ADR accepted. |
| **C — Optional MutationExecutor policy enforcement** | A profile-backed `PolicyGate` (the `PolicyGate` Protocol in [`src/trellis/mutate/executor.py`](../../src/trellis/mutate/executor.py), per the `DefaultPolicyGate` pattern in [`src/trellis/mutate/policy_gate.py`](../../src/trellis/mutate/policy_gate.py)) that a deployment installs to `warn` or `deny` writes violating the active profile. Reuses the existing `Enforcement` (`WARN` / `ENFORCE` / `AUDIT_ONLY`) levels — no new enforcement vocabulary. | Opt-in only. Off by default; never installed by `trellis admin init`. | A specific deployment wants write-time enforcement. |
| **D — Plugin-provided profiles** | Integration packages ship a profile (e.g., `trellis_uc.profile.yaml`, `fanduel_egp.profile.yaml`), discovered via the entry-point mechanism in [`adr-plugin-contract.md`](./adr-plugin-contract.md). A reserved `trellis.ontology_profiles` group would advertise them to `check-plugins`. | None beyond whichever of B/C the deployment has opted into. | A plugin author wants to ship a profile; wire the consumer only when the first one appears (the deferred-wiring policy from [`adr-plugin-contract.md`](./adr-plugin-contract.md)). |

The phases compose: a deployment can sit at B forever (lint in CI), move to C for production write enforcement, and pick up D's vendor profile as a starting template — without any phase forcing the next.

### 6.1 Reusing the existing policy machinery (Phase C detail)

Phase C does **not** introduce a parallel enforcement path. The `MutationExecutor` already calls an injected `PolicyGate.check(command) -> (allowed, message, warnings)` ([`executor.py`](../../src/trellis/mutate/executor.py)). A `ProfilePolicyGate` is just another implementation of that Protocol, evaluating the active profile's rules against the command. It honors the same `Enforcement` levels as `DefaultPolicyGate` ([`policy_gate.py`](../../src/trellis/mutate/policy_gate.py)): a `deny`-on-violation profile rule under `ENFORCE` rejects the write; under `WARN` it appends a warning; under `AUDIT_ONLY` it logs. No change to the five-stage pipeline; one more gate, injected when (and only when) a deployment opts in.

---

## 7. Acceptance criteria

Tracking #219's stated acceptance criteria:

- [x] **A design doc/ADR defines the Ontology Profile concept and explains why it is optional.** This document; §2.3 and §5.
- [x] **The profile can express:** domains (§2.1 `domains`), entity types (`entity_types`), edge kinds (`edge_kinds`), source authority (`source_authority`), required/recommended/forbidden properties (`required_properties` / `recommended_properties` / `forbidden_properties`), node-role defaults (`node_role_default`), cross-domain requirements (`cross_domain` / `requires_declared_by`), candidate vs accepted status (`status: candidate_only` / `promotion_requires_review`), projection membership (`projections`).
- [x] **The design explicitly preserves the open-string core.** §2.3, §5, and the `unknown_types` escape hatch.
- [x] **Includes an EGP-inspired example profile.** §2.2.
- [x] **Includes a migration path** (docs-only → CLI linter → optional mutation-policy enforcement → plugin-provided). §6.
- [x] **Explains how this differs from RDF/OWL and why it is lighter.** §4.

A future implementation ADR (or a phase landing under this one) will additionally require:

- `OntologyProfile` Pydantic model with `extra="forbid"`, tests for round-trip parse and internal-consistency validation.
- `trellis validate ontology-profile` and `trellis validate graph --profile` commands with `--format json` and CI-friendly exit codes.
- A documented example profile fixture under the agent guide.

---

## 8. Non-goals

- **Not changing the core type system.** Open strings remain the storage/API contract. The enums in [`enums.py`](../../src/trellis/schemas/enums.py) stay well-known defaults, not a closed set. ([`adr-graph-ontology.md`](./adr-graph-ontology.md) §5 still holds.)
- **Not a formal ontology / reasoner.** No RDF, OWL, SHACL, SPARQL, IRIs, JSON-LD, or inference. §4.
- **Not on by default.** No profile is loaded, linted, or enforced unless a deployment opts in. `trellis admin init` and the quickstart are untouched.
- **Not a runtime change at the recommended landing zone (Phase B).** The linter is read-only. Write-time enforcement (Phase C) is a separate, deliberate opt-in.
- **Not defining the promotion mechanics for candidate→accepted facts.** The profile *marks* `candidate_only` / `promotion_requires_review`; the promotion loop itself is [`adr-query-history-promotion.md`](./adr-query-history-promotion.md) (#218).
- **Not defining column-vs-dataset modeling rules.** The profile can *encode* such a guardrail (`Column.recommendation`, `node_role_default`); the guidance itself is [`adr-column-leaf-modeling-guardrails.md`](./adr-column-leaf-modeling-guardrails.md) (#221).
- **Not an interop/export format.** Consuming Trellis graphs from external EGP tooling is [`adr-egp-interop-bridge.md`](./adr-egp-interop-bridge.md) (#220); the `schema_alignment` field already serves RDF/JSON-LD export ([`adr-graph-ontology.md`](./adr-graph-ontology.md) §3.4). A profile is governance-in, not export-out.
- **Not claiming any code is implemented.** This ADR is **Proposed**; no `OntologyProfile` schema, `validate` command, or `ProfilePolicyGate` exists yet.
