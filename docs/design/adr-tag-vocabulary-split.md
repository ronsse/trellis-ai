# ADR: Tag Vocabulary Split — Policy-Relevant Schemas vs Flexible Tags

**Status:** Proposed
**Date:** 2026-04-18
**Deciders:** Trellis core
**Related:**
- [`../../src/trellis/schemas/classification.py`](../../src/trellis/schemas/classification.py) — current `ContentTags` schema
- [`../../src/trellis/schemas/enums.py`](../../src/trellis/schemas/enums.py) — `EntityType`, `EdgeKind` (explicit "well-known defaults, not a closed set")
- [`../../src/trellis/classify/`](../../src/trellis/classify/) — classifier pipeline + four deterministic classifiers
- [`../../src/trellis/retrieve/pack_builder.py`](../../src/trellis/retrieve/pack_builder.py) — filters by tags before similarity scoring
- [`../../CLAUDE.md`](../../CLAUDE.md) — extension-point policy for domain-specific types

---

## 1. Context

### What exists today

The `ContentTags` schema has five facets, all attached as a single annotation on items:

| Facet | Shape | Controlled? | Role |
|---|---|---|---|
| `domain` | `list[str]` | Open (any string) | What area of knowledge |
| `content_type` | `Literal` (9 values) | Closed enum | Shape of information |
| `scope` | `Literal` (4 values) | Closed enum | How broadly applicable |
| `signal_quality` | `Literal` (4 values) | Closed enum | Should this be retrieved at all |
| `retrieval_affinity` | `list[Literal]` (4 values) | Closed enum | Which retrieval tier |
| `custom` | `dict[str, list[str]]` | Open | Escape hatch |

`PackBuilder` filters on these tags pre-similarity. `compute_importance()` combines them with LLM base scores. `apply_noise_tags()` closes the feedback loop from effectiveness analysis.

### Where the current shape is biased

Three of the five facets were designed for agent-trace work and do not generalize well:

- **`content_type`** values (`pattern`, `decision`, `error-resolution`, `discovery`) are agent-trace framing. A SharePoint HR policy, a Unity Catalog table description, or a product-catalog chatbot source do not map cleanly — they collapse into `documentation` and lose signal.
- **`scope`** (`universal / org / project / ephemeral`) assumes a software-team model. Regulated enterprises speak in `department`, `business-unit`, `regulated-jurisdiction`; retail chatbots speak in `brand`, `region`, `channel`.
- **`retrieval_affinity`** is a closed four-value enum; any new retrieval tier requires a core schema change.

`domain` as an open list with namespace prefixes (`uc:governance`, `sp:legal`) is already the right shape. `signal_quality` and `custom` are domain-agnostic. Those stay.

### What is missing that cross-domain adopters will eventually need

Dimensions observed across enterprise knowledge-graph and agent-context use cases that have no first-class home today:

| Dimension | Example values | Why it matters |
|---|---|---|
| **Sensitivity** | `public / internal / confidential / restricted` | Gates *access policy*, not just retrieval |
| **Regulatory tags** | `pii / phi / pci / gdpr / export-controlled` | Compliance-driven filtering and redaction |
| **Lifecycle state** | `draft / current / deprecated / superseded / archived` | Confluence pages and UC assets rot; a chatbot recommending a deprecated procedure is a production incident |
| **Authority tier** | `canonical / sanctioned / community / unverified` | Hallucination control; weight ADRs differently from Slack threads |
| **Audience** | `engineer / analyst / executive / end-user` | Same source, different pack composition |
| **Intent** | `how-to / reference / explanation / troubleshooting` | Diátaxis framing; well-established in docs communities |
| **Modality** | `text / table / code / diagram / schema / log` | Drives retrieval strategy selection |

Today these would all go into `ContentTags.custom` with no validation, no policy enforcement, and no indexing.

### The decision to make

Do we:
- **(A)** Keep `ContentTags` as-is, add new facets by growing the single schema as needs emerge
- **(B)** Split `ContentTags` into multiple schemas along a principled axis, with a flexible tag layer for everything else
- **(C)** Do nothing — let adopters ship their own classifiers and store anything they need in metadata

---

## 2. Decision

**Option B: Split along the policy-relevance axis.** A dimension belongs in a first-class schema if, and only if, it being wrong or missing causes an *unsafe outcome* — unauthorized access, a deprecated procedure recommended in production, a compliance violation. Everything else stays in the flexible tag layer.

The axis is not `structured vs. free`. It is `policy-relevant vs. retrieval-shaping`.

### 2.1 Schemas

| Schema | Status | Purpose | Populated by |
|---|---|---|---|
| `ContentTags` | Existing, keep as flexible layer | Retrieval shaping, ranking, domain classification | Classifier pipeline |
| `DataClassification` | **New, first-class, optional in Phase 0** | Access policy, compliance gating | Dedicated classifier (future) or explicit mutation |
| `Lifecycle` | **New, first-class, optional in Phase 0** | Deprecation correctness, staleness handling | Dedicated classifier (future) or explicit mutation |

`DataClassification` passes the policy-relevance test: wrong sensitivity → wrong access decision. `Lifecycle` passes: wrong state → deprecated content recommended as current. Both cannot be safely retrofitted onto installed data once users depend on the system, so the schemas are defined now even though enforcement ships later.

### 2.2 What is explicitly *not* first-class

| Dimension | Why it stays in flex / derived |
|---|---|
| Authority tier | Derivable from graph position (`EDGE_PUBLISHED_IN canonical_ADR_folder`, sign-off edges) — not worth a dedicated schema |
| Audience / persona | Retrieval-shaping only, no policy consumer yet |
| Intent | Retrieval-shaping only, overlaps with `content_type` |
| Modality | Retrieval-shaping only; could graduate when a strategy explicitly dispatches on modality |

Any of these can graduate to first-class via the promotion path (§3.3) when a policy consumer materializes. Until then: namespaced entries under `ContentTags.custom` or `ContentTags.domain`.

### 2.3 Reserved namespaces in `ContentTags`

A validator rejects reserved names and prefixes in both `ContentTags.custom` keys and `ContentTags.domain` values. Each reserved name blocks both the bare form (e.g., `"sensitivity"`) and the namespaced form (e.g., `"sensitivity:pii"`):

```
sensitivity      # data-classification access tier
regulatory       # legal/compliance framework tags
lifecycle        # content temporal validity state
jurisdiction     # geographic/legal scope
authority        # trust tier (reserved even though derivable, to prevent collision)
retention        # retention policy tag (aligns with existing PolicyType.RETENTION)
redaction        # field-level masking tag (aligns with existing PolicyType.REDACTION)
```

`retention` and `redaction` are reserved because `PolicyType.RETENTION` and `PolicyType.REDACTION` already exist as first-class policy concepts in `schemas/enums.py`. Allowing content-level `custom["retention"]` would silently collide with the policy layer. Reserving now prevents future conflict.

Rejection is a validation error with an instructive message pointing to the correct schema (or to this ADR, for reserved-but-not-yet-materialized concepts). The reserved-prefix list is a versioned module constant.

**Why this validator is worth the cost even before enforcement ships:** reserving names later means breaking existing users. Reserving them now costs ~30 lines + tests and preserves every downstream option.

### 2.4 Definitions and disambiguation

The value of reserved namespaces is that they carry shared, crisp meaning. Conflation between `authority` and `regulatory` (or `sensitivity` and `jurisdiction`) is exactly what we are preventing. Each reserved name is defined below with a "this, not that" foil:

| Namespace | Answers the question | Example values | Not to be confused with |
|---|---|---|---|
| `sensitivity` | *Who is allowed to see this content?* | `public`, `internal`, `confidential`, `restricted` | `regulatory` (what laws apply), `jurisdiction` (where viewers are), `authority` (how trusted) |
| `regulatory` | *What legal or compliance framework governs this content?* | `pii`, `phi`, `pci`, `gdpr`, `hipaa`, `export-controlled` | `sensitivity` (access tier). A doc can be `sensitivity:internal` AND `regulatory:pii` — orthogonal. |
| `lifecycle` | *What is the temporal validity state of the content itself?* | `draft`, `current`, `deprecated`, `superseded`, `archived` | `authority` (a `deprecated` ADR is still canonical for history), `retention` (retention class is about purging, not state) |
| `jurisdiction` | *Where does the content apply, or where must the viewer be located?* | ISO country/region codes (`us`, `eu`, `apac-ex-cn`) | `sensitivity` (access control), `regulatory` (what law applies vs. where it applies) |
| `authority` | *How canonical or trusted is this content within its domain?* | `canonical`, `sanctioned`, `community`, `unverified`, `synthesized` | `lifecycle` (a current Slack thread is not canonical; a deprecated ADR still is), truthfulness of a specific claim (authority is about provenance weight, not correctness) |
| `retention` | *How long must this content be kept / when should it be purged?* | retention class names (`short-term`, `7-year`, `permanent`) | `lifecycle` (deprecated content can still be under a long retention hold), `sensitivity` (orthogonal — any tier can have any retention) |
| `redaction` | *What portions of the content should be masked when rendered to certain viewers?* | redaction-rule identifiers | `sensitivity` (access is all-or-nothing; redaction is field-level and works *with* sensitivity), deletion (redaction masks; retention deletes) |

**A concrete conflation example the validator prevents:** an agent tags a document with `custom["authority"] = ["public"]` meaning "this is public-facing guidance" — but `public` is a `sensitivity` value, not an `authority` value. The validator rejects the reserved key, the error message names the right schema, and the agent (or the human reviewing the agent's PR) sees the conflation before it ships.

**Note on "lifecycle" in the existing codebase.** The terms "Trace lifecycle" / "Entity lifecycle" in `stores/base/event_log.py` and the `# Lifecycle` comments marking `created_at`/`updated_at` columns refer to *event categorization* and *DB-row timestamps* respectively. These are orthogonal to content-state `Lifecycle`. The reservation applies only to `ContentTags.custom` keys and `ContentTags.domain` values, not to column names or event labels — no conflict with existing code.

---

## 3. Guardrails

The split only works if the flexible layer is mechanically prevented from carrying load-bearing weight. The guardrails below are the commitment.

### 3.1 Policy code is typed against structured schemas

Phase 4 (when it ships) introduces a `PolicyContext` dataclass that carries `DataClassification` and `Lifecycle` explicitly. The `PolicyGate` Protocol signature takes `PolicyContext`, never raw item metadata. `ContentTags` is not a parameter to policy evaluation — it is a separate input to retrieval ranking.

This means: even if someone stuffs `custom["sensitivity"] = "pii"` past the validator (they won't, but hypothetically), it has *zero effect on policy* — by construction, not by convention.

### 3.2 Defense-in-depth runtime assertion

A `ClassificationResolver` is the sanctioned accessor for policy code. It reads exclusively from structured columns. In debug builds, any policy path that reaches into `metadata["custom"]` triggers a runtime assertion. This catches agent-written code that bypasses the type system.

### 3.3 Promotion path for flex tags

Every write to `ContentTags.custom` emits a `CUSTOM_TAG_USED` event (**keys only, never values** — values may contain PII). An admin CLI aggregates these into a usage report. A flex tag graduates to first-class when all three are true:

1. Observed across multiple installations / tenants with consistent value vocabulary
2. A concrete policy or retrieval consumer wants to enforce it
3. A follow-up ADR proposes the schema

Without all three, the tag stays flexible. This prevents schema sprawl from speculation and makes the flex layer a *discovery mechanism*, not a dumping ground.

### 3.4 Agent-first ergonomics

Agents will write code against this system. The guardrails must produce *instructive* errors, not punitive ones:

- Validator rejection names the correct schema: `"'sensitivity:pii' is reserved — use DataClassification.regulatory_tags instead. See docs/design/adr-tag-vocabulary-split.md"`
- `schemas.md` carries a decision tree: "Where does this tag go?" that an agent can follow without reading the full ADR
- The MCP server (when Phase 4 ships) exposes classification state as a first-class tool so agents self-check before acting

---

## 4. Scope — Phase 0 only

This ADR commits to Phase 0. Later phases are sketched for future reference but are **not approved by this ADR** and will each require their own decision.

### 4.1 What Phase 0 ships

| Deliverable | Footprint |
|---|---|
| This ADR | ~400 lines of markdown |
| `DataClassification` Pydantic model, defined but not required anywhere | ~30 lines |
| `Lifecycle` Pydantic model, defined but not required anywhere | ~30 lines |
| Reserved-namespace validator on `ContentTags` | ~30 lines + ~50 lines tests |
| Short note in `docs/agent-guide/schemas.md` | ~40 lines |

Total: ~200 lines of code + one ADR + one doc update. No migrations, no classifier changes, no retrieval changes, no policy gates, no CLI surface, no MCP tools, no SDK re-exports.

### 4.2 What Phase 0 does *not* ship

- Storage migrations (no new columns on nodes/items)
- Default-value decisions for sensitivity or lifecycle state on existing data
- Any classifier that produces `DataClassification` or `Lifecycle`
- Retrieval filtering by sensitivity or lifecycle
- `SensitivityGate` or `LifecycleGate` policy types
- CLI commands like `trellis classify set-sensitivity`
- `CUSTOM_TAG_USED` telemetry events
- MCP tools or SDK re-exports for the new schemas

Each of these is a deliberate deferral. They are justifiable **when a design partner asks**. They are not justifiable for a pre-adoption POC.

### 4.3 The litmus test

A new user can install Trellis, ingest a trace, build a pack, and never encounter the words "sensitivity" or "lifecycle." If the getting-started flow touches either concept, Phase 0 over-shipped.

### 4.4 Later phases (informational only, not approved here)

| Phase | Scope | Gating signal |
|---|---|---|
| **Phase 1** | Storage migrations, classifier pipeline extension, optional population | A design partner wants to store classification data |
| **Phase 2** | `RegexSensitivityClassifier`, `LifecycleKeywordClassifier`, backfill tooling | Partner wants automatic classification |
| **Phase 3** | `PackBuilder` excludes deprecated items by default | Partner reports deprecated-content incidents |
| **Phase 4** | `SensitivityGate`, `LifecycleGate`, `PolicyContext`, `ClassificationResolver` + runtime assertion | Partner wants enforced access control |
| **Phase 5** | `CUSTOM_TAG_USED` telemetry, admin reporting CLI, promotion process | Multiple partners; time to graduate flex tags |

Each phase is independently shippable and independently rollback-able.

---

## 5. Consequences

### 5.1 What this preserves

- **No painful migration later.** Sensitivity and lifecycle have a defined shape from day one. When Phase 1 ships, existing users get a schema migration but not a *semantic* migration — data that was already in `ContentTags.custom` under reserved prefixes is blocked today, so there is nothing to reconcile.
- **Every pluggability path from the broader discussion.** The `Classifier` Protocol, `SearchStrategy` ABC, and `ProfileLoader` concept (not yet built) remain viable and composable with this split.
- **The POC story.** Phase 0 adds zero surface to getting-started, README, or CLI.

### 5.2 What this costs

- **A permanent reserved-names list.** Adding new reserved prefixes later is a breaking change. Current list (`sensitivity / regulatory / lifecycle / jurisdiction / authority`) must be generous enough to cover realistic policy dimensions without being so wide it prevents legitimate custom tags.
- **Two Pydantic models that live in the codebase unused.** Minor maintenance cost; offset by having a concrete shape to point at when design partners ask.
- **The decision is partly one-way.** The reserved-namespace contract and the policy-relevance axis are hard to reverse once users depend on them. The *schemas themselves* can still evolve (Pydantic fields can be added backward-compatibly).

### 5.3 What this forecloses

- Growing `ContentTags` organically by adding new `Literal` facets for each new dimension (the rejected Option A). Future cross-domain dimensions go through the promotion path or into `custom` / `domain` with namespacing.

---

## 6. Alternatives considered

### 6.1 Option A — Grow `ContentTags` organically

Add `sensitivity`, `lifecycle`, etc. as new `Literal` facets on `ContentTags` as needs emerge. Rejected because:
- Conflates access-policy dimensions with retrieval-ranking dimensions — forces both to evolve on the same schema cadence
- Every new facet requires a schema migration; each one is breaking for anyone doing `extra="forbid"` validation (which `TrellisModel` does)
- Policy code would need to reach into a bag of mixed retrieval and access tags, losing the mechanical guarantee in §3.1

### 6.2 Option C — Do nothing

Let adopters ship their own classifiers and store anything they need in `custom`. Rejected because:
- Without reserved namespaces, multiple adopters will independently pick `custom["sensitivity"]` and build policy-adjacent code around it. Reconciling them later is strictly worse than reserving the names now.
- Sensitivity cannot be safely retrofitted onto installed data. The shape has to be defined *before* anyone depends on the absence.

### 6.3 Option B′ — Split *everything* (Audience, Intent, Modality, Authority) into first-class schemas now

Rejected as premature. Only Sensitivity and Lifecycle pass the policy-relevance test today. The others are retrieval-shaping; promoting them now pays schema-sprawl cost for no mechanical benefit. The promotion path (§3.3) exists to graduate them when signal emerges.

---

## 7. Open questions

These are not blockers for Phase 0 but must be resolved before Phase 1.

1. **Default sensitivity value.** `"internal"` (safe default, fewer accidental leaks) or `"public"` (matches current behavior for corpora that have no sensitive data)? Leaning `"internal"` with per-tenant config override. One-way door.
2. **Default lifecycle state.** `"current"` is near-obvious but distinguishes published-draft from archived-draft? Probably ship with just `"current"` as default and let classifiers promote to `"draft"` where appropriate.
3. **Classification on edges, not just nodes/items.** A `PERSON → treated_by → PROVIDER` edge in a healthcare graph can be sensitive independent of the nodes. Deferred to Phase 4 consideration.
4. **Per-tenant extensible schemas.** Can a SaaS customer add their own first-class schema? Current answer: no, use the promotion path. Worth revisiting if/when SaaS multi-tenancy is built.
5. **Scope of reserved-namespace list.** The current list (`sensitivity / regulatory / lifecycle / jurisdiction / authority`) is a commitment. Reviewers should push back on additions and omissions now — later is harder.
