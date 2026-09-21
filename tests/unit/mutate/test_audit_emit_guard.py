"""The audit emit cannot change what the governed pipeline reports (#551).

Stage 5 emitted ``MUTATION_EXECUTED`` on a bare ``self._emit(...)``, so an
``EventLog.emit`` that raised propagated out of ``execute`` **after the
handler's write had committed** — the caller was told the mutation failed
while the store change was durable, and no audit event recorded either half.

The guard sits at ``_emit_event``, the one seam every stage routes through,
rather than at Stage 5 alone. The reason is the *other* half of the same
defect: at the Stage 1-4 rejection sites a raise tells no lie about the store
(nothing was committed), but it still escapes ``execute`` and aborts the
surrounding ``execute_batch``, which the Stage 4 comments promise will not
happen under ``CONTINUE_ON_ERROR``. Guarding Stage 5 alone would leave a dead
event log degrading a *successful* command gracefully while taking the batch
down on a *failed* one.

Measured before any of this was written: zero of the 3,655 governed mutations
this deployment has executed since 2026-07-06 lost a Stage 5 emit. The defect
is latent, which is why the fix is a guard and a warning rather than an
outbox.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

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
    CommandStatus,
    Operation,
)
from trellis.mutate.executor import (
    AUDIT_EMIT_FAILED_MARKER,
    MutationExecutor,
)
from trellis.stores.base.event_log import EventLog, EventType

_ARGS = {"entity_type": "service", "name": "auth"}


def _cmd(**kwargs: Any) -> Command:
    return Command(operation=Operation.ENTITY_CREATE, args=dict(_ARGS), **kwargs)


def _handler(*, created_id: str = "ent_1", raises: BaseException | None = None):
    h = MagicMock()
    if raises is not None:
        h.handle.side_effect = raises
    else:
        h.handle.return_value = (created_id, "created")
    return h


def _event_log(*, fails: BaseException | None = None, has_key: bool = False):
    """An ``EventLog`` double whose ``emit`` raises *fails*, or works."""
    log = MagicMock(spec=EventLog)
    log.has_idempotency_key.return_value = has_key
    if fails is not None:
        log.emit.side_effect = fails
    return log


def _dead_log(**kwargs: Any):
    return _event_log(
        fails=StoreError("event log unavailable", store="events"), **kwargs
    )


def _allowing_gate(warnings: list[str] | None = None):
    gate = MagicMock()
    gate.check.return_value = (True, "", warnings or [])
    return gate


def _denying_gate():
    gate = MagicMock()
    gate.check.return_value = (False, "Approval required", [])
    return gate


def _audit_warnings(result) -> list[str]:
    return [w for w in result.warnings if w.startswith(AUDIT_EMIT_FAILED_MARKER)]


# --------------------------------------------------------------------------
# Every site that emits, with the log dead.
#
# Ten call sites thread the emit's outcome onto their own CommandResult, and
# they are a copy-paste family: the same two-line shape repeated per stage.
# That is the shape this repo has been bitten by before (#456 — six
# independent hand-written copies of one four-field expression, eleven of
# twelve mutants surviving the full suite), so every site is exercised rather
# than the interesting one.
# --------------------------------------------------------------------------


def _run_stage1_validate(log):
    ex = MutationExecutor(event_log=log, handlers={Operation.ENTITY_CREATE: _handler()})
    return ex.execute(Command(operation=Operation.ENTITY_CREATE, args={}))


def _run_stage2_policy(log):
    ex = MutationExecutor(
        event_log=log,
        policy_gate=_denying_gate(),
        handlers={Operation.ENTITY_CREATE: _handler()},
    )
    return ex.execute(_cmd())


def _run_stage3_duplicate_in_memory(log):
    ex = MutationExecutor(event_log=log, handlers={Operation.ENTITY_CREATE: _handler()})
    ex.execute(_cmd(idempotency_key="k-1"))
    return ex.execute(_cmd(idempotency_key="k-1"))


def _run_stage3_duplicate_persisted(log):
    log.has_idempotency_key.return_value = True
    ex = MutationExecutor(event_log=log, handlers={Operation.ENTITY_CREATE: _handler()})
    return ex.execute(_cmd(idempotency_key="k-2"))


def _run_stage4(log, exc):
    ex = MutationExecutor(
        event_log=log,
        handlers={Operation.ENTITY_CREATE: _handler(raises=exc)},
    )
    return ex.execute(_cmd())


def _run_stage4_validation(log):
    return _run_stage4(log, ValidationError("bad row", errors=["bad row"]))


def _run_stage4_policy_violation(log):
    return _run_stage4(log, PolicyViolationError("denied", policy_id="p-1"))


def _run_stage4_idempotency(log):
    return _run_stage4(log, IdempotencyError(idempotency_key="k-3"))


def _run_stage4_store_error(log):
    return _run_stage4(log, StoreError("graph down", store="graph"))


def _run_stage4_panic(log):
    return _run_stage4(log, RuntimeError("boom"))


def _run_stage5_success(log):
    ex = MutationExecutor(event_log=log, handlers={Operation.ENTITY_CREATE: _handler()})
    return ex.execute(_cmd())


EMIT_SITES = [
    ("stage1_validate", _run_stage1_validate, CommandStatus.FAILED),
    ("stage2_policy", _run_stage2_policy, CommandStatus.REJECTED),
    (
        "stage3_duplicate_in_memory",
        _run_stage3_duplicate_in_memory,
        CommandStatus.DUPLICATE,
    ),
    (
        "stage3_duplicate_persisted",
        _run_stage3_duplicate_persisted,
        CommandStatus.DUPLICATE,
    ),
    ("stage4_validation", _run_stage4_validation, CommandStatus.REJECTED),
    ("stage4_policy_violation", _run_stage4_policy_violation, CommandStatus.REJECTED),
    ("stage4_idempotency", _run_stage4_idempotency, CommandStatus.DUPLICATE),
    ("stage4_store_error", _run_stage4_store_error, CommandStatus.FAILED),
    ("stage4_panic", _run_stage4_panic, CommandStatus.FAILED),
    ("stage5_success", _run_stage5_success, CommandStatus.SUCCESS),
]


class TestEveryEmitSiteDegradesInsteadOfRaising:
    @pytest.mark.parametrize(
        ("run", "expected"),
        [pytest.param(run, expected, id=name) for name, run, expected in EMIT_SITES],
    )
    def test_the_reported_status_is_unchanged_by_a_dead_event_log(
        self, run, expected
    ) -> None:
        healthy = run(_event_log())
        degraded = run(_dead_log())
        assert healthy.status == expected
        assert degraded.status == expected

    @pytest.mark.parametrize(
        "run", [pytest.param(run, id=name) for name, run, _ in EMIT_SITES]
    )
    def test_the_missing_audit_event_is_stated_on_the_result(self, run) -> None:
        result = run(_dead_log())
        assert _audit_warnings(result), (
            "a result whose audit event was lost must say so — silently "
            "dropping it is the degradation this guard exists to avoid"
        )

    @pytest.mark.parametrize(
        "run", [pytest.param(run, id=name) for name, run, _ in EMIT_SITES]
    )
    def test_a_healthy_event_log_adds_no_warning(self, run) -> None:
        assert _audit_warnings(run(_event_log())) == []


class TestStageFiveIsTheSiteThatWouldHaveLied:
    """The committed-write case — #551's headline."""

    def test_a_committed_write_is_still_reported_as_a_success(self) -> None:
        handler = _handler(created_id="ent_42")
        ex = MutationExecutor(
            event_log=_dead_log(), handlers={Operation.ENTITY_CREATE: handler}
        )

        result = ex.execute(_cmd())

        assert result.status == CommandStatus.SUCCESS
        assert result.created_id == "ent_42"
        assert result.target_id == _cmd().target_id
        handler.handle.assert_called_once()

    def test_the_warning_names_the_event_that_is_missing(self) -> None:
        ex = MutationExecutor(
            event_log=_dead_log(), handlers={Operation.ENTITY_CREATE: _handler()}
        )

        (warning,) = _audit_warnings(ex.execute(_cmd()))

        assert EventType.MUTATION_EXECUTED in warning
        assert "StoreError" in warning
        assert "event log unavailable" in warning

    def test_the_status_is_not_downgraded_to_failed(self) -> None:
        """FAILED would be the lie: the handler's write is durable."""
        ex = MutationExecutor(
            event_log=_dead_log(), handlers={Operation.ENTITY_CREATE: _handler()}
        )
        result = ex.execute(_cmd())
        assert result.status is not CommandStatus.FAILED
        assert result.status is not CommandStatus.REJECTED

    def test_policy_warnings_are_kept_and_come_first(self) -> None:
        """The audit warning is additive — it must not displace Stage 2's."""
        ex = MutationExecutor(
            event_log=_dead_log(),
            policy_gate=_allowing_gate(["retention policy applies"]),
            handlers={Operation.ENTITY_CREATE: _handler()},
        )

        result = ex.execute(_cmd())

        assert result.warnings[0] == "retention policy applies"
        assert len(_audit_warnings(result)) == 1
        assert len(result.warnings) == 2

    def test_a_healthy_stage_five_keeps_the_result_byte_identical(self) -> None:
        """The transparency property #424/#425 pinned for the gate.

        A working event log must leave the result exactly as it was before
        the guard existed, or every consumer of ``warnings`` inherits a new
        constant.
        """
        log = _event_log()
        ex = MutationExecutor(
            event_log=log, handlers={Operation.ENTITY_CREATE: _handler()}
        )

        result = ex.execute(_cmd())

        assert result.warnings == []
        log.emit.assert_called_once()
        assert log.emit.call_args.args[0] is EventType.MUTATION_EXECUTED


class TestTheBatchContractSurvivesADeadEventLog:
    """``execute_batch`` relies on per-command results, not on exceptions.

    The Stage 4 comments say a re-raise "would mid-air-abort a batch even
    when the caller asked for continue on error" — and then the emit inside
    those very ``except`` arms could do exactly that.
    """

    def _batch(self, strategy: BatchStrategy) -> CommandBatch:
        return CommandBatch(
            commands=[_cmd(), _cmd(), _cmd()],
            strategy=strategy,
        )

    def test_continue_on_error_still_runs_every_command(self) -> None:
        handler = _handler()
        handler.handle.side_effect = [
            ("ent_1", "created"),
            StoreError("graph down", store="graph"),
            ("ent_3", "created"),
        ]
        ex = MutationExecutor(
            event_log=_dead_log(), handlers={Operation.ENTITY_CREATE: handler}
        )

        results = ex.execute_batch(self._batch(BatchStrategy.CONTINUE_ON_ERROR))

        assert [r.status for r in results] == [
            CommandStatus.SUCCESS,
            CommandStatus.FAILED,
            CommandStatus.SUCCESS,
        ]
        assert all(_audit_warnings(r) for r in results)

    def test_stop_on_error_still_stops(self) -> None:
        """The guard must not rescue a genuinely failed command."""
        handler = _handler()
        handler.handle.side_effect = [
            StoreError("graph down", store="graph"),
            ("ent_2", "created"),
            ("ent_3", "created"),
        ]
        ex = MutationExecutor(
            event_log=_dead_log(), handlers={Operation.ENTITY_CREATE: handler}
        )

        results = ex.execute_batch(self._batch(BatchStrategy.STOP_ON_ERROR))

        assert [r.status for r in results] == [CommandStatus.FAILED]


class TestTheGuardIsScopedToTheEmitAlone:
    def test_a_failing_handler_is_not_absorbed_by_the_audit_guard(self) -> None:
        """A mutant that widens the ``try`` to cover Stage 4 dies here."""
        ex = MutationExecutor(
            event_log=_event_log(),
            handlers={Operation.ENTITY_CREATE: _handler(raises=RuntimeError("boom"))},
        )

        result = ex.execute(_cmd())

        assert result.status == CommandStatus.FAILED
        assert _audit_warnings(result) == []

    def test_the_handler_runs_exactly_once_when_the_emit_fails(self) -> None:
        handler = _handler()
        ex = MutationExecutor(
            event_log=_dead_log(), handlers={Operation.ENTITY_CREATE: handler}
        )

        ex.execute(_cmd())

        handler.handle.assert_called_once()

    def test_no_event_log_produces_no_audit_warning(self) -> None:
        """``event_log=None`` skips Stage 5 and says nothing — by design.

        It is not a shipped state: every ``MutationExecutor(...)`` in
        ``src/`` passes a real log (pinned by
        ``tests/unit/test_policy_gate_rule.py``), and a knowledge-plane-only
        deployment configures the ``null`` backend so the store resolves to
        ``NullEventLog`` rather than to ``None`` (#196). Nothing failed
        here, so there is nothing to warn about.
        """
        ex = MutationExecutor(handlers={Operation.ENTITY_CREATE: _handler()})

        result = ex.execute(_cmd())

        assert result.status == CommandStatus.SUCCESS
        assert result.warnings == []


class TestWhatTheGuardCatches:
    @pytest.mark.parametrize(
        "exc",
        [
            pytest.param(StoreError("down", store="events"), id="store_error"),
            pytest.param(TrellisError("down"), id="trellis_error"),
            pytest.param(RuntimeError("down"), id="runtime_error"),
            pytest.param(ConnectionError("refused"), id="oserror_subclass"),
            pytest.param(ValueError("bad payload"), id="value_error"),
            pytest.param(TypeError("bad payload"), id="type_error"),
        ],
    )
    def test_a_backend_that_raises_any_of_these_degrades(self, exc) -> None:
        """``StoreError`` is what a well-behaved backend raises. The rest
        cover one that is not — an event log raising ``ConnectionError``
        must not be the difference between a warning and an aborted batch.
        """
        ex = MutationExecutor(
            event_log=_event_log(fails=exc),
            handlers={Operation.ENTITY_CREATE: _handler()},
        )

        result = ex.execute(_cmd())

        assert result.status == CommandStatus.SUCCESS
        assert type(exc).__name__ in _audit_warnings(result)[0]

    @pytest.mark.parametrize(
        "exc",
        [
            pytest.param(KeyboardInterrupt(), id="keyboard_interrupt"),
            pytest.param(SystemExit(), id="system_exit"),
        ],
    )
    def test_the_catch_is_enumerated_so_interrupts_still_propagate(self, exc) -> None:
        """The tuple is explicit, not ``except Exception`` — an operator's
        Ctrl-C must not be downgraded to a warning on a CommandResult.
        """
        ex = MutationExecutor(
            event_log=_event_log(fails=exc),
            handlers={Operation.ENTITY_CREATE: _handler()},
        )

        with pytest.raises(type(exc)):
            ex.execute(_cmd())
