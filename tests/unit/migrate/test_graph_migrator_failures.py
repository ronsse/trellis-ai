"""Failure-injection tests for ``GraphMigrator`` — C2 Phase 4.

The migrator's default behavior is *raise on first failing step*. Opt-in
``strategy=BatchStrategy.CONTINUE_ON_ERROR`` captures each failure into
``MigrationReport.step_failures`` and continues. These tests inject
controlled exceptions at each step and assert both modes.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from trellis.migrate import (
    GraphMigrator,
    MigrationReport,
    MigrationStepError,
    MigrationStepFailure,
)
from trellis.mutate.commands import BatchStrategy
from trellis.stores.sqlite.graph import SQLiteGraphStore


@pytest.fixture
def source_store(tmp_path: Path) -> SQLiteGraphStore:
    store = SQLiteGraphStore(db_path=tmp_path / "src.db")
    # Seed two nodes so per-node iteration has something to do.
    store.upsert_node("n1", node_type="X", properties={"name": "one"})
    store.upsert_node("n2", node_type="X", properties={"name": "two"})
    return store


@pytest.fixture
def dest_store(tmp_path: Path) -> SQLiteGraphStore:
    return SQLiteGraphStore(db_path=tmp_path / "dst.db")


class TestDefaultRaisesOnStepFailure:
    """Default ``BatchStrategy.STOP_ON_ERROR`` must raise on the first failure."""

    def test_upsert_node_failure_raises_migration_step_error(
        self, source_store: SQLiteGraphStore, dest_store: SQLiteGraphStore
    ) -> None:
        migrator = GraphMigrator(source_store, dest_store)
        with (
            patch.object(
                dest_store,
                "upsert_node",
                side_effect=RuntimeError("dest disk full"),
            ),
            pytest.raises(MigrationStepError) as excinfo,
        ):
            migrator.run()
        # Step + entity preserved, original chained via __cause__.
        assert excinfo.value.step == "upsert_node"
        assert excinfo.value.entity_id in {"n1", "n2"}
        assert isinstance(excinfo.value.__cause__, RuntimeError)
        assert "dest disk full" in str(excinfo.value.__cause__)

    def test_get_node_failure_raises(
        self, source_store: SQLiteGraphStore, dest_store: SQLiteGraphStore
    ) -> None:
        migrator = GraphMigrator(source_store, dest_store)
        with (
            patch.object(
                dest_store,
                "get_node",
                side_effect=RuntimeError("destination unreachable"),
            ),
            pytest.raises(MigrationStepError) as excinfo,
        ):
            migrator.run()
        assert excinfo.value.step == "get_node"

    def test_read_nodes_failure_raises(
        self, dest_store: SQLiteGraphStore, tmp_path: Path
    ) -> None:
        src = SQLiteGraphStore(db_path=tmp_path / "src.db")
        src.upsert_node("n1", node_type="X", properties={})
        migrator = GraphMigrator(src, dest_store)
        with (
            patch.object(src, "query", side_effect=RuntimeError("source closed")),
            pytest.raises(MigrationStepError) as excinfo,
        ):
            migrator.run()
        assert excinfo.value.step == "read_nodes"
        # Entity id is None for the top-level read step.
        assert excinfo.value.entity_id is None


class TestContinueOnErrorCapturesFailures:
    """``BatchStrategy.CONTINUE_ON_ERROR`` records failures into the report."""

    def test_one_step_failure_continues_and_records(
        self, source_store: SQLiteGraphStore, dest_store: SQLiteGraphStore
    ) -> None:
        migrator = GraphMigrator(source_store, dest_store)
        # Fail upsert for n1 only; n2 should still migrate.
        original_upsert = dest_store.upsert_node

        def upsert_with_one_failure(node_id: str, **kw: object) -> str:
            if node_id == "n1":
                msg = "n1 forbidden"
                raise RuntimeError(msg)
            return original_upsert(node_id, **kw)  # type: ignore[arg-type]

        with patch.object(
            dest_store, "upsert_node", side_effect=upsert_with_one_failure
        ):
            report = migrator.run(strategy=BatchStrategy.CONTINUE_ON_ERROR)

        # Failure captured, migration continued.
        assert len(report.step_failures) == 1
        failure = report.step_failures[0]
        assert isinstance(failure, MigrationStepFailure)
        assert failure.step == "upsert_node"
        assert failure.entity_id == "n1"
        assert failure.error_class == "RuntimeError"
        assert "n1 forbidden" in failure.message
        assert "RuntimeError" in failure.traceback
        # n2 still wrote through.
        assert report.nodes_written == 1
        assert dest_store.get_node("n2") is not None

    def test_multiple_step_failures_all_captured(
        self, source_store: SQLiteGraphStore, dest_store: SQLiteGraphStore
    ) -> None:
        migrator = GraphMigrator(source_store, dest_store)
        with patch.object(
            dest_store,
            "upsert_node",
            side_effect=RuntimeError("dest is down"),
        ):
            report = migrator.run(strategy=BatchStrategy.CONTINUE_ON_ERROR)

        # Both nodes failed to upsert; both failures recorded.
        assert len(report.step_failures) == 2
        steps = {f.step for f in report.step_failures}
        assert steps == {"upsert_node"}
        entities = {f.entity_id for f in report.step_failures}
        assert entities == {"n1", "n2"}
        # No nodes written.
        assert report.nodes_written == 0


class TestMigrationReportShape:
    """``step_failures`` field defaults to empty list and round-trips cleanly."""

    def test_default_report_has_empty_step_failures(self) -> None:
        report = MigrationReport()
        assert report.step_failures == []

    def test_step_failure_serializes_via_dataclass(self) -> None:
        # ``dataclasses.asdict`` is the CLI JSON path; the new field must
        # come through without a custom serializer.
        from dataclasses import asdict

        report = MigrationReport(
            step_failures=[
                MigrationStepFailure(
                    step="upsert_node",
                    entity_id="abc",
                    error_class="RuntimeError",
                    message="boom",
                    traceback="Traceback...\n",
                ),
            ],
        )
        payload = asdict(report)
        assert payload["step_failures"] == [
            {
                "step": "upsert_node",
                "entity_id": "abc",
                "error_class": "RuntimeError",
                "message": "boom",
                "traceback": "Traceback...\n",
            },
        ]

    def test_summary_includes_step_failure_count(self) -> None:
        report = MigrationReport(
            nodes_read=2,
            nodes_written=1,
            step_failures=[
                MigrationStepFailure(
                    step="upsert_node",
                    entity_id="n1",
                    error_class="RuntimeError",
                    message="boom",
                    traceback="",
                ),
            ],
        )
        assert "step_failures=1" in report.summary()


class TestSequentialIsTreatedLikeStopOnError:
    """``BatchStrategy.SEQUENTIAL`` and ``STOP_ON_ERROR`` both raise.

    Migration is inherently sequential — SEQUENTIAL has no separate
    semantics here and is mapped to the raise-on-first behavior.
    """

    def test_sequential_strategy_raises(
        self, source_store: SQLiteGraphStore, dest_store: SQLiteGraphStore
    ) -> None:
        migrator = GraphMigrator(source_store, dest_store)
        with (
            patch.object(dest_store, "upsert_node", side_effect=RuntimeError("nope")),
            pytest.raises(MigrationStepError),
        ):
            migrator.run(strategy=BatchStrategy.SEQUENTIAL)


class TestErrorsListBackwardsCompat:
    """Legacy ``errors`` list keeps working alongside ``step_failures``."""

    def test_continue_on_error_populates_both_lists(
        self, source_store: SQLiteGraphStore, dest_store: SQLiteGraphStore
    ) -> None:
        # The legacy ``errors`` field (list[tuple[str, str]]) keeps being
        # populated so existing CLI output paths still work.
        migrator = GraphMigrator(source_store, dest_store)
        with patch.object(
            dest_store, "upsert_node", side_effect=ValueError("validation")
        ):
            report = migrator.run(strategy=BatchStrategy.CONTINUE_ON_ERROR)
        assert len(report.errors) == 2
        assert all("ValueError" in msg for _, msg in report.errors)
        assert len(report.step_failures) == 2
