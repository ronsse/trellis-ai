"""Tests for TRELLIS_UI_ENABLED — static UI mount + root redirect gating."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import trellis_api.app as app_module
from trellis.errors import ConfigError
from trellis.stores.registry import StoreRegistry
from trellis_api.app import create_app, resolve_ui_enabled


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test with no exposure / auth configuration."""
    for var in (
        "TRELLIS_UI_ENABLED",
        "TRELLIS_OPS_DETAIL",
        "TRELLIS_METRICS_PUBLIC",
        "TRELLIS_AUTH_MODE",
        "TRELLIS_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def registry(tmp_path):
    """Fresh registry bound to the app module for each test."""
    reg = StoreRegistry(stores_dir=tmp_path / "stores")
    app_module._registry = reg
    yield reg
    reg.close()
    app_module._registry = None


def _client(registry) -> TestClient:
    """App over the fixture registry, without running the lifespan
    (which would re-init ``_registry`` from the real config dir)."""
    return TestClient(create_app())


class TestResolveUiEnabled:
    def test_unset_defaults_true(self) -> None:
        assert resolve_ui_enabled() is True

    @pytest.mark.parametrize("raw", ["true", "True", "TRUE", " true "])
    def test_true_case_insensitive(
        self, raw: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRELLIS_UI_ENABLED", raw)
        assert resolve_ui_enabled() is True

    @pytest.mark.parametrize("raw", ["false", "False", "FALSE", " false "])
    def test_false_case_insensitive(
        self, raw: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRELLIS_UI_ENABLED", raw)
        assert resolve_ui_enabled() is False

    @pytest.mark.parametrize("raw", ["1", "0", "yes", "no", "on", "off", "bogus"])
    def test_anything_else_raises_loudly(
        self, raw: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRELLIS_UI_ENABLED", raw)
        with pytest.raises(ConfigError, match="TRELLIS_UI_ENABLED"):
            resolve_ui_enabled()


class TestUiEnabled:
    """Default posture — back-compat, nothing changes."""

    def test_root_redirects_to_ui(self, registry) -> None:
        resp = _client(registry).get("/", follow_redirects=False)
        assert resp.status_code == 307
        assert resp.headers["location"] == "/ui/"

    def test_ui_index_served(self, registry) -> None:
        resp = _client(registry).get("/ui/")
        assert resp.status_code == 200
        assert "Trellis" in resp.text

    def test_explicit_true_same_as_default(
        self, registry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRELLIS_UI_ENABLED", "true")
        client = _client(registry)
        assert client.get("/", follow_redirects=False).headers["location"] == "/ui/"
        assert client.get("/ui/").status_code == 200


class TestUiDisabled:
    @pytest.fixture(autouse=True)
    def _disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TRELLIS_UI_ENABLED", "false")

    def test_ui_not_mounted(self, registry) -> None:
        assert _client(registry).get("/ui/").status_code == 404

    def test_root_redirects_to_version(self, registry) -> None:
        resp = _client(registry).get("/", follow_redirects=False)
        assert resp.status_code == 307
        assert resp.headers["location"] == "/api/version"

    def test_root_redirect_lands_on_version_handshake(self, registry) -> None:
        resp = _client(registry).get("/")
        assert resp.status_code == 200
        assert "api_version" in resp.json()

    def test_api_routes_unaffected(self, registry) -> None:
        # Auth defaults to "off" here — the UI toggle must not change
        # the API surface, only the static page.
        assert _client(registry).get("/api/v1/stats").status_code == 200


class TestStartupValidation:
    """Bad exposure-env values must crash create_app, not lurk."""

    def test_invalid_ui_enabled_crashes_create_app(
        self, registry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRELLIS_UI_ENABLED", "maybe")
        with pytest.raises(ConfigError, match="TRELLIS_UI_ENABLED"):
            create_app()

    def test_invalid_ops_detail_crashes_create_app(
        self, registry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRELLIS_OPS_DETAIL", "everyone")
        with pytest.raises(ConfigError, match="TRELLIS_OPS_DETAIL"):
            create_app()

    def test_invalid_metrics_public_crashes_create_app(
        self, registry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRELLIS_METRICS_PUBLIC", "totally")
        with pytest.raises(ConfigError, match="TRELLIS_METRICS_PUBLIC"):
            create_app()
