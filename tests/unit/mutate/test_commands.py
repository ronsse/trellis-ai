"""Tests for mutation command schemas."""

from __future__ import annotations

import pytest

from trellis.mutate.commands import (
    BatchStrategy,
    Command,
    CommandBatch,
    CommandResult,
    CommandStatus,
    Operation,
    OperationRegistry,
)


class TestOperation:
    def test_all_operations_are_strings(self) -> None:
        for op in Operation:
            assert isinstance(op, str)
            assert "." in op  # all ops are dotted

    def test_operation_count(self) -> None:
        # 17 base + OBSERVATION_RECORD + MEASUREMENT_RECORD (Item 1 Phase 1)
        # + RETENTION_RESTORE (adr-retention-prune.md §6 — the governed
        # inverse of retention.prune, without which phase-one archival's
        # "walk it back by re-stamping" claim has no sanctioned path)
        # + ALIAS_UPSERT (governed alias/backfill writes)
        assert len(Operation) == 21


class TestCommand:
    def test_creates_with_defaults(self) -> None:
        cmd = Command(
            operation=Operation.ENTITY_CREATE,
            args={"entity_type": "service", "name": "auth"},
        )
        assert cmd.command_id
        assert cmd.operation == Operation.ENTITY_CREATE
        assert cmd.requested_by == "unknown"

    def test_with_idempotency_key(self) -> None:
        cmd = Command(
            operation=Operation.TRACE_INGEST,
            args={"trace": {}},
            idempotency_key="unique-key-123",
            requested_by="agent-1",
        )
        assert cmd.idempotency_key == "unique-key-123"

    def test_with_target(self) -> None:
        cmd = Command(
            operation=Operation.ENTITY_UPDATE,
            target_id="ent_123",
            target_type="entity",
            args={"entity_id": "ent_123", "name": "new-name"},
        )
        assert cmd.target_id == "ent_123"

    def test_forbids_extra_fields(self) -> None:
        with pytest.raises(ValueError, match="extra"):
            Command(operation=Operation.ENTITY_CREATE, args={}, bogus="nope")  # type: ignore[call-arg]


class TestCommandResult:
    def test_success_result(self) -> None:
        r = CommandResult(
            command_id="cmd_1",
            status=CommandStatus.SUCCESS,
            operation=Operation.ENTITY_CREATE,
            created_id="ent_new",
        )
        assert r.status == CommandStatus.SUCCESS
        assert r.created_id == "ent_new"

    def test_rejected_result(self) -> None:
        r = CommandResult(
            command_id="cmd_1",
            status=CommandStatus.REJECTED,
            operation=Operation.PRECEDENT_PROMOTE,
            message="Policy requires approval",
        )
        assert r.status == CommandStatus.REJECTED


class TestCommandBatch:
    def test_creates_batch(self) -> None:
        cmds = [
            Command(
                operation=Operation.ENTITY_CREATE,
                args={"entity_type": "service", "name": "a"},
            ),
            Command(
                operation=Operation.ENTITY_CREATE,
                args={"entity_type": "service", "name": "b"},
            ),
        ]
        batch = CommandBatch(commands=cmds)
        assert batch.batch_id
        assert len(batch.commands) == 2
        assert batch.strategy == BatchStrategy.STOP_ON_ERROR

    def test_continue_on_error_strategy(self) -> None:
        batch = CommandBatch(commands=[], strategy=BatchStrategy.CONTINUE_ON_ERROR)
        assert batch.strategy == BatchStrategy.CONTINUE_ON_ERROR


class TestOperationRegistry:
    @pytest.fixture
    def registry(self) -> OperationRegistry:
        return OperationRegistry()

    def test_validates_valid_command(self, registry: OperationRegistry) -> None:
        cmd = Command(
            operation=Operation.ENTITY_CREATE,
            args={"entity_type": "service", "name": "auth"},
        )
        valid, errors = registry.validate(cmd)
        assert valid is True
        assert errors == []

    def test_rejects_missing_args(self, registry: OperationRegistry) -> None:
        cmd = Command(
            operation=Operation.ENTITY_CREATE,
            args={"entity_type": "service"},
        )  # missing "name"
        valid, errors = registry.validate(cmd)
        assert valid is False
        assert "name" in errors[0]

    def test_allows_extra_args(self, registry: OperationRegistry) -> None:
        cmd = Command(
            operation=Operation.ENTITY_CREATE,
            args={"entity_type": "s", "name": "a", "extra": "ok"},
        )
        valid, _errors = registry.validate(cmd)
        assert valid is True

    def test_get_required_args(self, registry: OperationRegistry) -> None:
        required = registry.get_required_args(Operation.LINK_CREATE)
        assert "source_id" in required
        assert "target_id" in required
        assert "edge_kind" in required

    def test_all_operations_registered(self, registry: OperationRegistry) -> None:
        for op in Operation:
            required = registry.get_required_args(op)
            assert isinstance(required, set)

    def test_validates_all_ops_with_correct_args(
        self, registry: OperationRegistry
    ) -> None:
        # Every operation should validate when all required args provided.
        # FEEDBACK_RECORD's "rating" is bound to [0.0, 1.0] below, so the
        # generic "test_value" string stand-in would fail it; give that one
        # operation a real in-range value instead.
        for op in Operation:
            required = registry.get_required_args(op)
            args = dict.fromkeys(required, "test_value")
            if op is Operation.FEEDBACK_RECORD:
                args["rating"] = 0.5
            cmd = Command(operation=op, args=args)
            valid, errors = registry.validate(cmd)
            assert valid is True, f"{op}: {errors}"


class TestFeedbackRecordRatingBound:
    """``feedback.record``'s ``rating`` must be a real number in [0.0, 1.0]."""

    @pytest.fixture
    def registry(self) -> OperationRegistry:
        return OperationRegistry()

    @pytest.mark.parametrize(
        "rating",
        [
            float("nan"),
            float("inf"),
            float("-inf"),
            -0.1,
            1.1,
            5.0,
            pytest.param(10**400, id="int-beyond-float"),
            True,
            False,
            "0.7",
            None,
        ],
    )
    def test_rejects_an_out_of_range_or_non_numeric_rating(
        self, registry: OperationRegistry, rating: object
    ) -> None:
        cmd = Command(
            operation=Operation.FEEDBACK_RECORD,
            args={"target_id": "t1", "rating": rating},
        )
        valid, errors = registry.validate(cmd)
        assert valid is False
        assert len(errors) == 1
        assert "rating" in errors[0]

    @pytest.mark.parametrize("rating", [0.0, 0.7, 1.0, 1, 0])
    def test_accepts_an_in_range_rating(
        self, registry: OperationRegistry, rating: object
    ) -> None:
        cmd = Command(
            operation=Operation.FEEDBACK_RECORD,
            args={"target_id": "t1", "rating": rating},
        )
        valid, errors = registry.validate(cmd)
        assert valid is True
        assert errors == []

    def test_other_operations_are_unaffected(self, registry: OperationRegistry) -> None:
        """The bound is ``feedback.record``-only; it must not leak elsewhere."""
        cmd = Command(
            operation=Operation.ENTITY_CREATE,
            args={"entity_type": "service", "name": "auth", "rating": 5.0},
        )
        valid, _errors = registry.validate(cmd)
        assert valid is True
