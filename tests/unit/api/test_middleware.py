"""Tests for request-ID middleware + structured error envelope."""

from __future__ import annotations

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

from trellis_api.middleware import (
    REQUEST_ID_HEADER,
    request_id_middleware,
    unhandled_exception_handler,
)


@pytest.fixture
def app() -> FastAPI:
    """Bare app with both middleware pieces wired but no routers."""
    application = FastAPI()
    application.add_middleware(BaseHTTPMiddleware, dispatch=request_id_middleware)
    application.add_exception_handler(Exception, unhandled_exception_handler)

    @application.get("/echo")
    def echo() -> dict[str, str]:
        return {"hello": "world"}

    @application.get("/boom")
    def boom() -> None:
        msg = "internal kaboom — should not leak to client"
        raise RuntimeError(msg)

    @application.get("/http-error")
    def http_error() -> None:
        # FastAPI handles HTTPException itself; verify we don't
        # accidentally swallow it into the generic 500 envelope.
        raise HTTPException(status_code=418, detail="i am a teapot")

    return application


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    # ``raise_server_exceptions=False`` makes TestClient return the
    # 500 response built by the exception handler instead of re-raising
    # the underlying error. Without this, every request to /boom
    # propagates the RuntimeError out of the call and the test
    # framework reports it as an error rather than a 500 response.
    return TestClient(app, raise_server_exceptions=False)


class TestRequestIdMiddleware:
    def test_generates_id_when_none_supplied(self, client: TestClient) -> None:
        resp = client.get("/echo")
        assert resp.status_code == 200
        request_id = resp.headers.get(REQUEST_ID_HEADER)
        assert request_id is not None
        # ULIDs from generate_ulid are 26-char Crockford base32. Just
        # assert non-empty + reasonably sized so an implementation
        # change doesn't make the test brittle.
        assert len(request_id) >= 16

    def test_echoes_caller_supplied_id(self, client: TestClient) -> None:
        supplied = "req-abc-12345"
        resp = client.get("/echo", headers={REQUEST_ID_HEADER: supplied})
        assert resp.headers[REQUEST_ID_HEADER] == supplied

    def test_each_request_gets_unique_id(self, client: TestClient) -> None:
        ids = {client.get("/echo").headers[REQUEST_ID_HEADER] for _ in range(5)}
        assert len(ids) == 5


class TestUnhandledExceptionHandler:
    def test_uncaught_exception_returns_500_envelope(self, client: TestClient) -> None:
        resp = client.get("/boom")
        assert resp.status_code == 500
        body = resp.json()
        assert body["code"] == "internal_error"
        assert body["message"] == "internal server error"
        # Internal exception message must NOT leak to the client.
        assert "kaboom" not in body["message"]
        # request_id must be in the body so operators can correlate.
        assert body["request_id"] is not None

    def test_500_envelope_carries_request_id_header(self, client: TestClient) -> None:
        supplied = "req-trace-99"
        resp = client.get("/boom", headers={REQUEST_ID_HEADER: supplied})
        assert resp.status_code == 500
        assert resp.headers[REQUEST_ID_HEADER] == supplied
        assert resp.json()["request_id"] == supplied

    def test_http_exception_passes_through(self, client: TestClient) -> None:
        """HTTPException is FastAPI's signal — must not get swallowed
        by the generic 500 envelope."""
        resp = client.get("/http-error")
        assert resp.status_code == 418
        body = resp.json()
        assert body["detail"] == "i am a teapot"
        # Default FastAPI shape, NOT our envelope.
        assert "code" not in body
