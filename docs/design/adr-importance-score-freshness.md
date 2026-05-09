# ADR: Importance-Score Freshness — Tag-Change-Triggered Recompute, with Read-Time Decay Guardrail

**Status:** Accepted (2026-05-09; user confirmed all-at-once impl with no fallback paths)
**Date:** 2026-05-09
**Deciders:** Trellis core
**Related:**
- [`./adr-deferred-cognition.md`](./adr-deferred-cognition.md) — LLM-derived `auto_importance` is an enrichment-mode artifact; this ADR governs how it ages
- [`./adr-tag-vocabulary-split.md`](./adr-tag-vocabulary-split.md) — defines the `Lifecycle` schema; this ADR is *not* the same axis (lifecycle is editorial state, not score age)
- [`./adr-graph-ontology.md`](./adr-graph-ontology.md) — `node_role` semantics; curated nodes need a different freshness story than semantic ones
- [`../../src/trellis/classify/importance.py`](../../src/trellis/classify/importance.py) — `compute_importance()` (composite tag boosts, ~32 LOC)
- [`../../src/trellis/classify/refresh.py`](../../src/trellis/classify/refresh.py) — already emits `TAGS_REFRESHED`; this ADR's primary hook
- [`../../src/trellis/classify/feedback.py`](../../src/trellis/classify/feedback.py) — `apply_noise_tags()` already stamps `classified_at`
- [`../../src/trellis/retrieve/strategies.py`](../../src/trellis/retrieve/strategies.py) — `_apply_importance()` and `_apply_recency_decay()` (half-life 30d, floor 0.3)
- [`../../src/trellis/schemas/classification.py`](../../src/trellis/schemas/classification.py) — `ContentTags.classified_at` already exists (Gap 1.1)
- [`../../src/trellis_workers/enrichment/service.py`](../../src/trellis_workers/enrichment/service.py) — `EnrichmentService` writes `auto_importance` into doc metadata
- [`../../TODO.md`](../../TODO.md) — Logic Gap 3.5

---

## 1. Context

### What exists today

There are *two* numeric importance signals on retrieved items, computed at different times by different code paths:

| Signal | Where it lives | Set by | When |
|---|---|---|---|
| `metadata["auto_importance"]` | document/vector/graph node metadata, JSON float `[0.0, 1.0]` | `EnrichmentService.enrich()` (LLM call) | Once, during enrichment |
| `compute_importance(tags)` | derived on the fly from `ContentTags` | `compute_importance()` in `classify/importance.py` | Composite of `signal_quality` + `scope` boosts; called by callers that need a single number, not stored |

At retrieval time (`_apply_importance` in `strategies.py`), only `metadata["auto_importance"]` is consulted:

```python
def _apply_importance(base_score: float, metadata: dict[str, Any]) -> float:
    """Apply importance weighting: base_score * (1.0 + importance)."""
    importance = float(metadata.get("auto_importance", 0.0))
    importance = max(0.0, min(1.0, importance))  # clamp 0-1
    return base_score * (1.0 + importance)
```

This score has **no associated timestamp** and **no decay**. A document scored `auto_importance=0.9` six months ago is multiplied by `1.9` exactly the same as one scored `0.9` yesterday.

### What is decayed today

`_apply_recency_decay()` in the same module already implements an exponential half-life:

```
decay = 0.5 ** (age_days / half_life_days)   # half-life 30d
score = base_score * (floor + (1 - floor) * decay)   # floor 0.3
```

But it operates on the item's **content timestamp** (`updated_at` / `created_at`), not on the *score's* timestamp. A long-stable document whose content has not changed in a year — but whose importance was scored a year ago against a now-shifted task distribution — gets correctly down-weighted by content age, but the staleness of the score itself is invisible.

### Why this is a real gap and not a paper one

The bias depends on the domain profile:

- **Stable-doc domains** (architecture ADRs, regulated reference docs). Importance is mostly a function of *position in the corpus*, not of a transient relevance signal. A 2-year-old "high-importance" ADR is still high-importance; the score is well-calibrated by time. Gap 3.5 is essentially harmless here.
- **Alert / dashboard / operational domains.** Importance reflects "is this firing right now / does it correlate with current incidents". A score from six months ago is actively misleading — a stale dashboard scored `0.9` will out-rank a fresh `0.7` even after content recency decay (because importance is a multiplier, not a co-decayed term). This is where Gap 3.5 lands hardest.
- **Mixed corpora.** The `signal_quality="noise"` feedback loop already addresses one corner case (items consistently correlated with failure). But it only flips the bottom of the distribution; the top is unmaintained.

### The decision to make

Three sub-options for what "stale importance" should mean operationally:

- **(A)** *Time-based decay on the score itself.* Add `score_age` (or `importance_scored_at`) to every classified item. At retrieval, decay `auto_importance` with a half-life similar to the recency decay. Refresh is implicit in the math; no background job needed.
- **(B)** *Tag-change-triggered recompute.* Hook `TAGS_REFRESHED` (already emitted by `classify/refresh.py`) and re-derive importance whenever tags are re-classified. Couple importance refresh to the existing reclassification cadence; persist the result.
- **(C)** *Computed-on-read.* Never store importance. Compute `compute_importance(tags)` on every retrieval call. Trades store cost for compute cost; eliminates staleness by construction.

These are not mutually exclusive — but the recommendation that follows is opinionated about which one is the *primary* mechanism and which (if any) is a guardrail.

---

## 2. Decision

**Primary: Option B (tag-change-triggered recompute) — bind importance refresh to the existing `TAGS_REFRESHED` event so importance ages on the same cadence as the tags it is derived from.**

**Guardrail: a bounded read-time half-life decay** *only when* `importance_scored_at` is older than a configurable horizon (default 180 days) and the score is above a meaningful floor (default 0.5). This catches the alert/dashboard pathology without globally re-pricing every score on every read.

**Reject Option C** (computed-on-read) for `auto_importance`: the LLM-derived float is not a pure function of stored tags — it embeds the LLM's judgment at score time and cannot be reproduced from `ContentTags` alone. Recomputing on read would silently drop the LLM's contribution. Option C is *correct* for the deterministic `compute_importance(tags)` boost — that one is already a pure function and is already computed on read; this ADR does not change that.

### 2.1 Why B over A as the primary mechanism

- **The infrastructure already exists.** `classify/refresh.py` runs the pipeline against stale items and emits `TAGS_REFRESHED`. Importance refresh is one extra hook in `reclassify_item` (between merge and persist), not a new subsystem.
- **It avoids a schema migration on every classified item.** Option A requires a `score_age` (or `importance_scored_at`) field on every record that has an `auto_importance`. Option B can re-use `ContentTags.classified_at` as the freshness witness — it is already populated, already persisted, already understood by the refresh path.
- **It is consistent with the deferred-cognition stance.** `auto_importance` came from an LLM. Recomputing it requires an LLM call. That LLM call belongs in enrichment mode, not in the retrieval hot path. Tying the recompute to the existing tag-refresh cadence keeps LLM cost predictable and bounded.
- **It generalizes when we add new importance contributors** (e.g., a learned classifier, a graph-position-derived weight). All of them stamp at refresh time; the read path doesn't need to know which one wrote.

### 2.2 Why a read-time decay guardrail anyway

Option B alone leaves a hole: items that are *not* re-classified — because their tags have not drifted, or because the refresh batch hasn't reached them — keep their original importance forever. For stable-doc domains that's correct. For alert/dashboard domains it's the original bug.

A bounded read-time decay closes this without re-scoring everything:

- **Triggered only past a horizon.** Default 180 days since `importance_scored_at`. Below that, no decay applied (preserves current behavior for the recent majority).
- **Triggered only above a floor.** Default `auto_importance >= 0.5`. Low scores aren't worth decaying — they barely move the multiplier already.
- **Capped at a floor.** Same `RECENCY_FLOOR = 0.3` semantic as content decay — never zero out a score, just dampen it.
- **Per-domain tunable** via the existing `ParameterRegistry` plumbing in `strategies.py` (the recency-decay params are already registry-resolved per `(component, domain)`).

This is the operational hatch for "we know this domain has volatile importance and the refresh worker hasn't caught up."

### 2.3 What is *not* in scope for this ADR

- **Importance for `node_role="curated"` nodes.** Curated nodes carry `GenerationSpec` and are immutable across versions. Their importance lifecycle should be governed by the same role-immutability rules (`adr-deferred-cognition.md` §4.4). A curated node's importance does not get re-scored — instead, if the underlying signal changes, a *new* curated node is created. This ADR's refresh hook is a no-op when `node_role == "curated"`.
- **Importance for `Lifecycle.state == "deprecated"`.** Already handled by the lifecycle axis — a deprecated item should be filtered or down-weighted by lifecycle, not by a stale-importance heuristic.
- **The deterministic boost from `compute_importance(tags)`.** That function is pure and already runs on read. It implicitly refreshes whenever tags refresh.
- **Backfilling a score for items that have never been enriched.** Out of scope; that is the enrichment worker's job, not the freshness story.

---

## 3. Schema, event, and code changes

### 3.1 Schema additions

`ContentTags` already carries `classified_at` (Gap 1.1). One additional field:

```python
class ContentTags(TrellisModel):
    ...
    classified_at: datetime | None = None
    classified_mode: ClassifierMode | None = None

    #: When the *importance score* embedded in this item's metadata was
    #: last computed. Distinct from ``classified_at`` because importance
    #: can refresh on a different cadence (e.g., re-derived from refreshed
    #: tags via `compute_importance`, or re-scored by the enrichment
    #: worker). ``None`` means "never stamped" — same legacy/hand-edit
    #: story as ``classified_at``. See adr-importance-score-freshness.md.
    importance_scored_at: datetime | None = None
```

Storage location of the actual `auto_importance` value is unchanged (it lives in `metadata["auto_importance"]` per the existing convention). The new field is on `ContentTags` so a single read can answer "what tags, when classified, when last scored".

### 3.2 Event semantics

No new `EventType`. `TAGS_REFRESHED` already carries before/after diffs of the full tag set; once `importance_scored_at` is part of `ContentTags`, the diff naturally surfaces a change in the score-age stamp. The `TAGS_REFRESHED` payload's `after` block will include the new field.

If a future need surfaces (e.g., "importance was re-scored *without* a tag change, e.g., by a new learned classifier"), an `IMPORTANCE_RESCORED` event can be added then. We deliberately do not pre-allocate it.

### 3.3 Refresh-path changes

In `src/trellis/classify/refresh.py`, inside `reclassify_item`, after the pipeline produces `merged.tags` and before `document_store.put`:

```python
# Hook B: re-derive importance against the freshly-merged tags so the
# stored score ages on the same cadence as the tags it depends on.
fresh_tags_obj = merged.to_content_tags()
prior_importance = float(metadata.get("auto_importance", 0.0))
new_importance = compute_importance(
    fresh_tags_obj,
    base_importance=prior_importance,  # preserve LLM contribution
)
metadata["auto_importance"] = new_importance
fresh_tags_obj = fresh_tags_obj.model_copy(
    update={"importance_scored_at": datetime.now(UTC)}
)
fresh_tags = fresh_tags_obj.model_dump(mode="json")
```

Notes:
- `base_importance=prior_importance` preserves the LLM's prior judgment (we re-apply tag-derived boosts on top, not from scratch). This is a deliberate trade-off: it means the LLM contribution is "frozen" once written and only the deterministic boosts re-derive. Re-running the LLM is enrichment-worker territory, not refresh territory (per `adr-deferred-cognition.md` §4.3 — re-tagging is an enrichment action that already exists).
- The hook is *idempotent*: running refresh twice produces the same result modulo the timestamp.

In `src/trellis/classify/feedback.py`, `apply_noise_tags()` already stamps `classified_at`. It should additionally stamp `importance_scored_at` (the noise tag changes the `signal_quality` boost in `compute_importance`, so the score *did* effectively re-age):

```python
content_tags["signal_quality"] = "noise"
content_tags["classified_at"] = stamp
content_tags["importance_scored_at"] = stamp  # new
```

### 3.4 Read-path guardrail

In `src/trellis/retrieve/strategies.py`, replace `_apply_importance` with a freshness-aware variant:

```python
DEFAULT_IMPORTANCE_FRESH_HORIZON_DAYS = 180.0
DEFAULT_IMPORTANCE_DECAY_FLOOR = 0.3
DEFAULT_IMPORTANCE_DECAY_THRESHOLD = 0.5  # only decay scores >= this

def _apply_importance(
    base_score: float,
    metadata: dict[str, Any],
    *,
    now: datetime | None = None,
    fresh_horizon_days: float = DEFAULT_IMPORTANCE_FRESH_HORIZON_DAYS,
    floor: float = DEFAULT_IMPORTANCE_DECAY_FLOOR,
    decay_threshold: float = DEFAULT_IMPORTANCE_DECAY_THRESHOLD,
) -> float:
    """Apply importance weighting with bounded staleness decay.

    Decay is applied *only* when:
      * `importance_scored_at` (or fallback `classified_at`) is past the
        horizon, AND
      * the raw importance is at or above ``decay_threshold``.

    Below those thresholds the function returns the legacy behavior:
    `base_score * (1.0 + clamp(importance, 0, 1))`.
    """
    importance = float(metadata.get("auto_importance", 0.0))
    if importance == 0.0:
        # No importance score → no multiplier, no freshness check needed.
        return base_score
    importance = max(0.0, min(1.0, importance))
    if importance < decay_threshold:
        return base_score * (1.0 + importance)

    # Locate the freshness witness. ContentTags is the canonical home;
    # importance_scored_at is REQUIRED for any item carrying auto_importance.
    # Greenfield project — no fallback to classified_at, no fallback for
    # missing stamps. A missing stamp is a bug in the writer path; surface
    # it loudly rather than silently treating the score as fresh.
    tags = metadata.get("content_tags") or {}
    raw_stamp = (
        tags.get("importance_scored_at")
        or metadata.get("importance_scored_at")
    )
    if raw_stamp is None:
        raise ValueError(
            "auto_importance is set but importance_scored_at is missing — "
            "writer path is broken. Item metadata: "
            f"keys={sorted(metadata.keys())}"
        )
    decayed = _decay_importance_if_stale(
        importance,
        raw_stamp,
        now=now,
        fresh_horizon_days=fresh_horizon_days,
        floor=floor,
    )
    return base_score * (1.0 + decayed)
```

`_decay_importance_if_stale` mirrors the structure of `_apply_recency_decay` — same half-life math, but the `age_days` is measured from `importance_scored_at` and the decay starts only past `fresh_horizon_days` (a "no-op grace period"). Outside the horizon the formula is the same `floor + (1 - floor) * 0.5 ** ((age - horizon) / half_life)` shape.

The constants are exposed via `ParameterRegistry` so per-domain overrides work the same way as the existing recency params (`recency_half_life_days`, `recency_floor`).

### 3.5 No legacy compat — greenfield writer contract

- **`importance_scored_at` is REQUIRED for any item with `auto_importance` set.** Missing stamp → loud `ValueError` at read. No fallback to `classified_at`, no "treat as fresh" path. (User decision 2026-05-09: no silent fallbacks.)
- The contract is enforced at the writer side: every code path that sets `auto_importance` must also stamp `importance_scored_at`. There are exactly three writer paths after this ADR:
  1. `EnrichmentService.enrich()` — original LLM-based scoring; stamps at write time.
  2. `reclassify_item()` in `classify/refresh.py` — re-derives via `compute_importance` + stamps (per §3.3).
  3. `apply_noise_tags()` in `classify/feedback.py` — adjusts `signal_quality` so importance changes; re-stamps (per §3.3 close).
- No bulk migration script needed for greenfield. If pre-existing items exist in a deployment, operators run `trellis admin reclassify-stale --max-age-days=0` once before deploying this ADR's read path; the refresh sweep stamps everything via path (2).

---

## 4. Consequences

### Positive

- **Closes Gap 3.5 with one schema field and one read-path tweak.** No new event types, no new background workers, no migration runner.
- **Aligns with the existing freshness story.** `classified_at` (Gap 1.1) and `importance_scored_at` (this ADR) live together in `ContentTags`, refresh through the same path, surface in the same event payload.
- **Domain-tunable via existing plumbing.** Operators in alert/dashboard domains tighten `fresh_horizon_days` per-domain through `ParameterRegistry`; operators in stable-doc domains leave defaults alone.
- **Preserves the deferred-cognition stance.** No LLM call is added to retrieval. The LLM contribution to `auto_importance` is preserved across refreshes (frozen prior, rebuilt boosts) until the enrichment worker re-runs the LLM voluntarily.
- **Failure mode is conservative.** Missing freshness witness → no decay → same behavior as today. We never *increase* the score and we never zero it out (floor `0.3`).

### Negative / trade-offs

- **The LLM contribution can persist arbitrarily long.** If `auto_importance=0.9` was an LLM judgment call from 2 years ago and the tags haven't changed, only the read-time guardrail will dampen it — not the refresh hook. Closing this fully requires re-running the LLM, which is enrichment-worker scope. Acceptable for a POC.
- **A second freshness field on `ContentTags`** that is mostly redundant with `classified_at` for users who don't separately re-score importance. Documented as "may equal `classified_at` in the common case".
- **Read-path complexity grows by one branch.** `_apply_importance` is no longer a one-liner. Mitigated by the threshold (`decay_threshold=0.5`) — most items skip the freshness check entirely.
- **Tag-change-triggered recompute will sometimes "refresh" a score that didn't really need refreshing** (e.g., refresh detected no tag change → we still re-stamp). Cost: a metadata write. Mitigated by `reclassify_item`'s existing "tags unchanged → skip" early-out (see `refresh.py:122-130`); we extend that early-out to also skip when importance would not change.

### Neutral

- **Schema is additive.** No `extra="forbid"` violations; new field is `Optional` with a `None` default.
- **No CLI surface required for the POC.** Existing `trellis admin reclassify-stale` already exercises the new hook.

---

## 5. Implementation sketch

Suggested sequencing for the implementer (sibling impl agent):

1. **Schema field.** Add `importance_scored_at: datetime | None = None` to `ContentTags` in `src/trellis/schemas/classification.py`. Mirror the docstring style used for `classified_at` and `classified_mode`.
2. **Refresh hook.** Wire the recompute into `reclassify_item` in `src/trellis/classify/refresh.py` per §3.3. Extend the "tags unchanged" early-out to also compare `auto_importance`.
3. **Feedback hook.** Add the `importance_scored_at` stamp to `apply_noise_tags()` in `src/trellis/classify/feedback.py`.
4. **Read-path guardrail.** Replace `_apply_importance` in `src/trellis/retrieve/strategies.py` with the freshness-aware variant per §3.4. Surface the three new constants through `ParameterRegistry` alongside the existing `recency_half_life_days` / `recency_floor` keys.
5. **Tests.**
   - Unit: refresh re-stamps `importance_scored_at`; `apply_noise_tags` re-stamps; read-path applies decay only when stale + above threshold; missing stamp → fail-open.
   - Contract: existing classifier-pipeline contract tests should pick up the new field via `model_dump`; verify no test regressions.
   - Property: `_apply_importance` is monotonic in `importance` for fresh items (preserves current invariant).
6. **TODO.md entry.** Strike Gap 3.5 with a line pointing at this ADR + the impl PR.

Estimated impl scope: ~150-250 LOC (schema field + 3 small functions + tests). No backend / store changes required — the field rides inside the existing `metadata["content_tags"]` JSON blob.

---

## 6. Open questions

- **Per-`node_role` defaults.** Should `node_role="semantic"` default to a tighter horizon (e.g., 90d) and `curated` skip decay entirely? Probably yes, but defer the decision until we have effectiveness telemetry per role.
- **Should `IMPORTANCE_RESCORED` exist as a distinct event** for the future case where a learned classifier re-scores without changing tags? Pre-allocating costs nothing; the `TAGS_REFRESHED` payload reuse is fine for now. Open for the impl agent to argue either way.
- **Interaction with the dual-loop evolution ADR.** If the learning loop eventually emits its own importance updates, does it write through `TAGS_REFRESHED` or does it need a dedicated path? Likely the former, but flag for review when that loop ships.
- **Is `decay_threshold=0.5` the right default**, or should it be derived from the distribution (e.g., decay only items in the top quartile of stored importance)? POC default is fine; revisit after the effectiveness loop has data to drive a calibration.

---

## 7. Resolved scope (originally a POC-stage split-ship recommendation)

User decision 2026-05-09: **ship everything in one impl PR — schema field + refresh hook + feedback hook + read-path guardrail.** No split-shipment.

Rationale: the total scope is small (~150-250 LOC); greenfield project so there is no legacy data to migrate around; the no-fallback contract (§3.5) requires the read path and the writer paths to ship together (otherwise reads would raise on items the writer hadn't stamped yet).

---

## 8. References

- `src/trellis/classify/importance.py` — `compute_importance()`, the deterministic-boost half of the score.
- `src/trellis/classify/refresh.py` — `reclassify_item()` / `reclassify_stale()`, the refresh hook surface.
- `src/trellis/classify/feedback.py` — `apply_noise_tags()`, the secondary stamping site.
- `src/trellis/retrieve/strategies.py` — `_apply_importance()`, `_apply_recency_decay()`, the read-path scoring layer; `ParameterRegistry` plumbing for per-domain tuning.
- `src/trellis/schemas/classification.py` — `ContentTags` schema, where the new field lands.
- `src/trellis_workers/enrichment/service.py` — `EnrichmentService.enrich()`, the original writer of `auto_importance`.
- `src/trellis/stores/base/event_log.py` — `TAGS_REFRESHED` event definition.
- `TODO.md` — Logic Gap 3.5 ("Importance scores are stale; no temporal decay on scores themselves").
