# DoD-3 Reframe — Proposal (DRAFT, awaiting owner decision)

**Status:** draft · **Owner decision required** · Author: loop-starvation diagnosis 2026-08-16
**Context:** board [ronsse/trellis-ai#275](https://github.com/ronsse/trellis-ai/issues/275) · playbook `roadmap-driver-cloud-playbook.md` · memory `trellis-live-on-skynet-2026-07`

## The problem

DoD-3 currently reads:

> **#3 — Loop unstarved:** within 30d of #255, advisories/lessons > 0; curate not all-zeros.

The 2026-08-16 diagnosis (trace `01M04DVM1NXEJAMY0ZSAEEQV05`) showed this gate **cannot pass at single-user throughput, even though the loop is healthy.** The plumbing everyone kept "fixing" is now green:

| Signal (prod, 30d) | Value | Read |
|---|---|---|
| Writes accepted / rejected | 951 / 0 | write path healthy |
| `injected_coverage` | **1.0** | promote/demote join is fed |
| `attribution_rate` | **~0.29** | item attribution flowing |
| `graded_observations` | 6 | loop input present |
| Advisory generation (90d) | 2 generated | demote half produces |
| `promote_candidates` (min_support=2) | **0** | — |
| `precedents_promoted` | **0** | human review never run |

The two zeros at the bottom are the entire "starvation" signal, and neither is a defect:

1. **`promote_candidates = 0` is a throughput wall, not a bug.** The promote path needs an item to recur across **≥2 graded packs** (`min_support=2`); item-level advisories need **≥5** (`min_sample=5`). At ~12 packs/month across disjoint topics, **no memory item recurs** — every learning candidate shows `times_served=1`. Only degenerate strategy-correlation advisories clear the bar ("semantic 60% vs 0% without" — the "without" bucket is empty).
2. **`precedents_promoted = 0` is human-gated.** Promotion requires a person to run `learning-candidates → curate promote-learning`. That review has never been run on prod, and at `min_support=2` there is nothing to promote anyway.

**Conclusion:** "advisories/lessons > 0 within 30d" measures an *output* that a personal-throughput deployment structurally cannot produce. Keeping it as written means DoD-3 stays red forever while the loop is, in fact, correctly wired and fed. That is a mislabeled gate, not a broken system.

## What we can and can't change

- **Can:** the *definition* of DoD-3 (this doc), the nightly *measurement* (done 2026-08-16 — honest 30d block), the playbook *interpretation* (done).
- **Should not, by default:** lower the loop's promote/advisory thresholds. `min_support=1` / `min_sample<5` would "pass" the gate by promoting items graded **exactly once** — minting a standing lesson from a single success or failure. That is overfitting: the whole point of variation→selection is that a precedent earned repeated confirmation. A one-shot grade is noise.

## Options

| # | Option | Effect | Verdict |
|---|---|---|---|
| A | Widen measurement only (done), keep the criterion | Honest numbers, but DoD-3 stays red forever at this throughput | insufficient |
| B | Redefine DoD-3 to gate on **loop health** (fed + wired + generating), not promoted output | Flips green now, honestly; isolates the throughput-gated part | **recommended** |
| C | Lower `min_support` / `min_sample` | "Passes" by promoting single-grade items | reject as default (overfitting); at most an opt-in low-throughput mode, separate feature |
| D | Do nothing until usage rises | DoD-3 blocked indefinitely on a signal we don't control | reject |

## Recommendation — split DoD-3 into 3a (health) + 3b (output)

**#3a — Loop fed & wired (verifiable now, `skynet`):** over a 30d window —
`injected_coverage ≥ 0.9` **and** `attribution_rate ≥ 0.2` **and** `graded_observations ≥ 5` **and** curate not all-zeros.
Current: 1.0 / 0.29 / 6 / `advisories_generated` → **PASS today.**

**#3b — Loop yields standing memory (`skynet`, `blocked:signal`):** ≥1 precedent promoted through the human review path, **or** ≥1 `promote_candidate` at the real `min_support=2`.
Current: 0 / 0 → **open, correctly gated on throughput**, not on code.

This makes the honest half of the criterion pass now, and pins the genuinely-throughput-bound half as `blocked:signal` — the same label #261 already carries for "needs ≥30 days of feedback." Deployer-#2-readiness can then be defined against **3a** (the loop is correctly built and fed), with **3b** as a post-adoption maturity signal rather than a launch blocker.

## Decision requested from the owner

1. **Adopt the 3a / 3b split?** (recommended: yes)
2. **3a thresholds** as proposed (0.9 / 0.2 / 5), or adjust?
3. **Is 3b a launch blocker for deployer-#2, or a `blocked:signal` maturity gate?** (recommended: maturity gate — do not block adoption on personal-throughput recurrence)
4. **Low-throughput opt-in mode** (`min_support=1` behind a flag) — build as a separate feature, or reject? (recommended: reject for now; revisit if a multi-user pilot changes throughput)

## Out of scope (separate follow-ups, not part of this decision)

- `advisories.json` accumulates near-duplicate global-scope strategy advisories (`AdvisoryStore.put_many` not de-duping) — 14 stored, all `category=approach`.
- Degenerate "vs 0% without" strategy advisories should not surface as guidance when the "without" cohort is empty — a floor on the comparison cohort in `AdvisoryGenerator._strategy_correlation`.
