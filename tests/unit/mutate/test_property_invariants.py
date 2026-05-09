"""Property-based invariants for the governed mutation pipeline.

Uses ``hypothesis`` to generate random Commands and assert pipeline
invariants that should hold across the entire input space:

1. **Single event per SUCCESS** — accepted commands emit exactly one
   ``MUTATION_EXECUTED`` event when an event log is attached.
2. **Single rejection event per rejection (Option A — uniform emit)** —
   every rejection (validate / policy / idempotency-replay) emits
   exactly one ``MUTATION_REJECTED`` event whose payload carries a
   ``reason`` field naming the stage. Symmetric audit trail across all
   three rejection paths.
3. **Idempotency replay** — same key twice produces one
   ``MUTATION_EXECUTED`` (the first call) plus one ``MUTATION_REJECTED``
   with ``reason="idempotency_replay"`` (the second call); the handler
   runs only once.
4. **STOP_ON_ERROR halts the batch** at the first failure; downstream
   commands are never handled and emit no events.

Stores are mocked. No SQLite, no real backends — pure pipeline property
checks that finish in well under a second.
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
    required = OperationRegistry().get_required_args(op)
    if not required:
        # Force an unknown-op-style failure by sending empty args with a
        # multi-arg op. Pick LINK_CREATE which always requires args.
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


def _emitted_event_types(event_log: MagicMock) -> list[str]:
    """Return the list of emitted event-type values, in call order."""
    return [call.args[0].value for call in event_log.emit.call_args_list]


def _emitted_payloads(event_log: MagicMock) -> list[dict[str, Any]]:
    """Return the list of emitted payloads, in call order."""
    return [call.kwargs["payload"] for call in event_log.emit.call_args_list]


# ---------------------------------------------------------------------------
# Property 1: single MUTATION_EXECUTED per SUCCESS
# ---------------------------------------------------------------------------


class TestSingleEventPerSuccess:
    @given(cmd=valid_commands())
    def test_success_emits_exactly_one_event(self, cmd: Command) -> None:
        event_log = MagicMock()
        event_log.has_idempotency_key.return_value = False
        executor = MutationExecutor(
            event_log=event_log,
            handlers=_all_op_handlers(),
        )
        result = executor.execute(cmd)
        assert result.status == CommandStatus.SUCCESS
        assert _emitted_event_types(event_log) == ["mutation.executed"]


# ---------------------------------------------------------------------------
# Property 2: every rejection emits exactly one MUTATION_REJECTED event
# (Option A — uniform emit across validate / policy / idempotency)
# ---------------------------------------------------------------------------


class TestUniformRejectionEvents:
    """Any rejection produces exactly 1 ``MUTATION_REJECTED`` event whose
    payload's ``reason`` field names the stage that rejected the command.

    This was previously asymmetric: only policy rejections emitted, while
    validate-stage and idempotency-replay rejections were silent. Option A
    makes all three paths uniform so the audit trail is symmetric.
    """

    @given(cmd=invalid_commands())
    def test_validate_rejection_emits_one_event_with_reason(
        self,
        cmd: Command,
    ) -> None:
        event_log = MagicMock()
        event_log.has_idempotency_key.return_value = False
        executor = MutationExecutor(
            event_log=event_log,
            handlers=_all_op_handlers(),
        )
        result = executor.execute(cmd)
        assert result.status == CommandStatus.FAILED
        assert _emitted_event_types(event_log) == ["mutation.rejected"]
        payload = _emitted_payloads(event_log)[0]
        assert payload["reason"] == "validate"
        assert payload["status"] == CommandStatus.REJECTED

    @given(cmd=valid_commands())
    def test_policy_rejection_emits_one_event_with_reason(
        self,
        cmd: Command,
    ) -> None:
        gate = MagicMock()
        gate.check.return_value = (False, "denied", [])
        event_log = MagicMock()
        event_log.has_idempotency_key.return_value = False
        executor = MutationExecutor(
            event_log=event_log,
            policy_gate=gate,
            handlers=_all_op_handlers(),
        )
        result = executor.execute(cmd)
        assert result.status == CommandStatus.REJECTED
        assert _emitted_event_types(event_log) == ["mutation.rejected"]
        payload = _emitted_payloads(event_log)[0]
        assert payload["reason"] == "policy_violation"

    @given(cmd=valid_commands(), key=_safe_text)
    def test_idempotency_replay_emits_one_rejection_after_first_success(
        self,
        cmd: Command,
        key: str,
    ) -> None:
        cmd = cmd.model_copy(update={"idempotency_key": key})
        replay = cmd.model_copy(update={"command_id": cmd.command_id + "_replay"})

        event_log = MagicMock()
        event_log.has_idempotency_key.return_value = False
        executor = MutationExecutor(
            event_log=event_log,
            handlers=_all_op_handlers(),
        )
        r1 = executor.execute(cmd)
        r2 = executor.execute(replay)

        assert r1.status == CommandStatus.SUCCESS
        assert r2.status == CommandStatus.DUPLICATE
        # Two events total: SUCCESS for the first call, REJECTED for the replay.
        assert _emitted_event_types(event_log) == [
            "mutation.executed",
            "mutation.rejected",
        ]
        replay_payload = _emitted_payloads(event_log)[1]
        assert replay_payload["reason"] == "idempotency_replay"
        assert replay_payload["idempotency_key"] == key


# ---------------------------------------------------------------------------
# Property 3: idempotency replay does not double-handle
# ---------------------------------------------------------------------------


class TestIdempotencyReplay:
    @given(cmd=valid_commands(), key=_safe_text)
    def test_replay_does_not_double_handle(self, cmd: Command, key: str) -> None:
        cmd = cmd.model_copy(update={"idempotency_key": key})
        replay = cmd.model_copy(update={"command_id": cmd.command_id + "_replay"})

        handlers = _all_op_handlers()
        event_log = MagicMock()
        event_log.has_idempotency_key.return_value = False
        executor = MutationExecutor(
            event_log=event_log,
            handlers=handlers,
        )
        executor.execute(cmd)
        executor.execute(replay)

        # Handler runs exactly once even though we submitted twice.
        assert handlers[cmd.operation].handle.call_count == 1


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
        event_log = MagicMock()
        event_log.has_idempotency_key.return_value = False
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

        assert len(results) == good_before + 1
        for r in results[:good_before]:
            assert r.status == CommandStatus.SUCCESS
        assert results[-1].status == CommandStatus.FAILED

        # Each SUCCESS before the failure emits one MUTATION_EXECUTED, the
        # validate-stage rejection emits one MUTATION_REJECTED, then nothing
        # more (downstream commands are never executed).
        assert event_log.emit.call_count == good_before + 1
        assert handlers[Operation.ENTITY_CREATE].handle.call_count == good_before
