# `eval/` — Trellis evaluation harness

This directory is **not** part of the published `trellis-ai` package.
It's the in-repo home for evaluation scenarios that exercise Trellis
against realistic-shaped workloads and produce signal that unblocks the
Phase 3 deferred items in
[`../docs/design/plan-neo4j-hardening.md`](../docs/design/plan-neo4j-hardening.md).

The strategy doc that explains *why* this directory exists, what lives
where, and what the first scenarios are is
[`../docs/design/plan-evaluation-strategy.md`](../docs/design/plan-evaluation-strategy.md).
Read that before adding work here.

## Layout

```
eval/
├── runner.py          # CLI + library entry point: discover, run, write reports
├── conftest.py        # shared pytest fixtures
├── scenarios/         # one subpackage per scenario; each exposes scenario.run()
├── datasets/          # small committed fixtures (≤ a few MB total)
├── generators/        # synthetic data generators (deterministic, seeded)
├── metrics/           # reusable metric helpers
└── reports/           # gitignored — runner output (.gitkeep only)
```

## How to run

From the repo root, with the `dev` extra installed:

```bash
# List discovered scenarios
python -m eval.runner --list

# Run the no-op smoke scenario
python -m eval.runner --scenario _example

# Run everything against a real config dir
python -m eval.runner --scenario all --config-dir ~/.trellis
```

Reports land under `eval/reports/report-<UTC>.{json,md}`. The runner
exits non-zero if any scenario reports `fail` or `regress`.

## How to add a scenario

1. Create `eval/scenarios/<name>/scenario.py` with a single
   `run(registry: StoreRegistry) -> ScenarioReport` function.
2. Put any synthetic input under `eval/generators/` and any committed
   fixtures under `eval/datasets/` (small only — multi-MB binary blobs
   get fetched at runtime, not committed).
3. Update §5 of the strategy doc with the actual measured numbers and
   which Phase 3 item became actionable.

The `_example` scenario is the minimal template.

## What this is **not**

* Not a pytest replacement. The unit-test suite in `tests/` is still the
  authority on correctness; eval scenarios test *behaviour at workload*.
* Not a CI gate. Scenarios are heavyweight (the populated-graph one is
  ~30 min); they run on a schedule or on `workflow_dispatch`, not on
  every push. The only CI-resident piece is the runner smoke test in
  `tests/unit/eval/`.
* Not a published package surface. Hatch's wheel/sdist whitelists never
  list `eval/`, so `pip install trellis-ai` doesn't ship it.
