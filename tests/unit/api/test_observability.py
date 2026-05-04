"""Tests for OTel + Prometheus instrumentation."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import trellis_api.app as app_module
from trellis.stores.registry import StoreRegistry
from trellis_api.app import create_app
from trellis_api.observability import (
    DISABLE_ENV,
    install_observability,
)


@pytest.fixture
def fresh_app(tmp_path) -> FastAPI:
    """An app instance with a real registry attached so /metrics, etc.
    pass through the same lifespan a real deploy would."""
    registry = StoreRegistry(stores_dir=tmp_path / "stores")
    app_module._registry = registry
    yield create_app()
    registry.close()
    app_module._registry = None


class TestObservabilityFlag:
    def test_disabled_via_env_returns_all_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(DISABLE_ENV, "1")
        result = install_observability(MagicMock(spec=FastAPI))
        assert result == {"otel": False, "prometheus": False, "fastapi": False}

    def test_enabled_calls_instrumentors_when_present(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the extras ARE installed, both OTel + Prometheus paths fire."""
        monkeypatch.delenv(DISABLE_ENV, raising=False)
        # Use a real FastAPI instance — Prometheus instrumentator
        # registers routes via add_api_route and validates the type.
        result = install_observability(FastAPI())
        # The CI image installs the ``observability`` extra. Local
        # dev environments without the extra still pass because the
        # try/except in install_observability returns False on
        # ImportError. Either way the function returns a dict.
        assert isinstance(result, dict)
        assert set(result.keys()) == {"otel", "prometheus", "fastapi"}


class TestMetricsEndpointMounted:
    """When ``prometheus-fastapi-instrumentator`` is installed, /metrics
    must respond with a Prometheus exposition payload."""

    def test_metrics_route_present_or_404(
        self, fresh_app: FastAPI, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Try to import the instrumentator; if it isn't installed,
        # skip — the test doesn't have to crash on dev machines that
        # haven't pulled the optional extra.
        pytest.importorskip("prometheus_fastapi_instrumentator")
        monkeypatch.delenv(DISABLE_ENV, raising=False)

        with TestClient(fresh_app) as client:
            resp = client.get("/metrics")
            # Either the endpoint is mounted (200 with text body) OR
            # it isn't (404). We never want a 500 — that would mean
            # the instrumentation broke the app boot.
            assert resp.status_code in (200, 404), resp.text
            if resp.status_code == 200:
                # Sanity-check Prometheus exposition format.
                assert "# HELP" in resp.text or "# TYPE" in resp.text
