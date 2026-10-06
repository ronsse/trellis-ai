"""Smoke tests for the ``trellis admin migrate-graph`` CLI wrapper.

Exercises the end-to-end CLI path against two SQLite databases — proves
the YAML config-loading, output formatting, and exit-code branches work.
The library-level tests in ``tests/unit/migrate/`` cover the migration
semantics; this file pins the CLI surface only.
"""

from __future__ import annotations

import errno
import json
import os
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.cli_output import plain
from tests.unreadable_paths import (
    UNREADABLE_PATH_IDS,
    UNREADABLE_PATH_SHAPES,
    UnreadablePathShape,
    unreadable,
)
from trellis.core.error_sanitize import SUPPRESSED_MARKER
from trellis.stores.sqlite.graph import SQLiteGraphStore
from trellis_cli.admin import admin_app
from trellis_cli.exit_codes import EXIT_STORE

#: What a real read-only SQLite file raises on a write.
_READONLY_DB = "attempt to write a readonly database"

#: A synthetic row value shaped like the opaque token a real Postgres/
#: Neo4j/ArcadeDB duplicate-key message quotes (``DETAIL: Key (col)=(...)
#: already exists.``) — 40+ chars of ``[A-Za-z0-9+_-]`` trips the
#: sanitizer's long-opaque-token heuristic. Synthetic, not a real secret.
_SYNTHETIC_SECRET = "synthetic-secret-a1b2c3d4e5f6a1b2c3d4e5f6"  # noqa: S105 — test placeholder, not a real credential
_SYNTHETIC_STORE_ERROR = (
    'duplicate key value violates unique constraint "node_pkey" DETAIL: '
    f"Key (external_id)=({_SYNTHETIC_SECRET}) already exists."
)


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


#: ``--from-config`` files the loader cannot use, and a phrase its refusal
#: carries. ``None`` makes the path a directory.
_UNUSABLE_CONFIGS: dict[str, tuple[bytes | None, str]] = {
    "list": (b"- graph\n- backend\n", "must contain a 'graph:' block"),
    "scalar": (b"42\n", "must contain a 'graph:' block"),
    "bad_utf8": (
        b"graph:\n  backend: sqlite\n  db_path: \xff\n",
        "is not valid utf-8 text (byte offset 36)",
    ),
    "directory": (None, "Is a directory"),
}


@pytest.mark.parametrize("shape", list(_UNUSABLE_CONFIGS))
def test_migrate_graph_refuses_an_unusable_config(
    shape: str,
    tmp_path: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A config file the loader cannot use exits 2 with its reason.

    The path is relative, which keeps each message inside 80 columns so
    Rich cannot fold it mid-token, and its ``[cfg]`` segment is markup:
    Rich deletes it unless the message escapes the path.
    """
    content, expected = _UNUSABLE_CONFIGS[shape]
    monkeypatch.chdir(tmp_path)
    src = tmp_path / "[cfg]" / "src.yaml"
    src.parent.mkdir()
    if content is None:
        src.mkdir()
    else:
        src.write_bytes(content)
    dst_config, _ = _write_sqlite_config(tmp_path, "dst")

    result = runner.invoke(
        admin_app,
        [
            "migrate-graph",
            "--from-config",
            "[cfg]/src.yaml",
            "--to-config",
            str(dst_config),
        ],
    )

    assert result.exit_code == 2, result.output
    output = " ".join(plain(result.output).split())
    assert "[cfg]/src.yaml" in output
    assert expected in output


@pytest.mark.parametrize("shape", UNREADABLE_PATH_SHAPES, ids=UNREADABLE_PATH_IDS)
def test_migrate_graph_refuses_an_unreadable_config_path(
    shape: UnreadablePathShape,
    tmp_path: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A config path the loader cannot read exits 2 with the OS's reason.

    The loader used to check ``Path.exists()``, which called the first two
    shapes absent ("Config file not found") and re-raised the third as a
    traceback. ``unsearchable_parent`` skips where chmod does not restrict
    the process (root); the shape probes that itself.
    """
    monkeypatch.chdir(tmp_path)
    src = tmp_path / "[cfg]" / "src.yaml"
    dst_config, _ = _write_sqlite_config(tmp_path, "dst")

    with unreadable(shape, src):
        result = runner.invoke(
            admin_app,
            [
                "migrate-graph",
                "--from-config",
                "[cfg]/src.yaml",
                "--to-config",
                str(dst_config),
            ],
        )

    assert result.exit_code == 2, result.output
    output = " ".join(plain(result.output).split())
    assert f"Could not read [cfg]/src.yaml: {shape.message_fragment}" in output


def test_migrate_graph_refuses_a_config_name_too_long(
    tmp_path: Path, runner: CliRunner
) -> None:
    """A file name longer than NAME_MAX exits 2 with the OS's reason.

    ``stat`` raises ``ENAMETOOLONG``, which ``Path.exists()`` re-raised as a
    traceback. No chmod is involved, so this fails before the fix even as
    root. The path is not asserted: it cannot fit in 80 columns, and Rich
    folds an overlong token mid-word.
    """
    dst_config, _ = _write_sqlite_config(tmp_path, "dst")

    result = runner.invoke(
        admin_app,
        [
            "migrate-graph",
            "--from-config",
            str(tmp_path / ("x" * 300 + ".yaml")),
            "--to-config",
            str(dst_config),
        ],
    )

    assert result.exit_code == 2, result.output
    output = " ".join(plain(result.output).split())
    assert "Could not read" in output
    assert os.strerror(errno.ENAMETOOLONG) in output


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


class TestStoreFailureTextIsSanitized:
    """#753 follow-up 2: a destination store's own failure text reaches
    stdout unsanitized on every rendering path, while every other CLI
    failure path routes ``str(exc)`` through ``sanitize_error_message``
    first.

    ``_SYNTHETIC_STORE_ERROR`` reproduces the ``DETAIL: Key (col)=(...)
    already exists.`` shape a real Postgres/Neo4j/ArcadeDB duplicate-key
    violation quotes, with a 41-char opaque token standing in for the
    row value — long enough to trip the sanitizer's existing
    long-opaque-token heuristic (no sanitizer pattern is added here).
    """

    @staticmethod
    def _seeded_configs(
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        message: str = _SYNTHETIC_STORE_ERROR,
    ) -> tuple[Path, Path]:
        """Seed the source store, THEN patch writes to fail.

        Seeding the source also calls ``upsert_node``, so the patch must
        land after seeding or it breaks setup instead of the migration.
        """
        src_config, src_db = _write_sqlite_config(tmp_path, "src")
        dst_config, _ = _write_sqlite_config(tmp_path, "dst")
        src = SQLiteGraphStore(db_path=src_db)
        src.upsert_node("n1", node_type="X", properties={})
        src.close()

        def _refuse_write(*_args: object, **_kwargs: object) -> None:
            raise sqlite3.IntegrityError(message)

        monkeypatch.setattr(SQLiteGraphStore, "upsert_node", _refuse_write)
        return src_config, dst_config

    def test_fail_fast_abort_line_is_sanitized(
        self, tmp_path: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Default (stop-on-error) strategy: the ``Migration aborted:``
        line wraps ``MigrationStepError``, which embeds ``str(exc)`` of
        the underlying store error verbatim."""
        src_config, dst_config = self._seeded_configs(tmp_path, monkeypatch)

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

        output = plain(result.output)
        assert _SYNTHETIC_SECRET not in output
        assert SUPPRESSED_MARKER in output
        assert result.exit_code == EXIT_STORE

    def test_fail_fast_abort_line_is_sanitized_under_format_json(
        self, tmp_path: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``MigrationStepError`` is caught and rendered before the
        ``--format`` branch is reached, so ``--format json`` does not
        change this path — re-measured, not assumed."""
        src_config, dst_config = self._seeded_configs(tmp_path, monkeypatch)

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

        output = plain(result.output)
        assert _SYNTHETIC_SECRET not in output
        assert SUPPRESSED_MARKER in output
        assert result.exit_code == EXIT_STORE

    def test_continue_on_error_text_list_is_sanitized(
        self, tmp_path: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        src_config, dst_config = self._seeded_configs(tmp_path, monkeypatch)

        result = runner.invoke(
            admin_app,
            [
                "migrate-graph",
                "--from-config",
                str(src_config),
                "--to-config",
                str(dst_config),
                "--continue-on-error",
            ],
        )

        output = plain(result.output)
        assert _SYNTHETIC_SECRET not in output
        assert SUPPRESSED_MARKER in output
        assert result.exit_code == EXIT_STORE

    def test_continue_on_error_json_errors_and_step_failures_are_sanitized(
        self, tmp_path: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        src_config, dst_config = self._seeded_configs(tmp_path, monkeypatch)

        result = runner.invoke(
            admin_app,
            [
                "migrate-graph",
                "--from-config",
                str(src_config),
                "--to-config",
                str(dst_config),
                "--continue-on-error",
                "--format",
                "json",
            ],
        )

        payload = json.loads(result.stdout)
        assert payload["status"] == "error"
        assert payload["errors"], "the failed step must survive into the payload"
        assert payload["errors"][0]["message"] == SUPPRESSED_MARKER
        assert payload["step_failures"], "structured failures must survive too"
        assert payload["step_failures"][0]["message"] == SUPPRESSED_MARKER
        assert result.exit_code == EXIT_STORE
        # step_failures[].traceback is a raw traceback.format_exception()
        # string whose last line repeats the same leaking message, and it
        # is just as user-facing (stdout, --format json) as the message
        # field above — the sanitizer is all-or-nothing, so a leaking
        # traceback is replaced wholesale with the marker, never partially
        # redacted.
        assert _SYNTHETIC_SECRET not in payload["step_failures"][0]["traceback"]
        assert payload["step_failures"][0]["traceback"] == SUPPRESSED_MARKER

    def test_a_clean_failure_message_renders_unchanged(
        self, tmp_path: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failure whose text the sanitizer leaves alone (no leak
        heuristic trips) must render exactly as it did before this
        change — sanitizing is not a license to also truncate or alter
        clean operator-facing text."""
        src_config, dst_config = self._seeded_configs(
            tmp_path, monkeypatch, message=_READONLY_DB
        )

        result = runner.invoke(
            admin_app,
            [
                "migrate-graph",
                "--from-config",
                str(src_config),
                "--to-config",
                str(dst_config),
                "--continue-on-error",
                "--format",
                "json",
            ],
        )

        payload = json.loads(result.stdout)
        assert payload["errors"][0]["message"] == f"IntegrityError: {_READONLY_DB}"
        assert payload["step_failures"][0]["message"] == _READONLY_DB
        # Not asserted here: whether step_failures[].traceback itself
        # renders unchanged for this message. traceback.format_exception()
        # embeds the absolute source-file path of every frame, so whether
        # it trips the long-opaque-token heuristic depends on the
        # checkout's own path length, not on this message's content — this
        # worktree's directory name is exactly 40 chars and trips it
        # on its own. That is a property of the shared sanitizer, not of
        # this message, and is noted as a finding in the PR body rather
        # than pinned to a path-length-dependent assertion here.
