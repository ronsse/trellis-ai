"""The plain values a served pack item's provenance line is rendered from.

:func:`provenance_inputs` reads only what is already on the
:class:`~trellis.schemas.pack.PackItem` at render time, and never a store:
the document form (:func:`~trellis.schemas.document_metadata.document_form_of`),
``source_system``, the session-capture marker, the graph ``node_role``, the
recency clock the collecting search stamped, and the collecting axis.
Feedback history is not consulted. The wording lives in
:func:`trellis_wire.provenance.format_provenance_line`.

**Age is the stamp recency scoring read, or nothing.** The search stamps
which clock won under ``metadata["recency_clock"]`` but not the stamp itself.
When that clock is ``source`` the winning stamp is in the metadata bag, so
:func:`~trellis.retrieve.strategies.resolve_recency_stamp` over the bag alone
returns the same stamp, and the age is taken from it. When it is ``row`` the
stamp was a store column that is not on the served item, and when it is
``none`` there was no usable stamp; in both cases the age is left out rather
than read from the store.

**An item the semantic axis served gets no age.** Its metadata is the vector
row's embed-time snapshot, and the embed hook writes ``created_at`` as embed
time when the document carried none
(:func:`~trellis.retrieve.embed_ingest_hook.build_vector_row`). The search
then labels that stamp ``source``, and nothing on the item tells it from a
copied source stamp, so the age is left out. The decision keys on the axis
that served the item, not on the stamp's value.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from trellis.retrieve.disclosure import POINTER_SELECTION_REASON
from trellis.retrieve.strategies import (
    RECENCY_CLOCK_METADATA_KEY,
    RECENCY_CLOCK_SOURCE,
    resolve_recency_stamp,
)
from trellis.schemas.document_metadata import document_form_of
from trellis.schemas.pack import PackItem

_SECONDS_PER_DAY = 86400.0

#: The axis whose items carry a vector row's metadata snapshot; equal to
#: :data:`trellis.retrieve.builder_factory.SEMANTIC_AXIS` (pinned by a test;
#: importing it here would pull the builder into the formatter's path).
VECTOR_SNAPSHOT_AXIS = "semantic"


def _parse(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
    else:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _source_age_days(metadata: dict[str, Any], now: datetime) -> float | None:
    """Age in days of the source-clock stamp recency scoring read, or ``None``."""
    if metadata.get(RECENCY_CLOCK_METADATA_KEY) != RECENCY_CLOCK_SOURCE:
        return None
    resolved = resolve_recency_stamp(metadata, now=now)
    if resolved.clock != RECENCY_CLOCK_SOURCE:
        return None
    stamp = _parse(resolved.stamp)
    if stamp is None:
        return None
    return (now - stamp).total_seconds() / _SECONDS_PER_DAY


def provenance_inputs(
    item: PackItem, *, now: datetime | None = None
) -> dict[str, Any] | None:
    """The values for ``item``'s provenance line, or ``None`` for a pointer.

    An item served as a disclosure pointer carries a label in place of its
    body, so it gets no line. The returned mapping has exactly the keyword
    arguments of :func:`trellis_wire.provenance.format_provenance_line`; any
    string in it may come from stored metadata and is sanitised there, not
    here.
    """
    if item.selection_reason == POINTER_SELECTION_REASON:
        return None
    metadata = item.metadata if isinstance(item.metadata, dict) else {}
    clock = metadata.get(RECENCY_CLOCK_METADATA_KEY)
    axis = item.strategy_source or metadata.get("source_strategy")
    age_days = (
        None
        if axis == VECTOR_SNAPSHOT_AXIS
        else _source_age_days(metadata, now or datetime.now(UTC))
    )
    return {
        "form": document_form_of(metadata),
        "source_system": metadata.get("source_system"),
        "captured": metadata.get("distilled") is True,
        "node_role": metadata.get("node_role"),
        "age_days": age_days,
        "clock": clock if isinstance(clock, str) else None,
        "axis": axis,
    }


__all__ = ["VECTOR_SNAPSHOT_AXIS", "provenance_inputs"]
