"""``evidence.reingest_redacted`` on the generic command door (#1049).

The flag is a boolean. ``POST /evidence`` and ``POST /documents`` answer any
other value with a 422; ``POST /commands/batch`` and MCP ``execute_mutation``
build the command with ``build_evidence_ingest_command_from_args`` and the
handler refuses it. The builder used to carry the flag only ``if
reingest_redacted:``, so ``None``, ``0`` and ``""`` read as an absent flag
while ``"yes"`` was refused. Absent and ``False`` stay the unflagged write, and
their derived idempotency key is unchanged.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.redaction_tombstone import make_tombstone
from trellis.core.document_write import is_redaction_tombstone
from trellis.core.vector_metadata import resolve_vector_store
from trellis.mutate import build_curate_executor
from trellis.mutate.commands import CommandStatus
from trellis.mutate.evidence_ingest import (
    build_evidence_ingest_command,
    build_evidence_ingest_command_from_args,
)
from trellis.stores.registry import StoreRegistry

NOT_BOOLEANS = [None, 0, 1, "", "true", "yes", [], [True]]
FALSY_NOT_BOOLEANS = [None, 0, ""]
ID = "doc-k"
TEXT = "Amber ledger text"


@pytest.fixture
def registry(tmp_path: Path):
    stores_dir = tmp_path / "stores"
    stores_dir.mkdir()
    reg = StoreRegistry(stores_dir=stores_dir)
    yield reg
    reg.close()


def _build(**kwargs: Any):
    return build_evidence_ingest_command(
        content=TEXT,
        requested_by="test:flag",
        doc_id=ID,
        metadata={"a": 1},
        **kwargs,
    )


def _from_args(**flag: Any):
    evidence = {"doc_id": ID, "content": TEXT, "metadata": {"a": 1}, **flag}
    return build_evidence_ingest_command_from_args(
        {"evidence": evidence}, requested_by="test:flag"
    )


class TestCommandPreparation:
    def test_absent_and_false_are_the_same_unflagged_command(self) -> None:
        """Defective subject: carry ``False`` (``if reingest_redacted is not
        None``) and every unflagged write's derived key changes. The bytes are
        pinned against a golden key in ``test_document_write_tombstone.py``."""
        absent = _build()

        for command in (
            _build(reingest_redacted=False),
            _from_args(),
            _from_args(reingest_redacted=False),
        ):
            assert command.idempotency_key == absent.idempotency_key
            assert command.args == absent.args
        assert "reingest_redacted" not in absent.args["evidence"]

    @pytest.mark.parametrize("value", NOT_BOOLEANS, ids=repr)
    def test_a_set_non_boolean_is_carried_as_given(self, value: object) -> None:
        """Defective subject: ``if reingest_redacted:`` drops ``None``, ``0``
        and ``""``, so the handler never sees them and the write is a plain
        unflagged one."""
        command = _from_args(reingest_redacted=value)

        carried = command.args["evidence"]["reingest_redacted"]
        assert carried == value
        assert type(carried) is type(value)

    @pytest.mark.parametrize("value", FALSY_NOT_BOOLEANS, ids=repr)
    def test_a_falsy_non_boolean_does_not_share_the_unflagged_key(
        self, value: object
    ) -> None:
        """The idempotency stage answers before the handler's refusal, so a
        refused value that derived the unflagged key would read as a replay of
        the unflagged write that already succeeded."""
        command = _from_args(reingest_redacted=value)

        assert command.idempotency_key != _build().idempotency_key
        assert command.idempotency_key != _build(reingest_redacted=True).idempotency_key


class TestRefusedThroughTheExecutor:
    @pytest.mark.parametrize("value", NOT_BOOLEANS, ids=repr)
    def test_a_set_non_boolean_over_a_tombstone_is_refused_and_nothing_is_written(
        self, registry: StoreRegistry, value: object
    ) -> None:
        make_tombstone(
            registry.knowledge.document_store, resolve_vector_store(registry), ID
        )
        held = registry.knowledge.document_store.get(ID)

        result = build_curate_executor(registry, clearance="restricted").execute(
            _from_args(reingest_redacted=value)
        )

        assert result.status is CommandStatus.REJECTED
        assert "evidence.reingest_redacted must be a boolean" in result.message
        assert registry.knowledge.document_store.get(ID) == held
        assert is_redaction_tombstone(held)

    @pytest.mark.parametrize("value", FALSY_NOT_BOOLEANS, ids=repr)
    def test_a_set_non_boolean_over_a_live_document_is_refused_too(
        self, registry: StoreRegistry, value: object
    ) -> None:
        """The refusal is about the value, not about the row it would reach."""
        result = build_curate_executor(registry, clearance="restricted").execute(
            _from_args(reingest_redacted=value)
        )

        assert result.status is CommandStatus.REJECTED
        assert registry.knowledge.document_store.get(ID) is None

    @pytest.mark.parametrize("value", FALSY_NOT_BOOLEANS, ids=repr)
    def test_a_refused_value_is_not_answered_as_a_replay_of_the_unflagged_write(
        self, registry: StoreRegistry, value: object
    ) -> None:
        """Defective subject: leave the value out of the digest and the retry
        below answers DUPLICATE (the flag silently ignored) instead of the
        refusal."""
        executor = build_curate_executor(registry, clearance="restricted")
        first = executor.execute(_from_args())
        assert first.status is CommandStatus.SUCCESS

        replay = build_curate_executor(registry, clearance="restricted").execute(
            _from_args(reingest_redacted=value)
        )

        assert replay.status is CommandStatus.REJECTED
        assert "must be a boolean" in replay.message

    def test_false_is_the_unflagged_write_and_is_refused_over_a_tombstone(
        self, registry: StoreRegistry
    ) -> None:
        make_tombstone(
            registry.knowledge.document_store, resolve_vector_store(registry), ID
        )

        result = build_curate_executor(registry, clearance="restricted").execute(
            _from_args(reingest_redacted=False)
        )

        assert result.status is CommandStatus.REJECTED
        assert "--reingest-redacted" in result.message
        assert is_redaction_tombstone(registry.knowledge.document_store.get(ID))
