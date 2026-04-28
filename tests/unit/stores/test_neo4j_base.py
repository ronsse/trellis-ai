"""Tests for ``trellis.stores.neo4j.base`` — DriverConfig + build_driver.

Pure unit tests; no live Neo4j required. The ``GraphDatabase.driver``
call is patched so we can assert the kwargs that flow through.
"""

from __future__ import annotations

import dataclasses
from unittest.mock import patch

import pytest

pytest.importorskip("neo4j")

from trellis.stores.neo4j.base import (
    DriverConfig,
    build_driver,
    check_driver_installed,
)


class TestDriverConfig:
    def test_defaults_match_documented_production_safe_values(self) -> None:
        cfg = DriverConfig()
        assert cfg.connection_timeout == 30.0
        assert cfg.max_connection_pool_size == 100
        assert cfg.max_transaction_retry_time == 30.0
        assert cfg.keep_alive is True
        assert cfg.user_agent == "trellis-ai"

    def test_is_frozen(self) -> None:
        cfg = DriverConfig()
        with pytest.raises(dataclasses.FrozenInstanceError):
            cfg.connection_timeout = 5.0  # type: ignore[misc]

    def test_overrides_individual_fields(self) -> None:
        cfg = DriverConfig(
            connection_timeout=5.0,
            max_connection_pool_size=10,
            max_transaction_retry_time=2.0,
            keep_alive=False,
            user_agent="custom",
        )
        assert cfg.connection_timeout == 5.0
        assert cfg.max_connection_pool_size == 10
        assert cfg.max_transaction_retry_time == 2.0
        assert cfg.keep_alive is False
        assert cfg.user_agent == "custom"

    def test_two_default_instances_are_equal(self) -> None:
        # Frozen dataclass should be hashable + equal-by-value so callers
        # can use it as a cache key.
        assert DriverConfig() == DriverConfig()
        assert hash(DriverConfig()) == hash(DriverConfig())


class TestBuildDriver:
    def test_passes_uri_and_auth(self) -> None:
        with patch("trellis.stores.neo4j.base.GraphDatabase") as mock_gd:
            build_driver("bolt://localhost:7687", "neo4j", "secret")
            mock_gd.driver.assert_called_once()
            args, kwargs = mock_gd.driver.call_args
            assert args == ("bolt://localhost:7687",)
            assert kwargs["auth"] == ("neo4j", "secret")

    def test_default_config_kwargs_flow_through(self) -> None:
        with patch("trellis.stores.neo4j.base.GraphDatabase") as mock_gd:
            build_driver("bolt://x", "neo4j", "p")
            kwargs = mock_gd.driver.call_args.kwargs
            assert kwargs["connection_timeout"] == 30.0
            assert kwargs["max_connection_pool_size"] == 100
            assert kwargs["max_transaction_retry_time"] == 30.0
            assert kwargs["keep_alive"] is True
            assert kwargs["user_agent"] == "trellis-ai"

    def test_custom_config_overrides_flow_through(self) -> None:
        cfg = DriverConfig(
            connection_timeout=2.0,
            max_connection_pool_size=5,
            keep_alive=False,
            user_agent="testing/1.0",
        )
        with patch("trellis.stores.neo4j.base.GraphDatabase") as mock_gd:
            build_driver("bolt://x", "u", "p", config=cfg)
            kwargs = mock_gd.driver.call_args.kwargs
            assert kwargs["connection_timeout"] == 2.0
            assert kwargs["max_connection_pool_size"] == 5
            assert kwargs["keep_alive"] is False
            assert kwargs["user_agent"] == "testing/1.0"

    def test_returns_what_graphdatabase_returns(self) -> None:
        with patch("trellis.stores.neo4j.base.GraphDatabase") as mock_gd:
            sentinel = object()
            mock_gd.driver.return_value = sentinel
            result = build_driver("bolt://x", "u", "p")
            assert result is sentinel


class TestCheckDriverInstalled:
    def test_passes_when_installed(self) -> None:
        # neo4j is installed (importorskip at module top would have skipped
        # this whole file otherwise), so this should be a no-op.
        check_driver_installed()

    def test_raises_with_install_hint_when_missing(self) -> None:
        with (
            patch("trellis.stores.neo4j.base.HAS_NEO4J", False),
            pytest.raises(ImportError, match=r"trellis-ai\[neo4j\]"),
        ):
            check_driver_installed()
