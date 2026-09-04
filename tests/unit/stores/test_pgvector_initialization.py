"""Unit coverage for pgvector initialization ordering."""

from __future__ import annotations

import importlib
import sys
from collections.abc import Iterator
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

from trellis.errors import ConfigError


@pytest.fixture
def pgvector_module(monkeypatch: pytest.MonkeyPatch) -> Iterator[ModuleType]:
    class PsycopgError(Exception):
        pass

    class InsufficientPrivilegeError(PsycopgError):
        pass

    psycopg = ModuleType("psycopg")
    psycopg.Error = PsycopgError  # type: ignore[attr-defined]
    psycopg.errors = type(  # type: ignore[attr-defined]
        "errors",
        (),
        {"InsufficientPrivilege": InsufficientPrivilegeError},
    )
    psycopg.connect = MagicMock()  # type: ignore[attr-defined]

    psycopg_pool = ModuleType("psycopg_pool")
    psycopg_pool.ConnectionPool = MagicMock()  # type: ignore[attr-defined]

    pgvector = ModuleType("pgvector")
    pgvector.__path__ = []  # type: ignore[attr-defined]
    pgvector_psycopg = ModuleType("pgvector.psycopg")
    pgvector_psycopg.register_vector = MagicMock()  # type: ignore[attr-defined]

    module_names = (
        "trellis.stores.pgvector",
        "trellis.stores.pgvector.store",
        "trellis.stores.postgres.base",
    )
    previous = {name: sys.modules.get(name) for name in module_names}
    for name in module_names:
        sys.modules.pop(name, None)
    monkeypatch.setitem(sys.modules, "psycopg", psycopg)
    monkeypatch.setitem(sys.modules, "psycopg_pool", psycopg_pool)
    monkeypatch.setitem(sys.modules, "pgvector", pgvector)
    monkeypatch.setitem(sys.modules, "pgvector.psycopg", pgvector_psycopg)

    yield importlib.import_module("trellis.stores.pgvector.store")

    for name in module_names:
        sys.modules.pop(name, None)
        if previous[name] is not None:
            sys.modules[name] = previous[name]


def _connection_with_cursor() -> tuple[MagicMock, MagicMock]:
    connection = MagicMock()
    connection.__enter__.return_value = connection
    cursor = MagicMock()
    connection.cursor.return_value.__enter__.return_value = cursor
    return connection, cursor


def test_extension_is_created_before_registered_pool_opens(
    pgvector_module: ModuleType,
) -> None:
    events: list[str] = []
    bootstrap_connection, bootstrap_cursor = _connection_with_cursor()
    pooled_connection, pooled_cursor = _connection_with_cursor()
    pooled_cursor.fetchone.return_value = ("vector(3)",)

    pool = MagicMock()
    pool.connection.return_value.__enter__.return_value = pooled_connection

    def connect(*args: object, **kwargs: object) -> MagicMock:
        events.append("plain_connect")
        return bootstrap_connection

    def execute_bootstrap(sql: str) -> None:
        if sql == "CREATE EXTENSION IF NOT EXISTS vector":
            events.append("create_extension")

    def create_pool(*args: object, **kwargs: object) -> MagicMock:
        events.append("create_pool")
        return pool

    bootstrap_cursor.execute.side_effect = execute_bootstrap

    with (
        patch.object(pgvector_module.psycopg, "connect", side_effect=connect),
        patch(
            "trellis.stores.postgres.base.ConnectionPool",
            side_effect=create_pool,
        ),
    ):
        store = pgvector_module.PgVectorStore(
            "postgresql://example/test",
            dimensions=3,
        )

    assert events[:3] == ["plain_connect", "create_extension", "create_pool"]
    store.close()


def test_extension_provisioning_failure_is_actionable_and_opens_no_pool(
    pgvector_module: ModuleType,
) -> None:
    connection, cursor = _connection_with_cursor()
    cursor.execute.side_effect = pgvector_module.psycopg.errors.InsufficientPrivilege(
        "permission denied to create extension vector"
    )

    with (
        patch.object(pgvector_module.psycopg, "connect", return_value=connection),
        patch("trellis.stores.postgres.base.ConnectionPool") as create_pool,
        pytest.raises(ConfigError, match="CREATE EXTENSION vector"),
    ):
        pgvector_module.PgVectorStore(
            "postgresql://example/test",
            dimensions=3,
        )

    create_pool.assert_not_called()
