"""``StoreRegistry.configured_backend`` and ``configured_sqlite_path``.

Two read-only questions a caller can ask without opening a store: which
backend a store type is configured for, and — for SQLite — which file it
would open. ``trellis admin health`` asks both (F1), and it must not
connect to find out: building a store is what connects, and building a
SQLite store is what creates the stores dir and the file health reports on.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from trellis.errors import ValidationError
from trellis.stores.registry import _PLANE_OF, StoreRegistry, _reset_backend_cache

#: The default file of each store health checks — literal, not imported, so
#: the accessor is pinned against the names rather than against itself.
_DEFAULT_FILES = {
    "document": "documents.db",
    "graph": "graph.db",
    "vector": "vectors.db",
    "event_log": "events.db",
    "trace": "traces.db",
}

#: Backends that would fail loudly if anything tried to build them here:
#: the DSN env vars are unset in these tests, and no neo4j is listening.
_UNBUILDABLE = {
    "knowledge": {
        "document": {"backend": "postgres"},
        "vector": "pgvector",
        "graph": {"backend": "neo4j", "uri": "bolt://127.0.0.1:1"},
    },
    "operational": {"trace": {"backend": "postgres"}},
}


@pytest.fixture(autouse=True)
def _no_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRELLIS_KNOWLEDGE_PG_DSN", raising=False)
    monkeypatch.delenv("TRELLIS_OPERATIONAL_PG_DSN", raising=False)


def test_backend_reads_dict_form_string_form_and_defaults(tmp_path: Path) -> None:
    registry = StoreRegistry.from_config_dict(_UNBUILDABLE, data_dir=tmp_path)

    assert {st: registry.configured_backend(st) for st in _PLANE_OF} == {
        "graph": "neo4j",
        "vector": "pgvector",
        "document": "postgres",
        "blob": "local",
        "trace": "postgres",
        "event_log": "sqlite",
        "outcome": "sqlite",
        "parameter": "sqlite",
        "tuner_state": "sqlite",
        "api_key": "sqlite",
    }


@pytest.mark.parametrize(
    "document",
    [
        {"backend": "postgress"},
        {"backend": None},
        {"backend": {"password": "p"}},
        "postgresql://u:p@h/db",
        None,
    ],
    ids=["typo", "null", "map", "dsn", "null-store"],
)
def test_a_value_that_names_no_registered_backend_is_none(
    tmp_path: Path, document: object
) -> None:
    # Never the value itself: a DSN written where a name belongs would reach
    # whoever prints the answer (``admin health`` does).
    config = {"knowledge": {"document": document}}
    registry = StoreRegistry.from_config_dict(config, data_dir=tmp_path)

    assert registry.configured_backend("document") is None


def test_a_plugin_backend_is_a_registered_name(tmp_path: Path) -> None:
    def entry_points(*, group: str) -> list[SimpleNamespace]:
        if group != "trellis.stores.document":
            return []
        return [SimpleNamespace(name="custom", value="pkg.mod:Store")]

    config = {"knowledge": {"document": {"backend": "custom"}}}
    registry = StoreRegistry.from_config_dict(config, data_dir=tmp_path)
    with patch("trellis.plugins.loader.entry_points", side_effect=entry_points):
        _reset_backend_cache()
        try:
            assert registry.configured_backend("document") == "custom"
        finally:
            _reset_backend_cache()


@pytest.mark.parametrize("accessor", ["configured_backend", "configured_sqlite_path"])
def test_unknown_store_type_raises_rather_than_defaulting(
    tmp_path: Path, accessor: str
) -> None:
    # ``_resolve_backend`` alone answers "sqlite" for any name it has never
    # heard of; a typo must not read as a healthy default.
    registry = StoreRegistry.from_config_dict({}, data_dir=tmp_path)

    with pytest.raises(ValidationError, match="documents"):
        getattr(registry, accessor)("documents")


def test_resolving_builds_nothing_and_creates_nothing(tmp_path: Path) -> None:
    registry = StoreRegistry.from_config_dict(_UNBUILDABLE, data_dir=tmp_path)

    for store_type in _PLANE_OF:
        registry.configured_backend(store_type)
        registry.configured_sqlite_path(store_type)

    assert list(tmp_path.iterdir()) == []


def test_sqlite_path_defaults_under_stores_dir(tmp_path: Path) -> None:
    registry = StoreRegistry.from_config_dict({}, data_dir=tmp_path)

    assert {st: registry.configured_sqlite_path(st) for st in _DEFAULT_FILES} == {
        st: tmp_path / "stores" / name for st, name in _DEFAULT_FILES.items()
    }


def test_explicit_db_path_wins(tmp_path: Path) -> None:
    explicit = tmp_path / "elsewhere" / "docs.sqlite"
    config = {
        "knowledge": {"document": {"backend": "sqlite", "db_path": str(explicit)}}
    }
    registry = StoreRegistry.from_config_dict(config, data_dir=tmp_path)

    assert registry.configured_sqlite_path("document") == explicit
    assert registry.configured_sqlite_path("graph") == tmp_path / "stores" / "graph.db"


def test_non_sqlite_backends_have_no_sqlite_path(tmp_path: Path) -> None:
    registry = StoreRegistry.from_config_dict(_UNBUILDABLE, data_dir=tmp_path)

    for store_type in ("document", "vector", "graph", "trace", "blob"):
        assert registry.configured_sqlite_path(store_type) is None, store_type
    assert registry.configured_sqlite_path("event_log") == (
        tmp_path / "stores" / "events.db"
    )


def test_no_stores_dir_and_no_db_path_has_no_sqlite_path() -> None:
    registry = StoreRegistry(config={})

    assert registry.configured_backend("document") == "sqlite"
    assert registry.configured_sqlite_path("document") is None


def test_paths_are_the_files_building_the_stores_creates(tmp_path: Path) -> None:
    """The accessor and ``_instantiate`` must not drift apart."""
    explicit = tmp_path / "elsewhere" / "docs.sqlite"
    explicit.parent.mkdir()
    config = {
        "knowledge": {"document": {"backend": "sqlite", "db_path": str(explicit)}}
    }
    registry = StoreRegistry.from_config_dict(config, data_dir=tmp_path)
    resolved = {st: registry.configured_sqlite_path(st) for st in _DEFAULT_FILES}

    with registry:
        registry.knowledge.document_store  # noqa: B018 - building is the point
        registry.knowledge.graph_store  # noqa: B018
        registry.knowledge.vector_store  # noqa: B018
        registry.operational.event_log  # noqa: B018
        registry.operational.trace_store  # noqa: B018
        created = {
            path
            for path in tmp_path.rglob("*")
            if path.is_file() and path.suffix in {".db", ".sqlite"}
        }

    assert created == set(resolved.values())
