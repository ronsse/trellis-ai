"""Live AuraDB / local-Neo4j connectivity check (env-gated).

Validates Phase 1.3 against a real Neo4j: the ping fires, succeeds
against a reachable instance, and fails fast (within ``connection_timeout``)
against an unreachable URI.

Skipped unless ``TRELLIS_TEST_NEO4J_URI`` is set, like the other
``test_neo4j_*`` suites.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("neo4j")

from trellis.stores.neo4j.base import (
    DriverConfig,
    build_driver,
    verify_connectivity,
)
from trellis.stores.registry import RegistryValidationError, StoreRegistry

URI = os.environ.get("TRELLIS_TEST_NEO4J_URI", "")
USER = os.environ.get("TRELLIS_TEST_NEO4J_USER", "neo4j")
PASSWORD = os.environ.get("TRELLIS_TEST_NEO4J_PASSWORD", "")
DATABASE = os.environ.get("TRELLIS_TEST_NEO4J_DATABASE", "neo4j")

pytestmark = [
    pytest.mark.neo4j,
    pytest.mark.skipif(not URI, reason="TRELLIS_TEST_NEO4J_URI not set"),
]


def test_verify_connectivity_returns_against_reachable_instance() -> None:
    driver = build_driver(URI, USER, PASSWORD)
    try:
        # No exception raised → reachable. The driver method itself
        # returns None on success.
        verify_connectivity(driver)
    finally:
        driver.close()


def test_validate_with_check_connectivity_passes_for_reachable() -> None:
    config = {
        "graph": {
            "backend": "neo4j",
            "uri": URI,
            "user": USER,
            "password": PASSWORD,
            "database": DATABASE,
        },
    }
    registry = StoreRegistry(config=config)
    try:
        # Should not raise — instance is reachable.
        registry.validate(store_types=["graph"], check_connectivity=True)
    finally:
        registry.close()


def test_validate_aggregates_unreachable_into_registry_error() -> None:
    # Use a config-time-valid but runtime-unreachable URI. The
    # ``connection_timeout=2`` keeps the test under ~5 seconds even if
    # the network silently drops the SYN.
    config = {
        "graph": {
            "backend": "neo4j",
            "uri": "neo4j+s://does-not-exist-trellis-test.invalid:7687",
            "user": "neo4j",
            "password": "irrelevant",
            "driver_config": {"connection_timeout": 2.0},
        },
    }
    registry = StoreRegistry(config=config)
    try:
        with pytest.raises(RegistryValidationError) as excinfo:
            registry.validate(store_types=["graph"], check_connectivity=True)
        labels = [label for label, _ in excinfo.value.errors]
        # The unreachable instance fails at __init__'s _init_schema (which
        # opens a session), not at the connectivity ping itself — either
        # way the error must surface in the aggregate. We assert on
        # ``graph`` (config-stage failure) rather than the
        # ``neo4j-driver:`` label because validate() short-circuits the
        # connectivity branch when the store cache is empty.
        assert "graph" in labels
    finally:
        registry.close()


def test_driver_config_connection_timeout_caps_unreachable_attempt() -> None:
    """Tighter DriverConfig values surface unreachable hosts faster than the
    production-default 30s timeout.
    """
    import time

    from neo4j.exceptions import DriverError

    cfg = DriverConfig(connection_timeout=1.0)
    driver = build_driver(
        "neo4j+s://does-not-exist-trellis-test.invalid:7687",
        "neo4j",
        "irrelevant",
        config=cfg,
    )
    try:
        t0 = time.monotonic()
        # DriverError is the common base for ServiceUnavailable / AuthError /
        # SecurityError — covers DNS failure, connection refused, and TLS
        # handshake failure without over-narrowing.
        with pytest.raises(DriverError):
            verify_connectivity(driver)
        elapsed = time.monotonic() - t0
        # 1s timeout + driver overhead. Generous upper bound to avoid
        # flakes on a slow CI runner.
        assert elapsed < 10.0, (
            f"connectivity ping took {elapsed:.1f}s with a 1s timeout"
        )
    finally:
        driver.close()
