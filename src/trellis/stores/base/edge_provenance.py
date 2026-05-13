"""Shared helpers for the edge provenance columns.

These five columns (`source_trace_id`, `agent_id`, `confidence`,
`evidence_ref`, `extractor_tier`) live as first-class fields on the
``edges`` table for every graph backend per Phase 3 of
``adr-graph-ontology.md`` (item 2 of the self-improvement program).
The schema layer (:class:`trellis.schemas.graph.Edge`) carries the
canonical validation; this module exposes the same checks at the store
boundary so backends can validate before issuing a write, without
forcing every store call site through Pydantic.

The helpers are intentionally tiny — the per-backend write paths still
own the actual SQL/Cypher. This module's only job is:

1. Pull the five fields out of an ``upsert_edge``-style ``properties``
   kwarg or a per-row ``spec`` dict in a bulk call.
2. Validate ``confidence`` range and ``extractor_tier`` allowlist with
   the *same* error messages the schema raises.
3. Surface a single tuple of field names so backend row-to-dict helpers
   can iterate without typoing one of them.
"""

from __future__ import annotations

from typing import Any

from trellis.schemas.graph import ALLOWED_EXTRACTOR_TIERS

#: Canonical ordering for the five provenance columns. Used by every
#: backend's INSERT row builder and row-to-dict helper. Tuple (not list)
#: so it's immutable and hashable.
EDGE_PROVENANCE_FIELDS: tuple[str, ...] = (
    "source_trace_id",
    "agent_id",
    "confidence",
    "evidence_ref",
    "extractor_tier",
)


def validate_edge_provenance(
    *,
    source_trace_id: str | None = None,
    agent_id: str | None = None,
    confidence: float | None = None,
    evidence_ref: str | None = None,
    extractor_tier: str | None = None,
) -> None:
    """Raise ``ValueError`` if any provenance field violates its contract.

    Mirrors the validation in :class:`trellis.schemas.graph.Edge` so
    callers that drive the store directly (bypassing the Pydantic model)
    still get the same loud failure. The duplication is intentional —
    forcing every store boundary through Pydantic would bloat the hot
    path on backends that don't already round-trip through the schema.

    Args:
        source_trace_id: opaque str (no validation beyond type).
        agent_id: opaque str (no validation beyond type).
        confidence: float in [0.0, 1.0] when set.
        evidence_ref: opaque str (no validation beyond type).
        extractor_tier: one of :data:`ALLOWED_EXTRACTOR_TIERS` when set.

    Raises:
        ValueError: ``confidence`` outside [0, 1] or ``extractor_tier``
            not in the allowlist.
        TypeError: if any provided field has the wrong runtime type.
    """
    if source_trace_id is not None and not isinstance(source_trace_id, str):
        msg = (
            f"source_trace_id must be str or None, got {type(source_trace_id).__name__}"
        )
        raise TypeError(msg)
    if agent_id is not None and not isinstance(agent_id, str):
        msg = f"agent_id must be str or None, got {type(agent_id).__name__}"
        raise TypeError(msg)
    if evidence_ref is not None and not isinstance(evidence_ref, str):
        msg = f"evidence_ref must be str or None, got {type(evidence_ref).__name__}"
        raise TypeError(msg)
    if confidence is not None:
        # bool is a subclass of int in Python — reject explicitly so a
        # ``confidence=True`` typo doesn't silently land as 1.0.
        if isinstance(confidence, bool) or not isinstance(confidence, int | float):
            msg = (
                f"confidence must be a float in [0.0, 1.0] or None, "
                f"got {type(confidence).__name__}"
            )
            raise TypeError(msg)
        if not (0.0 <= float(confidence) <= 1.0):
            msg = f"confidence must be in [0.0, 1.0], got {confidence!r}"
            raise ValueError(msg)
    if extractor_tier is not None:
        if not isinstance(extractor_tier, str):
            msg = (
                f"extractor_tier must be str or None, "
                f"got {type(extractor_tier).__name__}"
            )
            raise TypeError(msg)
        if extractor_tier not in ALLOWED_EXTRACTOR_TIERS:
            msg = (
                f"extractor_tier must be one of "
                f"{sorted(ALLOWED_EXTRACTOR_TIERS)}, got {extractor_tier!r}"
            )
            raise ValueError(msg)


def extract_edge_provenance(
    spec: dict[str, Any] | None,
) -> dict[str, Any]:
    """Pull the five provenance fields out of an edge spec dict.

    Missing keys default to ``None``. Does **not** validate; call
    :func:`validate_edge_provenance` afterwards if the caller didn't
    already.

    Args:
        spec: A dict with optional ``source_trace_id`` / ``agent_id`` /
            ``confidence`` / ``evidence_ref`` / ``extractor_tier`` keys.
            ``None`` is accepted as shorthand for "no provenance".

    Returns:
        A dict with all five keys present (value ``None`` when the
        caller didn't supply them).
    """
    if spec is None:
        return dict.fromkeys(EDGE_PROVENANCE_FIELDS)
    return {field: spec.get(field) for field in EDGE_PROVENANCE_FIELDS}
