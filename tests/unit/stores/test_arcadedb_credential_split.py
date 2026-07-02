"""ArcadeDB admin/runtime credential split (issue #193).

Privileged phases — database creation (``ensure_database``) and typed-
property / vector-index DDL — must run with the optional
``admin_user`` / ``admin_password`` pair when configured, while the
runtime Bolt driver and runtime HTTP SQL stick to the least-privilege
``user`` / ``password``. When the admin pair is absent everything falls
back to the runtime pair (single-credential deployments unchanged).

Pure-unit: network entry points are mocked; assertions are on which
credentials reach which call.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("neo4j")

from trellis.stores.registry import StoreRegistry

# Test placeholder credentials (S105/S106 would fire on inline literals).
_RUNTIME_PW = "runtime-pw"
_ADMIN_PW = "admin-pw"


def _silence_init_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the Bolt-backed _init_schema so __init__ never hits the network."""
    monkeypatch.setattr(
        "trellis.stores.arcadedb.graph.ArcadeDBGraphStore._init_schema",
        lambda self: None,
    )


def _split_config() -> dict[str, dict[str, str]]:
    return {
        "graph": {
            "backend": "arcadedb",
            "uri": "bolt://localhost:7687",
            "user": "trellis_app",
            "password": _RUNTIME_PW,
            "admin_user": "root",
            "admin_password": _ADMIN_PW,
            "database": "trellis_test",
            "http_url": "http://localhost:2480",
        },
    }


class TestRegistryGraphCredentialSplit:
    def test_privileged_phases_use_admin_pair_runtime_driver_does_not(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _silence_init_schema(monkeypatch)

        with (
            patch("trellis.stores.arcadedb.base.build_arcadedb_driver") as mock_build,
            patch("trellis.stores.arcadedb.base.ensure_database") as mock_ensure,
            patch(
                "trellis.stores.arcadedb.graph."
                "ArcadeDBGraphStore._init_arcadedb_edge_provenance_schema"
            ) as mock_migrate,
        ):
            mock_build.return_value = MagicMock()
            registry = StoreRegistry(config=_split_config())
            _ = registry.knowledge.graph_store

            # Database creation ran with the admin pair.
            mock_ensure.assert_called_once_with(
                "http://localhost:2480", "root", _ADMIN_PW, "trellis_test"
            )
            # Typed-property DDL ran with the admin pair.
            mock_migrate.assert_called_once_with(
                http_url="http://localhost:2480",
                user="root",
                password=_ADMIN_PW,
                database="trellis_test",
            )
            # The runtime Bolt driver never saw the admin secret.
            mock_build.assert_called_once()
            args = mock_build.call_args.args
            assert args[1] == "trellis_app"
            assert args[2] == _RUNTIME_PW

    def test_admin_params_not_forwarded_to_constructor(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The store constructor gets an injected driver; the admin pair
        is consumed by the registry and must not travel further."""
        _silence_init_schema(monkeypatch)
        registry = StoreRegistry(config=_split_config())

        with (
            patch("trellis.stores.arcadedb.base.build_arcadedb_driver") as mock_build,
            patch("trellis.stores.arcadedb.base.ensure_database"),
            patch(
                "trellis.stores.arcadedb.graph."
                "ArcadeDBGraphStore._init_arcadedb_edge_provenance_schema"
            ),
        ):
            mock_build.return_value = MagicMock()
            forwarded = registry._inject_arcadedb_driver(
                dict(_split_config()["graph"])
            )
        assert "admin_user" not in forwarded
        assert "admin_password" not in forwarded
        assert "password" not in forwarded  # driver XOR password mutex
        assert forwarded["user"] == "trellis_app"

    def test_fallback_without_admin_pair_uses_runtime_credentials(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Single-credential deployments keep the pre-#193 behavior."""
        _silence_init_schema(monkeypatch)
        config = _split_config()
        del config["graph"]["admin_user"]
        del config["graph"]["admin_password"]

        with (
            patch("trellis.stores.arcadedb.base.build_arcadedb_driver") as mock_build,
            patch("trellis.stores.arcadedb.base.ensure_database") as mock_ensure,
            patch(
                "trellis.stores.arcadedb.graph."
                "ArcadeDBGraphStore._init_arcadedb_edge_provenance_schema"
            ) as mock_migrate,
        ):
            mock_build.return_value = MagicMock()
            registry = StoreRegistry(config=config)
            _ = registry.knowledge.graph_store

            mock_ensure.assert_called_once_with(
                "http://localhost:2480", "trellis_app", _RUNTIME_PW, "trellis_test"
            )
            assert mock_migrate.call_args.kwargs["password"] == _RUNTIME_PW

    def test_admin_pair_resolves_from_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _silence_init_schema(monkeypatch)
        monkeypatch.setenv("TRELLIS_ARCADEDB_ADMIN_USER", "root")
        monkeypatch.setenv("TRELLIS_ARCADEDB_ADMIN_PASSWORD", _ADMIN_PW)
        config = _split_config()
        del config["graph"]["admin_user"]
        del config["graph"]["admin_password"]

        with (
            patch("trellis.stores.arcadedb.base.build_arcadedb_driver") as mock_build,
            patch("trellis.stores.arcadedb.base.ensure_database") as mock_ensure,
            patch(
                "trellis.stores.arcadedb.graph."
                "ArcadeDBGraphStore._init_arcadedb_edge_provenance_schema"
            ),
        ):
            mock_build.return_value = MagicMock()
            registry = StoreRegistry(config=config)
            _ = registry.knowledge.graph_store
            mock_ensure.assert_called_once_with(
                "http://localhost:2480", "root", _ADMIN_PW, "trellis_test"
            )
            assert mock_build.call_args.args[2] == _RUNTIME_PW


class TestGraphStoreDirectConstructionSplit:
    def test_store_owned_path_splits_credentials(self) -> None:
        from trellis.stores.arcadedb.graph import ArcadeDBGraphStore

        with (
            patch("trellis.stores.arcadedb.graph.build_arcadedb_driver") as mock_build,
            patch("trellis.stores.arcadedb.graph.ensure_database") as mock_ensure,
            patch.object(
                ArcadeDBGraphStore, "_init_arcadedb_edge_provenance_schema"
            ) as mock_migrate,
            patch.object(ArcadeDBGraphStore, "_init_schema", lambda self: None),
        ):
            mock_build.return_value = MagicMock()
            ArcadeDBGraphStore(
                "bolt://localhost:7687",
                user="trellis_app",
                password=_RUNTIME_PW,
                admin_user="root",
                admin_password=_ADMIN_PW,
                database="trellis_test",
                http_url="http://localhost:2480",
            )
            mock_ensure.assert_called_once_with(
                "http://localhost:2480", "root", _ADMIN_PW, "trellis_test"
            )
            mock_migrate.assert_called_once_with(
                http_url="http://localhost:2480",
                user="root",
                password=_ADMIN_PW,
                database="trellis_test",
            )
            assert mock_build.call_args.args[1] == "trellis_app"
            assert mock_build.call_args.args[2] == _RUNTIME_PW


class TestVectorStoreDdlSplit:
    def test_init_ddl_uses_admin_pair_runtime_sql_does_not(self) -> None:
        from trellis.stores.arcadedb.vector import ArcadeDBVectorStore

        with patch("trellis.stores.arcadedb.vector.execute_sql") as mock_sql:
            mock_sql.return_value = [{"cnt": 0}]
            store = ArcadeDBVectorStore(
                http_url="http://localhost:2480",
                user="trellis_app",
                password=_RUNTIME_PW,
                admin_user="root",
                admin_password=_ADMIN_PW,
                database="trellis_test",
                dimensions=3,
            )
            # Every init-time DDL statement carried the admin pair.
            ddl_calls = mock_sql.call_args_list
            assert len(ddl_calls) == 4  # vertex type + 2 properties + index
            for call in ddl_calls:
                assert call.args[1] == "root"
                assert call.args[2] == _ADMIN_PW

            mock_sql.reset_mock()
            # Runtime SQL sticks to the least-privilege pair.
            store.count()
            assert mock_sql.call_args.args[1] == "trellis_app"
            assert mock_sql.call_args.args[2] == _RUNTIME_PW

    def test_vector_fallback_without_admin_pair(self) -> None:
        from trellis.stores.arcadedb.vector import ArcadeDBVectorStore

        with patch("trellis.stores.arcadedb.vector.execute_sql") as mock_sql:
            ArcadeDBVectorStore(
                http_url="http://localhost:2480",
                user="trellis_app",
                password=_RUNTIME_PW,
                database="trellis_test",
                dimensions=3,
            )
            for call in mock_sql.call_args_list:
                assert call.args[1] == "trellis_app"
                assert call.args[2] == _RUNTIME_PW
