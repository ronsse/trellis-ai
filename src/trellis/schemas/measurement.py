"""Measurement entity schema.

A ``Measurement`` is a scalar / numeric / boolean measurement attached
to an entity. E.g., ``filter_rate=0.95``, ``null_rate=0.03``,
``p99_latency_ms=412``.

See ``docs/design/adr-observation-entity-type.md`` for the design
rationale and ``docs/design/plan-observation-entity-type.md`` for the
implementation plan.

Distinct from :class:`~trellis.schemas.observation.Observation`:
``Observation`` is the **narrative, evidence-bearing** claim;
``Measurement`` is the **machine-comparable, time-series-shaped**
counterpart. The ADR (§5.6 of plan-self-improvement-program.md) marks
``Measurement`` nodes as *append-only by convention* — a new
measurement is a new node with new ``measured_at``, not a mutation of
an existing one — to keep the SCD-2 cost of high-frequency metric
streams bounded.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field

from trellis.core.base import TrellisModel, utc_now
from trellis.core.ids import generate_ulid


class Measurement(TrellisModel):
    """A scalar measurement attached to a subject entity.

    Use ``Measurement`` for machine-comparable numeric / boolean
    metrics — anything you would graph over time. Use
    :class:`~trellis.schemas.observation.Observation` for narrative
    claims that don't reduce to a single value.

    Required-field discipline (per the POC directive in
    ``plan-self-improvement-program.md`` §2): producers that omit a
    required field must raise at draft-time validation — no silent
    defaults.
    """

    measurement_id: str = Field(default_factory=generate_ulid)
    """Stable identifier. ULID generated when not supplied."""

    subject_entity_id: str
    """The entity the measurement is *about*."""

    subject_entity_type: str
    """Open-string entity type of the subject (per CLAUDE.md
    type-extensibility rule)."""

    metric_name: str
    """Semantic name of the metric (``"null_rate"``,
    ``"query_count"``, ``"p99_latency_ms"``). Open string — analytics
    group by this. Maps to the ``kind`` property convention in
    ``adr-observation-entity-type.md`` §2.3."""

    metric_value: float
    """The measured value. ``Measurement`` rows are scalar by
    contract; richer payloads (lists/dicts/strings) belong on
    :class:`Observation` instead."""

    unit: str | None = None
    """Optional unit for the scalar value (``"percent"``,
    ``"count"``, ``"ms"``, ``"per_query"``)."""

    measured_at: datetime = Field(default_factory=utc_now)
    """When the measurement was taken (defaults to now, UTC)."""

    observer_agent_id: str
    """Which agent (human or automated) recorded the measurement."""

    metadata: dict[str, Any] = Field(default_factory=dict)
    """Open bag for the conventional keys documented in
    ``adr-observation-entity-type.md`` §2.3 (``window_start``,
    ``window_end``, ``sample_size``, ``method``, ``confidence``,
    freshness/tag data). Defaults to ``{}`` for consistency with every
    other Trellis schema (``Evidence``, ``Entity``, ``Precedent``,
    ``Outcome``, ``Pack``) — saves consumers from ``None``-checks."""
