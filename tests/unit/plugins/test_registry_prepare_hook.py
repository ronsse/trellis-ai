"""Contract tests for backend-owned registry parameter preparation."""

from __future__ import annotations

import importlib
import os
import sys
from dataclasses import dataclass
from types import ModuleType
from typing import Any, ClassVar
from unittest.mock import MagicMock, patch

import pytest

from trellis.errors import ConfigError
from trellis.plugins import loader
from trellis.stores.registry import StoreRegistry, _reset_backend_cache


@dataclass
class _FakeEntryPoint:
    name: str
    value: str
    dist: None = None


class _PreparedStore:
    prepared_calls: ClassVar[list[tuple[str, int]]] = []

    @classmethod
    def prepare_registry_params(
        cls,
        ctx: Any,
        store_type: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        call_number = ctx.shared.setdefault("synthetic:preparations", 0) + 1
        ctx.shared["synthetic:preparations"] = call_number
        cls.prepared_calls.append((store_type, call_number))
        if call_number == 1:
            ctx.register_closer(ctx.shared.setdefault("synthetic:closer", _Closer()))
        return {
            **params,
            "prepared_call": call_number,
            "env_value": ctx.env["TRELLIS_SYNTHETIC_VALUE"],
        }

    def __init__(
        self,
        *,
        configured: str,
        prepared_call: int,
        env_value: str,
    ) -> None:
        self.configured = configured
        self.prepared_call = prepared_call
        self.env_value = env_value

    def close(self) -> None:
        pass


class _Closer:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1


def test_plugin_hook_shares_context_and_registers_closer(
    monkeypatch: Any,
) -> None:
    module_name = "_trellis_synthetic_registry_plugin"
    module = ModuleType(module_name)
    module.PreparedStore = _PreparedStore
    monkeypatch.setitem(sys.modules, module_name, module)
    monkeypatch.setenv("TRELLIS_SYNTHETIC_VALUE", "from-env")

    def fake_entry_points(*, group: str) -> list[_FakeEntryPoint]:
        if group in {"trellis.stores.graph", "trellis.stores.vector"}:
            return [
                _FakeEntryPoint(
                    name="synthetic",
                    value=f"{module_name}:PreparedStore",
                )
            ]
        return []

    monkeypatch.setattr(loader, "entry_points", fake_entry_points)
    _reset_backend_cache()
    _PreparedStore.prepared_calls = []
    registry = StoreRegistry(
        config={
            "graph": {"backend": "synthetic", "configured": "graph"},
            "vector": {"backend": "synthetic", "configured": "vector"},
        }
    )

    graph = registry.knowledge.graph_store
    vector = registry.knowledge.vector_store

    assert graph.configured == "graph"
    assert graph.prepared_call == 1
    assert vector.configured == "vector"
    assert vector.prepared_call == 2
    assert graph.env_value == vector.env_value == "from-env"
    assert _PreparedStore.prepared_calls == [("graph", 1), ("vector", 2)]

    closer = registry._registry_shared["synthetic:closer"]
    registry.close()
    registry.close()
    assert closer.calls == 1


def test_neo4j_store_hooks_share_one_driver() -> None:
    from trellis.stores.neo4j.graph import Neo4jGraphStore
    from trellis.stores.neo4j.vector import Neo4jVectorStore

    registry = StoreRegistry()
    params = {
        "uri": "bolt://localhost:7687",
        "user": "neo4j",
        "password": "test-password",
    }
    driver = MagicMock()

    with patch("trellis.stores.neo4j.base.build_driver", return_value=driver) as build:
        graph = Neo4jGraphStore.prepare_registry_params(
            registry._registry_context("graph", "neo4j"),
            "graph",
            params,
        )
        vector = Neo4jVectorStore.prepare_registry_params(
            registry._registry_context("vector", "neo4j"),
            "vector",
            params,
        )

    build.assert_called_once()
    assert graph["driver"] is vector["driver"] is driver
    assert "password" not in graph
    assert "password" not in vector


def test_arcadedb_hook_tracks_migration_once_per_shared_driver() -> None:
    from trellis.stores.arcadedb.graph import ArcadeDBGraphStore

    registry = StoreRegistry()
    params = {
        "uri": "bolt://localhost:7687",
        "user": "root",
        "password": "test-password",
        "database": "trellis",
        "http_url": "http://localhost:2480",
    }
    driver = MagicMock()

    with (
        patch(
            "trellis.stores.arcadedb.graph.build_arcadedb_driver",
            return_value=driver,
        ),
        patch("trellis.stores.arcadedb.graph.ensure_database"),
        patch.object(
            ArcadeDBGraphStore,
            "_init_arcadedb_edge_provenance_schema",
        ) as migrate,
    ):
        first = ArcadeDBGraphStore.prepare_registry_params(
            registry._registry_context("graph", "arcadedb"),
            "graph",
            params,
        )
        second = ArcadeDBGraphStore.prepare_registry_params(
            registry._registry_context("graph", "arcadedb"),
            "graph",
            params,
        )

    assert first["driver"] is second["driver"] is driver
    migrate.assert_called_once()


# The ``requires '<key>'`` messages the neo4j and arcadedb registry hooks raise,
# each reached with the key present in the store's config but empty and its env
# var unset. An empty value counts as unset, so the message must not read as if
# the key were absent.
@pytest.mark.parametrize(
    ("store_path", "store_type", "params", "key", "env_var"),
    [
        pytest.param(
            "neo4j.graph:Neo4jGraphStore",
            "graph",
            {"uri": ""},
            "uri",
            "TRELLIS_NEO4J_URI",
            id="neo4j-uri",
        ),
        pytest.param(
            "neo4j.graph:Neo4jGraphStore",
            "graph",
            {"uri": "bolt://localhost:7687", "password": ""},
            "password",
            "TRELLIS_NEO4J_PASSWORD",
            id="neo4j-password",
        ),
        pytest.param(
            "arcadedb.graph:ArcadeDBGraphStore",
            "graph",
            {"uri": ""},
            "uri",
            "TRELLIS_ARCADEDB_URI",
            id="arcadedb-graph-uri",
        ),
        pytest.param(
            "arcadedb.graph:ArcadeDBGraphStore",
            "graph",
            {"uri": "bolt://localhost:7687", "password": ""},
            "password",
            "TRELLIS_ARCADEDB_PASSWORD",
            id="arcadedb-graph-password",
        ),
        pytest.param(
            "arcadedb.vector:ArcadeDBVectorStore",
            "vector",
            {"http_url": ""},
            "http_url",
            "TRELLIS_ARCADEDB_HTTP_URL",
            id="arcadedb-vector-http_url",
        ),
        pytest.param(
            "arcadedb.vector:ArcadeDBVectorStore",
            "vector",
            {"http_url": "http://localhost:2480", "password": ""},
            "password",
            "TRELLIS_ARCADEDB_PASSWORD",
            id="arcadedb-vector-password",
        ),
    ],
)
def test_an_empty_connection_value_is_named_as_empty_not_missing(
    monkeypatch: pytest.MonkeyPatch,
    store_path: str,
    store_type: str,
    params: dict[str, str],
    key: str,
    env_var: str,
) -> None:
    for name in list(os.environ):
        if name.startswith(("TRELLIS_NEO4J_", "TRELLIS_ARCADEDB_")):
            monkeypatch.delenv(name)
    module_name, class_name = store_path.split(":")
    store_cls = getattr(
        importlib.import_module(f"trellis.stores.{module_name}"), class_name
    )
    backend = module_name.split(".")[0]
    registry = StoreRegistry()

    with pytest.raises(ConfigError) as excinfo:
        store_cls.prepare_registry_params(
            registry._registry_context(store_type, backend), store_type, params
        )

    assert f"requires a non-empty '{key}' in config or {env_var}" in str(excinfo.value)
    assert excinfo.value.setting == f"stores.{store_type}.{key}"
