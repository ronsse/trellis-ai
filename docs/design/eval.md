# Eval harness — operator reference

The strategy doc explaining *why* the harness exists and what scenarios
are planned is
[`plan-evaluation-strategy.md`](./plan-evaluation-strategy.md). This
page is the operator-facing how-to: how to run scenarios, how to read
reports, how to add new ones.

## Where it lives

The harness is **not in this repo**. The evaluation program (runner,
scenarios, generators, corpora) moved to the private `trellis-evals`
repo on 2026-07-12, with its history. Every command and path below
(`python -m eval.runner`, `eval/scenarios/`, `eval/reports/`) refers to a
checkout of that repo; see its `README.md` for setup.

What stays here:

* The unit and contract tests. This repo's CI runs no eval scenario, and
  `tests/unit/eval/` went with the harness.
* [`eval/reports/`](../../eval/reports/), which holds eight run reports from
  2026-06-17 to 2026-07-10 as committed records. Nothing in this repo
  writes to it.
* `tests/integration/_live_wipe.py`, vendored from the harness.

The harness is not shipped in the `trellis-ai` wheel: the wheel lists
only the `src/` packages, and the sdist whitelist does not list `eval/`.
If a scenario targets a particular backend (Postgres, Neo4j, pgvector)
install the matching extra too — see the deployment docs under
[`docs/deployment/`](../deployment/) for backend-specific setup.

## Quickstart

Run these from a `trellis-evals` checkout.

```bash
# What scenarios exist
python -m eval.runner --list

# Smoke-test the runner with the no-op scenario
python -m eval.runner --scenario _example

# Run a real scenario (named) against your local config dir
python -m eval.runner --scenario multi_backend_equivalence

# Run everything; useful for the scheduled / dispatched workflow
python -m eval.runner --scenario all --config-dir ~/.trellis
```

The runner exits non-zero when any scenario reports `fail` or `regress`,
so it works as a CI step even though the heavy scenarios aren't run on
every push.

## Reports

Every run writes two files under `eval/reports/`:

| File | Purpose |
|---|---|
| `report-<UTC>.json` | Machine-readable: scenario name, status, metrics dict, findings, decision. Diff this across runs to detect drift. |
| `report-<UTC>.md` | Human-readable summary. Read this first. |

The `decision` field on every scenario is the point of the run — it
states which Phase 3 deferred item the result informs and what the
recommendation is. Metrics without a decision are noise.

In `trellis-evals`, `eval/reports/` is gitignored (`.gitkeep` keeps the
directory). Reports are local artifacts; share them in PR descriptions or
copy notable numbers back into
[`plan-evaluation-strategy.md`](./plan-evaluation-strategy.md) §5 when
they unblock a Phase 3 item.

## Adding a scenario

In a `trellis-evals` checkout:

1. Create `eval/scenarios/<name>/scenario.py`.
2. Expose a single `run(registry: StoreRegistry) -> ScenarioReport`.
   `ScenarioReport` is `dataclass`-shaped; see
   `eval/runner.py`.
3. Generators go in `eval/generators/`, fixtures in `eval/datasets/`.
   Generators must be deterministic given a seed; large datasets are
   fetched at runtime, not committed.
4. After the scenario lands and you've measured something, update §5 of
   the strategy doc with the actual numbers and which Phase 3 item
   became actionable.

The minimal template is `_example/scenario.py`.

## What the runner does *not* do

* No randomization-seed asserts, no statistical-significance harness, no
  baseline-diff regression gates. That's the deferred §7.1 work; it
  comes after the scenarios exist and we know which thresholds matter.
* No persistent reporting / dashboard. Reports are flat files; if a
  scheduled run produces something interesting, the human running it
  decides where it goes.
* No backend provisioning. The runner uses whatever `StoreRegistry` you
  hand it. Configure the registry the way you'd configure any other
  Trellis usage — env vars, `~/.trellis/config.yaml`, or the
  `recommended-config.yaml` shapes.

## CI posture

Nothing runs the eval suite automatically. This repo's CI runs no
scenario (no workflow mentions `eval`, and `tests/unit/eval/` was removed
with the harness), and `trellis-evals` had no `.github/workflows/`
directory when [`decision-ledger.md`](./decision-ledger.md) F-2 measured
it on 2026-08-27. The runner's own smoke test, `tests/test_runner_smoke.py`,
is in `trellis-evals`; run it and any heavyweight scenario by hand. Plan §6
has the per-scenario wall-time expectations.
