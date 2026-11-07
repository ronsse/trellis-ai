"""A handler's outcome reaches ``CommandResult.metadata`` and no further.

``CommandHandler.handle`` returns a fixed ``(created_id, message)`` pair, so a
handler that must tell the caller how its write came out (``entity.create`` with
``if_absent`` found the node and wrote nothing) leaves a value under
``Command.metadata["handler_outcome"]``. The executor copies a known value to
``CommandResult.metadata["outcome"]``. These tests use a stub handler, so they
pin the executor's half on its own; the ``entity.create`` half is in
``test_graph_replace_conditional.py``.
"""

from __future__ import annotations

from typing import Any

import pytest

from trellis.errors import ValidationError
from trellis.mutate.commands import (
    HANDLER_OUTCOME_KEY,
    HANDLER_OUTCOMES,
    OUTCOME_LEFT_ALONE,
    OUTCOME_METADATA_KEY,
    Command,
    CommandStatus,
    Operation,
)
from trellis.mutate.executor import MutationExecutor
from trellis.mutate.reserved_metadata import (
    RESERVED_COMMAND_METADATA_KEYS,
    ReservedMetadataKeyError,
    reject_reserved_metadata,
)

_NOTHING: Any = object()


class _Reports:
    """Sets whatever the test hands it under the outcome key, then succeeds."""

    def __init__(self, outcome: Any = _NOTHING, *, then_raise: bool = False) -> None:
        self.outcome = outcome
        self.then_raise = then_raise

    def handle(self, command: Command) -> tuple[str | None, str]:
        if self.outcome is not _NOTHING:
            command.metadata[HANDLER_OUTCOME_KEY] = self.outcome
        if self.then_raise:
            msg = "refused after setting an outcome"
            raise ValidationError(msg, errors=[msg], code="stub_refusal")
        return "id-1", "done"


def _command(**kwargs: Any) -> Command:
    return Command(
        operation=Operation.ENTITY_CREATE,
        args={"entity_type": "concept", "name": "n"},
        **kwargs,
    )


def _executor(handler: _Reports) -> MutationExecutor:
    return MutationExecutor(handlers={Operation.ENTITY_CREATE: handler})


def test_the_outcome_vocabulary_is_closed() -> None:
    assert frozenset({OUTCOME_LEFT_ALONE}) == HANDLER_OUTCOMES


def test_a_known_outcome_is_copied_onto_the_result() -> None:
    result = _executor(_Reports(OUTCOME_LEFT_ALONE)).execute(_command())

    assert result.status == CommandStatus.SUCCESS
    assert result.metadata == {OUTCOME_METADATA_KEY: OUTCOME_LEFT_ALONE}
    assert result.message == "done"
    assert result.created_id == "id-1"


def test_a_success_with_no_outcome_adds_no_key() -> None:
    result = _executor(_Reports()).execute(_command())

    assert result.status == CommandStatus.SUCCESS
    assert result.metadata == {}


@pytest.mark.parametrize("unknown", ["created", "", "LEFT_ALONE", 1, ["left_alone"]])
def test_a_value_outside_the_vocabulary_is_dropped(unknown: Any) -> None:
    result = _executor(_Reports(unknown)).execute(_command())

    assert result.status == CommandStatus.SUCCESS
    assert OUTCOME_METADATA_KEY not in result.metadata


def test_an_outcome_planted_on_the_command_is_not_reported() -> None:
    """Only what the handler sets during ``handle`` counts, so a value already
    on the command (a caller's, or a previous run's on a reused object) is
    cleared before the handler runs."""
    command = _command(metadata={HANDLER_OUTCOME_KEY: OUTCOME_LEFT_ALONE})

    result = _executor(_Reports()).execute(command)

    assert result.status == CommandStatus.SUCCESS
    assert OUTCOME_METADATA_KEY not in result.metadata


def test_a_reused_command_does_not_carry_the_last_run_forward() -> None:
    handler = _Reports(OUTCOME_LEFT_ALONE)
    executor = _executor(handler)
    command = _command()

    first = executor.execute(command)
    handler.outcome = _NOTHING
    second = executor.execute(command)

    assert first.metadata == {OUTCOME_METADATA_KEY: OUTCOME_LEFT_ALONE}
    assert OUTCOME_METADATA_KEY not in second.metadata


def test_a_refusal_after_the_handler_set_an_outcome_reports_none() -> None:
    result = _executor(_Reports(OUTCOME_LEFT_ALONE, then_raise=True)).execute(
        _command()
    )

    assert result.status == CommandStatus.REJECTED
    assert OUTCOME_METADATA_KEY not in result.metadata
    assert result.metadata == {"rejection_reason": "stub_refusal"}


def test_the_handler_key_is_reserved_at_every_caller_boundary() -> None:
    assert HANDLER_OUTCOME_KEY in RESERVED_COMMAND_METADATA_KEYS
    with pytest.raises(ReservedMetadataKeyError):
        reject_reserved_metadata({HANDLER_OUTCOME_KEY: OUTCOME_LEFT_ALONE})
