"""Test that the FastAPI lifespan invokes ``StoreRegistry.close()`` on shutdown.

This is the load-bearing test for Phase 1.2 of the Neo4j hardening
plan: it pins the contract that the lifespan exit path actually fires
``registry.close()``, which is what guarantees Neo4j drivers and other
backend connections release on uvicorn shutdown.

Doesn't require a real Neo4j — the test patches
``StoreRegistry.from_config_dir`` so we can spy on the close call.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

import trellis_api.app as app_module
from trellis_api.app import create_app


def test_lifespan_calls_registry_close_on_shutdown() -> None:
    fake_registry = MagicMock(name="StoreRegistry")
    fake_registry.validate.return_value = None

    with patch.object(
        app_module.StoreRegistry, "from_config_dir", return_value=fake_registry
    ):
        app = create_app()
        with TestClient(app) as client:
            # Lifespan startup ran; module-level _registry is the fake.
            assert app_module._registry is fake_registry
            fake_registry.validate.assert_called_once()
            fake_registry.close.assert_not_called()
            # Hit a route to confirm the app is alive (cheap unversioned probe).
            response = client.get("/healthz")
            assert response.status_code == 200
        # Exiting the TestClient context triggers lifespan shutdown.
        fake_registry.close.assert_called_once()
        # Module-level slot is cleared so a stale reference doesn't survive.
        assert app_module._registry is None


def test_lifespan_runs_validate_before_yielding() -> None:
    """Validation runs before the first request lands — fail-fast contract."""
    fake_registry = MagicMock(name="StoreRegistry")
    call_log: list[str] = []
    fake_registry.validate.side_effect = lambda: call_log.append("validate")
    fake_registry.close.side_effect = lambda: call_log.append("close")

    with patch.object(
        app_module.StoreRegistry, "from_config_dir", return_value=fake_registry
    ):
        app = create_app()
        with TestClient(app):
            call_log.append("yielded")
        call_log.append("after_close")

    assert call_log == ["validate", "yielded", "close", "after_close"]
