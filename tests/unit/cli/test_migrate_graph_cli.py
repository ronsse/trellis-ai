"""Smoke tests for the ``trellis admin migrate-graph`` CLI wrapper.

Exercises the end-to-end CLI path against two SQLite databases — proves
the YAML config-loading, output formatting, and exit-code branches work.
The library-level tests in ``tests/unit/migrate/`` cover the migration
semantics; this file pins the CLI surface only.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.cli_output import plain
from trellis.stores.sqlite.graph import SQLiteGraphStore
from trellis_cli.admin import admin_app
from trellis_cli.exit_codes import EXIT_STORE

#: What a real read-only SQLite file raises on a write.
_READONLY_DB = "attempt to write a readonly database"


def _write_sqlite_config(tmp_path: Path, name: str) -> tuple[Path, Path]:
    db_path = tmp_path / f"{name}.db"
    config_path = tmp_path / f"{name}-config.yaml"
    config_path.write_text(
        f"graph:\n  backend: sqlite\n  db_path: {db_path}\n",
        encoding="utf-8",
    )
    return config_path, db_path


@pytest.fixture(autouse=True)
def _isolated_config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the operator's real ~/.trellis out of these tests.

    The command under test takes explicit --from-config/--to-config, but
    the meta-trace wiring around every CLI invocation opens the *default*
    registry — with a real config dir present (e.g. postgres backends and
    no DSN in the test env) that construction fails and poisons the exit
    code. Point the env at an empty tmp dir so the wiring degrades
    gracefully instead.
    """
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "trellis-config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(tmp_path / "trellis-data"))


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_migrate_graph_text_output(tmp_path: Path, runner: CliRunner) -> None:
    src_config, src_db = _write_sqlite_config(tmp_path, "src")
    dst_config, dst_db = _write_sqlite_config(tmp_path, "dst")

    src = SQLiteGraphStore(db_path=src_db)
    src.upsert_node("n1", node_type="X", properties={"k": "v"})
    src.close()

    result = runner.invoke(
        admin_app,
        [
            "migrate-graph",
            "--from-config",
            str(src_config),
            "--to-config",
            str(dst_config),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "nodes=1/1" in plain(result.output)

    # Verify the destination actually has the node.
    dst = SQLiteGraphStore(db_path=dst_db)
    assert dst.get_node("n1") is not None
    dst.close()


def test_migrate_graph_dry_run(tmp_path: Path, runner: CliRunner) -> None:
    src_config, src_db = _write_sqlite_config(tmp_path, "src")
    dst_config, dst_db = _write_sqlite_config(tmp_path, "dst")

    src = SQLiteGraphStore(db_path=src_db)
    src.upsert_node("n1", node_type="X", properties={})
    src.close()

    result = runner.invoke(
        admin_app,
        [
            "migrate-graph",
            "--from-config",
            str(src_config),
            "--to-config",
            str(dst_config),
            "--dry-run",
        ],
    )
    assert result.exit_code == 0
    assert "DRY RUN" in result.output

    dst = SQLiteGraphStore(db_path=dst_db)
    # Dry-run wrote nothing.
    assert dst.get_node("n1") is None
    dst.close()


def test_migrate_graph_json_output(tmp_path: Path, runner: CliRunner) -> None:
    src_config, src_db = _write_sqlite_config(tmp_path, "src")
    dst_config, _ = _write_sqlite_config(tmp_path, "dst")

    src = SQLiteGraphStore(db_path=src_db)
    src.upsert_node("n1", node_type="X", properties={})
    src.close()

    result = runner.invoke(
        admin_app,
        [
            "migrate-graph",
            "--from-config",
            str(src_config),
            "--to-config",
            str(dst_config),
            "--format",
            "json",
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["nodes_read"] == 1
    assert payload["nodes_written"] == 1
    assert payload["dry_run"] is False
    assert payload["errors"] == []


def test_migrate_graph_missing_config_file(tmp_path: Path, runner: CliRunner) -> None:
    dst_config, _ = _write_sqlite_config(tmp_path, "dst")
    missing = tmp_path / "does-not-exist.yaml"

    result = runner.invoke(
        admin_app,
        [
            "migrate-graph",
            "--from-config",
            str(missing),
            "--to-config",
            str(dst_config),
        ],
    )
    assert result.exit_code != 0
    assert "not found" in result.output.lower()


def test_migrate_graph_invalid_config_shape(tmp_path: Path, runner: CliRunner) -> None:
    bad_config = tmp_path / "bad.yaml"
    bad_config.write_text("not_graph: hello\n", encoding="utf-8")
    dst_config, _ = _write_sqlite_config(tmp_path, "dst")

    result = runner.invoke(
        admin_app,
        [
            "migrate-graph",
            "--from-config",
            str(bad_config),
            "--to-config",
            str(dst_config),
        ],
    )
    assert result.exit_code != 0
    assert "graph" in result.output.lower()


def test_migrate_graph_capacity_exceeded_returns_nonzero(
    tmp_path: Path, runner: CliRunner
) -> None:
    src_config, src_db = _write_sqlite_config(tmp_path, "src")
    dst_config, _ = _write_sqlite_config(tmp_path, "dst")

    src = SQLiteGraphStore(db_path=src_db)
    for i in range(5):
        src.upsert_node(f"n{i}", node_type="X", properties={})
    src.close()

    result = runner.invoke(
        admin_app,
        [
            "migrate-graph",
            "--from-config",
            str(src_config),
            "--to-config",
            str(dst_config),
            "--max-nodes",
            "2",
        ],
    )
    assert result.exit_code != 0
    assert "max_nodes" in result.output or "exceeding" in result.output


class TestFailedMigrationExitsTheSameWayOnBothSurfaces:
    """#437: the exit code must not depend on ``--format``.

    ``raise typer.Exit(code=EXIT_STORE)`` used to sit inside the ``else``
    (text) arm of ``if output_format == "json"``, so a failed migration
    exited ``5`` for a human reading prose and ``0`` for the script that
    parsed the JSON — the surface built for machine consumption was the one
    that reported success for a failed store migration.

    The failure is produced the way the migrator actually produces one:
    ``--continue-on-error`` captures per-step write failures into
    ``report.errors`` rather than raising, which is the only path that
    reaches the branch under test. Every other failure mode
    (capacity exceeded, ``MigrationStepError``) exits from inside the
    ``try`` above it and never gets that far.
    """

    @staticmethod
    def _failing_migration_argv(src_config: Path, dst_config: Path) -> list[str]:
        return [
            "migrate-graph",
            "--from-config",
            str(src_config),
            "--to-config",
            str(dst_config),
            "--continue-on-error",
        ]

    @pytest.fixture
    def failing_migration_argv(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> list[str]:
        """Argv for a seeded source and a destination whose node writes fail.

        The source is populated *before* the patch, so reads still work and
        the run gets far enough to record a write failure per node. The
        exception is the one a real read-only SQLite file raises, so the
        migrator's ``except Exception`` catches exactly what it would in
        production rather than a stand-in.
        """
        src_config, src_db = _write_sqlite_config(tmp_path, "src")
        dst_config, _ = _write_sqlite_config(tmp_path, "dst")

        src = SQLiteGraphStore(db_path=src_db)
        src.upsert_node("n1", node_type="X", properties={})
        src.close()

        def _refuse_write(*_args: object, **_kwargs: object) -> None:
            raise sqlite3.OperationalError(_READONLY_DB)

        monkeypatch.setattr(SQLiteGraphStore, "upsert_node", _refuse_write)
        return self._failing_migration_argv(src_config, dst_config)

    def test_json_reports_the_failure_in_the_exit_code(
        self, failing_migration_argv: list[str], runner: CliRunner
    ) -> None:
        result = runner.invoke(admin_app, [*failing_migration_argv, "--format", "json"])

        assert result.exit_code == EXIT_STORE, (
            f"a failed migration exited {result.exit_code} on --format json; "
            f"the machine surface must not report success (#437)"
        )

    def test_json_payload_still_parses_and_carries_the_errors(
        self, failing_migration_argv: list[str], runner: CliRunner
    ) -> None:
        """The exit code must not be bought by breaking the JSON contract.

        #403 and #422 were both about ``--format json`` handing back
        something a consumer could not parse; fixing an exit code by
        emptying or corrupting the payload would trade one defect for the
        other. ``status`` is asserted alongside because the ADR names it as
        the key JSON callers branch on, and a ``status`` that disagreed with
        the exit code would be a third signal rather than a fix.
        """
        result = runner.invoke(admin_app, [*failing_migration_argv, "--format", "json"])

        payload = json.loads(result.stdout)
        assert payload["status"] == "error"
        assert payload["errors"], "the failed steps must survive into the payload"
        assert payload["errors"][0]["target"].startswith("upsert_node:")
        assert payload["step_failures"], "structured failures must survive too"
        assert payload["nodes_read"] == 1
        assert payload["nodes_written"] == 0

    def test_text_exit_code_is_unchanged(
        self, failing_migration_argv: list[str], runner: CliRunner
    ) -> None:
        result = runner.invoke(admin_app, failing_migration_argv)

        assert result.exit_code == EXIT_STORE
        assert "Errors:" in result.output

    def test_a_clean_migration_still_says_ok_on_both_surfaces(
        self, tmp_path: Path, runner: CliRunner
    ) -> None:
        """The other half of the parity claim: success must stay success.

        Hoisting the exit out of the format branch is only correct if it
        fires on the failure flag and nothing else.
        """
        src_config, src_db = _write_sqlite_config(tmp_path, "src")
        src = SQLiteGraphStore(db_path=src_db)
        src.upsert_node("n1", node_type="X", properties={})
        src.close()

        # A fresh destination per invocation: reusing one would make the
        # second run an idempotent no-op, which exits 0 for a different
        # reason than the one under test.
        json_dst, _ = _write_sqlite_config(tmp_path, "dst-json")
        json_result = runner.invoke(
            admin_app,
            [*self._failing_migration_argv(src_config, json_dst), "--format", "json"],
        )
        assert json_result.exit_code == 0, json_result.output
        assert json.loads(json_result.stdout)["status"] == "ok"

        text_dst, _ = _write_sqlite_config(tmp_path, "dst-text")
        text_result = runner.invoke(
            admin_app, self._failing_migration_argv(src_config, text_dst)
        )
        assert text_result.exit_code == 0, text_result.output
