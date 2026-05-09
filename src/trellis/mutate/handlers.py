"""Command handlers for curate operations."""

from __future__ import annotations

from typing import Any

import structlog

from trellis.errors import StoreError, ValidationError
from trellis.mutate.commands import Command, Operation
from trellis.schemas.trace import Trace
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry

logger = structlog.get_logger(__name__)


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
    """

    def __init__(self, registry: StoreRegistry) -> None:
        self._registry = registry

    def handle(self, command: Command) -> tuple[str | None, str]:
        raw = command.args["trace"]
        trace = raw if isinstance(raw, Trace) else Trace.model_validate(raw)

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


class PrecedentPromoteHandler:
    """Emit PRECEDENT_PROMOTED event with title/description/domain from args."""

    def __init__(self, registry: StoreRegistry) -> None:
        self._registry = registry

    def handle(self, command: Command) -> tuple[str | None, str]:
        event = self._registry.operational.event_log.emit(
            EventType.PRECEDENT_PROMOTED,
            source="mutation_executor",
            entity_id=command.target_id,
            entity_type="trace",
            payload={
                "trace_id": command.args["trace_id"],
                "title": command.args["title"],
                "description": command.args["description"],
                "domain": command.args.get("domain"),
            },
        )
        return event.event_id, f"Precedent promoted: {command.args['title']}"


class LabelAddHandler:
    """Read node from graph store, add label to properties, upsert."""

    def __init__(self, registry: StoreRegistry) -> None:
        self._registry = registry

    def handle(self, command: Command) -> tuple[str | None, str]:
        target_id = command.args["target_id"]
        label = command.args["label"]
        store = self._registry.knowledge.graph_store

        node = store.get_node(target_id)
        if node is None:
            return None, f"Node not found: {target_id}"

        props = dict(node["properties"])
        labels = props.get("labels", [])
        if label not in labels:
            labels.append(label)
        props["labels"] = labels

        # Preserve node_role + generation_spec — both are immutable across
        # versions, so re-upsert must carry the existing values forward.
        store.upsert_node(
            node_id=target_id,
            node_type=node["node_type"],
            properties=props,
            node_role=node.get("node_role", "semantic"),
            generation_spec=node.get("generation_spec"),
        )

        self._registry.operational.event_log.emit(
            EventType.LABEL_ADDED,
            source="mutation_executor",
            entity_id=target_id,
            payload={"label": label},
        )
        return target_id, f"Label '{label}' added to {target_id}"


class LabelRemoveHandler:
    """Read node, remove label from properties, upsert."""

    def __init__(self, registry: StoreRegistry) -> None:
        self._registry = registry

    def handle(self, command: Command) -> tuple[str | None, str]:
        target_id = command.args["target_id"]
        label = command.args["label"]
        store = self._registry.knowledge.graph_store

        node = store.get_node(target_id)
        if node is None:
            return None, f"Node not found: {target_id}"

        props = dict(node["properties"])
        labels = props.get("labels", [])
        if label in labels:
            labels.remove(label)
        props["labels"] = labels

        # Preserve node_role + generation_spec (immutable across versions).
        store.upsert_node(
            node_id=target_id,
            node_type=node["node_type"],
            properties=props,
            node_role=node.get("node_role", "semantic"),
            generation_spec=node.get("generation_spec"),
        )

        self._registry.operational.event_log.emit(
            EventType.LABEL_REMOVED,
            source="mutation_executor",
            entity_id=target_id,
            payload={"label": label},
        )
        return target_id, f"Label '{label}' removed from {target_id}"


class FeedbackRecordHandler:
    """Emit FEEDBACK_RECORDED event."""

    def __init__(self, registry: StoreRegistry) -> None:
        self._registry = registry

    def handle(self, command: Command) -> tuple[str | None, str]:
        event = self._registry.operational.event_log.emit(
            EventType.FEEDBACK_RECORDED,
            source="mutation_executor",
            entity_id=command.target_id,
            payload={
                "target_id": command.args["target_id"],
                "rating": command.args["rating"],
                "comment": command.args.get("comment"),
            },
        )
        return event.event_id, f"Feedback recorded: rating={command.args['rating']}"


class EntityCreateHandler:
    """Create entity node via graph store, return node_id.

    Supports optional ``node_role`` and ``generation_spec`` command args to
    create structural or curated nodes. Defaults to a semantic node when
    omitted. The graph store rejects invalid combinations (e.g., curated
    without a generation_spec) via ``validate_node_role_args``.
    """

    def __init__(self, registry: StoreRegistry) -> None:
        self._registry = registry

    def handle(self, command: Command) -> tuple[str | None, str]:
        props: dict[str, Any] = dict(command.args.get("properties", {}))
        props["name"] = command.args["name"]

        # Use caller-supplied entity_id if provided, otherwise auto-generate ULID
        caller_id = command.args.get("entity_id")
        node_role = command.args.get("node_role", "semantic")
        generation_spec = command.args.get("generation_spec")
        node_id = self._registry.knowledge.graph_store.upsert_node(
            node_id=caller_id,
            node_type=command.args["entity_type"],
            properties=props,
            node_role=node_role,
            generation_spec=generation_spec,
        )

        self._registry.operational.event_log.emit(
            EventType.ENTITY_CREATED,
            source="mutation_executor",
            entity_id=node_id,
            entity_type=command.args["entity_type"],
            payload={
                "name": command.args["name"],
                "node_role": node_role,
            },
        )
        return node_id, f"Entity created: {command.args['name']}"


class LinkCreateHandler:
    """Validate both endpoints exist, then create edge via graph store.

    Pre-flight FK validation runs at the start of :meth:`handle` (before any
    side effect) so orphan edges can't be created in the first place. The
    legacy CLI ``graph-health`` command surfaces orphans post-hoc as a
    safety net; this handler closes the door at ingest time.

    The check resolves each endpoint via :meth:`_resolve_node` (direct
    ``get_node`` lookup, then property-based fallback on ``entity_id``).
    On miss, raises :class:`trellis.errors.ValidationError` with a message
    that names which side (source / target / both) failed and which IDs
    were attempted — the executor turns that into a ``MUTATION_REJECTED``
    event and a ``CommandStatus.FAILED`` result, so ``LINK_CREATED`` is
    never emitted for a dangling edge.

    Escape hatch: pass ``allow_dangling=True`` in ``command.args`` to skip
    FK validation. This is for bootstrap / edge-before-node ingest paths
    (e.g. extractors that emit edges in dependency order before their
    referenced nodes exist). Default is ``False`` — strict.
    """

    def __init__(self, registry: StoreRegistry) -> None:
        self._registry = registry

    def _resolve_node(self, node_id: str) -> str | None:
        """Resolve a node ID, falling back to property-based lookup.

        Tries:
          1. Direct ``get_node(node_id)`` (exact match on node_id column)
          2. Property lookup: ``properties->>'entity_id' = node_id``
        """
        store = self._registry.knowledge.graph_store
        if store.get_node(node_id) is not None:
            return node_id
        # Fallback: search by entity_id stored in properties
        results = store.query(properties={"entity_id": node_id}, limit=1)
        if results:
            node_id_val: str | None = results[0]["node_id"]
            return node_id_val
        return None

    def _resolve_endpoints(
        self, source_id: str, target_id: str
    ) -> tuple[str, str]:
        """Resolve both edge endpoints or raise :class:`ValidationError`.

        Centralises the FK-validation block so the happy-path of
        :meth:`handle` doesn't carry the per-side error wiring. Both
        endpoints are checked even on a single miss so callers see all
        root causes in one round trip.
        """
        resolved_source = self._resolve_node(source_id)
        resolved_target = self._resolve_node(target_id)
        missing: list[str] = []
        if resolved_source is None:
            missing.append(
                f"source_id={source_id!r} does not reference an existing entity"
            )
        if resolved_target is None:
            missing.append(
                f"target_id={target_id!r} does not reference an existing entity"
            )
        if missing:
            msg = f"LINK_CREATE FK check failed: {'; '.join(missing)}"
            # ``code`` becomes the ``reason`` field on the MUTATION_REJECTED
            # event the executor emits — see Variant A' in
            # docs/design/adr-extraction-validation.md.
            raise ValidationError(msg, errors=missing, code="orphan_edge")
        # Both checks passed → both resolved values are non-None.
        return resolved_source, resolved_target  # type: ignore[return-value]

    def handle(self, command: Command) -> tuple[str | None, str]:
        source_id = command.args["source_id"]
        target_id = command.args["target_id"]
        edge_kind = command.args["edge_kind"]
        allow_dangling = bool(command.args.get("allow_dangling", False))
        store = self._registry.knowledge.graph_store

        if not allow_dangling:
            source_id, target_id = self._resolve_endpoints(source_id, target_id)

        edge_id = store.upsert_edge(
            source_id=source_id,
            target_id=target_id,
            edge_type=edge_kind,
            properties=command.args.get("properties"),
        )

        self._registry.operational.event_log.emit(
            EventType.LINK_CREATED,
            source="mutation_executor",
            entity_id=edge_id,
            payload={
                "source_id": source_id,
                "target_id": target_id,
                "edge_kind": edge_kind,
            },
        )
        return edge_id, f"Link created: {source_id} --[{edge_kind}]--> {target_id}"


def create_curate_handlers(
    registry: StoreRegistry,
) -> dict[str, Any]:
    """Create all curate operation handlers for a given registry."""
    return {
        Operation.TRACE_INGEST: TraceIngestHandler(registry),
        Operation.PRECEDENT_PROMOTE: PrecedentPromoteHandler(registry),
        Operation.LABEL_ADD: LabelAddHandler(registry),
        Operation.LABEL_REMOVE: LabelRemoveHandler(registry),
        Operation.FEEDBACK_RECORD: FeedbackRecordHandler(registry),
        Operation.ENTITY_CREATE: EntityCreateHandler(registry),
        Operation.LINK_CREATE: LinkCreateHandler(registry),
        Operation.TRACE_INGEST: TraceIngestHandler(registry),
    }
