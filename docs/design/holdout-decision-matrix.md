# Pack holdout: what each result means for what ships next

The pack holdout withholds a share of packs so that the effect of serving them
can be measured against tasks that did not receive them (`TRELLIS_PACK_HOLDOUT_RATE`;
method and every figure in [`operations.md` § `trellis analyze holdout`](../agent-guide/operations.md#trellis-analyze-holdout)).
An experiment like this is only worth running if the answer is written down
before anyone sees it, so this page fixes what each answer commits the project
to. Nothing here depends on the numbers: it names a rule for reading the
result, then three outcomes and the action each one takes.

## Before the horizon

The outcome stays unread until the pre-registered horizon, and the code keeps
that, not the habit of the people running it. Set `TRELLIS_HOLDOUT_READOUT_AFTER`
to that instant (an ISO-8601 date or timestamp, read as UTC) and every
outcome-producing surface refuses until it passes; see
[the readout seal](../agent-guide/operations.md#readout-seal). While it holds,
two things stay open: `trellis analyze holdout --operational`, which says whether
the experiment is running (arm counts, the funnel, capture coverage) and reads
no outcome value, and the freeze on serving changes that keeps the two arms
comparable.

## The rule for reading the result

Read the primary outcome once, at the horizon, with the analysis the
pre-registration named. The decision uses three things together:

- **The estimate and its interval.** The stratum-weighted difference in the
  primary outcome, served minus withheld, with its 95% confidence interval. The
  question is whether the interval excludes zero, and on which side.
- **The guardrails.** The secondary outcomes the report carries beside the
  primary. A guardrail is clean when its interval does not exclude a harm the
  pre-registration said would matter.
- **The report's own status.** A report that refuses (a window that has not
  settled, several holdout rates, no withheld arm) has no result. A refusal is
  not a null: fix what it names and read again.

Do not read a secondary figure, a per-build breakdown or an adjusted fit to
rescue a primary that did not decide. They are descriptive, and the report
labels them so.

## The three outcomes

### The effect is positive: the interval excludes zero and the guardrails are clean

Serving packs pays, by the measure the experiment set.

- Keep a small standing holdout as a monitor, so a later change that erodes the
  effect shows up as a drift in the same estimate. A standing holdout is a
  measuring instrument, not a hold on the roadmap, so it can be small.
- Lift the freeze on serving changes.
- Ship each later serving change as its own flag split, with its own arm and its
  own window, read the same way. A change that rides the same flag as the
  holdout cannot be told apart from it.

### No effect: the interval includes zero

Read this as "no effect larger than the planned minimum detectable effect was
found", which is what the report says, and not as "no effect".

- The packs as currently built do not pay for themselves on this measure.
  Treat that as a finding about these packs, not about retrieval in general.
- Changes that were not part of the treatment still need their own window. A
  redesigned precedent loop, a new strategy or a different pack format were not
  in the tested arm, so this result neither supports nor rules them out.
- No wider rollout until a positive read. The holdout continues at its current
  rate while the next candidate is built, and the next read uses the same
  analysis.

### Harm: the interval excludes zero on the wrong side, or a guardrail is breached

Serving packs made the measured outcome worse.

- Raise the holdout rate, so fewer tasks are exposed while the cause is found.
- Make retrieval opt-in for dispatched agents instead of the default.
- Investigate before any further serving change: what the packs served to the
  tasks that did worse, whether the harm concentrates in a build, an agent type
  or a pack form, and whether it is an artefact of how outcomes are recorded.
  The `--by-commit` table is the place to look once the primary has decided, and
  only as a lead.

## What this page does not decide

It does not set the horizon, the primary outcome, the planned effect size or the
rate. Those are in the pre-registration and in the configuration the experiment
ran under. It does not cover a read that was started early, or a window that
was repaired mid-run; both start from a note on the experiment, not a row here.
