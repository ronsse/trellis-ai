"""Command and operation schemas for the governed mutation pipeline."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import Field

from trellis.core.base import VersionedModel, utc_now
from trellis.core.ids import generate_ulid


class Operation(StrEnum):
    """All mutation verbs supported by the pipeline."""

    # Ingest
    TRACE_INGEST = "trace.ingest"
    TRACE_APPEND_STEP = "trace.append_step"
    TRACE_RECORD_OUTCOME = "trace.record_outcome"
    EVIDENCE_INGEST = "evidence.ingest"
    EVIDENCE_ATTACH = "evidence.attach"

    # Curate
    PRECEDENT_PROMOTE = "precedent.promote"
    PRECEDENT_UPDATE = "precedent.update"
    ENTITY_CREATE = "entity.create"
    ENTITY_UPDATE = "entity.update"
    ENTITY_MERGE = "entity.merge"
    LINK_CREATE = "link.create"
    LINK_REMOVE = "link.remove"
    LABEL_ADD = "label.add"
    LABEL_REMOVE = "label.remove"
    FEEDBACK_RECORD = "feedback.record"

    # Maintain
    REDACTION_APPLY = "redaction.apply"
    RETENTION_PRUNE = "retention.prune"
    PACK_PUBLISH = "pack.publish"
    PACK_INVALIDATE = "pack.invalidate"


class CommandStatus(StrEnum):
    """Outcome status of a command execution."""

    SUCCESS = "success"
    REJECTED = "rejected"
    FAILED = "failed"
    DUPLICATE = "duplicate"


class BatchStrategy(StrEnum):
    """Strategy for executing a batch of commands."""

    SEQUENTIAL = "sequential"
    STOP_ON_ERROR = "stop_on_error"
    CONTINUE_ON_ERROR = "continue_on_error"


class Command(VersionedModel):
    """A mutation command submitted to the pipeline."""

    command_id: str = Field(default_factory=generate_ulid)
    operation: Operation
    target_id: str | None = None
    target_type: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    requested_by: str = "unknown"
    idempotency_key: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class CommandResult(VersionedModel):
    """Result of executing a command through the pipeline."""

    command_id: str
    status: CommandStatus
    operation: Operation
    target_id: str | None = None
    created_id: str | None = None
    message: str = ""
    warnings: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    executed_at: datetime = Field(default_factory=utc_now)


class CommandBatch(VersionedModel):
    """A batch of commands to execute."""

    batch_id: str = Field(default_factory=generate_ulid)
    commands: list[Command]
    strategy: BatchStrategy = BatchStrategy.STOP_ON_ERROR
    requested_by: str = "unknown"
    metadata: dict[str, Any] = Field(default_factory=dict)


class OperationRegistry:
    """Registry of valid operations and their argument schemas."""

    def __init__(self) -> None:
        self._schemas: dict[Operation, set[str]] = {}
        self._register_defaults()

    def _register_defaults(self) -> None:
        """Register default required args for each operation."""
        self._schemas[Operation.TRACE_INGEST] = {"trace"}
        self._schemas[Operation.TRACE_APPEND_STEP] = {"trace_id", "step"}
        self._schemas[Operation.TRACE_RECORD_OUTCOME] = {"trace_id", "outcome"}
        self._schemas[Operation.EVIDENCE_INGEST] = {"evidence"}
        self._schemas[Operation.EVIDENCE_ATTACH] = {
            "evidence_id",
            "target_id",
            "target_type",
        }
        self._schemas[Operation.PRECEDENT_PROMOTE] = {
            "trace_id",
            "title",
            "description",
        }
        self._schemas[Operation.PRECEDENT_UPDATE] = {"precedent_id"}
        self._schemas[Operation.ENTITY_CREATE] = {"entity_type", "name"}
        self._schemas[Operation.ENTITY_UPDATE] = {"entity_id"}
        self._schemas[Operation.ENTITY_MERGE] = {"source_id", "target_id"}
        self._schemas[Operation.LINK_CREATE] = {
            "source_id",
            "target_id",
            "edge_kind",
        }
        self._schemas[Operation.LINK_REMOVE] = {"edge_id"}
        self._schemas[Operation.LABEL_ADD] = {"target_id", "label"}
        self._schemas[Operation.LABEL_REMOVE] = {"target_id", "label"}
        self._schemas[Operation.FEEDBACK_RECORD] = {"target_id", "rating"}
        self._schemas[Operation.REDACTION_APPLY] = {"target_id", "reason"}
        self._schemas[Operation.RETENTION_PRUNE] = set()
        self._schemas[Operation.PACK_PUBLISH] = {"pack"}
        self._schemas[Operation.PACK_INVALIDATE] = {"pack_id"}

    def validate(self, command: Command) -> tuple[bool, list[str]]:
        """Validate a command's args against its operation's schema.

        Returns (valid, errors).
        """
        required = self._schemas.get(command.operation)
        if required is None:
            return False, [f"Unknown operation: {command.operation}"]
        missing = required - set(command.args.keys())
        if missing:
            return False, [f"Missing required args: {', '.join(sorted(missing))}"]
        return True, []

    def get_required_args(self, operation: Operation) -> set[str]:
        """Get required args for an operation."""
        return self._schemas.get(operation, set())
