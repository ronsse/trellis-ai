"""``trellis admin migrate`` through the CLI: report by default, ``--apply`` to write.

The cases the command exists for are in ``tests/unit/ops/test_schema_migrate.py``
(the framework) and ``tests/unit/ops/test_schema_migrate_not_on_open.py`` (nothing
else writes the record). These cover the command's own surface: what the report
prints, what ``--apply`` writes, the exit code, and that the exit code does not
move with ``--format``.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from tests.cli_output import plain
from trellis.ops.schema_migrate import BASELINE_VERSION, VERSION_TABLE
from trellis.schemas import well_known as wk
from trellis.stores.registry import _PLANE_OF
from trellis_cli import admin_migrate
from trellis_cli.main import app
from trellis_cli.stores import _get_registry, _reset_registry

runner = CliRunner()

_ARMS = pytest.mark.parametrize("arm", [[], ["--format", "json"]], ids=["text", "json"])


class _Deployment:
    def __init__(self, root: Path) -> None:
        self.stores = root / "data" / "stores"


@pytest.fixture
def deploy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Deployment:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("COLUMNS", "400")  # Rich must not fold a cell
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(tmp_path / "data"))
    init = runner.invoke(app, ["admin", "init"])
    assert init.exit_code == 0, init.output
    _reset_registry()
    # ``admin init`` creates no database; opening the stores does, which is the
    # shape every running deployment already has.
    registry = _get_registry()
    for store_type in _PLANE_OF:
        registry._get(store_type)
    return _Deployment(tmp_path)


def _migrate(*args: str):
    return runner.invoke(app, ["admin", "migrate", *args])


def _json(*args: str, code: int = 0) -> dict[str, Any]:
    result = _migrate(*args, "--format", "json")
    assert result.exit_code == code, result.output
    return json.loads(result.stdout)


def _stores(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {s["store"]: s for s in payload["stores"]}


def _has_version_table(db: Path) -> bool:
    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = ?", (VERSION_TABLE,)
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def _activities() -> list[dict[str, Any]]:
    registry = _get_registry()
    return registry.knowledge.graph_store.query(
        node_type=wk.ACTIVITY,
        properties={"analyzer_name": "cli.admin.migrate"},
        limit=10,
    )


def _break_document_store(deploy: _Deployment) -> None:
    """Replace the document database with one of a shape no version matches."""
    path = deploy.stores / "documents.db"
    _get_registry().close()
    _reset_registry()
    for suffix in ("", "-wal", "-shm"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE unrelated (x INTEGER)")
    conn.commit()
    conn.close()


class TestReport:
    def test_json_report_names_each_store_and_writes_nothing(
        self, deploy: _Deployment
    ) -> None:
        payload = _json()

        assert payload["status"] == "ok"
        assert payload["mode"] == "report"
        assert payload["baseline_version"] == BASELINE_VERSION
        doc = _stores(payload)["document"]
        assert doc["current_version"] == BASELINE_VERSION
        assert doc["target_version"] == BASELINE_VERSION
        assert doc["recorded"] is False
        assert doc["status"] == "current"
        assert not _has_version_table(deploy.stores / "documents.db")

    def test_text_report_says_nothing_was_written_and_how_to_apply(
        self, deploy: _Deployment
    ) -> None:
        result = _migrate()

        out = plain(result.output)
        assert result.exit_code == 0, out
        assert "nothing written" in out
        assert "--apply" in out
        assert "(from shape)" in out
        assert not _has_version_table(deploy.stores / "documents.db")

    def test_a_report_records_no_meta_activity(self, deploy: _Deployment) -> None:
        _json()

        assert _activities() == []


class TestApply:
    def test_apply_stamps_the_baseline_and_a_second_apply_changes_nothing(
        self, deploy: _Deployment
    ) -> None:
        first = _json("--apply")

        assert first["mode"] == "apply"
        assert _stores(first)["document"]["status"] == "stamped"
        assert _has_version_table(deploy.stores / "documents.db")

        second = _json("--apply")

        assert _stores(second)["document"]["status"] == "current"
        conn = sqlite3.connect(deploy.stores / "documents.db")
        try:
            rows = conn.execute(f"SELECT version FROM {VERSION_TABLE}").fetchall()  # noqa: S608
        finally:
            conn.close()
        assert rows == [(BASELINE_VERSION,)]

    def test_a_report_after_apply_reads_the_record(self, deploy: _Deployment) -> None:
        _json("--apply")

        doc = _stores(_json())["document"]

        assert doc["recorded"] is True
        assert doc["status"] == "current"

    def test_apply_records_one_meta_activity_unless_told_not_to(
        self, deploy: _Deployment
    ) -> None:
        _json("--apply", "--no-meta-trace")
        assert _activities() == []

        _json("--apply")
        assert len(_activities()) == 1


class TestExitCodeDoesNotDependOnFormat:
    @_ARMS
    def test_a_clean_run_exits_zero_in_both_formats(
        self, deploy: _Deployment, arm: list[str]
    ) -> None:
        assert _migrate(*arm).exit_code == 0
        assert _migrate("--apply", *arm).exit_code == 0

    @_ARMS
    def test_an_unrecognised_store_exits_five_in_both_formats(
        self, deploy: _Deployment, arm: list[str]
    ) -> None:
        _break_document_store(deploy)

        result = _migrate("--apply", "--no-meta-trace", *arm)

        assert result.exit_code == 5, result.output
        if arm:
            payload = json.loads(result.stdout)
            assert payload["status"] == "error"
            assert _stores(payload)["document"]["status"] == "unrecognized"
        else:
            assert "UNRECOGNIZED" in plain(result.output)
        assert not _has_version_table(deploy.stores / "documents.db")

    @_ARMS
    def test_a_pending_report_is_not_a_failure(
        self, deploy: _Deployment, arm: list[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from trellis.ops import schema_migrate

        chain = schema_migrate.MigrationSet(
            [
                schema_migrate.Migration(
                    "document", "sqlite", 2, "later", lambda conn: None
                )
            ]
        )
        real = schema_migrate.run_migrate
        monkeypatch.setattr(
            admin_migrate,
            "run_migrate",
            lambda registry, *, apply: real(registry, apply=apply, migrations=chain),
        )

        result = _migrate(*arm)

        assert result.exit_code == 0, result.output
        if arm:
            doc = _stores(json.loads(result.stdout))["document"]
            assert doc["status"] == "pending"
            assert doc["pending"] == [2]
        else:
            assert "pending" in plain(result.output)


def test_the_command_is_listed_under_admin() -> None:
    result = runner.invoke(app, ["admin", "--help"])

    assert result.exit_code == 0
    assert "migrate" in plain(result.output)
