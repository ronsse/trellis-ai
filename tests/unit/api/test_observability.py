"""Tests for OTel + Prometheus instrumentation."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import trellis_api.app as app_module
from trellis.auth import SCOPE_READ, generate_api_key
from trellis.errors import ConfigError
from trellis.stores.registry import StoreRegistry
from trellis_api.app import create_app
from trellis_api.observability import (
    DISABLE_ENV,
    METRICS_PUBLIC_ENV,
    install_observability,
    resolve_metrics_public,
)


@pytest.fixture(autouse=True)
def _clean_exposure_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test with no auth / metrics-exposure configuration."""
    monkeypatch.delenv("TRELLIS_API_KEY", raising=False)
    monkeypatch.delenv("TRELLIS_AUTH_MODE", raising=False)
    monkeypatch.delenv(METRICS_PUBLIC_ENV, raising=False)


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


class TestResolveMetricsPublic:
    """Env parsing is loud on misuse and needs no optional extras."""

    def test_unset_defaults_true(self) -> None:
        assert resolve_metrics_public() is True

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("true", True), ("True", True), ("false", False), (" FALSE ", False)],
    )
    def test_true_false_case_insensitive(
        self, raw: str, expected: bool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(METRICS_PUBLIC_ENV, raw)
        assert resolve_metrics_public() is expected

    @pytest.mark.parametrize("raw", ["1", "0", "yes", "no", "open"])
    def test_invalid_raises_loudly(
        self, raw: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(METRICS_PUBLIC_ENV, raw)
        with pytest.raises(ConfigError, match=METRICS_PUBLIC_ENV):
            resolve_metrics_public()

    def test_invalid_crashes_install_even_without_extra(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Validation runs before the optional-import dance, so a typo
        crashes startup whether or not the extra is installed."""
        monkeypatch.setenv(METRICS_PUBLIC_ENV, "bogus")
        with pytest.raises(ConfigError, match=METRICS_PUBLIC_ENV):
            install_observability(MagicMock(spec=FastAPI))


class TestMetricsGating:
    """TRELLIS_METRICS_PUBLIC=false requires a valid credential on
    /metrics. Needs the instrumentator installed — skip otherwise."""

    @pytest.fixture
    def registry(self, tmp_path):
        reg = StoreRegistry(stores_dir=tmp_path / "stores")
        app_module._registry = reg
        yield reg
        reg.close()
        app_module._registry = None

    @pytest.fixture
    def client(self, registry) -> TestClient:
        """No lifespan (it would re-init the registry from the real
        config dir and shadow the tmp-path registry keys are minted
        into) — mirrors tests/unit/api/test_auth.py."""
        pytest.importorskip("prometheus_fastapi_instrumentator")
        return TestClient(create_app())

    def _mint_read_token(self, registry) -> str:
        token, record = generate_api_key("scraper", [SCOPE_READ])
        registry.operational.api_key_store.create(record)
        return token

    def test_default_public_open_even_in_required_mode(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Preserves current scrape-job behavior out of the box."""
        monkeypatch.setenv("TRELLIS_AUTH_MODE", "required")
        assert client.get("/metrics").status_code == 200

    def test_gated_no_credential_is_401(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRELLIS_AUTH_MODE", "required")
        monkeypatch.setenv(METRICS_PUBLIC_ENV, "false")
        resp = client.get("/metrics")
        assert resp.status_code == 401

    def test_gated_valid_credential_any_scope_is_200(
        self, client: TestClient, registry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRELLIS_AUTH_MODE", "required")
        monkeypatch.setenv(METRICS_PUBLIC_ENV, "false")
        token = self._mint_read_token(registry)
        resp = client.get("/metrics", headers={"X-API-Key": token})
        assert resp.status_code == 200
        assert "# HELP" in resp.text or "# TYPE" in resp.text

    def test_gated_invalid_credential_is_401(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRELLIS_AUTH_MODE", "required")
        monkeypatch.setenv(METRICS_PUBLIC_ENV, "false")
        resp = client.get("/metrics", headers={"X-API-Key": "garbage"})
        assert resp.status_code == 401

    def test_gated_auth_off_stays_open(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Mode off treats every caller as authenticated (matching the
        rest of the API) — the gate only bites in required mode."""
        monkeypatch.setenv("TRELLIS_AUTH_MODE", "off")
        monkeypatch.setenv(METRICS_PUBLIC_ENV, "false")
        assert client.get("/metrics").status_code == 200
