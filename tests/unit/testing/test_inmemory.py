"""Parity between the in-process testing shim and the real API app.

``trellis.testing.in_memory_client`` (and the lower-level
:func:`trellis.testing.inmemory._build_app` it wraps) mounts the same
routers as :func:`trellis_api.app.create_app`, but until #741 follow-up 5
it registered none of the three exception handlers ``create_app`` wires
up: the structured 500 envelope (:func:`unhandled_exception_handler`),
the typed-error mapping for :class:`~trellis.errors.TrellisError`
(:func:`trellis_error_handler`, #459/#443), and the non-finite-body 422
fix (:func:`request_validation_error_handler`, #741). An adopter writing
tests against the shim saw a raw traceback, FastAPI's default 422 shape,
and an unmapped ``TrellisError`` — none of which production ever answers.

:func:`trellis_api.app.register_exception_handlers` is now the one place
both apps register them from. This module pins:

* the shim's status/body equal ``create_app()``'s for three request
  shapes (a TrellisError subclass escaping a route, a finite validation
  error, and the #741 NaN-echoing shape) — each assertion also pins the
  *correct* value independently of the other side, so a mutant that
  breaks both apps identically (dropping a line from the shared
  function itself) still fails a test rather than only disagreeing with
  itself;
* both apps register exactly the same exception-handler keys.

Every id here is synthetic.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fastapi.exceptions import RequestValidationError, WebSocketRequestValidationError
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException

import trellis_api.app as app_module
from trellis.errors import TrellisError
from trellis.stores.registry import StoreRegistry
from trellis.testing.inmemory import _build_app
from trellis_api.app import create_app

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

#: The complete set create_app and the shim must both register: FastAPI's
#: own defaults (HTTPException, the two validation-error types) plus the
#: three ``register_exception_handlers`` adds.
_EXPECTED_HANDLER_KEYS = {
    Exception,
    HTTPException,
    RequestValidationError,
    WebSocketRequestValidationError,
    TrellisError,
}


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
    """The shim app, as ``in_memory_client`` builds it."""
    app = _build_app(registry)
    return TestClient(app, raise_server_exceptions=False)


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
    """A ``TrellisError`` subclass escaping a route maps to 409, not 500.

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

        # Each side's own correctness, so a mutant that drops this handler
        # from the shared function (breaking both identically) still
        # fails here rather than only passing an equality check against
        # an equally-broken sibling.
        assert prod_resp.status_code == 409
        assert shim_resp.status_code == 409

        shim_body = shim_resp.json()
        prod_body = prod_resp.json()
        assert prod_body["code"] == "config_error"
        assert shim_body["code"] == "config_error"

        # request_id differs by construction: the shim doesn't install the
        # request-id middleware (explicitly out of scope for this change —
        # see trellis/testing/inmemory.py), so it is always null there
        # where create_app's is a minted ULID. Compare everything else
        # directly rather than pinning the envelope as a literal twice.
        for body in (shim_body, prod_body):
            body.pop("request_id")
        assert shim_body == prod_body


class TestValidationErrorParity:
    """FastAPI body validation on a typed route (``POST /api/v1/packs``)."""

    def test_finite_error_matches_and_is_422(self, registry: StoreRegistry) -> None:
        body = {"intent": "test-intent-finite", "max_items": "not-a-number"}

        shim_resp = _shim_client(registry).post("/api/v1/packs", json=body)
        prod_resp = _prod_client(registry).post("/api/v1/packs", json=body)

        assert prod_resp.status_code == 422
        assert shim_resp.status_code == 422
        assert shim_resp.json() == prod_resp.json()

    def test_nan_echoing_error_matches_and_is_422_not_500(
        self, registry: StoreRegistry
    ) -> None:
        """The #741 shape: a bare ``NaN`` token the rejected value echoes.

        Before this handler is wired, FastAPI's default handler tries to
        JSON-encode the echoed ``NaN`` with ``allow_nan=False`` and raises,
        so the catch-all (where one exists) answers 500.
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


def test_create_app_and_shim_register_the_same_handler_keys(
    registry: StoreRegistry,
) -> None:
    """Both apps register exactly the handler-key set this module expects.

    Asserted against the independent, hand-written ``_EXPECTED_HANDLER_KEYS``
    (not just against each other), so a mutant that drops one handler from
    the *shared* function — which would break both apps identically —
    still fails this test instead of agreeing with its equally-broken
    sibling.
    """
    shim_app = _build_app(registry)
    prod_app = create_app()

    shim_keys = set(shim_app.exception_handlers)
    prod_keys = set(prod_app.exception_handlers)

    assert shim_keys == _EXPECTED_HANDLER_KEYS
    assert prod_keys == _EXPECTED_HANDLER_KEYS
    assert shim_keys == prod_keys
