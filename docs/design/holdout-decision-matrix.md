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

The seal is read from the environment of the process that runs the analysis.
Set it for every way the command is run: the server, a scheduled job, and any
host shell or wrapper script. One that lacks it is unsealed.

## The rule for reading the result

Read the primary outcome once, at the horizon, with the analysis the
pre-registration named. The primary outcome is the log of a task's assistant
turns (`log1p_turns`), and lower is the good side: the pre-registration reads
fewer turns as less effort. The decision uses two things together:

- **The estimate and its interval.** The stratum-weighted difference in the
  primary outcome, served minus withheld, with its 95% confidence interval. The
  question is whether the interval excludes zero, and on which side. Below zero
  means served tasks took fewer turns.
- **The report's own status.** A report that refuses (a window that has not
  settled, several holdout rates, no withheld arm, a truncated scan) has no
  result. A refusal is not a null: fix what it names and read again.

**The guardrails do not decide.** The pre-registration lists them as reported
and never decisive, and the report gives each one per arm with no interval,
p-value or verdict, so there is no threshold for one to breach. They qualify
how a result is worded. The pre-registration reads fewer turns as efficiency
when the PR rate is unchanged, and says more turns needs the guardrails to
interpret. The readout quotes them beside the estimate, and says so when one
cuts against it.

Do not read a secondary figure, a per-build breakdown or an adjusted fit to
rescue a primary that did not decide. They are descriptive, and the report
labels them so.

## What the result covers

The readout states every outcome with this scope, because the estimate supports
nothing wider.

- **The unit.** A finished sub-agent task whose first retrieval built a
  non-empty pack. A task that never retrieved is outside the estimate, so the
  result says nothing about agents that do not call retrieval.
- **The treatment.** The pack as deployed, averaged over the builds that
  served during the window, not any single build.
- **The sessions.** Strata are parent session by ISO week, so the result
  describes the sessions that dispatched tasks in the window. When one session
  dispatched most of them, it is a result about that session's work. The
  readout quotes the largest parent session's share of eligible tasks
  (`descriptive.top_parent_share`) and the heaviest stratum's share of the
  estimator's weight (`inference.top_stratum_weight_share`) beside the
  estimate.
- **Spillover.** A withheld task still learns from its parent session's
  brief, which pulls the estimate toward zero. "No effect" is read with that in
  mind, and the size of a decided result is likely understated.

## The three outcomes

### Serving helps: the interval lies below zero

Serving packs pays, by the measure the experiment set: served tasks took fewer
turns. Call it efficiency only when the PR rate is about the same in both arms.
If the served arm opened fewer PRs, the readout says so beside the result. The
actions are the same either way.

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

### Harm: the interval lies above zero

Served tasks took more turns, which is worse by the measure the experiment set.
A guardrail figure does not put a result here, and does not take one out.

- Raise the holdout rate, so fewer tasks are exposed while the cause is found.
- Make retrieval opt-in for dispatched agents instead of the default.
- Investigate before any further serving change: what the guardrails show
  (more turns with more PRs or commits is a different finding from more turns
  with none), what the packs served to the tasks that did worse, whether the
  harm concentrates in a build, an agent type or a pack form, and whether it is
  an artefact of how outcomes are recorded. The `--by-commit` table is the
  place to look once the primary has decided, and only as a lead.

## The read

Two runs, each made once, over the same window. The window end, length and rate
are the experiment's own; [`ROADMAP.md` gate N4](../ROADMAP.md) gives the full
command for the experiment this repository runs.

1. **The primary.** `trellis analyze holdout --until <horizon> --days <days> --rate <rate> --by-commit --limit <cap> --format json`.
   The outcome above is read from this run and no other.
2. **The first sensitivity run.** The same command with `--itt`, which keeps
   the tasks cut off by a usage limit. The pre-registration names it. It is
   recorded beside the primary and cannot rescue or overturn it.

A run that is refused carried no figure, so repeating it is not a second look.
For a window that has not settled, wait for the sweep the refusal names. For a
truncated scan, raise `--limit` and keep `--until`, `--days` and `--rate` as
they were.

## What this page does not decide

It does not set the horizon, the primary outcome, the planned effect size or the
rate. Those are in the pre-registration and in the configuration the experiment
ran under. It does not cover a read that was started early, or a window that
was repaired mid-run; both start from a note on the experiment, not a row here.

The pre-registration and its amendments hold measurements of the deployment
that ran the experiment, so they are kept in that deployment's private
repository and not here. An amendment lands there before the analysis it
governs. So that the text can be checked against what was written before the
read, each version's SHA-256 is recorded here:

| Text | Committed | SHA-256 |
|---|---|---|
| Pre-registration with Amendment 1 | 2026-10-30 | `a67dbce44e1e65a9e6c0134833144f9412393444c11cd9b7e7fe6a79918fa5c9` |
| The same, with Amendment 2 | 2026-11-17 | `e887c525b225691742cf0bd364a463684c3eb2dca8ecd39e2a1bad81ed7c0a77` |

Amendment 2 changes no rule of the primary. It states the scope of the claim as
this page does, restates that guardrails do not decide, names the good side,
and declares one more descriptive check: how far a single parent session
carries the result. That check cannot overturn or rescue the primary. The
owner delegated the decision on these rules to the agent that drafted them,
which adopted them on 2026-11-17. The text is not owner-signed.
