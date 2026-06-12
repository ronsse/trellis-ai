"""Pure-unit coverage for the registry's ArcadeDB provenance-migration wiring.

The live ``test_arcadedb_graph.py`` suite proves end-to-end installation
of the typed-property schema against a real ArcadeDB. This file covers
the registry-side wiring without needing a live instance — it asserts
the registry calls the migration helper at the right boundaries
(new-driver + cached-driver paths) and forwards the right params to the
``ArcadeDBGraphStore`` constructor.

These tests reproduce the bug PRs #126 + #127 reviewers identified: the
registry used to strip ``http_url`` from forwarded params, leaving the
constructor's injected-driver branch unable to recognise the registry
path and logging a spurious "migration skipped" warning every boot. The
cached-driver short-circuit additionally bypassed the migration call
itself. Both gaps are covered below.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("neo4j")

from trellis.stores.registry import StoreRegistry

# Test placeholder credential. ruff's S105/S106 ("hardcoded password")
# would otherwise trigger on every ArcadeDB(..., password=...) call.
_DUMMY_PASSWORD = "test-pw"  # noqa: S105


def _silence_init_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the Bolt-backed _init_schema so __init__ never hits the network."""
    monkeypatch.setattr(
        "trellis.stores.arcadedb.graph.ArcadeDBGraphStore._init_schema",
        lambda self: None,
    )


def _arcadedb_graph_config() -> dict[str, dict[str, str]]:
    return {
        "graph": {
            "backend": "arcadedb",
            "uri": "bolt://localhost:7687",
            "user": "root",
            "password": _DUMMY_PASSWORD,
            "database": "trellis_test",
            "http_url": "http://localhost:2480",
        },
    }


class TestRegistryRunsMigrationOnNewDriverPath:
    """When the registry builds a new ArcadeDB Bolt driver, the typed-
    property migration helper must fire with all credentials in scope.
    """

    def test_migration_helper_called_with_full_credentials(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _silence_init_schema(monkeypatch)

        with (
            patch("trellis.stores.arcadedb.base.build_arcadedb_driver") as mock_build,
            patch("trellis.stores.arcadedb.base.ensure_database"),
            patch(
                "trellis.stores.arcadedb.graph."
                "ArcadeDBGraphStore._init_arcadedb_edge_provenance_schema"
            ) as mock_migrate,
        ):
            mock_build.return_value = MagicMock()
            registry = StoreRegistry(config=_arcadedb_graph_config())
            _ = registry.knowledge.graph_store
            mock_migrate.assert_called_once_with(
                http_url="http://localhost:2480",
                user="root",
                password=_DUMMY_PASSWORD,
                database="trellis_test",
            )

    def test_registry_records_key_as_migrated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """After a successful migration the (uri, user) key is recorded
        so a subsequent injection on the same registry doesn't repeat
        the HTTP DDL round-trip."""
        _silence_init_schema(monkeypatch)

        with (
            patch("trellis.stores.arcadedb.base.build_arcadedb_driver") as mock_build,
            patch("trellis.stores.arcadedb.base.ensure_database"),
            patch(
                "trellis.stores.arcadedb.graph."
                "ArcadeDBGraphStore._init_arcadedb_edge_provenance_schema"
            ),
        ):
            mock_build.return_value = MagicMock()
            registry = StoreRegistry(config=_arcadedb_graph_config())
            _ = registry.knowledge.graph_store
            assert (
                "bolt://localhost:7687",
                "root",
            ) in registry._arcadedb_provenance_migrated


class TestRegistryForwardsHttpUrlToConstructor:
    """``http_url`` must reach the constructor so the injected-driver
    branch can recognise the registry path and avoid the spurious
    "migration skipped" warning. ``password`` must remain stripped to
    preserve the constructor's "driver XOR password" mutex.
    """

    def test_http_url_forwarded_password_stripped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _silence_init_schema(monkeypatch)

        captured: dict[str, object] = {}

        # Spy on _inject_arcadedb_driver's returned params (what the
        # registry actually forwards to the store constructor).
        original = StoreRegistry._inject_arcadedb_driver

        def _spy(self: StoreRegistry, params: dict[str, object]) -> dict[str, object]:
            result = original(self, params)
            captured.update(result)
            return result

        with (
            patch("trellis.stores.arcadedb.base.build_arcadedb_driver") as mock_build,
            patch("trellis.stores.arcadedb.base.ensure_database"),
            patch(
                "trellis.stores.arcadedb.graph."
                "ArcadeDBGraphStore._init_arcadedb_edge_provenance_schema"
            ),
            patch.object(StoreRegistry, "_inject_arcadedb_driver", _spy),
        ):
            mock_build.return_value = MagicMock()
            registry = StoreRegistry(config=_arcadedb_graph_config())
            _ = registry.knowledge.graph_store

        # http_url forwarded — constructor uses this to suppress the
        # spurious "migration skipped" warning under the registry path.
        assert captured.get("http_url") == "http://localhost:2480"
        # password stripped — preserves the constructor's mutex.
        assert "password" not in captured
        # driver injected.
        assert captured.get("driver") is not None


class TestCachedDriverPathRunsMigration:
    """The cached-driver short-circuit used to silently bypass the
    typed-property migration. It must now invoke the helper exactly
    once per ``(uri, user)`` and record the key so repeat injections
    on the same registry are no-ops.
    """

    def test_cached_path_runs_migration_when_not_yet_recorded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _silence_init_schema(monkeypatch)
        registry = StoreRegistry()
        cached_driver = MagicMock(name="cached_driver")
        key = ("bolt://localhost:7687", "root")
        registry._bolt_drivers[key] = cached_driver
        # Deliberately do NOT pre-record migration for this key — force
        # the cached path to invoke the migration helper.
        assert key not in registry._arcadedb_provenance_migrated

        with patch(
            "trellis.stores.arcadedb.graph."
            "ArcadeDBGraphStore._init_arcadedb_edge_provenance_schema"
        ) as mock_migrate:
            params = {
                "uri": "bolt://localhost:7687",
                "user": "root",
                "password": _DUMMY_PASSWORD,
                "database": "trellis_test",
                "http_url": "http://localhost:2480",
            }
            result = registry._inject_arcadedb_driver(params)
            assert result["driver"] is cached_driver
            assert "password" not in result
            mock_migrate.assert_called_once_with(
                http_url="http://localhost:2480",
                user="root",
                password=_DUMMY_PASSWORD,
                database="trellis_test",
            )
            assert key in registry._arcadedb_provenance_migrated

    def test_cached_path_skips_migration_when_already_recorded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Second cached injection for the same (uri, user) is a true
        no-op — the recorded marker dodges the redundant HTTP round-
        trip."""
        _silence_init_schema(monkeypatch)
        registry = StoreRegistry()
        cached_driver = MagicMock(name="cached_driver")
        key = ("bolt://localhost:7687", "root")
        registry._bolt_drivers[key] = cached_driver
        registry._arcadedb_provenance_migrated.add(key)

        with patch(
            "trellis.stores.arcadedb.graph."
            "ArcadeDBGraphStore._init_arcadedb_edge_provenance_schema"
        ) as mock_migrate:
            params = {
                "uri": "bolt://localhost:7687",
                "user": "root",
                "password": _DUMMY_PASSWORD,
                "database": "trellis_test",
                "http_url": "http://localhost:2480",
            }
            registry._inject_arcadedb_driver(params)
            mock_migrate.assert_not_called()

    def test_cached_path_warns_when_credentials_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the cached path can't resolve http_url+password (the
        registry was constructed without them, e.g. driver pre-cached
        externally), it must warn rather than silently skipping."""
        _silence_init_schema(monkeypatch)
        registry = StoreRegistry()
        cached_driver = MagicMock(name="cached_driver")
        key = ("bolt://10.0.0.1:7687", "root")
        registry._bolt_drivers[key] = cached_driver

        with (
            patch(
                "trellis.stores.arcadedb.graph."
                "ArcadeDBGraphStore._init_arcadedb_edge_provenance_schema"
            ) as mock_migrate,
            patch("trellis.stores.registry.logger.warning") as mock_warning,
        ):
            # No password, no http_url in params. ``derive_http_url_
            # from_bolt`` parses "10.0.0.1" out of the URI but the
            # missing password still trips the warning branch.
            params = {
                "uri": "bolt://10.0.0.1:7687",
                "user": "root",
                "database": "trellis_test",
            }
            registry._inject_arcadedb_driver(params)
            mock_migrate.assert_not_called()
            assert mock_warning.called
            event = mock_warning.call_args.args[0]
            assert event == (
                "arcadedb_provenance_schema_migration_skipped_cached_driver"
            )


class TestConstructorBranchAcceptsRegistryPath:
    """The constructor's injected-driver branch must demote its
    "migration skipped" warning to debug when http_url is forwarded —
    the registry path is healthy and the spurious warning was firing
    on every boot.
    """

    def test_constructor_demotes_log_when_http_url_present(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _silence_init_schema(monkeypatch)
        from trellis.stores.arcadedb.graph import ArcadeDBGraphStore

        with patch("trellis.stores.arcadedb.graph.logger.warning") as mock_warning:
            ArcadeDBGraphStore(
                "bolt://x",
                user="u",
                driver=MagicMock(),
                http_url="http://x:2480",
            )
        # No warning should fire when http_url is forwarded — the
        # registry already ran the migration.
        for call in mock_warning.call_args_list:
            event = call.args[0] if call.args else None
            assert event != (
                "arcadedb_provenance_schema_migration_skipped_injected_driver"
            )

    def test_constructor_warns_when_no_http_url_on_injected_driver(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Direct callers that inject a driver WITHOUT forwarding
        http_url still get the warning — they may not have run the
        migration externally so the FLOAT MIN/MAX constraint is at
        risk."""
        _silence_init_schema(monkeypatch)
        from trellis.stores.arcadedb.graph import ArcadeDBGraphStore

        with patch("trellis.stores.arcadedb.graph.logger.warning") as mock_warning:
            ArcadeDBGraphStore("bolt://x", user="u", driver=MagicMock())
        warning_events = [
            call.args[0] for call in mock_warning.call_args_list if call.args
        ]
        assert (
            "arcadedb_provenance_schema_migration_skipped_injected_driver"
            in warning_events
        )
