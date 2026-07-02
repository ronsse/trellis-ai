"""Per-axis metric computations for ``skill_loop_convergence``.

Three pure-function reducers turn captured event payloads into the
per-period curves the scenario reports. Each helper consumes a raw
payload list (typed as ``list[dict[str, Any]]`` to avoid coupling to
the EventLog row shape, which evolves) plus the run parameters the
curve is normalised against, and returns a
:class:`~trellis.core.base.TrellisModel`-backed result so the report
payload stays schema-validated.

Event sources in the reference-driver build (issue #249):

- :func:`coverage_curve` — enrichment records the scenario captures as
  the reference curator lands each governed ``ENTITY_CREATE`` update
  (payload: ``node_id`` / ``period`` / ``variant_id``). When the F2
  curator skill ships its ``node.enriched`` event type, the same
  reducer consumes those payloads unchanged.
- :func:`retrieval_lift_curve` — per-panel ``PACK_QUALITY_SCORED``
  payloads (real events emitted by ``PackBuilder``'s assembly-time
  evaluator hook), period-stamped by the scenario at capture time.
- :func:`variant_survival_rate` — per-period pool snapshots from the
  reference evolver (payload: ``period`` / ``alive`` / ``culled``).
  The F5 evolver's population trace slots into the same shape.
"""

from __future__ import annotations

from typing import Any

import structlog
from pydantic import Field

from trellis.core.base import TrellisModel

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Result schemas
# ---------------------------------------------------------------------------


class CoverageCurve(TrellisModel):
    """Per-period coverage of the seed under-populated node set.

    ``periods`` and ``coverage`` are zip-aligned: index ``i`` is the
    fraction of seed nodes that had received at least one enrichment
    by the end of period ``i``.
    """

    periods: list[int] = Field(default_factory=list)
    coverage: list[float] = Field(default_factory=list)
    seed_node_count: int = 0
    final_coverage: float = 0.0


class LiftCurve(TrellisModel):
    """Per-period retrieval-quality lift over the baseline corpus.

    Each entry is the mean ``evaluate_pack`` weighted score across a
    fixed query panel evaluated at the end of that period. ``baseline``
    is the score on a pre-loop run; ``lift`` is ``per_period[i] -
    baseline``.
    """

    periods: list[int] = Field(default_factory=list)
    per_period_score: list[float] = Field(default_factory=list)
    baseline: float = 0.0
    lift: list[float] = Field(default_factory=list)


class VariantSurvival(TrellisModel):
    """Per-period survival rate of the evolver's prompt-variant pool.

    ``per_period_alive`` is the count of variants still in the active
    pool at the end of each period. ``per_period_culled`` is the count
    removed in that period. ``survival_rate`` is alive / initial_pool
    so a flat 1.0 means no variants were culled.
    """

    periods: list[int] = Field(default_factory=list)
    per_period_alive: list[int] = Field(default_factory=list)
    per_period_culled: list[int] = Field(default_factory=list)
    survival_rate: list[float] = Field(default_factory=list)
    initial_pool_size: int = 0


# ---------------------------------------------------------------------------
# Reducers
# ---------------------------------------------------------------------------


def coverage_curve(
    events: list[dict[str, Any]],
    *,
    seed_node_ids: list[str],
    periods: int,
) -> CoverageCurve:
    """Reduce enrichment events into a per-period coverage curve.

    ``events`` carry ``node_id`` + ``period``. Coverage at period ``i``
    is the cumulative fraction of ``seed_node_ids`` enriched at least
    once by the end of that period. Events for unknown node ids are
    ignored (defensive — the loop should never produce them).
    """
    seed_set = set(seed_node_ids)
    enriched_by_period: dict[int, set[str]] = {}
    for event in events:
        node_id = event.get("node_id")
        period = event.get("period")
        if node_id not in seed_set or not isinstance(period, int):
            continue
        enriched_by_period.setdefault(period, set()).add(node_id)

    covered: set[str] = set()
    period_axis: list[int] = []
    coverage: list[float] = []
    total = len(seed_set)
    for period in range(periods):
        covered |= enriched_by_period.get(period, set())
        period_axis.append(period)
        coverage.append(len(covered) / total if total else 0.0)

    return CoverageCurve(
        periods=period_axis,
        coverage=coverage,
        seed_node_count=total,
        final_coverage=coverage[-1] if coverage else 0.0,
    )


def retrieval_lift_curve(
    events: list[dict[str, Any]],
    *,
    baseline: float,
    periods: int,
) -> LiftCurve:
    """Reduce period-stamped ``PACK_QUALITY_SCORED`` payloads into a lift curve.

    ``events`` carry ``period`` + ``weighted_score`` (the scenario stamps
    ``period`` onto each captured payload when it runs the panel). The
    per-period value is the mean weighted score across that period's
    panel; ``lift`` subtracts the pre-loop ``baseline``. Periods with no
    captured scores carry the previous period's value forward so the
    curve stays zip-aligned with the coverage axis.
    """
    by_period: dict[int, list[float]] = {}
    for event in events:
        period = event.get("period")
        score = event.get("weighted_score")
        if not isinstance(period, int) or not isinstance(score, (int, float)):
            continue
        by_period.setdefault(period, []).append(float(score))

    period_axis: list[int] = []
    per_period_score: list[float] = []
    previous = baseline
    for period in range(periods):
        scores = by_period.get(period)
        value = sum(scores) / len(scores) if scores else previous
        period_axis.append(period)
        per_period_score.append(value)
        previous = value

    return LiftCurve(
        periods=period_axis,
        per_period_score=per_period_score,
        baseline=baseline,
        lift=[score - baseline for score in per_period_score],
    )


def variant_survival_rate(
    events: list[dict[str, Any]],
    *,
    initial_pool_size: int,
) -> VariantSurvival:
    """Reduce per-period pool snapshots into a survival curve.

    ``events`` carry ``period`` / ``alive`` / ``culled`` — one snapshot
    per period, emitted by the evolver driver after any pruning for
    that period has run. Snapshots arrive in period order from the
    loop, but the reducer sorts defensively.
    """
    snapshots = sorted(
        (
            event
            for event in events
            if isinstance(event.get("period"), int)
            and isinstance(event.get("alive"), int)
        ),
        key=lambda event: event["period"],
    )
    period_axis = [event["period"] for event in snapshots]
    alive = [event["alive"] for event in snapshots]
    culled = [int(event.get("culled", 0)) for event in snapshots]
    survival = [
        count / initial_pool_size if initial_pool_size else 0.0 for count in alive
    ]
    return VariantSurvival(
        periods=period_axis,
        per_period_alive=alive,
        per_period_culled=culled,
        survival_rate=survival,
        initial_pool_size=initial_pool_size,
    )
