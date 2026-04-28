"""Tests for ``StoreRegistry.validate(check_connectivity=...)``.

Covers Phase 1.3 of plan-neo4j-hardening.md: the optional Bolt
round-trip that turns "Neo4j unreachable" from an opaque first-request
Bolt error into a startup failure aggregated alongside config errors.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("neo4j")

from trellis.stores.registry import (
    RegistryValidationError,
    StoreRegistry,
    _resolve_connectivity_check,
)

_DUMMY_PASSWORD = "test-pw"  # noqa: S105 — test placeholder, not a real credential


def _silence_init_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "trellis.stores.neo4j.graph.Neo4jGraphStore._init_schema",
        lambda self: None,
    )
    monkeypatch.setattr(
        "trellis.stores.neo4j.vector.Neo4jVectorStore._init_schema",
        lambda self: None,
    )


def _neo4j_only_config() -> dict[str, dict[str, str]]:
    return {
        "graph": {
            "backend": "neo4j",
            "uri": "bolt://localhost:7687",
            "user": "neo4j",
            "password": _DUMMY_PASSWORD,
        },
    }


# ---------------------------------------------------------------------------
# Env-var resolver
# ---------------------------------------------------------------------------


class TestResolveConnectivityCheck:
    def test_default_is_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TRELLIS_VALIDATE_CONNECTIVITY", raising=False)
        assert _resolve_connectivity_check(None) is False

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "ON"])
    def test_truthy_env_values_enable(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        monkeypatch.setenv("TRELLIS_VALIDATE_CONNECTIVITY", value)
        assert _resolve_connectivity_check(None) is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", "", "anything"])
    def test_falsy_env_values_disable(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        monkeypatch.setenv("TRELLIS_VALIDATE_CONNECTIVITY", value)
        assert _resolve_connectivity_check(None) is False

    def test_explicit_true_overrides_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TRELLIS_VALIDATE_CONNECTIVITY", "no")
        assert _resolve_connectivity_check(True) is True

    def test_explicit_false_overrides_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRELLIS_VALIDATE_CONNECTIVITY", "yes")
        assert _resolve_connectivity_check(False) is False


# ---------------------------------------------------------------------------
# validate() with connectivity check
# ---------------------------------------------------------------------------


class TestValidateConnectivityDefault:
    def test_default_does_not_ping(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _silence_init_schema(monkeypatch)
        monkeypatch.delenv("TRELLIS_VALIDATE_CONNECTIVITY", raising=False)
        with patch("trellis.stores.neo4j.base.GraphDatabase") as mock_gd:
            driver = MagicMock(name="driver")
            mock_gd.driver.return_value = driver
            registry = StoreRegistry(config=_neo4j_only_config())
            registry.validate(store_types=["graph"])
            driver.verify_connectivity.assert_not_called()


class TestValidateConnectivityExplicit:
    def test_explicit_true_pings_each_neo4j_driver(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _silence_init_schema(monkeypatch)
        with patch("trellis.stores.neo4j.base.GraphDatabase") as mock_gd:
            driver = MagicMock(name="driver")
            mock_gd.driver.return_value = driver
            registry = StoreRegistry(config=_neo4j_only_config())
            registry.validate(store_types=["graph"], check_connectivity=True)
            driver.verify_connectivity.assert_called_once()

    def test_pings_each_distinct_uri_user_pair(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _silence_init_schema(monkeypatch)
        config = {
            "graph": {
                "backend": "neo4j",
                "uri": "bolt://a:7687",
                "user": "neo4j",
                "password": _DUMMY_PASSWORD,
            },
            "vector": {
                "backend": "neo4j",
                "uri": "bolt://b:7687",
                "user": "neo4j",
                "password": _DUMMY_PASSWORD,
                "dimensions": 8,
            },
        }
        with patch("trellis.stores.neo4j.base.GraphDatabase") as mock_gd:
            drivers = [MagicMock(name="driver_a"), MagicMock(name="driver_b")]
            mock_gd.driver.side_effect = drivers
            registry = StoreRegistry(config=config)
            registry.validate(store_types=["graph", "vector"], check_connectivity=True)
            drivers[0].verify_connectivity.assert_called_once()
            drivers[1].verify_connectivity.assert_called_once()

    def test_shared_driver_is_pinged_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # graph + vector against the same instance → one driver in the
        # cache → one ping.
        _silence_init_schema(monkeypatch)
        config = {
            "graph": {
                "backend": "neo4j",
                "uri": "bolt://x:7687",
                "user": "neo4j",
                "password": _DUMMY_PASSWORD,
            },
            "vector": {
                "backend": "neo4j",
                "uri": "bolt://x:7687",
                "user": "neo4j",
                "password": _DUMMY_PASSWORD,
                "dimensions": 8,
            },
        }
        with patch("trellis.stores.neo4j.base.GraphDatabase") as mock_gd:
            shared = MagicMock(name="shared")
            mock_gd.driver.return_value = shared
            registry = StoreRegistry(config=config)
            registry.validate(store_types=["graph", "vector"], check_connectivity=True)
            shared.verify_connectivity.assert_called_once()


class TestValidateConnectivityFailures:
    def test_failure_aggregated_into_registry_validation_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _silence_init_schema(monkeypatch)
        with patch("trellis.stores.neo4j.base.GraphDatabase") as mock_gd:
            driver = MagicMock(name="driver")
            driver.verify_connectivity.side_effect = RuntimeError(
                "ServiceUnavailable: cannot connect"
            )
            mock_gd.driver.return_value = driver
            registry = StoreRegistry(config=_neo4j_only_config())
            with pytest.raises(RegistryValidationError) as excinfo:
                registry.validate(store_types=["graph"], check_connectivity=True)
            errors = excinfo.value.errors
            assert len(errors) == 1
            label, exc = errors[0]
            assert label.startswith("neo4j-driver:bolt://")
            assert "ServiceUnavailable" in str(exc)

    def test_connectivity_failure_aggregates_with_config_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _silence_init_schema(monkeypatch)
        # graph: valid neo4j; trace: postgres without a DSN → config error.
        config = {
            "graph": {
                "backend": "neo4j",
                "uri": "bolt://x:7687",
                "user": "neo4j",
                "password": _DUMMY_PASSWORD,
            },
            "trace": {"backend": "postgres"},  # missing dsn → ValueError
        }
        monkeypatch.delenv("TRELLIS_KNOWLEDGE_PG_DSN", raising=False)
        monkeypatch.delenv("TRELLIS_OPERATIONAL_PG_DSN", raising=False)
        monkeypatch.delenv("TRELLIS_PG_DSN", raising=False)

        with patch("trellis.stores.neo4j.base.GraphDatabase") as mock_gd:
            driver = MagicMock(name="driver")
            driver.verify_connectivity.side_effect = RuntimeError("unreachable")
            mock_gd.driver.return_value = driver
            registry = StoreRegistry(config=config)
            with pytest.raises(RegistryValidationError) as excinfo:
                registry.validate(
                    store_types=["graph", "trace"], check_connectivity=True
                )
            labels = [label for label, _ in excinfo.value.errors]
            # Both kinds of failure surface in the same aggregate.
            assert any(label == "trace" for label in labels)
            assert any(label.startswith("neo4j-driver:") for label in labels)

    def test_connectivity_skipped_when_no_neo4j_drivers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No neo4j stores configured → connectivity branch finds an empty
        # cache and contributes zero failures, even with the flag on.
        registry = StoreRegistry(stores_dir=None, config={})
        # Should not raise (no targets to validate, no drivers to ping).
        registry.validate(store_types=[], check_connectivity=True)


class TestValidateConnectivityViaEnv:
    def test_env_var_enables_ping_without_kwarg(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _silence_init_schema(monkeypatch)
        monkeypatch.setenv("TRELLIS_VALIDATE_CONNECTIVITY", "1")
        with patch("trellis.stores.neo4j.base.GraphDatabase") as mock_gd:
            driver = MagicMock(name="driver")
            mock_gd.driver.return_value = driver
            registry = StoreRegistry(config=_neo4j_only_config())
            registry.validate(store_types=["graph"])
            driver.verify_connectivity.assert_called_once()

    def test_explicit_false_overrides_truthy_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _silence_init_schema(monkeypatch)
        monkeypatch.setenv("TRELLIS_VALIDATE_CONNECTIVITY", "1")
        with patch("trellis.stores.neo4j.base.GraphDatabase") as mock_gd:
            driver = MagicMock(name="driver")
            mock_gd.driver.return_value = driver
            registry = StoreRegistry(config=_neo4j_only_config())
            registry.validate(store_types=["graph"], check_connectivity=False)
            driver.verify_connectivity.assert_not_called()
