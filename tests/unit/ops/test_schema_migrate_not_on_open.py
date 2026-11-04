"""Nothing opens a store and thereby versions, or migrates, it.

``trellis admin migrate --apply`` is the only writer of the version record. A
step that ran when a store was opened would be a production mutation the next
time any process (the CLI after a pull, the API at start, the MCP server)
opened a database: no operator asked for it and no report said it was coming.

So every surface that opens stores is run here on a fresh deployment and on a
database a previous open created (the shape every existing deployment has), and
the schema is read back with ``sqlite_master`` and compared. The same suite
scans ``src/`` so a store, the registry or a server cannot import the migrator.

**What is pinned for Postgres.** Only the source scan. The behavioural tests read
``sqlite_master``; nothing here opens a Postgres store, so a mutation in a
Postgres store's ``_init_schema`` that created or wrote the version table would
be caught by the scan only if it imported the migrator or named the table.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

import trellis.mcp.wiring as mcp_wiring
from trellis.ops.schema_migrate import VERSION_TABLE
from trellis.stores.registry import _PLANE_OF, StoreRegistry
from trellis_api.app import get_registry, lifespan
from trellis_cli.main import app
from trellis_cli.stores import _get_registry, _reset_registry

if TYPE_CHECKING:
    from tests.structlog_isolation import IsolatedCliRunner


SRC = Path(__file__).resolve().parents[3] / "src"

#: The only modules that may name the migrator or its version table.
ALLOWED = {
    SRC / "trellis" / "ops" / "schema_migrate.py",
    SRC / "trellis_cli" / "admin_migrate.py",
}

# Hand-read: ``find src -name '*.py'`` listed 458 files when this was written.
# A scan that matched nothing would pass vacuously, so it must see most of them.
SOURCE_FILE_FLOOR = 400

# A word match: another module logs ``schema_migrated``, which is not this one.
NAMES_MIGRATOR = re.compile(rf"\bschema_migrate\b|\b{VERSION_TABLE}\b")


def snapshot(stores_dir: Path) -> dict[str, list[tuple[str, str, str, str | None]]]:
    """Every ``.db`` file under ``stores_dir`` -> its ``sqlite_master`` rows."""
    out: dict[str, list[tuple[str, str, str, str | None]]] = {}
    for path in sorted(stores_dir.glob("*.db")):
        uri = f"{path.resolve().as_uri()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            out[path.name] = conn.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master"
                " ORDER BY type, name"
            ).fetchall()
        finally:
            conn.close()
    return out


def version_tables(
    snap: dict[str, list[tuple[str, str, str, str | None]]],
) -> list[str]:
    return [
        f"{db}:{row[1]}"
        for db, rows in snap.items()
        for row in rows
        if VERSION_TABLE in row[1]
    ]


def open_every_store(registry: StoreRegistry) -> None:
    for store_type in _PLANE_OF:
        registry._get(store_type)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway deployment; returns its stores directory."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(tmp_path / "data"))
    _reset_registry()
    return tmp_path / "data" / "stores"


def seed_main_shaped(
    stores_dir: Path,
) -> dict[str, list[tuple[str, str, str, str | None]]]:
    """Open every store once, as every running deployment already has."""
    stores_dir.mkdir(parents=True, exist_ok=True)
    registry = StoreRegistry(stores_dir=stores_dir)
    open_every_store(registry)
    registry.close()
    snap = snapshot(stores_dir)
    assert snap, "opening the stores created no database"
    return snap


class TestRegistryOpen:
    def test_a_fresh_open_creates_no_version_table(self, env: Path) -> None:
        env.mkdir(parents=True)
        registry = StoreRegistry(stores_dir=env)
        open_every_store(registry)
        registry.close()

        assert version_tables(snapshot(env)) == []

    def test_reopening_existing_stores_changes_no_schema(self, env: Path) -> None:
        before = seed_main_shaped(env)

        registry = StoreRegistry(stores_dir=env)
        open_every_store(registry)
        registry.close()

        assert snapshot(env) == before

    def test_opening_after_apply_does_not_add_to_or_touch_the_record(
        self, env: Path
    ) -> None:
        """The record exists only because ``--apply`` wrote it; a later open
        neither adds a version nor drops the table."""
        from trellis.ops.schema_migrate import run_migrate

        seed_main_shaped(env)
        registry = StoreRegistry(stores_dir=env)
        run_migrate(registry, apply=True)
        registry.close()
        stamped = snapshot(env)
        assert version_tables(stamped), "apply stamped nothing; test is vacuous"

        registry = StoreRegistry(stores_dir=env)
        open_every_store(registry)
        registry.close()

        assert snapshot(env) == stamped


class TestEverySurfaceThatOpensStores:
    def test_admin_init_creates_no_version_table_and_is_stable(
        self, env: Path, cli_runner: IsolatedCliRunner
    ) -> None:
        first = cli_runner.invoke(app, ["admin", "init"])
        assert first.exit_code == 0, first.output
        # ``init`` itself creates no database; opening the stores does.
        open_every_store(_get_registry())
        seeded = snapshot(env)
        assert seeded, "no store was created; test is vacuous"
        assert version_tables(seeded) == []
        _reset_registry()

        second = cli_runner.invoke(app, ["admin", "init"])
        assert second.exit_code == 0, second.output
        open_every_store(_get_registry())

        assert snapshot(env) == seeded

    async def test_the_api_lifespan_changes_no_schema(self, env: Path) -> None:
        before = seed_main_shaped(env)

        async with lifespan(MagicMock()):
            open_every_store(get_registry())

        assert snapshot(env) == before

    async def test_the_api_lifespan_on_a_fresh_deployment_creates_no_record(
        self, env: Path
    ) -> None:
        async with lifespan(MagicMock()):
            open_every_store(get_registry())

        assert snapshot(env), "the lifespan opened no store; test is vacuous"
        assert version_tables(snapshot(env)) == []

    def test_the_mcp_server_registry_changes_no_schema(
        self, env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        before = seed_main_shaped(env)
        monkeypatch.setattr(mcp_wiring, "_registry", None)

        registry = mcp_wiring._get_registry()
        open_every_store(registry)
        registry.close()

        assert snapshot(env) == before


class TestNothingOnTheOpenPathImportsTheMigrator:
    def test_only_the_migrator_and_its_command_name_it(self) -> None:
        files = list(SRC.rglob("*.py"))
        assert len(files) >= SOURCE_FILE_FLOOR, len(files)
        assert set(files) >= ALLOWED, "an allowed module moved; update ALLOWED"
        offenders = [
            str(p.relative_to(SRC))
            for p in files
            if p not in ALLOWED and NAMES_MIGRATOR.search(p.read_text())
        ]
        assert not offenders, (
            "only trellis.ops.schema_migrate and trellis_cli.admin_migrate may name"
            f" the migrator or its version table; found in {offenders}"
        )
