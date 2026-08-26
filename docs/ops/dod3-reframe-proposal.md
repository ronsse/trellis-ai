# DoD-3 Reframe — ACCEPTED 2026-08-16

**Status:** ✅ ACCEPTED 2026-08-16 (all four decisions adopted as recommended) · Author: loop-starvation diagnosis 2026-08-16
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

## Decisions (accepted 2026-08-16)

1. **Adopt the 3a / 3b split** — ✅ **yes**.
2. **3a thresholds** `injected_coverage ≥ 0.9 · attribution_rate ≥ 0.2 · ≥5 graded observations · curate not all-zeros` — ✅ **adopted as proposed**.
3. **3b disposition** — ✅ **maturity gate, `blocked:signal`** — *not* a deployer-#2 launch blocker.
4. **Low-throughput opt-in mode** (`min_support=1` behind a flag) — ✅ **rejected for now**; revisit only if a multi-user pilot changes throughput.

**Rollout (applied 2026-08-16).** DoD criterion #3 in the 11 becomes **3a** (met today: coverage 1.0, attribution ~0.29, 6 graded obs, curate non-zero). **3b** is tracked as a post-adoption maturity signal *outside* the 11, labelled `blocked:signal`. Applied to: board #275 DoD table + note, the cloud playbook DoD table + mapping, and the Layer-2 nightly (now emits a computed `dod3a_met` verdict).

## Out of scope (separate follow-ups, not part of this decision)

- `advisories.json` accumulates near-duplicate global-scope strategy advisories (`AdvisoryStore.put_many` not de-duping) — 14 stored, all `category=approach`.
- Degenerate "vs 0% without" strategy advisories should not surface as guidance when the "without" cohort is empty — a floor on the comparison cohort in `AdvisoryGenerator._strategy_correlation`.
