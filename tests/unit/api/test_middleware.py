"""Tests for request-ID middleware + structured error envelope."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, NoReturn

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

from tests.structlog_isolation import reset_structlog_global_state
from trellis.errors import StoreError, TrellisError
from trellis_api.logging import _UVICORN_LOGGERS, configure_logging
from trellis_api.middleware import (
    REQUEST_ID_HEADER,
    request_id_middleware,
    trellis_error_handler,
    unhandled_exception_handler,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

#: A unique violation's DETAIL names the key's value. This one is synthetic,
#: and nothing else in this module contains ``synthetic-secret``.
_DETAIL = "DETAIL: Key (name)=(synthetic-secret) already exists"
_STORE_MESSAGE = "Alias bind for node-synthetic-1 failed: _DriverError"


class _DriverError(Exception):
    """A backend driver's own exception, outside the Trellis hierarchy."""


def _driver_call() -> NoReturn:
    raise _DriverError(_DETAIL)


@pytest.fixture
def app() -> FastAPI:
    """Bare app with the middleware and both handlers wired but no routers."""
    application = FastAPI()
    application.add_middleware(BaseHTTPMiddleware, dispatch=request_id_middleware)
    application.add_exception_handler(Exception, unhandled_exception_handler)
    application.add_exception_handler(TrellisError, trellis_error_handler)

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

    @application.get("/store-error")
    def store_error() -> None:
        # A type-only StoreError chained from the driver's error, the shape
        # a store that maps a driver failure raises (#702, #713).
        try:
            _driver_call()
        except _DriverError as exc:
            raise StoreError(_STORE_MESSAGE, store="graph") from exc

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


class TestErrorLogLines:
    """What each handler writes to the operator log.

    Read as a deployment renders it, through the API's real chain in its
    default JSON format, one record per line: ``capture_logs`` on its own
    records ``exc_info`` without rendering it, and a rendered traceback is
    where a chained cause gets printed.
    """

    @pytest.fixture(autouse=True)
    def _isolate_logging(self, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
        """Leave no configured chain, root handler or uvicorn wiring behind."""
        monkeypatch.setenv("TRELLIS_LOG_LEVEL", "INFO")
        root = logging.getLogger()
        root_state = (list(root.handlers), root.level)
        uvicorn = {name: logging.getLogger(name) for name in _UVICORN_LOGGERS}
        uvicorn_state = {
            name: (list(lg.handlers), lg.propagate, lg.level)
            for name, lg in uvicorn.items()
        }
        reset_structlog_global_state()
        try:
            yield
        finally:
            reset_structlog_global_state()
            root.handlers, level = root_state
            root.setLevel(level)
            for name, (handlers, propagate, lg_level) in uvicorn_state.items():
                uvicorn[name].handlers = handlers
                uvicorn[name].propagate = propagate
                uvicorn[name].setLevel(lg_level)

    def test_a_typed_failure_logs_its_type_and_message_and_not_its_cause(
        self,
        client: TestClient,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("TRELLIS_LOG_FORMAT", "json")
        configure_logging()

        resp = client.get("/store-error", headers={REQUEST_ID_HEADER: "req-synth-1"})
        out = capsys.readouterr().err

        assert resp.status_code == 500
        assert resp.json() == {
            "code": "store_error",
            "message": _STORE_MESSAGE,
            "request_id": "req-synth-1",
            "store": "graph",
        }
        [line] = [ln for ln in out.splitlines() if "api_trellis_error" in ln]
        record = json.loads(line)
        record.pop("timestamp")  # a clock
        # No ``exception`` key: that is the field a rendered traceback fills.
        assert record == {
            "event": "api_trellis_error",
            "level": "error",
            "path": "/store-error",
            "method": "GET",
            "request_id": "req-synth-1",
            "exc_type": "StoreError",
            "error": _STORE_MESSAGE,
            "error_code": "STORE_ERROR",
            "status_code": 500,
        }
        assert "synthetic-secret" not in out

    def test_an_untyped_failure_keeps_its_traceback(
        self,
        client: TestClient,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An unexpected bug reaches the catch-all, which logs its stack.

        JSON, so the traceback is read off the ``api_unhandled_exception``
        record itself rather than from anywhere in the stream.
        """
        monkeypatch.setenv("TRELLIS_LOG_FORMAT", "json")
        configure_logging()

        resp = client.get("/boom")
        out = capsys.readouterr().err

        assert resp.status_code == 500
        [line] = [ln for ln in out.splitlines() if "api_unhandled_exception" in ln]
        record = json.loads(line)
        assert record["exc_type"] == "RuntimeError"
        # Absent unless the handler logs with the exception attached.
        stack = record.get("exception", "")
        assert stack.startswith("Traceback (most recent call last)")
        assert stack.endswith(
            "RuntimeError: internal kaboom — should not leak to client"
        )
