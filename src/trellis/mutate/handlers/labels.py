"""The ``label.add`` and ``label.remove`` handlers."""

from __future__ import annotations

from typing import Any

from trellis.errors import ValidationError
from trellis.mutate.commands import Command
from trellis.mutate.handlers._common import _rewrite_current_node
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry


class LabelAddHandler:
    """Read node from graph store, add label to properties, write a new version."""

    def __init__(self, registry: StoreRegistry) -> None:
        self._registry = registry

    def handle(self, command: Command) -> tuple[str | None, str]:
        target_id = command.args["target_id"]
        label = command.args["label"]
        store = self._registry.knowledge.graph_store

        def add(node: dict[str, Any]) -> dict[str, Any]:
            props = dict(node["properties"])
            labels = props.get("labels", [])
            if label not in labels:
                labels.append(label)
            props["labels"] = labels
            return {"properties": props}

        if _rewrite_current_node(store, target_id, add) is None:
            # Raised, not returned: the executor audits any return as a write
            # that happened. ``code`` becomes the rejection's audit reason. A
            # node purged mid-command is refused the same way.
            msg = f"Node not found: {target_id}"
            raise ValidationError(msg, errors=[msg], code="target_not_found")

        self._registry.operational.event_log.emit(
            EventType.LABEL_ADDED,
            source="mutation_executor",
            entity_id=target_id,
            payload={"label": label},
        )
        return target_id, f"Label '{label}' added to {target_id}"


class LabelRemoveHandler:
    """Read node, remove label from properties, write a new version."""

    def __init__(self, registry: StoreRegistry) -> None:
        self._registry = registry

    def handle(self, command: Command) -> tuple[str | None, str]:
        target_id = command.args["target_id"]
        label = command.args["label"]
        store = self._registry.knowledge.graph_store

        def remove(node: dict[str, Any]) -> dict[str, Any]:
            props = dict(node["properties"])
            labels = props.get("labels", [])
            if label in labels:
                labels.remove(label)
            props["labels"] = labels
            return {"properties": props}

        if _rewrite_current_node(store, target_id, remove) is None:
            # Raised, as in LabelAddHandler.
            msg = f"Node not found: {target_id}"
            raise ValidationError(msg, errors=[msg], code="target_not_found")

        self._registry.operational.event_log.emit(
            EventType.LABEL_REMOVED,
            source="mutation_executor",
            entity_id=target_id,
            payload={"label": label},
        )
        return target_id, f"Label '{label}' removed from {target_id}"
