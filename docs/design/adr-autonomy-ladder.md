# ADR: The autonomy ladder

**Status:** Proposed
**Date:** 2026-06-12
**Deciders:** Trellis core
**Related:**
- [`./plan-self-improvement-program.md`](./plan-self-improvement-program.md) — the program this ADR governs; §2 (POC hard rules) and §5.4 (idempotency of self-modifying loops)
- [`./adr-well-known-promotion-loop.md`](./adr-well-known-promotion-loop.md) — surface-only promotion loop; the canonical example of a Tier-3 (never-automated) capability
- [`./adr-graph-ontology.md`](./adr-graph-ontology.md) — §5.4, the one-way commitment that makes `well_known.py` promotion Tier 3
- [`./adr-column-leaf-modeling-guardrails.md`](./adr-column-leaf-modeling-guardrails.md) — leaf promotion, a Tier-2 (human-gated) capability
- [`./adr-dual-loop-evolution.md`](./adr-dual-loop-evolution.md) — the demote/promote loop whose autonomous half this ADR bounds

---

## 1. Context

Trellis has, over the last several phases, grown the *machinery* for acting
on its own degradation:

- `apply_noise_tags` demotes low-effectiveness items automatically.
- Advisory fitness suppresses and restores advisories on their own success
  signal.
- The tuner pipeline (`src/trellis/learning/tuners/`) proposes parameter
  changes (`RuleTuner`), gates them (`PromotionPolicy.promote_proposal`),
  and — crucially — can *watch what happens after a promotion* and roll it
  back (`rollback.monitor_post_promotion`, `PostPromotionPolicy.auto_demote`).

The rollback machinery exists and is tested. What does **not** exist is a
**policy contract that says when it is allowed to run without a human in the
loop.** Today `auto_demote` is a bare boolean an operator can flip; nothing
says *which* capabilities may be made autonomous, what invariants an
autonomous capability must satisfy, or how a capability earns or loses that
status. The manual `trellis metrics promote --commit` is the only sanctioned
promote path, and post-promotion monitoring is invoked by hand.

This is the gap the autonomy ladder closes. It is a *governance* ADR: it
adds almost no new mechanism (Part 2 of WP9 wires Tier 1, but the promote
and rollback functions it calls already exist). It names four tiers, assigns
every existing and near-term self-action to one, states the invariants Tier 1
must satisfy, and fixes the only path by which a capability changes tier.

The program's stance ([`plan-self-improvement-program.md`](./plan-self-improvement-program.md)) is
**semi-autonomous**: the system may act on its own where the action is cheap
to undo and narrow in blast radius, and must defer to a human everywhere
else. The ladder is the operational form of that stance.

## 2. Decision

Adopt a four-tier autonomy model. **A capability's tier is assigned by
reversibility × blast radius — not by the system's confidence in the
action.** Confidence calibrates *whether* an action fires within a tier; it
never promotes an action to a higher tier. The rejection of a confidence-only
model is explicit and load-bearing (§5).

### 2.1 The assignment rule

> **Tier = f(reversibility, blast radius).**
>
> *Reversibility* — can the action be undone through existing versioned
> state, with no human archaeology? *Blast radius* — how far does a wrong
> action propagate before something catches it (one data row? one retrieval
> pack? the shared graph? `main`)?
>
> An action is eligible for a *lower-numbered* (more autonomous) tier only
> when **both** axes are favourable. A single irreversible *or*
> wide-blast-radius axis pins the action to Tier 2 or Tier 3 regardless of
> how confident the system is.

Confidence is deliberately excluded from the tier assignment. A 99%-confident
edit to `well_known.py` is still a one-way commitment, so it stays Tier 3. A
low-confidence noise tag is still a reversible data-plane write, so it stays
Tier 0. (§5 develops why.)

### 2.2 The tiers

| Tier | Name | Reversibility | Blast radius | Approval | Examples (today / near-term) |
|---|---|---|---|---|---|
| **0** | Fully automatic | Trivially reversible (re-tag / re-score) | One data-plane item, retrieval-shaping only | None — runs unattended | `apply_noise_tags`; advisory confidence decay / suppression |
| **1** | Automatic with auto-rollback | Reversible through existing versioned state (`ParameterSet` versions) | One tuneable component's parameters; degradation is monitored and unwound | None at action time, but four invariants (§2.3) must hold, and config opt-in is per-scope, default OFF | Parameter promotions (tuner proposals) via `trellis worker tune` |
| **2** | Human-gated, machine-prepared | Varies; the human owns the irreversible step | Crosses into the shared graph or the codebase | **Human approves** the prepared artifact | Learning promotions to the graph; leaf promotions ([`adr-column-leaf-modeling-guardrails.md`](./adr-column-leaf-modeling-guardrails.md)); code-authoring proposals (program Item 7) |
| **3** | Never automated | Irreversible by construction (one-way commitment) | Permanent vocabulary; production `main` | **Human authors** the change; no machine write path exists | `well_known.py` ontology promotion ([`adr-graph-ontology.md`](./adr-graph-ontology.md) §5.4); merging agent-authored code to `main` |

Tier 0 names and bounds what already runs unattended — it does not newly
automate anything. Tier 3 names what must never be automated and is enforced
by the *absence* of a machine write path, not by a runtime flag (consistent
with [`adr-well-known-promotion-loop.md`](./adr-well-known-promotion-loop.md), which keeps the
registry surface-only on purpose).

### 2.3 Tier-1 invariants

A capability may run at Tier 1 (auto-apply without action-time approval) only
when **all four** invariants hold. They are AND-ed; a single failure drops the
capability to Tier 2 (machine-prepared, human-approved).

- **(a) Reversible through existing versioned state.** The action targets a
  versioned artifact whose prior version is the rollback target. For
  parameter promotion this is the `ParameterSet` version chain: the snapshot
  the promotion replaced is the snapshot a rollback restores. If a scope has
  no resolvable baseline (the first-ever snapshot, the bootstrap case),
  there is nothing to roll back to, so that promotion is **not** auto-applied
  — it falls to manual review. The auto policy enforces this by default
  (`require_baseline=True`).
- **(b) Post-change monitoring exists and is enabled.** Auto-apply is only
  offered *bundled with* monitoring armed to undo it. In the implementation,
  `run_auto_promotion` runs `monitor_post_promotion` with `auto_demote=True`
  immediately after each promotion; `AutoPromotePolicy.__post_init__` rejects
  a `PostPromotionPolicy` whose `auto_demote` is `False`. You cannot
  auto-promote without arming the rollback.
- **(c) The auto-action emits a dedicated audit event.** Every autonomous
  action writes a self-identifying event *in addition to* the normal
  governance event. Auto-promotion emits `PARAMS_AUTO_PROMOTED` alongside the
  pipeline's `PARAMS_UPDATED`; auto-rollback emits `PARAMS_AUTO_ROLLED_BACK`
  alongside the rollback's `PARAMS_UPDATED`. The audit trail distinguishes
  "a human promoted this" from "the system promoted this on its own."
- **(d) Per-scope opt-in; global default OFF.** Tier-1 autonomy is never on
  by default. It is enabled per scope through configuration
  (`learning.auto_promote.enabled`, default `false`). With it off, the
  capability behaves exactly as it did before Tier 1 existed — running
  `trellis worker tune` is then byte-identical to a bare tuner pass.

A capability that meets only (a) and (c) but lacks armed monitoring is **not**
Tier 1 — it is Tier 2 until monitoring lands.

### 2.4 Chosen tier-1 thresholds (parameter promotion)

Auto-promotion runs the *same* `PromotionPolicy` gate the manual path runs,
but with **strictly stricter** thresholds. The justification is the
no-reviewer asymmetry: a manual promotion has a human about to eyeball it, so
its gate only has to catch obvious non-starters; an unattended promotion has
no such reviewer, so it must clear a higher evidentiary bar before the system
acts alone.

| Threshold | Manual default (`PromotionPolicy`) | Auto default (`AutoPromotePolicy`) | Why stricter |
|---|---|---|---|
| `min_sample_size` | 5 | **30** | 30 matches `DEFAULT_RULES`' own `min_sample_size`, i.e. the auto floor never trusts a cell the tuner itself would not have fired on. 6× the manual floor. |
| `min_effect_size` | 0.15 | **0.25** | Only changes whose measured relative delta against the live baseline is large enough that noise is an unlikely explanation auto-apply. |
| baseline required | no (bootstrap allowed) | **yes** (`require_baseline=True`) | Invariant (a): no baseline ⇒ nothing to roll back to ⇒ not auto-applied. |
| `post_min_samples` (monitor) | 20 | 20 | Inherited from `PostPromotionPolicy`; the rollback side already demands 20 post-promotion samples before any demotion verdict, guarding against `n=2` thrash. |

`AutoPromotePolicy.__post_init__` *asserts* that the auto thresholds dominate
the manual defaults — a future edit cannot silently weaken the autonomous gate
below the human-reviewed one without raising at construction. Operators may
tighten further per scope but never loosen below the manual floor.

Non-qualifying proposals are left `pending`, not rejected — exactly the state
the manual `trellis metrics promote` path expects. The autonomous pass and the
manual pass are two readers of the same proposal store; the autonomous one
only acts on the strict subset it is confident *and* authorised to act on.

## 3. How a capability moves between tiers

**A capability changes tier only through an amendment to this ADR.** There is
no runtime promotion of a capability up the ladder, no "it's been stable for N
days, auto-upgrade it" heuristic, and no config flag that moves a Tier-2
capability to Tier 1.

The procedure to move a capability (e.g. to make leaf promotion Tier 1 once it
gains versioned reversibility and armed monitoring):

1. Demonstrate the target tier's invariants hold for the capability — for a
   move to Tier 1, all four of §2.3, with the monitoring code in place and
   tested.
2. Amend this ADR: move the capability's row in the §2.2 table, add a dated
   amendment note explaining the evidence, and cross-reference the ADR that
   added the missing invariant (e.g. the one that introduced versioned leaf
   state).
3. Only after this ADR's amendment is **Accepted** may the enabling code path
   ship.

This mirrors the well-known promotion loop's discipline
([`adr-well-known-promotion-loop.md`](./adr-well-known-promotion-loop.md) §2.5): the formal
artifact (an ADR amendment) is the gate, made low-friction but never
short-circuited. Tier *demotion* (pulling a capability back to a more
conservative tier after an incident) follows the same amendment path but
should be expedited — a capability can always be made *less* autonomous
immediately by disabling its opt-in config while the amendment is drafted.

## 4. Why this shape

### 4.1 Why reversibility × blast radius, not a single axis

Either axis alone is insufficient. A reversible action with catastrophic
blast radius (auto-rewriting every edge in the shared graph, then "undoing"
it) is still a Tier-2-or-worse action because the in-between state is visible
to every agent reading the graph. An irreversible action with tiny blast
radius (permanently naming one canonical type) is still Tier 3 because the
commitment is forever. The product of the two axes is what determines whether
unattended action is safe.

### 4.2 Why Tier 1 bundles monitoring inseparably

The whole point of Tier 1 over Tier 2 is that the system, not a human,
catches a bad action. That only works if the catch is *armed at the moment of
the action*, not "scheduled to maybe run later." Bundling promotion and
armed rollback into one call (`run_auto_promotion`) makes "promoted but
unmonitored" unrepresentable.

### 4.3 Why Tier 3 is enforced by absence, not a flag

A runtime flag that says "do not auto-edit `well_known.py`" can be flipped. An
absent code path cannot. Tier 3 capabilities have *no machine write surface* —
the well-known loop is surface-only, and there is no `git push origin main`
inside any worker. This is defence by construction, the strongest kind.

## 5. Explicit rejection of a confidence-only model

A tempting alternative: tier actions by the system's *confidence* — "auto-apply
anything we're >95% sure about, ask a human for the rest." We reject this.

- **Confidence is a property of the prediction; tier is a property of the
  consequence.** A statistical model can be calibrated and still be wrong; the
  question the ladder answers is not "how sure are we?" but "what happens when
  we're wrong, and can we take it back?" A 95%-confident edit to a one-way
  commitment is still a one-way commitment — the 5% tail is permanent.
- **Confidence drifts; consequences don't.** Sample composition shifts, a
  proxy metric decouples from the true objective, an upstream extractor
  changes shape. A confidence-gated system silently expands its own autonomy
  as its (possibly miscalibrated) confidence rises. A consequence-gated system
  has a fixed, auditable autonomy boundary that only an ADR amendment moves.
- **Confidence-only collapses the audit story.** "The system was confident"
  is not an account a reviewer can act on after an incident. "The action was
  Tier 1, reversible through version chain X, monitored by policy Y, and
  rolled back at event Z" is.

Confidence still does real work — *inside* a tier. The tuner's `min_effect_size`
and `min_sample_size` are confidence proxies that decide whether a Tier-1
action fires at all. They tune the firing rate; they never change the tier.

## 6. Consequences

### 6.1 What this enables

- The existing rollback machinery gets a governing contract: `auto_demote` is
  no longer a bare boolean but the enforcement of Tier-1 invariant (b).
- `trellis worker tune` (WP9 Part 2) becomes a sanctioned Tier-1 surface,
  config-gated and default-off, with a self-identifying audit trail.
- Future capabilities (leaf promotion, code authoring) have a clear, written
  bar to clear before they could ever be made more autonomous — and a clear
  statement that, today, they are not.
- Operators get a single page that answers "what can this system do without
  asking me?" — currently: re-tag noise, decay advisories, and (only if they
  opt a scope in) auto-promote/auto-rollback parameters. Nothing else.

### 6.2 What this does not do

- Does not automate anything at Tier 2 or Tier 3. Learning-to-graph
  promotion, leaf promotion, code authoring, and `well_known.py` edits remain
  human-gated or human-authored.
- Does not add a confidence-based escalation path (§5).
- Does not change the manual `trellis metrics promote` behaviour — it remains
  byte-identical, and the autonomous path reuses its pipeline rather than
  forking it.
- Does not introduce a new mutation route. Auto-promotion calls the same
  `promote_proposal`; auto-rollback calls the same `monitor_post_promotion`.

### 6.3 Costs and risks

- **Operator trust calibration.** Tier 1 asks operators to opt a scope in
  before they have watched the loop on their traffic. The stricter thresholds
  and the dedicated audit events are the mitigation; the per-scope granularity
  lets them start narrow.
- **A miscalibrated reward signal can still thrash within Tier 1.** If
  `success` (the tuner's reward) decouples from real value, the loop could
  promote and roll back repeatedly without a human noticing. The
  `PARAMS_AUTO_PROMOTED` / `PARAMS_AUTO_ROLLED_BACK` event pair is designed so
  an effectiveness analyzer (or an operator) can detect exactly this
  oscillation; defining an alarm on it is follow-up, not in scope here.
- **Tier drift pressure.** There will be pressure to move capabilities down
  the ladder "because it's been fine." The amendment-only rule (§3) is the
  deliberate friction against that pressure.

## 7. References

- `src/trellis/learning/tuners/promotion.py` — `PromotionPolicy`, `promote_proposal` (the Tier-1 promote path)
- `src/trellis/learning/tuners/rollback.py` — `monitor_post_promotion`, `PostPromotionPolicy.auto_demote` (the Tier-1 rollback path)
- `src/trellis/learning/tuners/auto_promote.py` — `AutoPromotePolicy`, `run_auto_promotion` (the Tier-1 governance contract added by WP9)
- `src/trellis_cli/worker.py` — `trellis worker tune` (the Tier-1 CLI surface)
- `adr-graph-ontology.md` §5.4 — one-way commitment (Tier 3)
- `adr-well-known-promotion-loop.md` §2.5 — surface-only, ADR-gated promotion (Tier 3 pattern)
- `adr-column-leaf-modeling-guardrails.md` — leaf promotion (Tier 2)
