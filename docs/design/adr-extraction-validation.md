# ADR: Extraction Validation Boundary — Where Malformed-Trace Rejection Lives

**Status:** Accepted (2026-05-09; user confirmed event name, Variant A' scope, and no-fallback enforcement)
**Date:** 2026-05-09
**Deciders:** Trellis core
**Related:**
- [`./adr-deferred-cognition.md`](./adr-deferred-cognition.md) — Writes deposit structure; intelligence is deferred. Constrains where validation may live.
- [`./adr-tag-vocabulary-split.md`](./adr-tag-vocabulary-split.md) — Establishes the "policy-relevance vs retrieval-shaping" axis used here.
- [`../../src/trellis/extract/base.py`](../../src/trellis/extract/base.py) — Extractor Protocol + the contract clause "must not raise for recoverable errors".
- [`../../src/trellis/extract/dispatcher.py`](../../src/trellis/extract/dispatcher.py) — Routes extractors, emits `EXTRACTION_DISPATCHED` and `EXTRACTOR_FALLBACK`.
- [`../../src/trellis/extract/commands.py`](../../src/trellis/extract/commands.py) — `result_to_batch()`, the draft → Command conversion.
- [`../../src/trellis/mutate/executor.py`](../../src/trellis/mutate/executor.py) — 5-stage governed pipeline; uniform `MUTATION_REJECTED { reason }` on rejection.
- [`../../src/trellis/mutate/handlers.py`](../../src/trellis/mutate/handlers.py) — `LinkCreateHandler` already does FK validation; `EntityCreateHandler` defers to graph store role validation.
- [`../../src/trellis/schemas/extraction.py`](../../src/trellis/schemas/extraction.py) — `EntityDraft`, `EdgeDraft`, `ExtractionResult.unparsed_residue`.
- [`../../src/trellis/retrieve/pack_builder.py`](../../src/trellis/retrieve/pack_builder.py) — Assembly-time `RejectedItem` shape and `_build_filters()`.
- [`../../TODO.md`](../../TODO.md) — Logic Gap 1.3.

---

## 1. Context

### 1.1 The gap

From `TODO.md` Logic Gap 1.3:

> Items can land with no entities, or entities without provenance, or empty tag facets. The contract says extractors "must not raise for recoverable errors" and surface via `unparsed_residue`, but there is no downstream validator that rejects or quarantines empty/orphan records. Accumulates knowledge-plane junk that silently degrades retrieval quality.

Three failure shapes were named: **empty extraction** (no entities, no edges), **orphan provenance** (entities without `generation_spec` when one is required), and **empty tag facets** (`ContentTags` with all retrieval-shaping facets blank). Each has a different cause, a different blast radius, and a different right answer. This ADR treats them as one boundary problem because they share the same architectural question: *whose job is it to say "no, this trace is junk"?*

### 1.2 What already exists

The audit's framing — "no downstream validator" — is true at the *extraction* boundary. But the system already has several validators worth naming because they constrain the design space.

**At the dispatcher boundary (`extract/dispatcher.py`).** `ExtractionDispatcher.dispatch()` already has a soft signal for one of the three shapes: `result.entities and result.edges` is empty → emits `EXTRACTOR_FALLBACK { reason="empty_result" }` (lines 110–117). This is signal-only — the dispatcher returns the empty result anyway. `analyze_extractor_fallbacks()` in `extract/telemetry.py` aggregates these for graduation tracking ("rules silently fail for this source").

**At the Command boundary (`mutate/executor.py`).** Stage 1 (validate) checks `OperationRegistry` schemas — currently *required-keys-present* only (`commands.py:149-160`). For `ENTITY_CREATE` that's `{entity_type, name}`; for `LINK_CREATE` it's `{source_id, target_id, edge_kind}`. Missing args fire `MUTATION_REJECTED { reason="validate" }`. This is **field-presence only** — it does not look at semantic shape (e.g., "entity has no provenance"), and it operates per-Command, not on the whole `ExtractionResult`.

**At the handler boundary (`mutate/handlers.py`).** `LinkCreateHandler._resolve_endpoints()` does pre-flight FK validation against the graph store and raises `ValidationError` if either endpoint is missing (escape hatch: `allow_dangling=True` for cross-batch edges). `EntityCreateHandler` defers role-vs-`generation_spec` validation to the graph store via `validate_node_role_args` (called inside `upsert_node`). Both surface as `MUTATION_REJECTED { reason="validate" }` from the executor's perspective when the handler raises — *but* per swarm2 B-8 the unified `_emit_rejection()` only fires from the three named stages. **Handler-raised `ValidationError` currently routes through `_emit()` with `MUTATION_REJECTED` and *no* `reason` field** (executor.py:172-182). That asymmetry matters for §4.

**At the assembly boundary (`retrieve/pack_builder.py`).** `_build_filters()` already excludes `signal_quality:noise` by default and supports tag-based filters. `RejectedItem { item_id, item_type, relevance_score, reason, strategy_source }` is the shape for assembly-time rejections — `dedup`, `semantic_dedup`, `structural_filter`, `session_dedup`, `max_items`, `token_budget`. *No existing rejection reason names "malformed source data".*

### 1.3 What "malformed" actually means, schema-wise

| Shape | Concretely | Today's behavior |
|---|---|---|
| **Empty extraction** | `ExtractionResult.entities == [] and edges == []` | Dispatcher emits `EXTRACTOR_FALLBACK { reason="empty_result" }`, then returns the result; `result_to_batch()` produces an empty `CommandBatch`; `MutationExecutor.execute_batch()` runs zero commands and reports zero outcomes. **No rejection, no quarantine, no event in the mutation log.** |
| **Orphan provenance** | `EntityDraft` with `node_role=NodeRole.CURATED` but no `generation_spec` (or with `node_role=SEMANTIC` from an LLM-tier extractor that didn't stamp one) | If `node_role=curated`, the graph store's `validate_node_role_args` raises and the executor reports `FAILED` (no `reason` on the event — see §1.2). For `semantic` from LLM, no rejection — the unstamped item lands without a provenance trail. |
| **Empty tag facets** | `MergedClassification` with empty `tags` dict (no facets fired). `to_content_tags()` returns `ContentTags(domain=[], content_type=None, scope=None, signal_quality="standard", ...)` | Lands in metadata as empty/None. PackBuilder filters won't exclude it (default `signal_quality:not_in:[noise]` admits `"standard"`). The item is retrievable by keyword/vector but invisible to tag-based filtering. |
| **Orphan edge** | `EdgeDraft` whose `source_id`/`target_id` references a draft-local id that wasn't emitted as a `EntityDraft` in the same batch | `LinkCreateHandler` FK check fails; `MUTATION_REJECTED` (no reason). The entity draft, if any, still landed. |
| **Unparsed residue carrying signal** | `unparsed_residue` non-empty but `entities`/`edges` non-empty too | Today: residue is silently dropped after `EXTRACTION_DISPATCHED`. No event records that signal was lost. |

These are five distinct shapes. Conflating them in a single rejector is a category error; treating them as five independent problems is over-engineering. §3 splits them deliberately.

### 1.4 Why the gap is open

Two reasons. First, the dispatcher's "must not raise for recoverable errors" contract pushed validation downstream by design — the extractor's job is to surface signal, not gatekeep. Second, the existing field-presence validator at the Command boundary was sufficient for the deterministic-extractor era (dbt, OpenLineage, JSON rules); LLM-tier extractors made the orphan-provenance and empty-tag shapes much more common because LLMs don't reliably stamp `generation_spec` and don't always produce content classifiers can match on.

Nothing has bitten in production yet. There is no design partner asking, no incident report, no observed retrieval-quality regression traceable to these shapes. The risk is the audit's framing: junk *accumulates*, then degrades retrieval *silently*. By the time the impact is measurable, the corpus is already polluted.

### 1.5 What we have to be careful about

- **The extractor contract is load-bearing.** "Must not raise for recoverable errors" is what makes the tiered-extraction graduation path (LLM → Hybrid → Deterministic) work — extractors return what they could, and downstream graduation telemetry watches the gaps. A validator that punishes extractors for residue would invert the contract.
- **`MUTATION_REJECTED { reason }` is a swarm2 B-8 commitment.** Any new rejection must follow that shape. New `reason` values are additive (open-string convention, per `extract/dispatcher.py:44-48` comment), but they must namespace cleanly against the existing three (`validate`, `policy_violation`, `idempotency_replay`).
- **The deferred-cognition ADR forbids LLM in the write path.** Validators must be deterministic. A validator that calls a model to "decide if this is junk" is out.
- **POC-stage scope discipline.** Per the user's preference and the pattern set by Logic Gap 1.2 (audit-closed, no code change) and Logic Gap 2.4 (signal-only, no auto-action), default to the smallest change that makes the gap observable — and only escalate to enforcement when a consumer asks.

---

## 2. Decision drivers

| Driver | Constraint |
|---|---|
| Extractor contract | Validation cannot live *inside* the extractor — that re-introduces "extractors raise for recoverable errors". |
| Deferred cognition | Validators are deterministic, microsecond-order, no network. |
| Audit symmetry (swarm2 B-8) | New rejections emit `MUTATION_REJECTED { reason=... }` through the same `_emit_rejection()` path. No sibling event types. |
| Existing pre-flight checks | `LinkCreateHandler` already does FK validation. Don't duplicate. |
| Two halves of the gap have different timescales | Rejection (don't write junk) is on the write path. Quarantine (write but flag) is on the retrieval path. They want different mechanisms. |
| Extension hooks before implementation | The validator surface should be a Protocol so domain-specific validators can be added (matches the `Classifier`, `PolicyGate`, `Extractor` pattern). |
| POC discipline | Smallest change that closes the gap. Signal-only first; enforcement opt-in. |

---

## 3. Considered options

The user task identified three options. Research surfaced two more variants worth naming.

### 3.1 Option A — Post-extraction stage in `MutationExecutor`

Add a sixth pipeline stage (or expand stage 1 / "validate") so each `Command` is checked for *semantic* shape, not just field presence. Examples: `ENTITY_CREATE` with `node_role=curated` *and* missing `generation_spec` rejects with `reason="malformed_extraction"`; `LINK_CREATE` whose source isn't in the same `ExtractionResult`'s entity drafts and isn't `allow_dangling` rejects with `reason="orphan_edge"`.

**Where it lives:** `src/trellis/mutate/executor.py` (new validator hook + new reason values).

**Pros:**
- Symmetric with existing rejection model. `MUTATION_REJECTED { reason }` shape already exists; consumers (`mutate.executor` tests, audit replay) already understand it.
- Per-Command granularity is the right grain for FK / role / required-arg checks.
- Catches the gap at the *write* boundary — junk never lands in the store.
- Composes cleanly with `LinkCreateHandler`'s pre-flight FK check (consolidate into one stage instead of two raise-paths).

**Cons:**
- Per-Command means it can't see the *whole extraction*. "All entities in this result lack provenance" is hard to express command-by-command without smuggling cross-Command state.
- Empty-extraction (zero entities, zero edges) doesn't generate Commands at all — there's nothing to reject. The dispatcher's `EXTRACTOR_FALLBACK { reason="empty_result" }` would still be the only signal.
- Empty-tag-facets is a property of the *classified* item (downstream of extraction) — it lives in `ContentTags`, not in any Command this stage sees.
- Pollutes the executor's contract: stage 1 today is "args present"; expanding it to "semantic shape" is a meaningful contract change.

**Variant A′ — Promote `LinkCreateHandler.ValidationError` to use `_emit_rejection { reason="orphan_edge" }`.** Tiny, no new stage, just fixes the missing `reason` field on the handler-raised path identified in §1.2. Worth doing regardless of which option wins.

### 3.2 Option B — Pre-pack filter in `PackBuilder`

Add a new `RejectedItem.reason="malformed_source"` (or split: `"empty_classification"`, `"missing_provenance"`) and check items at assembly time. Items still land in stores; PackBuilder excludes them from delivered packs.

**Where it lives:** `src/trellis/retrieve/pack_builder.py` (new filter alongside `structural_filter`, `session_dedup`).

**Pros:**
- Existing rejection-tracking surface (`RejectedItem`, `PACK_ASSEMBLED.rejected_items` telemetry) already wired into `analyze_pack_telemetry()` — operators *already see* rejection reasons in the telemetry CLI.
- Doesn't touch the write path. Backward-compatible by definition.
- Catches *all three shapes* uniformly because it operates on persisted items, after classification.
- Empty-tag detection is natural here — items have already been classified at this point.
- Symmetric with how Gap 3.2 (semantic dedup) and Gap 3.4 (telemetry) shipped: opt-in config, signal-rich telemetry, no enforcement until proven.

**Cons:**
- **Junk still lands in the store** — pollutes vector space, embedding compute, graph traversal cost. The audit's "accumulates knowledge-plane junk" framing is *not* addressed; only the retrieval impact is.
- A filter that runs on every retrieval is paying repeat cost for a one-time problem. (Mitigation: cache the malformed-flag in metadata at first detection.)
- Doesn't help non-pack consumers (graph queries, raw store reads, future tools that bypass `PackBuilder`).
- Adds another thing to `PackBuilder.build`'s long step list — already at 9 steps.

### 3.3 Option C — Lazy validation on `PackItem` access

Never reject. Surface a per-item validity score derived from "does this item have provenance / non-empty facets / non-orphan edges" and let consumers decide.

**Where it lives:** `src/trellis/schemas/pack.py` (new field on `PackItem`) + a derivation helper.

**Pros:**
- Maximally non-invasive. No new rejection paths.
- Composes with `compute_importance()` — could feed validity into ranking.
- Honors deferred cognition by deferring even the *judgment* of malformedness.

**Cons:**
- Doesn't actually *close* the gap — it just exposes it. Junk continues to accumulate; consumers individually decide whether to care.
- Adds a new field every consumer must learn about; high coordination cost for low signal.
- "Validity" is a fuzzy concept; defining it without a policy consumer is exactly the kind of speculation Phase 0 of `adr-tag-vocabulary-split.md` warned against.
- Effectively equivalent to "do nothing": every consumer that doesn't read the field has the current behavior.

### 3.4 Option D — Validator Protocol at the dispatcher boundary

Introduce `ExtractionValidator` Protocol; `ExtractionDispatcher.dispatch()` runs validators against the `ExtractionResult` before returning it. Validator outcomes become events; the dispatcher can be configured to drop the result (return empty) or pass through with annotated rejection.

```python
class ExtractionValidator(Protocol):
    name: str
    def validate(self, result: ExtractionResult, *, source_hint: str | None) -> list[ValidationFinding]: ...
```

**Where it lives:** `src/trellis/extract/validators.py` (new) + `extract/dispatcher.py` (wire-up) + new `EXTRACTION_VALIDATED` event (or reuse `EXTRACTOR_FALLBACK { reason="validation_failed" }`).

**Pros:**
- Sees the whole `ExtractionResult` (including `unparsed_residue`) at the right boundary — before drafts become Commands.
- Pluggable: matches the existing `Classifier`, `Extractor` Protocol pattern, including `allowed_modes`-style governance hooks for future per-domain rules.
- Preserves the extractor contract: extractors still don't raise; *validators* are where the "yes/no" call lives.
- Composable with Option A: a validator can *flag*; the executor stage *rejects*. Two halves of one design.

**Cons:**
- New Protocol surface. Even if it composes well, that's more API to maintain.
- Doesn't address empty-tag-facets directly because classification runs *after* extraction, not inside it.
- Two emission paths if both this and the executor reject — risk of double-counting in telemetry.

### 3.5 Option E — Do nothing yet (audit-closed, document the gap)

Match the disposition used for Logic Gap 1.2: write the audit decision into the extraction module's docstring, name the deferred work, and close the TODO entry without code change.

**Pros:**
- Zero risk. Zero scope creep.
- Matches the "no design partner pushing" pattern.
- Preserves option value — every other option above stays available.

**Cons:**
- The gap stays open in the corpus-quality sense. Junk accumulates.
- Three distinct failure shapes that *will* hit eventually have no observability — when one bites, root-causing it from the existing telemetry will be hard.
- Loses the cheap-but-valuable signal a validator hook would emit even in signal-only mode.

---

## 4. Decision

**Option D (validator Protocol at the dispatcher boundary), enforcing — when any validator fires the dispatcher quarantines the entities/edges into `unparsed_residue` and emits `EXTRACTION_REJECTED`. Plus Variant A′: `LinkCreateHandler.ValidationError` routes through `_emit_rejection({reason: "orphan_edge"})`.**

Greenfield project — no silent-fallback / signal-only intermediate stage. When a deterministic validator sees malformed extraction shape, the dispatcher rejects outright; the original signal is preserved in `unparsed_residue` for replay / forensics, but no Commands flow through. Operators see the rejection in the event log and can wire the analyzer for trends.

### 4.1 What this ADR ships

| Deliverable | Footprint |
|---|---|
| This ADR | ~600 lines of markdown |
| `ExtractionValidator` Protocol | ~30 lines in new `src/trellis/extract/validators.py` |
| Three default validators (see §5.2) | ~80 lines |
| `EXTRACTION_REJECTED` event type | ~10 lines in `event_log.py` + ~30 lines in `dispatcher.py` (enforcement: drop entities/edges into residue when validators fire) |
| Variant A′: `LinkCreateHandler.ValidationError` → `_emit_rejection { reason="orphan_edge" }` (LinkCreateHandler ONLY — other handlers stay as-is) | ~10 lines in `executor.py` + `handlers.py` |
| `analyze_extraction_validation()` analyzer in `extract/telemetry.py` | ~80 lines (mirrors `analyze_extractor_fallbacks` shape) |
| Tests | ~150 lines (validator unit tests + dispatcher integration covering enforcement) |

Total: ~400 lines of code + this ADR. No migrations, no breaking changes, no new CLI surface.

### 4.2 What this ADR does *not* ship

- **No new rejection in `PackBuilder`.** Option B is *not* taken; assembly-time filtering would fight the "junk shouldn't be in the store" framing.
- **No `MUTATION_REJECTED { reason="malformed_extraction" }` from the executor.** Option A's per-Command stage adds executor surface for marginal benefit; the validator already saw the whole result.
- **No empty-tag-facets validator.** That's a classification-pipeline concern, not extraction. Defer to a sibling ADR if it's worth solving — see §6.
- **No per-domain validator policies.** Defaults only; per-domain configuration awaits a consumer.
- **No sweep of all handlers for Variant A′.** Only `LinkCreateHandler` raises `ValidationError` from a handler today; other handlers are not touched in this ADR. (User decision 2026-05-09.)
- **No severity tier / signal-only mode.** Validators reject directly; no `Literal["info", "warn", "reject"]` discriminator. (Greenfield, no fallback paths.)

### 4.3 Why this and not the others

- **Why not A alone:** Per-Command granularity can't see the whole extraction. Empty-extraction makes zero Commands; A is structurally blind to it.
- **Why not B alone:** It's signal-rich but addresses the wrong half — the audit is about junk *accumulating*, not about junk being *retrieved*. PackBuilder filtering masks the problem; D + executor consolidation lets us *not write* it.
- **Why not C:** Doesn't close the gap. Same outcome as Option E with extra schema surface.
- **Why not E:** The gap has three concrete deterministic failure shapes (§1.3). Cheap to detect, cheap to enforce.
- **Why D + A′:** A′ fixes a real, latent inconsistency (handler-raised rejections don't carry `reason`) regardless of D. D rejects bad extractions at the right boundary.

### 4.4 What "rejection" looks like at the dispatch boundary

When any validator returns at least one `ValidationFinding`, `ExtractionDispatcher.dispatch()`:

1. Records the original `entities` + `edges` into the returned result's `unparsed_residue` (under a structured key — see §5.3) so the original extraction is recoverable for forensics / replay.
2. Returns `ExtractionResult(entities=[], edges=[], unparsed_residue=<populated>, ...)` — empty drafts mean `result_to_batch()` produces an empty `CommandBatch`; `MutationExecutor` runs zero Commands; nothing lands in the stores.
3. Emits `EXTRACTION_REJECTED { source_hint, extractor_used, findings: [ValidationFinding] }` to the event log. Operators / analyzers consume this to find systematic upstream issues.

No silent path: every rejected extraction is observable, the original signal is preserved in residue, and no caller sees stale or malformed data.

---

## 5. Implementation sketch

### 5.1 Files touched

```
src/trellis/extract/validators.py          NEW — Protocol + 3 defaults
src/trellis/extract/dispatcher.py          MODIFIED — wire validators, emit events
src/trellis/extract/telemetry.py           MODIFIED — add analyze_extraction_validation()
src/trellis/extract/__init__.py            MODIFIED — re-export
src/trellis/stores/base/event_log.py       MODIFIED — add EXTRACTION_REJECTED EventType
src/trellis/mutate/executor.py             MODIFIED — route handler ValidationError through _emit_rejection
src/trellis/mutate/handlers.py             MODIFIED — LinkCreateHandler raises with structured reason
tests/unit/extract/test_validators.py      NEW
tests/unit/extract/test_dispatcher.py      EXTENDED — validator integration
tests/unit/extract/test_telemetry.py       EXTENDED — analyze_extraction_validation
tests/unit/mutate/test_handlers.py         EXTENDED — orphan_edge reason field
docs/agent-guide/operations.md             OPTIONAL — note new event type
```

### 5.2 Validator Protocol

```python
# src/trellis/extract/validators.py

@dataclass
class ValidationFinding:
    """One finding from one validator."""
    validator_name: str
    severity: Literal["info", "warn"]   # "reject" reserved for Phase 2
    code: str                            # short stable identifier, e.g. "empty_result"
    message: str                         # human-readable
    affected: dict[str, Any] = field(default_factory=dict)  # e.g. {"draft_index": 3}


@runtime_checkable
class ExtractionValidator(Protocol):
    name: str
    severity: Literal["info", "warn"] = "warn"

    def validate(
        self,
        result: ExtractionResult,
        *,
        source_hint: str | None = None,
    ) -> list[ValidationFinding]: ...


# --- defaults ---

class EmptyResultValidator:
    """Flag results with zero entities AND zero edges."""
    name = "empty_result"
    # Note: dispatcher already emits EXTRACTOR_FALLBACK { reason="empty_result" }
    # for graduation tracking. This validator emits EXTRACTION_REJECTED
    # for the same shape but in the validation namespace, so retrieval-quality
    # analyzers don't have to understand both event types. Mild redundancy
    # for clean separation.

class OrphanProvenanceValidator:
    """Flag EntityDraft with node_role=curated but no generation_spec."""
    name = "orphan_provenance"
    # Defers to graph store's own check at handler time, but flags AT EXTRACTION
    # so retrieval-quality telemetry sees the failure pattern even if the
    # mutation was rejected (so the entity was never persisted, never tagged).

class DraftLocalReferenceValidator:
    """Flag EdgeDraft.source_id/target_id that don't match any EntityDraft.entity_id
    in the same result AND aren't allow_dangling."""
    name = "orphan_edge"
    # Catches the "extractor emitted edges referring to entities it didn't emit"
    # case at the extraction boundary, before LinkCreateHandler fails the
    # FK check at handler time. Same root cause, earlier signal.
```

### 5.3 Dispatcher integration

```python
# src/trellis/extract/dispatcher.py

class ExtractionDispatcher:
    def __init__(
        self,
        registry: ExtractorRegistry,
        *,
        event_log: EventLog | None = None,
        validators: list[ExtractionValidator] | None = None,
    ) -> None:
        ...
        self._validators = validators or []

    async def dispatch(self, raw_input, *, source_hint=None, context=None) -> ExtractionResult:
        ... # existing select + extract + EXTRACTOR_FALLBACK
        result = await extractor.extract(...)

        # Validation pass — enforcement, not signal-only.
        if self._validators:
            findings = self._collect_findings(result, source_hint=source_hint)
            if findings:
                # Quarantine the original entities/edges into residue so the
                # signal is recoverable; return empty drafts so no Commands
                # flow downstream.
                quarantined_residue = {
                    **(result.unparsed_residue or {}),
                    "rejected_by_validators": {
                        "entities": [e.model_dump(mode="json") for e in result.entities],
                        "edges": [e.model_dump(mode="json") for e in result.edges],
                        "findings": [f.__dict__ for f in findings],
                    },
                }
                self._emit_extraction_rejected(
                    source_hint=source_hint,
                    extractor_used=extractor.name,
                    findings=findings,
                )
                return ExtractionResult(
                    entities=[],
                    edges=[],
                    unparsed_residue=quarantined_residue,
                    extractor_used=extractor.name,
                )

        # Existing fallback signals + EXTRACTION_DISPATCHED still fire.
        ...
        return result
```

### 5.4 New event type

```python
# src/trellis/stores/base/event_log.py

#: Emitted by ExtractionDispatcher when one or more
#: ExtractionValidator instances flag a malformed extraction result.
#: Enforcing: when this event fires the dispatcher has already quarantined
#: the original entities/edges into ``unparsed_residue["rejected_by_validators"]``
#: and returned an empty result, so no Commands flow downstream. Operators
#: consume this for trend analysis via analyze_extraction_validation().
#: Payload: { source_hint, extractor_used, findings: [ValidationFinding] }.
#: Closes Logic Gap 1.3. See docs/design/adr-extraction-validation.md.
EXTRACTION_REJECTED = "extraction.rejected"
```

### 5.5 Variant A′ — handler rejection symmetry

```python
# src/trellis/mutate/executor.py — Stage 4 (Execute)

try:
    created_id, message = handler.handle(command)
except ValidationError as exc:               # NEW branch
    log.warning("handler_rejected", errors=exc.errors)
    self._emit_rejection(
        command,
        reason=getattr(exc, "code", "handler_validate"),  # e.g. "orphan_edge"
        message=str(exc),
    )
    return CommandResult(
        command_id=command.command_id,
        status=CommandStatus.REJECTED,
        operation=command.operation,
        message=str(exc),
    )
except Exception as exc:
    ... # existing path unchanged
```

`LinkCreateHandler` raises `ValidationError(msg, errors=missing, code="orphan_edge")` instead of bare `ValidationError(msg, errors=missing)`. Other handlers can adopt the same `code` kwarg as they grow validation paths.

### 5.6 Telemetry analyzer

```python
# src/trellis/extract/telemetry.py — NEW function

def analyze_extraction_validation(
    event_log: EventLog,
    *,
    days: int = 30,
    limit: int = 5000,
) -> ExtractionValidationReport:
    """Aggregate EXTRACTION_REJECTED events into per-source / per-validator
    counts and per-code distributions. Produces findings highlighting
    sources whose validation rate exceeds _HIGH_VALIDATION_RATE over
    >= _MIN_SOURCE_SAMPLES dispatches — same shape as
    analyze_extractor_fallbacks()."""
```

CLI surface deferred. Agents can invoke this through the same Python entry point as the existing `analyze_extractor_fallbacks()`.

---

## 6. Consequences

### 6.1 What this preserves

- **Extractor contract.** Extractors still don't raise. The validator layer is *external* to the extractor — it observes outputs and emits findings.
- **Deferred cognition.** Validators are deterministic. No model in the write path.
- **Audit symmetry.** New rejection (`EXTRACTION_REJECTED`) is event-log-recorded with a stable payload shape. Variant A′ closes a real `MUTATION_REJECTED` symmetry gap.
- **Graduation telemetry.** `EXTRACTOR_FALLBACK { reason="empty_result" }` keeps firing for graduation-tracking purposes; `EXTRACTION_REJECTED` fires in parallel for retrieval-quality purposes. Same shape, separate consumers.

### 6.2 What this costs

- **Two events for one shape.** Empty-result fires both `EXTRACTOR_FALLBACK` (graduation lens) and `EXTRACTION_REJECTED` (validation lens). Consumers join on `extractor_used` + `source_hint`. Net cost: ~1 extra event per dispatch, only when validators fire. Acceptable — both consumers want the data in their own framing.
- **One new Protocol surface.** Adds maintenance load. Mitigation: use the existing `Extractor` Protocol pattern verbatim; agents already understand it.
- **Validator runs synchronously per dispatch.** Microsecond-order for the three defaults (pure list scans). If a domain-specific validator is heavy, it's the validator's problem to be cheap or async — Phase 1 doesn't add async support.

### 6.3 What this forecloses

- **PackBuilder-side malformedness filtering** (Option B) is not foreclosed but is now redundant. If retrieval-quality regression *does* materialize before Phase 2 enforcement ships, B can land as a stop-gap; the validator events make the filter rule trivial to derive.

### 6.4 Tests required

- `test_validators.py` — unit tests per default validator: empty result fires `empty_result`; missing provenance fires only when role=curated; orphan edge fires only when target not in batch *and* not allow_dangling.
- `test_dispatcher.py` — validators run, findings stamped on `EXTRACTION_REJECTED`, dispatch still returns result unchanged.
- `test_telemetry.py` — analyzer aggregates per-source, surfaces findings above threshold.
- `test_handlers.py` — `LinkCreateHandler` orphan rejection now carries `reason="orphan_edge"`.
- `test_executor.py` — handler-raised `ValidationError` routes through `_emit_rejection` (audit symmetry).

### 6.5 Breaking changes

None. The dispatcher constructor gains an optional `validators=` kwarg defaulted to empty; existing callers see no behavior change. The new event type is additive. Variant A′ changes a `MUTATION_REJECTED` payload to *include* a `reason` field that was previously absent — strictly additive for the payload, strictly more useful for replay.

---

## 7. Resolved decisions (originally open questions)

User decisions 2026-05-09:

1. **Event name: `EXTRACTION_REJECTED`.** "Rejected" is the better description of the action — when the event fires, the dispatcher has rejected the extraction. No `severity` discriminator (greenfield, no signal-only mode).

2. **Empty-tag-facets shape: defer to a separate ADR.** Classification runs after extraction; the validator at this boundary cannot inspect classified tags. A future `ClassificationValidator` Protocol on `ClassifierPipeline` (mirroring this design) is the right home if the gap proves real.

3. **Variant A′ scope: `LinkCreateHandler` only.** No sweep of other handlers in this ADR. Other handlers don't raise `ValidationError` today; if one starts to, the impl agent for *that* handler adds the `code` kwarg.

4. **CLI surface: deferred.** `analyze_extraction_validation()` is reachable via Python; CLI wrapper waits until a consumer asks.

5. **Per-source-hint validators: deferred.** Defaults are global; per-source configuration lands when a second consumer needs different rules.

---

## 8. Alternatives explicitly rejected

- **Inline validation in extractors.** Inverts the contract that makes the graduation path work. Rejected on architectural grounds.
- **Schema-level rejection via Pydantic.** Adding `model_validator` to `ExtractionResult` to forbid empty `entities + edges` sounds elegant but breaks the extractor contract — tolerant JSON parsers (`extract/llm.py`) deliberately return empty results on parse failure, with `unparsed_residue` carrying the signal. Schema validation would re-introduce raises on recoverable errors.
- **A new `ExtractionStore` for quarantine.** Real solution to "where does junk go", but an entire new store ABC is way past POC discipline. Defer to Phase 2; Phase 1 events suffice for the observation half.
