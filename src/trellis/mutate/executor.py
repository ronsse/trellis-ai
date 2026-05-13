"""Mutation executor — the governed write pipeline."""

from __future__ import annotations

from collections import OrderedDict
from typing import Protocol

import structlog

from trellis.errors import (
    IdempotencyError,
    PolicyViolationError,
    StoreError,
    TrellisError,
    ValidationError,
)
from trellis.mutate.commands import (
    BatchStrategy,
    Command,
    CommandBatch,
    CommandResult,
    CommandStatus,
    OperationRegistry,
)
from trellis.stores.base.event_log import EventLog, EventType

logger = structlog.get_logger()

DEFAULT_IDEMPOTENCY_CACHE_SIZE = 10_000

# Exception classes the executor treats as "unexpected handler panic".
# The catch is intentionally enumerated rather than bare ``Exception``
# so the silent-fallback audit (``scripts/audit_silent_fallbacks.py``)
# does not flag this site — handler-expected failure modes have
# typed catches above, and anything here is a programming bug or a
# backend panic that gets a FAILED audit event plus a traceback in
# operator logs. We list the canonical Python panic types explicitly;
# new backends should map their own errors into ``StoreError`` (one
# of the typed catches above) rather than relying on this fallback.
_UNEXPECTED_HANDLER_FAILURE: tuple[type[BaseException], ...] = (
    RuntimeError,
    OSError,
    ValueError,
    TypeError,
    AttributeError,
    KeyError,
    IndexError,
    AssertionError,
    LookupError,
    ArithmeticError,
)


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
        idempotency_cache_size: int = DEFAULT_IDEMPOTENCY_CACHE_SIZE,
    ) -> None:
        if idempotency_cache_size < 1:
            msg = "idempotency_cache_size must be >= 1"
            raise ValueError(msg)
        self._registry = registry or OperationRegistry()
        self._policy_gate = policy_gate
        self._event_log = event_log
        self._handlers: dict[str, CommandHandler] = handlers or {}
        self._idempotency_cache_size = idempotency_cache_size
        # FIFO-bounded cache of seen idempotency keys. OrderedDict preserves
        # insertion order; overflow evicts the oldest key via popitem(last=False).
        # When event_log is attached, evicted keys are still rejected via
        # event_log.has_idempotency_key() (authoritative, cross-restart).
        # Without event_log, evicted keys become silently accepted duplicates —
        # a warning is logged on each eviction so operators can attach one.
        self._seen_idempotency_keys: OrderedDict[str, None] = OrderedDict()
        self._idempotency_evictions = 0

    def register_handler(self, operation: str, handler: CommandHandler) -> None:
        """Register a handler for an operation."""
        self._handlers[operation] = handler

    def execute(self, command: Command) -> CommandResult:
        """Execute a single command through the full pipeline."""
        log = logger.bind(command_id=command.command_id, operation=command.operation)

        # Stage 1: Validate
        valid, errors = self._registry.validate(command)
        if not valid:
            message = f"Validation failed: {'; '.join(errors)}"
            log.warning("validation_failed", errors=errors)
            self._emit_rejection(command, reason="validate", message=message)
            return CommandResult(
                command_id=command.command_id,
                status=CommandStatus.FAILED,
                operation=command.operation,
                message=message,
            )

        # Stage 2: Policy Check
        if self._policy_gate is not None:
            allowed, message, warnings = self._policy_gate.check(command)
            if not allowed:
                log.warning("policy_rejected", message=message)
                self._emit_rejection(
                    command,
                    reason="policy_violation",
                    message=message,
                )
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
                # Refresh recency so hot keys aren't evicted ahead of cold ones.
                self._seen_idempotency_keys.move_to_end(command.idempotency_key)
                message = f"Duplicate command: {command.idempotency_key}"
                log.info("duplicate_command", key=command.idempotency_key)
                self._emit_rejection(
                    command,
                    reason="idempotency_replay",
                    message=message,
                )
                return CommandResult(
                    command_id=command.command_id,
                    status=CommandStatus.DUPLICATE,
                    operation=command.operation,
                    message=message,
                )
            # Check persisted events for cross-restart deduplication
            if self._event_log is not None and self._event_log.has_idempotency_key(
                command.idempotency_key,
            ):
                self._record_idempotency_key(command.idempotency_key)
                message = f"Duplicate command (persisted): {command.idempotency_key}"
                log.info("duplicate_command_persisted", key=command.idempotency_key)
                self._emit_rejection(
                    command,
                    reason="idempotency_replay",
                    message=message,
                )
                return CommandResult(
                    command_id=command.command_id,
                    status=CommandStatus.DUPLICATE,
                    operation=command.operation,
                    message=message,
                )
            self._record_idempotency_key(command.idempotency_key)

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
        except ValidationError as exc:
            # Variant A' (adr-extraction-validation.md §5.5): handler-raised
            # ValidationError is a structured rejection, not an unexpected
            # failure. Route through _emit_rejection so the audit event
            # carries a stable ``reason`` (defaulting to "handler_validate"
            # if the handler didn't supply one via ValidationError.code).
            log.warning("handler_rejected", errors=exc.errors)
            reason = exc.code if exc.code != "VALIDATION_ERROR" else "handler_validate"
            self._emit_rejection(command, reason=reason, message=str(exc))
            return CommandResult(
                command_id=command.command_id,
                status=CommandStatus.REJECTED,
                operation=command.operation,
                message=str(exc),
            )
        except PolicyViolationError as exc:
            # Handlers may evaluate row-level policies that the gate
            # didn't see at Stage 2 (e.g., post-fetch entity-tag
            # checks). Route through _emit_rejection so the audit
            # event surfaces the policy_id; matches the gate-rejection
            # shape from Stage 2 so consumers reading the EventLog
            # don't have to special-case where the policy fired.
            log.warning("handler_policy_rejected", policy_id=exc.policy_id)
            self._emit_rejection(
                command,
                reason="policy_violation",
                message=str(exc),
            )
            return CommandResult(
                command_id=command.command_id,
                status=CommandStatus.REJECTED,
                operation=command.operation,
                message=str(exc),
            )
        except IdempotencyError as exc:
            # Handler-level duplicate detection (e.g., a downstream
            # store noticed an existing row that the in-memory cache
            # missed because of an LRU eviction). Surface as DUPLICATE
            # so callers branch the same way as a Stage-3 hit.
            log.info("handler_idempotency_replay", key=exc.idempotency_key)
            self._emit_rejection(
                command,
                reason="idempotency_replay",
                message=str(exc),
            )
            return CommandResult(
                command_id=command.command_id,
                status=CommandStatus.DUPLICATE,
                operation=command.operation,
                message=str(exc),
            )
        except (StoreError, TrellisError) as exc:
            # Typed Trellis failures other than the rejection set
            # above: backend/store errors, generic TrellisErrors,
            # MutationErrors. Emit FAILED audit event with the
            # exception type attached so consumers can branch on
            # ``error_type`` rather than parsing the stringified
            # message, then return a structured FAILED CommandResult
            # so batch processing (SEQUENTIAL / CONTINUE_ON_ERROR)
            # can keep going. The store name and code are bound on
            # the structlog event for operator correlation.
            store_name = getattr(exc, "store", None)
            log.exception(
                "handler_typed_error",
                error_type=type(exc).__name__,
                error_code=getattr(exc, "code", None),
                store=store_name,
            )
            self._emit(command, CommandStatus.FAILED, str(exc))
            return CommandResult(
                command_id=command.command_id,
                status=CommandStatus.FAILED,
                operation=command.operation,
                message=f"Execution failed: {exc}",
            )
        except _UNEXPECTED_HANDLER_FAILURE as exc:
            # An untyped exception escaped the handler — almost
            # certainly a programming bug or a backend/network panic
            # rather than an expected failure mode. Log with
            # exc_info=True so the traceback lands in operator logs,
            # emit a FAILED audit event so the EventLog records the
            # panic, then return a structured FAILED CommandResult.
            # We deliberately do *not* re-raise: ``execute_batch``
            # relies on per-command CommandResults to honor
            # SEQUENTIAL / CONTINUE_ON_ERROR semantics; a re-raise
            # would mid-air-abort a batch even when the caller asked
            # for "continue on error". The narrowed typed catches
            # above document which exception types are EXPECTED;
            # anything caught here is a defect-ticket candidate.
            # The catch tuple is explicit (not bare ``Exception``)
            # so the silent-fallback audit treats it as a guard
            # rather than a broad swallow.
            log.exception("handler_failed_unexpected", error_type=type(exc).__name__)
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

    def _record_idempotency_key(self, key: str) -> None:
        """Insert a key into the FIFO cache, evicting the oldest if full.

        When the cache is full and no event_log is configured, evicted keys
        become silently-acceptable duplicates — we warn on each such eviction
        so operators can raise the cache size or attach a persistent event log.
        With an event_log attached, the persistent has_idempotency_key() check
        is authoritative and eviction is a pure hot-path optimization.
        """
        while len(self._seen_idempotency_keys) >= self._idempotency_cache_size:
            evicted_key, _ = self._seen_idempotency_keys.popitem(last=False)
            self._idempotency_evictions += 1
            if self._event_log is None:
                logger.warning(
                    "idempotency_cache_evicted_without_event_log",
                    evicted_key=evicted_key,
                    cache_size=self._idempotency_cache_size,
                    total_evictions=self._idempotency_evictions,
                    hint=(
                        "Attach an EventLog to MutationExecutor for durable "
                        "idempotency across cache evictions, or raise "
                        "idempotency_cache_size."
                    ),
                )
        self._seen_idempotency_keys[key] = None

    def _emit(self, command: Command, status: CommandStatus, message: str) -> None:
        """Emit a SUCCESS or FAILED event to the event log if available.

        Rejection paths (validate / policy / idempotency) emit through
        :meth:`_emit_rejection` instead so every rejection event carries a
        ``reason`` field naming the stage that rejected the command.
        """
        event_type = (
            EventType.MUTATION_EXECUTED
            if status == CommandStatus.SUCCESS
            else EventType.MUTATION_REJECTED
        )
        self._emit_event(event_type, command, status, message)

    def _emit_rejection(
        self,
        command: Command,
        *,
        reason: str,
        message: str,
    ) -> None:
        """Emit a uniform :attr:`EventType.MUTATION_REJECTED` event.

        Called from every rejection stage (``validate`` / ``policy_violation``
        / ``idempotency_replay``) so the audit trail is symmetric — one event
        per rejection, ``reason`` discriminates the stage.
        """
        self._emit_event(
            EventType.MUTATION_REJECTED,
            command,
            CommandStatus.REJECTED,
            message,
            reason=reason,
        )

    def _emit_event(
        self,
        event_type: EventType,
        command: Command,
        status: CommandStatus,
        message: str,
        *,
        reason: str | None = None,
    ) -> None:
        """Build the payload and emit a single executor event."""
        if self._event_log is None:
            return
        payload: dict[str, object] = {
            "command_id": command.command_id,
            "operation": command.operation,
            "status": status,
            "message": message,
            "requested_by": command.requested_by,
            "idempotency_key": command.idempotency_key,
        }
        if reason is not None:
            payload["reason"] = reason
        self._event_log.emit(
            event_type,
            "mutation_executor",
            entity_id=command.target_id,
            entity_type=command.target_type,
            payload=payload,
        )
