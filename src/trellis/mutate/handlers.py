"""Command handlers for curate operations."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import structlog

from trellis.errors import NotFoundError, StoreError, ValidationError
from trellis.extract.entity_resolution import bind_name_alias
from trellis.feedback.models import SUCCESS_RATING_THRESHOLD
from trellis.mutate.commands import Command, Operation
from trellis.mutate.retention import (
    ARCHIVED_STATE,
    MAX_DOCUMENTS_SCANNED,
    ResolutionReport,
    RetentionCandidate,
    RetentionCriteria,
    resolve_candidates,
)
from trellis.schemas.classification import LIFECYCLE_KEY, Lifecycle
from trellis.schemas.measurement import Measurement
from trellis.schemas.observation import Observation
from trellis.schemas.trace import Trace
from trellis.schemas.well_known import (
    HAS_MEASUREMENT,
    HAS_OBSERVATION,
    MEASUREMENT,
    OBSERVATION,
)
from trellis.stores.base.event_log import EventType
from trellis.stores.null.event_log import NullEventLog
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
    """Emit PRECEDENT_PROMOTED event with title/description/domain from args.

    Serves two sources that both feed ``get_lessons`` (which reads
    ``PRECEDENT_PROMOTED`` events via ``list_precedents``):

    * **Trace-mined** precedents pass ``trace_id`` and default to
      ``entity_type="trace"`` — the emitted payload is unchanged.
    * **Learning-scoring** promotions pass the promoted graph entity's id as
      ``target_id`` with ``entity_type="precedent"`` and no ``trace_id``;
      without this the entity landed but stayed invisible to ``get_lessons``.
    """

    def __init__(self, registry: StoreRegistry) -> None:
        self._registry = registry

    def handle(self, command: Command) -> tuple[str | None, str]:
        args = command.args
        payload: dict[str, Any] = {
            "title": args["title"],
            "description": args["description"],
            "domain": args.get("domain"),
        }
        # Preserve the trace-path payload byte-for-byte; only add provenance
        # keys for the entity-sourced (learning) path so nothing downstream
        # that reads ``trace_id`` on a trace precedent is surprised.
        trace_id = args.get("trace_id")
        if trace_id is not None:
            payload["trace_id"] = trace_id
        source_item_id = args.get("source_item_id")
        if source_item_id is not None:
            payload["source_item_id"] = source_item_id

        event = self._registry.operational.event_log.emit(
            EventType.PRECEDENT_PROMOTED,
            source="mutation_executor",
            entity_id=command.target_id or trace_id,
            entity_type=args.get("entity_type", "trace"),
            payload=payload,
        )
        return event.event_id, f"Precedent promoted: {args['title']}"


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
    """Emit FEEDBACK_RECORDED event.

    ``pack_id`` is forwarded when the caller supplied one. It is the join
    key: :func:`trellis.learning.pack_observations.join_pack_feedback`
    matches feedback to packs strictly on ``payload["pack_id"]`` and skips
    events that lack it. ``POST /feedback`` has accepted a ``pack_id``
    field since the wire DTO was written ("Link feedback to a context
    pack") and the route has always put it in ``command.args`` — but this
    handler dropped it before emitting, so the link the caller asked for
    was never made and the event could not join. Nothing infers a pack
    here: an absent ``pack_id`` stays absent.

    ``success`` rides along with it, and has to. Every consumer reads
    ``payload["success"]`` and only then falls back to ``rating``;
    ``_join_one`` in particular resolves an absent key to ``"failure"``.
    Forwarding the join key alone would therefore have made a governed
    ``rating=0.9`` join as a *failed* delivery — a wrong signal reaching
    the loop, which is worse than the unjoinable silence it replaced. The
    derivation is :data:`~trellis.feedback.models.SUCCESS_RATING_THRESHOLD`,
    the same one :meth:`PackFeedback.from_agent_signal` applies, so the
    two feedback families cannot disagree about what a given rating means.

    This path still carries no per-item attribution — the governed
    command schema has no place for it, and the surfaces that do (the MCP
    ``record_feedback`` tool, ``POST /packs/{pack_id}/feedback``) route
    through ``PackFeedback`` instead. A joined observation from here has
    an outcome and no cited items, which is exactly what was sent.
    """

    def __init__(self, registry: StoreRegistry) -> None:
        self._registry = registry

    def handle(self, command: Command) -> tuple[str | None, str]:
        rating = command.args["rating"]
        payload: dict[str, Any] = {
            "target_id": command.args["target_id"],
            "rating": rating,
            "comment": command.args.get("comment"),
        }
        if isinstance(rating, int | float):
            payload["success"] = float(rating) >= SUCCESS_RATING_THRESHOLD
        pack_id = command.args.get("pack_id")
        if isinstance(pack_id, str) and pack_id.strip():
            payload["pack_id"] = pack_id.strip()
        event = self._registry.operational.event_log.emit(
            EventType.FEEDBACK_RECORDED,
            source="mutation_executor",
            entity_id=command.target_id,
            payload=payload,
        )
        return event.event_id, f"Feedback recorded: rating={command.args['rating']}"


class EntityCreateHandler:
    """Create entity node via graph store, return node_id.

    Supports optional ``node_role`` and ``generation_spec`` command args to
    create structural or curated nodes. Defaults to a semantic node when
    omitted. The graph store rejects invalid combinations (e.g., curated
    without a generation_spec) via ``validate_node_role_args``.

    ``document_ids`` follows the same omission semantics as
    :class:`EntityUpdateHandler`: an omitted field carries the stored link
    forward, an explicit value replaces it.
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
        # ``document_ids`` is the first-class graph↔document link (Phase 4 of
        # ADR planes-and-substrates). Threading it here gives the pointer a
        # governed write path — the pointer-not-prose invariant depends on a
        # created entity being able to carry an ``evidence_ref`` document
        # pointer without smuggling it through metadata.
        #
        # ENTITY_CREATE on an existing id is an upsert (it opens a new SCD-2
        # version), so an *omitted* ``document_ids`` has to carry the stored
        # link forward the way EntityUpdateHandler does. Passing ``None``
        # straight through would write NULL and silently destroy a link some
        # other writer established — e.g. re-extracting an entity that
        # ``save_knowledge`` had linked to its document. Omission means
        # "leave it alone"; an explicit value still replaces.
        document_ids = self._resolve_document_ids(command, caller_id)
        node_id = self._registry.knowledge.graph_store.upsert_node(
            node_id=caller_id,
            node_type=command.args["entity_type"],
            properties=props,
            node_role=node_role,
            generation_spec=generation_spec,
            document_ids=document_ids,
        )
        bind_name_alias(
            self._registry.knowledge.graph_store,
            entity_id=node_id,
            name=command.args["name"],
        )

        self._registry.operational.event_log.emit(
            EventType.ENTITY_CREATED,
            source="mutation_executor",
            entity_id=node_id,
            entity_type=command.args["entity_type"],
            payload={
                "name": command.args["name"],
                "node_role": node_role,
                "document_ids": document_ids or [],
            },
        )
        return node_id, f"Entity created: {command.args['name']}"

    def _resolve_document_ids(
        self, command: Command, caller_id: str | None
    ) -> list[str] | None:
        """Command's ``document_ids``, else the stored link, else ``None``.

        Only reads the store when the caller both named an id and omitted
        the field — the common create-a-fresh-node path stays a single
        write.
        """
        if "document_ids" in command.args:
            supplied = command.args["document_ids"]
            return cast("list[str] | None", supplied)
        if caller_id is None:
            return None
        existing = self._registry.knowledge.graph_store.get_node(caller_id)
        if existing is None:
            return None
        # ``get_node`` returns a (possibly empty) list; normalise empty to
        # ``None`` so ``validate_document_ids`` sees a clean value.
        return cast("list[str] | None", existing.get("document_ids") or None)


class EntityUpdateHandler:
    """Update an existing entity node, emitting ``ENTITY_UPDATED``.

    Wires :data:`Operation.ENTITY_UPDATE` into the governed pipeline. The
    enum verb shipped with no handler, so the executor rejected every
    ``entity.update`` command with "No handler registered for:
    entity.update" — this closes that gap.

    **SCD-2 discipline.** The update is never an in-place edit. Reading the
    current version and re-``upsert_node``-ing closes the old version
    (``valid_to`` is set) and inserts a fresh version row;
    ``get_node_history`` is the audit trail. ``node_role`` and
    ``generation_spec`` are immutable across versions (the graph store
    enforces role immutability via ``check_node_role_immutable``), so they
    are carried forward from the existing version rather than reset to the
    ``upsert_node`` defaults — the same discipline
    :class:`LabelAddHandler` uses.

    **Partial update.** ``properties`` is *merged* into the existing bag
    (not replaced) so a caller can attach a single field — e.g. an
    ``evidence_ref`` pointer — without resending the whole node. Likewise
    ``document_ids`` is carried forward when the caller omits it: absent
    means "leave the graph↔document link untouched", present means "replace
    it". This keeps a version bump from silently dropping the
    pointer-not-prose link.

    Idempotency: a same-payload re-submission still creates a new version
    row (SCD-2 has no same-value short-circuit at this layer); use
    ``Command.idempotency_key`` when at-most-once semantics are required.
    """

    def __init__(self, registry: StoreRegistry) -> None:
        self._registry = registry

    def handle(self, command: Command) -> tuple[str | None, str]:
        entity_id = command.args["entity_id"]
        store = self._registry.knowledge.graph_store

        existing = store.get_node(entity_id)
        if existing is None:
            # Surface as a typed NotFoundError → the executor maps it to a
            # FAILED CommandResult (StoreError branch) rather than a silent
            # success. Updating a nonexistent entity is a caller error.
            raise NotFoundError(entity_type="entity", entity_id=entity_id)

        # Partial update: merge caller-supplied properties onto the existing
        # bag, then let an explicit ``name`` win.
        props: dict[str, Any] = dict(existing["properties"])
        props.update(command.args.get("properties") or {})
        if "name" in command.args:
            props["name"] = command.args["name"]

        # ``node_type`` is mutable across versions but defaults to
        # carry-forward when the caller omits ``entity_type``.
        node_type = command.args.get("entity_type") or existing["node_type"]

        # Carry the existing document link forward unless the caller supplies
        # a replacement. ``get_node`` always returns ``document_ids`` as a
        # (possibly empty) list; normalise an empty list to ``None`` ("no
        # link") so ``validate_document_ids`` sees a clean value.
        if "document_ids" in command.args:
            document_ids = command.args["document_ids"]
        else:
            document_ids = existing.get("document_ids") or None

        node_role = existing.get("node_role", "semantic")
        node_id = store.upsert_node(
            node_id=entity_id,
            node_type=node_type,
            properties=props,
            node_role=node_role,
            generation_spec=existing.get("generation_spec"),
            document_ids=document_ids,
        )
        if "name" in command.args and command.args["name"] != (
            existing.get("properties") or {}
        ).get("name"):
            bind_name_alias(
                store,
                entity_id=node_id,
                name=command.args["name"],
            )

        self._registry.operational.event_log.emit(
            EventType.ENTITY_UPDATED,
            source="mutation_executor",
            entity_id=node_id,
            entity_type=node_type,
            payload={
                "name": props.get("name"),
                "node_role": node_role,
                "document_ids": document_ids or [],
            },
        )
        return node_id, f"Entity updated: {node_id}"


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

    def _resolve_endpoints(self, source_id: str, target_id: str) -> tuple[str, str]:
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


class ObservationRecordHandler:
    """Persist an Observation as a graph node + ``hasObservation`` edge.

    Wires :data:`Operation.OBSERVATION_RECORD` into the governed mutation
    pipeline so empirical-observation ingestion follows the same audit /
    idempotency / policy contract as every other mutation. ``args``
    expects an ``observation`` key holding either an :class:`Observation`
    instance or a dict; dicts are routed through
    ``Observation.model_validate`` so any missing required field raises
    a :class:`pydantic.ValidationError` *inside* the handler — surfaced
    as ``CommandStatus.FAILED`` to the caller, never silently defaulted.

    Idempotency semantics: re-recording with the same id REPLACES the
    existing observation and re-emits the ``OBSERVATION_RECORDED`` event.
    This diverges from :class:`TraceIngestHandler`'s
    short-circuit-on-repeat behavior because Observations are mutable
    signals over time — an agent may revise its confidence as new
    evidence arrives. Use a new ``observation_id`` to record a distinct
    signal. See ``docs/design/adr-observation-entity-type.md`` §2.1;
    ``hasObservation`` is the canonical edge from the subject entity to
    the observation node.
    """

    def __init__(self, registry: StoreRegistry) -> None:
        self._registry = registry

    def handle(self, command: Command) -> tuple[str | None, str]:
        raw = command.args["observation"]
        try:
            obs = (
                raw if isinstance(raw, Observation) else Observation.model_validate(raw)
            )
        except Exception as exc:
            # Loud-on-missing-required-field discipline. The executor
            # turns ValidationError into a structured rejection event.
            msg = f"Observation validation failed: {exc}"
            raise ValidationError(msg, code="observation_validation") from exc

        store = self._registry.knowledge.graph_store

        # Idempotent upsert — repeat submissions of the same observation_id
        # collapse onto the existing node. The graph store's SCD-2 layer
        # treats a same-payload upsert as a no-op version close + reopen.
        props: dict[str, Any] = {
            "observation_id": obs.observation_id,
            "subject_entity_id": obs.subject_entity_id,
            "subject_entity_type": obs.subject_entity_type,
            "observer_agent_id": obs.observer_agent_id,
            "content": obs.content,
            "confidence": obs.confidence,
            "observed_at": obs.observed_at.isoformat(),
        }
        if obs.evidence_ref is not None:
            props["evidence_ref"] = obs.evidence_ref
        if obs.metadata is not None:
            props["metadata"] = obs.metadata

        node_id = store.upsert_node(
            node_id=obs.observation_id,
            node_type=OBSERVATION,
            properties=props,
        )

        # Best-effort hasObservation edge from the subject entity to the
        # new observation node. The subject may not be a graph node in
        # all deployments (e.g., raw trace IDs) — when the FK fails we
        # still keep the observation row, but the edge is skipped. This
        # matches the open-string entity-type rule (CLAUDE.md
        # type-extensibility).
        #
        # Narrow the catch to ``StoreError`` (which ``NotFoundError``
        # subclasses): unexpected exceptions (DB driver bugs, schema
        # errors) are escalated rather than silently swallowed — see
        # PR #122 (C2 Phase 5) for the silent-fallback discipline.
        try:
            store.upsert_edge(
                source_id=obs.subject_entity_id,
                target_id=node_id,
                edge_type=HAS_OBSERVATION,
            )
        except (NotFoundError, StoreError) as exc:
            logger.info(
                "observation_edge_skipped",
                subject_entity_id=obs.subject_entity_id,
                observation_id=node_id,
                reason=str(exc),
            )

        self._registry.operational.event_log.emit(
            EventType.OBSERVATION_RECORDED,
            source="mutation_executor",
            entity_id=node_id,
            entity_type=OBSERVATION,
            payload={
                "observation_id": node_id,
                "subject_entity_id": obs.subject_entity_id,
                "subject_entity_type": obs.subject_entity_type,
                "observer_agent_id": obs.observer_agent_id,
                "confidence": obs.confidence,
            },
        )
        return node_id, f"Observation recorded: {node_id}"


class MeasurementRecordHandler:
    """Persist a Measurement as an append-only graph node.

    Sibling of :class:`ObservationRecordHandler` keyed on
    ``measurement_id``. See ``docs/design/adr-observation-entity-type.md``
    §2.1 — Measurement rows are *append-only by convention* so the SCD-2
    cost of high-frequency metric streams stays bounded.

    Idempotency semantics: re-recording with the same id REPLACES the
    existing measurement and re-emits the ``MEASUREMENT_RECORDED`` event.
    This diverges from :class:`TraceIngestHandler`'s
    short-circuit-on-repeat behavior because Measurements are mutable
    signals over time — a metric may be re-measured with a corrected
    value. Use a new ``measurement_id`` to record a distinct signal.
    """

    def __init__(self, registry: StoreRegistry) -> None:
        self._registry = registry

    def handle(self, command: Command) -> tuple[str | None, str]:
        raw = command.args["measurement"]
        try:
            meas = (
                raw if isinstance(raw, Measurement) else Measurement.model_validate(raw)
            )
        except Exception as exc:
            msg = f"Measurement validation failed: {exc}"
            raise ValidationError(msg, code="measurement_validation") from exc

        store = self._registry.knowledge.graph_store

        props: dict[str, Any] = {
            "measurement_id": meas.measurement_id,
            "subject_entity_id": meas.subject_entity_id,
            "subject_entity_type": meas.subject_entity_type,
            "metric_name": meas.metric_name,
            "metric_value": meas.metric_value,
            "observer_agent_id": meas.observer_agent_id,
            "measured_at": meas.measured_at.isoformat(),
        }
        if meas.unit is not None:
            props["unit"] = meas.unit
        if meas.metadata is not None:
            props["metadata"] = meas.metadata

        node_id = store.upsert_node(
            node_id=meas.measurement_id,
            node_type=MEASUREMENT,
            properties=props,
        )

        # Same narrow-catch discipline as ObservationRecordHandler —
        # silent-fallback hygiene per PR #122 (C2 Phase 5). Measurement
        # gets its own ``hasMeasurement`` edge kind so consumers can
        # route on edge kind alone without inspecting the target node's
        # type (adr-observation-entity-type.md §2.2).
        try:
            store.upsert_edge(
                source_id=meas.subject_entity_id,
                target_id=node_id,
                edge_type=HAS_MEASUREMENT,
            )
        except (NotFoundError, StoreError) as exc:
            logger.info(
                "measurement_edge_skipped",
                subject_entity_id=meas.subject_entity_id,
                measurement_id=node_id,
                reason=str(exc),
            )

        self._registry.operational.event_log.emit(
            EventType.MEASUREMENT_RECORDED,
            source="mutation_executor",
            entity_id=node_id,
            entity_type=MEASUREMENT,
            payload={
                "measurement_id": node_id,
                "subject_entity_id": meas.subject_entity_id,
                "subject_entity_type": meas.subject_entity_type,
                "metric_name": meas.metric_name,
                "metric_value": meas.metric_value,
                "observer_agent_id": meas.observer_agent_id,
            },
        )
        return node_id, f"Measurement recorded: {node_id}"


#: Upper bound on ``redaction.apply``'s ``reason`` arg. The reason is
#: written verbatim into the append-only audit log — the one payload
#: field that *could* re-contain the redacted content — so it is kept
#: short and rejected loudly rather than silently truncated.
MAX_REDACTION_REASON_CHARS = 2000

#: Cap on the linked observation / measurement id lists in the
#: ``REDACTION_APPLIED`` payload (they are follow-up pointers, not an
#: exhaustive index).
_LINKED_SIGNAL_LIMIT = 100


class RedactionApplyHandler:
    """Hard-purge a graph entity, emitting ``REDACTION_APPLIED``.

    Wires :data:`Operation.REDACTION_APPLY` into the governed pipeline. The
    enum verb shipped with no handler (the same gap ``entity.update`` had
    before :class:`EntityUpdateHandler`), so the executor rejected every
    ``redaction.apply`` command with "No handler registered" — defect
    cleanups had to fall back to *neutralizing* entities via
    ``entity.update`` because no governed deletion path existed.

    **Redaction is a purge, not an SCD-2 close.** ``delete_node`` physically
    removes *all* version rows, cascades to every edge version touching the
    node, and drops its alias rows (pinned by the graph-store contract
    tests). After redaction the entity is unreachable through ``get_node``,
    ``as_of`` time-travel, and ``get_node_history`` — a redaction that
    time-travel can resurrect would not be a redaction. The vector entry is
    deleted *before* the graph purge (``item_id == node_id`` is the
    shape-#2 contract on the bolt backends; the standalone stores key
    vectors by document id, so the delete is a recorded no-op there): a
    vector-backend failure therefore aborts before anything irreversible
    happens. The reverse failure — vector gone, graph purge raises — leaves
    the entity intact minus its re-derivable embedding, never the reverse.

    **The EventLog is the audit trail, and it never re-contains content.**
    The ``REDACTION_APPLIED`` payload carries the ``reason``, counts, and id
    pointers only — no name, no properties (see
    :attr:`~trellis.stores.base.event_log.EventType.REDACTION_APPLIED` for
    the schema). Because that event is the only record that survives the
    purge, the handler refuses to run against a ``NullEventLog``
    (``code="redaction_requires_event_log"``), and the emit itself is
    guarded: if it fails after the purge, the content-free payload is
    preserved in operator logs rather than reporting FAILED for a redaction
    that already happened. Scope is the Knowledge Plane: prior
    Operational-Plane events (e.g. the original ``ENTITY_CREATED`` payload)
    and traces are immutable by design and are not rewritten.

    **Scope: graph entities only; linked records become pointers.**
    Documents linked from any purged version are *not* cascaded — a
    document may back many entities — and their union across versions rides
    the payload so a future document-level redaction can locate them.
    Observations and Measurements *about* the subject are independent
    governed nodes and are likewise not cascaded, but they carry
    ``subject_entity_id`` as a property, so property-based queries keep
    serving them after the purge; their ids ride the payload so the
    operator can redact each one individually (they are graph nodes — this
    same verb applies). A ``target_id`` that is not a graph node raises
    :class:`~trellis.errors.NotFoundError` (→ ``CommandStatus.FAILED``).

    Idempotency: re-redacting a purged id fails with ``NotFoundError``, and
    a concurrent-purge race is detected via ``delete_node``'s return value
    so the loser never emits a second audit event; use
    ``Command.idempotency_key`` when at-most-once semantics are required. A
    blank ``reason`` is rejected (``code="redaction_reason_required"``) and
    an over-long one too (``code="redaction_reason_too_long"``) — the
    recorded justification is the point of governed redaction, and it must
    stay short and content-free.
    """

    def __init__(self, registry: StoreRegistry) -> None:
        self._registry = registry

    def handle(self, command: Command) -> tuple[str | None, str]:
        target_id = command.args["target_id"]
        reason = str(command.args["reason"] or "").strip()
        if not reason:
            msg = "redaction.apply requires a non-empty reason for the audit trail"
            raise ValidationError(msg, code="redaction_reason_required")
        if len(reason) > MAX_REDACTION_REASON_CHARS:
            msg = (
                f"redaction.apply reason exceeds {MAX_REDACTION_REASON_CHARS} "
                "chars; it is written verbatim to the append-only audit log — "
                "keep it short and content-free"
            )
            raise ValidationError(msg, code="redaction_reason_too_long")

        event_log = self._registry.operational.event_log
        if isinstance(event_log, NullEventLog):
            msg = (
                "redaction.apply requires a persisting event log: the "
                "REDACTION_APPLIED event is the only record that survives "
                "the purge, and this deployment's event_log backend is 'null'"
            )
            raise ValidationError(msg, code="redaction_requires_event_log")

        graph = self._registry.knowledge.graph_store
        node = graph.get_node(target_id)
        if node is None:
            raise NotFoundError(entity_type="entity", entity_id=target_id)

        entity_type = node["node_type"]
        history = graph.get_node_history(target_id)
        node_versions = len(history)
        # Union across ALL versions: the purge takes every version's rows
        # with it, and the payload is the only place the graph→document
        # pointers survive — the current version alone under-reports when
        # a later version replaced the link.
        document_ids = sorted(
            {d for version in history for d in version.get("document_ids") or []}
        )
        edges = len(graph.get_edges(target_id, direction="both"))
        aliases = len(graph.get_aliases(target_id))
        linked_observation_ids = [
            row["node_id"]
            for row in graph.query(
                node_type=OBSERVATION,
                properties={"subject_entity_id": target_id},
                limit=_LINKED_SIGNAL_LIMIT,
            )
        ]
        linked_measurement_ids = [
            row["node_id"]
            for row in graph.query(
                node_type=MEASUREMENT,
                properties={"subject_entity_id": target_id},
                limit=_LINKED_SIGNAL_LIMIT,
            )
        ]

        # Vector entry first: if the vector backend raises, the command
        # fails with the graph untouched — nothing irreversible has
        # happened yet.
        vector_deleted = self._registry.knowledge.vector_store.delete(target_id)

        if not graph.delete_node(target_id):
            # Lost a race: another writer purged the node between our read
            # and the delete. Fail rather than emit a second
            # REDACTION_APPLIED carrying counts this command did not purge.
            raise NotFoundError(entity_type="entity", entity_id=target_id)

        payload: dict[str, Any] = {
            "target_id": target_id,
            "target_kind": "entity",
            "reason": reason,
            "command_id": command.command_id,
            "requested_by": command.requested_by,
            "node_versions_purged": node_versions,
            "edges_purged": edges,
            "aliases_purged": aliases,
            "vector_deleted": vector_deleted,
            "document_ids": document_ids,
            "linked_observation_ids": linked_observation_ids,
            "linked_measurement_ids": linked_measurement_ids,
        }
        message = (
            f"Entity redacted: {target_id} "
            f"({node_versions} version(s), {edges} edge(s), "
            f"{aliases} alias(es), vector_deleted={vector_deleted})"
        )
        try:
            event_log.emit(
                EventType.REDACTION_APPLIED,
                source="mutation_executor",
                entity_id=target_id,
                entity_type=entity_type,
                payload=payload,
            )
        except Exception:
            # Same discipline as ops/write_health.py: the purge already
            # happened, so raising would report FAILED for a completed
            # redaction and (via the executor's own emit against the same
            # log) lose the record entirely. Preserve the content-free
            # payload in operator logs instead.
            logger.warning(
                "redaction_audit_emit_failed",
                target_id=target_id,
                entity_type=entity_type,
                redaction_payload=payload,
                exc_info=True,
            )
            message += (
                " (WARNING: REDACTION_APPLIED audit emit failed; "
                "payload preserved in operator logs)"
            )
        return target_id, message


MAX_RETENTION_REASON_CHARS = 2000


def _sync_vector_lifecycle(
    registry: StoreRegistry,
    item_id: str,
    lifecycle: dict[str, Any],
) -> None:
    """Mirror a lifecycle stamp onto the item's vector row.

    **A vector row's metadata is a snapshot taken at embed time**, and the
    semantic strategy builds its :class:`~trellis.schemas.pack.PackItem`
    from that snapshot rather than from the document store. So an archival
    written only through ``document_store.put`` leaves the semantic path
    serving the item as though nothing happened —
    :func:`~trellis.retrieve.lifecycle.exclude_archived` reads
    ``item.metadata`` and simply never sees a lifecycle key.

    That is not hypothetical: the first production prune archived 35
    documents and every one of them kept a vector row whose metadata still
    read ``signal_quality="standard"``. The keyword path honoured the
    archival immediately (it reads the document store); the semantic path
    did not, and a pack-level test written against
    :class:`~trellis.retrieve.strategies.KeywordSearch` alone could not see
    the difference.

    Metadata-only update: the existing embedding is re-``upsert``-ed
    unchanged, so nothing is re-embedded and no cost is incurred. A missing
    vector row is normal (structural nodes, un-embedded documents) and is a
    no-op rather than an error — retention must not fail because an item was
    never embedded.
    """
    try:
        store = registry.knowledge.vector_store
        row = store.get(item_id)
        if row is None:
            return
        metadata = dict(row.get("metadata") or {})
        metadata[LIFECYCLE_KEY] = lifecycle
        store.upsert(item_id, row["vector"], metadata)
    except Exception:
        # Fail soft and loud: the document-store stamp is the authoritative
        # record and has already been written. A vector backend outage must
        # not roll back an archival, but it does leave the semantic path
        # stale, so it is logged rather than swallowed.
        logger.warning(
            "retention_vector_lifecycle_sync_failed",
            item_id=item_id,
            exc_info=True,
        )


def _prune_message(
    report: ResolutionReport,
    by_reason: dict[str, int],
    *,
    dry_run: bool,
    archived: int,
    skipped: int,
    resynced: int,
) -> str:
    """Render the operator-facing summary for one prune run.

    Every qualifier here reports something that would otherwise be silent:
    a capped scan (the candidate set is a prefix), a requested lifecycle
    state this phase cannot select at all, items that vanished mid-run,
    vector rows repaired, and items protected because a human restored them.
    """
    verb = "would archive" if dry_run else "archived"
    counts = ", ".join(f"{k}={v}" for k, v in sorted(by_reason.items())) or "none"
    message = (
        f"retention.prune ({'dry run' if dry_run else 'applied'}): "
        f"{verb} {len(report.candidates) if dry_run else archived} item(s) "
        f"[{counts}]"
    )
    if report.unselectable_lifecycle_states:
        # #419: without this the operator cannot tell "no rows are in that
        # state" from "this phase can never select that state".
        inert = ", ".join(report.unselectable_lifecycle_states)
        # Phrased as the rule, not the writer: "current items were explicitly
        # restored" would be the same provenance claim #419's dangerous half
        # was about. ``current`` is protected for what the value asserts,
        # whoever wrote it.
        message += (
            f"; WARNING lifecycle state(s) [{inert}] cannot be selected by "
            "phase one (archived is already archived; current asserts the "
            "item belongs in service) — they scanned nothing and their zero "
            "says nothing about the corpus"
        )
    if report.scan_truncated:
        message += (
            f"; WARNING scan capped at {MAX_DOCUMENTS_SCANNED} documents — "
            "candidate set is a prefix, re-run to continue"
        )
    if skipped:
        message += f"; {skipped} skipped (vanished between resolve and write)"
    if resynced:
        message += f"; {resynced} stale vector row(s) re-synced"
    if report.skipped_restored:
        # Not "(explicitly restored)": ``retention.restore`` is the writer
        # that motivated the guard, not the only one that can trip it — an
        # ``entity.update`` property merge or any bag-forwarding save writes
        # ``current`` too, and lands in this same counter (#419).
        message += (
            f"; {report.skipped_restored} protected (state=current — "
            "asserted in service)"
        )
    return message


class RetentionPruneHandler:
    """Resolve retention criteria and archive the resulting candidates.

    Wires :data:`Operation.RETENTION_PRUNE` into the governed pipeline. The
    enum verb shipped with an empty ``set()`` args schema and no handler, so
    the executor rejected every ``retention.prune`` command with "No handler
    registered" — the exact gap ``redaction.apply`` had before
    :class:`RedactionApplyHandler`, and the reason 24 noise-tagged captures
    had to be demoted-and-kept rather than removed. Decision record:
    ``docs/design/adr-retention-prune.md`` (Option A, phase one).

    **Phase one is archival, not purge.** The handler stamps
    :class:`~trellis.schemas.classification.Lifecycle`
    ``state="archived"`` and retrieval stops serving the item
    (:mod:`trellis.retrieve.lifecycle`). Physical removal is deliberately
    deferred until the archived population is real: retention is criteria-
    driven and a criteria bug is a *batch* mistake, so the version an
    operator can walk back is the one worth shipping first. This makes
    ``Lifecycle`` — schema-without-behaviour since it landed — both its
    first writer and its first enforcement point.

    **Dry-run by default.** ``dry_run`` defaults to ``True`` when the caller
    omits it. Destructive-by-default is right for a single named redaction
    target and wrong for a predicate that resolves to a set the caller has
    not seen; a dry run emits the same event flagged ``dry_run=True`` so the
    preview is as auditable as the real thing.

    **Criteria-driven batches tolerate a moving candidate set.** An item
    that vanishes between resolution and write is counted in ``skipped``,
    not raised — unlike redaction's single-target
    :class:`~trellis.errors.NotFoundError`, a batch whose population shifts
    underneath it has not failed. Items already archived are filtered during
    resolution, so a re-run is a no-op rather than a second version bump.
    Use ``Command.idempotency_key`` when at-most-once semantics are needed.

    **Never traces, never event rows.** Enforced by construction:
    :func:`~trellis.mutate.retention.resolve_candidates` reads the document
    and graph stores and nothing else. Confirmed entities are dropped on
    every criterion — confirmation is a human's judgment that the entity is
    real, and age is not evidence against it.

    ``PolicyType.RETENTION`` gets its first consumer here: a deny rule on
    ``retention.prune`` is evaluated by the executor's standard policy stage
    before this handler runs, which is the operator's kill switch.
    """

    def __init__(self, registry: StoreRegistry) -> None:
        self._registry = registry

    def handle(self, command: Command) -> tuple[str | None, str]:
        raw_criteria = command.args["criteria"]
        if not isinstance(raw_criteria, dict):
            msg = "retention.prune criteria must be an object"
            raise ValidationError(msg, code="retention_criteria_invalid")
        criteria = RetentionCriteria.model_validate(raw_criteria)

        reason = str(command.args["reason"] or "").strip()
        if not reason:
            msg = "retention.prune requires a non-empty reason for the audit trail"
            raise ValidationError(msg, code="retention_reason_required")
        if len(reason) > MAX_RETENTION_REASON_CHARS:
            msg = (
                f"retention.prune reason exceeds {MAX_RETENTION_REASON_CHARS} "
                "chars; it is written verbatim to the append-only audit log — "
                "keep it short and content-free"
            )
            raise ValidationError(msg, code="retention_reason_too_long")

        dry_run = bool(command.args.get("dry_run", True))

        event_log = self._registry.operational.event_log
        if not dry_run and isinstance(event_log, NullEventLog):
            msg = (
                "retention.prune requires a persisting event log for a "
                "non-dry run: the RETENTION_PRUNED event is the only record "
                "that the archival happened, and this deployment's event_log "
                "backend is 'null'"
            )
            raise ValidationError(msg, code="retention_requires_event_log")

        report = resolve_candidates(criteria, self._registry)

        archived = 0
        skipped = 0
        resynced = 0
        if not dry_run:
            for candidate in report.candidates:
                if self._archive(candidate, reason):
                    archived += 1
                else:
                    skipped += 1
            # Self-healing: an item archived before the vector sync existed
            # still has a stale snapshot, and the semantic path serves from
            # that snapshot. Re-running the prune repairs them — no new verb
            # for what is a one-shot consequence of shipping the sync late.
            resynced = self._resync_archived(report.already_archived_ids)

        by_reason: dict[str, int] = {}
        by_kind: dict[str, int] = {}
        for candidate in report.candidates:
            code = candidate.reason_code
            by_reason[code] = by_reason.get(code, 0) + 1
            by_kind[candidate.kind] = by_kind.get(candidate.kind, 0) + 1

        payload: dict[str, Any] = {
            "criteria": criteria.model_dump(mode="json"),
            "reason": reason,
            "command_id": command.command_id,
            "requested_by": command.requested_by,
            "dry_run": dry_run,
            "phase": "archival",
            "candidates": len(report.candidates),
            "archived": archived,
            "skipped": skipped,
            "by_reason": by_reason,
            "by_kind": by_kind,
            "documents_scanned": report.documents_scanned,
            "entities_scanned": report.entities_scanned,
            "scan_truncated": report.scan_truncated,
            "skipped_already_archived": report.skipped_already_archived,
            "skipped_confirmed": report.skipped_confirmed,
            "skipped_restored": report.skipped_restored,
            # #419: which requested lifecycle states contributed a
            # guaranteed zero. Audited alongside the criteria that named
            # them so a reviewer reading this event later can tell an inert
            # criterion from an empty corpus.
            "unselectable_lifecycle_states": report.unselectable_lifecycle_states,
            "vector_rows_resynced": resynced,
            # Capped pointer, not an index — see _LINKED_SIGNAL_LIMIT.
            "item_ids": [c.item_id for c in report.candidates[:_LINKED_SIGNAL_LIMIT]],
        }

        message = _prune_message(
            report,
            by_reason,
            dry_run=dry_run,
            archived=archived,
            skipped=skipped,
            resynced=resynced,
        )

        try:
            event_log.emit(
                EventType.RETENTION_PRUNED,
                source="mutation_executor",
                entity_id=command.command_id,
                entity_type="retention_run",
                payload=payload,
            )
        except Exception:
            # Same discipline as RedactionApplyHandler: for a non-dry run the
            # archival already happened, so raising would report FAILED for
            # completed work. Preserve the payload in operator logs.
            logger.warning(
                "retention_audit_emit_failed",
                command_id=command.command_id,
                retention_payload=payload,
                exc_info=True,
            )
            message += (
                " (WARNING: RETENTION_PRUNED audit emit failed; "
                "payload preserved in operator logs)"
            )
        return command.command_id, message

    def _resync_archived(self, item_ids: list[str]) -> int:
        """Re-stamp vector rows for already-archived items whose row is stale.

        Returns the number actually repaired — a row that already carries the
        archived stamp is left alone, so a steady-state re-run reports zero.
        """
        if not item_ids:
            return 0
        lifecycle = Lifecycle(state=ARCHIVED_STATE).model_dump(mode="json")
        vector_store = self._registry.knowledge.vector_store
        repaired = 0
        for item_id in item_ids:
            try:
                row = vector_store.get(item_id)
            except Exception:
                logger.warning(
                    "retention_vector_resync_read_failed",
                    item_id=item_id,
                    exc_info=True,
                )
                continue
            if row is None:
                continue
            metadata = row.get("metadata") or {}
            record = metadata.get(LIFECYCLE_KEY)
            if isinstance(record, dict) and record.get("state") == ARCHIVED_STATE:
                continue
            _sync_vector_lifecycle(self._registry, item_id, lifecycle)
            repaired += 1
        return repaired

    def _archive(self, candidate: RetentionCandidate, reason: str) -> bool:
        """Stamp one candidate archived. Returns False if it vanished."""
        lifecycle = Lifecycle(
            state=ARCHIVED_STATE,
            valid_until=datetime.now(UTC),
            deprecation_reason=reason,
        ).model_dump(mode="json")

        if candidate.kind == "document":
            store = self._registry.knowledge.document_store
            doc = store.get(candidate.item_id)
            if doc is None:
                return False
            metadata = dict(doc.get("metadata") or {})
            metadata[LIFECYCLE_KEY] = lifecycle
            # Metadata-only: the content written back is the row's own, and
            # only the lifecycle stamp changes.
            #
            # Masked on the *pack* surfaces — ``retrieve.lifecycle``'s
            # ``exclude_archived`` drops the row at the collect seam — and
            # ``_restore`` is the operation that hands it back to retrieval
            # still carrying whatever stamp this write left. But that is not
            # the same as latent (#406): ``retrieve.file_context`` reads the
            # column straight off ``list_documents``, with no lifecycle
            # predicate, so an archived document's bump moves the
            # ``newest_item_at`` its path reports to the read hook's staleness
            # gate immediately.
            store.put(
                candidate.item_id, doc["content"], metadata, preserve_updated_at=True
            )
            _sync_vector_lifecycle(self._registry, candidate.item_id, lifecycle)
            return True

        graph = self._registry.knowledge.graph_store
        node = graph.get_node(candidate.item_id)
        if node is None:
            return False
        props = dict(node["properties"])
        props[LIFECYCLE_KEY] = lifecycle
        # SCD-2: re-upserting closes the current version and opens a new one,
        # so the pre-archival state stays readable via get_node_history and
        # ``as_of``. Role and generation_spec are immutable across versions.
        graph.upsert_node(
            node_id=candidate.item_id,
            node_type=node["node_type"],
            properties=props,
            node_role=node.get("node_role", "semantic"),
            generation_spec=node.get("generation_spec"),
            document_ids=node.get("document_ids") or None,
        )
        return True


class RetentionRestoreHandler:
    """Return archived items to ``Lifecycle.state="current"``.

    Wires :data:`Operation.RETENTION_RESTORE`. This is the half that makes
    :class:`RetentionPruneHandler`'s central claim true: phase one is
    archival rather than purge *because* "a wrong prune is walked back by
    re-stamping" — and re-stamping needs a governed path, or the claim is
    only rhetorical. Direct store writes are not an option
    (``CLAUDE.md``: all mutations go through the governed pipeline), so
    without this verb an operator who over-pruned had no sanctioned remedy
    at all.

    **Explicit ids, not a predicate.** ``retention.prune`` resolves criteria
    because it selects a population nobody has enumerated. Restore is the
    opposite situation: the operator knows exactly which items were wrong,
    because their ids ride the ``RETENTION_PRUNED`` payload. Re-deriving
    them from criteria would re-run the selection that was wrong the first
    time.

    Restoring sets ``state="current"`` and clears the archival's
    ``valid_until`` / ``deprecation_reason`` rather than deleting the
    lifecycle record — the item is current again, and the ``RETENTION_PRUNED``
    → ``RETENTION_RESTORED`` event pair is the history.

    An id that is not archived is counted in ``skipped``, not raised: a
    corrective batch assembled from an audit payload will legitimately name
    items someone else already restored.
    """

    def __init__(self, registry: StoreRegistry) -> None:
        self._registry = registry

    def handle(self, command: Command) -> tuple[str | None, str]:
        raw_ids = command.args["item_ids"]
        if not isinstance(raw_ids, list) or not raw_ids:
            msg = "retention.restore requires a non-empty item_ids list"
            raise ValidationError(msg, code="retention_restore_ids_required")
        item_ids = [str(i) for i in raw_ids]

        reason = str(command.args["reason"] or "").strip()
        if not reason:
            msg = "retention.restore requires a non-empty reason for the audit trail"
            raise ValidationError(msg, code="retention_reason_required")
        if len(reason) > MAX_RETENTION_REASON_CHARS:
            msg = f"retention.restore reason exceeds {MAX_RETENTION_REASON_CHARS} chars"
            raise ValidationError(msg, code="retention_reason_too_long")

        restored: list[str] = []
        skipped: list[str] = []
        for item_id in item_ids:
            if self._restore(item_id):
                restored.append(item_id)
            else:
                skipped.append(item_id)

        payload: dict[str, Any] = {
            "reason": reason,
            "command_id": command.command_id,
            "requested_by": command.requested_by,
            "restored": len(restored),
            "skipped": len(skipped),
            "restored_ids": restored,
            "skipped_ids": skipped,
        }
        message = f"retention.restore: restored {len(restored)} item(s)" + (
            f", {len(skipped)} not archived (skipped)" if skipped else ""
        )
        try:
            self._registry.operational.event_log.emit(
                EventType.RETENTION_RESTORED,
                source="mutation_executor",
                entity_id=command.command_id,
                entity_type="retention_restore",
                payload=payload,
            )
        except Exception:
            logger.warning(
                "retention_restore_audit_emit_failed",
                command_id=command.command_id,
                restore_payload=payload,
                exc_info=True,
            )
            message += " (WARNING: RETENTION_RESTORED audit emit failed)"
        return command.command_id, message

    def _restore(self, item_id: str) -> bool:
        """Un-archive one item. Returns False if it was not archived."""
        current = Lifecycle(state="current").model_dump(mode="json")

        doc_store = self._registry.knowledge.document_store
        doc = doc_store.get(item_id)
        if doc is not None:
            metadata = dict(doc.get("metadata") or {})
            record = metadata.get(LIFECYCLE_KEY)
            if not isinstance(record, dict) or record.get("state") != ARCHIVED_STATE:
                return False
            metadata[LIFECYCLE_KEY] = current
            # Metadata-only, and unmasked (#406). Restore exists to make the
            # item servable again, so unlike the archive it undoes there is
            # no downstream filter left to render a bumped ``updated_at``
            # moot: without the flag, un-archiving a two-year-old note hands
            # it back to ``KeywordSearch`` as the freshest document in the
            # corpus. An operator walking back a bad prune from the
            # ``RETENTION_PRUNED`` payload would thereby *promote* exactly
            # the items they meant only to restore.
            #
            # Scoped to the document branch, and that scoping is now
            # *correct* rather than merely pending. The entity branch below
            # re-opens an SCD-2 version whose ``updated_at`` is now — but
            # SCD-2 carries ``created_at`` forward, ``GraphStore.query`` is
            # ``ORDER BY created_at DESC LIMIT n`` on every shipped backend,
            # and since #420 ``GraphSearch`` decays off that same column
            # (``retrieve.strategies.GRAPH_RECENCY_CLOCK_FIELD``). So a
            # restored node neither re-enters the graph axis's candidate
            # window nor gains rank inside it, and ``upsert_node`` needs no
            # ``preserve_updated_at`` equivalent. Fixed reader-side because
            # the writers that actually move a node's ``updated_at`` are the
            # ordinary ones (``entity.update``, extraction upserts), which no
            # kwarg here would ever have been passed on.
            doc_store.put(item_id, doc["content"], metadata, preserve_updated_at=True)
            _sync_vector_lifecycle(self._registry, item_id, current)
            return True

        graph = self._registry.knowledge.graph_store
        node = graph.get_node(item_id)
        if node is None:
            return False
        props = dict(node["properties"])
        record = props.get(LIFECYCLE_KEY)
        if not isinstance(record, dict) or record.get("state") != ARCHIVED_STATE:
            return False
        props[LIFECYCLE_KEY] = current
        graph.upsert_node(
            node_id=item_id,
            node_type=node["node_type"],
            properties=props,
            node_role=node.get("node_role", "semantic"),
            generation_spec=node.get("generation_spec"),
            document_ids=node.get("document_ids") or None,
        )
        return True


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
        Operation.ENTITY_UPDATE: EntityUpdateHandler(registry),
        Operation.LINK_CREATE: LinkCreateHandler(registry),
        Operation.OBSERVATION_RECORD: ObservationRecordHandler(registry),
        Operation.MEASUREMENT_RECORD: MeasurementRecordHandler(registry),
        Operation.REDACTION_APPLY: RedactionApplyHandler(registry),
        Operation.RETENTION_PRUNE: RetentionPruneHandler(registry),
        Operation.RETENTION_RESTORE: RetentionRestoreHandler(registry),
    }
