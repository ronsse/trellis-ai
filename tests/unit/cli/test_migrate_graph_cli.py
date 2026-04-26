"""Smoke tests for the ``trellis admin migrate-graph`` CLI wrapper.

Exercises the end-to-end CLI path against two SQLite databases — proves
the YAML config-loading, output formatting, and exit-code branches work.
The library-level tests in ``tests/unit/migrate/`` cover the migration
semantics; this file pins the CLI surface only.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from trellis.stores.sqlite.graph import SQLiteGraphStore
from trellis_cli.admin import admin_app


def _write_sqlite_config(tmp_path: Path, name: str) -> tuple[Path, Path]:
    db_path = tmp_path / f"{name}.db"
    config_path = tmp_path / f"{name}-config.yaml"
    config_path.write_text(
        f"graph:\n  backend: sqlite\n  db_path: {db_path}\n",
        encoding="utf-8",
    )
    return config_path, db_path


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
    assert "nodes=1/1" in result.output

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
    payload = json.loads(result.output)
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
