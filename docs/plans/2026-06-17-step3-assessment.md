# Step 3 — Quality / Impact Assessment

> Produced by the Phase-2 assessment swarm (2026-06-17): three scouts (eval↔dashboard
> alignment, scenario inventory, live run) + synthesis. **Headline finding:** the handoff's
> premise that "dashboard numbers == eval numbers by construction" is **only partially true**
> — see §2. Section 6 lists the concrete fixes that would make it true.

## 1. Purpose

Step 3 answers one question: **does Trellis measurably make agents better at their work, and can we instrument that improvement so it is reproducible and defensible?**

The assessment rests on three claims:

- **C1 — retrieval lift:** as an agent accumulates traces over N rounds, retrieval improves and agent success rises.
- **C2 — noise demotion:** the curation loop measurably demotes low-value (noise) context so packs get cleaner over time.
- **C3 — promote-loop durable value:** the advisory / promote loop (and the program-level self-improvement axes) adds value that persists rather than evaporating.

"Defensible" has a second requirement beyond the claims: the **dashboard an operator watches must compute the same numbers the eval harness uses to certify convergence.** If the two diverge, the dashboard is a different instrument than the one Step 3 validates against, and the assessment leans on a number nobody can reproduce live. Section 2 tests exactly that.

## 2. Instrument alignment — dashboard vs. eval convergence metrics

**Verdict: NOT aligned by construction.** The central claim "dashboard numbers == eval numbers" holds only partially, for **2 of 5** dashboard metrics, and only under restrictive conditions.

| Dashboard metric | Eval counterpart | Identical? | Condition / drift |
|---|---|---|---|
| `pack_success_rate` (`metrics_timeseries.py:242-245`, ratio `:452`) | `round_success_rate` (`_convergence_common.py:194`) | Partial | Same arithmetic + 4dp rounding **only** when the whole run lands in one UTC day and every feedback joins a `PACK_ASSEMBLED` event |
| `reference_rate` (`metrics_timeseries.py:302-303`, ratio `:452`) | `round_useful_fraction_overall` (`_convergence_common.py:197-199`) | Partial | Same pooled `sum-referenced / sum-served` + 4dp; same per-day / join-drop / zero-served caveats |
| `advisory_fitness` mean `new_confidence` + suppressed count (`metrics_timeseries.py:341-347`, `:469`) | only `advisories_suppressed_total` (`_convergence_common.py:169,255`) — no confidence mean exists | No | Mean-confidence value has **no eval counterpart**; suppressed count is a different computation path (in-loop tally vs. per-day event re-count) |
| `noise_tag_volume` (`metrics_timeseries.py:368-373`, `_after_is_noise:377-392`) | `noise_items_tagged_total` (`_convergence_common.py:166,238`) | No | Eval counts noise **candidates** (`len(noise_candidates)`); dashboard counts emitted `TAGS_REFRESHED` **events** per UTC day. Candidate count ≠ event count; cumulative ≠ per-day |
| `parameter_promotions` (`metrics_timeseries.py:413-436`) | (none) | No | Convergence scenarios run no tuner; **zero eval analogue** |
| join helper `join_pack_feedback` (`pack_observations.py:104-125`, key `:122-124`) | consumed indirectly via `_record_round_feedback:290-295` | Misleading | Dashboard + learning bridge call the helper identically; **the eval metric math never calls it** — it reads in-memory `_RoundResult` objects |

Confirmed drift dimensions:

1. **Join scope.** The eval convergence metrics do **not** call `join_pack_feedback`. `_base_round_metrics` / `_convergence_stats` (`_convergence_common.py:131-200`) compute from in-memory `_RoundResult` objects (`scenario.py:486-497`), not from a `PACK_ASSEMBLED ⋈ FEEDBACK_RECORDED` log read. Parity holds only because `_record_round_feedback` faithfully writes the same per-pack bits the dashboard later re-reads. "Both sides share the join helper" is false for the metric math.
2. **Denominator domain.** Eval `pack_success_rate` = successes / `len(rounds)` (all rounds, `:194`); dashboard = successes / joined-feedback-count, dropping un-joined feedback (`metrics_timeseries.py:236-237`). Diverges whenever a pack-assembly event is missing or pruned.
3. **Windowing.** Dashboard buckets per UTC calendar day (`_bucket_key:130-135`); eval scalars are corpus-wide over all rounds. A run crossing midnight UTC splits into per-day dashboard points that individually ≠ the eval scalar.
4. **Zero-served handling.** Dashboard skips feedback with `served_count==0` (`metrics_timeseries.py:295`); eval includes all rounds in the pooled sums (harmless for the ratio, but a structural filter difference).
5. **Three metrics with no counterpart.** `advisory_fitness` mean, `noise_tag_volume`, `parameter_promotions` are computed from EventLog audit-event **counts**, whereas the eval tracks cumulative in-process loop return-value tallies (or nothing at all for `parameter_promotions`).

The docstrings in `metrics_timeseries.py` (e.g. `:320-326`) **overstate parity** — they cross-reference eval helpers conceptually, but the numbers are not identical by construction.

## 3. Scenario inventory & claim coverage

| Scenario | Kind | Real LLM? | C1 | C2 | C3 | Primary metric / note |
|---|---|---|---|---|---|---|
| `agent_loop_convergence` | convergence | no | ✓ (weak) | ✓ | ✓ | Canonical baseline, 30 rounds. Precise baseline → `useful_delta` near zero by design; suppression branch not organically exercised |
| `agent_loop_convergence_degraded` | convergence | no | ✓ **strong** | ✓ **strong** | — | 15 distractors/domain, 4-item budget, 200 rounds. Cleanest deterministic C1+C2; pass requires `useful_delta >= +0.10`. No C3 by design |
| `program_convergence` | convergence | no | ✓ (axes A/B) | ✓ | ✓ **fullest** | **Only deterministic source** for C3 advisory-hit-rate (axis C) + program axes D–I |
| `program_regression_suite` | deterministic | no | ✓ (gate) | — | ✓ (gate) | CI gate over program_convergence axes; C2 not explicitly gated |
| `multi_backend_feedback` | equivalence | no | — | ✓ (robustness) | ✓ (robustness) | Proves loop counters are backend-independent; no fresh claim |
| `agent_loop_convergence_real_llm` | real_llm | **yes** | ✓ | ✓ | ✓ | Phase A; adds cost/latency telemetry. Needs MOONSHOT+OPENAI; $1.00 cap |
| `dbt_corpus_convergence` | real_llm | **yes** | ✓ | ✓ | ✓ | Phase B-1, real Jaffle Shop manifest. **Fails (not skips)** without creds |
| `github_corpus_convergence` | real_llm | **yes** | ✓ | ✓ | ✓ | Phase B-2, trellis-ai PR snapshot. **Fails (not skips)** without creds |
| `program_convergence_real_llm` | real_llm | **yes** | ✓ | — | ✓ | E3, real embeddings. Cleanly skips without `OPENAI_API_KEY`; has mock hatch; $2.00 cap |
| `skill_loop_convergence` | skeleton | no | (planned) | — | (planned) | **NotImplementedError / status=skip. Substantiates nothing yet** |
| `_example` | smoke | no | — | — | — | Runner-harness smoke test only |
| `multi_backend_equivalence` | equivalence | no | — | — | — | Storage-layer recall/equivalence; not a convergence scenario |

**Claim coverage — every claim has at least one deterministic scenario; none is real-LLM-only:**

- **C1** — deterministic: `agent_loop_convergence_degraded` (strong), `program_convergence` (axes A/B), `program_regression_suite` (gate); `agent_loop_convergence` is deterministic but a **weak** signal.
- **C2** — deterministic: `agent_loop_convergence_degraded` (strongest), `agent_loop_convergence`, `program_convergence`; `multi_backend_feedback` proves backend-equivalence.
- **C3** — deterministic: `program_convergence` (axis C + D–I, the fullest source), `agent_loop_convergence` regime-shift mode (exercises the suppression branch), `program_regression_suite` (gate).

**Gaps to flag:**
- The **strongest single C1+C2 deterministic demonstration concentrates in one scenario** (`agent_loop_convergence_degraded`).
- The **only deterministic C3 advisory-hit-rate (axis C) signal lives in `program_convergence` / `program_regression_suite`** — single point of coverage.
- `skill_loop_convergence` (intended for C1 axis Q + C3 axis R) is a skeleton and **not usable for Step 3 yet**.

## 4. Live run result

**Scenario executed:** `agent_loop_convergence` (deterministic, no API), against the live `~/.trellis` SQLite registry.
**Command:** `.venv/bin/python -m eval.runner --scenario agent_loop_convergence --config-dir ~/.trellis`
**Status:** `pass` in **0.173s** — 30 rounds × 3 domains, 18 traces ingested, 22 entities upserted, 6 distractors planted.
**Report:** `eval/reports/report-20260617T034326_925972Z.json`

Captured numbers:

| Metric | Value | Claim |
|---|---|---|
| `round_success_rate` | 1.0 | C1 |
| `round_useful_fraction_overall` | 0.6475 | C1 |
| `convergence.useful_delta` | **+0.5714** (Q1 0.4286 → Q4 1.0) | C1 |
| `convergence.weighted_delta` | **+0.0975** (Q1 0.903 → Q4 1.0) | C1 |
| `round_total_items_served / referenced` | 139 / 90 | C1 |
| `loops.noise_items_tagged_total` | **100** | C2 |
| `loops.advisories_generated_total` | 2 | C3 |
| `loops.advisories_boosted_total` | **10** | C3 |
| `loops.advisories_suppressed_total` | **0** | C3 (see note) |
| `loops.effectiveness_runs / advisory_runs` | 6 / 6 | — |
| per-domain success rate (all 3 domains) | 1.0 | — |

**On `advisories_suppressed_total = 0`:** this is the **expected, documented** result in default mode, not a failure. Per the scenario README, default mode demonstrates convergence but does not organically exercise the suppression branch — the per-domain success leveled at 1.0 prevents anti-pattern advisories from forming. Suppression is exercised only via the opt-in regime-shift mode and unit tests (`test_suppresses_failing_advisory`, `test_auto_restore_when_evidence_recovers`).

**Caveats on what this run does and does not show:**
- This is the **weak** C1 corpus — yet on this live run `useful_delta` came in strongly positive (+0.5714). The earlier "near-zero on precise baseline" characterization is the design caveat for the scenario; this particular registry produced a clear lift.
- **No `*_real_llm` scenario was run.** Cost/latency telemetry and semantic-retrieval behavior are unmeasured here.
- **Dashboard parity was confirmed by construction, not by a live render** (`dashboard_confirmed=true`): the dashboard path imports the same `join_pack_feedback` and reads the same `FEEDBACK_RECORDED` + `PACK_ASSEMBLED` events the scenario emits. **This is parity-by-construction, and Section 2 shows that construction-level parity is itself only partial** — no pixel-level dashboard render was executed.

## 5. The assessment's claims & evidence plan

### C1 — retrieval lift
**Claim:** As an agent accumulates traces over N rounds, retrieval improves and round success / useful-fraction rises (`convergence.useful_delta > 0`, `round_success_rate` high/rising).
**Substantiating scenarios + metrics:**
- Deterministic primary: `agent_loop_convergence_degraded` — `useful_delta >= +0.10` Q1→Q4 climb (the designed "improves with use" curve).
- Deterministic supporting: `program_convergence` axes A (pack quality) + B (useful item fraction); `program_regression_suite` gates these thresholds; `agent_loop_convergence` (live: `useful_delta +0.5714`, weighted `+0.0975`).
**Real-LLM required?** **No** for the core claim. A real-LLM run (`agent_loop_convergence_real_llm`, `program_convergence_real_llm`, dbt/github corpora) is needed only to show the lift **holds under real semantic retrieval and on real corpora**, and to attach cost/latency — additive evidence, not a prerequisite.

### C2 — noise demotion
**Claim:** The curation loop measurably demotes noise; pack useful-fraction rises as distractors are tagged out.
**Substantiating scenarios + metrics:**
- Deterministic primary: `agent_loop_convergence_degraded` — heavy distractor pool → `loops.noise_items_tagged_total` + Q1→Q4 useful-fraction trajectory (cleanest curve).
- Deterministic supporting: `agent_loop_convergence` (live: 100 noise items tagged), `program_convergence`; `multi_backend_feedback` proves the counter is backend-independent.
**Real-LLM required?** **No.** Real-LLM scenarios re-run identical noise-tagging math under real summaries/embeddings — confirmatory, not required.

### C3 — promote-loop durable value
**Claim:** The advisory/promote loop and program self-improvement axes add value that persists (advisories boosted and landing in successful rounds; program axes D–I trend up).
**Substantiating scenarios + metrics:**
- Deterministic primary: `program_convergence` — `axis.C_advisory_hit_rate.delta` plus axes D–I (observation enrichment, provenance queryability, extraction-failure cluster decay, schema-evolution candidates, meta-trace density, self-authored proposals). This is the **only** deterministic axis-C source.
- Deterministic supporting: `agent_loop_convergence` regime-shift mode (exercises the suppression branch end-to-end); `program_regression_suite` gates axes C–I; `multi_backend_feedback` proves promote/suppress counters are backend-independent.
- Live `agent_loop_convergence` shows the boost half (`advisories_boosted_total=10`) but `suppressed=0` (default mode does not exercise suppression).
**Real-LLM required?** **No** for the deterministic claim. **Open caveat:** the suppression branch is only exercised deterministically via opt-in regime-shift mode + unit tests — it has **not** been shown in a standard live run. A real-LLM corpus run (dbt/github) would strengthen C3's "durable on real data" framing but is not required to substantiate the core claim.

## 6. Open items / next moves

**Instrument drift to fix (blocks "dashboard == eval" framing):**
1. **Reconcile windowing.** Dashboard buckets per UTC day; eval is corpus-wide. Either expose a corpus-wide (single-bucket) dashboard view for parity, or document that per-day points equal the eval scalar only for single-UTC-day runs. (`metrics_timeseries.py:130-135` vs. `_convergence_common.py:194,197-199`.)
2. **Reconcile the denominator.** Dashboard drops un-joined feedback (`metrics_timeseries.py:236-237`); eval uses all rounds. Decide which is canonical and align, or document the divergence on missing/pruned pack-assembly events.
3. **Correct the docstrings.** `metrics_timeseries.py:320-326` (and the `pack_success_rate` / `reference_rate` docstrings) claim parity that holds only conditionally. Downgrade the language to "matches under single-UTC-day + fully-joined + served>0" so the assessment doesn't cite an overstated guarantee.
4. **Stop implying `advisory_fitness`, `noise_tag_volume`, `parameter_promotions` mirror eval numbers** — they have no eval counterpart (or a different computation path). Either add the matching eval metric (e.g. a mean-confidence and an emitted-event count on the eval side) or label these dashboard-only.

**Missing deterministic coverage:**
5. **Single-point C3 axis-C risk.** The only deterministic advisory-hit-rate signal lives in `program_convergence` / `program_regression_suite`. Add a second deterministic source or accept the concentration explicitly.
6. **Suppression branch not in any standard live run.** `advisories_suppressed_total` is exercised only via opt-in regime-shift mode + unit tests. To claim the demote half of C3 from a live run, execute the regime-shift mode and capture it.
7. **`skill_loop_convergence` is a skeleton** (NotImplementedError, status=skip). Its intended C1 axis-Q retrieval-lift and C3 axis-R variant-survival signals are unavailable. Flag as not-yet-usable; do not cite for Step 3.

**Blocking a fully defensible assessment:**
8. **No real-LLM run has been executed.** Every number in Section 4 is deterministic/synthetic. Claims that *only* a real-LLM run can show — semantic-retrieval lift on real corpora, and the cost/latency envelope — are **open**. The dbt/github corpus scenarios **fail rather than skip** without `MOONSHOT_API_KEY` + `OPENAI_API_KEY`; `program_convergence_real_llm` skips cleanly and offers a `TRELLIS_EVAL_REAL_LLM_MOCK=1` hatch (a smoke path, not real substantiation). Running at least one real-LLM scenario within its cost cap ($1.00 / $2.00) is the next move to close C1/C2/C3 on real data.
9. **Dashboard parity is asserted, never rendered.** No live dashboard render against the eval event store has been done. Given the drift in Section 2, a literal render-and-compare on a single-UTC-day run is needed before claiming the operator sees the certified numbers.
