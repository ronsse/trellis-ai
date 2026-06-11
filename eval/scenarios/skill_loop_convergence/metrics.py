"""Per-axis metric computations for ``skill_loop_convergence``.

Skeleton only. Three pure-function reducers turn EventLog rows into
the per-period curves the scenario reports. Each helper consumes the
raw event payload list (typed as ``list[dict[str, Any]]`` to avoid
coupling to the EventLog row shape, which evolves) and returns a
:class:`TrellisModel`-backed result so the report payload stays
schema-validated.

F-phase wiring:

- :func:`coverage_curve` — F1 + F2 jointly. F1 defines what
  "enriched" means at the graph level; F2 emits ``NODE_ENRICHED``
  when the curator finishes a node.
- :func:`retrieval_lift_curve` — F3 (feedback + per-period pack
  quality scoring) and F6 (this scenario — the per-period query
  panel).
- :func:`variant_survival_rate` — F5 (score-based evolver). F5
  emits the variant-population trace; this reducer summarises it
  into a "fraction of variants alive at period N" curve.
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
    fraction of seed nodes that had at least one ``NODE_ENRICHED``
    event emitted by the end of period ``i``.
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
    """Per-period survival rate of the F5 evolver's prompt-variant pool.

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


def coverage_curve(events: list[dict[str, Any]]) -> CoverageCurve:
    """Reduce ``NODE_ENRICHED`` events into a per-period coverage curve.

    F1 + F2 fill this in (F1 defines the seed under-populated node
    set; F2 emits ``NODE_ENRICHED`` from the curator skill). Stub:
    raises :class:`NotImplementedError`.
    """
    msg = "F1 (graph-skill harness) + F2 (curator skill) fill this in"
    raise NotImplementedError(msg)


def retrieval_lift_curve(events: list[dict[str, Any]]) -> LiftCurve:
    """Reduce ``PACK_QUALITY_SCORED`` events into a retrieval-lift curve.

    F3 fills this in (feedback path — emits ``PACK_QUALITY_SCORED``
    per-period). The query panel itself is fixed by F6 so the lift
    is comparable across runs. Stub: raises
    :class:`NotImplementedError`.
    """
    msg = "F3 (feedback path) fills this in"
    raise NotImplementedError(msg)


def variant_survival_rate(events: list[dict[str, Any]]) -> VariantSurvival:
    """Reduce F5 evolver events into a per-period variant-survival curve.

    F5 fills this in (score-based evolver — emits the per-period
    variant-population trace that this reducer summarises). Stub:
    raises :class:`NotImplementedError`.
    """
    msg = "F5 (score-based evolver) fills this in"
    raise NotImplementedError(msg)
