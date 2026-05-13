"""Observation entity schema.

An ``Observation`` is a qualitative or compound claim about an entity,
derived from a trace, log, or analysis. E.g., "this column is rarely
projected", "this service shows error spikes on Mondays".

See ``docs/design/adr-observation-entity-type.md`` for the design
rationale and ``docs/design/plan-observation-entity-type.md`` for the
implementation plan. This schema is the Phase 0 building block; Phase 1
adds the SDK + MCP surface, Phase 2 adds the retrieval strategy.

This module ships the **Pydantic model** for the canonical
``Observation`` node payload. The well-known canonical name
``"Observation"`` and the ``hasObservation`` / ``wasDerivedFrom`` edge
kinds are registered in :mod:`trellis.schemas.well_known`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field

from trellis.core.base import TrellisModel, utc_now
from trellis.core.ids import generate_ulid


class Observation(TrellisModel):
    """A qualitative/compound observation about a subject entity.

    Empirical claim derived from a trace, log row, or analysis run.
    Distinct from :class:`~trellis.schemas.measurement.Measurement`
    (scalar/time-series). One analysis can produce a single
    ``Observation`` ("column X exhibits a filter-projection asymmetry")
    plus several ``Measurement`` rows (``filter_count=950``,
    ``project_count=23``).

    Both ``Observation`` and ``Measurement`` are ``node_role="semantic"``
    by default. See
    ``docs/design/adr-observation-entity-type.md`` §2.1.

    Required-field discipline (per the POC directive in
    ``plan-self-improvement-program.md`` §2 and ADR §4.2): producers
    that omit a required field must raise at draft-time validation —
    no silent defaults.
    """

    observation_id: str = Field(default_factory=generate_ulid)
    """Stable identifier. ULID generated when not supplied."""

    subject_entity_id: str
    """The entity the observation is *about*."""

    subject_entity_type: str
    """Open-string entity type of the subject (per CLAUDE.md
    type-extensibility rule — well-known canonicals are recommended,
    not required)."""

    observer_agent_id: str
    """Which agent (human or automated) produced this observation."""

    content: str
    """The observation text / narrative description."""

    confidence: float = Field(ge=0.0, le=1.0)
    """How confident the producer is in the observation, in ``[0.0, 1.0]``."""

    observed_at: datetime = Field(default_factory=utc_now)
    """When the observation was made (defaults to now, UTC)."""

    evidence_ref: str | None = None
    """Optional pointer to supporting evidence (e.g., a ``trace_id``,
    ``document_id``, or other URN). Free-form by design; the consuming
    surface decides how to resolve."""

    metadata: dict[str, Any] = Field(default_factory=dict)
    """Open bag for the conventional keys documented in
    ``adr-observation-entity-type.md`` §2.3 (``kind``, ``value``,
    ``unit``, ``window_start``, ``window_end``, ``sample_size``,
    ``method``, freshness/tag data). The registry does not enforce
    schema here so domain producers can carry domain-specific signal
    without amendment. Defaults to ``{}`` for consistency with every
    other Trellis schema (``Evidence``, ``Entity``, ``Precedent``,
    ``Outcome``, ``Pack``) — saves consumers from ``None``-checks."""
