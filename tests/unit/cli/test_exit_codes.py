"""C2 Phase 6 — explicit CLI exit codes (`docs/design/adr-cli-exit-codes.md`).

Covers:
- The five canonical exit-code constants are present and stable.
- CLI swallow sites cited in the silent-fallback audit now surface
  structured failures (log signal or typed exit) rather than degrading
  to empty results.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from trellis.core.error_sanitize import SUPPRESSED_MARKER
from trellis.mutate.commands import CommandResult, CommandStatus, Operation
from trellis_cli import exit_codes
from trellis_cli.extract_refresh import _emit_refresh_event, _snapshot_entities


class TestExitCodeMap:
    """The five canonical codes must stay stable — operators script
    against them. Changing any of these is a breaking change."""

    def test_codes_have_documented_values(self) -> None:
        assert exit_codes.EXIT_OK == 0
        assert exit_codes.EXIT_INTERNAL == 1
        assert exit_codes.EXIT_VALIDATION == 2
        assert exit_codes.EXIT_POLICY == 3
        assert exit_codes.EXIT_IDEMPOTENCY == 4
        assert exit_codes.EXIT_STORE == 5

    def test_codes_are_unique(self) -> None:
        values = {
            exit_codes.EXIT_OK,
            exit_codes.EXIT_INTERNAL,
            exit_codes.EXIT_VALIDATION,
            exit_codes.EXIT_POLICY,
            exit_codes.EXIT_IDEMPOTENCY,
            exit_codes.EXIT_STORE,
        }
        assert len(values) == 6


class TestRefusalExitCode:
    """A refused or failed write exits by its reason, for curate and ingest alike."""

    def test_the_arg_check_refusal_exits_2_beside_a_store_failure(self) -> None:
        """Every CLI site supplies its args, so map the executor's own result."""
        from trellis.errors import StoreError
        from trellis.mutate.commands import Command
        from trellis.mutate.executor import MutationExecutor

        handler = MagicMock()
        handler.handle.side_effect = StoreError("synthetic outage", store="graph")
        executor = MutationExecutor(handlers={Operation.ENTITY_CREATE: handler})
        refused = executor.execute(
            Command(operation=Operation.ENTITY_CREATE, args={"name": "syn-b"})
        )
        failed = executor.execute(
            Command(
                operation=Operation.ENTITY_CREATE,
                args={"entity_type": "service", "name": "syn-b"},
            )
        )
        assert [exit_codes.refusal_exit_code(r) for r in (refused, failed)] == [
            exit_codes.EXIT_VALIDATION,
            exit_codes.EXIT_STORE,
        ]

    @pytest.mark.parametrize(
        ("status", "metadata", "code"),
        [
            pytest.param(
                CommandStatus.REJECTED,
                {"rejection_reason": "policy_violation"},
                exit_codes.EXIT_POLICY,
                id="policy",
            ),
            pytest.param(
                CommandStatus.REJECTED,
                {"rejection_reason": "immutable_core"},
                exit_codes.EXIT_VALIDATION,
                id="other-refusal",
            ),
            pytest.param(
                CommandStatus.REJECTED, {}, exit_codes.EXIT_VALIDATION, id="no-reason"
            ),
            pytest.param(CommandStatus.FAILED, {}, exit_codes.EXIT_STORE, id="failed"),
            # The reason is read only on a refusal: a FAILED result is a store
            # outcome whatever its metadata says.
            pytest.param(
                CommandStatus.FAILED,
                {"rejection_reason": "policy_violation"},
                exit_codes.EXIT_STORE,
                id="failed-with-reason",
            ),
        ],
    )
    def test_maps_status_and_reason(
        self, status: CommandStatus, metadata: dict[str, str], code: int
    ) -> None:
        result = CommandResult(
            command_id="c",
            status=status,
            operation=Operation.TRACE_INGEST,
            metadata=metadata,
        )
        assert exit_codes.refusal_exit_code(result) == code


#: A store error quoting its DSN, credentials and all.
_DRIVER = "no route to postgres://ops:pw@db:5432/kb"

_NAME_PARTIAL = pytest.mark.parametrize("name_partial", [False, True])


def _batch(*answers: tuple[CommandStatus, str]) -> list[CommandResult]:
    """Results answering a batch's commands in order, each with its message."""
    return [
        CommandResult(
            command_id=f"cmd-{i}",
            status=status,
            operation=Operation.ENTITY_CREATE,
            message=message,
        )
        for i, (status, message) in enumerate(answers)
    ]


class TestBatchOutcome:
    """One reading of a batch's results for ingest and extract alike (#687, #730)."""

    @_NAME_PARTIAL
    @pytest.mark.parametrize(
        "statuses",
        [
            pytest.param([CommandStatus.SUCCESS] * 2, id="all-success"),
            pytest.param([CommandStatus.DUPLICATE] * 2, id="duplicate-only"),
            pytest.param([CommandStatus.SUCCESS, CommandStatus.DUPLICATE], id="both"),
            pytest.param([], id="empty"),
        ],
    )
    def test_a_batch_with_no_failure_is_done(
        self, statuses: list[CommandStatus], name_partial: bool
    ) -> None:
        """A duplicate replays a write that landed; an empty batch refused nothing."""
        results = _batch(*((s, "done") for s in statuses))
        outcome = exit_codes.batch_outcome(
            results, done="ingested", name_partial=name_partial
        )
        assert outcome == (None, None, {"status": "ingested"})

    @_NAME_PARTIAL
    def test_a_batch_refused_throughout_is_refused_by_its_first_result(
        self, name_partial: bool
    ) -> None:
        results = _batch(
            (CommandStatus.REJECTED, "first"), (CommandStatus.FAILED, "second")
        )
        outcome = exit_codes.batch_outcome(
            results, done="ingested", name_partial=name_partial
        )
        assert outcome.refusal is outcome.first_failure is results[0]
        assert outcome.status == {"status": "error", "message": "first"}

    @_NAME_PARTIAL
    def test_a_mixed_batch_is_done_and_names_its_first_failure_on_request(
        self, name_partial: bool
    ) -> None:
        results = _batch(
            (CommandStatus.SUCCESS, "done"),
            (CommandStatus.REJECTED, "first"),
            (CommandStatus.FAILED, "second"),
        )
        outcome = exit_codes.batch_outcome(
            results, done="backfilled", name_partial=name_partial
        )
        assert outcome.refusal is None
        assert outcome.first_failure is results[1]
        named = {"message": "first"} if name_partial else {}
        assert outcome.status == {"status": "backfilled", **named}

    @pytest.mark.parametrize(
        ("answers", "status"),
        [
            pytest.param([(CommandStatus.FAILED, _DRIVER)], "error", id="refused"),
            pytest.param(
                [(CommandStatus.SUCCESS, "done"), (CommandStatus.FAILED, _DRIVER)],
                "refreshed",
                id="partial",
            ),
        ],
    )
    def test_driver_text_stays_out_of_the_payload(
        self, answers: list[tuple[CommandStatus, str]], status: str
    ) -> None:
        """The payload is an artifact; the result keeps the text for the human form."""
        results = _batch(*answers)
        outcome = exit_codes.batch_outcome(results, done="refreshed", name_partial=True)
        assert outcome.status == {"status": status, "message": SUPPRESSED_MARKER}
        assert outcome.first_failure is results[-1]
        assert results[-1].message == _DRIVER


class TestExtractRefreshSnapshotErrors:
    """Per-entity snapshot errors no longer hit a bare ``Exception``
    swallow — the catch is narrowed and each failure logs."""

    def test_snapshot_failure_logs_and_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from trellis_cli import extract_refresh as er

        captured: list[tuple[str, dict]] = []

        def _debug(event: str, **kw: object) -> None:
            captured.append((event, dict(kw)))

        monkeypatch.setattr(er._logger, "debug", _debug)

        graph = MagicMock()
        graph.get_node.side_effect = RuntimeError("backend down")
        registry = MagicMock()
        registry.knowledge.graph_store = graph

        out = _snapshot_entities(registry, ["ent_1", "ent_2"])
        assert out == {"ent_1": None, "ent_2": None}
        events = [e for e, _ in captured]
        assert events.count("extract_refresh_snapshot_get_node_failed") == 2

    def test_snapshot_unexpected_type_still_propagates(self) -> None:
        """A SystemExit or other non-listed exception must propagate so
        truly unexpected failures aren't masked by the narrowed catch."""
        graph = MagicMock()
        graph.get_node.side_effect = SystemExit("boom")
        registry = MagicMock()
        registry.knowledge.graph_store = graph

        with pytest.raises(SystemExit):
            _snapshot_entities(registry, ["ent_1"])


class TestExtractRefreshEmitErrors:
    """The TAGS_REFRESHED emit no longer catches bare ``Exception``."""

    def test_emit_failure_logs_with_error_type(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from trellis_cli import extract_refresh as er

        captured: list[tuple[str, dict]] = []

        def _exception(event: str, **kw: object) -> None:
            captured.append((event, dict(kw)))

        monkeypatch.setattr(er._logger, "exception", _exception)

        registry = MagicMock()
        registry.operational.event_log.emit.side_effect = OSError("disk full")
        _emit_refresh_event(
            registry,
            "ent_1",
            "service",
            {"changed": {"description": ["old", "new"]}},
            source_name="dbt",
            extractor_used="dbt-manifest",
        )
        events = [(e, kw.get("error_type")) for e, kw in captured]
        assert ("extract_refresh_emit_failed", "OSError") in events
