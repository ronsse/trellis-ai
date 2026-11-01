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
from collections.abc import Callable
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
from trellis_cli.exit_codes import EXIT_INTERNAL, EXIT_STORE

#: The timestamp columns each SQLite table holds, by the name this module gives
#: the row (an edge row has no ``updated_at``).
_TIMESTAMP_COLUMNS = {
    "node": ("created_at", "updated_at", "valid_from"),
    "edge": ("created_at", "valid_from"),
    "alias": ("created_at", "updated_at", "valid_from"),
}
_CLOCK_FIELDS = ("created_at", "updated_at", "valid_from", "valid_to")
_NOT_MOVED = ["documents", "traces", "event_log", "vectors", "blobs", "feedback"]
#: What a real read-only SQLite file raises on a write.
_READONLY_DB = "attempt to write a readonly database"
#: Each count below is different from every other, so a count read from the
#: wrong table, or a current count mistaken for a closed one, fails an assert.
_CURRENT = {"nodes": 4, "edges": 3, "aliases": 2}
_CLOSED = {"nodes": 3, "edges": 2, "aliases": 1}


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


def _ticking_clock(start: datetime) -> Callable[[], datetime]:
    now = start

    def _tick() -> datetime:
        nonlocal now
        now += timedelta(minutes=1)
        return now

    return _tick


def _seed_versioned_source(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Write a source graph whose closed rows differ in number per table.

    Current: 4 nodes, 3 edges, 2 aliases. Closed: 3 nodes, 2 edges, 1 alias.
    Most of it is stamped from 2020, so a destination row stamped by the copy
    cannot be confused with a carried-over one. One node's history is stamped
    from 2999, so its closed row has a ``valid_to`` that only a cutoff past
    every closed row counts.
    """
    src = SQLiteGraphStore(db_path=db_path)
    with monkeypatch.context() as patch:
        patch.setattr(
            sqlite_graph, "utc_now", _ticking_clock(datetime(2020, 1, 1, tzinfo=UTC))
        )
        for rev in (1, 2, 3):
            src.upsert_node("n1", node_type="X", properties={"rev": rev})
            src.upsert_edge("n1", "n2", "rel", properties={"rev": rev})
        src.upsert_node("n2", node_type="X", properties={})
        src.upsert_node("n4", node_type="X", properties={})
        src.upsert_edge("n3", "n4", "rel", properties={})
        src.upsert_alias("n1", "sys", "raw-1", raw_name="first")
        src.upsert_alias("n1", "sys", "raw-1", raw_name="second")
        src.upsert_alias("n2", "sys", "raw-2", raw_name="other")
        patch.setattr(
            sqlite_graph, "utc_now", _ticking_clock(datetime(2999, 1, 1, tzinfo=UTC))
        )
        src.upsert_node("n3", node_type="X", properties={"rev": 1})
        src.upsert_node("n3", node_type="X", properties={"rev": 2})
        src.upsert_edge("n2", "n3", "rel", properties={})
    assert len(src.get_node_history("n1")) == 3, "the source must hold three versions"
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


def _only_current_node(tmp_path: Path) -> dict[str, Path]:
    """A one-node source with no history and a destination that does not exist yet."""
    src_config, src_db = _config(tmp_path, "src")
    dst_config, dst_db = _config(tmp_path, "dst")
    src = SQLiteGraphStore(db_path=src_db)
    src.upsert_node("n1", node_type="X", properties={})
    src.close()
    return {
        "src_config": src_config,
        "src_db": src_db,
        "dst_config": dst_config,
        "dst_db": dst_db,
    }


def _current_rows(db_path: Path) -> list[int]:
    """Current (nodes, edges, aliases) rows in a SQLite graph file."""
    conn = sqlite3.connect(db_path)
    try:
        return [
            conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE valid_to IS NULL"  # noqa: S608
            ).fetchone()[0]
            for table in ("nodes", "edges", "entity_aliases")
        ]
    finally:
        conn.close()


def _run_json(runner: CliRunner, paths: dict[str, Path], *extra: str) -> dict[str, Any]:
    result = runner.invoke(
        admin_app,
        _argv(paths["src_config"], paths["dst_config"], *extra, "--format", "json"),
    )
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def _run_text(runner: CliRunner, paths: dict[str, Path], *extra: str) -> str:
    result = runner.invoke(
        admin_app, _argv(paths["src_config"], paths["dst_config"], *extra)
    )
    assert result.exit_code == 0, result.output
    return plain(result.output)


def _refuse(error: Exception) -> Callable[..., None]:
    def _raise(*_args: object, **_kwargs: object) -> None:
        raise error

    return _raise


class TestWhatTheCopyDoes:
    """The claims the report makes, pinned to the rows a copy actually writes."""

    def test_a_node_with_three_versions_arrives_with_one(
        self, versioned: dict[str, Path], runner: CliRunner
    ) -> None:
        _run_json(runner, versioned)

        dst = SQLiteGraphStore(db_path=versioned["dst_db"])
        try:
            assert len(dst.get_node_history("n1")) == 1
            current = dst.get_node("n1")
            assert current is not None
            assert current["properties"] == {"rev": 3}
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
            for key in _TIMESTAMP_COLUMNS[kind]:
                assert before[key] is not None, f"{kind} has no {key} to compare"
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

    def test_a_dry_run_still_creates_the_destination_database(
        self, tmp_path: Path, runner: CliRunner
    ) -> None:
        """The docs say so, so the behaviour they describe is the tested one."""
        paths = _only_current_node(tmp_path)
        assert not paths["dst_db"].exists()

        _run_json(runner, paths, "--dry-run")

        assert paths["dst_db"].exists()
        assert _current_rows(paths["dst_db"]) == [0, 0, 0]


class TestCurrentRowsTheWalkNeverReaches:
    """Edges and aliases are read through current nodes, so some are left out.

    A clean run still copies fewer current rows than the source holds when an
    edge or an alias hangs off an id with no current node. The copy is
    unchanged by this report; the report says so and counts the edges.
    """

    @pytest.fixture
    def dangling(self, tmp_path: Path) -> dict[str, Path]:
        src_config, src_db = _config(tmp_path, "src")
        dst_config, dst_db = _config(tmp_path, "dst")
        src = SQLiteGraphStore(db_path=src_db)
        src.upsert_node("n1", node_type="X", properties={})
        src.upsert_edge("ghost", "n1", "rel")
        src.upsert_edge("n1", "ghost2", "rel")
        src.upsert_alias("ghost", "sys", "r1", raw_name="x")
        src.close()
        return {
            "src_config": src_config,
            "src_db": src_db,
            "dst_config": dst_config,
            "dst_db": dst_db,
        }

    def test_the_fixture_leaves_current_rows_behind_on_a_clean_run(
        self, dangling: dict[str, Path], runner: CliRunner
    ) -> None:
        """The premise: source holds [1, 2, 1] current rows, destination [1, 1, 0]."""
        payload = _run_json(runner, dangling)

        assert payload["status"] == "ok"
        assert payload["errors"] == []
        assert _current_rows(dangling["src_db"]) == [1, 2, 1]
        assert _current_rows(dangling["dst_db"]) == [1, 1, 0]

    def test_the_json_counts_the_edges_it_did_not_reach(
        self, dangling: dict[str, Path], runner: CliRunner
    ) -> None:
        scope = _run_json(runner, dangling)["copy_scope"]

        assert scope["current_rows_copied"] == {"nodes": 1, "edges": 1, "aliases": 0}
        assert scope["current_edges_not_reached_counted"] is True
        assert scope["current_edges_not_reached"] == 1
        assert scope["source_version_rows_not_copied"] == {
            "nodes": 0,
            "edges": 0,
            "aliases": 0,
        }
        assert "no current node" in scope["unreached_note"]
        assert "aliases are not" in scope["unreached_note"]

    def test_the_text_says_the_same(
        self, dangling: dict[str, Path], runner: CliRunner
    ) -> None:
        text = _run_text(runner, dangling)

        assert "Current edges not reached: 1." in text
        assert (
            "Not reached: edges and aliases are read through the current nodes" in text
        )

    def test_a_source_with_only_closed_history_reaches_every_current_edge(
        self, versioned: dict[str, Path], runner: CliRunner
    ) -> None:
        """Closed edge rows are history, not unreached: ``count_edges`` skips them."""
        scope = _run_json(runner, versioned)["copy_scope"]

        assert scope["current_edges_not_reached_counted"] is True
        assert scope["current_edges_not_reached"] == 0

    @pytest.mark.parametrize(
        "error", [NotImplementedError(), sqlite3.OperationalError("disk I/O error")]
    )
    def test_an_edge_count_the_source_cannot_give_is_not_counted_rather_than_zero(
        self,
        dangling: dict[str, Path],
        runner: CliRunner,
        monkeypatch: pytest.MonkeyPatch,
        error: Exception,
    ) -> None:
        monkeypatch.setattr(SQLiteGraphStore, "count_edges", _refuse(error))

        payload = _run_json(runner, dangling)
        text = _run_text(runner, dangling, "--dry-run")

        scope = payload["copy_scope"]
        assert payload["status"] == "ok"
        assert scope["current_edges_not_reached_counted"] is False
        assert scope["current_edges_not_reached"] is None
        assert "Current edges not reached: not counted" in text


class TestJsonOutputStatesTheLimits:
    def test_closed_rows_show_as_rows_not_copied_table_by_table(
        self, versioned: dict[str, Path], runner: CliRunner
    ) -> None:
        payload = _run_json(runner, versioned)
        scope = payload["copy_scope"]

        assert scope["current_rows_copied"] == _CURRENT
        assert scope["source_version_rows_counted"] is True
        assert scope["source_version_rows_not_copied"] == _CLOSED

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

    def test_a_closed_row_stamped_after_2021_is_counted_too(
        self, versioned: dict[str, Path], runner: CliRunner
    ) -> None:
        """One node's closed row has ``valid_to`` in 2999, past any early cutoff."""
        conn = sqlite3.connect(versioned["src_db"])
        try:
            late = conn.execute(
                "SELECT COUNT(*) FROM nodes WHERE valid_to >= '2900'"
            ).fetchone()[0]
        finally:
            conn.close()
        assert late == 1, "the fixture must hold one closed row stamped far ahead"

        counted = _run_json(runner, versioned)["copy_scope"]
        assert counted["source_version_rows_not_copied"]["nodes"] == _CLOSED["nodes"]

    def test_a_source_with_no_history_counts_zero_rather_than_nothing(
        self, tmp_path: Path, runner: CliRunner
    ) -> None:
        paths = _only_current_node(tmp_path)

        scope = _run_json(runner, paths)["copy_scope"]

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
        assert (payload["nodes_read"], payload["nodes_written"]) == (4, 4)
        assert (payload["edges_read"], payload["edges_written"]) == (3, 3)
        assert (payload["aliases_read"], payload["aliases_written"]) == (2, 2)

    def test_a_dry_run_counts_the_history_and_deletes_none_of_it(
        self, versioned: dict[str, Path], runner: CliRunner
    ) -> None:
        payload = _run_json(runner, versioned, "--dry-run")

        assert payload["dry_run"] is True
        assert payload["copy_scope"]["source_version_rows_not_copied"] == _CLOSED
        src = SQLiteGraphStore(db_path=versioned["src_db"])
        try:
            assert len(src.get_node_history("n1")) == 3
        finally:
            src.close()

    @pytest.mark.parametrize(
        "error", [NotImplementedError(), sqlite3.OperationalError("disk I/O error")]
    )
    def test_a_count_the_source_cannot_give_is_reported_as_not_counted(
        self,
        versioned: dict[str, Path],
        runner: CliRunner,
        monkeypatch: pytest.MonkeyPatch,
        error: Exception,
    ) -> None:
        monkeypatch.setattr(SQLiteGraphStore, "compact_versions", _refuse(error))

        payload = _run_json(runner, versioned)

        scope = payload["copy_scope"]
        assert payload["status"] == "ok"
        assert scope["source_version_rows_counted"] is False
        assert scope["source_version_rows_not_copied"] is None

    def test_a_count_with_an_unexpected_shape_cannot_fail_a_finished_copy(
        self,
        versioned: dict[str, Path],
        runner: CliRunner,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            SQLiteGraphStore, "compact_versions", lambda *_a, **_k: object()
        )

        payload = _run_json(runner, versioned)

        assert payload["status"] == "ok"
        assert payload["copy_scope"]["source_version_rows_counted"] is False
        assert _current_rows(versioned["dst_db"]) == [4, 3, 2]


class TestWhatTheNumbersCount:
    """``current_rows_copied`` is what was written, not what was read."""

    def test_a_rerun_copies_nothing_though_it_reads_everything(
        self, versioned: dict[str, Path], runner: CliRunner
    ) -> None:
        _run_json(runner, versioned)

        payload = _run_json(runner, versioned)

        assert payload["copy_scope"]["current_rows_copied"] == {
            "nodes": 0,
            "edges": 0,
            "aliases": 0,
        }
        assert payload["nodes_read"] == _CURRENT["nodes"]
        assert payload["nodes_skipped"] == _CURRENT["nodes"]

    def test_a_partial_failure_counts_only_the_rows_that_landed(
        self,
        versioned: dict[str, Path],
        runner: CliRunner,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            SQLiteGraphStore,
            "upsert_edge",
            _refuse(sqlite3.OperationalError(_READONLY_DB)),
        )

        result = runner.invoke(
            admin_app,
            _argv(
                versioned["src_config"],
                versioned["dst_config"],
                "--continue-on-error",
                "--format",
                "json",
            ),
        )

        payload = json.loads(result.stdout)
        assert result.exit_code == EXIT_STORE
        assert payload["edges_read"] == _CURRENT["edges"]
        assert payload["copy_scope"]["current_rows_copied"] == {
            "nodes": 4,
            "edges": 0,
            "aliases": 2,
        }

    def test_a_rerun_adds_missing_rows_and_updates_or_removes_none(
        self, versioned: dict[str, Path], runner: CliRunner
    ) -> None:
        """What the docs say a re-run does, so a cutover that relies on it is warned."""
        _run_json(runner, versioned)
        src = SQLiteGraphStore(db_path=versioned["src_db"])
        try:
            src.upsert_node("n1", node_type="X", properties={"rev": 9})
            src.upsert_edge("n1", "n2", "rel", properties={"rev": 9})
            src.upsert_alias("n1", "sys", "raw-1", raw_name="changed")
            assert src.delete_node("n4")
            src.upsert_node("n5", node_type="X", properties={})
        finally:
            src.close()

        payload = _run_json(runner, versioned)

        assert payload["status"] == "ok"
        assert payload["copy_scope"]["current_rows_copied"] == {
            "nodes": 1,
            "edges": 0,
            "aliases": 0,
        }
        dst = SQLiteGraphStore(db_path=versioned["dst_db"])
        try:
            n1 = dst.get_node("n1")
            assert n1 is not None
            assert n1["properties"] == {"rev": 3}, "an existing node was updated"
            edge = dst.get_edges("n1", direction="outgoing")[0]
            assert edge["properties"] == {"rev": 3}, "an existing edge was updated"
            assert dst.get_aliases("n1")[0]["raw_name"] == "second"
            assert dst.get_node("n4") is not None, "a deleted node was removed"
            assert dst.get_node("n5") is not None
        finally:
            dst.close()


class TestTextOutputStatesTheLimits:
    def test_every_limit_is_stated(
        self, versioned: dict[str, Path], runner: CliRunner
    ) -> None:
        text = _run_text(runner, versioned)

        assert "Copied current graph rows only: nodes=4 edges=3 aliases=2." in text
        assert (
            "Source version rows not copied (closed history): "
            "nodes=3 edges=2 aliases=1." in text
        )
        assert "Current edges not reached: 0." in text
        assert "Timestamps are not preserved" in text
        assert "created_at and valid_from" in text
        assert "Not moved by this command: documents, traces, event_log" in text
        assert "vectors, blobs, feedback." in text

    def test_the_limits_print_when_no_history_was_lost(
        self, tmp_path: Path, runner: CliRunner
    ) -> None:
        paths = _only_current_node(tmp_path)

        text = _run_text(runner, paths)

        assert (
            "Source version rows not copied (closed history): "
            "nodes=0 edges=0 aliases=0." in text
        )
        assert "Timestamps are not preserved" in text
        assert "Not moved by this command" in text

    def test_a_dry_run_says_would_copy(
        self, versioned: dict[str, Path], runner: CliRunner
    ) -> None:
        text = _run_text(runner, versioned, "--dry-run")

        assert "Would copy current graph rows only" in text
        assert "Copied current graph rows only" not in text
        assert "nodes=3 edges=2 aliases=1." in text

    @pytest.mark.parametrize(
        "error", [NotImplementedError(), sqlite3.OperationalError("disk I/O error")]
    )
    def test_an_uncountable_history_is_not_printed_as_zero(
        self,
        versioned: dict[str, Path],
        runner: CliRunner,
        monkeypatch: pytest.MonkeyPatch,
        error: Exception,
    ) -> None:
        monkeypatch.setattr(SQLiteGraphStore, "compact_versions", _refuse(error))

        text = _run_text(runner, versioned)

        assert "Source version rows not copied: not counted" in text
        assert "closed history" not in text


class TestAFailedRunStillStatesTheLimitsAndKeepsItsExit:
    """A run that finishes with errors carries the statement; no exit code moves."""

    @pytest.fixture
    def failing(
        self, versioned: dict[str, Path], monkeypatch: pytest.MonkeyPatch
    ) -> dict[str, Path]:
        monkeypatch.setattr(
            SQLiteGraphStore,
            "upsert_node",
            _refuse(sqlite3.OperationalError(_READONLY_DB)),
        )
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
        assert payload["copy_scope"]["source_version_rows_not_copied"] == _CLOSED

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


class TestARunThatAbortsStatesNoLimits:
    """Two paths leave before the scope is built; the docs say so, so they are pinned.

    A store error under the default strategy prints one text line in both
    formats, so a caller reading ``.copy_scope`` from ``--format json`` gets a
    parse failure and must read the exit code first. The destination can hold a
    partial copy. A source over ``--max-nodes`` exits 1 before anything is
    written.
    """

    @pytest.mark.parametrize("fmt", ["text", "json"])
    def test_a_store_error_aborts_with_one_text_line_and_a_partial_copy(
        self,
        versioned: dict[str, Path],
        runner: CliRunner,
        monkeypatch: pytest.MonkeyPatch,
        fmt: str,
    ) -> None:
        original = SQLiteGraphStore.upsert_node
        calls = {"count": 0}

        def _fail_the_second_write(
            self: SQLiteGraphStore, *args: Any, **kwargs: Any
        ) -> Any:
            calls["count"] += 1
            if calls["count"] == 2:
                raise sqlite3.OperationalError(_READONLY_DB)
            return original(self, *args, **kwargs)

        monkeypatch.setattr(SQLiteGraphStore, "upsert_node", _fail_the_second_write)

        result = runner.invoke(
            admin_app,
            _argv(versioned["src_config"], versioned["dst_config"], "--format", fmt),
        )

        assert result.exit_code == EXIT_STORE
        assert "Migration aborted:" in plain(result.stdout)
        with pytest.raises(json.JSONDecodeError):
            json.loads(result.stdout)
        assert "copy_scope" not in result.stdout
        assert "Timestamps are not preserved" not in plain(result.stdout)
        assert _current_rows(versioned["dst_db"])[0] == 1

    @pytest.mark.parametrize("fmt", ["text", "json"])
    def test_a_source_over_max_nodes_exits_1_before_writing_anything(
        self, versioned: dict[str, Path], runner: CliRunner, fmt: str
    ) -> None:
        result = runner.invoke(
            admin_app,
            _argv(
                versioned["src_config"],
                versioned["dst_config"],
                "--max-nodes",
                "1",
                "--format",
                fmt,
            ),
        )

        assert result.exit_code == EXIT_INTERNAL
        assert "max_nodes=1" in plain(result.stdout)
        with pytest.raises(json.JSONDecodeError):
            json.loads(result.stdout)
        assert "copy_scope" not in result.stdout
        assert _current_rows(versioned["dst_db"]) == [0, 0, 0]
