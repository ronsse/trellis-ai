"""``trellis admin migrate-graph`` states what a copy leaves behind.

The command copies the current graph rows through ``GraphStore``'s upsert
methods. That drops closed SCD-2 version rows, gives every written row a fresh
clock, and touches no other store, yet an ``ok`` run once said nothing about
any of it. These tests pin the statement on both output formats and pin each
claim in it to what the code does, against two SQLite files. The Postgres and
Neo4j backends are not exercised here.
"""

from __future__ import annotations

import inspect
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import trellis.stores.sqlite.graph as sqlite_graph
from tests.cli_output import plain
from trellis.stores.base.graph import GraphStore
from trellis.stores.sqlite.graph import SQLiteGraphStore
from trellis_cli.admin import admin_app
from trellis_cli.exit_codes import EXIT_STORE

_CLOCK_FIELDS = ("created_at", "updated_at", "valid_from", "valid_to")
_NOT_MOVED = ["documents", "traces", "event_log", "vectors", "feedback"]
#: What a real read-only SQLite file raises on a write.
_READONLY_DB = "attempt to write a readonly database"


@pytest.fixture(autouse=True)
def _isolated_config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the operator's real ~/.trellis out of these tests (see the sibling file)."""
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "trellis-config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(tmp_path / "trellis-data"))


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _config(tmp_path: Path, name: str) -> tuple[Path, Path]:
    db_path = tmp_path / f"{name}.db"
    config_path = tmp_path / f"{name}-config.yaml"
    config_path.write_text(
        f"graph:\n  backend: sqlite\n  db_path: {db_path}\n", encoding="utf-8"
    )
    return config_path, db_path


def _argv(src_config: Path, dst_config: Path, *extra: str) -> list[str]:
    return [
        "migrate-graph",
        "--from-config",
        str(src_config),
        "--to-config",
        str(dst_config),
        *extra,
    ]


def _seed_versioned_source(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Write a source graph with one closed version of a node, an edge and an alias.

    The store's clock is pinned to 2020, so a destination row stamped by the
    copy cannot be confused with a carried-over one.
    """
    now = datetime(2020, 1, 1, tzinfo=UTC)

    def _tick() -> datetime:
        nonlocal now
        now += timedelta(minutes=1)
        return now

    src = SQLiteGraphStore(db_path=db_path)
    with monkeypatch.context() as patch:
        patch.setattr(sqlite_graph, "utc_now", _tick)
        src.upsert_node("n1", node_type="X", properties={"rev": 1})
        src.upsert_node("n1", node_type="X", properties={"rev": 2})
        src.upsert_node("n2", node_type="X", properties={})
        src.upsert_edge("n1", "n2", "rel", properties={"rev": 1})
        src.upsert_edge("n1", "n2", "rel", properties={"rev": 2})
        src.upsert_alias("n1", "sys", "raw-1", raw_name="first")
        src.upsert_alias("n1", "sys", "raw-1", raw_name="second")
    assert len(src.get_node_history("n1")) == 2, "the source must hold two versions"
    src.close()


@pytest.fixture
def versioned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    src_config, src_db = _config(tmp_path, "src")
    dst_config, dst_db = _config(tmp_path, "dst")
    _seed_versioned_source(src_db, monkeypatch)
    return {
        "src_config": src_config,
        "src_db": src_db,
        "dst_config": dst_config,
        "dst_db": dst_db,
    }


def _run_json(runner: CliRunner, paths: dict[str, Path], *extra: str) -> dict[str, Any]:
    result = runner.invoke(
        admin_app,
        _argv(paths["src_config"], paths["dst_config"], *extra, "--format", "json"),
    )
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


class TestWhatTheCopyDoes:
    """The claims the report makes, pinned to the rows a copy actually writes."""

    def test_a_node_with_two_versions_arrives_with_one(
        self, versioned: dict[str, Path], runner: CliRunner
    ) -> None:
        _run_json(runner, versioned)

        dst = SQLiteGraphStore(db_path=versioned["dst_db"])
        try:
            assert len(dst.get_node_history("n1")) == 1
            current = dst.get_node("n1")
            assert current is not None
            assert current["properties"] == {"rev": 2}
        finally:
            dst.close()

    def test_no_timestamp_is_carried_over(
        self, versioned: dict[str, Path], runner: CliRunner
    ) -> None:
        _run_json(runner, versioned)

        src = SQLiteGraphStore(db_path=versioned["src_db"])
        dst = SQLiteGraphStore(db_path=versioned["dst_db"])
        try:
            src_node, dst_node = src.get_node("n1"), dst.get_node("n1")
            src_edge = src.get_edges("n1", direction="outgoing")[0]
            dst_edge = dst.get_edges("n1", direction="outgoing")[0]
            src_alias, dst_alias = src.get_aliases("n1")[0], dst.get_aliases("n1")[0]
        finally:
            src.close()
            dst.close()

        assert src_node is not None
        assert dst_node is not None
        for kind, before, after in (
            ("node", src_node, dst_node),
            ("edge", src_edge, dst_edge),
            ("alias", src_alias, dst_alias),
        ):
            for key in _CLOCK_FIELDS:
                if key in before and before[key] is not None:
                    assert after[key] != before[key], f"{kind} {key} was carried over"
        assert dst_edge["edge_id"] != src_edge["edge_id"]
        assert dst_alias["alias_id"] != src_alias["alias_id"]

    def test_the_graph_abc_accepts_no_timestamp_to_carry_over(self) -> None:
        """The fixed timestamp statement holds while no upsert takes a clock or id.

        A backend cannot preserve what the ABC gives it no way to pass. If one of
        these methods grows such a parameter, the statement and the docs that
        repeat it need another look.
        """
        stamped = {*_CLOCK_FIELDS, "edge_id", "alias_id"}
        for name in ("upsert_node", "upsert_edge", "upsert_alias"):
            params = set(inspect.signature(getattr(GraphStore, name)).parameters)
            assert not params & stamped, f"{name} now accepts {params & stamped}"


class TestJsonOutputStatesTheLimits:
    def test_two_versions_show_as_rows_not_copied(
        self, versioned: dict[str, Path], runner: CliRunner
    ) -> None:
        payload = _run_json(runner, versioned)
        scope = payload["copy_scope"]

        assert scope["current_rows_copied"] == {"nodes": 2, "edges": 1, "aliases": 1}
        assert scope["source_version_rows_counted"] is True
        assert scope["source_version_rows_not_copied"] == {
            "nodes": 1,
            "edges": 1,
            "aliases": 1,
        }

    def test_the_count_matches_the_source_rows_the_copy_left_out(
        self, versioned: dict[str, Path], runner: CliRunner
    ) -> None:
        payload = _run_json(runner, versioned)

        conn = sqlite3.connect(versioned["src_db"])
        try:
            closed = {
                table: conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE valid_to IS NOT NULL"  # noqa: S608
                ).fetchone()[0]
                for table in ("nodes", "edges", "entity_aliases")
            }
        finally:
            conn.close()
        counted = payload["copy_scope"]["source_version_rows_not_copied"]
        assert (counted["nodes"], counted["edges"], counted["aliases"]) == (
            closed["nodes"],
            closed["edges"],
            closed["entity_aliases"],
        )

    def test_a_source_with_no_history_counts_zero_rather_than_nothing(
        self, tmp_path: Path, runner: CliRunner
    ) -> None:
        src_config, src_db = _config(tmp_path, "src")
        dst_config, _ = _config(tmp_path, "dst")
        src = SQLiteGraphStore(db_path=src_db)
        src.upsert_node("n1", node_type="X", properties={})
        src.close()

        result = runner.invoke(
            admin_app, _argv(src_config, dst_config, "--format", "json")
        )

        scope = json.loads(result.stdout)["copy_scope"]
        assert scope["source_version_rows_counted"] is True
        assert scope["source_version_rows_not_copied"] == {
            "nodes": 0,
            "edges": 0,
            "aliases": 0,
        }

    def test_timestamps_and_unmoved_stores_are_stated(
        self, versioned: dict[str, Path], runner: CliRunner
    ) -> None:
        scope = _run_json(runner, versioned)["copy_scope"]

        assert scope["timestamps_preserved"] is False
        assert "created_at" in scope["timestamps_note"]
        assert "valid_from" in scope["timestamps_note"]
        assert scope["not_moved"] == _NOT_MOVED
        assert "current graph rows only" in scope["copies"]

    def test_existing_keys_keep_their_meaning_and_status_stays_ok(
        self, versioned: dict[str, Path], runner: CliRunner
    ) -> None:
        payload = _run_json(runner, versioned)

        assert payload["status"] == "ok"
        assert payload["dry_run"] is False
        assert payload["errors"] == []
        assert (payload["nodes_read"], payload["nodes_written"]) == (2, 2)
        assert (payload["edges_read"], payload["edges_written"]) == (1, 1)
        assert (payload["aliases_read"], payload["aliases_written"]) == (1, 1)

    def test_a_dry_run_counts_the_history_and_deletes_none_of_it(
        self, versioned: dict[str, Path], runner: CliRunner
    ) -> None:
        payload = _run_json(runner, versioned, "--dry-run")

        assert payload["dry_run"] is True
        assert payload["copy_scope"]["source_version_rows_not_copied"]["nodes"] == 1
        src = SQLiteGraphStore(db_path=versioned["src_db"])
        try:
            assert len(src.get_node_history("n1")) == 2
        finally:
            src.close()

    def test_a_count_the_source_cannot_give_is_reported_as_not_counted(
        self,
        versioned: dict[str, Path],
        runner: CliRunner,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def _unsupported(*_args: object, **_kwargs: object) -> None:
            raise NotImplementedError

        monkeypatch.setattr(SQLiteGraphStore, "compact_versions", _unsupported)

        payload = _run_json(runner, versioned)

        scope = payload["copy_scope"]
        assert payload["status"] == "ok"
        assert scope["source_version_rows_counted"] is False
        assert scope["source_version_rows_not_copied"] is None


class TestTextOutputStatesTheLimits:
    @staticmethod
    def _text(runner: CliRunner, paths: dict[str, Path], *extra: str) -> str:
        result = runner.invoke(
            admin_app, _argv(paths["src_config"], paths["dst_config"], *extra)
        )
        assert result.exit_code == 0, result.output
        return plain(result.output)

    def test_every_limit_is_stated(
        self, versioned: dict[str, Path], runner: CliRunner
    ) -> None:
        text = self._text(runner, versioned)

        assert "Copied current graph rows only: nodes=2 edges=1 aliases=1." in text
        assert (
            "Source version rows not copied (closed history): "
            "nodes=1 edges=1 aliases=1." in text
        )
        assert "Timestamps are not preserved" in text
        assert "created_at, updated_at and valid_from" in text
        assert "Not moved by this command: documents, traces, event_log" in text
        assert "vectors, feedback." in text

    def test_the_limits_print_when_no_history_was_lost(
        self, tmp_path: Path, runner: CliRunner
    ) -> None:
        src_config, src_db = _config(tmp_path, "src")
        dst_config, _ = _config(tmp_path, "dst")
        src = SQLiteGraphStore(db_path=src_db)
        src.upsert_node("n1", node_type="X", properties={})
        src.close()

        result = runner.invoke(admin_app, _argv(src_config, dst_config))

        text = plain(result.output)
        assert result.exit_code == 0, result.output
        assert (
            "Source version rows not copied (closed history): "
            "nodes=0 edges=0 aliases=0." in text
        )
        assert "Timestamps are not preserved" in text
        assert "Not moved by this command" in text

    def test_a_dry_run_says_would_copy(
        self, versioned: dict[str, Path], runner: CliRunner
    ) -> None:
        text = self._text(runner, versioned, "--dry-run")

        assert "Would copy current graph rows only" in text
        assert "Copied current graph rows only" not in text
        assert "nodes=1 edges=1 aliases=1." in text

    def test_an_uncountable_history_is_not_printed_as_zero(
        self,
        versioned: dict[str, Path],
        runner: CliRunner,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def _unsupported(*_args: object, **_kwargs: object) -> None:
            raise NotImplementedError

        monkeypatch.setattr(SQLiteGraphStore, "compact_versions", _unsupported)

        text = self._text(runner, versioned)

        assert "Source version rows not copied: not counted" in text
        assert "closed history" not in text


class TestAFailedRunStillStatesTheLimitsAndKeepsItsExit:
    """The statement is added to a failing run too, and no exit code moves."""

    @pytest.fixture
    def failing(
        self, versioned: dict[str, Path], monkeypatch: pytest.MonkeyPatch
    ) -> dict[str, Path]:
        def _refuse(*_args: object, **_kwargs: object) -> None:
            raise sqlite3.OperationalError(_READONLY_DB)

        monkeypatch.setattr(SQLiteGraphStore, "upsert_node", _refuse)
        return versioned

    @pytest.mark.parametrize("fmt", ["text", "json"])
    def test_the_exit_code_is_the_same_on_both_formats(
        self, failing: dict[str, Path], runner: CliRunner, fmt: str
    ) -> None:
        result = runner.invoke(
            admin_app,
            _argv(
                failing["src_config"],
                failing["dst_config"],
                "--continue-on-error",
                "--format",
                fmt,
            ),
        )

        assert result.exit_code == EXIT_STORE

    def test_the_json_payload_carries_the_scope_beside_status_error(
        self, failing: dict[str, Path], runner: CliRunner
    ) -> None:
        result = runner.invoke(
            admin_app,
            _argv(
                failing["src_config"],
                failing["dst_config"],
                "--continue-on-error",
                "--format",
                "json",
            ),
        )

        payload = json.loads(result.stdout)
        assert payload["status"] == "error"
        assert payload["copy_scope"]["timestamps_preserved"] is False
        assert payload["copy_scope"]["source_version_rows_not_copied"]["nodes"] == 1

    def test_the_text_lines_follow_the_summary_and_precede_the_errors(
        self, failing: dict[str, Path], runner: CliRunner
    ) -> None:
        result = runner.invoke(
            admin_app,
            _argv(failing["src_config"], failing["dst_config"], "--continue-on-error"),
        )

        text = plain(result.output)
        assert "Timestamps are not preserved" in text
        assert text.index("Timestamps are not preserved") < text.index("Errors:")
