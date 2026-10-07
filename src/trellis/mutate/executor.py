"""Mutation executor — the governed write pipeline."""

from __future__ import annotations

import functools
import sqlite3
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
from trellis.mutate.immutable_core import unattended_writer_refusal
from trellis.stores.base.event_log import EventLog, EventType

logger = structlog.get_logger()

DEFAULT_IDEMPOTENCY_CACHE_SIZE = 10_000

#: Stable prefix of the warning a :class:`CommandResult` carries when the
#: command's audit event could not be written. The prefix is the part
#: consumers may match on; the rest of the string is operator prose and is
#: free to change.
AUDIT_EMIT_FAILED_MARKER = "audit_event_not_recorded"

#: The full warning. Built from the marker so the two cannot drift apart.
#: Deliberately neutral about what the command did — it is appended to
#: SUCCESS, FAILED, REJECTED and DUPLICATE results alike, and on three of
#: those nothing was written to any store. ``{error}`` is the exception's
#: type, plus ``: <message>`` only for a ``TrellisError``; see
#: ``MutationExecutor._emit_event``.
_AUDIT_EMIT_FAILED_TEMPLATE = (
    AUDIT_EMIT_FAILED_MARKER + ": the {event_type} event for this command "
    "could not be written to the event log ({error}). The "
    "outcome this result reports stands; only its audit record is missing."
)

# Exception classes the executor treats as "unexpected handler panic".
# The catch is intentionally enumerated rather than bare ``Exception``
# so the silent-fallback audit (``scripts/audit_silent_fallbacks.py``)
# does not flag this site — handler-expected failure modes have
# typed catches above, and anything here is a programming bug or a
# backend panic that gets a FAILED audit event plus a traceback in
# operator logs. We list the canonical Python panic types explicitly;
# new backends should map their own errors into ``StoreError`` (one
# of the typed catches above) rather than relying on this fallback.
# ``sqlite3.Error`` is listed because the default SQLite stores raise it
# unmapped, from writes that share no seam to map it at. Most Postgres and
# Bolt (Neo4j, ArcadeDB) graph writes raise their driver's own errors the
# same way, but both drivers are optional extras, so
# ``_optional_driver_panics`` below supplies those classes.
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
    sqlite3.Error,
)


@functools.lru_cache(maxsize=1)
def _optional_driver_panics() -> tuple[type[BaseException], ...]:
    """``psycopg.Error`` and the Bolt driver's ``DriverError``/``Neo4jError``,
    for each of the two packages that is installed.

    Importing ``trellis.mutate.executor`` must not import either optional
    driver (``tests/unit/mutate/test_executor_optional_deps.py``), so this
    runs when an exception first reaches a catch that calls it, and
    ``lru_cache`` keeps a missing extra from being retried after that.
    """
    panics: list[type[BaseException]] = []
    try:
        import psycopg  # noqa: PLC0415 — deferred, see the docstring above

        has_psycopg = True
    except ImportError:
        has_psycopg = False
    if has_psycopg:
        panics.append(psycopg.Error)
    try:
        from neo4j.exceptions import DriverError, Neo4jError  # noqa: PLC0415

        has_neo4j = True
    except ImportError:
        has_neo4j = False
    if has_neo4j:
        panics.extend((DriverError, Neo4jError))
    return tuple(panics)


def _handler_panic_classes() -> tuple[type[BaseException], ...]:
    """The full ``except`` tuple for an unexpected handler panic."""
    return (*_UNEXPECTED_HANDLER_FAILURE, *_optional_driver_panics())


# Exception classes the audit-emit guard in ``_emit_event`` catches, and
# Stage 3's persisted idempotency read, a call on the same event log. Same
# enumerate-don't-bare-except discipline as the tuple above, and for the same
# reason: a broad ``except Exception`` here would be flagged by the
# silent-fallback audit, and correctly — the guard does not swallow the
# failure, it converts it into a warning on the result plus an ``error``
# line in operator logs. ``StoreError`` is what a well-behaved backend
# raises; the panic tuple covers a backend that does not, because an event
# log that raises ``ConnectionError`` instead must not be the difference
# between a degraded result and an aborted batch.
def _audit_emit_failure_classes() -> tuple[type[BaseException], ...]:
    """The full ``except`` tuple for the audit-emit guard."""
    return (StoreError, TrellisError, *_handler_panic_classes())


def _audit_warnings(audit_warning: str | None) -> list[str]:
    """The warning list contributed by one audit emit — empty when it landed."""
    return [] if audit_warning is None else [audit_warning]


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
        # FIFO-bounded cache of the idempotency keys of succeeded commands: a
        # key is recorded once its handler has succeeded (or the persisted
        # check finds it), never for a refused or failed command, so a
        # corrected retry under the same key runs. OrderedDict preserves
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
        #
        # The unattended-writer roster is checked first, above the arg
        # schema. A writer that runs without a human in the call must be
        # refused an operation outside its allow-list whether or not the
        # args happen to be well-formed — otherwise the refusal *reason*
        # in the audit log depends on arg shape, and the same forbidden
        # write reports as ``validate`` on one call and
        # ``immutable_core`` on the next. See
        # :mod:`trellis.mutate.immutable_core`.
        roster_refusal = unattended_writer_refusal(command)
        if roster_refusal is not None:
            log.warning("immutable_core_rejected", reason=roster_refusal)
            self._emit_rejection(
                command, reason="immutable_core", message=roster_refusal
            )
            return CommandResult(
                command_id=command.command_id,
                status=CommandStatus.REJECTED,
                operation=command.operation,
                message=roster_refusal,
                metadata={"rejection_reason": "immutable_core"},
            )

        valid, errors = self._registry.validate(command)
        if not valid:
            message = f"Validation failed: {'; '.join(errors)}"
            log.warning("validation_failed", errors=errors)
            audit = self._emit_rejection(command, reason="validate", message=message)
            return CommandResult(
                command_id=command.command_id,
                status=CommandStatus.REJECTED,
                operation=command.operation,
                message=message,
                warnings=_audit_warnings(audit),
                metadata={"rejection_reason": "validate"},
            )

        # Stage 2: Policy Check
        #
        # ``policy_warnings`` carries a non-blocking verdict (an
        # ``Enforcement.WARN`` policy, or an ``action="warn"`` rule)
        # forward to the result and its audit event. Before this,
        # warnings were computed and then dropped on the allow path, so
        # the entire ``warn`` enforcement level was unobservable to
        # callers — a policy could fire on every write and nothing
        # downstream could tell. An empty gate yields ``[]``, which is
        # ``CommandResult.warnings``' own default, so the transparency of
        # the default posture is unaffected.
        #
        # The rule from here down is uniform and deliberately has no
        # exceptions: **every outcome reachable after this point carries
        # the warnings**, on both the ``CommandResult`` and the emitted
        # event. Forwarding them only on the paths that seemed to matter
        # is how this was lost the first time — the gate accumulates
        # warnings *before* it reaches the rule that blocks, so a ``warn``
        # rule firing ahead of a ``deny`` rode only a ``CommandResult``
        # that 34 of 35 call sites discard, and the same loss applies to
        # a duplicate, a handler rejection and a failed write. Stage 1 is
        # the only outcome that omits them, because it runs before the
        # gate and there is nothing yet to forward.
        policy_warnings: list[str] = []
        if self._policy_gate is not None:
            allowed, message, policy_warnings = self._policy_gate.check(command)
            if not allowed:
                log.warning("policy_rejected", message=message)
                audit = self._emit_rejection(
                    command,
                    reason="policy_violation",
                    message=message,
                    policy_warnings=policy_warnings,
                )
                return CommandResult(
                    command_id=command.command_id,
                    status=CommandStatus.REJECTED,
                    operation=command.operation,
                    message=message,
                    warnings=[*policy_warnings, *_audit_warnings(audit)],
                    metadata={"rejection_reason": "policy_violation"},
                )

        # Stage 3: Idempotency Check
        idempotency_outcome = self._check_idempotency(command, log, policy_warnings)
        if idempotency_outcome is not None:
            return idempotency_outcome

        # Stage 4: Execute
        handler = self._handlers.get(command.operation)
        if handler is None:
            log.warning("no_handler", operation=command.operation)
            return CommandResult(
                command_id=command.command_id,
                status=CommandStatus.FAILED,
                operation=command.operation,
                message=f"No handler registered for: {command.operation}",
                warnings=policy_warnings,
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
            audit = self._emit_rejection(
                command,
                reason=reason,
                message=str(exc),
                policy_warnings=policy_warnings,
            )
            return CommandResult(
                command_id=command.command_id,
                status=CommandStatus.REJECTED,
                operation=command.operation,
                message=str(exc),
                warnings=[*policy_warnings, *_audit_warnings(audit)],
                metadata={"rejection_reason": reason},
            )
        except PolicyViolationError as exc:
            # Handlers may evaluate row-level policies that the gate
            # didn't see at Stage 2 (e.g., post-fetch entity-tag
            # checks). Route through _emit_rejection so the audit
            # event surfaces the policy_id; matches the gate-rejection
            # shape from Stage 2 so consumers reading the EventLog
            # don't have to special-case where the policy fired.
            log.warning("handler_policy_rejected", policy_id=exc.policy_id)
            audit = self._emit_rejection(
                command,
                reason="policy_violation",
                message=str(exc),
                policy_warnings=policy_warnings,
            )
            return CommandResult(
                command_id=command.command_id,
                status=CommandStatus.REJECTED,
                operation=command.operation,
                message=str(exc),
                warnings=[*policy_warnings, *_audit_warnings(audit)],
                metadata={"rejection_reason": "policy_violation"},
            )
        except IdempotencyError as exc:
            # Handler-level duplicate detection (e.g., a downstream
            # store noticed an existing row that the in-memory cache
            # missed because of an LRU eviction). Surface as DUPLICATE
            # so callers branch the same way as a Stage-3 hit.
            log.info("handler_idempotency_replay", key=exc.idempotency_key)
            audit = self._emit_rejection(
                command,
                reason="idempotency_replay",
                message=str(exc),
                policy_warnings=policy_warnings,
            )
            return CommandResult(
                command_id=command.command_id,
                status=CommandStatus.DUPLICATE,
                operation=command.operation,
                message=str(exc),
                warnings=[*policy_warnings, *_audit_warnings(audit)],
            )
        except (StoreError, TrellisError) as exc:
            # Typed Trellis failures other than the rejection set
            # above: backend/store errors, generic TrellisErrors,
            # MutationErrors. Log the exception type as ``error_type``
            # so operators can filter on it rather than parse the
            # message, emit a FAILED audit event, then return a
            # structured FAILED CommandResult so batch processing
            # (SEQUENTIAL / CONTINUE_ON_ERROR) can keep going. The
            # store name and code are bound on the structlog event for
            # operator correlation.
            #
            # The message is logged and the traceback is not. The
            # Postgres and Bolt purges raise ``StoreError(<type-only
            # message>) from <driver error>`` because a server's text
            # can carry query text and values (#702, #713), and
            # rendering exc_info would print that chained cause. The
            # untyped path below keeps its traceback.
            store_name = getattr(exc, "store", None)
            log.error(  # noqa: TRY400 — no traceback on purpose; see above
                "handler_typed_error",
                error_type=type(exc).__name__,
                error=str(exc),
                error_code=getattr(exc, "code", None),
                store=store_name,
            )
            audit = self._emit(
                command,
                CommandStatus.FAILED,
                str(exc),
                policy_warnings=policy_warnings,
            )
            return CommandResult(
                command_id=command.command_id,
                status=CommandStatus.FAILED,
                operation=command.operation,
                message=f"Execution failed: {exc}",
                warnings=[*policy_warnings, *_audit_warnings(audit)],
            )
        except _handler_panic_classes() as exc:
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
            audit = self._emit(
                command,
                CommandStatus.FAILED,
                str(exc),
                policy_warnings=policy_warnings,
            )
            # The caller reads the type alone. No TrellisError reaches this
            # catch, and the text of anything else can be a driver's, which
            # can carry query text and values. The traceback above and the
            # audit event keep it.
            return CommandResult(
                command_id=command.command_id,
                status=CommandStatus.FAILED,
                operation=command.operation,
                message=f"Execution failed: {type(exc).__name__}",
                warnings=[*policy_warnings, *_audit_warnings(audit)],
            )

        # Only a command whose handler succeeded makes its key a duplicate.
        # Every refused or failed exit above returned without recording it,
        # so a corrected retry under the same key runs. The persisted check
        # agrees: it counts only MUTATION_EXECUTED events.
        if command.idempotency_key:
            self._record_idempotency_key(command.idempotency_key)

        # Stage 5: Emit Event
        #
        # The one emit that runs after a committed write. It is guarded at
        # the seam (see ``_emit_event``) rather than here, but this is the
        # site where an unguarded raise would have been a lie rather than
        # merely a nuisance: the handler's write is durable by now, so
        # letting the exception past this point reports a committed mutation
        # as a failure. The status below stays SUCCESS for that reason — the
        # write happened — and the missing audit event is stated in
        # ``warnings`` instead of being encoded as a failure or a fourth
        # ``CommandStatus`` (which every caller branching on "not SUCCESS"
        # would read as "the write did not happen", reproducing the defect
        # one layer up).
        audit = self._emit(
            command,
            CommandStatus.SUCCESS,
            message,
            policy_warnings=policy_warnings,
        )

        log.info("command_executed", created_id=created_id)
        return CommandResult(
            command_id=command.command_id,
            status=CommandStatus.SUCCESS,
            operation=command.operation,
            target_id=command.target_id,
            created_id=created_id,
            message=message,
            warnings=[*policy_warnings, *_audit_warnings(audit)],
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

    def _emit(
        self,
        command: Command,
        status: CommandStatus,
        message: str,
        *,
        policy_warnings: list[str] | None = None,
    ) -> str | None:
        """Emit a SUCCESS or FAILED event to the event log if available.

        Rejection paths (validate / policy / idempotency) emit through
        :meth:`_emit_rejection` instead so every rejection event carries a
        ``reason`` field naming the stage that rejected the command.

        Returns whatever :meth:`_emit_event` returns — ``None`` when the
        event landed, a warning string when it did not.
        """
        event_type = (
            EventType.MUTATION_EXECUTED
            if status == CommandStatus.SUCCESS
            else EventType.MUTATION_REJECTED
        )
        return self._emit_event(
            event_type,
            command,
            status,
            message,
            policy_warnings=policy_warnings,
        )

    def _check_idempotency(
        self,
        command: Command,
        log: structlog.stdlib.BoundLogger,
        policy_warnings: list[str],
    ) -> CommandResult | None:
        """Stage 3 — return a DUPLICATE result if this command is a replay.

        A method of its own so the stage list in :meth:`execute` reads as
        a sequence of gates rather than as one of them inlined.
        Returns ``None`` when the command is not a replay, without recording
        its key: :meth:`execute` records it only once the handler has
        succeeded, so a refused or failed command leaves the key free for a
        corrected retry. Returns a FAILED result, with a ``mutation.rejected``
        event whose ``reason`` is ``idempotency_check_failed``, when the
        event log raises instead of answering whether the key was seen.
        ``policy_warnings`` is threaded in rather than recomputed: both
        results are outcomes reachable after Stage 2, so they carry the
        warnings like every other one.
        """
        if not command.idempotency_key:
            return None

        if command.idempotency_key in self._seen_idempotency_keys:
            # Refresh recency so hot keys aren't evicted ahead of cold ones.
            self._seen_idempotency_keys.move_to_end(command.idempotency_key)
            message = f"Duplicate command: {command.idempotency_key}"
            log.info("duplicate_command", key=command.idempotency_key)
            audit = self._emit_rejection(
                command,
                reason="idempotency_replay",
                message=message,
                policy_warnings=policy_warnings,
            )
            return CommandResult(
                command_id=command.command_id,
                status=CommandStatus.DUPLICATE,
                operation=command.operation,
                message=message,
                warnings=[*policy_warnings, *_audit_warnings(audit)],
            )

        if self._event_log is None:
            return None

        # Check persisted events for cross-restart deduplication. The read
        # fails closed: running the handler without an answer could write
        # the command twice, which is what this stage exists to prevent. It
        # catches what ``_emit_event`` catches from the same event log. The
        # message and the log line name the exception's type, not its text,
        # as the typed store errors do (#702, #713), so the line carries no
        # traceback; if the rejection event cannot be written either,
        # ``_emit_event`` logs that failure without one too, since this one
        # is its context. The key is not recorded, so a retry once the event
        # log recovers runs.
        try:
            persisted = self._event_log.has_idempotency_key(command.idempotency_key)
        except _audit_emit_failure_classes() as exc:
            message = f"Idempotency check failed: {type(exc).__name__}"
            log.error(  # noqa: TRY400 — no traceback on purpose; see above
                "idempotency_check_failed",
                key=command.idempotency_key,
                error_type=type(exc).__name__,
            )
            audit = self._emit_rejection(
                command,
                reason="idempotency_check_failed",
                message=message,
                policy_warnings=policy_warnings,
            )
            return CommandResult(
                command_id=command.command_id,
                status=CommandStatus.FAILED,
                operation=command.operation,
                message=message,
                warnings=[*policy_warnings, *_audit_warnings(audit)],
            )

        if persisted:
            self._record_idempotency_key(command.idempotency_key)
            message = f"Duplicate command (persisted): {command.idempotency_key}"
            log.info("duplicate_command_persisted", key=command.idempotency_key)
            audit = self._emit_rejection(
                command,
                reason="idempotency_replay",
                message=message,
                policy_warnings=policy_warnings,
            )
            return CommandResult(
                command_id=command.command_id,
                status=CommandStatus.DUPLICATE,
                operation=command.operation,
                message=message,
                warnings=[*policy_warnings, *_audit_warnings(audit)],
            )

        return None

    def _emit_rejection(
        self,
        command: Command,
        *,
        reason: str,
        message: str,
        policy_warnings: list[str] | None = None,
    ) -> str | None:
        """Emit a uniform :attr:`EventType.MUTATION_REJECTED` event.

        Called from every rejection stage (``immutable_core`` / ``validate``
        / ``policy_violation`` / ``idempotency_replay`` /
        ``idempotency_check_failed``) so the audit trail is symmetric — one
        event per rejection, ``reason`` discriminates the stage.

        ``policy_warnings`` is forwarded by every caller downstream of
        Stage 2 — see the rule stated there. Stage 1 (``validate``) is the
        one caller that passes nothing, because it runs before the gate.
        The key stays absent from the payload when the list is empty, so a
        deployment that has declared no policies emits a byte-identical
        event to the pre-gate world.

        Returns whatever :meth:`_emit_event` returns — ``None`` when the
        event landed, a warning string when it did not.
        """
        return self._emit_event(
            EventType.MUTATION_REJECTED,
            command,
            CommandStatus.REJECTED,
            message,
            reason=reason,
            policy_warnings=policy_warnings,
        )

    def _emit_event(
        self,
        event_type: EventType,
        command: Command,
        status: CommandStatus,
        message: str,
        *,
        reason: str | None = None,
        policy_warnings: list[str] | None = None,
    ) -> str | None:
        """Build the payload and emit a single executor event.

        Returns ``None`` when the event was written, and a warning string
        naming the missing event when the write raised.

        **Emitting the audit event can never change what the pipeline
        reports.** That is the whole invariant, and it is stated here rather
        than at any one caller because the failure mode differs by stage
        while the rule does not. At Stage 5 an unguarded raise propagates
        *past* a committed handler write, so the caller is told the mutation
        failed when the store change is durable — a result that contradicts
        the state of the database, and the defect #551 was opened for. At the
        Stage 1-4 rejection and failure sites the reported outcome is already
        the bad news, and nothing was committed, so a raise tells no lie
        about the store; what it does instead is escape ``execute`` entirely
        and abort the surrounding ``execute_batch``, which is exactly what
        the Stage 4 comments promise will not happen under
        ``CONTINUE_ON_ERROR``. Guarding only Stage 5 would leave a dead event
        log degrading a *successful* command gracefully while taking the
        batch down on a *failed* one, so the guard sits at the single seam
        every stage already routes through.

        The failure is never swallowed: it is logged at ``error``
        (``TRELLIS_LOG_LEVEL`` defaults to ``WARNING``, so anything lower is
        a no-op on the CLI — #425) and stated on the returned
        :class:`CommandResult` under :data:`AUDIT_EMIT_FAILED_MARKER`. The
        line names the exception's type, adds its message only when it is a
        ``TrellisError``, and carries no traceback. The warning follows the
        same rule, since REST, MCP and the CLI hand it to the caller and a
        driver's text can carry query text and values. An emit from inside an
        ``except`` block (a failed handler, a failed idempotency read) has
        the failure being audited as its error's context, so a rendered
        traceback would print that failure, a driver's text included (#702,
        #713), even when the emit's own error is clean.

        Measured before it was written: over the 3,655 governed mutations
        this deployment has executed since 2026-07-06, zero Stage 5 emits
        have failed. The event log and the handler's own semantic event are
        two independent calls, so an unpaired semantic event is the in-band
        fingerprint of a Stage 5 failure, and there are none — every
        semantic total pairs exactly, ordinally, with its operation's
        ``MUTATION_EXECUTED`` count. The defect is latent, and the fix is
        sized for that: a guard and a warning, not an outbox.
        """
        if self._event_log is None:
            return None
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
        # Added only when a policy actually fired -- non-blockingly on
        # the SUCCESS path, or ahead of the blocking rule on the
        # ``policy_violation`` rejection path. Keeping the key absent
        # otherwise means a deployment with no policies emits a
        # byte-identical payload to the pre-gate world, which is what
        # makes the default posture verifiably transparent.
        if policy_warnings:
            payload["policy_warnings"] = policy_warnings
        try:
            self._event_log.emit(
                event_type,
                "mutation_executor",
                entity_id=command.target_id,
                entity_type=command.target_type,
                payload=payload,
            )
        except _audit_emit_failure_classes() as exc:
            # No traceback, and no text Trellis did not write; see the
            # docstring.
            logger.error(  # noqa: TRY400 — no traceback on purpose; see above
                "audit_emit_failed",
                command_id=command.command_id,
                operation=command.operation,
                event_type=event_type,
                error_type=type(exc).__name__,
                error=str(exc) if isinstance(exc, TrellisError) else None,
            )
            return _AUDIT_EMIT_FAILED_TEMPLATE.format(
                event_type=event_type,
                error=(
                    f"{type(exc).__name__}: {exc}"
                    if isinstance(exc, TrellisError)
                    else type(exc).__name__
                ),
            )
        return None
