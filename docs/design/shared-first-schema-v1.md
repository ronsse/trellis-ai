# Shared-First Schema V1

## Purpose

Define a lean schema direction for Trellis that supports:

- shared canonical memory
- observable retrieval packs
- shared instruction binding
- immutable execution traces
- scored outcomes and feedback
- safe schema evolution

This design is intentionally conservative. It should support future systems, including search-based or Darwinian LLM workflows, by extension rather than by pre-modeling every possible optimization concept.

## Core Principle

Optimize the graph for **safe extension**, not for all foreseeable use cases.

That means a small number of stable primitives should be first-class now, while specialized future workflows should attach to those primitives until repeated usage justifies promotion into dedicated schema objects.

## Stable Primitives To Keep First-Class

### Canonical Memory

- `entity`
- `entity_alias`
- `edge`
- `document`
- `trace`
- `precedent`
- `policy`

### Retrieval Memory

- `context_pack`
- `context_pack_item`
- `retrieval_policy`

### Shared Agent Behavior

- `instruction_bundle`
- `instruction_binding`

### Learning Signals

- `feedback_event`
- `assessment`

## Why These Primitives Matter

### 1. Canonical identity is expensive to retrofit

If the graph is expected to reconcile multiple source systems, aliasing and crosswalk support should be explicit from the start.

Without first-class identity reconciliation, every integration worker has to reinvent resolution logic for the same object.

Examples:

- Unity Catalog table to dbt model
- git file to dbt model
- runtime job to source table
- external system object to canonical entity

### 2. Retrieval must be observable

If packs are only ephemeral responses, the system cannot learn which context helped and which context was noise.

Making packs first-class allows the graph to record:

- what was requested
- what candidates were considered
- what was included
- why it was selected
- what policy version shaped the pack
- what outcome followed

This is the substrate for future curator-agent tuning.

### 3. Instructions should be shared and versioned

Shared instruction bundles allow agents and skills to consume governed behavior without scattering prompt logic across clients and integrations.

Instruction bindings should support:

- agent-specific targeting
- skill-specific targeting
- task-type targeting
- domain targeting
- versioned rollout

### 4. Scored outcomes are a general need

A generic `assessment` object is worth adding early.

It should support:

- verifier scores
- pass/fail gates
- benchmark metrics
- retrieval effectiveness signals
- future fitness values for search-based systems

This is a better early abstraction than adding first-class `candidate`, `generation`, or `selection_event` objects before they are needed.

## Recommended V1 Logical Objects

### `entity`

Required concepts:

- stable `entity_id`
- `entity_type`
- `canonical_name`
- `source_of_truth`
- `status`
- `properties`
- temporal fields

### `entity_alias`

Required concepts:

- `entity_id`
- `source_system`
- `raw_id`
- `raw_name`
- `match_confidence`
- `is_primary`

### `edge`

Required concepts:

- `source_entity_id`
- `target_entity_id`
- `edge_type`
- `source_of_truth`
- `properties`
- temporal fields

### `document`

Required concepts:

- `content_uri`
- `searchable_text`
- `metadata`
- optional entity linkage

### `trace`

Required concepts:

- immutable `trace_id`
- `source`
- `intent`
- `context`
- ordered `steps`
- `metadata`
- optional `outcome`, `evidence_used`, and `artifacts_produced`

### `context_pack`

Required concepts:

- `pack_id`
- request intent
- agent and skill identity
- policy version used
- token or size budget
- target entities
- creation time

### `context_pack_item`

Required concepts:

- `pack_id`
- item type and item id
- `included`
- `rank`
- `selection_reason`
- `score_breakdown`
- `estimated_tokens`

### `feedback_event`

Required concepts:

- target type and id
- feedback kind
- rating or label
- source trace if applicable
- who gave it and when

### `assessment`

Required concepts:

- subject type and id
- assessment type
- score
- metrics
- evaluator
- optional evaluation context

### `precedent`

Required concepts:

- title and description
- confidence
- applicability
- source trace references
- lifecycle status

### `retrieval_policy`

Required concepts:

- applicability scope
- ranking weights
- filters
- budget rules
- version and status

### `instruction_bundle`

Required concepts:

- bundle type
- target scope
- version
- lifecycle status
- content

### `instruction_binding`

Required concepts:

- bundle reference
- targeting rules
- priority
- temporal/version fields

## V1 Boundaries

The following concepts should remain out of scope for the core schema until real product pressure emerges:

- `experiment`
- `candidate`
- `generation`
- `selection_event`
- `mutation_event`
- `agent_private_memory`
- optimizer-specific lineage objects

These can be represented initially through:

- `trace.metadata`
- `assessment`
- `document`
- `entity`
- `context_pack`

If multiple workflows need the same concept repeatedly, promote it later into a first-class object.

## Schema Evolution Rule

Add a specialized schema object only when:

1. multiple workflows need it
2. its lifecycle cannot be expressed cleanly through existing primitives
3. it materially improves retrieval, curation, or governance

Until then:

- store specialized semantics in stable primitives
- keep identity, provenance, and versioning strict
- prefer additive evolution over replacement

## Implications For Trellis

If Trellis adopts this direction, the most valuable upstream improvements are:

1. first-class alias/crosswalk support
2. richer `context_pack` and `context_pack_item` observability
3. first-class `instruction_bundle` and `instruction_binding`
4. a generic `assessment` object for scored outcomes

These improvements would support current use cases while also making the platform a better substrate for future curation and optimization systems.
