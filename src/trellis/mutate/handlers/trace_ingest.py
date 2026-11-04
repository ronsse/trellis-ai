"""The ``trace.ingest`` handler."""

from __future__ import annotations

from trellis.errors import StoreError, ValidationError
from trellis.mutate.commands import Command
from trellis.schemas.trace import Trace
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry


class TraceIngestHandler:
    """Validate a trace, store it, and emit TRACE_INGESTED.

    Wires :data:`Operation.TRACE_INGEST` into the governed mutation pipeline
    so trace ingestion follows the same audit / idempotency / policy contract
    as every other mutation. ``args["trace"]`` may be either a ``Trace``
    instance or a dict; dicts are validated through ``Trace.model_validate``
    so the executor's validate stage owns schema enforcement, not the store.

    Idempotency: if a trace with the given ``trace_id`` already exists, the
    handler returns the existing id without re-emitting an event. Combined
    with ``Command.idempotency_key`` (executor-level FIFO + EventLog-backed
    cross-restart check), repeated submissions are safe.

    A blank ``trace_id`` (empty or whitespace-only) is refused with
    ``ValidationError`` code ``trace_id_empty`` before the store is read.
    Stored under ``""``, it would make every later blank-id trace "already
    ingested". The check sits here, not on ``Trace``, so a stored trace with
    an empty id still loads.
    """

    def __init__(self, registry: StoreRegistry) -> None:
        self._registry = registry

    def handle(self, command: Command) -> tuple[str | None, str]:
        raw = command.args["trace"]
        trace = raw if isinstance(raw, Trace) else Trace.model_validate(raw)
        if not trace.trace_id.strip():
            msg = "trace_id must not be empty; omit it to have one generated"
            raise ValidationError(msg, errors=[msg], code="trace_id_empty")

        store = self._registry.operational.trace_store
        if store.get(trace.trace_id) is not None:
            return trace.trace_id, f"Trace already ingested: {trace.trace_id}"

        try:
            trace_id = store.append(trace)
        except StoreError:
            # Race: another writer landed the same trace between our get()
            # and append(). Treat as idempotent success rather than failure.
            if store.get(trace.trace_id) is not None:
                return trace.trace_id, f"Trace already ingested: {trace.trace_id}"
            raise

        self._registry.operational.event_log.emit(
            EventType.TRACE_INGESTED,
            source="mutation_executor",
            entity_id=trace_id,
            entity_type="trace",
            payload={
                "trace_id": trace_id,
                "source": trace.source.value,
                "intent": trace.intent,
                "domain": trace.context.domain if trace.context else None,
                "agent_id": trace.context.agent_id if trace.context else None,
            },
        )
        return trace_id, f"Trace ingested: {trace_id}"
