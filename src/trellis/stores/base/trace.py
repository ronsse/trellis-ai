"""TraceStore — abstract interface for immutable trace storage."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel

from trellis.errors import ValidationError
from trellis.schemas.trace import Trace

#: How many levels a trace's free-form payload may nest: ``steps[].args``,
#: ``steps[].result``, ``outcome.metrics`` and ``metadata``. A field counts its
#: own container as level 1, so ``{"a": {"b": 1}}`` is 2 deep. A container is
#: anything the JSON writer turns into an object or an array: a dict, a pydantic
#: model, or a list, tuple, set or frozenset.
#:
#: The bound exists because a store keeps a trace as JSON and reads it back with
#: a parser that gives up past 200 levels of the whole document (measured on
#: pydantic-core's ``model_validate_json``). The writer has no such limit up to
#: about 250 levels, so a trace nested between the two was accepted by ``append``
#: and then failed every ``query`` and ``get`` that reached its row. The payload
#: sits at most three levels down (trace, ``steps``, step), so 100 stays well
#: inside that limit, and no real trace comes near it: tool output and agent
#: state nest in single digits to low tens.
MAX_TRACE_PAYLOAD_DEPTH = 100

TRACE_TOO_DEEP_CODE = "trace_too_deep"


def as_utc(moment: datetime) -> datetime:
    """Return *moment* as an aware UTC datetime; a naive one is read as UTC.

    ``Trace.created_at`` accepts a naive datetime. Every ``TraceStore`` and its
    callers settle the question the same way, as UTC whatever the session or
    process time zone, so naive and aware values compare by instant instead of
    raising or sorting as text.
    """
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    try:
        return moment.astimezone(UTC)
    except OverflowError:
        # ``datetime.min`` at a positive offset, or ``datetime.max`` at a
        # negative one, is an instant ``datetime`` cannot name in UTC. Clamp to
        # the edge it fell off, so a stamp a writer could build still sorts, is
        # found by a bound, and loads from a cursor file, where it would
        # otherwise raise out of every one of them.
        if (moment.utcoffset() or timedelta(0)) >= timedelta(0):
            return datetime.min.replace(tzinfo=UTC)
        return datetime.max.replace(tzinfo=UTC)


def _nests_deeper_than(value: Any, limit: int) -> bool:
    """Whether *value* nests more than *limit* levels of container.

    A container is a dict, a pydantic model (its field values), or a list,
    tuple, set or frozenset. The store writes a trace with ``model_dump_json``,
    which turns a model into an object and a set into an array, so a model or a
    set placed in a free-form field is as deep on disk as the dict or list it
    becomes.

    Iterative, so a payload nested far past the interpreter's recursion limit
    is measured rather than crashing the measurement, and it stops at the first
    level over *limit*, so it also ends on a structure that contains itself.
    """
    stack: list[tuple[Any, int]] = [(value, 0)]
    while stack:
        node, depth = stack.pop()
        if isinstance(node, dict):
            children: Any = node.values()
        elif isinstance(node, BaseModel):
            # Iterating a model yields ``(name, value)`` for its fields and any
            # extras, which is what the JSON writer walks.
            children = [field_value for _name, field_value in node]
        elif isinstance(node, (list, tuple, set, frozenset)):
            children = node
        else:
            continue
        if depth + 1 > limit:
            return True
        stack.extend((child, depth + 1) for child in children)
    return False


def check_trace_nesting(trace: Trace) -> None:
    """Refuse a trace whose payload nests deeper than the store can read back.

    Every :class:`TraceStore` calls this first in ``append``, before it writes
    anything. It sits on the write path rather than on :class:`Trace` because a
    check on the schema also runs when a stored row is read, and a row written
    before the bound existed must still load.

    Raises :class:`~trellis.errors.ValidationError` with code
    ``trace_too_deep``, which the mutation executor reports as a rejection. The
    message names the field and the bound, never the value.
    """
    fields: list[tuple[str, Any]] = []
    for index, step in enumerate(trace.steps):
        fields.append((f"steps[{index}].args", step.args))
        fields.append((f"steps[{index}].result", step.result))
    if trace.outcome is not None:
        fields.append(("outcome.metrics", trace.outcome.metrics))
    fields.append(("metadata", trace.metadata))
    for path, value in fields:
        if _nests_deeper_than(value, MAX_TRACE_PAYLOAD_DEPTH):
            msg = (
                f"trace {path} nests more than {MAX_TRACE_PAYLOAD_DEPTH} levels "
                "deep; flatten it, or store the nested part as a JSON string"
            )
            raise ValidationError(msg, errors=[msg], code=TRACE_TOO_DEEP_CODE)


class TraceStore(ABC):
    """Abstract interface for immutable trace storage."""

    @abstractmethod
    def append(self, trace: Trace) -> str:
        """Store a trace (immutable).

        Returns trace_id. Raises if trace_id already exists. Every backend
        calls :func:`check_trace_nesting` before writing, so a trace it could
        not read back is refused instead of stored.
        """

    @abstractmethod
    def get(self, trace_id: str) -> Trace | None:
        """Get trace by ID."""

    @abstractmethod
    def query(
        self,
        *,
        source: str | None = None,
        domain: str | None = None,
        agent_id: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 50,
    ) -> list[Trace]:
        """Query traces with filters, newest first.

        ``since`` and ``until`` are inclusive and compare by instant: a trace
        stored with a ``-11:00`` or ``+14:00`` offset is found by the moment it
        happened, not by the wall-clock digits it was written with. A naive
        bound or a naive stored ``created_at`` is read as UTC (see
        :func:`as_utc`). The returned trace keeps the ``created_at`` it was
        appended with (an offset's seconds, if it has any, are dropped by the
        trace's JSON round trip, as they always were), so a caller that
        compares or sorts them must normalise with :func:`as_utc` first.
        """

    @abstractmethod
    def count(self, *, source: str | None = None, domain: str | None = None) -> int:
        """Count traces with optional filters."""

    @abstractmethod
    def count_by_domain(self) -> dict[str, int]:
        """Count traces grouped by ``domain``, keyed by the stored value.

        A trace with no domain is omitted, not keyed under ``None`` — a
        caller building a domain filter wants only values it could pass
        back into ``query(domain=...)``. Backs a server-side-count filter
        populated from the whole store rather than one fetched page.
        """

    @abstractmethod
    def close(self) -> None:
        """Cleanup."""
