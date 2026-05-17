# ADR addendum: Dual-loop evolution — advisory restoration

**Status:** Proposed 2026-05-16
**Amends:** [`adr-dual-loop-evolution.md`](./adr-dual-loop-evolution.md) (Proposed 2026-04-13, amended 2026-05-14)
**Deciders:** Trellis core
**Related:**
- [`./adr-dual-loop-evolution.md`](./adr-dual-loop-evolution.md) §2.4 — Advisory fitness tracking (the loop this addendum scopes the restore half of)
- [`./plan-evaluation-strategy.md`](./plan-evaluation-strategy.md) §5.5.2 row 1 — Regime-shift demo that surfaced the gap
- `src/trellis/retrieve/effectiveness.py::run_advisory_fitness_loop` — The `auto_restore` branch this addendum scopes
- `src/trellis/stores/advisory_store.py::AdvisoryStore.list` — The delivery-filter fence this addendum scopes
- `TODO.md` — "Advisory restoration unreachable in scenario context" (2026-04-28)

---

## 0. What this addendum does and does not do

This addendum **does not** ship code. It scopes a design question that surfaced after the dual-loop fitness machinery landed (`adr-dual-loop-evolution.md` §2.4, Phases 1–4 shipped 2026-04-13 → 2026-04-15). The original ADR specified suppression mechanics but left the symmetric restore half implicit. The 2026-04-28 regime-shift demo surfaced that the implicit half is unreachable in any organic loop. This addendum spells out four design options for closing the gap, and recommends a default — **manual baseline** — that the project can adopt today without a code change, while preserving the architectural fence the existing `AdvisoryStore.list()` semantics enforce.

What this addendum **does** do:

- Names the architectural fence (delivery filter vs. scoring filter) that makes auto-restore unreachable.
- Documents three "automatic restore" options (a / b / c) and one "manual baseline" option (d), each with pros, cons, and an implementation sketch.
- Recommends (d) as the default until a concrete production trigger moves the project off it.
- Names the trigger that would move the project off the manual baseline.

What this addendum **does not** do:

- It does not retire the `auto_restore` branch in `run_advisory_fitness_loop`. The branch stays as unit-tested machinery — useful in a future world where one of (a) / (b) / (c) lands, and harmless today because it is never reached.
- It does not retire `test_auto_restore_when_evidence_recovers`. The test exercises the branch via manual evidence injection; it documents the intended behavior and stays as an executable spec.
- It does not change `AdvisoryStore.suppress` / `restore` semantics. Both methods remain available, both remain idempotent, and the operator-facing API (today: direct method call; option (d) adds a CLI/REST wrapper) is unchanged in shape.

## 1. Context

### 1.1 The fence

`AdvisoryStore.list()` filters `SUPPRESSED` advisories by default. The default is the right shape for the read path: a suppressed advisory should not be delivered to agents. `PackBuilder` consumes `advisory_store.list()` at pack-assembly time, sees only `ACTIVE` advisories, and emits `advisory_ids` into `PACK_ASSEMBLED` accordingly. No suppressed advisory enters a pack.

That fence is correct. It is also why `run_advisory_fitness_loop`'s `auto_restore` branch cannot fire in any organic loop:

1. The fitness loop reads from the EventLog. To rescore a suppressed advisory it needs `PACK_ASSEMBLED` events that include that advisory's id in `advisory_ids`.
2. A suppressed advisory is excluded from `advisory_store.list()`. `PackBuilder` never sees it. Therefore no `PACK_ASSEMBLED` event after the suppression names that advisory.
3. The fitness loop has nothing new to score. `presentations` stays at zero in the analysis window, `min_presentations >= 3` gates the advisory out of `advisory_scores`, and the restore branch never runs.

The branch is exercised today only by `test_auto_restore_when_evidence_recovers` ([`tests/unit/retrieve/test_effectiveness.py:953`](../../tests/unit/retrieve/test_effectiveness.py)) via manual evidence injection: the test calls `_emit_outcomes(event_log, "adv_rebound", successes=3, failures=0)` directly, bypassing the PackBuilder fence. That test pins the branch's *behavior* but does not exercise its *triggering condition*.

### 1.2 Why this surfaced now

Scenario 5.4 (`agent_loop_convergence`) ran the regime-shift demo for [`plan-evaluation-strategy.md` §5.5.2 row 1](./plan-evaluation-strategy.md) on 2026-04-28. Three advisories crossed the suppression threshold under the planned regime shift; suppression worked as designed. The expected complement — at least one suppressed advisory rebounding once the regime stabilized — never materialized. The synthetic corpus produces ~50 rounds in seconds, and within those rounds the suppressed advisories accrued zero new presentations. The fence held, exactly as designed.

The plan §5.5.2 row 1 has restoration on its punch list, so the gap is recorded. What the gap doesn't tell us is whether **production** agent activity hits the same wall. Production corpora are longer-lived than 50-round synthetic runs; advisories suppressed in week 1 may sit in the store for months before fresh evidence arrives. Whether that evidence ever arrives without the fence being relaxed is the empirical question this addendum punts to operator review.

### 1.3 The shape of the question

There are two layers stacked on top of each other:

| Layer | Question | Where it gets answered |
|---|---|---|
| **Architectural** | Is "deliver only ACTIVE advisories" the right filter at the read path? | Yes. PackBuilder must not surface suppressed advice. This stays. |
| **Operational** | Should suppressed advisories ever re-enter circulation, and if so, by what mechanism? | Open. This addendum scopes it. |

Conflating the two — relaxing the delivery filter to make the scoring filter symmetric — would mean accepting that an advisory with `status=SUPPRESSED` could surface to an agent. That tradeoff is not free. Each option below is explicit about whether it touches the delivery filter or only the scoring filter.

## 2. Decision

**The project ships option (d) — manual baseline — as the default.** Operators who observe a suppressed advisory that the underlying corpus now warrants reinstating call `AdvisoryStore.restore(advisory_id)` directly or through a thin CLI/REST wrapper. The auto-restore branch in `run_advisory_fitness_loop` stays in the tree, unit-tested, **unreachable in organic loops by design**, and ready to be wired up by option (a), (b), or (c) when production signal warrants.

The project does **not** ship (a), (b), or (c) until the trigger in §4 fires. Each is recorded below for completeness, with an implementation sketch sized for a future swarm unit.

## 3. Options

### 3.1 Option (a) — age out failure evidence by time window

**Sketch.** Add a `max_evidence_age_days` parameter to `run_advisory_fitness_loop`. When scoring an advisory, the EventLog query is sub-scoped so that `PACK_ASSEMBLED` / `FEEDBACK_RECORDED` events older than the window are excluded from the per-advisory `successes` / `presentations` aggregation. If the advisory's *recent* evidence (still inside the window) is too thin to score (`presentations < min_presentations`), the advisory's confidence reverts toward a neutral baseline — call it `0.5` — at a controlled rate per loop run. Once the neutral baseline clears `suppress_below + _RESTORE_HYSTERESIS`, `auto_restore` fires.

**Pros.**
- Most "natural" semantics: the loop forgets old failure evidence rather than pretending the advisory is still being delivered. Aligns with the existing `days` parameter that already bounds the analysis window.
- No change to `AdvisoryStore.list()`; the read-path fence stays intact.
- One place to tune. `max_evidence_age_days` and the decay rate are two scalars on top of the existing fitness-loop parameter surface.

**Cons.**
- Requires longer-lived corpora than synthetic scenarios produce. A 30-day window means scenario 5.4 (which runs 50 rounds in seconds) cannot exercise this path without time-mocking. Real production data is the first place we'd see it work.
- The "neutral baseline drift" is a new policy with no operator visibility. An operator watching the store might see a suppressed advisory's `confidence` field tick upward over days without understanding why.
- Susceptible to corpus pauses: if no agent runs against a domain for `max_evidence_age_days`, *every* suppressed advisory for that domain decays toward neutral and resurrects. That is plausibly wrong (the domain stopped being exercised, not the advisory stopped being bad).

**Implementation sketch (size: ~150 LOC + tests).**
1. Add `max_evidence_age_days: int | None = None` to `analyze_advisory_effectiveness` and `run_advisory_fitness_loop`. Default `None` preserves current behavior exactly.
2. Add `recency_decay_rate: float = 0.05` to `run_advisory_fitness_loop`. When a suppressed advisory has zero recent presentations inside the window, blend its confidence toward `0.5` at `recency_decay_rate` per run.
3. Add an `ADVISORY_DECAYED` event (or extend `ADVISORY_RESTORED` payload with a `decay_source` field) so the resurrection path is visible in the EventLog.
4. Tests: one happy-path test (suppressed advisory with stale evidence drifts up and restores), one no-op test (zero presentations does not decay an *active* advisory), one corpus-pause test asserting the "no recent corpus activity → still resurrects" trap is visible.

### 3.2 Option (b) — dedicated "rescore-suppressed" pass that reads from EventLog directly

**Sketch.** Add a `rescore_suppressed_advisories()` entry point alongside `run_advisory_fitness_loop`. It does not call PackBuilder. It walks the EventLog directly for `PACK_ASSEMBLED` events whose `advisory_ids` include any SUPPRESSED advisory's id (sourced from `advisory_store.list(include_suppressed=True)`), aggregates the joined `FEEDBACK_RECORDED` outcomes, and rescores. If the rescored confidence clears `suppress_below + _RESTORE_HYSTERESIS`, it calls `advisory_store.restore()`.

The catch: under today's fence, no `PACK_ASSEMBLED` event after the suppression names the advisory. So this option only does useful work if it is paired with a *delivery sidecar* — a probabilistic "shadow delivery" path where `PackBuilder` occasionally includes a suppressed advisory in a pack at small `ɛ` (e.g., 1% of packs) for evidence-gathering purposes. That sidecar is a substantial addition.

**Pros.**
- Cleanest separation: scoring filter and delivery filter live in different methods. No `AdvisoryStore.list()` signature change.
- Shadow delivery is bandit-shaped, which is the right model for "is this still bad?" — explore at low cost, exploit otherwise. Pairs with future Phase-5-flavoured work.
- Operators can run `rescore-suppressed` ad-hoc (CLI command) as a diagnostic without it being part of the standing fitness loop.

**Cons.**
- Shadow delivery breaks the architectural simplicity of "ACTIVE advisories are delivered, SUPPRESSED are not." A 1% shadow delivery rate is a *much* more interesting invariant for downstream consumers to reason about than the binary one.
- Shadow delivery affects user-visible packs. Even at 1% the operator must opt in; this is the equivalent of a feature flag on a production retrieval surface.
- Two code paths to keep in sync: the standing fitness loop scores active advisories, the rescore-suppressed pass scores suppressed ones, and they share most of the aggregation logic.

**Implementation sketch (size: ~400 LOC + tests, plus opt-in feature flag).**
1. Add `rescore_suppressed_advisories(event_log, advisory_store, *, days=30, restore_above=None) -> AdvisoryEffectivenessReport`. Mirrors `analyze_advisory_effectiveness` but inverts the `include_suppressed` flag and restricts to SUPPRESSED ids.
2. Add a `PackBuilder` option `shadow_advisory_rate: float = 0.0`. When `> 0.0`, with that probability per pack, include one randomly-chosen suppressed advisory's id in the emitted `advisory_ids` (but **do not** surface the advisory text to the agent — the inclusion is for telemetry only). Mark these via a `shadow=True` flag in the pack-assembled payload so downstream effectiveness analysis can distinguish.
3. Add `trellis analyze rescore-suppressed-advisories` CLI command. Returns the report; operator decides whether to call `restore`.
4. Add `TRELLIS_ADVISORY_SHADOW_DELIVERY_RATE` env var, default `0.0`. Setting it `> 0.0` is the explicit opt-in; the system is loud about it on startup.
5. Tests: shadow delivery flag round-trip, rescore-suppressed end-to-end against an EventLog populated with shadow-flagged events, no-op when `shadow_rate=0.0`.

### 3.3 Option (c) — change `AdvisoryStore.list()` semantics so the fitness loop sees SUPPRESSED entries while PackBuilder still doesn't

**Sketch.** Add a parameter to `AdvisoryStore.list()` distinguishing **delivery** from **scoring** calls. Concretely: `list(*, for_scoring: bool = False)`. When `for_scoring=True`, both ACTIVE and SUPPRESSED entries return. When `for_scoring=False` (the default — preserves PackBuilder semantics), only ACTIVE returns. The fitness loop changes its call to `advisory_store.list(for_scoring=True)`, joins to `PACK_ASSEMBLED` events normally, and the existing restore branch fires the moment a suppressed advisory's recent EventLog evidence rebounds.

Same architectural catch as option (b): SUPPRESSED advisories are *not delivered to agents*, so under today's PackBuilder semantics no `PACK_ASSEMBLED` event after the suppression names that advisory. Option (c) on its own does not fix the unreachable branch — it just makes the branch *call shape* easier. To make (c) actually drive restorations, the project must either (i) accept that suppressed advisories' EventLog evidence will only arrive from operator action (essentially option (d) wearing a "for_scoring" hat) or (ii) layer the shadow-delivery sidecar from option (b) on top.

**Pros.**
- The smallest production-code surface change. One parameter, one new call site.
- Splits "delivery filter" from "scoring filter" explicitly in the API surface, which is a clean factoring even if (a) or (b) lands instead.
- Composes with both (a) and (b) cleanly — if `for_scoring` exists, (a) and (b) both want it.

**Cons.**
- On its own, it does not fix the unreachable branch. The architectural fence between "no delivery" and "no rescoring" is the same fence, viewed from two angles. Adding a parameter to `list()` does not create new EventLog rows.
- Worth doing as a refactor if and when (a) or (b) is shipping. Doing it speculatively today, without (a) or (b), just spreads the unreachable behavior over a wider API surface.
- API churn for negative value: every caller of `AdvisoryStore.list()` must consider the new parameter. Tests, CLIs, and the analyze stack all gain a flag they have no operational reason to set today.

**Implementation sketch (size: ~50 LOC + tests).**
1. Change `AdvisoryStore.list` signature to `list(*, scope=None, min_confidence=0.0, include_suppressed=False, for_scoring=False)`. When `for_scoring=True`, force `include_suppressed=True` internally.
2. Update `run_advisory_fitness_loop` (and `analyze_advisory_effectiveness`) to pass `for_scoring=True`.
3. Tests: explicit `for_scoring=True` test asserting SUPPRESSED advisories surface, and a regression test asserting PackBuilder's default call still excludes them.
4. **Defer:** without (a) or (b), no integration test can demonstrate end-to-end auto-restore. The branch remains effectively unreachable, just behind a different API surface.

### 3.4 Option (d) — manual baseline (operator-driven `restore()`)

**Sketch.** Status quo on the code, plus a thin CLI/REST wrapper so operators don't need to drop into a Python REPL to call the method. Operators identify a suppressed advisory worth restoring via `trellis analyze list-advisories --status suppressed` (or the equivalent REST endpoint), inspect the suppression reason and current confidence, and either:

- Restore directly: `trellis admin restore-advisory <advisory_id>`. The CLI calls `advisory_store.restore(advisory_id)` and emits an `ADVISORY_RESTORED` event with `source="operator"` so the audit trail reflects the manual override.
- Hard-delete and regenerate: `trellis admin remove-advisory <advisory_id> && trellis analyze generate-advisories`. Useful when the suppression reason indicates the advisory itself was malformed, not just stale.

The fitness loop is **not** modified. The unreachable auto-restore branch stays in place (still tested, still ready to be wired up by a future (a) / (b) / (c)).

**Pros.**
- Costs no implementation work today beyond the CLI/REST wrapper (~80 LOC + tests, doable as a single follow-up unit).
- Preserves both fences: delivery filter and scoring filter remain coupled. There is no "shadow delivery" surface for operators to audit.
- The operator is the policy. If a suppressed advisory should come back, a human looked at it and decided. That is the right authority for a hint surface that targets agent behavior.
- Aligns with the C1.6 / C1.7 discipline (validate before designing). We do not have production data on what fraction of suppressed advisories *should* be restored, what the false-positive cost looks like, or what cadence operators want. Manual baseline lets that signal accumulate cheaply.
- All four options assume an operator-facing surface for *inspecting* suppressed advisories. Manual baseline ships that surface and stops there.

**Cons.**
- Does not close the loop. A suppressed advisory rebounds only if a human notices, which is the wrong direction for a system that claims to be self-improving.
- Cannot be exercised by scenario 5.4 or the program-convergence regression suite — no eval signal validates "restoration works in production."
- Scales with operator attention. Fleets of agents producing advisories at scale produce more suppressions than any operator can review. The manual baseline is appropriate for the POC stage; it has a ceiling.
- Risks a long tail of "should have been restored" suppressions silently accumulating in the store. Without a periodic audit, the suppression set grows monotonically.

**Implementation sketch (size: ~80 LOC + tests, single follow-up unit when authorized).**
1. Add `trellis admin restore-advisory <advisory_id>` CLI command. Resolves the advisory, calls `advisory_store.restore()`, emits an `ADVISORY_RESTORED` event with `source="operator"` and an optional `--reason` flag captured in the event payload.
2. Add `POST /api/v1/advisories/{advisory_id}/restore` REST endpoint with the same semantics.
3. Add `trellis analyze list-advisories --status suppressed` to the existing `analyze` CLI surface (paired with `--status active` and `--status all`).
4. Tests: round-trip restore via CLI + via REST, `ADVISORY_RESTORED` event payload assertion, idempotency assertion (restoring an already-active advisory is a no-op).
5. No change to `run_advisory_fitness_loop`, `AdvisoryStore.list()`, or `PackBuilder`. The architectural fence stays.

## 4. How to decide — what would move us off the manual baseline?

The trigger that moves the project from (d) to one of (a) / (b) / (c) is **a real misfiring-but-recovered advisory caught manually in production**. Concretely:

1. **A production cycle suppresses an advisory** under the existing fitness loop. The suppression is correct at the time — the advisory's recent evidence really did warrant pulling it.
2. **The underlying corpus shifts** — a new domain partner onboards, an upstream system gets fixed, a previously-failing tool starts succeeding — such that the advisory's pattern is now valid again.
3. **An operator notices the rebound manually** and runs `trellis admin restore-advisory`. The restored advisory's next pack delivery correlates with measurable success uplift over the following N rounds, confirming the operator's call.

When that sequence happens **once**, the manual baseline has worked as designed — we caught the case, we have an artifact (the operator's recorded restore plus the post-restore success signal) that defines what "restoration should fire" looks like. That artifact is the test fixture for whichever automatic path we choose.

The decision between (a), (b), and (c) is then driven by the *texture* of that artifact:

- If the operator's signal was "the failure evidence is stale, fresh evidence is positive" — option (a) (age-out window) is the right shape. The artifact tells us how long the window should be.
- If the operator's signal was "the failure evidence is recent, but a shadow delivery would have surfaced positive evidence" — option (b) (rescore-suppressed pass + shadow delivery) is the right shape. The artifact tells us what shadow rate is sufficient.
- If the operator's signal was "I had to drop into Python to inspect the suppressed advisory because the API made it invisible" — option (c) (split delivery filter from scoring filter at the API) lands as a refactor regardless of (a) / (b).

A secondary, weaker trigger: **a regression test in `program_regression_suite` that catches a known-recovered advisory's reintegration.** If we can synthesize a regime-shift fixture that reliably exercises the rebound path under the existing fitness loop, the synthetic case becomes a proxy for the production one. Today's scenario 5.4 cannot exercise this because the corpus is too short-lived; a longer-window fixture or a time-mocked variant could. The synthetic trigger is weaker because it cannot tell us what real production restorations *look like* — only that the machinery would fire under some configuration. If the synthetic trigger fires before the production one, treat it as a signal to scope the work, not to ship it.

Until one of those triggers fires, the project stays on the manual baseline. The auto-restore branch in `run_advisory_fitness_loop` remains unit-tested machinery, ready to be wired up the moment we have a real artifact to design against.

## 5. References

- `docs/design/adr-dual-loop-evolution.md` §2.4 — Advisory fitness tracking (the loop this addendum scopes the restore half of).
- `docs/design/plan-evaluation-strategy.md` §5.5.2 row 1 — The regime-shift demo that surfaced the gap.
- `src/trellis/retrieve/effectiveness.py::run_advisory_fitness_loop` — The `auto_restore` branch, lines ~810–833 as of 2026-05-16.
- `src/trellis/stores/advisory_store.py::AdvisoryStore.list` — The delivery filter (default `include_suppressed=False`).
- `tests/unit/retrieve/test_effectiveness.py::test_auto_restore_when_evidence_recovers` — The unit test that pins the branch's intended behavior via manual evidence injection.
- `TODO.md` — "Advisory restoration unreachable in scenario context" (2026-04-28) — Original gap entry that this addendum responds to.
