"""C2 Phase 6 — narrowed executor catches.

The pre-Phase-6 ``except Exception`` at ``executor.py:190`` collapsed every
handler failure into a single FAILED CommandResult shape. This phase
splits the typed Trellis exceptions into their own branches so the
EventLog and the CommandResult both carry the right reason.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from trellis.errors import (
    IdempotencyError,
    PolicyViolationError,
    StoreError,
    TrellisError,
    ValidationError,
)
from trellis.mutate.commands import Command, CommandStatus, Operation
from trellis.mutate.executor import MutationExecutor


def _cmd(**kwargs: object) -> Command:
    return Command(
        operation=Operation.ENTITY_CREATE,
        args={"entity_type": "service", "name": "auth"},
        **kwargs,  # type: ignore[arg-type]
    )


def _handler_raising(exc: BaseException) -> MagicMock:
    h = MagicMock()
    h.handle.side_effect = exc
    return h


class TestExecutorTypedExceptionRouting:
    def test_policy_violation_routes_to_rejected_with_policy_reason(self) -> None:
        event_log = MagicMock()
        handler = _handler_raising(
            PolicyViolationError("post-fetch deny", policy_id="data_class_pii")
        )
        executor = MutationExecutor(
            event_log=event_log,
            handlers={Operation.ENTITY_CREATE: handler},
        )
        result = executor.execute(_cmd())
        assert result.status == CommandStatus.REJECTED
        payload = event_log.emit.call_args.kwargs["payload"]
        assert payload["reason"] == "policy_violation"

    def test_idempotency_error_routes_to_duplicate(self) -> None:
        event_log = MagicMock()
        executor = MutationExecutor(
            event_log=event_log,
            handlers={
                Operation.ENTITY_CREATE: _handler_raising(
                    IdempotencyError(idempotency_key="repeat-key")
                ),
            },
        )
        result = executor.execute(_cmd())
        assert result.status == CommandStatus.DUPLICATE
        payload = event_log.emit.call_args.kwargs["payload"]
        assert payload["reason"] == "idempotency_replay"

    def test_store_error_routes_to_failed_with_store_logged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from trellis.mutate import executor as executor_module

        log_events: list[tuple[str, dict]] = []

        def _exception(event: str, **kw: object) -> None:
            log_events.append((event, dict(kw)))

        # Bind on the module-level logger; the executor reads via
        # logger.bind() inside execute() but the underlying handler
        # is the same structlog instance.
        bound_logger = executor_module.logger.bind()
        monkeypatch.setattr(bound_logger, "exception", _exception)
        # Patch logger.bind to return our bound stub so .exception is
        # captured.
        monkeypatch.setattr(executor_module.logger, "bind", lambda **_: bound_logger)

        event_log = MagicMock()
        executor = MutationExecutor(
            event_log=event_log,
            handlers={
                Operation.ENTITY_CREATE: _handler_raising(
                    StoreError("connection refused", store="postgres")
                ),
            },
        )
        result = executor.execute(_cmd())
        assert result.status == CommandStatus.FAILED
        # The typed error path is observable in structured logs.
        store_events = [kw for e, kw in log_events if e == "handler_typed_error"]
        assert store_events, "expected handler_typed_error to be logged"
        assert store_events[0]["store"] == "postgres"
        assert store_events[0]["error_type"] == "StoreError"

    def test_validation_error_unchanged(self) -> None:
        """Existing ValidationError path must continue to route as REJECTED
        (regression cover for the typed-routing refactor)."""
        event_log = MagicMock()
        executor = MutationExecutor(
            event_log=event_log,
            handlers={
                Operation.ENTITY_CREATE: _handler_raising(
                    ValidationError("bad arg", code="orphan_edge")
                ),
            },
        )
        result = executor.execute(_cmd())
        assert result.status == CommandStatus.REJECTED
        payload = event_log.emit.call_args.kwargs["payload"]
        assert payload["reason"] == "orphan_edge"


class TestExecutorUnexpectedException:
    """Unexpected exceptions are logged with ``exc_info=True`` and the
    audit event is still emitted — but the FAILED CommandResult shape
    is preserved so ``execute_batch`` keeps working under
    SEQUENTIAL / CONTINUE_ON_ERROR."""

    def test_runtime_error_yields_failed_result_and_emits_event(self) -> None:
        event_log = MagicMock()
        executor = MutationExecutor(
            event_log=event_log,
            handlers={
                Operation.ENTITY_CREATE: _handler_raising(
                    RuntimeError("backend panic")
                ),
            },
        )
        result = executor.execute(_cmd())
        assert result.status == CommandStatus.FAILED
        assert "backend panic" in result.message
        # FAILED audit event written
        event_type = event_log.emit.call_args.args[0]
        assert event_type.value == "mutation.rejected"  # FAILED maps to REJECTED type

    def test_truly_unexpected_exception_propagates(self) -> None:
        """A non-listed exception type (e.g., SystemExit) must propagate
        rather than be silently caught. This guards against the narrowed
        catch growing back into a bare ``Exception``."""
        executor = MutationExecutor(
            handlers={
                Operation.ENTITY_CREATE: _handler_raising(SystemExit("ctrl-c")),
            },
        )
        with pytest.raises(SystemExit):
            executor.execute(_cmd())

    def test_unrelated_trellis_error_routes_to_failed(self) -> None:
        """A generic TrellisError (not Validation/Policy/Idempotency/Store)
        falls into the typed-error branch and emits FAILED."""
        event_log = MagicMock()
        executor = MutationExecutor(
            event_log=event_log,
            handlers={
                Operation.ENTITY_CREATE: _handler_raising(
                    TrellisError("generic typed error")
                ),
            },
        )
        result = executor.execute(_cmd())
        assert result.status == CommandStatus.FAILED
