# The internal agent: is it reviewing the right things?

**Date:** 2026-09-20 · **Status:** measured, no change made · **Method:** read-only
probes of the reference deployment's operational + knowledge Postgres, plus source
reading against `origin/main` @ `1ef5c9c`.

The ask was to *activate* the internal review loop and confirm it is "addressing the
correct problems and curating the system correctly." The loop is already running — the
finding is not that it is off, it is that **three of the five signals it ranks on cannot
be written by any code path in the repo**, so the review it produces is single-variable
and 91% demotion proposals. Activating the promote half today would persist a fabricated
observation into ~291 durable graph nodes. The metrics have to be fixed first; the
sequence is the deliverable.

All counts are aggregate. Candidate titles and per-domain breakdowns are deliberately
omitted — they carry personal subject matter and this repo is public.

---

## 1. What actually runs, nightly

Four cron jobs on the host, not systemd timers:

| Time (UTC) | Script | What it does |
|---|---|---|
| 03:00 | `capture-nightly.sh` | Session transcripts → distilled memories |
| 03:30 | `curate-nightly.sh` | **The review**: effectiveness, advisories, learning candidates |
| 04:30 | `backup-nightly.sh` | Dumps |
| 05:00 | `roadmap-nightly.sh` | DoD block on the tracking issue |

**All four invoke `$HOME/projects/trellis-ai/.venv/bin/trellis`** — the editable install,
which points at the live working tree. That checkout is not on `main` and is not the
container build. The nightly loop runs whatever branch the working tree happens to sit
on. This is worth knowing before reading any nightly output as evidence about `main`: it
is the third payout of the same trap recorded in `docs/design/swarm-handoff.md` §1.2.

The loop is **much healthier than the stale internal notes claimed**. Thirty days to
2026-09-20, operational event log:

| Event type | Rows |
|---|---|
| `mutation.executed` | 2667 |
| `memory_op.judged` | 1899 |
| `entity.created` | 1370 |
| `link.created` | 1102 |
| `memory.stored` | 862 |
| `pack.assembled` | 216 |
| `trace.ingested` | 186 |
| `feedback.recorded` | 100 |
| `write.rejected` | 91 |
| `advisory.drift_detected` | 63 |
| `advisory.suppressed` | 56 |
| `capture.sweep_completed` | 24 |

Feedback attribution over those 100 events: 81 pack-targeted, 69 naming
`helpful_item_ids`, **62 naming `unhelpful_item_ids`**. `attribution_rate` is **0.75**.
Prior notes recorded 0.32; that number is stale by a wide margin, and the *negative*
signal is now dense — which matters, because §3 is about a reader that never looks at it.

---

## 2. Three of the five ranking metrics have no producer

`analyze_learning_observations` ranks every candidate on five quantities. Measured
across all 291 candidates in last night's `intent_learning_candidates.json`:

| Metric | Distinct values | Range |
|---|---|---|
| `success_rate` | 9 | signal |
| `times_served` | 6 | 2–8 |
| `injection_rate` | **1** | 0.0 everywhere |
| `retry_rate` | **1** | 0.0 everywhere |
| `avg_selection_efficiency` | — | `null` everywhere |

These are not zero because the system is healthy. They are zero because **nothing can
write them.**

- `_accumulate_item` ([`scoring.py:151`](../../src/trellis/learning/scoring.py)) guards
  its increments on `observation["had_retry"]`, `observation["injected"]` and
  `observation["selection_efficiency"]`.
- Those three keys are set by exactly one place —
  [`pack_observations.py:223-229`](../../src/trellis/learning/pack_observations.py) —
  and only `if present` in the upstream payload.
- `PackFeedback` defines neither `had_retry` nor `injected`, so no feedback surface
  *can* emit them. A tree-wide sweep finds every occurrence of all three names to be a
  **reader**. (`injected` does appear as a writer in `retrieve/telemetry.py` and
  `retrieve/pack_value.py` — but nested inside per-strategy stats, a different key at a
  different level, not the top-level feedback field the bridge reads.)
- Confirmed on production rather than inferred: 141 `feedback.recorded` events,
  `had_retry` present on **0**, `injected` on **0**; 239 `pack.assembled` events,
  `selection_efficiency` present on **0**.

This is the repo's signature defect — a measurement path wired to a constant — in the
one component whose entire job is measurement. It is the same shape as C1's
`policies.json` (a full CRUD surface nothing read) and as the four separate instances
recorded in the 2026-08-02 swarm wave.

---

## 3. The classifier is single-variable wearing a two-variable interface

Live thresholds (`~/.trellis/learning_params.yaml`): `promote_success 0.75`,
`promote_retry 0.25`, `noise_success 0.4`, `noise_retry 0.5`.

`_recommend_learning_action` ([`scoring.py:599`](../../src/trellis/learning/scoring.py))
reads:

```python
if success_rate >= promote_success and retry_rate <= promote_retry:   # promote
if success_rate <= noise_success or retry_rate >= noise_retry:        # noise
```

With `retry_rate ≡ 0.0`:

- `retry_rate <= 0.25` is **always true** — the promote conjunct is inert.
- `retry_rate >= 0.5` **never fires** — the noise disjunct is inert.

So the whole classifier collapses to a threshold on `success_rate` alone. The
two-variable rule in the code and in the operator docs describes behaviour the
deployment has never had.

**And the review reads success but never unhelpfulness.** `_accumulate_item` increments
`success_count` on `outcome == "success"` and reads nothing else about item quality. 62
of the last 100 feedback events carry `unhelpful_item_ids`; the learning path consumes
**none** of them.

The consequence is the distribution: of 291 candidates, **265 are `investigate_noise`
and 26 are `promote_guidance`.** The nightly review is 91% demotion proposals, produced
by a rule that cannot see the only dense negative signal the deployment has.

This is precisely the hole [#336](https://github.com/ronsse/trellis-ai/issues/336)
closed one layer over, in `apply_noise_tags` — *demotion requires evidence of
unhelpfulness, never absence of evidence of helpfulness*. The demotion evidence gate
exists, is measured, and is documented in CLAUDE.md. It guards the classify layer's
noise proposals. It does **not** guard the learning layer's, which reach the operator
through a different file.

---

## 4. The promote half works, has never fired, and would mint bad nodes today

The intake path is real and wired: `trellis curate promote-learning --candidates …
--decisions …`, plus a REST twin at `trellis_api/routes/admin.py:937`. It has simply
never been exercised — `precedents_promoted: 0` in every nightly DoD block.

Dry-run through the real CLI, with three `promote_guidance` rows flipped to
`approved: true` in a **scratchpad copy** of the decisions template (the live template
was not touched), returns `approved_count: 3, ready_count: 3` with complete entity
payloads. Two problems are visible in that payload:

1. **`edge_payloads: []` and `target_entity_ids: []`.** A promoted precedent lands as an
   isolated node. That is the same defect already measured at the `save_knowledge` seam,
   where 182 of 196 such nodes are isolated and account for ~80% of the graph's total
   isolation. Promoting 291 candidates would roughly triple it.
2. **The generated description bakes in the dead constant** — the precedent text reads
   `"showed success_rate=1.0 and retry_rate=0.0"`. `retry_rate=0.0` is not an
   observation; it is §2's unwritable field. Activating promote today would persist a
   fabricated measurement into durable, immutable-by-convention graph nodes, where the
   next agent would read it as evidence.

**This reverses the obvious instinct.** "Activate the internal review" reads as *switch
the promote half on*. The measurement says the opposite: switching it on before §2 is
fixed is the one action that makes the problem permanent.

---

## 5. Advisories: 66 generated a night, 0 ever served

- 0 of 239 `pack.assembled` payloads carry an `advisories` key.
- 0 feedback events name a `followed_advisory_id`.
- The `advisory.generated` event type has **0 rows all time** — the generator emits
  `drift_detected` / `suppressed` instead.

Consistent with the four-cause analysis already on record: the flat pack path never
renders advisories at all, and every pack this deployment assembles is flat. The
generator is doing work nothing consumes.

---

## 6. Capture wrote nothing on 5 of the last 12 nights

Last night's sweep: 5 sessions triggered, 4 candidates distilled, 1 rejected on
worthiness, **3 blocked by the secret scanner on `high_entropy_string`**,
`memories_written: 0`. `memory.stored` last fired 2026-09-18.

One hypothesis was checked and **refuted**: the watermark is an `(mtime, size)` cursor
per transcript, so an appended long-running session *is* re-read on the next sweep.
Long sessions are not being captured once and abandoned. The live blocker is the secret
scanner's entropy heuristic firing on session content — worth a separate look, since a
scanner that blocks 3 of 4 candidates is either correctly protecting the corpus or
silently emptying it, and nothing currently distinguishes those.

---

## 7. `parameter_snapshots` is empty

`~/.trellis/data/stores/parameters.db` has one table and **0 rows**. The four thresholds
§3 evaluates against are static YAML written at setup; `worker tune` has never written a
snapshot. So the loop has no record of what it was tuned to, and no way to attribute a
change in candidate mix to a change in threshold.

---

## 8. Decisions

Ordered by what each unblocks. Items 1–3 are the internal agent; the rest is the
standing queue.

| # | Decision | Recommendation | Blocks |
|---|---|---|---|
| **1** | Fix the three unwritable metrics, or delete them? | **Fix `had_retry` + `injected` on `PackFeedback` and stamp `selection_efficiency` on `PACK_ASSEMBLED`.** Deleting them leaves a single-variable classifier honestly labelled, which is the cheaper option and a defensible second choice — but §3's real gap is the *unhelpful* signal, and that needs the same edit anyway. | 2, 3 |
| **2** | Should the learning path read `unhelpful_item_ids`? | **Yes — port #336's evidence gate.** The signal is dense (62/100) and the current 265/291 demotion rate is the exact failure #336 measured. | 3 |
| **3** | Activate `promote-learning`? | **Not yet.** Only after 1 and 2, and only with an edge-minting step, or it triples graph isolation and persists a fabricated `retry_rate=0.0`. | — |
| **4** | Merge order for the open PR stack | **#555 first** — it is the green fix for the `live-infra` red that most open PRs inherit. Then #584→#595, #596/#597/#598, #571's marker conjunct, #599, #600, #601, #583. | everything |
| **5** | Close #578? | Yes — M2 ported its surviving relation into #583. | — |
| **6** | The nightly loop runs the editable install on a plans branch | Decide whether that is intentional. If yes, it should be written down; if no, it is a one-line change to pin the venv. | reading any nightly output as evidence |
| **7** | Advisories generated but never served | Either render them on the flat pack path or stop generating 66 a night. | §5 |
| **8** | Secret scanner blocking 3 of 4 capture candidates | Needs a sample review to tell "protecting" from "emptying". | §6 |

Standing items from the corpus (§5 items 1–12) are unchanged: #596's `write_config`
veto, the 50-note prod link backfill, the alias backfill held until #530 ships, the
`my-claude-skills` items and the `feat/panel-multi-provider` merge, trigger dry-runs,
`backup-health pg_restore`, promoting the install-refusal test to `tools/`, the
UI-commits question, and the `[cloud]` extra rename.

---

## 9. Next tasks, in order

1. **Make the three metrics writable** — `PackFeedback` gains `had_retry` / `injected`;
   `PackBuilder` stamps `selection_efficiency` onto `PACK_ASSEMBLED`. Pin each with a
   test that fails when the field is absent from a real emitted event, not merely when a
   synthetic fixture carries it.
2. **Port the #336 evidence gate into `learning/scoring.py`** — `_accumulate_item` learns
   to count unhelpful citations; `_recommend_learning_action` requires them before
   proposing noise. Report the proposal and the verdict separately, exactly as
   `EffectivenessReport` does, so a proposal that shrinks at the gate stays visible.
3. **Add an anti-vacuity test for the classifier** — assert that both conjuncts are
   *reachable* on the live corpus, not just that the function returns a label. §3 is
   invisible to every existing test because they all feed synthetic observations that
   carry the fields production cannot produce.
4. **Give a promoted precedent its edges** before anything is promoted.
5. Then, and only then, run `promote-learning` for real on a small approved subset.

Items 1–4 are ordinary reversible code work. Item 5 is a production mutation and is the
owner's.
