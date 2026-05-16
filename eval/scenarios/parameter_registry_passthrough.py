"""Satellite scenario for Item 3 — registry passthrough regression test.

Per ``docs/design/plan-program-level-eval.md`` §4.2, this verifies the
recommendation engine's output actually changes when the
:class:`ParameterRegistry` is reconfigured. If the recommendation is
identical across two distinct registry seedings, the registry isn't
being read on the hot path — the regression fails loud.

Pattern mirrors :mod:`eval.scenarios.observation_retrieval` — single
file, deterministic SQLite, two run shapes (pytest functions + a
runner-compatible ``run()`` callable).

The hot path under test is
:func:`trellis.learning.scoring.analyze_learning_observations`, which
reads four required thresholds through
:func:`trellis.learning.scoring._resolve_required_threshold`:

* :data:`LEARNING_PROMOTE_SUCCESS_KEY`
* :data:`LEARNING_PROMOTE_RETRY_KEY`
* :data:`LEARNING_NOISE_SUCCESS_KEY`
* :data:`LEARNING_NOISE_RETRY_KEY`

We seed two distinct snapshots of these keys, run the analyzer with
the same observations, and assert the resulting recommendations differ.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

import pytest
import structlog

from eval.runner import Finding, ScenarioReport
from trellis.learning import analyze_learning_observations
from trellis.learning.scoring import (
    LEARNING_NOISE_RETRY_KEY,
    LEARNING_NOISE_SUCCESS_KEY,
    LEARNING_PROMOTE_RETRY_KEY,
    LEARNING_PROMOTE_SUCCESS_KEY,
    LEARNING_SCORING_COMPONENT,
)
from trellis.ops import ParameterRegistry
from trellis.schemas.parameters import ParameterScope, ParameterSet
from trellis.stores.sqlite.parameter import SQLiteParameterStore

logger = structlog.get_logger(__name__)


SCENARIO_NAME = "parameter_registry_passthrough"

# Two threshold sets chosen so the synthetic observation below crosses
# the recommendation boundary between them. Set A is permissive on
# promotion + strict on noise; the observation lands as ``promote_*``.
# Set B tightens promotion past the observation's success rate, so the
# same observation lands as ``None`` (filtered out, no recommendation).
# If the analyzer's output is identical under both sets, the registry
# is not being consulted — that's the regression we surface.
_THRESHOLDS_PERMISSIVE: dict[str, float] = {
    LEARNING_PROMOTE_SUCCESS_KEY: 0.6,
    LEARNING_PROMOTE_RETRY_KEY: 0.3,
    LEARNING_NOISE_SUCCESS_KEY: 0.2,
    LEARNING_NOISE_RETRY_KEY: 0.6,
}
_THRESHOLDS_STRICT: dict[str, float] = {
    LEARNING_PROMOTE_SUCCESS_KEY: 0.9,
    LEARNING_PROMOTE_RETRY_KEY: 0.1,
    LEARNING_NOISE_SUCCESS_KEY: 0.2,
    LEARNING_NOISE_RETRY_KEY: 0.6,
}

# Synthetic candidate that straddles the two threshold sets.
# success_rate = 4/5 = 0.8 (≥ permissive 0.6, < strict 0.9).
# retry_rate   = 1/5 = 0.2 (≤ permissive 0.3, > strict 0.1).
# No NOISE branch fires under either set (success_rate > 0.2, retry_rate < 0.6),
# so the only differentiator is the promote branch — that's the point.
_TARGET_ITEM_ID = "item:precedent:registry-passthrough-target"
_TARGET_INTENT_FAMILY = "source_analysis"
_OBSERVATIONS: list[dict[str, Any]] = [
    {
        "run_id": f"registry-passthrough-{i:02d}",
        "intent_family": _TARGET_INTENT_FAMILY,
        "phase": "analyze",
        "outcome": outcome,
        "had_retry": had_retry,
        "items": [
            {
                "item_id": _TARGET_ITEM_ID,
                "item_type": "precedent",
                "title": "registry passthrough target",
                "category": "retrieval_precedent",
            }
        ],
    }
    for i, (outcome, had_retry) in enumerate(
        [
            ("success", False),
            ("success", False),
            ("success", False),
            ("success", True),
            ("failure", False),
        ]
    )
]


def _seed_registry(
    param_store: SQLiteParameterStore,
    values: dict[str, float],
    *,
    source: str,
) -> ParameterRegistry:
    """Write ``values`` to ``param_store`` and return a fresh registry.

    A fresh registry per call avoids the in-memory cache silently
    serving the previous snapshot — the cache is the bug class this
    satellite is designed to catch.
    """
    param_store.put(
        ParameterSet(
            scope=ParameterScope(component_id=LEARNING_SCORING_COMPONENT),
            values=dict(values),
            source=source,
        )
    )
    return ParameterRegistry(param_store)


def _run_analyzer(registry: ParameterRegistry) -> list[dict[str, Any]]:
    """Drive the scoring path with the canned observations.

    Returns the candidate list (recommendations included) — callers
    compare these lists across registry seedings.
    """
    report = analyze_learning_observations(
        observations=_OBSERVATIONS,
        registry=registry,
        min_support=1,
    )
    candidates = report.get("candidates", [])
    if not isinstance(candidates, list):
        msg = (
            "analyze_learning_observations returned a non-list 'candidates' "
            f"field ({type(candidates).__name__}); registry passthrough "
            "cannot be checked"
        )
        raise TypeError(msg)
    return list(candidates)


def _summarise(candidates: list[dict[str, Any]]) -> dict[str, str]:
    """Project a candidate list down to ``{item_id: recommendation_type}``.

    The recommendation_type is the only thing the registry threshold
    swap can flip; the rest of the candidate payload (metrics,
    evidence_refs, etc.) is deterministic and unhelpful for the
    assertion.
    """
    return {
        str(c.get("item_id", "")): str(c.get("recommendation_type", ""))
        for c in candidates
    }


# ---------------------------------------------------------------------------
# pytest entry points
# ---------------------------------------------------------------------------


@pytest.fixture
def param_store_path(tmp_path: Path) -> Path:
    """A throw-away SQLite parameter-store path."""
    return tmp_path / "params.db"


def test_recommendation_changes_under_threshold_swap(
    param_store_path: Path,
) -> None:
    """Permissive vs strict registry must yield distinct recommendations.

    The observation lands as ``promote_precedent`` under the permissive
    set; under the strict set the promote gate stops gating, the noise
    gate doesn't fire, and the candidate is dropped from the output
    entirely. If both runs produce identical summaries, the analyzer
    is reading a hardcoded fallback rather than the registry — that's
    the regression.
    """
    permissive_store = SQLiteParameterStore(param_store_path)
    try:
        permissive_registry = _seed_registry(
            permissive_store, _THRESHOLDS_PERMISSIVE, source="eval:permissive"
        )
        permissive_summary = _summarise(_run_analyzer(permissive_registry))
    finally:
        permissive_store.close()

    strict_path = param_store_path.parent / "params_strict.db"
    strict_store = SQLiteParameterStore(strict_path)
    try:
        strict_registry = _seed_registry(
            strict_store, _THRESHOLDS_STRICT, source="eval:strict"
        )
        strict_summary = _summarise(_run_analyzer(strict_registry))
    finally:
        strict_store.close()

    assert permissive_summary != strict_summary, (
        "ParameterRegistry passthrough regression: identical recommendations "
        "under permissive vs strict thresholds — analyzer is not reading "
        f"the registry. permissive={permissive_summary} "
        f"strict={strict_summary}"
    )
    # Concrete shape assertion: permissive should promote the target,
    # strict should drop it. Future regressions that swap branches
    # without breaking the != assertion will still trip this.
    assert permissive_summary.get(_TARGET_ITEM_ID) == "promote_precedent"
    assert _TARGET_ITEM_ID not in strict_summary


def test_recommendation_is_stable_under_repeated_run(
    param_store_path: Path,
) -> None:
    """Two runs with the same registry must produce byte-identical output.

    Determinism guard — if the analyzer accidentally consults a clock
    or random source the regression suite's threshold assertions become
    flaky. We pin this here so the failure mode is obvious.
    """
    store = SQLiteParameterStore(param_store_path)
    try:
        registry = _seed_registry(store, _THRESHOLDS_PERMISSIVE, source="eval:repeated")
        first = _summarise(_run_analyzer(registry))
        second = _summarise(_run_analyzer(registry))
    finally:
        store.close()
    assert first == second, (
        f"analyzer output drifted between runs: first={first} second={second}"
    )


# ---------------------------------------------------------------------------
# Runner-compatible entry point
# ---------------------------------------------------------------------------


def run(*_args: Any, **_kwargs: Any) -> ScenarioReport:
    """Execute the passthrough check end-to-end and return a ScenarioReport.

    The runner-supplied registry is ignored — this satellite carries
    its own parameter store because the registry passthrough check
    requires write access to a :class:`SQLiteParameterStore`, which
    the runner's registry does not expose on its operational plane.
    """
    findings: list[Finding] = []
    metrics: dict[str, float] = {}
    status: str = "pass"

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        permissive_path = tmp_path / "permissive.db"
        strict_path = tmp_path / "strict.db"

        permissive_store = SQLiteParameterStore(permissive_path)
        try:
            permissive_registry = _seed_registry(
                permissive_store,
                _THRESHOLDS_PERMISSIVE,
                source="eval:permissive",
            )
            permissive_candidates = _run_analyzer(permissive_registry)
            permissive_summary = _summarise(permissive_candidates)
        finally:
            permissive_store.close()

        strict_store = SQLiteParameterStore(strict_path)
        try:
            strict_registry = _seed_registry(
                strict_store, _THRESHOLDS_STRICT, source="eval:strict"
            )
            strict_candidates = _run_analyzer(strict_registry)
            strict_summary = _summarise(strict_candidates)
        finally:
            strict_store.close()

    metrics["permissive.candidate_count"] = float(len(permissive_candidates))
    metrics["strict.candidate_count"] = float(len(strict_candidates))
    metrics["summaries_differ"] = 1.0 if permissive_summary != strict_summary else 0.0

    if permissive_summary == strict_summary:
        status = "regress"
        findings.append(
            Finding(
                severity="fail",
                message=(
                    "ParameterRegistry passthrough regression — identical "
                    "recommendations under permissive vs strict thresholds"
                ),
                detail={
                    "permissive_summary": permissive_summary,
                    "strict_summary": strict_summary,
                },
            )
        )
    else:
        findings.append(
            Finding(
                severity="info",
                message=(
                    f"registry passthrough live: permissive→"
                    f"{permissive_summary.get(_TARGET_ITEM_ID, 'absent')}, "
                    f"strict→{strict_summary.get(_TARGET_ITEM_ID, 'absent')}"
                ),
                detail={
                    "permissive_summary": permissive_summary,
                    "strict_summary": strict_summary,
                },
            )
        )

    return ScenarioReport(
        name=SCENARIO_NAME,
        status=status,  # type: ignore[arg-type]
        metrics=metrics,
        findings=findings,
        decision=(
            "Item 3 regression check: scoring path must consult the "
            "ParameterRegistry on every call. Failure here means a "
            "hardcoded default has crept back in — find the call site "
            "and route it through _resolve_required_threshold()."
        ),
    )


if __name__ == "__main__":  # pragma: no cover — operator convenience
    os.environ.setdefault("STRUCTLOG_DISABLE_CONFIG", "1")
    report = run()
    print(json.dumps(report.to_dict(), indent=2, default=str))
