"""Tests for mutation command schemas."""

from __future__ import annotations

import pytest

from trellis.mutate.commands import (
    ID_BEARING_ARG_KEYS,
    BatchStrategy,
    Command,
    CommandBatch,
    CommandResult,
    CommandStatus,
    Operation,
    OperationRegistry,
)

#: Every (operation, arg key) pair ID_BEARING_ARG_KEYS declares, flattened
#: for parametrization. Sorted so a parametrize id is stable across runs.
_ID_BEARING_PAIRS = sorted(
    (op, key) for op, keys in ID_BEARING_ARG_KEYS.items() for key in keys
)


def _base_args(registry: OperationRegistry, operation: Operation) -> dict[str, object]:
    """A minimal args dict that satisfies *operation*'s required schema,
    plus every optional id-bearing key it declares (so a test can target
    an optional key like ``feedback.record``'s ``pack_id`` the same way
    as a required one)."""
    required = registry.get_required_args(operation)
    args: dict[str, object] = dict.fromkeys(required, "test_value")
    if operation is Operation.FEEDBACK_RECORD:
        args["rating"] = 0.5
    for key in ID_BEARING_ARG_KEYS.get(operation, frozenset()):
        args.setdefault(key, "test_value")
    return args


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
        # + SCHEDULE_UPDATE + JOB_RUN_REQUEST (admin-only schedule-registry
        # writes, handled only by trellis.mutate.build_schedule_executor,
        # never create_curate_handlers; see
        # trellis/mutate/schedule_handlers.py)
        # + SETTINGS_UPDATE (governed settings-store write; null value
        # means "remove the override" rather than a separate RESET verb)
        # - PRECEDENT_UPDATE (removed: this verb had no handler anywhere
        # in src/ since the initial commit — see docs/design/adr-retention
        # -prune.md's "Sibling unhandled verbs" row — so every submission
        # of it failed with "No handler registered" and there was no
        # writer to lose. EventType.PRECEDENT_UPDATED is removed alongside
        # it; that event type was never emitted either. Neither member is
        # carried forward into the report-only auto-promote evaluator
        # added here; its commit mode is refused via ConfigError, not via
        # this verb)
        # + CLASSIFY_RULE_APPLY + CLASSIFY_RULE_REVOKE (governed tag/domain
        # candidate review)
        # + PRECEDENT_DEMOTE (manual-trigger precedent demotion; an SCD-2
        # rewrite to Lifecycle.state="demoted", provenance re-derived from
        # the precedent's own precedent.promote MUTATION_EXECUTED event)
        # + PRECEDENT_REINSTATE (demote's own reversal verb, human-only:
        # the SCD-2 rewrite back to Lifecycle.state="current", required
        # because entity.update refuses to touch a precedent's lifecycle
        # directly — see EntityUpdateHandler)
        assert len(Operation) == 27


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


class TestIdBearingArgNulSafety:
    """A NUL byte or lone surrogate minted into an id-bearing ``args`` value
    (``entity.create``'s ``entity_id`` and siblings — see
    ``ID_BEARING_ARG_KEYS``'s docstring) is refused at Stage 1, the same
    posture ``Command.target_id``/``idempotency_key``/``target_type``
    already have. Refusing rather than scrubbing matters here specifically:
    a scrubbed id would no longer match the raw key a later lookup sends.
    """

    @pytest.fixture
    def registry(self) -> OperationRegistry:
        return OperationRegistry()

    @pytest.mark.parametrize(("operation", "key"), _ID_BEARING_PAIRS)
    @pytest.mark.parametrize("hazard", ["\x00", "\ud800"], ids=["nul", "surrogate"])
    def test_rejects_hazard_in_id_bearing_arg(
        self,
        registry: OperationRegistry,
        operation: Operation,
        key: str,
        hazard: str,
    ) -> None:
        args = _base_args(registry, operation)
        args[key] = f"bad{hazard}value"
        cmd = Command(operation=operation, args=args)

        valid, errors = registry.validate(cmd)

        assert valid is False
        assert any(repr(key) in e and "NUL byte or lone surrogate" in e for e in errors)

    @pytest.mark.parametrize(("operation", "key"), _ID_BEARING_PAIRS)
    def test_honest_value_in_id_bearing_arg_passes(
        self, registry: OperationRegistry, operation: Operation, key: str
    ) -> None:
        args = _base_args(registry, operation)
        cmd = Command(operation=operation, args=args)

        valid, errors = registry.validate(cmd)

        assert valid is True, f"{operation}/{key}: {errors}"
        assert errors == []

    @pytest.mark.parametrize(
        ("operation", "free_text_key"),
        [
            (Operation.ENTITY_CREATE, "name"),
            (Operation.PRECEDENT_DEMOTE, "reason"),
            (Operation.LABEL_ADD, "label"),
        ],
    )
    def test_hazard_in_free_text_arg_still_accepted(
        self,
        registry: OperationRegistry,
        operation: Operation,
        free_text_key: str,
    ) -> None:
        """Content, not an identifier: ``args`` is not scanned wholesale —
        only the keys ``ID_BEARING_ARG_KEYS`` names for that operation."""
        args = _base_args(registry, operation)
        args[free_text_key] = "bad\x00 name with a NUL"

        valid, errors = registry.validate(Command(operation=operation, args=args))

        assert valid is True
        assert errors == []

    def test_mapping_hand_read_floor(self) -> None:
        """Pinned to the exact count found by reading every handler in
        ``mutate/handlers.py`` (see ``ID_BEARING_ARG_KEYS``'s docstring for
        which operation/key pairs and why, and which were deliberately left
        out). Deleting an entry drops one of these counts and fails here —
        a silent shrink is the one failure mode this test exists to catch.
        """
        assert len(ID_BEARING_ARG_KEYS) == 10
        assert len(_ID_BEARING_PAIRS) == 12
