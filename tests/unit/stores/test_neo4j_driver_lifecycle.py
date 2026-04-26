"""Pure-unit tests for the Neo4j driver-lifecycle contract.

The live ``test_neo4j_*.py`` suites cover behavior against AuraDB; this
file covers the constructor wiring + ``_owns_driver`` semantics + the
registry-side driver sharing without needing a real Neo4j. Always runs
in CI (only requires the ``neo4j`` Python package, which is in the
``[neo4j]`` extra and pre-installed in the test venv).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("neo4j")

from trellis.stores.neo4j.base import DriverConfig
from trellis.stores.registry import StoreRegistry

# Hardcoded placeholder credential. ruff's S106 ("hardcoded password")
# would otherwise trigger on every Neo4jGraphStore(..., password=...)
# call below. Defining the constant once + bandit-safe-value comment
# scopes the suppression to one place.
_DUMMY_PASSWORD = "test-pw"  # noqa: S105 — test placeholder, not a real credential


def _silence_init_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub _init_schema on both stores so __init__ doesn't hit the network."""
    monkeypatch.setattr(
        "trellis.stores.neo4j.graph.Neo4jGraphStore._init_schema",
        lambda self: None,
    )
    monkeypatch.setattr(
        "trellis.stores.neo4j.vector.Neo4jVectorStore._init_schema",
        lambda self: None,
    )


class TestStoreOwnsBuiltDriver:
    """When no ``driver`` is injected, the store builds one and owns it."""

    def test_graph_store_owns_built_driver(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _silence_init_schema(monkeypatch)
        from trellis.stores.neo4j.graph import Neo4jGraphStore

        with patch("trellis.stores.neo4j.graph.build_driver") as mock_build:
            mock_build.return_value = MagicMock()
            store = Neo4jGraphStore("bolt://x", user="u", password=_DUMMY_PASSWORD)
            assert store._owns_driver is True
            mock_build.assert_called_once_with(
                "bolt://x", "u", _DUMMY_PASSWORD, config=None
            )

        store.close()
        store._driver.close.assert_called_once()

    def test_vector_store_owns_built_driver(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _silence_init_schema(monkeypatch)
        from trellis.stores.neo4j.vector import Neo4jVectorStore

        with patch("trellis.stores.neo4j.vector.build_driver") as mock_build:
            mock_build.return_value = MagicMock()
            store = Neo4jVectorStore(
                "bolt://x", user="u", password=_DUMMY_PASSWORD, dimensions=8
            )
            assert store._owns_driver is True

        store.close()
        store._driver.close.assert_called_once()

    def test_driver_config_flows_through_to_build(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _silence_init_schema(monkeypatch)
        from trellis.stores.neo4j.graph import Neo4jGraphStore

        cfg = DriverConfig(connection_timeout=2.0, max_connection_pool_size=5)
        with patch("trellis.stores.neo4j.graph.build_driver") as mock_build:
            mock_build.return_value = MagicMock()
            Neo4jGraphStore(
                "bolt://x", user="u", password=_DUMMY_PASSWORD, driver_config=cfg
            )
            mock_build.assert_called_once_with(
                "bolt://x", "u", _DUMMY_PASSWORD, config=cfg
            )


class TestStoreSkipsCloseOnInjectedDriver:
    """When a ``driver`` is injected, the store does NOT own it."""

    def test_graph_store_skips_close_on_injected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _silence_init_schema(monkeypatch)
        from trellis.stores.neo4j.graph import Neo4jGraphStore

        injected = MagicMock()
        store = Neo4jGraphStore("bolt://x", user="u", driver=injected)
        assert store._owns_driver is False
        assert store._driver is injected

        store.close()
        # Caller (registry) owns the driver — store.close() is a no-op.
        injected.close.assert_not_called()

    def test_vector_store_skips_close_on_injected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _silence_init_schema(monkeypatch)
        from trellis.stores.neo4j.vector import Neo4jVectorStore

        injected = MagicMock()
        store = Neo4jVectorStore("bolt://x", user="u", dimensions=8, driver=injected)
        assert store._owns_driver is False

        store.close()
        injected.close.assert_not_called()


class TestConstructorRejectsConflictingArgs:
    """Mixing injected ``driver`` with ``password`` / ``driver_config`` is an error."""

    def test_graph_store_rejects_driver_plus_password(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _silence_init_schema(monkeypatch)
        from trellis.stores.neo4j.graph import Neo4jGraphStore

        with pytest.raises(ValueError, match="not both"):
            Neo4jGraphStore(
                "bolt://x", user="u", password=_DUMMY_PASSWORD, driver=MagicMock()
            )

    def test_graph_store_rejects_driver_plus_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _silence_init_schema(monkeypatch)
        from trellis.stores.neo4j.graph import Neo4jGraphStore

        with pytest.raises(ValueError, match="not both"):
            Neo4jGraphStore(
                "bolt://x",
                user="u",
                driver=MagicMock(),
                driver_config=DriverConfig(),
            )

    def test_vector_store_rejects_driver_plus_password(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _silence_init_schema(monkeypatch)
        from trellis.stores.neo4j.vector import Neo4jVectorStore

        with pytest.raises(ValueError, match="not both"):
            Neo4jVectorStore(
                "bolt://x",
                user="u",
                password=_DUMMY_PASSWORD,
                dimensions=8,
                driver=MagicMock(),
            )

    def test_graph_store_requires_password_when_no_driver(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _silence_init_schema(monkeypatch)
        from trellis.stores.neo4j.graph import Neo4jGraphStore

        with pytest.raises(ValueError, match="password is required"):
            Neo4jGraphStore("bolt://x", user="u")

    def test_vector_store_requires_password_when_no_driver(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _silence_init_schema(monkeypatch)
        from trellis.stores.neo4j.vector import Neo4jVectorStore

        with pytest.raises(ValueError, match="password is required"):
            Neo4jVectorStore("bolt://x", user="u", dimensions=8)


class TestRegistrySharesDriverAcrossNeo4jStores:
    """The graph + vector pair against the same instance reuses one driver."""

    def _make_registry(self) -> StoreRegistry:
        # ``StoreRegistry``'s internal config is flat per-store-type
        # (the plane-split YAML shape is flattened by
        # ``_extract_store_config`` before construction).
        config = {
            "graph": {
                "backend": "neo4j",
                "uri": "bolt://localhost:7687",
                "user": "neo4j",
                "password": "secret",
            },
            "vector": {
                "backend": "neo4j",
                "uri": "bolt://localhost:7687",
                "user": "neo4j",
                "password": "secret",
                "dimensions": 8,
            },
        }
        return StoreRegistry(config=config)

    def test_graph_and_vector_share_one_driver(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _silence_init_schema(monkeypatch)

        with patch("trellis.stores.neo4j.base.GraphDatabase") as mock_gd:
            sentinel_driver = MagicMock(name="shared_driver")
            mock_gd.driver.return_value = sentinel_driver

            registry = self._make_registry()
            graph = registry.knowledge.graph_store
            vector = registry.knowledge.vector_store

            # Driver was constructed exactly once even though we built two
            # stores against the same (uri, user).
            assert mock_gd.driver.call_count == 1
            assert graph._driver is sentinel_driver
            assert vector._driver is sentinel_driver
            assert graph._owns_driver is False
            assert vector._owns_driver is False

    def test_close_closes_each_shared_driver_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _silence_init_schema(monkeypatch)

        with patch("trellis.stores.neo4j.base.GraphDatabase") as mock_gd:
            shared = MagicMock(name="shared_driver")
            mock_gd.driver.return_value = shared

            registry = self._make_registry()
            _ = registry.knowledge.graph_store
            _ = registry.knowledge.vector_store

            registry.close()
            # Stores' close() are no-ops on injected drivers; the
            # registry's close() closes the shared driver exactly once.
            shared.close.assert_called_once()

    def test_close_survives_individual_store_close_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _silence_init_schema(monkeypatch)

        with patch("trellis.stores.neo4j.base.GraphDatabase") as mock_gd:
            shared = MagicMock(name="shared_driver")
            mock_gd.driver.return_value = shared

            registry = self._make_registry()
            graph = registry.knowledge.graph_store
            _ = registry.knowledge.vector_store

            # Force the store's own close() to raise; registry should still
            # close the shared driver.
            monkeypatch.setattr(
                graph, "close", MagicMock(side_effect=RuntimeError("boom"))
            )
            registry.close()
            shared.close.assert_called_once()


class TestRegistryDriverConfigPlumbing:
    def test_driver_config_dict_in_params_becomes_driver_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _silence_init_schema(monkeypatch)
        config = {
            "graph": {
                "backend": "neo4j",
                "uri": "bolt://x",
                "user": "u",
                "password": "p",
                "driver_config": {
                    "connection_timeout": 5.0,
                    "max_connection_pool_size": 7,
                },
            }
        }
        with patch("trellis.stores.neo4j.base.GraphDatabase") as mock_gd:
            mock_gd.driver.return_value = MagicMock()
            registry = StoreRegistry(config=config)
            _ = registry.knowledge.graph_store
            kwargs = mock_gd.driver.call_args.kwargs
            assert kwargs["connection_timeout"] == 5.0
            assert kwargs["max_connection_pool_size"] == 7

    def test_invalid_driver_config_type_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _silence_init_schema(monkeypatch)
        config = {
            "graph": {
                "backend": "neo4j",
                "uri": "bolt://x",
                "user": "u",
                "password": "p",
                "driver_config": "not-a-dict",
            }
        }
        registry = StoreRegistry(config=config)
        with pytest.raises(TypeError, match="driver_config must be"):
            _ = registry.knowledge.graph_store
