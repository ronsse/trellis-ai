# Plan: Parameter-registry wiring for learning thresholds

**Status:** Proposed 2026-05-11
**Owner:** swarm-pickable (smallest unit in the program)
**ADR:** none — sub-ADR scale; wakes existing dead code per [`adr-dual-loop-evolution.md`](./adr-dual-loop-evolution.md)
**Program:** [`plan-self-improvement-program.md`](./plan-self-improvement-program.md) item 3
**Depends on:** none
**Unblocks:** removes a category of dead code C1 lists; allows operators to tune learning thresholds per `(component, domain, intent_family)` cell instead of editing source.

## 1. Premise

`src/trellis/learning/scoring.py` contains two hard-coded thresholds:

```python
_NOISE_SUCCESS_THRESHOLD = 0.4
_NOISE_RETRY_THRESHOLD = 0.5
```

And the recommendation function `_recommend_learning_action()` looks them up via a `_lookup_threshold_from_registry(registry, key)` path that exists but **is never invoked from any caller** because `analyze_learning_observations()` doesn't accept a `registry` parameter. The registry plumbing is half-built.

This plan finishes the wiring and deletes the dead hard-coded constants. Per the POC directive: **no fallback to hard-coded defaults**. If no registry is passed, the function raises.

## 2. Scope

**In scope:**
- Add `registry: ParameterRegistry` as a required keyword-only parameter to `analyze_learning_observations()`.
- Thread the registry through to `_recommend_learning_action()`.
- Delete `_NOISE_SUCCESS_THRESHOLD` and `_NOISE_RETRY_THRESHOLD` module-level constants.
- Update all callers (CLI `analyze learning-observations` is the main one; tests inject mocks).
- Add registry-default seeding so a fresh registry has the historical defaults (0.4, 0.5) as the seed values for the `("learning", "*", "*")` cell — operator can override per-cell.

**Out of scope:**
- New parameter types beyond the existing two.
- ParameterRegistry refactoring.
- Tuner / rollback changes — they're independent consumers.

## 3. POC directives applied

- Calling `analyze_learning_observations()` without a registry **raises `TypeError`** (keyword required). No silent default-threshold path.
- A registry that lacks the required keys **raises `KeyError`** with a message naming the missing key and the seed defaults the operator should use. No fallback to module constants.
- The CLI `analyze learning-observations` constructs a registry from config; if config is absent, it constructs a registry seeded with the historical defaults and **logs at WARN level** that defaults are being used. The defaults live in the CLI module, not in `scoring.py`.

## 4. Files to touch

| File | Change |
|---|---|
| `src/trellis/learning/scoring.py` | Add `registry` kwarg to `analyze_learning_observations()`; thread to `_recommend_learning_action()`; delete `_NOISE_SUCCESS_THRESHOLD` / `_NOISE_RETRY_THRESHOLD`. |
| `src/trellis/ops/registry.py` (where `ParameterRegistry` actually lives — *not* `learning/parameters.py` as the plan originally guessed; corrected after Item 5 implementation) | Add `LEARNING_NOISE_SUCCESS_THRESHOLD` and `LEARNING_NOISE_RETRY_THRESHOLD` as registered parameter keys with type + range constraints. |
| `src/trellis_cli/analyze.py` | `analyze learning-candidates` command constructs registry from config; logs WARN if seeded with defaults. *(Plan originally named `learning-observations`; actual subcommand is `learning-candidates` — verified during Item 3 implementation.)* |
| `tests/unit/learning/test_scoring.py` | Update existing tests to pass mock registry; add test for missing-key KeyError; add test for missing-registry TypeError. |
| `tests/unit/cli/test_analyze.py` | Verify CLI emits WARN when defaults used; verify config-loaded registry path. |

## 5. Implementation steps

1. `ParameterRegistry` lives at `src/trellis/ops/registry.py` (confirmed during Item 5 implementation). Read its existing API (set/get/scope).
2. Read `analyze_learning_observations()` to confirm the existing `_lookup_threshold_from_registry()` helper and its callsite.
3. Modify signature: `analyze_learning_observations(observations: list[LearningObservation], *, registry: ParameterRegistry) -> list[LearningRecommendation]`.
4. Replace `_NOISE_SUCCESS_THRESHOLD` and `_NOISE_RETRY_THRESHOLD` usage with `registry.get(...)` calls scoped by the observation's `(component_id, domain, intent_family)`.
5. Delete the module-level constants.
6. Update the existing 5-6 test cases in `test_scoring.py` to construct a `ParameterRegistry` (in-memory) seeded with `{0.4, 0.5}` and pass it in.
7. Add 2 new tests: TypeError when registry omitted, KeyError when registry lacks the key.
8. CLI `analyze learning-observations`: construct registry from `~/.config/trellis/learning_params.yaml` if present; else construct in-memory with WARN-logged defaults; pass to `analyze_learning_observations()`.
9. Add CLI integration test verifying WARN path and config-loaded path.

## 6. Tests required

- `test_analyze_learning_observations_requires_registry` — TypeError on missing kwarg.
- `test_analyze_learning_observations_raises_on_missing_key` — KeyError if registry lacks `LEARNING_NOISE_SUCCESS_THRESHOLD`.
- `test_recommendation_uses_registry_threshold` — set registry to `{success: 0.7, retry: 0.8}`, verify recommendations change.
- `test_cli_warns_on_default_registry` — capture log output, assert WARN message.
- `test_cli_uses_config_registry_when_present` — write a fixture config, verify registry is built from it.

## 7. Done when

- All existing tests in `test_scoring.py` pass against the new signature.
- 5 new tests above pass.
- `grep -r "_NOISE_SUCCESS_THRESHOLD\|_NOISE_RETRY_THRESHOLD" src/` returns nothing.
- `trellis analyze learning-observations` exits cleanly against a fresh install (uses defaults + WARNs).
- mypy clean.

## 8. Estimated size

~80 LOC modified in `scoring.py` + ~40 LOC in `ops/registry.py` + ~60 LOC in CLI + ~150 LOC of tests = ~330 LOC total. Single PR. ~3 hours of swarm time.

## 9. Cleanup considerations

This is the smallest unit but it directly removes dead code (the orphaned helper `_lookup_threshold_from_registry()` had no callers). After landing, [`plan-cleanup-dead-code.md`](./plan-cleanup-dead-code.md) item C1.3 can be marked complete.

## 10. Risks

- **Existing operators with no config file** will see WARN spam on every CLI invocation until they create `~/.config/trellis/learning_params.yaml`. Mitigation: the WARN message is one line, includes the exact command to seed a config (`trellis admin init-learning-params`). Add that admin subcommand as part of this PR; ~30 LOC.
- **Other consumers of the hard-coded constants** — grep across the whole repo before deleting. If `tests/unit/scenarios/` or any worker imports the private constant, they need updating too.
