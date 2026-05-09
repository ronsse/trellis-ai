"""Property-based invariants for the governed mutation pipeline.

These tests use ``hypothesis`` to generate random Commands and assert
pipeline invariants that should hold across the entire input space:

1. Single event per SUCCESS — accepted commands emit exactly one
   ``MUTATION_EXECUTED`` event when an event log is attached.
2. Zero events for validation failures and idempotency duplicates.
   (NOTE: policy rejections DO emit one ``MUTATION_REJECTED`` event by
   design — see ``executor.py`` line ~108. The unit spec wording
   "zero events on rejection" is reconciled with reality below.)
3. Idempotency replay — same key twice produces one event total.
4. STOP_ON_ERROR halts the batch at the first failure: downstream
   commands are not handled and emit no events.

Stores are mocked. No SQLite, no real backends — these tests are
pure pipeline property checks that finish in well under a second.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from hypothesis import given
from hypothesis import strategies as st

from trellis.mutate.commands import (
    BatchStrategy,
    Command,
    CommandBatch,
    CommandStatus,
    Operation,
    OperationRegistry,
)
from trellis.mutate.executor import MutationExecutor

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Operations covered by the curate handlers + ones with simple required-arg
# shapes. We avoid ingest ops that need full Trace/Evidence pydantic models.
_OPS_WITH_SIMPLE_ARGS: list[tuple[Operation, dict[str, Any]]] = [
    (Operation.ENTITY_CREATE, {"entity_type": "service", "name": "auth"}),
    (Operation.ENTITY_UPDATE, {"entity_id": "ent_1"}),
    (Operation.ENTITY_MERGE, {"source_id": "a", "target_id": "b"}),
    (Operation.LINK_CREATE, {"source_id": "a", "target_id": "b", "edge_kind": "uses"}),
    (Operation.LINK_REMOVE, {"edge_id": "edge_1"}),
    (Operation.LABEL_ADD, {"target_id": "n1", "label": "v1"}),
    (Operation.LABEL_REMOVE, {"target_id": "n1", "label": "v1"}),
    (Operation.FEEDBACK_RECORD, {"target_id": "p1", "rating": 5}),
    (Operation.PRECEDENT_PROMOTE, {"trace_id": "t1", "title": "x", "description": "y"}),
    (Operation.PRECEDENT_UPDATE, {"precedent_id": "p1"}),
    (Operation.REDACTION_APPLY, {"target_id": "n1", "reason": "pii"}),
    (Operation.RETENTION_PRUNE, {}),
]

# Identifier-ish strings — printable, bounded, non-empty. Avoid surrogates and
# control characters which confuse logging on Windows.
_safe_text = st.text(
    alphabet=st.characters(min_codepoint=33, max_codepoint=126),
    min_size=1,
    max_size=24,
)


@st.composite
def valid_commands(draw: st.DrawFn) -> Command:
    """Commands that should pass validation (correct required args)."""
    op, args = draw(st.sampled_from(_OPS_WITH_SIMPLE_ARGS))
    idem = draw(st.one_of(st.none(), _safe_text))
    return Command(operation=op, args=dict(args), idempotency_key=idem)


@st.composite
def invalid_commands(draw: st.DrawFn) -> Command:
    """Commands that should fail validation (drop a required arg)."""
    op, args = draw(st.sampled_from(_OPS_WITH_SIMPLE_ARGS))
    # Skip ops with no required args — they can't be made invalid this way.
    required = OperationRegistry().get_required_args(op)
    if not required:
        # Force an unknown-op-style failure by sending an empty args
        # with a multi-arg op. Pick LINK_CREATE which always needs args.
        op = Operation.LINK_CREATE
        args = {}
    else:
        args = {k: v for k, v in args.items() if k != next(iter(required))}
    return Command(operation=op, args=dict(args))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ok_handler(created_id: str = "id_1", message: str = "ok") -> MagicMock:
    h = MagicMock()
    h.handle.return_value = (created_id, message)
    return h


def _all_op_handlers() -> dict[str, MagicMock]:
    """A handler for every operation in our test alphabet."""
    return {op: _ok_handler() for op, _ in _OPS_WITH_SIMPLE_ARGS}


def _mock_event_log() -> MagicMock:
    """Build an event-log mock that returns False for ``has_idempotency_key``.

    Centralised because every test in this module needs the same
    pre-canned ``has_idempotency_key`` return — without it the executor
    skips the in-process FIFO check and hits ``AttributeError`` on the
    bare MagicMock.
    """
    event_log = MagicMock()
    event_log.has_idempotency_key.return_value = False
    return event_log


# ---------------------------------------------------------------------------
# Property 1: single event per SUCCESS
# ---------------------------------------------------------------------------


class TestSingleEventPerSuccess:
    @given(cmd=valid_commands())
    def test_success_emits_exactly_one_event(self, cmd: Command) -> None:
        event_log = _mock_event_log()
        executor = MutationExecutor(
            event_log=event_log,
            handlers=_all_op_handlers(),
        )
        result = executor.execute(cmd)
        assert result.status == CommandStatus.SUCCESS
        # Exactly one MUTATION_EXECUTED event from the executor itself.
        # (Handlers may or may not emit additional events; we mocked them.)
        assert event_log.emit.call_count == 1
        emitted_event_type = event_log.emit.call_args[0][0]
        assert emitted_event_type.value == "mutation.executed"


# ---------------------------------------------------------------------------
# Property 2: zero events on validation failure / idempotency duplicate
# ---------------------------------------------------------------------------


class TestZeroEventsOnSilentRejection:
    """Validation failures and idempotency duplicates do not touch the event
    log. Policy rejections DO emit one ``MUTATION_REJECTED`` event by design
    — covered separately in ``TestPolicyRejectionEmitsOneEvent``.
    """

    @given(cmd=invalid_commands())
    def test_validation_failure_emits_no_events(self, cmd: Command) -> None:
        event_log = _mock_event_log()
        executor = MutationExecutor(
            event_log=event_log,
            handlers=_all_op_handlers(),
        )
        result = executor.execute(cmd)
        assert result.status == CommandStatus.FAILED
        assert event_log.emit.call_count == 0

    @given(key=_safe_text)
    def test_idempotency_duplicate_emits_no_events(self, key: str) -> None:
        event_log = _mock_event_log()
        executor = MutationExecutor(
            event_log=event_log,
            handlers=_all_op_handlers(),
        )
        cmd1 = Command(
            operation=Operation.ENTITY_CREATE,
            args={"entity_type": "svc", "name": "x"},
            idempotency_key=key,
        )
        cmd2 = Command(
            operation=Operation.ENTITY_CREATE,
            args={"entity_type": "svc", "name": "x"},
            idempotency_key=key,
        )
        r1 = executor.execute(cmd1)
        r2 = executor.execute(cmd2)
        assert r1.status == CommandStatus.SUCCESS
        assert r2.status == CommandStatus.DUPLICATE
        # Only one event total — from the first SUCCESS. Duplicate emits none.
        assert event_log.emit.call_count == 1


# ---------------------------------------------------------------------------
# Property 2b (documented exception): policy rejection emits exactly one event
# ---------------------------------------------------------------------------


class TestPolicyRejectionEmitsOneEvent:
    """Policy rejection emits exactly one ``MUTATION_REJECTED`` event — this
    is the documented executor behaviour (executor.py L106-115). Listed as a
    distinct property to make the contract explicit.
    """

    @given(cmd=valid_commands())
    def test_policy_rejection_emits_one_rejection_event(self, cmd: Command) -> None:
        gate = MagicMock()
        gate.check.return_value = (False, "denied", [])
        event_log = _mock_event_log()
        executor = MutationExecutor(
            event_log=event_log,
            policy_gate=gate,
            handlers=_all_op_handlers(),
        )
        result = executor.execute(cmd)
        assert result.status == CommandStatus.REJECTED
        assert event_log.emit.call_count == 1
        emitted_event_type = event_log.emit.call_args[0][0]
        assert emitted_event_type.value == "mutation.rejected"


# ---------------------------------------------------------------------------
# Property 3: idempotency replay does not double-apply
# ---------------------------------------------------------------------------


class TestIdempotencyReplay:
    @given(cmd=valid_commands(), key=_safe_text)
    def test_replay_does_not_double_handle(self, cmd: Command, key: str) -> None:
        # Force an idempotency key onto a freshly-built command (the strategy
        # may have given None — we want determinism here).
        cmd = cmd.model_copy(update={"idempotency_key": key})

        handlers = _all_op_handlers()
        event_log = _mock_event_log()
        executor = MutationExecutor(
            event_log=event_log,
            handlers=handlers,
        )
        r1 = executor.execute(cmd)
        # Submit a second command with the same idempotency key.
        cmd2 = cmd.model_copy(update={"command_id": cmd.command_id + "_replay"})
        r2 = executor.execute(cmd2)

        assert r1.status == CommandStatus.SUCCESS
        assert r2.status == CommandStatus.DUPLICATE

        # The handler for this op was called exactly once (not twice).
        target_handler = handlers[cmd.operation]
        assert target_handler.handle.call_count == 1

        # And exactly one event was emitted — the SUCCESS. The DUPLICATE
        # short-circuit emits no event.
        assert event_log.emit.call_count == 1


# ---------------------------------------------------------------------------
# Property 4: STOP_ON_ERROR halts the batch
# ---------------------------------------------------------------------------


class TestBatchStopOnError:
    @given(
        good_before=st.integers(min_value=0, max_value=3),
        good_after=st.integers(min_value=1, max_value=4),
    )
    def test_stop_on_error_halts_at_first_failure(
        self,
        good_before: int,
        good_after: int,
    ) -> None:
        handlers = _all_op_handlers()
        event_log = _mock_event_log()
        executor = MutationExecutor(
            event_log=event_log,
            handlers=handlers,
        )

        good = lambda: Command(  # noqa: E731
            operation=Operation.ENTITY_CREATE,
            args={"entity_type": "svc", "name": "x"},
        )
        # Validation failure: required args missing.
        bad = Command(operation=Operation.ENTITY_CREATE, args={})

        commands: list[Command] = [
            *[good() for _ in range(good_before)],
            bad,
            *[good() for _ in range(good_after)],
        ]
        batch = CommandBatch(commands=commands, strategy=BatchStrategy.STOP_ON_ERROR)
        results = executor.execute_batch(batch)

        # Halted at the bad command → results length == good_before + 1
        assert len(results) == good_before + 1
        for r in results[:good_before]:
            assert r.status == CommandStatus.SUCCESS
        assert results[-1].status == CommandStatus.FAILED

        # Only the SUCCESSes before the failure emit events. Validation
        # failure emits zero. None of the post-failure commands are touched.
        assert event_log.emit.call_count == good_before
        assert handlers[Operation.ENTITY_CREATE].handle.call_count == good_before

    @given(
        good_before=st.integers(min_value=0, max_value=3),
        good_after=st.integers(min_value=1, max_value=4),
    )
    def test_continue_on_error_runs_all_commands(
        self,
        good_before: int,
        good_after: int,
    ) -> None:
        """Sanity counter-test: CONTINUE_ON_ERROR keeps running. Confirms the
        STOP_ON_ERROR property above isn't an artifact of, e.g., the bad
        command silently consuming the rest of the iterator.
        """
        handlers = _all_op_handlers()
        event_log = _mock_event_log()
        executor = MutationExecutor(
            event_log=event_log,
            handlers=handlers,
        )

        good = lambda: Command(  # noqa: E731
            operation=Operation.ENTITY_CREATE,
            args={"entity_type": "svc", "name": "x"},
        )
        bad = Command(operation=Operation.ENTITY_CREATE, args={})
        commands = [
            *[good() for _ in range(good_before)],
            bad,
            *[good() for _ in range(good_after)],
        ]
        batch = CommandBatch(
            commands=commands, strategy=BatchStrategy.CONTINUE_ON_ERROR
        )
        results = executor.execute_batch(batch)

        assert len(results) == len(commands)
        # All goods succeed; the bad one fails.
        success_count = sum(1 for r in results if r.status == CommandStatus.SUCCESS)
        assert success_count == good_before + good_after
