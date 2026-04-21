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


class TestIdempotencyCacheEviction:
    """Gap 4.1 — FIFO eviction replaces silent .clear(); loud warning when
    eviction happens without an event_log backstop."""

    def test_rejects_zero_cache_size(self) -> None:
        import pytest

        with pytest.raises(ValueError, match="idempotency_cache_size must be >= 1"):
            MutationExecutor(idempotency_cache_size=0)

    def test_fifo_eviction_drops_oldest_key(self) -> None:
        executor = MutationExecutor(
            handlers={Operation.ENTITY_CREATE: _handler()},
            idempotency_cache_size=3,
        )
        for i in range(3):
            executor.execute(_cmd(idempotency_key=f"k{i}"))
        # Fourth key evicts k0 (the oldest)
        executor.execute(_cmd(idempotency_key="k3"))

        cache = executor._seen_idempotency_keys
        assert list(cache.keys()) == ["k1", "k2", "k3"]
        assert executor._idempotency_evictions == 1

    def test_recent_keys_still_detected_as_duplicates_after_eviction(self) -> None:
        executor = MutationExecutor(
            handlers={Operation.ENTITY_CREATE: _handler()},
            idempotency_cache_size=2,
        )
        executor.execute(_cmd(idempotency_key="old"))
        executor.execute(_cmd(idempotency_key="mid"))
        # Evicts "old"
        executor.execute(_cmd(idempotency_key="new"))

        # "mid" and "new" should still be detected as duplicates
        mid_result = executor.execute(_cmd(idempotency_key="mid"))
        new_result = executor.execute(_cmd(idempotency_key="new"))
        assert mid_result.status == CommandStatus.DUPLICATE
        assert new_result.status == CommandStatus.DUPLICATE

    def test_hot_key_refreshed_on_duplicate_hit(self) -> None:
        """move_to_end() keeps re-seen keys warm so they aren't evicted
        before truly-cold keys."""
        executor = MutationExecutor(
            handlers={Operation.ENTITY_CREATE: _handler()},
            idempotency_cache_size=2,
        )
        executor.execute(_cmd(idempotency_key="a"))
        executor.execute(_cmd(idempotency_key="b"))
        # Re-hit "a" — it becomes the newest; "b" is now oldest.
        executor.execute(_cmd(idempotency_key="a"))
        # Insert "c" — evicts "b", keeps "a".
        executor.execute(_cmd(idempotency_key="c"))

        assert list(executor._seen_idempotency_keys.keys()) == ["a", "c"]

    def test_eviction_without_event_log_emits_warning(self, monkeypatch) -> None:
        from trellis.mutate import executor as executor_module

        warn_calls: list[tuple[str, dict]] = []

        def _capture(event: str, **kw: object) -> None:
            warn_calls.append((event, kw))

        monkeypatch.setattr(executor_module.logger, "warning", _capture)

        executor = MutationExecutor(
            handlers={Operation.ENTITY_CREATE: _handler()},
            idempotency_cache_size=2,
        )
        executor.execute(_cmd(idempotency_key="k0"))
        executor.execute(_cmd(idempotency_key="k1"))
        executor.execute(_cmd(idempotency_key="k2"))  # evicts k0

        events = [e for e, _ in warn_calls]
        assert "idempotency_cache_evicted_without_event_log" in events
        payload = next(
            kw for e, kw in warn_calls
            if e == "idempotency_cache_evicted_without_event_log"
        )
        assert payload["evicted_key"] == "k0"
        assert payload["cache_size"] == 2
        assert payload["total_evictions"] == 1

    def test_eviction_with_event_log_no_warning(self, monkeypatch) -> None:
        """With event_log attached, eviction is safe (persisted check is
        authoritative) — no warning should fire."""
        from trellis.mutate import executor as executor_module

        warn_calls: list[tuple[str, dict]] = []

        def _capture(event: str, **kw: object) -> None:
            warn_calls.append((event, kw))

        monkeypatch.setattr(executor_module.logger, "warning", _capture)

        event_log = MagicMock()
        event_log.has_idempotency_key.return_value = False
        executor = MutationExecutor(
            event_log=event_log,
            handlers={Operation.ENTITY_CREATE: _handler()},
            idempotency_cache_size=2,
        )
        executor.execute(_cmd(idempotency_key="k0"))
        executor.execute(_cmd(idempotency_key="k1"))
        executor.execute(_cmd(idempotency_key="k2"))  # evicts k0, silently OK

        assert not any(
            e == "idempotency_cache_evicted_without_event_log" for e, _ in warn_calls
        )
        assert executor._idempotency_evictions == 1

    def test_evicted_key_caught_via_persisted_event_log(self) -> None:
        """The real safety property: once a key has been evicted from the
        in-memory cache, a retry of that command is still rejected because
        the event log has persisted it."""
        event_log = MagicMock()
        # Return True only for the evicted key, simulating that it was
        # persisted to the event log when originally executed.
        event_log.has_idempotency_key.side_effect = lambda k: k == "evicted"
        executor = MutationExecutor(
            event_log=event_log,
            handlers={Operation.ENTITY_CREATE: _handler()},
            idempotency_cache_size=2,
        )
        executor.execute(_cmd(idempotency_key="evicted"))
        executor.execute(_cmd(idempotency_key="fresh1"))
        executor.execute(_cmd(idempotency_key="fresh2"))  # evicts "evicted"

        # Retry of the evicted key — persisted check must catch it
        result = executor.execute(_cmd(idempotency_key="evicted"))
        assert result.status == CommandStatus.DUPLICATE
        assert "persisted" in result.message


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
