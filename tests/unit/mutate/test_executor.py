"""Tests for MutationExecutor — the governed write pipeline."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn
from unittest.mock import MagicMock

import pytest

from trellis.core.error_sanitize import SUPPRESSED_MARKER
from trellis.errors import PolicyViolationError, StoreError, ValidationError
from trellis.mutate import build_curate_executor
from trellis.mutate.commands import (
    BatchStrategy,
    Command,
    CommandBatch,
    CommandStatus,
    Operation,
)
from trellis.mutate.executor import AUDIT_EMIT_FAILED_MARKER, MutationExecutor
from trellis.schemas.enums import TraceSource
from trellis.schemas.trace import Trace, TraceContext
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry
from trellis.stores.sqlite.event_log import SQLiteEventLog


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


def _psycopg_shaped_cause(
    message: str, *, sqlstate: str, constraint: str | None = None
) -> RuntimeError:
    """A fake exception carrying psycopg.Error's ``sqlstate``/``diag`` shape.

    psycopg is an optional extra, unavailable in this environment;
    ``summarize_exception`` reads these attributes duck-typed (not via
    ``isinstance``), so a plain exception carrying them is read the same
    way production reads the real ``psycopg.Error``. Subclasses
    ``RuntimeError`` (not bare ``Exception``) so it is one of the
    executor's enumerated panic-catch types even without psycopg
    installed (``_optional_driver_panics``). Meant to be attached as a
    Trellis error's ``__cause__`` — production wraps a driver error as
    ``raise StoreError(...) from exc`` — never raised on its own.
    """

    class _Diag:
        def __init__(self, name: str) -> None:
            self.constraint_name = name

    class _FakePsycopgError(RuntimeError):
        pass

    exc = _FakePsycopgError(message)
    exc.sqlstate = sqlstate  # type: ignore[attr-defined]
    if constraint is not None:
        exc.diag = _Diag(constraint)  # type: ignore[attr-defined]
    return exc


def _raise_inner_driver_failure() -> NoReturn:
    inner_message = "inner driver failure"
    raise RuntimeError(inner_message)


def _raise_store_error_chained_by_context_only() -> NoReturn:
    """Raise a ``StoreError`` whose ``__context__`` (not ``__cause__``)
    is set: a bare ``raise`` inside an ``except`` block, the shape R2
    must not confuse with an explicit ``raise ... from exc`` chain.
    ``from None`` only silences the traceback printer's chaining note
    (``__suppress_context__``) to satisfy B904 — it does not clear
    ``__context__`` itself, which stays set to the inner exception."""
    try:
        _raise_inner_driver_failure()
    except RuntimeError:
        outer_message = "backend down"
        raise StoreError(outer_message, store="pg") from None


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
        assert result.status == CommandStatus.REJECTED
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
        assert result.message == "Execution failed: RuntimeError"

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
        assert call_args.kwargs["payload"]["reason"] == "policy_violation"

    def test_validate_rejection_emits_event(self) -> None:
        """Option A: validate-stage rejection emits exactly one
        MUTATION_REJECTED event with ``reason="validate"`` so the audit
        trail is symmetric across all three rejection stages."""
        event_log = MagicMock()
        executor = MutationExecutor(
            event_log=event_log,
            handlers={Operation.ENTITY_CREATE: _handler()},
        )
        # Missing required args triggers validate-stage rejection
        result = executor.execute(Command(operation=Operation.ENTITY_CREATE, args={}))

        assert result.status == CommandStatus.REJECTED
        event_log.emit.assert_called_once()
        event_type, source = event_log.emit.call_args.args
        assert event_type.value == "mutation.rejected"
        assert source == "mutation_executor"
        payload = event_log.emit.call_args.kwargs["payload"]
        assert payload["reason"] == "validate"
        assert payload["status"] == CommandStatus.REJECTED
        assert "Validation failed" in payload["message"]

    def test_idempotency_rejection_emits_event(self) -> None:
        """Option A: idempotency-stage rejection emits exactly one
        MUTATION_REJECTED event with ``reason="idempotency_replay"``."""
        event_log = MagicMock()
        # has_idempotency_key returns False so the in-memory cache path is
        # the one exercised by the second submission.
        event_log.has_idempotency_key.return_value = False
        executor = MutationExecutor(
            event_log=event_log,
            handlers={Operation.ENTITY_CREATE: _handler()},
        )
        executor.execute(_cmd(idempotency_key="dup"))
        event_log.emit.reset_mock()  # drop the SUCCESS emit from the first call

        result = executor.execute(_cmd(idempotency_key="dup"))

        assert result.status == CommandStatus.DUPLICATE
        event_log.emit.assert_called_once()
        event_type, _source = event_log.emit.call_args.args
        assert event_type.value == "mutation.rejected"
        payload = event_log.emit.call_args.kwargs["payload"]
        assert payload["reason"] == "idempotency_replay"
        assert payload["idempotency_key"] == "dup"

    def test_register_handler(self) -> None:
        executor = MutationExecutor()
        handler = _handler()
        executor.register_handler(Operation.ENTITY_CREATE, handler)
        result = executor.execute(_cmd())
        assert result.status == CommandStatus.SUCCESS

    def test_handler_raised_validation_error_routes_through_emit_rejection(
        self,
    ) -> None:
        """Variant A' from adr-extraction-validation.md §5.5: a handler that
        raises ``ValidationError`` is treated as a structured rejection — the
        executor emits ``MUTATION_REJECTED`` with ``reason=exc.code`` and
        returns ``CommandStatus.REJECTED`` (not FAILED).

        Chained from a psycopg-shaped cause (R2): production wraps a
        driver error the same way (``raise StoreError(...) from exc``),
        so this proves the rejection audit reads the chained cause's own
        code, message and constraint off ``__cause__`` generically — not
        only on the typed ``StoreError`` site below.
        """
        event_log = MagicMock()
        handler = MagicMock()
        exc = ValidationError(
            "FK check failed",
            errors=["source missing", "target missing"],
            code="orphan_edge",
        )
        exc.__cause__ = _psycopg_shaped_cause(
            'insert or update on table "edges" violates foreign key'
            ' constraint "edges_fk"',
            sqlstate="23503",
            constraint="edges_fk",
        )
        handler.handle.side_effect = exc
        executor = MutationExecutor(
            event_log=event_log,
            handlers={Operation.ENTITY_CREATE: handler},
        )
        result = executor.execute(_cmd())

        assert result.status == CommandStatus.REJECTED
        assert "FK check failed" in result.message

        event_log.emit.assert_called_once()
        event_type, source = event_log.emit.call_args.args
        assert event_type.value == "mutation.rejected"
        assert source == "mutation_executor"
        payload = event_log.emit.call_args.kwargs["payload"]
        assert payload["reason"] == "orphan_edge"
        assert payload["status"] == CommandStatus.REJECTED
        assert "FK check failed" in payload["message"]
        # The audit event carries the ValidationError's own type and code.
        assert payload["error_type"] == "ValidationError"
        assert payload["error_code"] == "orphan_edge"
        # ValidationError itself has no .diag, so no top-level constraint.
        assert "constraint" not in payload
        # The chained driver error's own code, message and constraint —
        # read one level off __cause__, never __context__ — land nested.
        cause = payload["cause"]
        assert cause["error_type"] == "_FakePsycopgError"
        assert cause["error_code"] == "23503"
        assert cause["constraint"] == "edges_fk"
        assert cause["message"] == (
            'insert or update on table "..." violates foreign key constraint "..."'
        )

    def test_handler_validation_error_without_explicit_code_uses_default(
        self,
    ) -> None:
        """When a handler raises ValidationError without a custom ``code``,
        the rejection event uses the conventional ``handler_validate``
        reason — preserves the audit-symmetry contract for handlers that
        haven't yet adopted explicit codes."""
        event_log = MagicMock()
        handler = MagicMock()
        handler.handle.side_effect = ValidationError("plain validation failure")
        executor = MutationExecutor(
            event_log=event_log,
            handlers={Operation.ENTITY_CREATE: handler},
        )
        result = executor.execute(_cmd())

        assert result.status == CommandStatus.REJECTED
        payload = event_log.emit.call_args.kwargs["payload"]
        assert payload["reason"] == "handler_validate"

    def test_handler_raised_store_error_audits_its_type_and_code(self) -> None:
        """A handler-raised ``StoreError`` is the typed ``(StoreError,
        TrellisError)`` catch, distinct from both the rejection catches
        above and the untyped-panic catch below: FAILED, not REJECTED,
        and the audit event carries the error's type and its (fixed)
        ``STORE_ERROR`` code. ``STORE_ERROR`` and ``"StoreError"`` are
        both constants a handwritten assertion could satisfy by accident,
        so this also asserts the ``message`` and ``constraint`` values,
        which a constant cannot."""
        event_log = MagicMock()
        handler = MagicMock()
        exc = StoreError("backend down", store="pg")
        exc.diag = type("Diag", (), {"constraint_name": "pg_conn_pool"})()  # type: ignore[attr-defined]
        handler.handle.side_effect = exc
        executor = MutationExecutor(
            event_log=event_log,
            handlers={Operation.ENTITY_CREATE: handler},
        )
        result = executor.execute(_cmd())

        assert result.status == CommandStatus.FAILED
        assert result.message == "Execution failed: backend down"
        payload = event_log.emit.call_args.kwargs["payload"]
        assert payload["message"] == "backend down"
        assert payload["error_type"] == "StoreError"
        assert payload["error_code"] == "STORE_ERROR"
        assert payload["constraint"] == "pg_conn_pool"
        assert "cause" not in payload  # StoreError here chains nothing.

    def test_a_store_error_s_wrapped_driver_cause_reaches_the_audit(self) -> None:
        """R2 acceptance test. Production wraps a driver error as
        ``raise StoreError(f"... failed: {type(exc).__name__}") from exc``
        so the type-only wrapper text stays clear of the driver's own
        text — which means the SQLSTATE, the driver's own message and the
        violated constraint are reachable only off ``__cause__``. This
        chains a ``StoreError`` from a psycopg-shaped Postgres deadlock
        (SQLSTATE 40P01) whose second line carries a synthetic marker,
        and asserts the marker never reaches the stored event while the
        cause's own code, message and constraint do. Fails before R2.
        """
        event_log = MagicMock()
        handler = MagicMock()
        marker = "SYN-DEADLOCK-DETAIL-7f3a"
        cause = _psycopg_shaped_cause(
            f"deadlock detected\nDETAIL: Process 123 waits for ShareLock on"
            f" transaction 456; blocked by {marker}.",
            sqlstate="40P01",
            constraint="orders_pkey",
        )
        exc = StoreError("Purge of node n1 failed: DeadlockDetected", store="graph")
        exc.__cause__ = cause
        handler.handle.side_effect = exc
        executor = MutationExecutor(
            event_log=event_log,
            handlers={Operation.ENTITY_CREATE: handler},
        )

        result = executor.execute(_cmd())

        # CommandResult.message is unchanged by R2 — still the wrapper's
        # own type-only text, never the cause's.
        assert result.status == CommandStatus.FAILED
        assert result.message == (
            "Execution failed: Purge of node n1 failed: DeadlockDetected"
        )
        payload = event_log.emit.call_args.kwargs["payload"]
        assert payload["message"] == "Purge of node n1 failed: DeadlockDetected"
        assert payload["error_type"] == "StoreError"
        assert payload["error_code"] == "STORE_ERROR"
        assert marker not in json.dumps(payload, default=str)
        cause_summary = payload["cause"]
        assert cause_summary["error_code"] == "40P01"
        assert cause_summary["message"] == "deadlock detected"
        assert cause_summary["constraint"] == "orders_pkey"

    def test_no_cause_key_when_the_exception_chains_nothing(self) -> None:
        event_log = MagicMock()
        handler = MagicMock()
        handler.handle.side_effect = StoreError("backend down", store="pg")
        executor = MutationExecutor(
            event_log=event_log,
            handlers={Operation.ENTITY_CREATE: handler},
        )
        executor.execute(_cmd())
        payload = event_log.emit.call_args.kwargs["payload"]
        assert "cause" not in payload

    def test_no_cause_key_when_only_context_is_set(self) -> None:
        """A bare ``raise`` inside an ``except`` block sets the implicit
        ``__context__`` but leaves ``__cause__`` ``None`` (the helper's
        ``from None`` only silences the traceback printer, per its own
        docstring) — R2 must read ``__cause__`` only, never fall back to
        the implicit chain."""
        event_log = MagicMock()
        handler = MagicMock()
        captured: list[StoreError] = []
        try:
            _raise_store_error_chained_by_context_only()
        except StoreError as caught:
            captured.append(caught)
        exc = captured[0]
        assert exc.__cause__ is None
        assert exc.__context__ is not None
        handler.handle.side_effect = exc
        executor = MutationExecutor(
            event_log=event_log,
            handlers={Operation.ENTITY_CREATE: handler},
        )

        executor.execute(_cmd())

        payload = event_log.emit.call_args.kwargs["payload"]
        assert "cause" not in payload

    def test_a_bare_assertion_error_still_produces_one_failed_audit_event(
        self,
    ) -> None:
        """S8 (R3): ``summarize_exception`` must not assume non-empty
        text. A bare ``AssertionError()`` (or any ``raise X()`` with no
        arguments) has ``str(exc) == ""``, so ``text.splitlines()`` is
        ``[]`` — an unguarded ``[0]`` index raises ``IndexError`` out of
        the panic catch, and the mutant that drops the guard produces
        zero audit events instead of one FAILED rejection."""
        event_log = MagicMock()
        handler = MagicMock()
        handler.handle.side_effect = AssertionError()
        executor = MutationExecutor(
            event_log=event_log,
            handlers={Operation.ENTITY_CREATE: handler},
        )

        result = executor.execute(_cmd())

        assert result.status == CommandStatus.FAILED
        event_log.emit.assert_called_once()
        event_type, _source = event_log.emit.call_args.args
        assert event_type.value == "mutation.rejected"

    def test_a_dsn_in_a_handler_raised_validation_error_is_suppressed(
        self,
    ) -> None:
        """S12 + E1 (R3): the ValidationError-rejection path's audit
        message must be built from ``summarize_exception``'s sanitized
        summary, not ``str(exc)`` directly — a credential-bearing
        message must reach the stored event as ``SUPPRESSED_MARKER``,
        never verbatim."""
        event_log = MagicMock()
        handler = MagicMock()
        handler.handle.side_effect = ValidationError(
            "connect to postgresql://svc_user:s3cr3t@db.internal/trellis failed"
        )
        executor = MutationExecutor(
            event_log=event_log,
            handlers={Operation.ENTITY_CREATE: handler},
        )

        executor.execute(_cmd())

        payload = event_log.emit.call_args.kwargs["payload"]
        assert payload["message"] == SUPPRESSED_MARKER

    def test_a_dsn_in_a_handler_raised_store_error_is_suppressed(self) -> None:
        """S12 + E2 (R3): the typed ``StoreError``/``TrellisError`` path's
        audit message must also go through the sanitize layer."""
        event_log = MagicMock()
        handler = MagicMock()
        handler.handle.side_effect = StoreError(
            "connect to postgresql://svc_user:s3cr3t@db.internal/trellis failed",
            store="pg",
        )
        executor = MutationExecutor(
            event_log=event_log,
            handlers={Operation.ENTITY_CREATE: handler},
        )

        executor.execute(_cmd())

        payload = event_log.emit.call_args.kwargs["payload"]
        assert payload["message"] == SUPPRESSED_MARKER


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
            kw
            for e, kw in warn_calls
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

    def test_a_persisted_hit_enters_the_cache(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A key the event log answers for is cached, so the log is asked once.

        A fresh executor starts with an empty cache, so its first replay of
        a key an earlier executor ran is answered from the event log. The
        next replay is answered from the cache.
        """
        log = SQLiteEventLog(tmp_path / "events.db")
        earlier = MutationExecutor(
            event_log=log, handlers={Operation.ENTITY_CREATE: _handler()}
        )
        ran = earlier.execute(_cmd(idempotency_key="syn-key"))
        assert ran.status == CommandStatus.SUCCESS
        asked: list[str] = []
        has_key = log.has_idempotency_key

        def counting_has_key(key: str) -> bool:
            asked.append(key)
            return has_key(key)

        monkeypatch.setattr(log, "has_idempotency_key", counting_has_key)
        handler = _handler()
        fresh = MutationExecutor(
            event_log=log, handlers={Operation.ENTITY_CREATE: handler}
        )

        first = fresh.execute(_cmd(idempotency_key="syn-key"))
        second = fresh.execute(_cmd(idempotency_key="syn-key"))

        assert (first.status, first.message) == (
            CommandStatus.DUPLICATE,
            "Duplicate command (persisted): syn-key",
        )
        assert (second.status, second.message) == (
            CommandStatus.DUPLICATE,
            "Duplicate command: syn-key",
        )
        assert asked == ["syn-key"]
        handler.handle.assert_not_called()


class _RefusesSynBad:
    """Raises ``exc`` for the command named ``syn-bad``; succeeds otherwise.

    Records every name it was asked to handle, so a test can tell a
    command that ran from one answered DUPLICATE before reaching it.
    """

    def __init__(self, exc: Exception) -> None:
        self.exc = exc
        self.names: list[str] = []

    def handle(self, command: Command) -> tuple[str | None, str]:
        name = command.args["name"]
        self.names.append(name)
        if name == "syn-bad":
            raise self.exc
        return f"ent-{name}", "ok"


def _named(name: str, key: str) -> Command:
    return _cmd(args={"entity_type": "service", "name": name}, idempotency_key=key)


class TestIdempotencyKeyRecordedOnlyOnSuccess:
    """Only a command whose handler succeeded makes its key a duplicate.

    A command the handler refuses (REJECTED) or fails on (FAILED) leaves
    its key free, so a corrected retry under the same key runs on the
    same executor, as it does on a fresh one.
    """

    @pytest.mark.parametrize(
        ("exc", "first_status"),
        [
            pytest.param(
                ValidationError("syn refused", code="syn_refused"),
                CommandStatus.REJECTED,
                id="validation-rejected",
            ),
            pytest.param(
                PolicyViolationError("syn denied", policy_id="syn-policy"),
                CommandStatus.REJECTED,
                id="policy-rejected",
            ),
            pytest.param(
                StoreError("syn store down", store="syn"),
                CommandStatus.FAILED,
                id="store-failed",
            ),
            pytest.param(
                RuntimeError("syn panic"),
                CommandStatus.FAILED,
                id="untyped-failed",
            ),
            pytest.param(
                sqlite3.IntegrityError("syn constraint"),
                CommandStatus.FAILED,
                id="sqlite-failed",
            ),
        ],
    )
    def test_corrected_retry_under_the_same_key_runs(
        self,
        tmp_path: Path,
        exc: Exception,
        first_status: CommandStatus,
    ) -> None:
        log = SQLiteEventLog(tmp_path / "events.db")
        handler = _RefusesSynBad(exc)
        executor = MutationExecutor(
            event_log=log,
            handlers={Operation.ENTITY_CREATE: handler},
        )

        first = executor.execute(_named("syn-bad", "syn-key-1"))
        assert first.status == first_status
        # The persisted check counts only executed mutations, so the
        # refusal's own audit event does not make the key a duplicate.
        assert log.has_idempotency_key("syn-key-1") is False

        retry = executor.execute(_named("syn-fixed", "syn-key-1"))
        assert retry.status == CommandStatus.SUCCESS, retry.message
        assert retry.created_id == "ent-syn-fixed"
        assert handler.names == ["syn-bad", "syn-fixed"]


def _store_a_corrupt_row(path: Path) -> Callable[[], None]:
    """Make the persisted read raise a real ``sqlite3.OperationalError``.

    The read's ``json_extract`` raises on a ``mutation.executed`` row whose
    payload is not JSON, for any key it has not matched before reaching the
    row. Appends still work. Returns the step that deletes the row.
    """
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO events (event_id, event_type, source, occurred_at,"
        " recorded_at, payload_json) VALUES ('syn-corrupt', 'mutation.executed',"
        " 'syn', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00',"
        " 'not json')"
    )
    conn.commit()

    def recover() -> None:
        conn.execute("DELETE FROM events WHERE event_id = 'syn-corrupt'")
        conn.commit()
        conn.close()

    return recover


class TestPersistedIdempotencyReadFails:
    """An event log that cannot answer Stage 3's read fails the command closed.

    Running the handler without the answer could write a command twice,
    which is what Stage 3 exists to prevent. So the result is FAILED and the
    handler does not run. The key is not recorded, so the same command runs
    once the log can be read again.
    """

    def test_the_command_fails_closed_and_runs_once_the_log_recovers(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "events.db"
        log = SQLiteEventLog(path)
        handler = _handler(created_id="syn-node-1")
        executor = MutationExecutor(
            event_log=log, handlers={Operation.ENTITY_CREATE: handler}
        )
        earlier = _named("syn-earlier", "syn-key-earlier")
        assert executor.execute(earlier).status == CommandStatus.SUCCESS
        command = _named("syn-entity", "syn-key-read")
        recover = _store_a_corrupt_row(path)

        result = executor.execute(command)

        # The CommandResult names the exception's type only; the real
        # driver text ("malformed JSON" from json_extract, confirmed live)
        # reaches only the persisted audit event, as a summary.
        result_expected = "Idempotency check failed: OperationalError"
        assert (result.status, result.message, result.warnings) == (
            CommandStatus.FAILED,
            result_expected,
            [],
        )
        assert handler.handle.call_count == 1  # the earlier command's call
        rejected = log.get_events(event_type=EventType.MUTATION_REJECTED)
        audit_expected = "Idempotency check failed: malformed JSON"
        assert [
            (
                e.payload["command_id"],
                e.payload["reason"],
                e.payload["message"],
                e.payload["error_type"],
                e.payload["error_code"],
            )
            for e in rejected
        ] == [
            (
                command.command_id,
                "idempotency_check_failed",
                audit_expected,
                "OperationalError",
                "SQLITE_ERROR",
            )
        ]

        recover()
        retry = executor.execute(command)

        assert retry.status == CommandStatus.SUCCESS, retry.message
        assert handler.handle.call_count == 2
        executed = log.get_events(event_type=EventType.MUTATION_EXECUTED)
        assert [e.payload["command_id"] for e in executed] == [
            earlier.command_id,
            command.command_id,
        ]

    def test_the_driver_s_own_text_reaches_only_the_audit_event(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """R1 acceptance test: fails at head. ``has_idempotency_key``
        raising ``sqlite3.OperationalError("database is locked")`` must
        store that text (not just ``OperationalError``) on the audit
        event, while ``CommandResult.message`` stays type-only."""
        log = SQLiteEventLog(tmp_path / "events.db")
        handler = _handler(created_id="syn-node-1")
        executor = MutationExecutor(
            event_log=log, handlers={Operation.ENTITY_CREATE: handler}
        )
        command = _named("syn-entity", "syn-key-locked")

        def refuse(*args: object) -> bool:
            locked_message = "database is locked"
            raise sqlite3.OperationalError(locked_message)

        monkeypatch.setattr(log, "has_idempotency_key", refuse)

        result = executor.execute(command)

        assert (result.status, result.message) == (
            CommandStatus.FAILED,
            "Idempotency check failed: OperationalError",
        )
        rejected = log.get_events(event_type=EventType.MUTATION_REJECTED)
        assert len(rejected) == 1
        assert rejected[0].payload["message"] == (
            "Idempotency check failed: database is locked"
        )
        assert rejected[0].payload["error_type"] == "OperationalError"

    def test_a_chained_store_error_s_cause_reaches_the_idempotency_audit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """R1 + R2: a Postgres event log wraps every driver error as a
        ``StoreError`` (``stores/postgres/event_log.py``), so the
        idempotency-read failure the owner asked to be summarized is, in
        production, this chained shape rather than a raw driver
        exception."""
        log = SQLiteEventLog(tmp_path / "events.db")
        handler = _handler(created_id="syn-node-1")
        executor = MutationExecutor(
            event_log=log, handlers={Operation.ENTITY_CREATE: handler}
        )
        command = _named("syn-entity", "syn-key-chained")

        cause = _psycopg_shaped_cause(
            "could not serialize access due to concurrent update",
            sqlstate="40001",
            constraint=None,
        )
        wrapped = StoreError("Idempotency check failed: OperationalError", store="pg")
        wrapped.__cause__ = cause

        def refuse(*args: object) -> bool:
            raise wrapped

        monkeypatch.setattr(log, "has_idempotency_key", refuse)

        result = executor.execute(command)

        assert result.status == CommandStatus.FAILED
        rejected = log.get_events(event_type=EventType.MUTATION_REJECTED)
        assert len(rejected) == 1
        cause_summary = rejected[0].payload["cause"]
        assert cause_summary["error_code"] == "40001"
        assert cause_summary["message"] == (
            "could not serialize access due to concurrent update"
        )

    def test_an_unwritable_log_fails_closed_with_the_audit_warning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the rejection event cannot be written either, the result says so."""
        log = SQLiteEventLog(tmp_path / "events.db")
        handler = _handler(created_id="syn-node-1")
        executor = MutationExecutor(
            event_log=log, handlers={Operation.ENTITY_CREATE: handler}
        )
        earlier = _named("syn-earlier", "syn-key-earlier")
        assert executor.execute(earlier).status == CommandStatus.SUCCESS
        command = _named("syn-entity", "syn-key-gone")

        def refuse(*args: object) -> None:
            msg = "syn event log refused"
            raise sqlite3.OperationalError(msg)

        monkeypatch.setattr(log, "has_idempotency_key", refuse)
        monkeypatch.setattr(log, "append", refuse)

        result = executor.execute(command)

        assert (result.status, result.message) == (
            CommandStatus.FAILED,
            "Idempotency check failed: OperationalError",
        )
        assert [w.split(":")[0] for w in result.warnings] == [AUDIT_EMIT_FAILED_MARKER]
        assert handler.handle.call_count == 1  # the earlier command's call

        monkeypatch.undo()
        retry = executor.execute(command)

        assert retry.status == CommandStatus.SUCCESS, retry.message
        assert handler.handle.call_count == 2
        executed = log.get_events(event_type=EventType.MUTATION_EXECUTED)
        assert [e.payload["command_id"] for e in executed] == [
            earlier.command_id,
            command.command_id,
        ]


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
                # will be refused by validation
                Command(operation=Operation.ENTITY_CREATE, args={}),
                _cmd(),  # should not execute
            ],
            strategy=BatchStrategy.STOP_ON_ERROR,
        )
        results = executor.execute_batch(batch)
        assert len(results) == 2  # stopped after the refusal
        assert results[0].status == CommandStatus.SUCCESS
        assert results[1].status == CommandStatus.REJECTED

    @pytest.mark.parametrize("source", ["policy_gate", "handler"])
    def test_batch_stop_on_error_stops_on_a_rejection(self, source: str) -> None:
        handler = _handler()
        gate = MagicMock()
        gate.check.return_value = (True, "", [])
        if source == "policy_gate":
            gate.check.side_effect = [
                (True, "", []),
                (False, "denied", ["frozen"]),
                (True, "", []),
            ]
        else:
            handler.handle.side_effect = [
                ("id-1", "ok"),
                ValidationError("refused", code="test_refusal"),
                ("id-3", "ok"),
            ]
        executor = MutationExecutor(
            policy_gate=gate,
            handlers={Operation.ENTITY_CREATE: handler},
        )
        batch = CommandBatch(
            commands=[_cmd(), _cmd(), _cmd()],
            strategy=BatchStrategy.STOP_ON_ERROR,
        )
        results = executor.execute_batch(batch)
        assert [r.status for r in results] == [
            CommandStatus.SUCCESS,
            CommandStatus.REJECTED,
        ]
        assert handler.handle.call_count == (1 if source == "policy_gate" else 2)

    def test_batch_continue_on_error(self) -> None:
        good_handler = _handler()
        executor = MutationExecutor(
            handlers={Operation.ENTITY_CREATE: good_handler},
        )
        batch = CommandBatch(
            commands=[
                _cmd(),
                # will be refused by validation
                Command(operation=Operation.ENTITY_CREATE, args={}),
                _cmd(),
            ],
            strategy=BatchStrategy.CONTINUE_ON_ERROR,
        )
        results = executor.execute_batch(batch)
        assert len(results) == 3  # all attempted
        assert results[0].status == CommandStatus.SUCCESS
        assert results[1].status == CommandStatus.REJECTED
        assert results[2].status == CommandStatus.SUCCESS


class TestBuildCurateExecutor:
    """The ``build_curate_executor`` factory wires every default handler."""

    def test_round_trips_a_trace_through_the_pipeline(self, tmp_path: Path) -> None:
        stores_dir = tmp_path / "stores"
        stores_dir.mkdir()
        registry = StoreRegistry(stores_dir=stores_dir)
        executor = build_curate_executor(registry)
        trace = Trace(
            source=TraceSource.AGENT,
            intent="round-trip",
            steps=[],
            context=TraceContext(agent_id="a", domain="d"),
        )
        result = executor.execute(
            Command(
                operation=Operation.TRACE_INGEST,
                args={"trace": trace},
                target_id=trace.trace_id,
                target_type="trace",
            )
        )
        assert result.status == CommandStatus.SUCCESS
        assert result.created_id == trace.trace_id
        assert registry.operational.trace_store.get(trace.trace_id) is not None


class TestSQLiteDriverError:
    """A ``sqlite3.Error`` a SQLite store lets out of a handler is FAILED.

    The SQLite stores, the default for every store, raise driver errors
    unmapped on most write paths. ``query_only`` makes the graph store's
    connection refuse writes, so the handler raises a real
    ``sqlite3.OperationalError`` from inside the store.
    """

    def test_a_refused_write_is_failed_and_audited_once(self, tmp_path: Path) -> None:
        stores_dir = tmp_path / "stores"
        stores_dir.mkdir()
        registry = StoreRegistry(stores_dir=stores_dir)
        registry.knowledge.graph_store._conn.execute("PRAGMA query_only = ON")
        command = _cmd(args={"entity_type": "service", "name": "syn-entity"})

        result = build_curate_executor(registry).execute(command)

        assert result.status == CommandStatus.FAILED, result.message
        events = registry.operational.event_log
        rejected = events.get_events(event_type=EventType.MUTATION_REJECTED)
        assert [(e.payload["command_id"], e.payload["status"]) for e in rejected] == [
            (command.command_id, "failed")
        ]
        assert events.get_events(event_type=EventType.MUTATION_EXECUTED) == []
        # The caller reads the driver error's type and the audit its exact
        # text: no SQL statement, path or traceback rides along in either.
        driver_text = "attempt to write a readonly database"
        assert result.message == "Execution failed: OperationalError"
        assert rejected[0].payload["message"] == driver_text
        # The audit event also carries the error's type and the driver's
        # own structured code as separate fields, so a reader can filter
        # or group on them without parsing ``message``.
        assert rejected[0].payload["error_type"] == "OperationalError"
        assert rejected[0].payload["error_code"] == "SQLITE_READONLY"

    def test_a_multiline_driver_error_s_audit_message_keeps_only_the_first_line(
        self, tmp_path: Path
    ) -> None:
        """A driver error's continuation line can quote a row value; the
        audit ``message`` must not carry it past the first line.

        Synthesizes the shape a Postgres unique-violation renders as: a
        clean first line, then a ``DETAIL:`` line quoting the offending
        row value, then a ``CONTEXT:`` line. This fails on base/main,
        where the panic-path catch audits ``str(exc)`` whole — the
        quoted value and both continuation lines reach the event.
        """
        log = SQLiteEventLog(tmp_path / "events.db")
        driver_text = (
            "value too long for type character varying(50)\n"
            "DETAIL:  Key (email)=('syn-secret@example.com') already exists.\n"
            "CONTEXT:  COPY entities, line 3"
        )
        handler = MagicMock()
        handler.handle.side_effect = ValueError(driver_text)
        executor = MutationExecutor(
            event_log=log,
            handlers={Operation.ENTITY_CREATE: handler},
        )

        result = executor.execute(_cmd())

        assert result.status == CommandStatus.FAILED
        rejected = log.get_events(event_type=EventType.MUTATION_REJECTED)
        assert len(rejected) == 1
        audit_message = rejected[0].payload["message"]
        assert audit_message == "value too long for type character varying(50)"
        assert "syn-secret@example.com" not in audit_message
        assert "DETAIL" not in audit_message
        assert "CONTEXT" not in audit_message
        assert rejected[0].payload["error_type"] == "ValueError"
        # No code applies to a plain ValueError, so the key stays absent
        # from the payload entirely — the same convention ``reason`` and
        # ``policy_warnings`` already follow.
        assert "error_code" not in rejected[0].payload
        # The caller-facing CommandResult.message is unchanged by this —
        # it already named only the type, never the driver's text.
        assert result.message == "Execution failed: ValueError"

    def test_a_psycopg_shaped_error_s_sqlstate_and_constraint_reach_the_audit(
        self, tmp_path: Path
    ) -> None:
        """A psycopg-style driver exception's ``sqlstate`` and
        ``diag.constraint_name`` land on the audit event as ``error_code``
        and ``constraint``.

        psycopg is not installed in this environment (an optional extra),
        so the real exception class is unavailable; this fakes its shape
        — a plain ``Exception`` subclass carrying the same attributes
        psycopg's own ``Error`` exposes — which is all
        ``summarize_exception`` reads (duck-typed, not an isinstance
        check).
        """

        class _Diag:
            constraint_name = "entities_name_key"

        class _FakePsycopgError(RuntimeError):
            """Carries psycopg.Error's ``sqlstate``/``diag`` shape.

            Subclasses ``RuntimeError`` (not bare ``Exception``) so it is
            one of the executor's enumerated panic-catch types even
            without the real ``psycopg`` package installed — production
            code recognizes the real ``psycopg.Error`` the same way
            (``_optional_driver_panics``), by reading it out of
            ``sys.modules`` rather than importing it.
            """

            sqlstate = "23505"
            diag = _Diag()

        log = SQLiteEventLog(tmp_path / "events.db")
        handler = MagicMock()
        handler.handle.side_effect = _FakePsycopgError(
            'duplicate key value violates unique constraint "entities_name_key"'
        )
        executor = MutationExecutor(
            event_log=log,
            handlers={Operation.ENTITY_CREATE: handler},
        )

        result = executor.execute(_cmd())

        assert result.status == CommandStatus.FAILED
        rejected = log.get_events(event_type=EventType.MUTATION_REJECTED)
        assert len(rejected) == 1
        assert rejected[0].payload["error_type"] == "_FakePsycopgError"
        assert rejected[0].payload["error_code"] == "23505"
        assert rejected[0].payload["constraint"] == "entities_name_key"
        # The constraint name is also quoted in the error's own text; the
        # message field masks it rather than leaving it readable twice.
        assert rejected[0].payload["message"] == (
            'duplicate key value violates unique constraint "..."'
        )
