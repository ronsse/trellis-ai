"""Tests for MutationExecutor — the governed write pipeline."""

from __future__ import annotations

from unittest.mock import MagicMock

from trellis.mutate.commands import (
    BatchStrategy,
    Command,
    CommandBatch,
    CommandStatus,
    Operation,
)
from trellis.mutate.executor import MutationExecutor


def _cmd(
    op: Operation = Operation.ENTITY_CREATE,
    args: dict | None = None,
    **kwargs,
) -> Command:
    args = args or {"entity_type": "service", "name": "auth"}
    return Command(operation=op, args=args, **kwargs)


def _handler(created_id: str | None = None, message: str = "ok") -> MagicMock:
    h = MagicMock()
    h.handle.return_value = (created_id, message)
    return h


class TestMutationExecutor:
    def test_successful_execution(self) -> None:
        handler = _handler(created_id="ent_1")
        executor = MutationExecutor(
            handlers={Operation.ENTITY_CREATE: handler},
        )
        result = executor.execute(_cmd())
        assert result.status == CommandStatus.SUCCESS
        assert result.created_id == "ent_1"
        handler.handle.assert_called_once()

    def test_validation_failure(self) -> None:
        executor = MutationExecutor()
        # missing required args
        cmd = Command(operation=Operation.ENTITY_CREATE, args={})
        result = executor.execute(cmd)
        assert result.status == CommandStatus.FAILED
        assert "Validation failed" in result.message

    def test_policy_rejection(self) -> None:
        gate = MagicMock()
        gate.check.return_value = (
            False,
            "Approval required",
            ["needs manager approval"],
        )
        executor = MutationExecutor(
            policy_gate=gate,
            handlers={Operation.ENTITY_CREATE: _handler()},
        )
        result = executor.execute(_cmd())
        assert result.status == CommandStatus.REJECTED
        assert "Approval required" in result.message

    def test_policy_allows(self) -> None:
        gate = MagicMock()
        gate.check.return_value = (True, "", [])
        executor = MutationExecutor(
            policy_gate=gate,
            handlers={Operation.ENTITY_CREATE: _handler()},
        )
        result = executor.execute(_cmd())
        assert result.status == CommandStatus.SUCCESS

    def test_idempotency_duplicate(self) -> None:
        executor = MutationExecutor(
            handlers={Operation.ENTITY_CREATE: _handler()},
        )
        cmd1 = _cmd(idempotency_key="key-1")
        cmd2 = _cmd(idempotency_key="key-1")
        r1 = executor.execute(cmd1)
        r2 = executor.execute(cmd2)
        assert r1.status == CommandStatus.SUCCESS
        assert r2.status == CommandStatus.DUPLICATE

    def test_idempotency_different_keys(self) -> None:
        executor = MutationExecutor(
            handlers={Operation.ENTITY_CREATE: _handler()},
        )
        r1 = executor.execute(_cmd(idempotency_key="k1"))
        r2 = executor.execute(_cmd(idempotency_key="k2"))
        assert r1.status == CommandStatus.SUCCESS
        assert r2.status == CommandStatus.SUCCESS

    def test_no_handler_fails(self) -> None:
        executor = MutationExecutor()  # no handlers
        result = executor.execute(_cmd())
        assert result.status == CommandStatus.FAILED
        assert "No handler" in result.message

    def test_handler_exception(self) -> None:
        handler = MagicMock()
        handler.handle.side_effect = RuntimeError("DB error")
        executor = MutationExecutor(
            handlers={Operation.ENTITY_CREATE: handler},
        )
        result = executor.execute(_cmd())
        assert result.status == CommandStatus.FAILED
        assert "DB error" in result.message

    def test_emits_event_on_success(self) -> None:
        event_log = MagicMock()
        executor = MutationExecutor(
            event_log=event_log,
            handlers={Operation.ENTITY_CREATE: _handler()},
        )
        executor.execute(_cmd())
        event_log.emit.assert_called_once()
        call_args = event_log.emit.call_args
        assert call_args[0][0].value == "mutation.executed"

    def test_emits_event_on_rejection(self) -> None:
        event_log = MagicMock()
        gate = MagicMock()
        gate.check.return_value = (False, "denied", [])
        executor = MutationExecutor(
            event_log=event_log,
            policy_gate=gate,
            handlers={Operation.ENTITY_CREATE: _handler()},
        )
        executor.execute(_cmd())
        event_log.emit.assert_called_once()
        call_args = event_log.emit.call_args
        assert call_args[0][0].value == "mutation.rejected"

    def test_register_handler(self) -> None:
        executor = MutationExecutor()
        handler = _handler()
        executor.register_handler(Operation.ENTITY_CREATE, handler)
        result = executor.execute(_cmd())
        assert result.status == CommandStatus.SUCCESS


class TestBatchExecution:
    def test_batch_sequential(self) -> None:
        handler = _handler()
        executor = MutationExecutor(
            handlers={Operation.ENTITY_CREATE: handler},
        )
        batch = CommandBatch(
            commands=[_cmd(), _cmd()],
            strategy=BatchStrategy.SEQUENTIAL,
        )
        results = executor.execute_batch(batch)
        assert len(results) == 2
        assert all(r.status == CommandStatus.SUCCESS for r in results)

    def test_batch_stop_on_error(self) -> None:
        good_handler = _handler()
        executor = MutationExecutor(
            handlers={Operation.ENTITY_CREATE: good_handler},
        )
        batch = CommandBatch(
            commands=[
                _cmd(),
                # will fail validation
                Command(operation=Operation.ENTITY_CREATE, args={}),
                _cmd(),  # should not execute
            ],
            strategy=BatchStrategy.STOP_ON_ERROR,
        )
        results = executor.execute_batch(batch)
        assert len(results) == 2  # stopped after failure
        assert results[0].status == CommandStatus.SUCCESS
        assert results[1].status == CommandStatus.FAILED

    def test_batch_continue_on_error(self) -> None:
        good_handler = _handler()
        executor = MutationExecutor(
            handlers={Operation.ENTITY_CREATE: good_handler},
        )
        batch = CommandBatch(
            commands=[
                _cmd(),
                # will fail validation
                Command(operation=Operation.ENTITY_CREATE, args={}),
                _cmd(),
            ],
            strategy=BatchStrategy.CONTINUE_ON_ERROR,
        )
        results = executor.execute_batch(batch)
        assert len(results) == 3  # all attempted
        assert results[0].status == CommandStatus.SUCCESS
        assert results[1].status == CommandStatus.FAILED
        assert results[2].status == CommandStatus.SUCCESS
