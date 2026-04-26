"""Tests for ``StoreRegistry.close()`` lifecycle + context-manager protocol.

Covers behavior that is *not* Neo4j-specific (the Neo4j-driver-sharing
tests live in ``test_neo4j_driver_lifecycle.py``):

* ``__enter__`` / ``__exit__`` semantics — caller-visible context-manager
  contract.
* ``close()`` is idempotent — second call is a no-op and doesn't re-fire
  store ``close()`` methods on already-cleared caches.
* Failures in any single backend's ``close()`` are logged and skipped
  rather than blocking cleanup of the rest.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from trellis.stores.registry import StoreRegistry


def test_enter_returns_self(tmp_path: pytest.MonkeyPatch) -> None:
    registry = StoreRegistry(stores_dir=tmp_path)  # type: ignore[arg-type]
    with registry as r:
        assert r is registry


def test_exit_calls_close_on_normal_exit(tmp_path) -> None:  # type: ignore[no-untyped-def]
    registry = StoreRegistry(stores_dir=tmp_path)
    closed = MagicMock()
    registry._cache["fake"] = closed
    with registry:
        pass
    closed.close.assert_called_once()


def test_exit_calls_close_when_body_raises(tmp_path) -> None:  # type: ignore[no-untyped-def]
    registry = StoreRegistry(stores_dir=tmp_path)
    closed = MagicMock()
    registry._cache["fake"] = closed
    err = RuntimeError("boom")
    with pytest.raises(RuntimeError, match="boom"), registry:
        raise err
    # close() must still fire on exceptional exit.
    closed.close.assert_called_once()


def test_close_is_idempotent(tmp_path) -> None:  # type: ignore[no-untyped-def]
    registry = StoreRegistry(stores_dir=tmp_path)
    store = MagicMock()
    registry._cache["fake"] = store
    registry.close()
    registry.close()
    # Second call finds an empty cache; the first-call close fires once.
    store.close.assert_called_once()


def test_close_continues_past_individual_failure(tmp_path) -> None:  # type: ignore[no-untyped-def]
    registry = StoreRegistry(stores_dir=tmp_path)
    failing = MagicMock()
    failing.close.side_effect = RuntimeError("close failed")
    healthy = MagicMock()
    registry._cache["bad"] = failing
    registry._cache["good"] = healthy
    registry.close()
    # Both close() calls fired; the failing one's exception was swallowed
    # so the healthy one still got cleaned up.
    failing.close.assert_called_once()
    healthy.close.assert_called_once()
    assert registry._cache == {}


def test_close_clears_neo4j_driver_cache(tmp_path) -> None:  # type: ignore[no-untyped-def]
    registry = StoreRegistry(stores_dir=tmp_path)
    driver = MagicMock(name="shared_driver")
    registry._neo4j_drivers[("bolt://x", "neo4j")] = driver
    registry.close()
    driver.close.assert_called_once()
    assert registry._neo4j_drivers == {}


def test_close_continues_past_neo4j_driver_failure(tmp_path) -> None:  # type: ignore[no-untyped-def]
    registry = StoreRegistry(stores_dir=tmp_path)
    bad = MagicMock(name="bad_driver")
    bad.close.side_effect = RuntimeError("driver close failed")
    good = MagicMock(name="good_driver")
    registry._neo4j_drivers[("bolt://x", "u1")] = bad
    registry._neo4j_drivers[("bolt://y", "u2")] = good
    registry.close()
    bad.close.assert_called_once()
    good.close.assert_called_once()
    assert registry._neo4j_drivers == {}
