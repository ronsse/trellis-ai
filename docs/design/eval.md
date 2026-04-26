# Eval harness — operator reference

The strategy doc explaining *why* the harness exists and what scenarios
are planned is
[`plan-evaluation-strategy.md`](./plan-evaluation-strategy.md). This
page is the operator-facing how-to: how to run scenarios, how to read
reports, how to add new ones.

## Where it lives

The harness is in the repo root at [`eval/`](../../eval/). It is **not**
shipped in the `trellis-ai` PyPI package — the wheel/sdist whitelists in
`pyproject.toml` only list `src/`. To use it, work from a checkout of
this repo with the `dev` extra installed:

```bash
uv pip install -e ".[dev]"
```

If a scenario targets a particular backend (Postgres, Neo4j, pgvector,
LanceDB) install the matching extra too — see
[`recommended-config.yaml`](../../recommended-config.yaml) for the
blessed shapes.

## Quickstart

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

`eval/reports/` is gitignored (`.gitkeep` keeps the directory). Reports
are local artifacts; share them in PR descriptions or copy notable
numbers back into [`plan-evaluation-strategy.md`](./plan-evaluation-strategy.md)
§5 when they unblock a Phase 3 item.

## Adding a scenario

1. Create `eval/scenarios/<name>/scenario.py`.
2. Expose a single `run(registry: StoreRegistry) -> ScenarioReport`.
   `ScenarioReport` is `dataclass`-shaped; see
   [`eval/runner.py`](../../eval/runner.py).
3. Generators go in `eval/generators/`, fixtures in `eval/datasets/`.
   Generators must be deterministic given a seed; large datasets are
   fetched at runtime, not committed.
4. After the scenario lands and you've measured something, update §5 of
   the strategy doc with the actual numbers and which Phase 3 item
   became actionable.

The minimal template is [`_example/scenario.py`](../../eval/scenarios/_example/scenario.py).

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

The repo's CI runs the smoke test in
[`tests/unit/eval/test_runner_smoke.py`](../../tests/unit/eval/test_runner_smoke.py),
which exercises discovery + execution against the `_example` scenario
only. Heavyweight scenarios run on `workflow_dispatch` or a scheduled
workflow — never per-push. Plan §6 has the per-scenario wall-time
expectations.
