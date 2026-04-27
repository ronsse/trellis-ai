"""Tests for ``StoreRegistry.close()`` lifecycle + context-manager protocol.

Pins the caller-visible contract: ``__enter__`` / ``__exit__`` semantics,
``close()`` idempotency, and the "individual backend close failure does
not block cleanup of the rest" guarantee.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from trellis.stores.registry import StoreRegistry


def test_enter_returns_self(tmp_path: Path) -> None:
    registry = StoreRegistry(stores_dir=tmp_path)
    with registry as r:
        assert r is registry


def test_exit_calls_close_on_normal_exit(tmp_path: Path) -> None:
    registry = StoreRegistry(stores_dir=tmp_path)
    closed = MagicMock()
    registry._cache["fake"] = closed
    with registry:
        pass
    closed.close.assert_called_once()


def test_exit_calls_close_when_body_raises(tmp_path: Path) -> None:
    registry = StoreRegistry(stores_dir=tmp_path)
    closed = MagicMock()
    registry._cache["fake"] = closed
    err_msg = "boom"
    with pytest.raises(RuntimeError, match=err_msg), registry:
        raise RuntimeError(err_msg)
    # close() must still fire on exceptional exit.
    closed.close.assert_called_once()


def test_close_is_idempotent(tmp_path: Path) -> None:
    registry = StoreRegistry(stores_dir=tmp_path)
    store = MagicMock()
    registry._cache["fake"] = store
    registry.close()
    registry.close()
    # Second call finds an empty cache; the first-call close fires once.
    store.close.assert_called_once()


def test_close_continues_past_individual_failure(tmp_path: Path) -> None:
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
