# ADR: Terminology — Canonical Term Map

**Status:** Accepted
**Date:** 2026-04-19
**Deciders:** Trellis core
**Related:**
- [`./adr-tag-vocabulary-split.md`](./adr-tag-vocabulary-split.md) — splits `ContentTags` into retrieval-shaping tags + `DataClassification` policy schema
- [`./adr-planes-and-substrates.md`](./adr-planes-and-substrates.md) — introduces Knowledge / Operational planes and blessed substrates
- [`./adr-graph-ontology.md`](./adr-graph-ontology.md) — aligns `EntityType` / `EdgeKind` well-known defaults with schema.org + PROV-O (canonical names live in `well_known.py`)
- [`../../src/trellis/classify/`](../../src/trellis/classify/) — the tagging pipeline module
- [`../../src/trellis/schemas/classification.py`](../../src/trellis/schemas/classification.py) — `ContentTags`, and (per the tag-vocabulary ADR) `DataClassification` / `Lifecycle`
- [`../../src/trellis/schemas/well_known.py`](../../src/trellis/schemas/well_known.py) — canonical entity-type and edge-kind constants + `canonicalize_*` helpers
- [`../../src/trellis/stores/registry.py`](../../src/trellis/stores/registry.py) — `_BUILTIN_BACKENDS` table
- [`../../CLAUDE.md`](../../CLAUDE.md) — project guide that indexes into this ADR

---

## 1. Context

Three ADRs are landing in parallel (tag-vocabulary split, storage planes and substrates, and the Client Boundary set). Each introduces or reuses vocabulary that overlaps with terms already in the code. Three collisions have high-enough severity that merging the ADRs without fixing them will bake ambiguity into the public surface — ADRs, CLAUDE.md, and the agent-facing guides.

### Collision 1 — "Classification" means three different things

- `src/trellis/classify/` is the **pipeline module** (`ClassifierPipeline`, deterministic + LLM classifiers).
- `ContentTags` lives in `src/trellis/schemas/classification.py` and is the existing **retrieval-shaping tag schema**.
- `DataClassification` (proposed in the tag-vocabulary split) will be a new **access-policy schema** in the same file.

A reader seeing "classification" in prose has no way to tell which is meant.

### Collision 2 — "Enrichment" is overloaded

- `EnrichmentService` (`trellis_workers.enrichment.service`) is a **specific worker class** that wraps LLM calls.
- `"enrichment"` is the **mode string** returned by `ClassifierPipeline.mode()` when an LLM classifier is configured.
- `docs/agent-guide/enriching-for-retrieval.md` uses "enriching" as **generic prose** for tagging content with `retrieval_affinity` — unrelated to both the service and the pipeline mode.

### Collision 3 — "Substrate" and "backend" risk becoming synonyms

The planes ADR introduces **substrate** as "the blessed default backend per plane". Without a sharp distinction, readers will treat it as a rename of `_BUILTIN_BACKENDS` entries (which this ADR does not intend).

### Non-collisions worth locking in

- **Advisory / feedback loop / dual-path** — CLAUDE.md already disambiguates EventLog (authoritative) vs JSONL (file-based) paths. This ADR reaffirms; no new decisions needed.
- **"Self-learning"** — zero occurrences in code or docs today. When it appears in issues or discussion, map to the established terms.

---

## 2. Decision — the canonical term map

### 2.1 Tagging pipeline (the artist formerly known as "classification pipeline")

| Term | Meaning |
|---|---|
| **Tagging pipeline** | Public, prose-level name for the classifier pipeline in `src/trellis/classify/`. Use this in documentation and conversation. |
| `src/trellis/classify/` | The module path. Kept as-is — renaming has high radius and the cost outweighs the benefit. |
| `ClassifierPipeline` | The class that orchestrates tagging. Canonical class name stays. |
| `Classifier` (Protocol) | A single tagging strategy (deterministic, LLM, etc.). |

### 2.2 Schemas in `classification.py`

| Term | Meaning | Role |
|---|---|---|
| `ContentTags` | Open-vocabulary retrieval-shaping tags (4 facets: `domain`, `content_type`, `scope`, `signal_quality`). | **Retrieval shaping.** Not policy. |
| `DataClassification` | Sensitivity + regulatory + jurisdiction labels. | **Access policy.** Gates unsafe outcomes. |
| `Lifecycle` | `current` / `deprecated` / `superseded` / `archived`. | **Staleness correctness.** Gates unsafe recommendations. |

All three schemas co-exist in `src/trellis/schemas/classification.py`. The filename stays; this ADR and the tag-vocabulary ADR are the authoritative note that the file contains both retrieval and policy schemas.

### 2.3 Pipeline modes

| Term | Meaning |
|---|---|
| **Ingestion mode** | Deterministic-only tagging, inline at ingest (microseconds). |
| **Enrichment mode** | Deterministic + LLM fallback, async for items where deterministic confidence falls below threshold. |
| `EnrichmentService` | The worker class in `trellis_workers.enrichment.service` that performs LLM-backed tagging inside enrichment mode. |

The word **"enrichment" in this project means pipeline-mode + its LLM worker class — nothing else.** Prose that wants to say "add metadata to content" should use *tag*, *annotate*, or *label*.

### 2.4 Storage planes and substrates

| Term | Meaning |
|---|---|
| **Knowledge Plane** | Stores agents read and write through the sanctioned bridges: `GraphStore`, `VectorStore`, `DocumentStore`, `BlobStore`. |
| **Operational Plane** | Stores Trellis talks to itself through: `TraceStore`, `EventLog`. Not populated by client systems. |
| **Substrate** | The **blessed default** backend for a given store within a plane. Documentation-level concept; one per store per plane. |
| **Backend** | Any implementation class registered in `_BUILTIN_BACKENDS`. Code-level concept; many per store. |

A substrate is *a* backend, but not every backend is blessed as the substrate for its plane. Substrate answers "what you get with zero configuration"; backend answers "what is registered in the table".

### 2.5 Graph ontology — entity types and edge kinds

| Term | Meaning |
|---|---|
| **Canonical name** | The schema.org / PROV-O–aligned form of an entity type or edge kind (e.g., `Person`, `SoftwareApplication`, `wasGeneratedBy`). Lives as a constant in [`schemas/well_known.py`](../../src/trellis/schemas/well_known.py). The form new code should emit. |
| **Legacy alias** | The lowercase / snake_case form on the original `EntityType` / `EdgeKind` enums (e.g., `person`, `system`, `trace_used_evidence`). Permanent — never removed, never repurposed. Resolves to a canonical via `canonicalize_entity_type` / `canonicalize_edge_kind`. |
| **Well-known default** | A canonical name defined in `well_known.py`. Trellis ships these as the recommended starting vocabulary; **type strings remain open** at the storage and API layers. Domain extensions (data platforms, infrastructure, etc.) define their own types in their own packages. |
| **Open-string type** | Any entity-type or edge-kind value that is neither a canonical name nor a registered legacy alias. Passes through `canonicalize_*` unchanged. Fully supported by storage and retrieval. |

The pair (canonical, alias) is **bijective by design**: every alias resolves to exactly one canonical, and no string is both. Multiple aliases may collapse onto the same canonical (e.g., `system` / `service` / `tool` all map to `SoftwareApplication`). Drift guards live in [`tests/unit/schemas/test_well_known.py`](../../tests/unit/schemas/test_well_known.py).

The single deliberate exception: the legacy `entity_type="domain"` is **not** aliased — it has been dropped from the canonical defaults because it collides with `ContentTags.domain` (the classification facet). Existing data using `entity_type="domain"` keeps working as an open-string type; new code should not emit it.

### 2.6 Feedback loop

| Term | Meaning |
|---|---|
| **Feedback loop** | The dual-path system described in CLAUDE.md — EventLog (authoritative, automated) + JSONL (file-based, human-in-the-loop). |
| `AdvisoryGenerator` | Produces deterministic, evidence-backed suggestions from outcome data. |
| **Advisory** | An individual suggestion produced by the generator. Schema in `schemas/advisory.py`. |
| **Effectiveness analysis** | The grading step that reads feedback from the EventLog and computes retrieval effectiveness metrics. |
| **Self-learning** | **Not a project term.** Map to *feedback loop* + *advisory generator* when encountered in discussion or the issue tracker. |

---

## 3. Cross-references and downstream actions

| Change | Purpose |
|---|---|
| Rename `docs/agent-guide/enriching-for-retrieval.md` → `tagging-for-retrieval.md` | Removes the "enriching" prose collision with pipeline-mode enrichment. |
| Update references in `TODO.md`, `docs/agent-guide/README.md`, `docs/agent-guide/modeling-guide.md` | Keeps navigation working after rename; renames the TODO item from "Classification guide" to "Tagging guide". |
| Add `## Terminology` section in `CLAUDE.md` | Single-paragraph glossary indexing into this ADR. |
| Cross-reference this ADR from `adr-tag-vocabulary-split.md` and `adr-planes-and-substrates.md` | Ensures the terms those ADRs introduce point back to canonical definitions. Done in later PRs that introduce/modify those ADRs. |

## 4. Consequences

**Positive**
- Ambiguous terms get single meanings before the tag-vocabulary and planes-and-substrates ADRs merge.
- CLAUDE.md glossary gives new readers a one-click entry point.
- Later contributors can grep a canonical term and land on both this ADR and the code.

**Cost**
- One ADR + a file rename + three link updates + a short CLAUDE.md section.
- No code change; no test change.

**Deliberately not covered**
- **Future module renames** (e.g. `src/trellis/classify/` → `src/trellis/tag/`). This ADR locks what current names *mean*; it does not close the door on a later rename if the cost/benefit shifts.
- **Domain / plugin package conventions.** Downstream packages (data-platform integrations, domain-specific extractors) should follow this map but are not bound by it at the type level.
