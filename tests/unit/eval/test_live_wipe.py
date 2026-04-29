"""Smoke tests for the eval live-backend wipe helper.

Live PG / Neo4j wipe paths are exercised by 5.1 / 5.3 / 5.5 against
``.env``-configured backends. These unit tests cover the SQLite-only
no-op behavior + the schema-init ordering invariant: a fresh
SQLite registry can be wiped without erroring, no tables get
truncated, and every store property has been instantiated by the
time wipe_live_state returns.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from eval._live_wipe import wipe_live_state

from trellis.stores.registry import StoreRegistry


@pytest.fixture
def sqlite_registry(tmp_path: Path):
    config = {
        "knowledge": {
            "graph": {"backend": "sqlite"},
            "vector": {"backend": "sqlite"},
            "document": {"backend": "sqlite"},
            "blob": {"backend": "local"},
        },
        "operational": {
            "trace": {"backend": "sqlite"},
            "event_log": {"backend": "sqlite"},
        },
    }
    with StoreRegistry(config=config, stores_dir=tmp_path) as registry:
        yield registry


def test_wipe_is_noop_for_sqlite_registry(sqlite_registry) -> None:
    """SQLite stores have no Postgres ``_conn`` and no Neo4j driver, so
    every ``_wipe_*_store`` helper short-circuits via the type-name
    check. ``wipe_live_state`` returns cleanly without error."""
    wipe_live_state(sqlite_registry)


def test_wipe_materialises_every_store(sqlite_registry) -> None:
    """After ``wipe_live_state`` returns, the registry's store cache
    contains all five store types — proving the helper forces lazy
    schema init on every store the wipe could touch.
    """
    wipe_live_state(sqlite_registry)
    expected = {"graph", "vector", "document", "trace", "event_log"}
    assert expected.issubset(sqlite_registry._cache.keys())


def test_wipe_can_be_called_repeatedly(sqlite_registry) -> None:
    """Idempotent — calling the helper multiple times in succession
    against a SQLite registry doesn't error or produce side effects
    that break the next call."""
    for _ in range(3):
        wipe_live_state(sqlite_registry)
