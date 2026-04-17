"""Command handlers for curate operations."""

from __future__ import annotations

from typing import Any

import structlog

from trellis.mutate.commands import Command, Operation
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry

logger = structlog.get_logger(__name__)


class PrecedentPromoteHandler:
    """Emit PRECEDENT_PROMOTED event with title/description/domain from args."""

    def __init__(self, registry: StoreRegistry) -> None:
        self._registry = registry

    def handle(self, command: Command) -> tuple[str | None, str]:
        event = self._registry.event_log.emit(
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
        store = self._registry.graph_store

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

        self._registry.event_log.emit(
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
        store = self._registry.graph_store

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

        self._registry.event_log.emit(
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
        event = self._registry.event_log.emit(
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
        node_id = self._registry.graph_store.upsert_node(
            node_id=caller_id,
            node_type=command.args["entity_type"],
            properties=props,
            node_role=node_role,
            generation_spec=generation_spec,
        )

        self._registry.event_log.emit(
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
    """Validate both nodes exist, create edge via graph store."""

    def __init__(self, registry: StoreRegistry) -> None:
        self._registry = registry

    def _resolve_node(self, node_id: str) -> str | None:
        """Resolve a node ID, falling back to property-based lookup.

        Tries:
          1. Direct ``get_node(node_id)`` (exact match on node_id column)
          2. Property lookup: ``properties->>'entity_id' = node_id``
        """
        store = self._registry.graph_store
        if store.get_node(node_id) is not None:
            return node_id
        # Fallback: search by entity_id stored in properties
        results = store.query(properties={"entity_id": node_id}, limit=1)
        if results:
            node_id_val: str | None = results[0]["node_id"]
            return node_id_val
        return None

    def handle(self, command: Command) -> tuple[str | None, str]:
        source_id = command.args["source_id"]
        target_id = command.args["target_id"]
        edge_kind = command.args["edge_kind"]
        store = self._registry.graph_store

        resolved_source = self._resolve_node(source_id)
        if resolved_source is None:
            msg = f"Source node not found: {source_id}"
            raise ValueError(msg)
        resolved_target = self._resolve_node(target_id)
        if resolved_target is None:
            msg = f"Target node not found: {target_id}"
            raise ValueError(msg)
        source_id = resolved_source
        target_id = resolved_target

        edge_id = store.upsert_edge(
            source_id=source_id,
            target_id=target_id,
            edge_type=edge_kind,
            properties=command.args.get("properties"),
        )

        self._registry.event_log.emit(
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
        Operation.PRECEDENT_PROMOTE: PrecedentPromoteHandler(registry),
        Operation.LABEL_ADD: LabelAddHandler(registry),
        Operation.LABEL_REMOVE: LabelRemoveHandler(registry),
        Operation.FEEDBACK_RECORD: FeedbackRecordHandler(registry),
        Operation.ENTITY_CREATE: EntityCreateHandler(registry),
        Operation.LINK_CREATE: LinkCreateHandler(registry),
    }
