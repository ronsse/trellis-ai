"""Pure-unit tests for the Neo4j driver-lifecycle contract.

The live ``test_neo4j_*.py`` suites cover behavior against AuraDB; this
file covers the constructor wiring + ``_owns_driver`` semantics + the
registry-side driver sharing without needing a real Neo4j. Always runs
in CI (only requires the ``neo4j`` Python package, which is in the
``[neo4j]`` extra and pre-installed in the test venv).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

pytest.importorskip("neo4j")

from trellis.errors import ConfigError
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


# Distinct per field and per source, so a crossed wire reads as the wrong
# value instead of a coincidental match.
_NEO4J_ENV = {
    "TRELLIS_NEO4J_URI": "bolt://env-host-7q:7687",
    "TRELLIS_NEO4J_USER": "env-user-3k",
    "TRELLIS_NEO4J_PASSWORD": "env-pw-9x",
    "TRELLIS_NEO4J_DATABASE": "env-db-5m",
}
_NEO4J_CFG = {
    "uri": "bolt://cfg-host-2d:7687",
    "user": "cfg-user-6h",
    "password": "cfg-pw-4j",
    "database": "cfg-db-8n",
}


class TestNeo4jEnvFallback:
    """Each connection field resolves config key, then env var.

    ``user`` and ``database`` then default to ``neo4j``; ``uri`` and
    ``password`` have no default. Driven through ``from_config_dir`` and a
    real ``config.yaml``, because that is the path a deployment takes:
    config names the backend and the ``TRELLIS_NEO4J_*`` variables supply
    the connection.
    """

    @pytest.fixture
    def driver_factory(self, monkeypatch: pytest.MonkeyPatch) -> Iterator[MagicMock]:
        for name in _NEO4J_ENV:
            monkeypatch.delenv(name, raising=False)
        _silence_init_schema(monkeypatch)
        with patch("trellis.stores.neo4j.base.GraphDatabase") as mock_gd:
            mock_gd.driver.return_value = MagicMock(name="registry_driver")
            yield mock_gd.driver

    def _registry(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        config_keys: tuple[str, ...] = (),
        env_vars: tuple[str, ...] = (),
    ) -> StoreRegistry:
        for name in env_vars:
            monkeypatch.setenv(name, _NEO4J_ENV[name])
        conn = {key: _NEO4J_CFG[key] for key in config_keys}
        knowledge = {
            "graph": {"backend": "neo4j", **conn},
            "vector": {"backend": "neo4j", "dimensions": 3, **conn},
        }
        config_dir = tmp_path / "cfg"
        config_dir.mkdir()
        (config_dir / "config.yaml").write_text(
            yaml.safe_dump({"knowledge": knowledge})
        )
        return StoreRegistry.from_config_dir(config_dir, tmp_path / "data")

    def test_env_supplies_every_field(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        driver_factory: MagicMock,
    ) -> None:
        registry = self._registry(tmp_path, monkeypatch, env_vars=tuple(_NEO4J_ENV))
        graph = registry.knowledge.graph_store
        vector = registry.knowledge.vector_store

        assert driver_factory.call_count == 1
        assert driver_factory.call_args.args[0] == "bolt://env-host-7q:7687"
        assert driver_factory.call_args.kwargs["auth"] == ("env-user-3k", "env-pw-9x")
        assert graph._driver is driver_factory.return_value
        assert vector._driver is driver_factory.return_value
        assert graph._database == "env-db-5m"
        assert vector._database == "env-db-5m"

    def test_config_key_wins_over_env_for_every_field(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        driver_factory: MagicMock,
    ) -> None:
        registry = self._registry(
            tmp_path,
            monkeypatch,
            config_keys=tuple(_NEO4J_CFG),
            env_vars=tuple(_NEO4J_ENV),
        )
        graph = registry.knowledge.graph_store
        vector = registry.knowledge.vector_store

        assert driver_factory.call_count == 1
        assert driver_factory.call_args.args[0] == "bolt://cfg-host-2d:7687"
        assert driver_factory.call_args.kwargs["auth"] == ("cfg-user-6h", "cfg-pw-4j")
        assert graph._database == "cfg-db-8n"
        assert vector._database == "cfg-db-8n"

    @pytest.mark.parametrize("env_value", [None, ""], ids=["unset", "empty"])
    def test_user_and_database_default_to_neo4j(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        driver_factory: MagicMock,
        env_value: str | None,
    ) -> None:
        # An exported-but-empty variable counts as unset, so it falls
        # through to the default instead of authenticating as "".
        if env_value is not None:
            monkeypatch.setenv("TRELLIS_NEO4J_USER", env_value)
            monkeypatch.setenv("TRELLIS_NEO4J_DATABASE", env_value)
        registry = self._registry(
            tmp_path,
            monkeypatch,
            env_vars=("TRELLIS_NEO4J_URI", "TRELLIS_NEO4J_PASSWORD"),
        )
        graph = registry.knowledge.graph_store
        vector = registry.knowledge.vector_store

        assert driver_factory.call_args.args[0] == "bolt://env-host-7q:7687"
        assert driver_factory.call_args.kwargs["auth"] == ("neo4j", "env-pw-9x")
        assert graph._database == "neo4j"
        assert vector._database == "neo4j"

    def test_each_field_resolves_independently(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        driver_factory: MagicMock,
    ) -> None:
        registry = self._registry(
            tmp_path,
            monkeypatch,
            config_keys=("uri", "user", "database"),
            env_vars=("TRELLIS_NEO4J_PASSWORD",),
        )
        graph = registry.knowledge.graph_store
        vector = registry.knowledge.vector_store

        assert driver_factory.call_args.args[0] == "bolt://cfg-host-2d:7687"
        assert driver_factory.call_args.kwargs["auth"] == ("cfg-user-6h", "env-pw-9x")
        assert graph._database == "cfg-db-8n"
        assert vector._database == "cfg-db-8n"

    @pytest.mark.parametrize("env_uri", [None, ""], ids=["unset", "empty"])
    def test_missing_uri_names_the_env_var(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        driver_factory: MagicMock,
        env_uri: str | None,
    ) -> None:
        # The password is present, so the URI is the only thing missing.
        if env_uri is not None:
            monkeypatch.setenv("TRELLIS_NEO4J_URI", env_uri)
        registry = self._registry(
            tmp_path, monkeypatch, env_vars=("TRELLIS_NEO4J_PASSWORD",)
        )
        with pytest.raises(ConfigError, match="TRELLIS_NEO4J_URI") as excinfo:
            _ = registry.knowledge.graph_store
        assert excinfo.value.setting == "stores.graph.uri"

    @pytest.mark.parametrize("env_password", [None, ""], ids=["unset", "empty"])
    def test_missing_password_names_the_env_var(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        driver_factory: MagicMock,
        env_password: str | None,
    ) -> None:
        if env_password is not None:
            monkeypatch.setenv("TRELLIS_NEO4J_PASSWORD", env_password)
        registry = self._registry(tmp_path, monkeypatch, config_keys=("uri",))
        with pytest.raises(ConfigError, match="TRELLIS_NEO4J_PASSWORD") as excinfo:
            _ = registry.knowledge.graph_store
        assert excinfo.value.setting == "stores.graph.password"

    def test_validate_pings_the_env_resolved_driver(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        driver_factory: MagicMock,
    ) -> None:
        # ``_check_bolt_connectivity`` pings only cache keys whose parts are
        # all strings, so a key built from unresolved params would make the
        # connectivity check pass while pinging nothing.
        registry = self._registry(tmp_path, monkeypatch, env_vars=tuple(_NEO4J_ENV))
        registry.validate(store_types=["graph", "vector"], check_connectivity=True)

        cached = {
            key
            for shared in registry._registry_shared.values()
            if isinstance(shared, dict)
            for key in shared
        }
        assert cached == {("bolt://env-host-7q:7687", "env-user-3k")}
        assert driver_factory.return_value.verify_connectivity.call_count == 1
