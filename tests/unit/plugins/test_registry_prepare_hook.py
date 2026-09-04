"""Contract tests for backend-owned registry parameter preparation."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from types import ModuleType
from typing import Any, ClassVar
from unittest.mock import MagicMock, patch

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
