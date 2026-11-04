"""``run_migrate``: report by default, stamp the baseline on ``--apply``.

Every store here is a real SQLite store opened by the registry, so the tests
exercise the shapes the stores actually create, not a hand-drawn imitation of
them. The migration chain that proves the framework runs steps is registered by
the test (:data:`SHIPPED_MIGRATIONS` is empty: this build has one version).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from trellis.errors import ConfigError
from trellis.ops.schema_migrate import (
    BASELINE_TABLES,
    BASELINE_VERSION,
    SHIPPED_MIGRATIONS,
    VERSION_TABLE,
    Migration,
    MigrationSet,
    detect_version,
    run_migrate,
)
from trellis.stores.registry import _PLANE_OF, StoreRegistry

SQLITE_STORES = sorted(t for t in _PLANE_OF if t != "blob")


@pytest.fixture
def registry(tmp_path: Path) -> StoreRegistry:
    stores = tmp_path / "stores"
    stores.mkdir()
    reg = StoreRegistry(stores_dir=stores)
    yield reg
    reg.close()


def open_every_store(reg: StoreRegistry) -> None:
    for store_type in _PLANE_OF:
        reg._get(store_type)


def db_path(reg: StoreRegistry, store_type: str) -> Path:
    path = reg.configured_sqlite_path(store_type)
    assert path is not None
    return path


def tables_of(path: Path) -> set[str]:
    uri = f"{path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        return {r[0] for r in rows}
    finally:
        conn.close()


def recorded_rows(path: Path) -> list[tuple[Any, ...]]:
    conn = sqlite3.connect(path)
    try:
        return conn.execute(
            f"SELECT store, version, description FROM {VERSION_TABLE}"  # noqa: S608
            " ORDER BY rowid"
        ).fetchall()
    finally:
        conn.close()


def by_store(report: Any) -> dict[str, Any]:
    return {s.store: s for s in report.stores}


def marker_chain(calls: list[int], *, fail_at: int | None = None) -> MigrationSet:
    """Two steps for the document store; each leaves a table behind."""

    def step(version: int):
        def apply(conn: Any) -> None:
            conn.execute(f"CREATE TABLE step_{version} (x INTEGER)")
            calls.append(version)
            if version == fail_at:
                msg = "boom"
                raise RuntimeError(msg)

        return apply

    return MigrationSet(
        [Migration("document", "sqlite", v, f"add step {v}", step(v)) for v in (2, 3)]
    )


class TestBaselineShapes:
    def test_every_sqlite_store_opens_with_its_baseline_tables(
        self, registry: StoreRegistry
    ) -> None:
        """The roster in ``BASELINE_TABLES`` is read against the stores themselves,
        so a store that renames a table cannot leave it stale."""
        open_every_store(registry)
        for store_type in SQLITE_STORES:
            tables = frozenset(tables_of(db_path(registry, store_type)))
            assert BASELINE_TABLES[("sqlite", store_type)] <= tables, store_type
            assert detect_version(store_type, "sqlite", tables) == BASELINE_VERSION

    def test_every_sqlite_store_type_has_a_baseline_entry(self) -> None:
        missing = [t for t in SQLITE_STORES if ("sqlite", t) not in BASELINE_TABLES]
        assert not missing, f"add BASELINE_TABLES entries for {missing}"

    def test_an_unknown_shape_has_no_version(self) -> None:
        assert detect_version("document", "sqlite", frozenset({"unrelated"})) is None

    def test_the_build_ships_the_baseline_and_nothing_pending(self) -> None:
        for store_type in _PLANE_OF:
            assert SHIPPED_MIGRATIONS.target(store_type, "sqlite") == BASELINE_VERSION
            assert not SHIPPED_MIGRATIONS.after(store_type, "sqlite", BASELINE_VERSION)


class TestReport:
    def test_a_fresh_deployment_reports_absent_and_creates_nothing(
        self, registry: StoreRegistry
    ) -> None:
        before = sorted(p.name for p in registry.stores_dir.iterdir())

        report = run_migrate(registry, apply=False)

        assert report.status == "ok"
        states = by_store(report)
        for store_type in SQLITE_STORES:
            assert states[store_type].status == "absent", store_type
        assert sorted(p.name for p in registry.stores_dir.iterdir()) == before

    def test_a_main_shaped_store_reads_as_the_baseline_without_a_record(
        self, registry: StoreRegistry
    ) -> None:
        open_every_store(registry)
        paths = {t: db_path(registry, t) for t in SQLITE_STORES}
        mtimes = {t: p.stat().st_mtime_ns for t, p in paths.items()}

        report = run_migrate(registry, apply=False)

        assert report.status == "ok"
        for store_type, state in by_store(report).items():
            if store_type == "blob":
                assert state.status == "unsupported"
                continue
            assert state.status == "current", store_type
            assert state.current_version == BASELINE_VERSION
            assert state.target_version == BASELINE_VERSION
            assert state.recorded is False
            assert state.pending == []
        for store_type, path in paths.items():
            assert VERSION_TABLE not in tables_of(path), store_type
            assert path.stat().st_mtime_ns == mtimes[store_type], store_type

    def test_pending_steps_are_listed_and_not_run(
        self, registry: StoreRegistry
    ) -> None:
        open_every_store(registry)
        calls: list[int] = []

        report = run_migrate(registry, apply=False, migrations=marker_chain(calls))

        doc = by_store(report)["document"]
        assert doc.status == "pending"
        assert doc.current_version == BASELINE_VERSION
        assert doc.target_version == 3
        assert doc.pending == [2, 3]
        assert calls == []
        assert "step_2" not in tables_of(db_path(registry, "document"))


class TestApply:
    def test_apply_stamps_the_baseline_and_changes_no_other_table(
        self, registry: StoreRegistry
    ) -> None:
        open_every_store(registry)
        before = {t: tables_of(db_path(registry, t)) for t in SQLITE_STORES}

        report = run_migrate(registry, apply=True)

        assert report.status == "ok"
        for store_type in SQLITE_STORES:
            state = by_store(report)[store_type]
            assert state.status == "stamped", store_type
            assert state.recorded is True
            assert state.applied == []
            path = db_path(registry, store_type)
            assert tables_of(path) - {VERSION_TABLE} == before[store_type]
            assert recorded_rows(path) == [(store_type, BASELINE_VERSION, "baseline")]

    def test_a_second_apply_is_a_no_op(self, registry: StoreRegistry) -> None:
        open_every_store(registry)
        run_migrate(registry, apply=True)
        snapshot = {t: recorded_rows(db_path(registry, t)) for t in SQLITE_STORES}

        again = run_migrate(registry, apply=True)

        assert again.status == "ok"
        for store_type in SQLITE_STORES:
            assert by_store(again)[store_type].status == "current", store_type
            assert recorded_rows(db_path(registry, store_type)) == snapshot[store_type]

    def test_a_report_after_apply_reads_the_record(
        self, registry: StoreRegistry
    ) -> None:
        open_every_store(registry)
        run_migrate(registry, apply=True)

        report = run_migrate(registry, apply=False)

        doc = by_store(report)["document"]
        assert (doc.status, doc.recorded, doc.current_version) == (
            "current",
            True,
            BASELINE_VERSION,
        )

    def test_apply_on_a_fresh_deployment_creates_no_file(
        self, registry: StoreRegistry
    ) -> None:
        report = run_migrate(registry, apply=True)

        assert report.status == "ok"
        assert not list(registry.stores_dir.iterdir())
        assert by_store(report)["document"].status == "absent"

    def test_a_shape_that_matches_no_version_is_refused_and_not_stamped(
        self, registry: StoreRegistry
    ) -> None:
        open_every_store(registry)
        foreign = db_path(registry, "document").with_name("other.db")
        conn = sqlite3.connect(foreign)
        conn.execute("CREATE TABLE unrelated (x INTEGER)")
        conn.commit()
        conn.close()
        # Point the document store at the foreign file by config.
        reg = StoreRegistry(
            config={
                "knowledge": {
                    "document": {"backend": "sqlite", "db_path": str(foreign)}
                }
            },
            stores_dir=registry.stores_dir,
        )

        report = run_migrate(reg, apply=True)

        doc = by_store(report)["document"]
        assert doc.status == "unrecognized"
        assert report.status == "error"
        assert VERSION_TABLE not in tables_of(foreign)


class TestStepsRunInOrderAndOnce:
    def test_steps_apply_once_in_version_order_with_a_row_each(
        self, registry: StoreRegistry
    ) -> None:
        open_every_store(registry)
        calls: list[int] = []
        chain = marker_chain(calls)

        first = run_migrate(registry, apply=True, migrations=chain)

        doc = by_store(first)["document"]
        assert doc.status == "applied"
        assert doc.applied == [2, 3]
        assert doc.current_version == 3
        assert doc.pending == []
        assert calls == [2, 3]
        path = db_path(registry, "document")
        assert {"step_2", "step_3"} <= tables_of(path)
        assert recorded_rows(path) == [
            ("document", 1, "baseline"),
            ("document", 2, "add step 2"),
            ("document", 3, "add step 3"),
        ]

        second = run_migrate(registry, apply=True, migrations=chain)

        assert by_store(second)["document"].status == "current"
        assert calls == [2, 3], "an applied step must not run again"
        # A store with no step of its own still stamps the baseline only.
        assert recorded_rows(db_path(registry, "trace")) == [("trace", 1, "baseline")]

    def test_a_failed_step_leaves_the_version_unstamped_and_the_schema_unchanged(
        self, registry: StoreRegistry
    ) -> None:
        open_every_store(registry)
        calls: list[int] = []

        report = run_migrate(
            registry, apply=True, migrations=marker_chain(calls, fail_at=3)
        )

        doc = by_store(report)["document"]
        assert report.status == "error"
        assert doc.status == "failed"
        assert doc.applied == [2]
        assert doc.current_version == 2
        assert "step to version 3 failed" in (doc.reason or "")
        assert "boom" in (doc.reason or "")
        path = db_path(registry, "document")
        tables = tables_of(path)
        assert "step_2" in tables, "the step before the failure stays applied"
        assert "step_3" not in tables, "the failed step's DDL rolled back"
        assert [r[1] for r in recorded_rows(path)] == [1, 2]
        # The other stores were not stopped by it.
        assert by_store(report)["trace"].status == "stamped"

    def test_a_failed_first_step_still_leaves_the_store_at_the_baseline(
        self, registry: StoreRegistry
    ) -> None:
        open_every_store(registry)

        report = run_migrate(
            registry, apply=True, migrations=marker_chain([], fail_at=2)
        )

        assert by_store(report)["document"].status == "failed"
        path = db_path(registry, "document")
        assert "step_2" not in tables_of(path)
        assert [r[1] for r in recorded_rows(path)] == [1]


class TestMigrationSetValidation:
    @staticmethod
    def step(version: int) -> Migration:
        return Migration("document", "sqlite", version, "x", lambda conn: None)

    def test_a_gap_is_rejected(self) -> None:
        with pytest.raises(ConfigError, match="no gap or repeat"):
            MigrationSet([self.step(2), self.step(4)])

    def test_a_repeat_is_rejected(self) -> None:
        with pytest.raises(ConfigError, match="no gap or repeat"):
            MigrationSet([self.step(2), self.step(2)])

    def test_a_chain_that_skips_the_first_step_is_rejected(self) -> None:
        with pytest.raises(ConfigError, match="no gap or repeat"):
            MigrationSet([self.step(3)])

    def test_chains_are_kept_per_store_and_backend(self) -> None:
        other = Migration("graph", "sqlite", 2, "x", lambda conn: None)
        chain = MigrationSet([self.step(2), other])
        assert chain.target("document", "sqlite") == 2
        assert chain.target("graph", "sqlite") == 2
        assert chain.target("document", "postgres") == BASELINE_VERSION
