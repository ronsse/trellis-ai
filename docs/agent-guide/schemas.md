# Schema Catalog

Machine-readable reference for all Trellis Pydantic schemas. All models use `extra="forbid"` -- unrecognized fields cause validation errors.

Base schema version: `0.1.0`

> **Before designing an ingestion runner or domain schema, read [modeling-guide.md](modeling-guide.md) first.** This catalog tells you what fields exist; the modeling guide tells you how to decide what should become a node, a property, or a document — and how to avoid the graph-inflation anti-patterns we've seen in the wild. The decisions you make at modeling time are hard to undo later.

---

## Trace

Immutable record of an agent or workflow execution.

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `trace_id` | `string` | No | ULID | Unique identifier |
| `source` | `TraceSource` | **Yes** | -- | Producer type |
| `intent` | `string` | **Yes** | -- | What this trace accomplished |
| `steps` | `list[TraceStep]` | No | `[]` | Steps executed |
| `evidence_used` | `list[EvidenceRef]` | No | `[]` | Evidence consumed |
| `artifacts_produced` | `list[ArtifactRef]` | No | `[]` | Artifacts created |
| `outcome` | `Outcome` or `null` | No | `null` | Execution outcome |
| `feedback` | `list[Feedback]` | No | `[]` | Quality feedback |
| `context` | `TraceContext` | **Yes** | -- | Execution context |
| `metadata` | `dict` | No | `{}` | Arbitrary key-value pairs |
| `schema_version` | `string` | No | `"0.1.0"` | Schema version |
| `created_at` | `datetime` | No | UTC now | Creation timestamp |
| `updated_at` | `datetime` | No | UTC now | Update timestamp |

Related: TraceStep, Outcome, Feedback, TraceContext, EvidenceRef, ArtifactRef

```json
{
  "source": "agent",
  "intent": "Fix broken import in auth module",
  "steps": [
    {"step_type": "tool_call", "name": "edit_file", "args": {"file": "auth.py"}, "result": {"status": "applied"}, "duration_ms": 150}
  ],
  "outcome": {"status": "success", "summary": "Import fixed"},
  "context": {"agent_id": "code-orchestrator", "domain": "backend"}
}
```

---

## TraceStep

A single step within a trace.

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `step_type` | `string` | **Yes** | -- | Step category (e.g., `tool_call`, `llm_call`, `decision`, `observation`) |
| `name` | `string` | **Yes** | -- | Step name |
| `args` | `dict` | No | `{}` | Input arguments |
| `result` | `dict` | No | `{}` | Output result |
| `error` | `string` or `null` | No | `null` | Error message if failed |
| `duration_ms` | `int` or `null` | No | `null` | Duration in milliseconds |
| `started_at` | `datetime` | No | UTC now | When step started |
| `schema_version` | `string` | No | `"0.1.0"` | Schema version |

```json
{
  "step_type": "tool_call",
  "name": "search_codebase",
  "args": {"query": "database pool", "file_pattern": "*.py"},
  "result": {"matches": 5, "files": ["db/pool.py", "db/config.py"]},
  "duration_ms": 320
}
```

---

## Outcome

Outcome of a trace execution.

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `status` | `OutcomeStatus` | No | `"unknown"` | Outcome status |
| `metrics` | `dict` | No | `{}` | Quantitative metrics |
| `summary` | `string` or `null` | No | `null` | Brief summary |
| `schema_version` | `string` | No | `"0.1.0"` | Schema version |

```json
{
  "status": "partial",
  "metrics": {"tests_passed": 18, "tests_failed": 2},
  "summary": "Deployed but 2 smoke tests failed"
}
```

---

## Feedback

Quality feedback on a trace or precedent.

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `feedback_id` | `string` | No | ULID | Unique identifier |
| `rating` | `float` or `null` | No | `null` | Quality score (0.0 to 1.0 by convention) |
| `label` | `string` or `null` | No | `null` | Categorical label |
| `comment` | `string` or `null` | No | `null` | Free-text comment |
| `given_by` | `string` | No | `"unknown"` | Who provided feedback |
| `given_at` | `datetime` | No | UTC now | When feedback was given |
| `schema_version` | `string` | No | `"0.1.0"` | Schema version |

```json
{
  "rating": 0.85,
  "label": "good",
  "comment": "Clean approach, well-tested",
  "given_by": "tech-lead"
}
```

---

## TraceContext

Context in which a trace was executed.

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `agent_id` | `string` or `null` | No | `null` | Agent identifier |
| `team` | `string` or `null` | No | `null` | Team or group |
| `domain` | `string` or `null` | No | `null` | Domain scope |
| `workflow_id` | `string` or `null` | No | `null` | Workflow identifier |
| `parent_trace_id` | `string` or `null` | No | `null` | Parent trace for nesting |
| `started_at` | `datetime` | No | UTC now | Execution start |
| `ended_at` | `datetime` or `null` | No | `null` | Execution end |
| `schema_version` | `string` | No | `"0.1.0"` | Schema version |

```json
{
  "agent_id": "code-orchestrator",
  "domain": "backend",
  "team": "platform",
  "started_at": "2026-03-10T14:00:00Z",
  "ended_at": "2026-03-10T14:05:00Z"
}
```

---

## EvidenceRef

Reference to evidence used or produced by a trace.

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `evidence_id` | `string` | **Yes** | -- | Evidence identifier |
| `role` | `string` | No | `"input"` | Role (e.g., `input`, `reference`, `context`) |
| `schema_version` | `string` | No | `"0.1.0"` | Schema version |

```json
{"evidence_id": "01JRK6M3QF8GHTM2XVZP3CWD9E", "role": "input"}
```

---

## ArtifactRef

Reference to an artifact produced by a trace.

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `artifact_id` | `string` | **Yes** | -- | Artifact identifier |
| `artifact_type` | `string` | **Yes** | -- | Type (e.g., `file`, `pr`, `note`, `entity`, `deployment`) |
| `schema_version` | `string` | No | `"0.1.0"` | Schema version |

```json
{"artifact_id": "pr_847", "artifact_type": "pr"}
```

---

## Entity

A named entity in the experience graph.

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `entity_id` | `string` | No | ULID | Unique identifier |
| `entity_type` | `string` | **Yes** | -- | Entity type (any string; see [well-known values](#entitytype-well-known-values)) |
| `name` | `string` | **Yes** | -- | Display name |
| `properties` | `dict` | No | `{}` | Arbitrary properties |
| `source` | `EntitySource` or `null` | No | `null` | Origin information |
| `metadata` | `dict` | No | `{}` | Arbitrary metadata |
| `node_role` | `NodeRole` | No | `"semantic"` | Role in the graph: `structural`, `semantic`, or `curated` (see [NodeRole](#noderole)) |
| `generation_spec` | `GenerationSpec` or `null` | No | `null` | Provenance record. **Required** when `node_role="curated"`, **must be null** otherwise |
| `schema_version` | `string` | No | `"0.1.0"` | Schema version |
| `created_at` | `datetime` | No | UTC now | Creation timestamp |
| `updated_at` | `datetime` | No | UTC now | Update timestamp |

> **`node_role` is immutable across SCD Type 2 versions.** Once an entity is created with a given role, subsequent `upsert_node` calls must preserve it or the store will reject the mutation. See the [modeling guide](modeling-guide.md) for guidance on choosing the right role.

```json
{
  "entity_type": "service",
  "name": "auth-service",
  "properties": {"language": "python", "team": "platform", "tier": "critical"},
  "source": {"origin": "manual", "detail": "Registered during onboarding"},
  "node_role": "semantic"
}
```

Structural example (schema-level metadata that should not participate in semantic retrieval):

```json
{
  "entity_type": "uc_column",
  "name": "orders.customer_id",
  "properties": {"table": "orders", "dtype": "bigint"},
  "node_role": "structural"
}
```

Curated example (derived knowledge with full provenance):

```json
{
  "entity_type": "domain",
  "name": "payments",
  "properties": {"summary": "Services and data assets that handle money movement"},
  "node_role": "curated",
  "generation_spec": {
    "generator_name": "community_detection_louvain",
    "generator_version": "1.0.0",
    "source_node_ids": ["ent_svc_auth", "ent_svc_billing", "ent_svc_ledger"],
    "parameters": {"resolution": 1.2}
  }
}
```

---

## EntitySource

Origin information for an entity.

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `origin` | `string` | **Yes** | -- | Where this entity came from (e.g., `manual`, `trace`, `ingestion`) |
| `detail` | `string` or `null` | No | `null` | Additional detail |
| `trace_id` | `string` or `null` | No | `null` | Trace that created this entity |
| `schema_version` | `string` | No | `"0.1.0"` | Schema version |

```json
{"origin": "trace", "trace_id": "01JRK5N7QF8GHTM2XVZP3CWD9E"}
```

---

## GenerationSpec

Provenance record for curated nodes. A curated node is one that was derived by a named generator (community detection, precedent promotion, clustering, etc.) rather than ingested from an external source. `generation_spec` is **required** on any entity with `node_role="curated"` and **must be null** for `structural` or `semantic` nodes.

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `generator_name` | `string` | **Yes** | -- | Identifier of the generator (e.g., `community_detection_louvain`, `precedent_promotion`) |
| `generator_version` | `string` | **Yes** | -- | Version string of the generator run |
| `generated_at` | `datetime` | No | UTC now | When the generator produced this node |
| `source_node_ids` | `list[string]` | No | `[]` | Entities that fed into the generator |
| `source_trace_ids` | `list[string]` | No | `[]` | Traces that fed into the generator |
| `parameters` | `dict` | No | `{}` | Generator parameters used for this run |
| `schema_version` | `string` | No | `"0.1.0"` | Schema version |

```json
{
  "generator_name": "precedent_promotion",
  "generator_version": "1.0.0",
  "source_trace_ids": ["01JRK5N7QF8GHTM2XVZP3CWD9E"],
  "parameters": {"min_confidence": 0.8}
}
```

---

## ContentTags

Classification tags attached to any stored item. Four orthogonal facets for pre-filtered retrieval.

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `domain` | `list[string]` | No | `[]` | Multi-label domain tags (extensible, no controlled vocabulary at schema level) |
| `content_type` | `string` or `null` | No | `null` | Single-label: `pattern`, `decision`, `error-resolution`, `discovery`, `procedure`, `constraint`, `configuration`, `code`, `documentation` |
| `scope` | `string` or `null` | No | `null` | Single-label: `universal`, `org`, `project`, `ephemeral` |
| `signal_quality` | `string` | No | `"standard"` | Computed: `high`, `standard`, `low`, `noise` |
| `custom` | `dict[string, list[string]]` | No | `{}` | Extension point for domain-specific facets |
| `classified_by` | `list[string]` | No | `[]` | Audit trail: which classifiers produced these tags |
| `classification_version` | `string` | No | `"1"` | Schema version for re-classification |

```json
{
  "domain": ["data-pipeline", "infrastructure"],
  "content_type": "error-resolution",
  "scope": "project",
  "signal_quality": "high",
  "classified_by": ["structural", "keyword_domain"],
  "classification_version": "1"
}
```

ContentTags are embedded in metadata/properties JSON on documents, entities, traces, and evidence. Use `tag_filters` on `PackBuilder.build()` or `content_tags` key in store `search()` filters.

### Reserved namespaces

Certain keys in `custom` and values in `domain` are reserved — the schema validator rejects them with an instructive error pointing to the correct destination. See [adr-tag-vocabulary-split.md](../design/adr-tag-vocabulary-split.md) for the full decision record and per-namespace definitions.

| Reserved name | Goes in | Answers |
|---|---|---|
| `sensitivity` | `DataClassification.sensitivity` | Who may see this content |
| `regulatory` | `DataClassification.regulatory_tags` | What compliance frameworks govern it |
| `jurisdiction` | `DataClassification.jurisdiction` | Where it applies / where the viewer must be |
| `lifecycle` | `Lifecycle.state` | Temporal validity state |
| `authority` | (derived from graph position) | Canonical vs. community provenance |
| `retention` | `Policy` with `PolicyType.RETENTION` | How long to keep |
| `redaction` | `Policy` with `PolicyType.REDACTION` | Field-level masking rules |

Both the bare form (`"sensitivity"`) and the namespaced form (`"sensitivity:pii"`) are rejected. Substring matches (`"sensitivity-aware"`) are allowed — reservation applies to `name` and `name:*` only.

`DataClassification` and `Lifecycle` are defined as first-class schemas but are **not required** in the current phase. Consumers can populate them explicitly; no classifier produces them by default and no policy gate enforces them yet. See [adr-tag-vocabulary-split.md](../design/adr-tag-vocabulary-split.md) for the phased rollout.

---

## DataClassification

Access-policy-relevant classification. Separate from `ContentTags` because sensitivity and regulatory tags gate *access* and *compliance*, not retrieval ranking.

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `sensitivity` | `string` | No | `"internal"` | One of `public`, `internal`, `confidential`, `restricted` |
| `regulatory_tags` | `list[string]` | No | `[]` | Open list: `pii`, `phi`, `pci`, `gdpr`, `hipaa`, `export-controlled`, custom values |
| `jurisdiction` | `list[string]` | No | `[]` | ISO country/region codes where content applies |
| `classified_by` | `list[string]` | No | `[]` | Audit trail |
| `classification_version` | `string` | No | `"1"` | Schema version |

---

## Lifecycle

Temporal validity state of content. Separate from `ContentTags.signal_quality` because quality captures "retrieve at all" while lifecycle captures "is this current / deprecated / superseded".

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `state` | `string` | No | `"current"` | One of `draft`, `current`, `deprecated`, `superseded`, `archived` |
| `valid_from` | `datetime` or `null` | No | `null` | When content became valid |
| `valid_until` | `datetime` or `null` | No | `null` | When content expires |
| `superseded_by` | `string` or `null` | No | `null` | Replacement node/item ID |
| `deprecation_reason` | `string` or `null` | No | `null` | Free text |
| `classification_version` | `string` | No | `"1"` | Schema version |

---

## EntityAlias

Cross-system identifier mapped onto a canonical entity.

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `alias_id` | `string` | No | ULID | Unique identifier |
| `entity_id` | `string` | **Yes** | -- | Canonical entity identifier |
| `source_system` | `string` | **Yes** | -- | Source namespace such as `unity_catalog`, `dbt`, or `git` |
| `raw_id` | `string` | **Yes** | -- | Native identifier in the source system |
| `raw_name` | `string` or `null` | No | `null` | Human-readable source name |
| `match_confidence` | `float` | No | `1.0` | Confidence in the mapping |
| `is_primary` | `bool` | No | `false` | Whether this alias is the preferred alias for that source |
| `schema_version` | `string` | No | `"0.1.0"` | Schema version |
| `created_at` | `datetime` | No | UTC now | Creation timestamp |
| `updated_at` | `datetime` | No | UTC now | Update timestamp |

```json
{
  "entity_id": "01JRK5N7QF8GHTM2XVZP3CWD9E",
  "source_system": "unity_catalog",
  "raw_id": "main.analytics.orders",
  "raw_name": "orders",
  "match_confidence": 0.97,
  "is_primary": true
}
```

---

## Evidence

A piece of evidence supporting traces, precedents, or entities. The `content_hash` is auto-computed from `content` if not provided.

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `evidence_id` | `string` | No | ULID | Unique identifier |
| `evidence_type` | `EvidenceType` | **Yes** | -- | Type of evidence |
| `content` | `string` or `null` | No | `null` | Text content |
| `uri` | `string` or `null` | No | `null` | URI or file path |
| `content_hash` | `string` | No | `""` (auto-computed) | SHA-256 hash prefix of content |
| `source_origin` | `string` | **Yes** | -- | Where this evidence came from (`trace`, `manual`, `ingestion`) |
| `source_trace_id` | `string` or `null` | No | `null` | Trace that produced this evidence |
| `attached_to` | `list[AttachmentRef]` | No | `[]` | What this evidence is attached to |
| `metadata` | `dict` | No | `{}` | Arbitrary metadata |
| `schema_version` | `string` | No | `"0.1.0"` | Schema version |
| `created_at` | `datetime` | No | UTC now | Creation timestamp |
| `updated_at` | `datetime` | No | UTC now | Update timestamp |

Related: AttachmentRef

```json
{
  "evidence_type": "snippet",
  "content": "Max connection pool size should be 20 per process with 30s idle timeout",
  "source_origin": "manual",
  "uri": "https://wiki.internal/db-guidelines#connection-pooling"
}
```

---

## AttachmentRef

Reference linking evidence to a target object.

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `target_id` | `string` | **Yes** | -- | Target object ID |
| `target_type` | `string` | **Yes** | -- | Target type (`trace`, `entity`, `precedent`) |
| `schema_version` | `string` | No | `"0.1.0"` | Schema version |

```json
{"target_id": "01JRK5N7QF8GHTM2XVZP3CWD9E", "target_type": "trace"}
```

---

## Precedent

Reusable institutional knowledge distilled from one or more traces.

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `precedent_id` | `string` | No | ULID | Unique identifier |
| `source_trace_ids` | `list[string]` | No | `[]` | Traces this was derived from |
| `title` | `string` | **Yes** | -- | Precedent title |
| `description` | `string` | **Yes** | -- | Detailed description of the pattern |
| `pattern` | `string` or `null` | No | `null` | Formal pattern description |
| `applicability` | `list[string]` | No | `[]` | Where this precedent applies |
| `confidence` | `float` | No | `0.0` | Confidence score (0.0 to 1.0) |
| `promoted_by` | `string` | **Yes** | -- | Who promoted this precedent |
| `evidence_refs` | `list[string]` | No | `[]` | Supporting evidence IDs |
| `feedback` | `list[Feedback]` | No | `[]` | Quality feedback |
| `metadata` | `dict` | No | `{}` | Arbitrary metadata |
| `schema_version` | `string` | No | `"0.1.0"` | Schema version |
| `created_at` | `datetime` | No | UTC now | Creation timestamp |
| `updated_at` | `datetime` | No | UTC now | Update timestamp |

```json
{
  "title": "Zero-downtime column addition pattern",
  "description": "Add nullable column with DEFAULT NULL, deploy code handling both states, backfill in batches",
  "source_trace_ids": ["01JRK5N7QF8GHTM2XVZP3CWD9E"],
  "applicability": ["database", "migration", "production"],
  "confidence": 0.9,
  "promoted_by": "code-orchestrator"
}
```

---

## Pack

A context pack assembled for an agent or workflow.

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `pack_id` | `string` | No | ULID | Unique identifier |
| `intent` | `string` | **Yes** | -- | Intent for pack assembly |
| `items` | `list[PackItem]` | No | `[]` | Included items |
| `retrieval_report` | `RetrievalReport` | No | Default report | How items were retrieved |
| `policies_applied` | `list[string]` | No | `[]` | Policies that were applied |
| `budget` | `PackBudget` | No | Default budget | Budget constraints |
| `domain` | `string` or `null` | No | `null` | Domain scope |
| `agent_id` | `string` or `null` | No | `null` | Agent scope |
| `skill_id` | `string` or `null` | No | `null` | Skill or capability requesting the pack |
| `target_entity_ids` | `list[string]` | No | `[]` | Canonical entities the pack is centered on |
| `assembled_at` | `datetime` | No | UTC now | When pack was assembled |
| `metadata` | `dict` | No | `{}` | Arbitrary metadata |
| `schema_version` | `string` | No | `"0.1.0"` | Schema version |
| `created_at` | `datetime` | No | UTC now | Creation timestamp |
| `updated_at` | `datetime` | No | UTC now | Update timestamp |

Related: PackItem, PackBudget, RetrievalReport

```json
{
  "intent": "Deploy checklist for staging",
  "domain": "platform",
  "agent_id": "deploy-agent",
  "skill_id": "release-playbook",
  "target_entity_ids": ["ent_service_api"],
  "budget": {"max_items": 20, "max_tokens": 8000},
  "items": [
    {
      "item_id": "01JRK5N7QF",
      "item_type": "precedent",
      "excerpt": "Always run smoke tests after deploy",
      "relevance_score": 0.95,
      "included": true,
      "rank": 1,
      "selection_reason": "selected_by_relevance",
      "score_breakdown": {"relevance_score": 0.95},
      "estimated_tokens": 10
    }
  ],
  "retrieval_report": {"queries_run": 2, "candidates_found": 15, "items_selected": 8, "strategies_used": ["keyword", "semantic"]}
}
```

---

## PackItem

A single item in a context pack.

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `item_id` | `string` | **Yes** | -- | Item identifier |
| `item_type` | `string` | **Yes** | -- | Type (`trace`, `evidence`, `precedent`, `entity`, `document`, `vector`) |
| `excerpt` | `string` | No | `""` | Text excerpt |
| `relevance_score` | `float` | No | `0.0` | Relevance score |
| `included` | `bool` | No | `true` | Whether the item was included in the final pack |
| `rank` | `int` or `null` | No | `null` | Final rank inside the assembled pack |
| `selection_reason` | `string` or `null` | No | `null` | Deterministic explanation for inclusion |
| `score_breakdown` | `dict[string, float]` | No | `{}` | Named scores contributing to selection |
| `estimated_tokens` | `int` or `null` | No | `null` | Estimated token cost of the item excerpt |
| `metadata` | `dict` | No | `{}` | Additional metadata |
| `schema_version` | `string` | No | `"0.1.0"` | Schema version |

```json
{
  "item_id": "01JRK5N7QF",
  "item_type": "trace",
  "excerpt": "Fixed auth import issue",
  "relevance_score": 0.87,
  "included": true,
  "rank": 2,
  "selection_reason": "selected_by_relevance",
  "score_breakdown": {"relevance_score": 0.87},
  "estimated_tokens": 6
}
```

---

## PackBudget

Budget constraints for a context pack.

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `max_items` | `int` | No | `50` | Maximum items |
| `max_tokens` | `int` | No | `8000` | Maximum estimated tokens |
| `schema_version` | `string` | No | `"0.1.0"` | Schema version |

```json
{"max_items": 20, "max_tokens": 4000}
```

---

## RetrievalReport

Report on how pack items were retrieved.

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `queries_run` | `int` | No | `0` | Number of queries executed |
| `candidates_found` | `int` | No | `0` | Total candidates before filtering |
| `items_selected` | `int` | No | `0` | Items that made it into the pack |
| `duration_ms` | `int` | No | `0` | Total retrieval time |
| `strategies_used` | `list[string]` | No | `[]` | Strategy names used (e.g., `keyword`, `semantic`, `graph`) |
| `schema_version` | `string` | No | `"0.1.0"` | Schema version |

```json
{"queries_run": 3, "candidates_found": 42, "items_selected": 12, "duration_ms": 150, "strategies_used": ["keyword", "semantic", "graph"]}
```

---

## Edge

A directed edge in the experience graph.

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `edge_id` | `string` | No | ULID | Unique identifier |
| `source_id` | `string` | **Yes** | -- | Source node ID |
| `target_id` | `string` | **Yes** | -- | Target node ID |
| `edge_kind` | `string` | **Yes** | -- | Relationship type (any string; see [well-known values](#edgekind-well-known-values)) |
| `properties` | `dict` | No | `{}` | Arbitrary properties |
| `schema_version` | `string` | No | `"0.1.0"` | Schema version |
| `created_at` | `datetime` | No | UTC now | Creation timestamp |
| `updated_at` | `datetime` | No | UTC now | Update timestamp |

```json
{
  "source_id": "01JRK5N7QF",
  "target_id": "01JRK6M3QF",
  "edge_kind": "entity_depends_on",
  "properties": {"strength": "hard"}
}
```

---

## Policy

Governance policy controlling operations in the experience graph.

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `policy_id` | `string` | No | ULID | Unique identifier |
| `policy_type` | `PolicyType` | **Yes** | -- | Policy type |
| `scope` | `PolicyScope` | **Yes** | -- | Where the policy applies |
| `rules` | `list[PolicyRule]` | No | `[]` | Policy rules |
| `enforcement` | `Enforcement` | No | `"enforce"` | Enforcement level |
| `metadata` | `dict` | No | `{}` | Arbitrary metadata |
| `schema_version` | `string` | No | `"0.1.0"` | Schema version |
| `created_at` | `datetime` | No | UTC now | Creation timestamp |
| `updated_at` | `datetime` | No | UTC now | Update timestamp |

Related: PolicyScope, PolicyRule

```json
{
  "policy_type": "mutation",
  "scope": {"level": "domain", "value": "production"},
  "rules": [
    {"operation": "entity.create", "condition": "always", "action": "require_approval"}
  ],
  "enforcement": "enforce"
}
```

---

## PolicyScope

Scope at which a policy applies.

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `level` | `string` | **Yes** | -- | Scope level (`global`, `domain`, `team`, `entity_type`) |
| `value` | `string` or `null` | No | `null` | Scope value (e.g., domain name) |
| `schema_version` | `string` | No | `"0.1.0"` | Schema version |

```json
{"level": "domain", "value": "payments"}
```

---

## PolicyRule

A single rule within a policy.

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `operation` | `string` | **Yes** | -- | Operation to match (e.g., `precedent.promote`, `*`) |
| `condition` | `string` | No | `"always"` | When the rule applies |
| `action` | `string` | No | `"allow"` | What to do (`allow`, `deny`, `require_approval`, `warn`) |
| `params` | `dict` | No | `{}` | Additional parameters |
| `schema_version` | `string` | No | `"0.1.0"` | Schema version |

```json
{"operation": "entity.create", "condition": "always", "action": "require_approval"}
```

---

## Command

A mutation command submitted to the governed write pipeline.

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `command_id` | `string` | No | ULID | Unique identifier |
| `operation` | `Operation` | **Yes** | -- | Mutation operation |
| `target_id` | `string` or `null` | No | `null` | Target object ID |
| `target_type` | `string` or `null` | No | `null` | Target object type |
| `args` | `dict` | No | `{}` | Operation arguments |
| `requested_by` | `string` | No | `"unknown"` | Who requested the mutation |
| `idempotency_key` | `string` or `null` | No | `null` | Key for deduplication |
| `metadata` | `dict` | No | `{}` | Arbitrary metadata |
| `created_at` | `datetime` | No | UTC now | When command was created |
| `schema_version` | `string` | No | `"0.1.0"` | Schema version |

```json
{
  "operation": "entity.create",
  "args": {"entity_type": "service", "name": "auth-service"},
  "requested_by": "code-orchestrator",
  "idempotency_key": "create_auth_20260310"
}
```

---

## CommandResult

Result of executing a command through the mutation pipeline.

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `command_id` | `string` | **Yes** | -- | Command that was executed |
| `status` | `CommandStatus` | **Yes** | -- | Outcome status |
| `operation` | `Operation` | **Yes** | -- | Operation that was executed |
| `target_id` | `string` or `null` | No | `null` | Target object ID |
| `created_id` | `string` or `null` | No | `null` | Newly created object ID |
| `message` | `string` | No | `""` | Result message |
| `warnings` | `list[string]` | No | `[]` | Policy warnings |
| `metadata` | `dict` | No | `{}` | Additional metadata |
| `executed_at` | `datetime` | No | UTC now | When command was executed |
| `schema_version` | `string` | No | `"0.1.0"` | Schema version |

```json
{
  "command_id": "01JRK7A3QF8GHTM2XVZP3CWD9E",
  "status": "success",
  "operation": "entity.create",
  "created_id": "01JRK7A4QF8GHTM2XVZP3CWD9E",
  "message": "Entity created"
}
```

---

## Enums

### TraceSource

| Value | Description |
|-------|-------------|
| `agent` | AI agent execution |
| `human` | Human action |
| `workflow` | Automated workflow |
| `system` | System-level operation |

### OutcomeStatus

| Value | Description |
|-------|-------------|
| `success` | All goals achieved |
| `failure` | Goals not achieved |
| `partial` | Some goals achieved |
| `unknown` | Outcome not determined |

### EntityType (well-known values)

The graph store accepts **any string** for entity types. The values below are well-known types used by the core agent tools. Domain-specific integrations (data platforms, infrastructure, etc.) should define their own types in their own package — they do not need to be added here.

| Value | Description |
|-------|-------------|
| `person` | Human individual |
| `system` | Software system |
| `service` | Microservice or API |
| `team` | Team or group |
| `document` | Document or specification |
| `concept` | Abstract concept |
| `domain` | Business domain |
| `file` | File or path |
| `project` | Project |
| `tool` | Tool or utility |

Examples of domain-specific types (not exhaustive): `uc_table`, `uc_schema`, `uc_column`, `dbt_model`, `dbt_source`, `pipeline`, `job`, `notebook`.

### NodeRole

Role an entity plays in the graph. Used by retrieval to decide which nodes participate in semantic search and which live purely as schema/context.

| Value | Description |
|-------|-------------|
| `structural` | Schema-level metadata (e.g., columns, enum values, nested field paths). Excluded from `PackBuilder` and `GraphSearch` results by default — pass `include_structural=True` to opt in. Must have `generation_spec=None`. |
| `semantic` | The default. Entities that represent real-world things (services, people, tables, domains) ingested from external sources. Participate in retrieval. Must have `generation_spec=None`. |
| `curated` | Derived nodes produced by a named generator (community detection, precedent promotion, etc.). Carry a full [`GenerationSpec`](#generationspec) provenance record and receive a 1.3x relevance boost in graph retrieval. |

`node_role` is **immutable** across SCD Type 2 versions — once set on an entity, subsequent upserts must preserve it. See [modeling-guide.md](modeling-guide.md) for guidance on choosing roles.

### EvidenceType

| Value | Description |
|-------|-------------|
| `document` | Full document |
| `snippet` | Text excerpt |
| `link` | URL reference |
| `config` | Configuration data |
| `image` | Image file |
| `file_pointer` | File path reference |

### PolicyType

| Value | Description |
|-------|-------------|
| `mutation` | Controls write operations |
| `access` | Controls read access |
| `retention` | Controls data lifecycle |
| `redaction` | Controls content redaction |

### Enforcement

| Value | Description |
|-------|-------------|
| `enforce` | Block violations |
| `warn` | Allow but emit warning |
| `audit_only` | Log only, no enforcement |

### EdgeKind (well-known values)

The graph store accepts **any string** for edge types. The values below are well-known types used by the core agent tools. Domain-specific integrations should define their own edge types as needed.

| Value | Description |
|-------|-------------|
| `trace_used_evidence` | Trace consumed this evidence |
| `trace_produced_artifact` | Trace created this artifact |
| `trace_touched_entity` | Trace interacted with this entity |
| `trace_promoted_to_precedent` | Trace was promoted to this precedent |
| `entity_related_to` | General entity relationship |
| `entity_part_of` | Entity is a component of another |
| `entity_depends_on` | Entity depends on another |
| `evidence_attached_to` | Evidence is attached to a target |
| `evidence_supports` | Evidence supports a claim |
| `precedent_applies_to` | Precedent applies to a domain or entity |
| `precedent_derived_from` | Precedent was derived from a source |

Examples of domain-specific edge types: `reads_from`, `writes_to`, `materializes_to`, `defined_in`, `owned_by`.

### Operation

| Value | Category | Description |
|-------|----------|-------------|
| `trace.ingest` | Ingest | Ingest a full trace |
| `trace.append_step` | Ingest | Append a step to a trace |
| `trace.record_outcome` | Ingest | Record a trace outcome |
| `evidence.ingest` | Ingest | Ingest evidence |
| `evidence.attach` | Ingest | Attach evidence to a target |
| `precedent.promote` | Curate | Promote trace to precedent |
| `precedent.update` | Curate | Update a precedent |
| `entity.create` | Curate | Create an entity |
| `entity.update` | Curate | Update an entity |
| `entity.merge` | Curate | Merge two entities |
| `link.create` | Curate | Create a graph edge |
| `link.remove` | Curate | Remove a graph edge |
| `label.add` | Curate | Add a label |
| `label.remove` | Curate | Remove a label |
| `feedback.record` | Curate | Record feedback |
| `redaction.apply` | Maintain | Redact content |
| `retention.prune` | Maintain | Run retention pruning |
| `pack.publish` | Maintain | Publish a context pack |
| `pack.invalidate` | Maintain | Invalidate a pack |

### CommandStatus

| Value | Description |
|-------|-------------|
| `success` | Command executed successfully |
| `rejected` | Policy gate rejected the command |
| `failed` | Execution failed (validation or handler error) |
| `duplicate` | Idempotency key already seen |

### BatchStrategy

| Value | Description |
|-------|-------------|
| `sequential` | Execute all commands in order |
| `stop_on_error` | Stop on first failure or rejection |
| `continue_on_error` | Execute all, collect all results |
