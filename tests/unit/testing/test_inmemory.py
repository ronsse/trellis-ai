"""Parity between the in-process testing shim and the real API app.

``trellis.testing.in_memory_client`` serves the app
:func:`trellis.testing.inmemory._build_app` builds, which mounts
:func:`trellis_api.app.create_app`'s routers and, through
:func:`trellis_api.app.register_exception_handlers`, its exception
handlers. A ``TrellisError`` and a ``NaN``-echoing validation error (#741)
answer the same status and body from both apps.

Every id here is synthetic.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

import trellis_api.app as app_module
from trellis.stores.registry import StoreRegistry
from trellis.testing.inmemory import _build_app
from trellis_api.app import create_app

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture
def registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[StoreRegistry]:
    monkeypatch.setenv("TRELLIS_AUTH_MODE", "off")
    monkeypatch.delenv("TRELLIS_API_KEY", raising=False)
    monkeypatch.delenv("TRELLIS_OPS_DETAIL", raising=False)
    reg = StoreRegistry(stores_dir=tmp_path / "stores")
    yield reg
    reg.close()


def _shim_client(registry: StoreRegistry) -> TestClient:
    """The shim's app, raising server exceptions as ``in_memory_client`` does.

    An exception no handler answers raises into the test rather than coming
    back as a 500, so a missing handler fails loudly here.
    """
    return TestClient(_build_app(registry))


def _prod_client(registry: StoreRegistry) -> TestClient:
    """The real app, same registry — mirrors tests/unit/api/test_non_finite_body.py.

    Deliberately not entered as a ``with`` block: that would run the real
    lifespan, which calls ``StoreRegistry.from_config_dir()`` and discards
    the registry passed in. Binding ``_registry`` directly is what
    ``_build_app`` itself does.
    """
    app = create_app()
    app_module._registry = registry
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _reset_registry_global() -> Iterator[None]:
    yield
    app_module._registry = None


class TestTrellisErrorParity:
    """A ``ConfigError`` escaping a route answers 409 from both apps.

    ``GET /api/version`` re-reads ``TRELLIS_OPS_DETAIL`` on every request
    (unlike the eager, startup-only validation ``create_app`` also does)
    and only consults it when the caller is unauthenticated — so
    ``TRELLIS_AUTH_MODE=required`` with no credential, plus an invalid
    value set *after* the app is built, reaches
    :func:`~trellis_api.routes.health.resolve_ops_detail` from inside the
    route rather than at app-construction time.
    """

    def test_shim_matches_create_app(
        self, registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRELLIS_AUTH_MODE", "required")

        # Build both apps before poisoning TRELLIS_OPS_DETAIL: create_app
        # validates it eagerly at construction time (see create_app's
        # startup chokepoint comment), so setting the bad value first would
        # crash create_app() here instead of exercising the per-request
        # re-read inside GET /api/version that this test targets.
        shim_client = _shim_client(registry)
        prod_client = _prod_client(registry)

        monkeypatch.setenv("TRELLIS_OPS_DETAIL", "not-a-real-value")
        shim_resp = shim_client.get("/api/version")
        prod_resp = prod_client.get("/api/version")

        # Each side's own value, so a handler dropped from the shared
        # function, which breaks both apps alike, still fails here.
        assert prod_resp.status_code == 409
        assert shim_resp.status_code == 409

        shim_body = shim_resp.json()
        prod_body = prod_resp.json()
        assert prod_body["code"] == "config_error"
        assert shim_body["code"] == "config_error"

        # request_id differs by construction: the shim installs no
        # request-id middleware, so it is null there and a minted ULID from
        # create_app. Compare everything else directly rather than pinning
        # the envelope as a literal twice.
        for body in (shim_body, prod_body):
            body.pop("request_id")
        assert shim_body == prod_body


class TestValidationErrorParity:
    """FastAPI body validation on a typed route (``POST /api/v1/packs``)."""

    def test_nan_echoing_error_matches_and_is_422_not_500(
        self, registry: StoreRegistry
    ) -> None:
        """The #741 shape: a bare ``NaN`` token the rejected value echoes.

        FastAPI's default 422 handler raises on encoding the echoed ``NaN``,
        so without ``request_validation_error_handler`` this request raises
        ``ValueError`` through the shim and answers 500 from ``create_app``.
        """
        raw = b'{"intent": "test-intent-nan", "max_items": NaN}'
        headers = {"content-type": "application/json"}

        shim_resp = _shim_client(registry).post(
            "/api/v1/packs", content=raw, headers=headers
        )
        prod_resp = _prod_client(registry).post(
            "/api/v1/packs", content=raw, headers=headers
        )

        assert prod_resp.status_code == 422
        assert shim_resp.status_code == 422
        shim_body = shim_resp.json()
        prod_body = prod_resp.json()
        assert shim_body == prod_body
        [error] = [e for e in shim_body["detail"] if e["loc"] == ["body", "max_items"]]
        assert error["input"] == "NaN"


def test_create_app_and_shim_register_the_same_handlers(
    registry: StoreRegistry,
) -> None:
    """Both apps map every exception type to the same handler.

    A handler added to or replaced in one app alone fails here; one dropped
    from both fails the tests above or ``tests/unit/api/test_boundary_errors.py``.
    """
    assert _build_app(registry).exception_handlers == create_app().exception_handlers
