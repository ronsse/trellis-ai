"""Mutation executor — the governed write pipeline."""

from __future__ import annotations

from typing import Protocol

import structlog

from trellis.mutate.commands import (
    BatchStrategy,
    Command,
    CommandBatch,
    CommandResult,
    CommandStatus,
    OperationRegistry,
)
from trellis.stores.event_log import EventLog, EventType

logger = structlog.get_logger()


class PolicyGate(Protocol):
    """Protocol for policy checking. Implementations injected by caller."""

    def check(self, command: Command) -> tuple[bool, str, list[str]]:
        """Check command against policies.

        Returns (allowed, message, warnings).
        """
        ...


class CommandHandler(Protocol):
    """Protocol for operation handlers. Maps operation to store writes."""

    def handle(self, command: Command) -> tuple[str | None, str]:
        """Execute the command.

        Returns (created_id, message).
        """
        ...


class MutationExecutor:
    """Executes commands through the governed write pipeline.

    Pipeline stages:
    1. Validate — check args against OperationRegistry
    2. Policy Check — run PolicyGate (if provided)
    3. Idempotency Check — skip if duplicate idempotency_key
    4. Execute — call handler for the operation
    5. Emit Event — append to event log (if provided)
    """

    def __init__(
        self,
        *,
        registry: OperationRegistry | None = None,
        policy_gate: PolicyGate | None = None,
        event_log: EventLog | None = None,
        handlers: dict[str, CommandHandler] | None = None,
    ) -> None:
        self._registry = registry or OperationRegistry()
        self._policy_gate = policy_gate
        self._event_log = event_log
        self._handlers: dict[str, CommandHandler] = handlers or {}
        # In-memory cache of seen idempotency keys. When event_log is available,
        # also checked against persisted events for cross-restart deduplication.
        self._seen_idempotency_keys: set[str] = set()

    def register_handler(self, operation: str, handler: CommandHandler) -> None:
        """Register a handler for an operation."""
        self._handlers[operation] = handler

    def execute(self, command: Command) -> CommandResult:
        """Execute a single command through the full pipeline."""
        log = logger.bind(command_id=command.command_id, operation=command.operation)

        # Stage 1: Validate
        valid, errors = self._registry.validate(command)
        if not valid:
            log.warning("validation_failed", errors=errors)
            return CommandResult(
                command_id=command.command_id,
                status=CommandStatus.FAILED,
                operation=command.operation,
                message=f"Validation failed: {'; '.join(errors)}",
            )

        # Stage 2: Policy Check
        if self._policy_gate is not None:
            allowed, message, warnings = self._policy_gate.check(command)
            if not allowed:
                log.warning("policy_rejected", message=message)
                self._emit(command, CommandStatus.REJECTED, message)
                return CommandResult(
                    command_id=command.command_id,
                    status=CommandStatus.REJECTED,
                    operation=command.operation,
                    message=message,
                    warnings=warnings,
                )

        # Stage 3: Idempotency Check
        if command.idempotency_key:
            if command.idempotency_key in self._seen_idempotency_keys:
                log.info("duplicate_command", key=command.idempotency_key)
                return CommandResult(
                    command_id=command.command_id,
                    status=CommandStatus.DUPLICATE,
                    operation=command.operation,
                    message=f"Duplicate command: {command.idempotency_key}",
                )
            # Check persisted events for cross-restart deduplication
            if self._event_log is not None and self._event_log.has_idempotency_key(
                command.idempotency_key,
            ):
                self._seen_idempotency_keys.add(command.idempotency_key)
                log.info("duplicate_command_persisted", key=command.idempotency_key)
                return CommandResult(
                    command_id=command.command_id,
                    status=CommandStatus.DUPLICATE,
                    operation=command.operation,
                    message=f"Duplicate command (persisted): {command.idempotency_key}",
                )
            # Bound the in-memory cache to avoid unbounded growth
            _max_cache_size = 10_000
            if len(self._seen_idempotency_keys) > _max_cache_size:
                self._seen_idempotency_keys.clear()
            self._seen_idempotency_keys.add(command.idempotency_key)

        # Stage 4: Execute
        handler = self._handlers.get(command.operation)
        if handler is None:
            log.warning("no_handler", operation=command.operation)
            return CommandResult(
                command_id=command.command_id,
                status=CommandStatus.FAILED,
                operation=command.operation,
                message=f"No handler registered for: {command.operation}",
            )

        try:
            created_id, message = handler.handle(command)
        except Exception as exc:
            log.exception("handler_failed")
            self._emit(command, CommandStatus.FAILED, str(exc))
            return CommandResult(
                command_id=command.command_id,
                status=CommandStatus.FAILED,
                operation=command.operation,
                message=f"Execution failed: {exc}",
            )

        # Stage 5: Emit Event
        self._emit(command, CommandStatus.SUCCESS, message)

        log.info("command_executed", created_id=created_id)
        return CommandResult(
            command_id=command.command_id,
            status=CommandStatus.SUCCESS,
            operation=command.operation,
            target_id=command.target_id,
            created_id=created_id,
            message=message,
        )

    def execute_batch(self, batch: CommandBatch) -> list[CommandResult]:
        """Execute a batch of commands according to the batch strategy.

        Strategies:

        - **SEQUENTIAL**: Execute all commands in order. Never stops early.
          Failed/rejected results are included but do not halt processing.
        - **STOP_ON_ERROR**: Execute commands in order, halt on the first
          ``FAILED`` or ``REJECTED`` result. Remaining commands are not
          executed.
        - **CONTINUE_ON_ERROR**: Execute all commands in order. Same as
          SEQUENTIAL in behaviour, but signals to the caller that errors
          were expected and handled.
        """
        log = logger.bind(
            batch_id=batch.batch_id,
            strategy=batch.strategy,
            count=len(batch.commands),
        )
        results: list[CommandResult] = []
        for command in batch.commands:
            result = self.execute(command)
            results.append(result)
            if batch.strategy == BatchStrategy.STOP_ON_ERROR and result.status in (
                CommandStatus.FAILED,
                CommandStatus.REJECTED,
            ):
                log.warning(
                    "batch_stopped_on_error",
                    failed_command=command.command_id,
                    executed=len(results),
                    remaining=len(batch.commands) - len(results),
                )
                break

        log.info(
            "batch_completed",
            executed=len(results),
            succeeded=sum(1 for r in results if r.status == CommandStatus.SUCCESS),
            failed=sum(1 for r in results if r.status == CommandStatus.FAILED),
            rejected=sum(1 for r in results if r.status == CommandStatus.REJECTED),
            duplicates=sum(1 for r in results if r.status == CommandStatus.DUPLICATE),
        )
        return results

    def _emit(self, command: Command, status: CommandStatus, message: str) -> None:
        """Emit an event to the event log if available."""
        if self._event_log is None:
            return
        event_type = (
            EventType.MUTATION_EXECUTED
            if status == CommandStatus.SUCCESS
            else EventType.MUTATION_REJECTED
        )
        self._event_log.emit(
            event_type,
            "mutation_executor",
            entity_id=command.target_id,
            entity_type=command.target_type,
            payload={
                "command_id": command.command_id,
                "operation": command.operation,
                "status": status,
                "message": message,
                "requested_by": command.requested_by,
                "idempotency_key": command.idempotency_key,
            },
        )
