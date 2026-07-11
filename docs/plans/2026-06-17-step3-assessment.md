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
| `skill_loop_convergence` | deterministic (reference-driver) | no | ✓ (axis Q) | — | ✗ (axis R reference-only) | Implemented 2026-07-02 (#249); opt-in `TRELLIS_EVAL_SKILL_LOOP=1`. Q citable for C1; R measurement-path-only — see §6 item 7 |
| `_example` | smoke | no | — | — | — | Runner-harness smoke test only |
| `multi_backend_equivalence` | equivalence | no | — | — | — | Storage-layer recall/equivalence; not a convergence scenario |

**Claim coverage — every claim has at least one deterministic scenario; none is real-LLM-only:**

- **C1** — deterministic: `agent_loop_convergence_degraded` (strong), `program_convergence` (axes A/B), `program_regression_suite` (gate); `agent_loop_convergence` is deterministic but a **weak** signal.
- **C2** — deterministic: `agent_loop_convergence_degraded` (strongest), `agent_loop_convergence`, `program_convergence`; `multi_backend_feedback` proves backend-equivalence.
- **C3** — deterministic: `program_convergence` (axis C + D–I, the fullest source), `agent_loop_convergence` regime-shift mode (exercises the suppression branch), `program_regression_suite` (gate).

**Gaps to flag:**
- The **strongest single C1+C2 deterministic demonstration concentrates in one scenario** (`agent_loop_convergence_degraded`).
- The **only deterministic C3 advisory-hit-rate (axis C) signal lives in `program_convergence` / `program_regression_suite`** — single point of coverage.
- `skill_loop_convergence` is implemented (reference-driver build, 2026-07-02): axis Q now adds a second deterministic C1 lift source; axis R remains **not citable for C3** (reference evolver — §6 item 7).

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
- Deterministic primary: `program_convergence` axes D–I (observation enrichment, provenance queryability, extraction-failure cluster decay, schema-evolution candidates, meta-trace density, self-authored proposals). **Caveat (confirmed 2026-06-24):** `axis.C_advisory_hit_rate` itself is currently `0.0` — and so is `agent_loop_convergence`'s new `loops.advisory_hit_rate` second source — because no item-scoped advisory survives into a pack on the synthetic corpus (see §6.5). So C3's deterministic weight rests on axes D–I and the boost/suppress counts, **not** on a positive advisory-hit-rate.
- Deterministic supporting: `agent_loop_convergence` regime-shift mode (exercises the suppression branch end-to-end); `program_regression_suite` gates axes C–I; `multi_backend_feedback` proves promote/suppress counters are backend-independent.
- Live `agent_loop_convergence` shows the boost half (`advisories_boosted_total=10`) but `suppressed=0` (default mode does not exercise suppression).
**Real-LLM required?** **No** for the deterministic claim. **Open caveat (empirically confirmed 2026-06-24):** the suppression *count* is **not** demonstrable from any standard live run on the current corpus. A regime-shift run (`regime_shift_round=15`, `advisory_min_sample_size=2`, 30 rounds) drove `round_success_rate` to 0.5 and `useful_delta` to −0.0952 and adjusted advisory confidence *downward* (`lift=-0.6`, `new_confidence` 0.66→0.582) — yet `advisories_suppressed_total` stayed **0**. The demote *mechanism* is therefore observable live (downward confidence blending), but the demote *event* (suppression) is unit-test-only on this corpus by design (the §5.5.1 row-3 fix levels pre-shift per-domain success to 1.0, so anti-pattern advisories never form). A real-LLM corpus run would strengthen C3's "durable on real data" framing but is not required to substantiate the core claim.

## 6. Open items / next moves

**Instrument drift (resolution decided 2026-06-24 — document as intentional, no behavior change):**
1. **Windowing — RESOLVED (documented).** Dashboard buckets per UTC day; eval is corpus-wide. Decision: keep both as-is. Per-day bucketing is the operational trend view; the eval scalar is a single-run certification number — they answer different questions and coincide only for a single-UTC-day run. Framed as intentional in the `metrics_timeseries.py` module docstring. (`metrics_timeseries.py:130-135` vs. `_convergence_common.py:194,197-199`.)
2. **Denominator — RESOLVED (documented).** Dashboard drops un-joined feedback (`metrics_timeseries.py:236-237`); eval counts all rounds. Decision: keep the dashboard's join-drop (an un-joined feedback row has no pack to attribute success to) and document the divergence rather than align. Noted in the `_compute_pack_success_rate` docstring.
3. **Correct the docstrings — DONE.** `metrics_timeseries.py` module + `_compute_pack_success_rate` / `_compute_reference_rate` / `_compute_advisory_fitness` docstrings now state the conditional parity and point here. (Commit `f7e9d63`.)
4. **Label dashboard-only metrics — DONE.** `advisory_fitness`, `noise_tag_volume`, `parameter_promotions` are now labeled dashboard-only (no eval counterpart) in their docstrings. (Commit `f7e9d63`.) Adding matching eval-side metrics is possible future work but not required for a defensible assessment.

**Missing deterministic coverage:**
5. **C3 advisory-hit-rate is vacuous on the current corpus — second source added, signal still needs corpus work (commit `098988f`).** A second deterministic source now exists: `agent_loop_convergence` emits `loops.advisory_hit_rate` via a shared `compute_advisory_hit_rate` formula (the same one `program_convergence` axis C now delegates to). **But running both confirmed the signal is `0.0` in each** — not a coverage-count problem, a vacuous-signal problem. Root cause: an item-scoped advisory only stamps `injected_advisory_ids` when `advisory.entity_id == item.item_id` (ENTITY/ANTI_PATTERN categories); on the synthetic corpus the lone entity advisory targets a *distractor* doc that the effectiveness loop tags noise and the PackBuilder excludes, so no stamped item ever reaches a pack (`total_presented == 0`). The advisory-hit-rate metric therefore substantiates C3 only once a scenario stages an item-scoped advisory on a doc that *survives* into successful packs. **The measurement path is now proven** (commit `0a28aef`): an opt-in `seed_reference_advisory=True` knob on `agent_loop_convergence` seeds a high-confidence ENTITY advisory per required-coverage doc and drives `loops.advisory_hit_rate` `0.0 → 1.0` end-to-end, with `useful_delta` provably unchanged (provenance stamping is pure annotation). What remains is the narrower **organic-generation gap**: the AdvisoryGenerator does not, on the synthetic corpus, *form* a helpful item-scoped advisory that survives noise-filtering (required docs appear in nearly all packs → no measurable presence differential → no advisory). Closing that needs corpus tuning with real blast radius (it would move the convergence deltas several tests assert) — deliberately deferred, not attempted. Until then the live organic C3 signal rests on `advisories_boosted_total` / `advisories_suppressed_total` + the unit tests, **not** on a positive advisory-hit-rate. `program_regression_suite`'s `THRESHOLD_C_LAST_QUARTER=0.0` passes vacuously and does not guard against this. **Organic generation demonstrated (2026-07-02, #248):** an opt-in `stage_organic_advisory=True` mode on `agent_loop_convergence` plants one probe doc and stages a deterministic presence differential (probe present on every 3rd staged-domain visit; threshold in (3/4, 1.0] so probe absence alone fails the round) — the `AdvisoryGenerator` then **forms the ENTITY advisory itself** from its own statistics over real pack/feedback event joins (nothing pre-seeded), the advisory survives noise-filtering, stamps later packs, and `loops.advisory_hit_rate` goes organically positive (0.67 on the reference knobs). The **default corpus remains deliberately untouched** (its axis C stays vacuously 0.0 and the regression-suite threshold note above still stands); the risky default-corpus tuning is thereby superseded — both halves of the claim (measurement path via seeding, organic formation via staging) now have deterministic demonstrations.
6. **Suppression branch not demonstrable live on the current corpus — confirmed.** Running regime-shift mode (2026-06-24) still yields `advisories_suppressed_total=0` (it produces downward confidence *adjustment*, `lift=-0.6`, but no suppression event), because the §5.5.1 row-3 fix levels pre-shift per-domain success to 1.0. To capture a non-zero live suppression count, a scenario variant with *non-uniform* pre-shift per-domain success is needed (pre-row-3 conditions); until then the demote half of C3 rests on `test_suppresses_failing_advisory` + the live downward-confidence signal, not a live suppression count.
7. **`skill_loop_convergence` — IMPLEMENTED as the reference-driver build (2026-07-02, issue #249).** The scenario now runs end-to-end behind `TRELLIS_EVAL_SKILL_LOOP=1` with 13 unit tests. Evidence rules after this build: **axis P (coverage) and axis Q (retrieval lift) are real measurements** — enrichment writes go through the governed `MutationExecutor`, packs through the real `PackBuilder`, scoring through the real `evaluate_pack` assembly-time hook emitting real `PACK_QUALITY_SCORED` events; Q's lift (consolidation of fragmented notes under a fixed pack budget, +0.22 on the smoke run, stability panel flat) **is citable for C1**. **Axis R is NOT citable for C3/F5**: the curator and evolver are deterministic scenario-local stand-ins (`_ReferenceCurator` / `_ReferenceEvolver`); R validates only that score-based pruning driven by *measured* pack scores finds deliberately weakened variants (falls-then-plateaus) — the same "measurement path proven, organic signal pending" posture as item 5's `seed_reference_advisory`. The report carries this disclaimer as a finding. F1–F5 production machinery remains gated in TODO.md Phase F; the drivers are the plug-in seam.

**Blocking a fully defensible assessment:**
8. **No real-LLM run has been executed — deferred by choice (2026-06-24).** Every number in Section 4 is deterministic/synthetic. Claims that *only* a real-LLM run can show — semantic-retrieval lift on real corpora, and the cost/latency envelope — remain **open by decision**: the deterministic C1/C2/C3 claims already stand, so the real-LLM run was deferred rather than blocking. When picked up, run `program_convergence_real_llm` (cleanly skips/caps at $2) or `agent_loop_convergence_real_llm` with `MOONSHOT_API_KEY` + `OPENAI_API_KEY`; the dbt/github corpus scenarios **fail rather than skip** without those creds.
9. **Dashboard parity — RENDERED AND CONFIRMED (commit `8bc319e`).** `tests/unit/eval/test_dashboard_eval_parity.py` now renders the dashboard (`compute_timeseries`) against the exact EventLog a convergence scenario emitted and asserts the values equal the eval's in-process metrics, on a non-trivial regime-shift run: eval `round_success_rate` 0.5 == dashboard `pack_success_rate` 0.5 (repooled across buckets), eval `round_useful_fraction_overall` ~0.43 == dashboard `reference_rate`. This upgrades §2's two matching metrics from parity-by-construction to parity-by-render under the single-UTC-day, fully-joined condition. (The three dashboard-only metrics still have no eval counterpart by design — §6 item 4.)

## 7. Reproduction on current HEAD (2026-07-10)

The §4 live run (2026-06-17) predates the MCP-over-HTTP work and the MinHash
lock fix. Re-ran the full deterministic suite on branch
`feat/mcp-http-transport` (HEAD `a8e5d88`) against **isolated scratch
registries** (one fresh SQLite config+data dir per scenario — the live
`~/.trellis` registry is the user's real agent memory and was **not** touched;
the 2026-06-17 run's use of `~/.trellis` is not repeated). Command shape:
`.venv/bin/python -m eval.runner --scenario <name> --config-dir <fresh> --data-dir <fresh>`.

**Every deterministic scenario still passes, and the baseline numbers are
byte-identical to §4** — the assessment is stable across the entire codebase
move, which is exactly the "reproducible and defensible" bar Step 3 sets.

| Scenario | Status | Key numbers (this run) | vs. prior |
|---|---|---|---|
| `agent_loop_convergence` (baseline) | `pass` (1.2s) | `useful_delta +0.5714`, `weighted_delta +0.0975`, served/ref 139/90, noise 100, boosted 10, suppressed 0, hit_rate 0.0 | **identical to §4** |
| `agent_loop_convergence_degraded` | `pass` (1.2s) | `useful_delta +0.49` (gate +0.10), `success_rate 0.915`, `useful_fraction 0.7349`, noise 851, boosted 34 | consistent (strong C1+C2) |
| `program_convergence` | `pass` (3.8s) | axis A +0.079, **axis B +0.500**, C 0.0 (vacuous, documented), D–I as designed; noise 73, boosted 5 | consistent |
| `program_regression_suite` | `pass` (2.2s) | **all 9 axis gates PASS + 4 satellites PASS** (A 0.0545, B 0.3438, C 0.0 vacuous-pass, D 11.82, E 1.0, F 0.0, G 1.0, H 1.0, I 1.333) | consistent (CI gate green) |

**C3 advisory-hit-rate — both opt-in demonstrations reconfirmed on HEAD** (the
single weakest number in §5; §6 item 5's "measurement path proven, organic
formation demonstrated" both re-verified live):
- `seed_reference_advisory=true` → `loops.advisory_hit_rate` **0.0 → 1.0** with
  `useful_delta` provably **unchanged at +0.5714** (provenance stamping is pure
  annotation, does not move convergence).
- `stage_organic_advisory=true` (`success_coverage_threshold=0.8`,
  `advisory_min_sample_size=2`) → `organic_advisories_formed=1`,
  `loops.advisory_hit_rate` **0.5962** organically (AdvisoryGenerator forms the
  advisory from its own event statistics, nothing pre-seeded).

**Net:** C1, C2, and C3 all stand deterministically on the shipping branch. The
two documented zeros (`axis.C=0.0`, `suppressed_total=0` in default mode) remain
corpus artifacts, not mechanism failures — both are driven positive by the
opt-in knobs above / the unit tests, exactly as §6 items 5–6 describe.

**Real-LLM status (open item #8) — still open, now with a concrete environment
read.** No cloud creds are present in this environment (`OPENAI_API_KEY`,
`MOONSHOT_API_KEY`, `ANTHROPIC_API_KEY` all unset). Local Ollama **is** up
(`hermes3:8b` chat, `nomic-embed-text` embeddings, OpenAI-compatible at
`localhost:11434/v1`). Three ways forward, in ascending cost/confidence:
- **Leave deferred** — the deterministic C1/C2/C3 claims already stand; #8 only
  adds "holds under real semantic retrieval on real corpora" + a cost envelope.
- **Local-Ollama code-path smoke** (~free) — repoint the real-LLM factory's
  chat + embedder `base_url` at Ollama and set `embedding_dim=768`
  (`nomic-embed-text`). Proves the real-LLM path executes end-to-end against a
  live model, but yields **no production-grade quality signal and no real cost
  envelope** — the two things #8 actually wants. A smoke, not the evidence.
- **Cloud run** ($1–2 capped) — the real evidence. `program_convergence_real_llm`
  caps at $2 and skips cleanly; `agent_loop_convergence_real_llm` needs
  `MOONSHOT_API_KEY` + `OPENAI_API_KEY`. **Requires a spend decision + creds via
  1Password — a user call, not taken here.**
